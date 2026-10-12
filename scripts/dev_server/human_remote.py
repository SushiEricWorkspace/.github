"""SSHは人間用要求だけを仲介し、既存の一括操作・秘密記録はホスト側で実行する。"""
from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import uuid

from .build_lock import BuildLockError, build_lock
from .connection_profiles import profile_hash
from .human_server import HumanServer, HumanStore, LocalTarget, human_failure
from .local_ipc import encode
from .registry import RegistryError, fields, identifier, require, ttl
from .ssh_connection import SshConnection
from . import storage

OPTIONS = {"acquire", "artifactId", "mode", "templateId", "snapshotId", "withoutSnapshot", "acceptEula",
           "seed", "ttlSeconds", "timeoutSeconds", "recoveryAction", "targetOperationId"}
ACTIONS = {"start", "stop", "pause", "restart", "renew", "recover"}


def serve_human(broker, payload):
    """確認済み仲介のroot/枠に限定する。秘密の引継ぎ材料はprotocol内だけで返す。"""
    command = payload["command"]
    require(command in ACTIONS | {"choices", "doctor", "status", "logs", "handoff", "supervisor-stop"},
            "INVALID_INPUT", "人間用操作名が不正です")
    fields(payload["options"], OPTIONS)
    parent = broker.root / "manager/human-clients"
    if not parent.exists():
        storage.mkdir(parent)
    store = HumanStore(parent / broker.client_id)
    try:
        profile = broker.profile
        path = store.path("profiles", profile["profileId"])
        if path.exists():
            require(store.connections.profile(profile["profileId"]) == profile,
                    "PROFILE_CHANGED", "ホストに保全した固定profileが一致しません")
        else:
            store.connections.save_profile(profile)
        target = LocalTarget(profile, via_broker=True)
        server = HumanServer(store, profile["profileId"], payload["owner"], broker.instance, target=target)
        if command == "choices":
            result = target.choices()
        elif command == "doctor":
            result = target.doctor(broker.instance, payload["options"]["artifactId"])
        elif command == "status":
            result = server.status()
        elif command == "logs":
            result = {"text": server.logs()}
        elif command == "handoff":
            identifier(payload["targetClient"], "targetClient")
            with build_lock(store.lock):
                result = server.handoff_material()
        elif command == "supervisor-stop":
            result = target.shutdown(store)
        else:
            require(payload["options"]["mode"] in {"keep", "prepare", "reset", "restore"}
                    and payload["options"]["recoveryAction"] in {"resume", "rollback"},
                    "INVALID_INPUT", "起動・復旧の選択が不正です")
            result = server.run(command, payload["options"], action_id=payload["actionId"], resume=payload["resume"] and store.path("actions", payload["actionId"]).exists())
        return {"ok": True, "result": result}
    except (RegistryError, BuildLockError, OSError, ValueError, TypeError, KeyError, StopIteration, KeyboardInterrupt) as error:
        return human_failure(error, store, payload["actionId"])


