#!/usr/bin/env python3
"""名前でprofile・枠を選ぶ、人間用の管理CLI一括操作入口。"""
import argparse
import os
import re
import struct
from pathlib import Path
import sys
import uuid

from dev_server.build_lock import BuildLockError
from dev_server.human_server import HumanServer, HumanStore, LocalTarget, human_failure
from dev_server.local_ipc import encode
from dev_server.registry import RegistryError, identifier, require
from dev_server.connection_profiles import profile_hash
from dev_server.human_remote import RemoteHuman


def select(items, value, key):
    matches = [item for item in items if value in (item[key], item["displayName"])]
    require(len(matches) == 1, "INVALID_INPUT", "名前またはIDで一つの候補を選んでください")
    return matches[0][key]


class HumanParser(argparse.ArgumentParser):
    def error(self, message):
        print("引数が不正です。--helpで形式を確認してください。", file=sys.stderr)
        self.exit(2)


def render(document, human):
    if not human:
        print(encode(document).decode())
        return
    if not document["ok"]:
        error = document["error"]
        print("未完了・復旧待ち: " + error["code"] + " — " + error["message"], file=sys.stderr)
        for key, label in (("phase", "段階"), ("actionId", "要求ID"), ("operationId", "操作ID"), ("evidence", "証跡")):
            if document.get(key) is not None:
                print(label + ": " + str(document[key]), file=sys.stderr)
        hints = {"SSH_REQUIRED": "OpenSSHクライアントを運用者が用意してください。",
                 "KNOWN_HOSTS_REQUIRED": "確認済みknown_hostsを--known-hostsで指定し、setupへ保存してください。",
                 "SSH_UNAVAILABLE": "SSHサーバー・既存認証・host key・対象Python 3.12・CLIのパスを確認してください。再送は同じ要求IDで明示します。",
                 "JDK_REQUIRED": "JDK 21を含む固定artifactを配備してください。自動インストールはしません。",
                 "INVALID_INPUT": "初回は非秘密profileを登録し、setupでprofile・枠・ownerを選んでください。",
                 "UNAVAILABLE": "64bit Python 3.12・固定CLI・承認済みroot・private権限と証跡を確認してください。",
                 "WRITER_PENDING": "Managerを先に正常切断し、writerと未完了操作を照合してください。",
                 "ACTION_RESOLVED": "解決済みの旧要求を再送せず、statusで現在状態を確認してください。",
                 "ACTION_PENDING": "同じ引数と要求IDに--resumeを追加して照合してください。新IDでは再送しません。"}
        if error["code"] in hints:
            print(hints[error["code"]], file=sys.stderr)
        return
    result = document["result"]
    if result.get("command") in {"start", "restart"}:
        print("端末を閉じても停止・返却されません。renew担当が期限前に更新し、stopで保存停止してください。")
    if "text" in result:
        print(result["text"])
        return
    print("成功: " + str(result.get("command", result.get("state", "確認完了"))))
    labels = {"profile": "profile", "instance": "枠", "instanceId": "枠ID", "state": "状態", "generation": "世代",
              "expiresAt": "貸出期限", "renewOwner": "renew担当", "owner": "貸出owner", "writers": "writer数",
              "pendingActionId": "途中要求ID", "evidence": "証跡", "directory": "private保存先", "handoffId": "引継ぎID",
              "host": "ホスト", "managementRoot": "管理root", "python": "Python", "java": "固定JDK", "approval": "運用承認"}
    for key, label in labels.items():
        if key in result and result[key] is not None:
            print(label + ": " + str(result[key]))
    for key in ("profiles", "instances", "templates"):
        for item in result.get(key, []):
            item_id = item.get("profileId", item.get("instanceId", item.get("templateId")))
            print(str(item_id) + " : " + item["displayName"])
    for name, value in result.get("endpoints", {}).items():
        print(name + ": " + value["address"] + ":" + str(value["port"]))
    if result.get("pendingRequest"):
        pending = result["pendingRequest"]
        print("途中段階: " + pending["phase"] + " / 操作ID: " + str(pending["operationId"]))
    for pending in result.get("clientPendingRequests", []):
        print("client未確認要求: " + pending["actionId"] + " / 段階: " + pending["phase"])
        print("証跡: " + pending["evidence"])
    if result.get("native"):
        print("native実行元: " + result["native"]["executable"])


