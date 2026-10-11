#!/usr/bin/env python3
"""宣言設定の初期化とローカルsupervisorへのJSON CLI。"""
import argparse
from pathlib import Path
import sys
import uuid
import sqlite3

from dev_server.build_lock import BuildLockError
from dev_server.contracts import ContractError
from dev_server.local_ipc import decode, encode, request
from dev_server.registry import RegistryError, initialize
from dev_server.supervisor import new_request, supervise


class JsonArgumentParser(argparse.ArgumentParser):
    """構文エラーもJSONにする。引数の値をエラーメッセージへ転載しない。"""
    def error(self, message):
        print(encode({"ok": False, "operationId": None, "error": {"code": "INVALID_INPUT", "message": "CLI引数が不正です。--helpで形式を確認してください"}}).decode())
        self.exit(2)


def main() -> int:
    parser = JsonArgumentParser(description="分離テスト環境の管理（macOS/Windows、Python 3.12）")
    parser.add_argument("command", choices=("init", "supervise", "doctor", "list", "status", "acquire", "renew", "release", "shutdown",
                                           "prepare", "start", "stop", "snapshot", "restore", "reset", "recover", "gc",
                                           "writer-open", "writer-io", "writer-close"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--operation-id")
    parser.add_argument("--payload", default="{}", help="command固有のJSON object")
    parser.add_argument("--payload-file", type=Path, help="shellの引用符処理を避けてJSON objectをファイルで指定")
    parser.add_argument("--payload-stdin", action="store_true", help="Manager本文をOS引数へ載せず標準入力から受け取る")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=5, help="IPC応答の待機期限。期限後も自動再送/killしない")
    args = parser.parse_args()
    operation_id = args.operation_id
    try:
        if not args.root.is_absolute():
            raise RegistryError("INVALID_INPUT", "管理rootは明示的な絶対パスで指定してください")
        if args.command == "init":
            operation_id = operation_id or uuid.uuid4().hex
            if args.inventory is None or args.payload != "{}" or args.payload_file is not None or args.payload_stdin:
                raise RegistryError("INVALID_INPUT", "initは--inventoryで宣言設定を指定してください")
            inventory = decode(args.inventory.read_bytes())
            if Path(inventory.get("managementRoot", "")) != args.root:
                raise RegistryError("INVALID_INPUT", "--rootとinventory.managementRootが一致しません")
            response = {"ok": True, "operationId": operation_id, "result": initialize(inventory, operation_id=operation_id, dry_run=args.dry_run)}
        elif args.command == "supervise":
            if args.dry_run or args.inventory is not None or args.payload != "{}" or args.payload_file is not None or args.payload_stdin:
                raise RegistryError("INVALID_INPUT", "superviseは初期化済みrootだけを指定してください")
            supervise(args.root)
            return 0
        else:
            if args.command in {"prepare", "restore", "reset", "recover", "start", "stop", "snapshot", "writer-open", "writer-io", "writer-close"} and operation_id is None:
                raise RegistryError("INVALID_INPUT", "runtime操作には再送用の--operation-idを明示してください")
            if args.inventory is not None:
                raise RegistryError("INVALID_INPUT", "--inventoryはinitだけで指定してください")
            if args.payload_file is not None and args.payload != "{}":
                raise RegistryError("INVALID_INPUT", "--payloadと--payload-fileは同時に指定できません")
            if args.payload_stdin and (args.payload_file is not None or args.payload != "{}"):
                raise RegistryError("INVALID_INPUT", "標準入力と他のpayload入力は併用できません")
            try:
                from dev_server.local_ipc import MAX_MESSAGE
                body = (sys.stdin.buffer.read(MAX_MESSAGE + 1) if args.payload_stdin else
                        args.payload_file.read_bytes() if args.payload_file is not None else args.payload.encode())
                if len(body) > MAX_MESSAGE:
                    raise ValueError("IPC上限超過")
                payload = decode(body)
            except ValueError:
                raise RegistryError("INVALID_INPUT", "payloadは有限値・重複項目なしのJSON objectで指定してください") from None
            message = new_request(args.command, payload, operation_id=operation_id, dry_run=args.dry_run)
            operation_id = message["operationId"]
            if args.timeout_seconds <= 0:
                raise RegistryError("INVALID_INPUT", "IPC待機期限は正整数で指定してください")
            response = request(args.root, message, timeout=args.timeout_seconds)
    except (RegistryError, ContractError) as error:
        response = {"ok": False, "operationId": operation_id, "error": {"code": getattr(error, "code", "INVALID_INPUT"), "message": str(error)}}
    except BuildLockError:
        response = {"ok": False, "operationId": operation_id, "error": {"code": "LOCK_BUSY", "message": "supervisorは既に稼働中、またはOS lockが使用中です"}}
    except sqlite3.Error:
        response = {"ok": False, "operationId": operation_id, "error": {"code": "DB_FAILURE", "message": "Registry障害です。既存データを自動再初期化しません"}}
    except (OSError, ValueError, TypeError):
        response = {"ok": False, "operationId": operation_id, "error": {"code": "UNAVAILABLE", "message": "管理資源・IPCへ接続できません。結果不明の変更を新しいIDで再送しないでください"}}
    print(encode(response).decode())
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
