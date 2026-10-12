"""SSH認証先で動く一接続・一枠の仲介。writer登録には自身のnative識別だけを使う。"""
import os
from pathlib import Path
import sys
import uuid

from .connection_profiles import TOKEN_FIELDS, profile_hash, validate_profile
from .local_ipc import encode, request, require_private, supported_os
from .registry import Registry, RegistryError, fields, identifier, require
from .ssh_protocol import CLI_COMMANDS, COMMANDS, ConnectionJournal, read_frame, write_frame
from .supervisor import new_request
from . import runtime_process
from .writer_api import Monitor


class Broker:
    def __init__(self, root, profile, instance_id, client_id, *, call=None, identity=None):
        supported_os()
        validate_profile(profile)
        require(profile["transport"] == "ssh" and profile["targetOs"] == {"win32": "windows", "darwin": "macos"}[sys.platform],
                "TARGET_MISMATCH", "対象ホストOSが一致しません")
        require(Path(profile["managementRoot"]) == root and Path(profile["python"]).resolve() == Path(sys.executable).resolve()
                and Path(profile["cli"]).resolve() == Path(__file__).parents[1] / "dev-server.py",
                "TARGET_MISMATCH", "承認済みroot/Python/CLIが一致しません")
        identifier(instance_id, "instanceId")
        identifier(client_id, "clientId")
        inventory = Registry(root).inventory
        require(instance_id in {item["instanceId"] for item in profile["instances"]}
                and instance_id in {item["instanceId"] for item in inventory["instances"]},
                "NOT_FOUND", "承認済み枠が登録されていません")
        require_private(root / "manager", directory=True)
        self.identity = (identity or runtime_process.identity)(os.getpid())
        require(self.identity is not None, "PROCESS_UNVERIFIED", "仲介のnative識別が必要です")
        self.instance = instance_id
        self.call = call or (lambda command, payload, operation: request(root, new_request(command, payload, operation_id=operation), timeout=35))
        self.journal = ConnectionJournal(root / "manager/ssh-connections",
                                         {"profileHash": profile_hash(profile), "instanceId": instance_id,
                                          "clientId": client_id, "pid": self.identity["pid"],
                                          "startMillis": self.identity["startMillis"], "osOwner": self.identity["owner"]})
        self.binding = None
        self.owner = None
        self.monitor = None
        self.state = "READY"

    def _result(self, command, payload, operation):
        response = self.call(command, payload, operation)
        require(response.get("operationId") == operation and type(response.get("ok")) is bool,
                "IPC_UNAVAILABLE", "管理CLI応答の識別が一致しません")
        if not response["ok"]:
            error = response["error"]
            raise RegistryError(error["code"], "管理CLIが要求を拒否しました",
                                rejected_before_effect=error.get("rejectedBeforeEffect") is True)
        return response["result"]

    def _checked(self):
        token = {key: self.binding[key] for key in TOKEN_FIELDS}
        checked = self._result("lease-check", dict(token, owner=self.owner), uuid.uuid4().hex)
        require(checked["state"] == "RUNNING" and checked["runId"] == self.binding["runId"]
                and checked["nonce"] == self.binding["nonce"] and checked["pending"] == 0
                and not any(item["status"] == "UNKNOWN" for item in checked["writerSessions"]),
                "STALE_RUN", "現在の貸出・世代・起動を確認できません")
        self._result("writer-check", self.binding, uuid.uuid4().hex)
        return checked

    def _validate(self, command, payload):
        require(self.state not in {"UNKNOWN", "CLOSED"}, "CONNECTION_PENDING", "結果不明・終了済みの接続は再利用できません")
        require(type(command) is str and command in COMMANDS, "INVALID_INPUT", "SSH仲介の許可対象外です")
        require(type(payload) is dict, "INVALID_INPUT", "payload objectが必要です")
        if command in CLI_COMMANDS:
            require(payload.get("instanceId") == self.instance, "TARGET_MISMATCH", "接続に固定した枠が必要です")
            require(command != "release" or self.binding is None, "WRITER_PENDING", "writer接続中は返却できません")
            return
        if command == "open":
            fields(payload, TOKEN_FIELDS | {"owner", "sessionId", "runId", "nonce"})
            require(payload["instanceId"] == self.instance and self.binding is None, "TARGET_MISMATCH", "未接続の同じ枠だけを開けます")
            for name in ("owner", "sessionId", "runId", "nonce"):
                identifier(payload[name], name)
            return
        if command == "close" and self.binding is None:
            fields(payload, set())
            return
        require(self.binding is not None, "NOT_CONNECTED", "writer接続が必要です")
        fields(payload, {"request"} if command == "io" else {"intervalTicks"} if command == "monitor-open"
               else {"timeoutSeconds"} if command == "monitor-read" else set())
        if command == "monitor-read":
            require(type(payload["timeoutSeconds"]) is int and 0 < payload["timeoutSeconds"] <= 30,
                    "INVALID_INPUT", "監視待機は1〜30秒にしてください")
        if command == "monitor-open":
            require(type(payload["intervalTicks"]) is int and 1 <= payload["intervalTicks"] <= 1200
                    and self.monitor is None, "INVALID_INPUT", "未購読の更新間隔は1〜1200tickにしてください")

    def execute(self, document):
        operation = None
        entry = None
        try:
            fields(document, {"version", "command", "operationId", "payload"})
            require(type(document["version"]) is int and document["version"] == 1, "INVALID_INPUT", "protocol version=1が必要です")
            operation = identifier(document["operationId"], "operationId")
            command, payload = document["command"], document["payload"]
            self._validate(command, payload)
            entry = self.journal.accepted(document)
            if command in CLI_COMMANDS:
                result = self._result(command, payload, operation)
            elif command == "open":
                token = {key: payload[key] for key in TOKEN_FIELDS}
                checked = self._result("lease-check", dict(token, owner=payload["owner"]), uuid.uuid4().hex)
                require(checked["state"] == "RUNNING" and checked["runId"] == payload["runId"] and checked["nonce"] == payload["nonce"]
                        and not any(item["status"] == "UNKNOWN" for item in checked["writerSessions"]),
                        "STALE_RUN", "貸出に固定した起動を確認できません")
                self.binding = {key: value for key, value in payload.items() if key != "owner"}
                self.owner = payload["owner"]
                result = self._result("writer-open", dict(self.binding, pid=self.identity["pid"],
                                      startMillis=self.identity["startMillis"], cwd=self.identity["cwd"]), operation)
                self.state = "OPEN"
            elif command == "io":
                result = self._result("writer-io", dict(self.binding, request=payload["request"]), operation)
            elif command == "monitor-open":
                checked = self._checked()
                self.monitor = Monitor(checked["managementEndpoint"], verify=self._checked)
                result = self.monitor.subscribe(payload["intervalTicks"], operation)
            elif command == "monitor-read":
                require(self.monitor is not None, "NOT_CONNECTED", "監視socketがありません")
                result = self.monitor.next(payload["timeoutSeconds"])
            elif command == "monitor-close":
                require(self.monitor is not None, "NOT_CONNECTED", "監視socketがありません")
                self.monitor.close(operation)
                self.monitor = None
                result = {"disconnected": True}
            else:
                if self.monitor is not None:
                    self.monitor.close(operation)
                    self.monitor = None
                result = (self._result("writer-close", dict(self.binding, drained=True, disconnected=True), operation)
                          if self.binding is not None else {"closed": True})
                self.state = "CLOSED"
            response = {"ok": True, "operationId": operation, "result": result}
            encode(response)
        except RegistryError as error:
            detail = {"code": error.code, "message": "接続契約または管理操作を確認してください"}
            if entry is None or error.rejected_before_effect:
                detail["rejectedBeforeEffect"] = True
            response = {"ok": False, "operationId": operation, "error": detail}
        except (OSError, ValueError, TypeError, KeyError, EOFError):
            response = {"ok": False, "operationId": operation, "error": {"code": "UNKNOWN", "message": "仲介操作の完了が不明です"}}
        if entry is not None:
            self.journal.finished(entry, response)
            if not response["ok"] and not response["error"].get("rejectedBeforeEffect"):
                self.state = "UNKNOWN"
            self.journal.state(self.state)
        return response

    def abandon(self):
        self.state = "UNKNOWN"
        self.journal.state("UNKNOWN")
        if self.monitor is not None:
            self.monitor.abort()