def main():
    parser = HumanParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="start", choices=("setup", "doctor", "choices", "start", "status", "logs", "stop", "pause", "restart",
                                           "renew", "recover", "handoff", "supervisor-stop"))
    parser.add_argument("--directory", type=Path, default=Path(os.environ.get("SUSHIERIC_HUMAN_DIRECTORY", str(Path.home() / ".sushieric-human"))), help="Git外のprivate記録先")
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
    parser.add_argument("--known-hosts", type=Path, help="既確認known_hostsの絶対パス")
    parser.add_argument("--ssh-timeout-seconds", type=int, default=1800, help="SSH一括操作の応答期限。期限後は保全")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--human-readable", action="store_true", help="日本語で結果と段階を表示")
    output.add_argument("--json", action="store_true", help="機械向けJSONで表示")
    args = parser.parse_args()
    action_id = args.action_id or uuid.uuid4().hex
    try:
        require(sys.version_info[:2] == (3, 12) and struct.calcsize("P") == 8, "UNAVAILABLE", "64bit Python 3.12で入口を実行してください")
        store = HumanStore(args.directory)
        launch = args.directory / "launch.json"
        defaults = store.connections._read(launch) if launch.exists() else {}
        if args.profile is None:
            args.profile = defaults.get("profileId")
        profiles = [store.connections.profile(path.stem) for path in sorted((args.directory / "profiles").glob("*.json"))]
        if args.command == "choices" and args.profile is None:
            result = {"profiles": [{"profileId": item["profileId"], "displayName": item["displayName"],
                                    "transport": item["transport"]} for item in profiles]}
        else:
            profile_id = select(profiles, args.profile, "profileId")
            profile = store.connections.profile(profile_id)
            if defaults.get("profileId") == profile_id:
                require(args.command == "setup" or defaults.get("profileHash") == profile_hash(profile),
                        "PROFILE_CHANGED", "固定profileを変更した後はsetupを明示して確認してください")
                for key, saved in (("instance", "instanceId"), ("owner", "owner"), ("artifact", "artifactId"), ("known_hosts", "knownHosts")):
                    if getattr(args, key) is None:
                        value = defaults.get(saved)
                        setattr(args, key, Path(value) if key == "known_hosts" and value else value)
            if args.command == "choices" and profile["transport"] == "local":
                render({"ok": True, "result": LocalTarget(profile).choices()}, args.human_readable)
                return 0
            require(args.owner is not None, "INVALID_INPUT", "--ownerを指定するかsetupで保存してください")
            instance = select(profile["instances"], args.instance, "instanceId")
            if args.command == "setup":
                identifier(args.owner, "owner")
                require(args.artifact is None or re.fullmatch(r"[0-9a-f]{64}", args.artifact) is not None,
                        "INVALID_INPUT", "固定artifactは64桁SHA-256にしてください")
                if profile["transport"] == "ssh":
                    require(args.known_hosts is not None and args.known_hosts.is_absolute() and args.known_hosts.is_file(),
                            "KNOWN_HOSTS_REQUIRED", "SSH設定には既確認known_hostsの絶対パスが必要です")
                record = {"version": 1, "profileId": profile_id, "profileHash": profile_hash(profile), "instanceId": instance,
                          "owner": args.owner, "artifactId": args.artifact,
                          "knownHosts": str(args.known_hosts) if args.known_hosts else None}
                store.connections._write(launch, record, replace=launch.exists())
                result = {"command": "setup", "profile": profile["displayName"], "instanceId": instance, "directory": str(args.directory)}
            else:
                require(not args.resume or args.action_id is not None, "INVALID_INPUT", "--resumeには--action-idが必要です")
                options = {"acquire": args.acquire, "artifactId": args.artifact, "mode": args.mode,
                           "templateId": args.template, "snapshotId": args.snapshot,
                           "withoutSnapshot": args.without_snapshot, "acceptEula": args.accept_eula,
                           "seed": args.seed, "ttlSeconds": args.ttl_seconds, "timeoutSeconds": args.timeout_seconds,
                           "recoveryAction": args.recovery_action, "targetOperationId": args.target_operation_id}
                if profile["transport"] == "ssh":
                    server = RemoteHuman(store, profile, instance, args.owner, known_hosts=args.known_hosts,
                                         timeout=args.ssh_timeout_seconds)
                    if args.command == "handoff":
                        result = server.handoff(args.target_client, options)
                    elif args.command in {"start", "stop", "pause", "restart", "renew", "recover"}:
                        result = server.run(args.command, options, action_id=action_id, resume=args.resume)
                    else:
                        result = server.read(args.command, options)
                else:
                    target = LocalTarget(profile)
                    server = HumanServer(store, profile_id, args.owner, instance, target=target)
                    if args.command == "choices":
                        result = target.choices()
                    elif args.command == "doctor":
                        result = target.doctor(instance, args.artifact)
                    elif args.command == "supervisor-stop":
                        result = target.shutdown(store)
                    elif args.command == "status":
                        result = server.status()
                    elif args.command == "logs":
                        result = {"text": server.logs()}
                    elif args.command == "handoff":
                        result = server.handoff(args.target_client)
                    else:
                        result = server.run(args.command, options, action_id=action_id, resume=args.resume)
        render({"ok": True, "result": result}, args.human_readable)
        return 0
    except (RegistryError, BuildLockError, OSError, ValueError, TypeError, KeyError, StopIteration, KeyboardInterrupt) as error:
        if hasattr(error, "human_response"):
            response = error.human_response
        elif "store" in locals():
            response = human_failure(error, store, action_id)
        else:
            response = {"ok": False, "error": {"code": getattr(error, "code", "UNAVAILABLE"),
                         "message": "Python 3.12・設定・private保存先を確認してください"}, "phase": "事前検査"}
        render(response, args.human_readable)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
