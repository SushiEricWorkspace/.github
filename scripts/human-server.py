#!/usr/bin/env python3
"""名前でprofile・枠を選ぶ、人間用の管理CLI一括操作入口。"""
import argparse
from pathlib import Path
import sys
import uuid

from dev_server.build_lock import BuildLockError
from dev_server.human_server import HumanServer, HumanStore, LocalTarget
from dev_server.local_ipc import encode
from dev_server.registry import RegistryError, require
from dev_server.template_control import TemplateControl


def select(items, value, key):
    matches = [item for item in items if value in (item[key], item["displayName"])]
    require(len(matches) == 1, "INVALID_INPUT", "名前またはIDで一つの候補を選んでください")
    return matches[0][key]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("choices", "start", "status", "logs", "stop", "pause", "restart",
                                           "renew", "recover", "handoff", "supervisor-stop"))
    parser.add_argument("--directory", type=Path, required=True, help="Git外のprivate記録先")
    parser.add_argument("--profile", help="profile IDまたは表示名")
    parser.add_argument("--instance", help="枠IDまたは表示名")
    parser.add_argument("--owner", help="貸出とrenewの担当")
    parser.add_argument("--action-id", help="省略時は生成して送信前にprivateへ保存")
    parser.add_argument("--resume", action="store_true", help="同じaction-id・同じ選択の明示再送")
    parser.add_argument("--acquire", action="store_true", help="選択枠の新規借用を明示")
    parser.add_argument("--artifact", help="管理rootへ配備済みの固定artifact ID。ビルドは別操作")
    parser.add_argument("--mode", choices=("keep", "prepare", "reset", "restore"), default="keep")
    parser.add_argument("--template", help="登録template ID（名前）")
    parser.add_argument("--snapshot", help="restore元またはstop時の新規snapshot ID")
    parser.add_argument("--without-snapshot", action="store_true", help="stop時にsnapshotなしの返却を明示")
    parser.add_argument("--accept-eula", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--ttl-seconds", type=int, default=3600, help="貸出期限。ownerは期限前にrenewする")
    parser.add_argument("--timeout-seconds", type=int, default=120, help="監督の起動停止待機期限")
    parser.add_argument("--recovery-action", choices=("resume", "rollback"), default="resume")
    parser.add_argument("--target-operation-id", help="recoverで照合する途中操作。自動rollbackはしない")
    parser.add_argument("--target-client", help="handoff先のManager client ID")
    args = parser.parse_args()
    action_id = args.action_id or uuid.uuid4().hex
    try:
        store = HumanStore(args.directory)
        profiles = [store.connections.profile(path.stem) for path in sorted((args.directory / "profiles").glob("*.json"))]
        if args.command == "choices" and args.profile is None:
            result = {"profiles": [{"profileId": item["profileId"], "displayName": item["displayName"],
                                    "transport": item["transport"]} for item in profiles]}
        else:
            profile_id = select(profiles, args.profile, "profileId")
            profile = store.connections.profile(profile_id)
            target = LocalTarget(profile)
            if args.command == "choices":
                templates = []
                for path in sorted((target.root / "templates").glob("*.json")):
                    template = TemplateControl(target.registry).registered(path.stem)
                    templates.append({"templateId": template["templateId"], "displayName": template["templateId"],
                                      "artifactId": template["artifactId"]})
                result = {"profile": profile["displayName"], "instances": profile["instances"], "templates": templates}
            else:
                require(args.owner is not None, "INVALID_INPUT", "--ownerが必要です")
                if args.command == "supervisor-stop":
                    result = target.shutdown(store)
                else:
                    instance = select(profile["instances"], args.instance, "instanceId")
                    server = HumanServer(store, profile_id, args.owner, instance, target=target)
                    if args.command == "status":
                        result = server.status()
                    elif args.command == "logs":
                        print(server.logs())
                        return 0
                    elif args.command == "handoff":
                        result = server.handoff(args.target_client)
                    else:
                        require(not args.resume or args.action_id is not None, "INVALID_INPUT", "--resumeには--action-idが必要です")
                        options = {"acquire": args.acquire, "artifactId": args.artifact, "mode": args.mode,
                                   "templateId": args.template, "snapshotId": args.snapshot,
                                   "withoutSnapshot": args.without_snapshot, "acceptEula": args.accept_eula,
                                   "seed": args.seed, "ttlSeconds": args.ttl_seconds, "timeoutSeconds": args.timeout_seconds,
                                   "recoveryAction": args.recovery_action, "targetOperationId": args.target_operation_id}
                        result = server.run(args.command, options, action_id=action_id, resume=args.resume)
        print(encode({"ok": True, "result": result}).decode())
        return 0
    except (RegistryError, BuildLockError, OSError, ValueError, TypeError, KeyError, StopIteration):
        error = sys.exception()
        code = getattr(error, "code", "LOCK_BUSY" if isinstance(error, BuildLockError) else "UNAVAILABLE")
        message = str(error) if isinstance(error, RegistryError) else "設定・private権限・管理資源を確認してください"
        phase, operation_id = "事前検査", None
        try:
            action = store.read("actions", action_id)
            sending = [step for step in action["steps"] if step["state"] == "SENDING"]
            phase = sending[0]["command"] if sending else action["state"]
            operation_id = sending[0]["operationId"] if sending else None
        except (NameError, OSError, RegistryError, ValueError, KeyError):
            pass
        print(encode({"ok": False, "error": {"code": code, "message": message}, "phase": phase,
                      "actionId": action_id, "operationId": operation_id,
                      "evidence": str(args.directory / "actions" / (action_id + ".json"))}).decode())
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
