"""人間用一括操作。変更の正本は管理CLIで、再送要求と秘密はGit外へ保全する。"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import uuid

from .build_lock import BuildLockError, build_lock
from .connection_profiles import ConnectionStore, profile_hash, validate_profile
from .local_ipc import MAX_MESSAGE, decode, encode, require_private, supported_os
from .registry import Registry, RegistryError, identifier, require, ttl
from .runtime_bundle import regular_bytes, verify_bundle
from .runtime_ports import instance_endpoints, prebind
from . import runtime_process, storage


class HumanStore:
    """同じprivate領域のprofile、枠ごとの貸出、要求ごとのjournalを分離する。"""
    def __init__(self, directory: Path):
        self.connections = ConnectionStore(directory)
        self.directory = directory
        for name in ("sessions", "actions", "supervisors"):
            path = directory / name
            if not path.exists():
                storage.mkdir(path)
            require_private(path, directory=True)
        self.lock = directory / "human.lock"
        if not self.lock.exists():
            storage.write(self.lock, b"0")
        require_private(self.lock)

    def path(self, kind, name):
        return self.connections._path(kind, name)

    def read(self, kind, name):
        return self.connections._read(self.path(kind, name))

    def write(self, kind, name, record):
        path = self.path(kind, name)
        self.connections._write(path, record, replace=path.exists())


class LocalTarget:
    """承認済み同一ホストのCLIだけを呼ぶ。SSHは別入口から仲介する。"""
    def __init__(self, profile: dict, *, via_broker=False):
        validate_profile(profile)
        supported_os()
        require(profile["transport"] == ("ssh" if via_broker else "local"),
                "HOST_MISMATCH", "SSH profileは確認済み仲介だけで実行できます")
        require(profile["targetOs"] == {"win32": "windows", "darwin": "macos"}[sys.platform]
                and (via_broker or profile["host"].casefold() == socket.gethostname().casefold()),
                "HOST_MISMATCH", "profileのOS・hostが実行ホストと一致しません")
        self.profile = profile
        self.root = Path(profile["managementRoot"])
        self.children = []
        require(Path(profile["cli"]).is_file(), "UNAVAILABLE", "profileの管理CLIがありません")
        try:
            process = subprocess.run([profile["python"], "-B", "-c",
                                      "import json,os,struct,sys; from dev_server.runtime_process import identity; "
                                      "print(json.dumps({'version':'%d.%d/%d' % (*sys.version_info[:2],struct.calcsize('P')*8),"
                                      "'executable':identity(os.getpid())['executable']}))"],
                                     cwd=Path(profile["cli"]).parent, capture_output=True, timeout=10)
            probe = decode(process.stdout)
            require(process.returncode == 0 and probe["version"] == "3.12/64",
                    "UNAVAILABLE", "対象ホストの64bit Python 3.12が必要です")
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            raise RegistryError("UNAVAILABLE", "対象ホストの64bit Python 3.12・native識別・CLIのパスを確認してください") from None
        self.python_executable = probe["executable"]
        self.registry = Registry(self.root)
        require(self.registry.inventory["operator"] == profile["approval"]["operator"],
                "APPROVAL_MISMATCH", "inventoryの運用者とprofileの承認が一致しません")
        declared = {item["instanceId"] for item in self.registry.inventory["instances"]}
        require(all(item["instanceId"] in declared for item in profile["instances"]),
                "APPROVAL_MISMATCH", "枠候補と承認済みinventoryが一致しません")

    def request(self, command, payload, operation_id, *, timeout=900, dry_run=False):
        arguments = [self.profile["python"], "-B", self.profile["cli"], command,
                     "--root", str(self.root), "--operation-id", operation_id,
                     "--payload-stdin", "--timeout-seconds", str(timeout)]
        if dry_run:
            arguments.append("--dry-run")
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        self.children.append(process)
        try:
            output, _ = process.communicate(encode(payload), timeout=timeout + 2)
            require(len(output) <= MAX_MESSAGE, "UNAVAILABLE", "管理CLIの応答上限を超えました")
            response = decode(output)
            require(response.get("operationId") == operation_id and type(response.get("ok")) is bool,
                    "UNAVAILABLE", "管理CLIの応答識別が一致しません")
            if not response["ok"]:
                code = response.get("error", {}).get("code", "UNAVAILABLE")
                if type(code) is not str or re.fullmatch("[A-Z_]{1,64}", code) is None:
                    code = "UNAVAILABLE"
                raise RegistryError(code, "管理CLIが拒否しました。journalと管理rootの証跡を確認してください")
            require(process.returncode == 0 and type(response.get("result")) is dict,
                    "UNAVAILABLE", "管理CLIの応答が不正です")
            return response["result"]
        except (ValueError, OSError, subprocess.TimeoutExpired):
            raise RegistryError("UNAVAILABLE", "応答が不明です。同じ要求の明示再送または証跡照合が必要です") from None

    def shutdown(self, store):
        """自分が生成した同じ監督だけを、全枠の返却をRegistry内でも確認して停止する。"""
        with build_lock(store.lock):
            key = self.profile["profileId"]
            record = store.read("supervisors", key)
            require(record["profileHash"] == profile_hash(self.profile) and record["state"] in {"READY", "STOPPED"},
                    "SUPERVISOR_UNKNOWN", "この入口が生成した監督の完成記録が必要です")
            actual = runtime_process.identity(record["pid"])
            if actual is None:
                step = record.get("shutdown")
                require(step is not None, "SUPERVISOR_UNKNOWN", "監督の停止要求が記録されていません")
                fingerprint = hashlib.sha256(json.dumps({"command": "shutdown-idle", "payload": step["payload"]},
                                                         sort_keys=True).encode()).hexdigest()
                with self.registry.transaction(read_only=True) as connection:
                    completed = connection.execute("SELECT fingerprint,response FROM operations WHERE id=?",
                                                   (step["operationId"],)).fetchone()
                    boot = connection.execute("SELECT value FROM meta WHERE key='supervisorBoot'").fetchone()
                require(completed is not None and completed["fingerprint"] == fingerprint
                        and boot is not None and boot[0] == record["bootId"]
                        and json.loads(completed["response"]) == {"shutdown": True, "bootId": record["bootId"]},
                        "SUPERVISOR_UNKNOWN", "同じ監督の正常停止確定を照合できません")
                record["state"] = "STOPPED"
                store.write("supervisors", key, record)
                return {"state": "STOPPED", "evidence": str(store.path("supervisors", key))}
            require(record["state"] == "READY", "SUPERVISOR_UNKNOWN", "終了記録の監督が生存しています。証跡を保全してください")
            runtime_process.require_identity(record["identity"], actual)
            step = record.get("shutdown")
            if step is None:
                step = {"operationId": uuid.uuid4().hex, "payload": {"expectedBootId": record["bootId"]}}
                record["shutdown"] = step
                store.write("supervisors", key, record)
            self.request("shutdown-idle", step["payload"], step["operationId"], timeout=10)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                actual = runtime_process.identity(record["pid"])
                if actual is None:
                    record["state"] = "STOPPED"
                    store.write("supervisors", key, record)
                    return {"state": "STOPPED", "evidence": str(store.path("supervisors", key))}
                runtime_process.require_identity(record["identity"], actual)
                time.sleep(0.1)
            raise RegistryError("SUPERVISOR_UNKNOWN", "監督終了が未確認です。所有記録を保全しています")

    def selection(self, mode, options):
        if mode == "prepare":
            require(type(options["seed"]) is int and -(2**63) <= options["seed"] < 2**63,
                    "INVALID_INPUT", "seedは符号付き64bit整数にしてください")
        elif mode in ("restore", "reset"):
            if mode == "reset":
                from .template_control import TemplateControl
                source = TemplateControl(self.registry).registered(identifier(options["templateId"], "templateId"))
            else:
                source = self.registry.snapshot._registered(identifier(options["snapshotId"], "snapshotId"))
            require(source["artifactId"] == options["artifactId"], "ARTIFACT_MISMATCH",
                    "snapshot/templateは同じ固定artifactだけを使用できます")

    def bound_artifact(self, instance, generation):
        identifier(generation, "generation")
        active = self.root / "instances" / instance / "active.json"
        require_private(active)
        record = decode(active.read_bytes())
        require(record["generationId"] == generation, "STALE_GENERATION", "active世代が一致しません")
        return record["artifactId"]

    def status(self, instance=None):
        return self.request("status" if instance else "list",
                            {"instanceId": instance} if instance else {}, uuid.uuid4().hex, timeout=10)

    def artifact(self, artifact_id):
        require(type(artifact_id) is str and re.fullmatch(r"[0-9a-f]{64}", artifact_id),
                "INVALID_INPUT", "固定artifactの64桁SHA-256を指定してください")
        bundle = self.root / "artifacts" / artifact_id
        manifest = verify_bundle(bundle)
        descriptor = decode(regular_bytes(bundle / "launch/profiles.json"))
        require(manifest["artifactId"] == artifact_id and descriptor["platform"] == platform.system()
                and descriptor["machine"] == platform.machine(),
                "ARTIFACT_MISMATCH", "固定artifactの識別・作成OSが一致しません")
        return manifest

    def declaration(self, instance):
        return next(item for item in self.registry.inventory["instances"] if item["instanceId"] == instance)

    def choices(self):
        from .template_control import TemplateControl
        templates = []
        for path in sorted((self.root / "templates").glob("*.json")):
            template = TemplateControl(self.registry).registered(path.stem)
            templates.append({"templateId": template["templateId"], "displayName": template["templateId"],
                              "artifactId": template["artifactId"]})
        return {"profile": self.profile["displayName"], "instances": self.profile["instances"], "templates": templates}

    def doctor(self, instance, artifact_id=None):
        manifest = self.artifact(artifact_id) if artifact_id else None
        java = None
        if manifest:
            java = next((tool["version"] for tool in manifest["tools"] if tool["name"] == "jdk"), None)
            require(type(java) is str and java.startswith("21."), "JDK_REQUIRED", "固定artifactにJDK 21が必要です")
        return {"host": socket.gethostname(), "targetOs": self.profile["targetOs"], "python": "3.12/64",
                "managementRoot": str(self.root), "instanceId": instance,
                "endpoints": instance_endpoints(self.declaration(instance)), "java": java,
                "native": runtime_process.identity(os.getpid()), "approval": self.profile["approval"]["reference"]}

    def bind_supervisor(self, record):
        actual = runtime_process.identity(record["pid"])
        expected = record["identity"]
        if sys.platform == "darwin" and expected != actual:
            arguments = [self.python_executable, "-B", self.profile["cli"], "supervise", "--root", str(self.root)]
            expected = dict(expected, executable=self.python_executable,
                            commandHash=runtime_process.command_hash(arguments))
        runtime_process.require_identity(expected, actual)
        record["identity"] = actual

    def ensure_supervisor(self, store, action):
        try:
            self.status()
            path = store.path("supervisors", self.profile["profileId"])
            if path.exists():
                record = store.read("supervisors", self.profile["profileId"])
                require(record["profileHash"] == profile_hash(self.profile), "PROFILE_CHANGED", "監督の固定設定が変わっています")
                if record["state"] == "CREATING":
                    require(record["identity"] is not None, "PROCESS_UNVERIFIED", "途中生成の監督所有識別が未記録です")
                    self.bind_supervisor(record)
                    log = store.directory / "supervisors" / (self.profile["profileId"] + ".log")
                    require_private(log)
                    ready = decode(log.read_bytes().splitlines()[0]).get("result", {})
                    with self.registry.transaction(read_only=True) as connection:
                        boot = connection.execute("SELECT value FROM meta WHERE key='supervisorBoot'").fetchone()
                    require(ready.get("supervisor") == "ready" and ready.get("pid") == record["pid"]
                            and boot is not None and ready.get("bootId") == boot[0],
                            "SUPERVISOR_UNKNOWN", "途中生成の監督起動証跡が一致しません")
                    record.update(state="READY", bootId=ready["bootId"])
                    store.write("supervisors", self.profile["profileId"], record)
            return
        except RegistryError as error:
            require(error.code == "UNAVAILABLE", error.code, "監督の診断が必要です")
        lock = self.root / "manager/locks/supervisor.lock"
        require_private(lock)
        try:
            with build_lock(lock):
                pass
        except BuildLockError:
            raise RegistryError("SUPERVISOR_UNKNOWN", "既存監督がlockを所有しています。応答喪失では別監督を生成しません") from None
        key = self.profile["profileId"]
        path = store.path("supervisors", key)
        if path.exists():
            previous = store.read("supervisors", key)
            require(previous["profileHash"] == profile_hash(self.profile) and previous["state"] == "STOPPED",
                    "SUPERVISOR_UNKNOWN", "前回の監督所有・終了証跡の確認が必要です")
            require(runtime_process.identity(previous["pid"]) is None, "SUPERVISOR_UNKNOWN", "前回の監督終了を確認できません")
            archive = key + "-" + uuid.uuid4().hex
            storage.move(path, store.path("supervisors", archive))
            old_log = store.directory / "supervisors" / (key + ".log")
            if old_log.exists():
                storage.move(old_log, store.directory / "supervisors" / (archive + ".log"))
        record = {"profileHash": profile_hash(self.profile), "actionId": action["actionId"],
                  "state": "CREATING", "pid": None, "identity": None, "bootId": None}
        store.write("supervisors", key, record)
        log = store.directory / "supervisors" / (key + ".log")
        require(not log.exists(), "SUPERVISOR_UNKNOWN", "監督の前回ログを上書きしません")
        storage.write(log, b"")
        arguments = [self.profile["python"], "-B", self.profile["cli"], "supervise", "--root", str(self.root)]
        options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_BREAKAWAY_FROM_JOB}
                   if os.name == "nt" else {"start_new_session": True})
        with log.open("ab") as output:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                       cwd=Path(self.profile["cli"]).parent, **options)
        self.children.append(process)
        record["pid"] = process.pid
        store.write("supervisors", key, record)
        record["identity"] = runtime_process.identity(process.pid)
        require(record["identity"] is not None, "PROCESS_UNVERIFIED", "生成した監督のnative識別を取得できません")
        store.write("supervisors", key, record)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            require(process.poll() is None, "SUPERVISOR_UNKNOWN", "生成した監督が終了しました。ログを保全しています")
            require_private(log)
            first = log.read_bytes().splitlines()
            if first:
                ready = decode(first[0]).get("result", {})
                require(ready.get("supervisor") == "ready" and ready.get("pid") == process.pid,
                        "SUPERVISOR_UNKNOWN", "監督の起動応答と所有PIDが一致しません")
                self.bind_supervisor(record)
                record.update(state="READY", bootId=ready["bootId"])
                store.write("supervisors", key, record)
                self.status()
                return
            time.sleep(0.1)
        raise RegistryError("SUPERVISOR_UNKNOWN", "監督接続期限を超えました。新規生成・killを行いません")


class HumanServer:
    """start/stop等をjournal順に進める。途中失敗は貸出・データと要求を残す。"""
    def __init__(self, store: HumanStore, profile_id: str, owner: str, instance: str, *, target=None):
        self.store = store
        self.profile = store.connections.profile(profile_id)
        self.owner = identifier(owner, "owner")
        self.instance = identifier(instance, "instanceId")
        require(instance in {item["instanceId"] for item in self.profile["instances"]},
                "INVALID_INPUT", "profileに登録した枠を名前またはIDで選んでください")
        self.target = target or LocalTarget(self.profile)
        self.key = hashlib.sha256(encode({"profileId": profile_id, "instanceId": instance})).hexdigest()
        self.session = None

    def load(self):
        path = self.store.path("sessions", self.key)
        if path.exists():
            self.session = self.store.read("sessions", self.key)
            require(self.session["profileHash"] == profile_hash(self.profile), "PROFILE_CHANGED", "固定設定が変わっています")
            require(self.session["owner"] == self.owner, "OWNER_MISMATCH", "他ownerの記録を使用できません")
        else:
            self.session = {"profileHash": profile_hash(self.profile), "owner": self.owner,
                            "token": None, "expiresAt": None, "actionId": None, "renewOwner": self.owner}
        return self.session

    def save(self):
        self.store.write("sessions", self.key, self.session)

    def checked(self):
        require(self.session["token"] is not None, "NO_LEASE", "この入口で明示取得した貸出が必要です")
        return self.target.request("lease-check", dict(self.session["token"], owner=self.owner), uuid.uuid4().hex, timeout=10)

    def row(self):
        result = self.target.status(self.instance)
        return result["instances"][0]

    def plan(self, command, options):
        row = self.row()
        token = self.session["token"]
        steps = []
        if command == "start":
            mode = options["mode"]
            self.target.selection(mode, options)
            if token is None:
                require(options["acquire"] is True, "ACQUIRE_REQUIRED", "初回・返却後は--acquireを明示してください")
                require(row["owner"] is None and row["state"] in ("AVAILABLE", "RECOVERY_REQUIRED"),
                        "UNAVAILABLE", "他ownerの貸出・未知状態を使用しません")
                require(mode != "keep", "RESTORE_REQUIRED", "新規借用はprepareまたは明示restore/resetが必要です")
                require((mode == "prepare") == (row["generation"] is None),
                        "RESTORE_REQUIRED", "空枠はprepare、既存データは明示restore/resetを選んでください")
                if row["state"] == "RECOVERY_REQUIRED":
                    recovery = {"instanceId": self.instance, "leaseId": None, "epoch": row["epoch"],
                                "expectedGeneration": row["generation"], "action": "resume",
                                "targetOperationId": None, "ttlSeconds": options["ttlSeconds"]}
                    steps += [("recover", recovery, True), ("recover", recovery, False)]
                steps.append(("acquire", {"instanceId": self.instance, "owner": self.owner,
                                         "ttlSeconds": options["ttlSeconds"]}, False))
            else:
                current = self.checked()
                require(not options["acquire"], "LEASE_EXISTS", "既存貸出へ二重acquireしません")
                require(current["state"] in ("READY", "LEASED_STOPPED"), "INVALID_STATE", "同じ貸出の停止枠だけ起動できます")
                self.no_writers(current)
                require(mode != "keep" or current["generationBound"], "RESTORE_REQUIRED", "返却後は明示restore/resetが必要です")
                if mode == "keep":
                    require(self.target.bound_artifact(self.instance, current["generation"]) == options["artifactId"],
                            "ARTIFACT_MISMATCH", "保持する世代と指定artifactが一致しません")
            if mode != "keep":
                require(options["acceptEula"] is True, "EULA_REQUIRED", "世代準備には--accept-eulaが必要です")
                payload = {"artifactId": options["artifactId"], "generationId": str(uuid.uuid4()), "acceptEula": True}
                payload.update({"seed": options["seed"]} if mode == "prepare" else
                               {"templateId": options["templateId"]} if mode == "reset" else {"snapshotId": options["snapshotId"]})
                steps.append((mode, payload, False))
            steps.append(("start", {"timeoutSeconds": options["timeoutSeconds"]}, False))
        elif command in ("stop", "pause", "restart"):
            current = self.checked()
            self.no_writers(current)
            require(current["generationBound"], "RESTORE_REQUIRED", "貸出に固定した世代が必要です")
            require(current["state"] in ("RUNNING", "LEASED_STOPPED"), "INVALID_STATE", "稼働中または正常停止済みの枠が必要です")
            if command == "restart":
                require(current["state"] == "RUNNING", "INVALID_STATE", "restartは同じ有効貸出の稼働中枠を正常停止します")
            if current["state"] == "RUNNING":
                steps.append(("stop", {"timeoutSeconds": options["timeoutSeconds"]}, False))
            if command == "restart":
                steps.append(("start", {"timeoutSeconds": options["timeoutSeconds"]}, False))
            if command == "stop":
                require(bool(options["snapshotId"]) != options["withoutSnapshot"],
                        "SNAPSHOT_CHOICE_REQUIRED", "--snapshotまたは--without-snapshotを選んでください")
                if options["snapshotId"]:
                    steps.append(("snapshot", {"action": "create", "snapshotId": options["snapshotId"]}, False))
                steps.append(("release", {}, False))
        elif command == "renew":
            self.checked()
            steps.append(("renew", {"ttlSeconds": options["ttlSeconds"]}, False))
        elif command == "recover":
            require(token is not None, "NO_LEASE", "私有記録の同じ貸出だけを復旧できます")
            require(row["owner"] == self.owner and row["epoch"] == token["epoch"], "OWNER_MISMATCH", "復旧対象の貸出が一致しません")
            expected = token["expectedGeneration"]
            if row["generation"] != expected:
                action = self.store.read("actions", self.session["actionId"])
                previous_id = action["previousActionId"]
                require(previous_id is not None, "STALE_GENERATION", "期待世代と現在世代の照合が必要です")
                previous = self.store.read("actions", previous_id)
                requested = [step for step in previous["steps"]
                             if step["operationId"] == options["targetOperationId"]
                             and step["command"] in ("prepare", "restore", "reset")
                             and step["payload"]["generationId"] == row["generation"]]
                require(len(requested) == 1, "STALE_GENERATION", "現在世代が保全した世代要求と一致しません")
                expected = row["generation"]
            payload = dict(token, expectedGeneration=expected, action=options["recoveryAction"],
                           targetOperationId=options["targetOperationId"], ttlSeconds=options["ttlSeconds"])
            steps += [("recover", payload, True), ("recover", payload, False)]
        else:
            raise RegistryError("INVALID_INPUT", "一括操作名が不正です")
        return steps

    @staticmethod
    def no_writers(current):
        require(current.get("writers") == 0 and current.get("pending") == 0
                and not any(item["status"] != "CLOSED" for item in current.get("writerSessions", [])),
                "WRITER_PENDING", "Managerを正常切断し、writer終了・未完了操作を照合してください")

    def run(self, command, options, *, action_id=None, resume=False):
        """同IDは同じ入力だけ許可する。結果不明の再送はresumeを明示した場合だけ。"""
        action_id = identifier(action_id or uuid.uuid4().hex, "actionId")
        ttl(options["ttlSeconds"])
        require(type(options["timeoutSeconds"]) is int and options["timeoutSeconds"] > 0,
                "INVALID_INPUT", "起動停止期限は正整数にしてください")
        intent = {"profileHash": profile_hash(self.profile), "owner": self.owner, "instanceId": self.instance,
                  "command": command, "options": deepcopy(options)}
        with build_lock(self.store.lock):
            self.load()
            path = self.store.path("actions", action_id)
            if path.exists():
                action = self.store.read("actions", action_id)
                require(action["intent"] == intent, "OPERATION_CONFLICT", "同じactionIdへ異なる操作・選択を指定できません")
                if action["state"] == "COMPLETED":
                    if self.session["actionId"] == action_id:
                        self.session["actionId"] = None
                        self.save()
                    return action["summary"]
                require(resume and self.session["actionId"] == action_id, "ACTION_PENDING",
                        "途中要求が残っています。同じactionIdの--resumeを明示してください")
            else:
                require(not resume, "NOT_FOUND", "再送対象のactionIdがありません")
                previous_action = self.session["actionId"]
                if previous_action is not None:
                    pending = self.store.read("actions", previous_action)
                    sending = [step for step in pending["steps"] if step["state"] == "SENDING"]
                    require(command == "recover" and len(sending) == 1
                            and sending[0]["command"] in ("prepare", "restore", "reset", "snapshot")
                            and options["targetOperationId"] == sending[0]["operationId"],
                            "ACTION_PENDING", "前回要求の同ID再送、または同じ世代・snapshot操作の明示recoverが必要です")
                if command == "start":
                    self.target.artifact(options["artifactId"])
                action = {"actionId": action_id, "intent": intent, "state": "PLANNING", "steps": [], "summary": None,
                          "previousActionId": previous_action}
                self.store.write("actions", action_id, action)
                self.session["actionId"] = action_id
                self.save()
            if action["state"] == "PLANNING":
                self.target.ensure_supervisor(self.store, action)
                try:
                    planned = self.plan(command, options)
                    if command == "start":
                        with prebind(instance_endpoints(self.target.declaration(self.instance))):
                            pass
                    recovery_id = uuid.uuid4().hex
                    for name, payload, dry in planned:
                        step_id = recovery_id if name == "recover" else uuid.uuid4().hex
                        action["steps"].append({"command": name, "extra": payload, "dryRun": dry,
                                                "operationId": step_id, "payload": None, "result": None, "state": "NEW"})
                    action["state"] = "RUNNING"
                    self.store.write("actions", action_id, action)
                except (RegistryError, OSError, ValueError):
                    action["state"] = "REJECTED"
                    self.store.write("actions", action_id, action)
                    self.session["actionId"] = action["previousActionId"]
                    self.save()
                    raise
            require(action["state"] == "RUNNING", "ACTION_PENDING", "監督生成または計画段階の証跡確認が必要です")
            for step in action["steps"]:
                if step["state"] == "COMPLETED":
                    self.apply(step)
                    continue
                require(step["state"] != "SENDING" or resume, "ACTION_PENDING", "応答が不明です。明示再送が必要です")
                if step["payload"] is None:
                    token = {} if step["command"] in ("acquire", "recover") else self.session["token"]
                    step["payload"] = dict(token, **step["extra"])
                step["state"] = "SENDING"
                self.store.write("actions", action_id, action)
                result = self.target.request(step["command"], step["payload"], step["operationId"],
                                             dry_run=step["dryRun"])
                step.update(result=result, state="COMPLETED")
                self.store.write("actions", action_id, action)
                self.apply(step)
            action["summary"] = {"actionId": action_id, "state": "COMPLETED", "command": command,
                                 "instanceId": self.instance, "generation": self.session["token"]["expectedGeneration"] if self.session["token"] else None,
                                 "expiresAt": self.session["expiresAt"], "renewOwner": self.owner,
                                 "evidence": str(self.store.path("actions", action_id))}
            if action.get("previousActionId"):
                action["summary"]["resolvedActionId"] = action["previousActionId"]
                previous = self.store.read("actions", action["previousActionId"])
                previous.update(state="RESOLVED", resolvedBy=action_id)
                self.store.write("actions", previous["actionId"], previous)
            action["state"] = "COMPLETED"
            self.store.write("actions", action_id, action)
            self.session["actionId"] = None
            self.save()
            return action["summary"]

    def apply(self, step):
        if step["dryRun"]:
            return
        command, result = step["command"], step["result"]
        if command == "acquire":
            self.session["token"] = {"instanceId": result["instanceId"], "leaseId": result["leaseId"],
                                     "epoch": result["epoch"], "expectedGeneration": result["generation"]}
            self.session["expiresAt"] = result["expiresAt"]
        elif command in ("prepare", "restore", "reset", "recover") and self.session["token"]:
            self.session["token"]["expectedGeneration"] = result["generation"]
            if command == "recover":
                checked = self.target.request("lease-check", dict(self.session["token"], owner=self.owner),
                                              uuid.uuid4().hex, timeout=10)
                self.session["expiresAt"] = checked["expiresAt"]
        elif command == "renew":
            self.session["expiresAt"] = result["expiresAt"]
        elif command == "release":
            self.session["token"] = None
            self.session["expiresAt"] = None
        self.save()

    def status(self):
        self.load()
        row = self.row()
        return {"profile": self.profile["displayName"], "instance": self.instance, "state": row["state"],
                "generation": row["generation"], "owner": row["owner"], "writers": row["writers"],
                "leaseExpired": row["leaseExpired"], "renewOwner": self.session["renewOwner"],
                "expiresAt": self.session["expiresAt"], "pendingActionId": self.session["actionId"],
                "endpoints": instance_endpoints(self.target.declaration(self.instance)),
                "pendingRequest": human_failure(RegistryError("ACTION_PENDING", "途中要求"), self.store,
                                                self.session["actionId"]) if self.session["actionId"] else None,
                "evidence": str(self.target.root / "instances" / self.instance / "diagnostics")}

    def handoff_material(self):
        """human.lockを所有した呼出側へ、共通offerに必要な秘密と照合結果だけを渡す。"""
        self.load()
        require(self.session["actionId"] is None, "ACTION_PENDING", "途中の要求を先に照合してください")
        return {"token": deepcopy(self.session["token"]), "checked": self.checked(),
                "sourceClient": "human-" + self.key}

    def handoff(self, target_client):
        """明示したManager clientへ一回限りの引継ぎIDを返す。renew責任は発行元に残す。"""
        with build_lock(self.store.lock):
            material = self.handoff_material()
            checked = material["checked"]
            handoff_id = self.store.connections.offer(self.profile, self.session["token"], checked,
                                                      owner=self.owner, source_client="human-" + self.key,
                                                      target_client=identifier(target_client, "targetClient"))
            return {"handoffId": handoff_id, "expiresAt": checked["expiresAt"], "renewOwner": self.owner,
                    "directory": str(self.store.directory)}

    def logs(self, limit=16384):
        self.load()
        row = self.row()
        require(row["runId"] is not None, "NOT_FOUND", "起動ログがありません")
        identifier(row["runId"], "runId")
        path = self.target.root / "instances" / self.instance / "diagnostics" / row["runId"] / "console.log"
        require_private(path)
        require(path.is_file() and path.stat().st_nlink == 1, "UNSUPPORTED_FILE", "通常のログファイルが必要です")
        with path.open("rb") as handle:
            offset = max(0, path.stat().st_size - limit)
            handle.seek(offset)
            text = handle.read(limit).decode("utf-8", errors="replace")
            if offset:
                text = text.partition("\n")[2]
        if self.session["token"]:
            text = text.replace(self.session["token"]["leaseId"], "[秘密]")
        if row["generation"]:
            identifier(row["generation"], "generation")
            properties = self.target.root / "instances" / self.instance / "generations" / row["generation"] / "server.properties"
            require_private(properties)
            for line in properties.read_text(encoding="utf-8").splitlines():
                if line.startswith("rcon.password="):
                    secret = line.partition("=")[2]
                    if secret:
                        text = text.replace(secret, "[秘密]")
        return "".join(char for char in text if char in "\n\t" or ord(char) >= 32 and ord(char) != 127)


def human_failure(error, store, action_id):
    """入口とSSH仲介へ、秘密を含めない段階・要求識別・証跡を返す。"""
    code = getattr(error, "code", "INTERRUPTED" if isinstance(error, KeyboardInterrupt)
                   else "LOCK_BUSY" if isinstance(error, BuildLockError) else "UNAVAILABLE")
    message = str(error) if isinstance(error, RegistryError) else "設定・private権限・管理資源と証跡を確認してください"
    phase, operation_id = "事前検査", None
    try:
        action = store.read("actions", action_id)
        sending = [step for step in action["steps"] if step["state"] == "SENDING"]
        phase = sending[0]["command"] if sending else action["state"]
        operation_id = sending[0]["operationId"] if sending else None
    except (OSError, RegistryError, ValueError, KeyError):
        pass
    return {"ok": False, "error": {"code": code, "message": message}, "phase": phase,
            "actionId": action_id, "operationId": operation_id,
            "evidence": str(store.directory / "actions" / (action_id + ".json"))}