def serve(root, incoming, outgoing):
    """stdoutはframe専用。EOF/壊れた要求/送信喪失でwriterや貸出を自動解除しない。"""
    broker = None
    try:
        hello = read_frame(incoming)
        fields(hello, {"version", "command", "operationId", "payload"})
        require(type(hello["version"]) is int and hello["version"] == 1 and hello["command"] == "hello",
                "INVALID_INPUT", "helloが必要です")
        operation = identifier(hello["operationId"], "operationId")
        fields(hello["payload"], {"profile", "instanceId", "clientId"})
        broker = Broker(root, **{ "profile": hello["payload"]["profile"], "instance_id": hello["payload"]["instanceId"],
                                "client_id": hello["payload"]["clientId"]})
        write_frame(outgoing, {"ok": True, "operationId": operation, "result": {"profileHash": profile_hash(hello["payload"]["profile"]),
                    "brokerPid": broker.identity["pid"], "osOwner": broker.identity["owner"]}})
        while True:
            response = broker.execute(read_frame(incoming))
            write_frame(outgoing, response)
            if broker.state == "CLOSED":
                return 0
    except (OSError, EOFError, ValueError, TypeError, KeyError, RegistryError):
        if broker is not None:
            broker.abandon()
        print("SSH仲介の終了を確認できません。private証跡を確認してください。", file=sys.stderr)
        return 1
