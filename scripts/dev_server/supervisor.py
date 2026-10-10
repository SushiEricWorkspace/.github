"""CLIの寿命に依存しないローカルsupervisor。JVM制御は後続Issueで接続する。"""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import uuid

from .build_lock import BuildLockError, build_lock
from .local_ipc import MAX_MESSAGE, decode, encode, listener, plain_path, require_private
from .registry import Registry, RegistryError, fields, identifier


@contextmanager
def private_lock(path: Path):
    """初期化済みのlockを待機せず取得する。dry-runで資源を新設しない。"""
    plain_path(path)
    require_private(path)
    with build_lock(path):
        yield


def dispatch(registry: Registry, message: dict) -> dict:
    """入力全体はログへ載せない。operationIdだけを応答へ引き継ぐ。"""
    operation_id = None
    try:
        fields(message, {"version", "command", "operationId", "payload", "dryRun"})
        if type(message["version"]) is not int or message["version"] != 1 or type(message["dryRun"]) is not bool:
            raise RegistryError("INVALID_INPUT", "version=1とbooleanのdryRunが必要です")
        operation_id = identifier(message["operationId"], "operationId")
        command = message["command"]
        if type(command) is not str:
            raise RegistryError("INVALID_INPUT", "commandは文字列で指定してください")
        payload = message["payload"]
        if command == "shutdown":
            fields(payload, set())
            if not message["dryRun"]:
                registry.stop_supervision()
            return {"ok": True, "operationId": operation_id, "result": {"shutdown": not message["dryRun"], "dryRun": message["dryRun"]}}
        if command in {"acquire", "renew", "release"} and type(payload) is dict and "instanceId" in payload:
            instance = identifier(payload["instanceId"], "instanceId")
            if instance not in {row["instanceId"] for row in registry.inventory["instances"]}:
                raise RegistryError("NOT_FOUND", "instanceが登録されていません")
            with private_lock(registry.root / "manager" / "locks" / f"instance-{instance}.lock"):
                result = registry.execute(command, payload, operation_id=operation_id, dry_run=message["dryRun"])
        else:
            result = registry.execute(command, payload, operation_id=operation_id, dry_run=message["dryRun"])
        return {"ok": True, "operationId": operation_id, "result": result}
    except RegistryError as error:
        return {"ok": False, "operationId": operation_id, "error": {"code": error.code, "message": str(error)}}
    except BuildLockError:
        return {"ok": False, "operationId": operation_id, "error": {"code": "LOCK_BUSY", "message": "資源のOS lockが使用中です"}}
    except sqlite3.Error:
        return {"ok": False, "operationId": operation_id, "error": {"code": "DB_FAILURE", "message": "Registry障害です。自動再実行せず診断してください"}}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "operationId": operation_id, "error": {"code": "INVALID_STATE", "message": "入力・権限・実体の検証に失敗しました"}}


def supervise(root: Path) -> None:
    """singleton OS lockを全期間保持する。再起動で不明枠を再貸出ししない。"""
    registry = Registry(root)
    with private_lock(root / "manager" / "locks" / "supervisor.lock"):
        with listener(root) as server:
            boot = registry.begin_supervision()
            print(encode({"ok": True, "result": {"supervisor": "ready", "bootId": boot, "pid": os.getpid()}}).decode(), flush=True)
            while True:
                connection = server.accept()
                if connection is None:
                    continue
                try:
                    if not connection.poll(5):
                        continue
                    message = decode(connection.recv_bytes(MAX_MESSAGE))
                    response = dispatch(registry, message)
                    connection.send_bytes(encode(response))
                    if response.get("ok") and response["result"].get("shutdown"):
                        return
                except (OSError, EOFError, ValueError):
                    # 壊れた接続はRegistry操作へ変換しない。payloadや秘密を出力しない。
                    pass
                finally:
                    connection.close()


def new_request(command: str, payload: dict, *, operation_id: str | None = None, dry_run: bool = False) -> dict:
    """再送には初回と同じIDを明示する。結果不明時に新しいIDを生成し直さない。"""
    return {"version": 1, "command": command, "operationId": operation_id or uuid.uuid4().hex,
            "payload": payload, "dryRun": dry_run}
