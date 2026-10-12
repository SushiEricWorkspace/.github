"""許可した使い捨てrootだけで、人間用入口と実Modの保存停止・返却を検証する。"""
import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys

from dev_server.connection_profiles import ConnectionStore
from dev_server.fixtures import contract_documents
from dev_server.local_ipc import decode, encode, make_private, plain_path
from dev_server.registry import initialize, require
from dev_server.runtime_bundle import verify_bundle
from dev_server.runtime_ports import instance_endpoints, prebind
from dev_server.snapshot_control import verify_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "bundle"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--minecraft-port", type=int, default=26205)
    args = parser.parse_args()
    for path in (args.root, args.bundle):
        require(path.is_absolute(), "INVALID_INPUT", "試験rootとbundleは絶対パスにしてください")
        plain_path(path)
    require(not args.root.exists(), "EXISTING_DATA", "既存試験rootを上書きしません")
    manifest = verify_bundle(args.bundle)
    inventory = contract_documents("smoke-v1")[0]["inventory"]
    inventory.update(operator="issue-16-disposable-test", managementRoot=str(args.root),
                     quotaBytes=32 * 1024**3, reserveBytes=1024**3)
    inventory["backup"] = {"destination": str(args.root.parent / (args.root.name + "-synthetic-backup")),
                           "separateMediumConfirmedBy": inventory["operator"]}
    for index, declaration in enumerate(inventory["instances"]):
        declaration["ports"] = {"minecraft": args.minecraft_port + index,
                                "rcon": args.minecraft_port + 10 + index,
                                "management": args.minecraft_port + 15 + index}
        with prebind(instance_endpoints(declaration)):
            pass
    initialize(inventory, operation_id="init-human-16")
    shutil.copytree(args.bundle, args.root / "artifacts" / manifest["artifactId"])
    private = args.root / "human-private"
    store = ConnectionStore(private)
    cli = Path(__file__).resolve().parent / "human-server.py"
    profile = {"version": 1, "profileId": "human-16", "displayName": "人間用 保存停止試験",
               "transport": "local", "targetOs": "windows" if sys.platform == "win32" else "macos",
               "host": socket.gethostname(), "managementRoot": str(args.root), "python": sys.executable,
               "cli": str(Path(__file__).resolve().parent / "dev-server.py"),
               "instances": [{"instanceId": "persistent-01", "displayName": "試験枠 1"}],
               "approval": {"operator": inventory["operator"], "reference": "Issue #16の使い捨てroot実Mod試験の明示許可"}}
    store.save_profile(profile)
    report = {"testOnly": True, "artifactId": manifest["artifactId"], "steps": []}
    base = [sys.executable, "-B", str(cli), "--directory", str(private),
            "--profile", profile["displayName"], "--instance", "試験枠 1", "--owner", "human-16-test"]
    def call(command, *options):
        process = subprocess.Popen([*base, command, *options], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, _ = process.communicate(timeout=950)
        except subprocess.TimeoutExpired:
            raise RuntimeError("CLI結果不明。rootとjournalを保全し、自動kill・再送しません") from None
        response = decode(output)
        require(process.returncode == 0 and response["ok"] is True, "TEST_FAILED",
                "人間用入口が停止しました。private journalを保全しています")
        result = response["result"]
        report["steps"].append({"command": command, "result": result})
        record = private / "verification-report.json"
        record.write_bytes(encode(report))
        make_private(record)
        print(encode({"step": command, "result": result}).decode(), flush=True)
        return result

    artifact = ["--artifact", manifest["artifactId"], "--timeout-seconds", "240"]
    first = call("start", "--action-id", "human-16-prepare", "--acquire", "--mode", "prepare",
                 "--accept-eula", "--seed", "52452502", *artifact)
    assert call("status")["state"] == "RUNNING"
    restarted = call("restart", "--action-id", "human-16-restart", "--timeout-seconds", "240")
    assert first["generation"] == restarted["generation"]
    call("renew", "--action-id", "human-16-renew")
    handed = call("handoff", "--target-client", "manager-human-16")
    assert handed["renewOwner"] == "human-16-test"
    call("stop", "--action-id", "human-16-snapshot-stop", "--snapshot", "human-16-saved", "--timeout-seconds", "240")
    saved = verify_snapshot(args.root / "snapshots/human-16-saved")
    assert saved["artifactId"] == manifest["artifactId"]
    assert any(item["path"].startswith("world/") for item in saved["files"])
    assert call("status")["state"] == "AVAILABLE"
    restored = call("start", "--action-id", "human-16-restore", "--acquire", "--mode", "restore",
                    "--accept-eula", "--snapshot", "human-16-saved", *artifact)
    assert restored["generation"] != first["generation"]
    call("stop", "--action-id", "human-16-final-stop", "--without-snapshot", "--timeout-seconds", "240")
    assert call("status")["state"] == "AVAILABLE"
    call("supervisor-stop")
    for declaration in inventory["instances"]:
        with prebind(instance_endpoints(declaration)):
            pass
    report["completed"] = True
    record = private / "verification-report.json"
    record.write_bytes(encode(report))
    make_private(record)
    print(encode({"completed": True, "evidence": str(record)}).decode(), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
