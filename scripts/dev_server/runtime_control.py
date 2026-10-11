"""初回prepareと固定bundleの起動/停止。保存終了証明をsnapshot cleanへ昇格しない。"""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time
import uuid

from .build_inputs import canonical, digest
from .contracts import LAYOUT_ID
from .local_ipc import decode, make_private, plain_path, require_private
from .registry import RegistryError, fields, identifier, require, ttl
from .runtime_bundle import launch_command, verify_bundle
from .runtime_ports import instance_endpoints, reserve, prebind, require_listeners, socket_keys
from . import runtime_process, runtime_rcon

TOKEN = {"instanceId", "leaseId", "epoch", "expectedGeneration"}


def write_json(path: Path, value: dict) -> None:
    """新規記録だけを作り、既存証跡や世代を上書きしない。"""
    plain_path(path)
    with path.open("xb") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    make_private(path)


def read_json(path: Path) -> dict:
    plain_path(path)
    require(path.is_file(), "PROOF_REQUIRED", "必要な管理記録がありません")
    return decode(path.read_bytes())


class RuntimeControl:
    """重いコピー・OS照合・RCON待機はinstance OS lock内、DB transaction外で行う。"""
    def __init__(self, registry, *, process_adapter=runtime_process):
        self.registry = registry
        self.process_adapter = process_adapter
        self.processes = {}

    def _paths(self, instance):
        root = self.registry.root / "instances" / instance
        plain_path(root)
        return root

    def _active(self, row):
        root = self._paths(row["id"])
        active = read_json(root / "active.json")
        require(active == {"version": 1, "generationId": row["generation"], "artifactId": row["artifact"]},
                "RECOVERY_REQUIRED", "activeとRegistryが一致しません")
        generation = root / "generations" / row["generation"]
        require_private(generation, directory=True)
        record = read_json(generation / "generation.json")
        require(record["version"] == 1 and record["layoutId"] == LAYOUT_ID
                and record["generationId"] == row["generation"] and record["artifactId"] == row["artifact"],
                "RECOVERY_REQUIRED", "世代の完成記録が一致しません")
        require(not (root / "journal.json").exists() and not (root / "staging").exists(),
                "RECOVERY_REQUIRED", "未完了の世代切替は後続エンジンで照合してください")
        for path in generation.rglob("*"):
            plain_path(path)
        return generation

    def _row(self, payload):
        with self.registry.transaction(read_only=True) as connection:
            row = dict(self.registry._instance(connection, payload["instanceId"]))
            self.registry._token(row, payload)
            return row

    def _space(self, required_bytes=0):
        used_bytes = 0
        for path in self.registry.root.rglob("*"):
            if os.name != "nt" and path == self.registry.root / "manager/supervisor.sock":
                require_private(path.parent, directory=True)
                info = path.lstat()
                require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                        and stat.S_IMODE(info.st_mode) == 0o600,
                        "IPC_UNVERIFIED", "管理socketの実体・所有権が一致しません")
                continue
            plain_path(path)
            if path.is_file():
                used_bytes += path.stat().st_size
        margin = self.registry.inventory["reserveBytes"]
        require(used_bytes + required_bytes + margin <= self.registry.inventory["quotaBytes"]
                and shutil.disk_usage(self.registry.root).free >= required_bytes + margin,
                "NO_SPACE", "runtime配備と起動余裕を確保できません。自動削除しません")

    def _record(self, run):
        with self.registry.transaction() as connection:
            connection.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                               (run["runId"], run["instanceId"], run["generationId"], json.dumps(run)))

    def _load_run(self, row):
        with self.registry.transaction(read_only=True) as connection:
            value = connection.execute("SELECT record FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            require(value is not None, "PROOF_REQUIRED", "起動記録がありません")
            return json.loads(value[0])

    def execute(self, command, payload, *, operation_id, dry_run=False):
        identifier(operation_id, "operationId")
        additional = {"prepare": {"artifactId", "generationId", "seed", "acceptEula"},
                      "start": {"timeoutSeconds"}, "stop": {"timeoutSeconds"}}
        fields(payload, TOKEN | additional[command])
        identifier(payload["instanceId"], "instanceId")
        if command == "prepare":
            identifier(payload["generationId"], "generationId")
            require(type(payload["artifactId"]) is str and re.fullmatch("[0-9a-f]{64}", payload["artifactId"]) is not None,
                    "INVALID_INPUT", "完成artifact IDが必要です")
            require(type(payload["seed"]) is int and -(2**63) <= payload["seed"] < 2**63
                    and payload["acceptEula"] is True, "INVALID_INPUT", "固定seedと運用者のEULA同意が必要です")
        else:
            ttl(payload["timeoutSeconds"])
        if command in {"start", "stop"} and not dry_run:
            self.refresh()
        fingerprint = digest(canonical({"command": command, "payload": payload}))
        replay = None
        with self.registry.transaction(read_only=True) as connection:
            for table in ("operations", "reference_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
            cached = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
            if cached:
                require(cached["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "operationIdに異なる要求を指定できません")
                row = self.registry._instance(connection, payload["instanceId"])
                self.registry._token(row, dict(payload, expectedGeneration=payload["generationId"]) if command == "prepare" else payload)
                require(cached["status"] == "COMPLETED", "RECOVERY_REQUIRED", "未確定の操作は自動再実行しません")
                response = json.loads(cached["response"])
                if command != "prepare":
                    require(row["run_id"] == response["runId"] and row["state"] == ("RUNNING" if command == "start" else "LEASED_STOPPED"),
                            "STALE_OPERATION", "別runまたは停止後へ古い結果を適用できません")
                replay = (dict(row), response)
        if replay:
            row, response = replay
            if command == "start":
                self._running(row)
            return dict(response, replayed=True, dryRun=dry_run)
        row = self._row(payload)
        generation = None
        if command == "prepare":
            require(row["state"] == "LEASED_STOPPED" and row["generation"] is None and not row["pending"],
                    "EXISTING_DATA", "初回の空枠だけをprepareできます。既存世代は上書きしません")
            root = self._paths(row["id"])
            require(not root.exists() or (root.is_dir() and not any(root.iterdir())),
                    "EXISTING_DATA", "instanceに既存データがあります")
            bundle = self.registry.root / "artifacts" / payload["artifactId"]
            manifest = verify_bundle(bundle)
            require(manifest["artifactId"] == payload["artifactId"], "ARTIFACT_MISMATCH", "指定artifactと実体が一致しません")
            require(manifest["layoutId"] == LAYOUT_ID, "LAYOUT_MISMATCH", "未対応data-layoutは配備できません")
            required_bytes = sum(item["size"] for item in manifest["files"]) + (bundle / "runtime.json").stat().st_size
            self._space(required_bytes)
        else:
            require(row["generation_bound"] and row["generation"] is not None and not row["pending"],
                    "INVALID_STATE", "このleaseへ完成generationが結び付いていません")
            generation = self._active(row)
            if command == "start":
                require(row["state"] in ("READY", "LEASED_STOPPED") and not row["writers"] and not row["children"],
                        "NOT_STOPPED", "停止・writer/child不在を確認できません")
                if row["run_id"]:
                    previous = self._load_run(row)
                    require(previous.get("jvmAndChildrenExited") and previous.get("runtimeSaveCompleted"),
                            "PROOF_REQUIRED", "前runの正常停止・保存終了を確認できません")
                    require(self.process_adapter.identity(previous["identity"]["pid"]) is None,
                            "PROCESS_MISMATCH", "前runのPIDが存在します。PID再利用も自動許可しません")
                verify_bundle(generation / "runtime")
                self._space()
            else:
                require(row["state"] == "RUNNING" and row["run_id"] in self.processes,
                        "NOT_RUNNING", "監督が所有するRUNNING runだけを停止します")
        endpoints = instance_endpoints(next(item for item in self.registry.inventory["instances"] if item["instanceId"] == row["id"]))
        if dry_run:
            if command == "stop":
                self._running(row)
            return {"dryRun": True, "wouldExecute": command, "instanceId": row["id"]}
        with self.registry.transaction() as connection:
            for table in ("operations", "reference_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
            current = self.registry._instance(connection, row["id"])
            self.registry._token(current, payload)
            require(current["state"] == row["state"] and not current["pending"], "BUSY", "枠の状態が変更されました")
            response = {"instanceId": row["id"], "pending": True}
            connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                               (operation_id, fingerprint, command, row["id"], "PENDING", json.dumps(response)))
            connection.execute("UPDATE instances SET state=?,pending=1,runtime_proof=NULL,clean_proof=NULL WHERE id=?",
                               ({"prepare": "PREPARING", "start": "STARTING", "stop": "STOPPING"}[command], row["id"]))
            connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)", (self.registry.clock(), operation_id, command))
            connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)",
                               (operation_id, "artifact:" + (payload["artifactId"] if command == "prepare" else row["artifact"]),
                                row["id"], row["lease"], row["epoch"]))
        try:
            if command == "prepare":
                result = self._prepare(row, payload, bundle)
            elif command == "start":
                result = self._start(row, payload, generation, endpoints, operation_id)
            else:
                result = self._stop(row, payload, generation)
            with self.registry.transaction() as connection:
                current = self.registry._instance(connection, row["id"])
                # prepareはDB世代をまだ変更していないため旧tokenを確認する。
                self.registry._token(current, payload)
                if command == "prepare":
                    connection.execute("UPDATE instances SET generation=?,artifact=?,generation_bound=1,state='READY',pending=0 WHERE id=?",
                                       (result["generation"], payload["artifactId"], row["id"]))
                elif command == "start":
                    connection.execute("UPDATE instances SET state='RUNNING',pending=0 WHERE id=?", (row["id"],))
                else:
                    connection.execute("UPDATE instances SET state='LEASED_STOPPED',pending=0,runtime_proof=? WHERE id=?",
                                       (json.dumps(result), row["id"]))
                connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation_id))
                connection.execute("DELETE FROM reservations WHERE operation=?", (operation_id,))
            return result
        except BaseException as error:
            code = error.code if isinstance(error, RegistryError) else "RUNTIME_FAILED"
            with self.registry.transaction() as connection:
                connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=?", (row["id"],))
                connection.execute("UPDATE runtime_operations SET status='RECOVERY_REQUIRED',response=? WHERE id=?",
                                   (json.dumps({"error": code}), operation_id))
            if isinstance(error, RegistryError):
                raise
            raise RegistryError(code, "runtime操作に失敗しました。保全し、自動kill/再送しません") from None

    def _prepare(self, row, payload, bundle):
        root = self._paths(row["id"])
        root.mkdir(exist_ok=True)
        make_private(root, directory=True)
        generations = root / "generations"
        generations.mkdir()
        make_private(generations, directory=True)
        generation = generations / payload["generationId"]
        generation.mkdir()
        make_private(generation, directory=True)
        shutil.copytree(bundle, generation / "runtime")
        verify_bundle(generation / "runtime")
        management = generation / "config/SushiEricServerMod/management"
        management.mkdir(parents=True)
        (management / "config.yml").write_text("port: " + str(next(item for item in self.registry.inventory["instances"] if item["instanceId"] == row["id"])["ports"]["management"]) + "\n", encoding="utf-8")
        declaration = next(item for item in self.registry.inventory["instances"] if item["instanceId"] == row["id"])
        properties = {"server-ip": declaration["minecraftBind"], "server-port": declaration["ports"]["minecraft"],
                      "enable-rcon": "true", "rcon.port": declaration["ports"]["rcon"], "rcon.password": secrets.token_hex(24),
                      "level-name": "world", "level-seed": payload["seed"], "enable-query": "false", "online-mode": "true"}
        path = generation / "server.properties"
        with path.open("x", encoding="utf-8") as handle:
            handle.write("".join(f"{key}={value}\n" for key, value in properties.items()))
            handle.flush()
            os.fsync(handle.fileno())
        make_private(path)
        (generation / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        record = {"version": 1, "generationId": payload["generationId"], "artifactId": payload["artifactId"],
                  "layoutId": LAYOUT_ID, "seed": payload["seed"]}
        write_json(generation / "generation.json", record)
        write_json(root / "active.json", {"version": 1, "generationId": payload["generationId"], "artifactId": payload["artifactId"]})
        return {"instanceId": row["id"], "generation": payload["generationId"], "prepared": True, "snapshotClean": False}

    def _proof(self, run, generation, *, stopped=False):
        proof = read_json(Path(run["proofPath"]))
        require(proof.get("schemaVersion") == 1 and proof.get("instanceId") == run["instanceId"]
                and proof.get("generationId") == run["generationId"] and proof.get("startNonce") == run["nonce"],
                "PROOF_MISMATCH", "Mod証跡のrun識別が一致しません")
        require(proof.get("requiresSupervisorProcessExit") is True and proof.get("requiresExternalWriterDrain") is True,
                "PROOF_MISMATCH", "Mod証跡の責務境界が一致しません")
        identity = proof["identity"]
        require(identity["pid"] == run["identity"]["pid"]
                and runtime_process.proof_start_millis(identity["processStartedAt"]) == run["identity"]["startMillis"]
                and Path(identity["cwd"]).resolve() == generation.resolve()
                and identity["artifactId"] == run["artifactId"]
                and identity["runtimeManifestSha256"] == run["manifestHash"]
                and Path(identity["runtimeManifest"]).resolve() == (generation / "runtime/runtime.json").resolve(),
                "PROOF_MISMATCH", "Mod証跡のプロセス・runtime識別が一致しません")
        expected_roots = {"world": generation / "world", "config": generation / "config",
                          "modConfig": generation / "config/SushiEricServerMod"}
        roots = proof["startup"]["roots"]
        require(roots.keys() == expected_roots.keys(), "ROOT_MISMATCH", "保存root集合が一致しません")
        for key, path in expected_roots.items():
            plain_path(Path(roots[key]))
            require(Path(roots[key]).resolve() == path.resolve(), "ROOT_MISMATCH", "実保存rootが一致しません")
        startup = proof["startup"]
        require(startup["ready"] is True and startup["managementEnabled"] is True
                and startup["managementStarted"] is True and startup["unknownWriters"] == [],
                "START_REJECTED", "Modの起動・保存構成が拒否されました")
        if stopped:
            saved = proof["saves"]
            require(proof["phase"] == "STOPPED" and proof["modSaveCompleted"] is True
                    and proof["vanillaShutdownTailReached"] is True and saved["failures"] == []
                    and saved["pendingWriters"] == 0 and saved["drained"] is True and saved["success"] is True
                    and all(saved["results"].get(key) is True for key in
                            ("management-drain", "money-history", "shop-history", "shop-stocks", "player-data")),
                    "SAVE_FAILED", "Mod/バニラ保存またはwriter drainが完了していません")
        else:
            require(proof["phase"] == "STARTED", "PROOF_MISMATCH", "起動完了phaseが一致しません")
        return proof

    def _start(self, row, payload, generation, endpoints, operation_id):
        nonce = uuid.uuid4().hex
        run_id = "run-" + nonce
        diagnostics = self._paths(row["id"]) / "diagnostics" / run_id
        diagnostics.mkdir(parents=True)
        make_private(diagnostics, directory=True)
        run = {"runId": run_id, "instanceId": row["id"], "generationId": row["generation"],
               "artifactId": row["artifact"], "nonce": nonce, "identity": None,
               "proofPath": str(diagnostics / "proof.json"), "cwd": str(generation.resolve()),
               "manifestHash": digest((generation / "runtime/runtime.json").read_bytes()), "endpoints": endpoints,
               "stopRequested": False, "jvmAndChildrenExited": False, "runtimeSaveCompleted": False}
        self._record(run)
        with self.registry.transaction() as connection:
            reserve(connection, row["id"], run_id, endpoints)
            connection.execute("UPDATE instances SET run_id=? WHERE id=?", (run_id, row["id"]))
        properties = {"instanceId": row["id"], "generationId": row["generation"], "startNonce": nonce,
                      "proofPath": run["proofPath"], "runtimeManifest": str(generation / "runtime/runtime.json")}
        require(not any(os.environ.get(key) for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH")),
                "UNREPRODUCIBLE_ENVIRONMENT", "暗黙のJVM環境入力を使用できません")
        arguments = launch_command(generation / "runtime", "server", generation,
                                   auxiliary=diagnostics / ".launch", jvm_properties=properties)
        run["commandHash"] = runtime_process.command_hash(arguments)
        self._record(run)

        def created():
            run["creation"] = {"pid": process.pid, "stage": process.stage, "ownerGroup": process.group}
            write_json(diagnostics / "creation.json", run["creation"])
            self._record(run)
            identity = self.process_adapter.identity(process.pid)
            run["identity"] = identity
            self._record(run)
            write_json(diagnostics / "process.json", {"runId": run_id, "nonce": nonce, "identity": identity})
            require(identity is not None and Path(identity["cwd"]).resolve() == generation.resolve()
                    and Path(identity["executable"]).resolve() == Path(arguments[0]).resolve()
                    and identity["commandHash"] == run["commandHash"], "PROCESS_MISMATCH", "作成したプロセス識別が一致しません")

        process = None
        try:
            # probesを閉じる瞬間のraceは、後段の実PID/socket・nonce照合で検出する。
            with prebind(endpoints):
                pass
            with (diagnostics / "console.log").open("xb") as log:
                process = self.process_adapter.OwnedProcess()
                self.processes[run_id] = process
                try:
                    process.start(arguments, generation, log, nonce, created)
                finally:
                    run["creation"] = {"pid": process.pid, "stage": process.stage, "ownerGroup": process.group}
                    self._record(run)
                deadline = time.monotonic() + payload["timeoutSeconds"]
                while True:
                    require(process.poll() is None, "START_FAILED", "起動途中でJVMが終了しました")
                    runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
                    if Path(run["proofPath"]).exists():
                        phase = read_json(Path(run["proofPath"]))["phase"]
                        if phase != "INITIALIZING":
                            proof = self._proof(run, generation)
                            require_listeners(endpoints, proof["startup"]["listeners"], self.process_adapter.sockets(process.pid))
                            runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
                            write_json(diagnostics / "startup-proof.json", proof)
                            break
                    require(time.monotonic() < deadline, "START_TIMEOUT", "起動期限を超過しました。自動killしません")
                    time.sleep(0.1)
            return {"instanceId": row["id"], "generation": row["generation"], "runId": run_id, "running": True, "snapshotClean": False}
        except BaseException:
            if process is not None and process.stage == "RUNNING":
                try:
                    if process.poll() is None:
                        self._rcon_stop(run, generation, process, payload["timeoutSeconds"])
                    self._wait_exit(run, process, payload["timeoutSeconds"])
                except (RegistryError, OSError):
                    pass
            raise

    def _rcon_stop(self, run, generation, process, timeout):
        def verify():
            runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
            expected = run["endpoints"]["rcon"]
            require(socket_keys([expected]) <= socket_keys(self.process_adapter.sockets(process.pid)),
                    "LISTENER_MISMATCH", "自身のRCON待受を確認できません")
        verify()
        require_private(generation / "server.properties")
        entries = [line.split("=", 1)[1].strip() for line in (generation / "server.properties").read_text(encoding="utf-8").splitlines()
                   if line.startswith("rcon.password=")]
        require(len(entries) == 1 and entries[0], "RCON_FAILED", "RCON秘密を一意に取得できません")
        run["stopRequested"] = True
        self._record(run)
        endpoint = run["endpoints"]["rcon"]
        runtime_rcon.stop(endpoint["address"], endpoint["port"], entries[0], timeout, verify=verify)
        run["stopAcknowledged"] = True
        self._record(run)

    def _wait_exit(self, run, process, timeout):
        deadline = time.monotonic() + timeout
        while process.poll() is None or process.members():
            require(time.monotonic() < deadline, "STOP_TIMEOUT", "JVM/child終了期限を超過しました。自動killしません")
            time.sleep(0.1)
        run["exitCode"] = process.poll()
        run["jvmAndChildrenExited"] = True
        self._record(run)
        with self.registry.transaction() as connection:
            connection.execute("DELETE FROM endpoint_reservations WHERE run=?", (run["runId"],))
        process.close()
        self.processes.pop(run["runId"], None)

    def _stop(self, row, payload, generation):
        run = self._load_run(row)
        process = self.processes[row["run_id"]]
        runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
        self._rcon_stop(run, generation, process, payload["timeoutSeconds"])
        self._wait_exit(run, process, payload["timeoutSeconds"])
        require(run["exitCode"] == 0, "SAVE_FAILED", "JVMが正常終了していません")
        proof = self._proof(run, generation, stopped=True)
        run["runtimeSaveCompleted"] = True
        run["proofHash"] = digest(Path(run["proofPath"]).read_bytes())
        self._record(run)
        result = {"instanceId": row["id"], "generation": row["generation"], "runId": run["runId"],
                  "nonce": run["nonce"], "runtimeStopped": True, "modSaveCompleted": True,
                  "stopRequested": run["stopRequested"], "stopAcknowledged": run["stopAcknowledged"], "exitCode": run["exitCode"],
                  "jvmAndChildrenExited": True, "externalWritersVerified": False, "snapshotClean": False,
                  "proofHash": run["proofHash"]}
        write_json(Path(run["proofPath"]).parent / "runtime-stop.json", result)
        return result

    def _running(self, row):
        run = self._load_run(row)
        process = self.processes.get(row["run_id"])
        require(process is not None and process.poll() is None, "PROCESS_MISMATCH", "JVMが監督下で稼働していません")
        runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
        generation = self._active(row)
        proof = self._proof(run, generation)
        require_listeners(run["endpoints"], proof["startup"]["listeners"], self.process_adapter.sockets(process.pid))

    def refresh(self):
        """異常終了をRUNNINGのまま返さない。予約/データは復旧照合まで保留する。"""
        with self.registry.transaction(read_only=True) as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM instances WHERE state='RUNNING'")]
        for row in rows:
            try:
                self._running(row)
            except (RegistryError, OSError, ValueError, KeyError):
                with self.registry.transaction() as connection:
                    connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=? AND state='RUNNING' AND run_id=?",
                                       (row["id"], row["run_id"]))
