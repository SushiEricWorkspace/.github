#!/usr/bin/env python3
"""管理接続の非秘密プロファイルをGit外へ登録・編集・検査する。接続は行わない。"""
import argparse
from pathlib import Path
import sys

from dev_server.connection_profiles import ConnectionStore
from dev_server.contracts import ContractError
from dev_server.local_ipc import MAX_MESSAGE, decode, encode
from dev_server.registry import RegistryError


class ProfileArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(encode({"ok": False, "error": {"code": "INVALID_INPUT", "message": "--helpで引数を確認してください"}}).decode())
        self.exit(2)


def main() -> int:
    parser = ProfileArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "edit", "check"))
    parser.add_argument("--directory", type=Path, required=True, help="Git外のprivate保存先（親は既存）")
    parser.add_argument("--profile-id", help="checkで指定するprofile ID")
    args = parser.parse_args()
    try:
        if args.command == "check":
            if args.profile_id is None:
                raise RegistryError("INVALID_INPUT", "checkには--profile-idが必要です")
            store = ConnectionStore(args.directory)
            profile = store.profile(args.profile_id)
        else:
            if args.profile_id is not None:
                raise RegistryError("INVALID_INPUT", "register/editのIDは標準入力のJSONへ指定してください")
            body = sys.stdin.buffer.read(MAX_MESSAGE + 1)
            if len(body) > MAX_MESSAGE:
                raise RegistryError("INVALID_INPUT", "profileの入力上限を超えています")
            profile = decode(body)
            store = ConnectionStore(args.directory)
            store.save_profile(profile, replace=args.command == "edit")
        response = {"ok": True, "profileId": profile["profileId"], "transport": profile["transport"],
                    "targetOs": profile["targetOs"], "checked": "format-only"}
    except (RegistryError, ContractError) as error:
        response = {"ok": False, "error": {"code": getattr(error, "code", "INVALID_INPUT"), "message": str(error)}}
    except (OSError, ValueError, TypeError):
        response = {"ok": False, "error": {"code": "INVALID_INPUT", "message": "入力・保存先・private権限を確認してください"}}
    print(encode(response).decode())
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