class RemoteHuman:
    """接続を越えてclientId・要求を保全する。再送は同IDのresumeを明示したときだけ。"""
    def __init__(self, store, profile, instance, owner, *, known_hosts, timeout=1800):
        require(shutil.which("ssh") is not None, "SSH_REQUIRED", "OpenSSHクライアントが必要です。自動導入はしません")
        require(known_hosts is not None, "KNOWN_HOSTS_REQUIRED", "既確認known_hostsの絶対パスを指定してください")
        ttl(timeout)
        self.store, self.profile, self.instance, self.owner = store, profile, instance, owner
        self.known_hosts, self.timeout = Path(known_hosts), timeout
        identifier(owner, "owner")
        self.key = hashlib.sha256(encode({"profileId": profile["profileId"], "instanceId": instance, "owner": owner})).hexdigest()
        with build_lock(store.lock):
            directory = store.directory / "remote-clients"
            if not directory.exists():
                storage.mkdir(directory)
            path = store.path("remote-clients", self.key)
            if path.exists():
                record = store.read("remote-clients", self.key)
                require(record["profileHash"] == profile_hash(profile), "PROFILE_CHANGED", "保全したSSH固定設定が変わっています")
            else:
                record = {"profileHash": profile_hash(profile), "clientId": "human-" + uuid.uuid4().hex}
                store.write("remote-clients", self.key, record)
            self.client_id = record["clientId"]

    def exchange(self, command, options, action_id, *, resume=False, target_client=None):
        payload = {"command": command, "options": options, "owner": self.owner,
                   "actionId": action_id, "resume": resume, "targetClient": target_client}
        connection = SshConnection(self.profile, self.instance, self.client_id, known_hosts=self.known_hosts,
                                   journal_parent=self.store.directory, timeout=min(15, self.timeout))
        try:
            response = connection.request("human", payload, operation_id=action_id, timeout=self.timeout)
            require(response["ok"], "SSH_UNAVAILABLE", "SSH仲介の契約とホスト側の証跡を確認してください")
            answer = response["result"]
            require(type(answer.get("ok")) is bool, "SSH_UNAVAILABLE", "一括操作の応答が不正です")
            closed = connection.request("close", {}, operation_id=uuid.uuid4().hex, timeout=15)
            require(closed["ok"], "SSH_UNAVAILABLE", "SSH正常終了を確認できません")
            return answer
        except BaseException:
            connection._unknown()
            raise

    def pending(self):
        """human.lockを所有した呼出側が、同じ固定選択の未確認要求を照合する。"""
        pending = []
        for path in sorted((self.store.directory / "actions").glob("*.json")):
            action = self.store.read("actions", path.stem)
            intent = action["intent"]
            if (intent.get("remote") is True and intent["profileHash"] == profile_hash(self.profile)
                    and intent["owner"] == self.owner and intent["instanceId"] == self.instance
                    and action["state"] == "SENDING"):
                pending.append((path.stem, action))
        return pending

    def run(self, command, options, *, action_id, resume=False):
        identifier(action_id, "actionId")
        intent = {"remote": True, "profileHash": profile_hash(self.profile), "owner": self.owner,
                  "instanceId": self.instance, "command": command, "options": deepcopy(options)}
        with build_lock(self.store.lock):
            pending = self.pending()
            require(command == "recover" or all(key == action_id for key, _ in pending),
                    "ACTION_PENDING", "clientの未確認要求をstatusで確認し、同じactionIdで照合してください")
            path = self.store.path("actions", action_id)
            if path.exists():
                action = self.store.read("actions", action_id)
                require(action["intent"] == intent, "OPERATION_CONFLICT", "同じactionIdに異なる選択を指定できません")
                if action["state"] == "COMPLETED":
                    return action["summary"]
                require(action["state"] != "RESOLVED", "ACTION_RESOLVED", "この要求はrecoverで解決済みです。statusから現在状態を確認してください")
                require(resume, "ACTION_PENDING", "保全した同じactionIdのresumeを明示してください")
            else:
                require(not resume, "NOT_FOUND", "再送対象のactionIdがありません")
                action = {"intent": intent, "steps": [], "state": "SENDING", "summary": None}
                self.store.write("actions", action_id, action)
            answer = self.exchange(command, options, action_id, resume=resume)
            if answer["ok"]:
                action.update(state="COMPLETED", summary=answer["result"])
                resolved = answer["result"].get("resolvedActionId")
                for key, previous in pending:
                    if key == resolved:
                        previous.update(state="RESOLVED", resolvedBy=action_id)
                        self.store.write("actions", key, previous)
            else:
                action.update(state="REJECTED" if answer["phase"] in {"REJECTED", "事前検査"} else "SENDING", failure=answer)
            self.store.write("actions", action_id, action)
            if not answer["ok"]:
                error = RegistryError(answer["error"]["code"], answer["error"]["message"])
                error.human_response = answer
                raise error
            return answer["result"]

    def read(self, command, options):
        if command == "supervisor-stop":
            with build_lock(self.store.lock):
                require(not self.pending(), "ACTION_PENDING", "clientの未確認要求を先に同IDで照合してください")
                answer = self.exchange(command, options, uuid.uuid4().hex)
        else:
            answer = self.exchange(command, options, uuid.uuid4().hex)
        if not answer["ok"]:
            raise RegistryError(answer["error"]["code"], answer["error"]["message"])
        result = answer["result"]
        if command == "status":
            with build_lock(self.store.lock):
                result["clientPendingRequests"] = [
                    dict(action.get("failure") or human_failure(RegistryError("ACTION_PENDING", "clientの応答が未確認です"),
                                                               self.store, key), actionId=key)
                    for key, action in self.pending()]
        return result

    def handoff(self, target_client, options):
        identifier(target_client, "targetClient")
        with build_lock(self.store.lock):
            require(not self.pending(), "ACTION_PENDING", "clientの未確認要求を先に同IDで照合してください")
            answer = self.exchange("handoff", options, uuid.uuid4().hex, target_client=target_client)
            require(answer["ok"], "HANDOFF_REJECTED", "ホスト側の貸出・要求を照合してください")
            material = answer["result"]
            handoff_id = self.store.connections.offer(self.profile, material["token"], material["checked"],
                                                      owner=self.owner, source_client=material["sourceClient"],
                                                      target_client=target_client)
        return {"handoffId": handoff_id, "expiresAt": material["checked"]["expiresAt"],
                "renewOwner": self.owner, "directory": str(self.store.directory)}
