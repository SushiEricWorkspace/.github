"""承認した新規rootでartifactの参照保護・通常コピー復元・明示GCを確認する。JVMは起動しない。"""
import argparse
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_server.build_inputs import canonical
from dev_server.fixtures import contract_documents
from dev_server.local_ipc import plain_path, make_private
from dev_server.registry import initialize, Registry, RegistryError, require
from dev_server.runtime_bundle import verify_bundle, _files
from dev_server import storage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.root, args.bundle):
        require(path.is_absolute(), "INVALID_INPUT", "絶対パスを指定してください")
        plain_path(path)
    require(not args.root.resolve().is_relative_to(args.bundle.resolve()),
            "INVALID_INPUT", "試験rootを入力bundle内へ作成できません")
    require(not args.root.exists(), "EXISTING_DATA", "既存試験rootを上書きしません")
    bundle = verify_bundle(args.bundle)
    storage.mkdir(args.root)
    inventory = contract_documents("smoke-v1")[0]["inventory"]
    inventory["quotaBytes"] = max(inventory["quotaBytes"],
                                  3 * sum(file["size"] for file in _files(args.bundle)) + inventory["reserveBytes"] + 10485760)
    inventory["managementRoot"] = str(args.root / "managed")
    inventory["backup"]["destination"] = str(args.root / "backup")
    initialize(inventory, operation_id="init-gc-trial")
    registry = Registry(Path(inventory["managementRoot"]))
    registry.begin_supervision()
    artifact = bundle["artifactId"]
    destination = registry.root / "artifacts" / artifact
    def copy(source, target):
        registry.runtime._space(sum(file["size"] for file in _files(source)) + 65536)
        shutil.copytree(source, target)
        for directory in [target, *[p for p in target.rglob("*") if p.is_dir()]]:
            make_private(directory, directory=True)
        for file in (p for p in target.rglob("*") if p.is_file()):
            make_private(file, executable=os.name != "nt" and os.access(file, os.X_OK))
    copy(args.bundle, destination)
    verify_bundle(destination)
    reference = "artifact:" + artifact
    report = {"backupConfirmedForProduction": False, "minecraftStarted": False, "steps": []}

    def execute(payload, operation):
        value = registry.execute("gc", payload, operation_id=operation)
        report["steps"].append({"operation": operation, "result": value})
        storage.write(args.root / (operation + ".json"), canonical(value))
        return value

    plan = execute({"action": "plan"}, "plan-before-pin")
    require([c["reference"] for c in plan["candidates"]] == [reference], "INVALID_STATE", "単独artifactが候補にありません")
    execute({"action": "pin", "reference": reference, "pinned": True}, "pin-artifact")
    require(execute({"action": "plan"}, "plan-protected")["candidates"] == [], "INVALID_STATE", "pin参照が回収候補になりました")
    execute({"action": "pin", "reference": reference, "pinned": False}, "unpin-artifact")
    with patch("dev_server.runtime_control.shutil.disk_usage", return_value=type("Usage", (), {"free": 0})()):
        try:
            registry.runtime.copy_space(1)
        except RegistryError as error:
            require(error.code == "NO_SPACE", "INVALID_STATE", "容量不足を拒否できません")
            report["steps"].append({"operation": "capacity-failure", "code": error.code})
        else:
            raise AssertionError("容量不足を拒否できません")
    require(destination.exists(), "INVALID_STATE", "容量不足でartifactが削除されました")
    backup = args.root / "backup" / artifact
    backup.parent.mkdir()
    make_private(backup.parent, directory=True)
    copy(destination, backup)
    require(_files(destination) == _files(backup), "PROOF_MISMATCH", "通常コピーが一致しません")
    execute({"action": "plan"}, "plan-apply")
    execute({"action": "apply", "planId": "plan-apply", "references": [reference],
             "approvedBy": inventory["operator"]}, "apply-artifact")
    require(not destination.exists(), "INVALID_STATE", "選択artifactが残っています")
    verify_bundle(backup)
    copy(backup, destination)
    verify_bundle(destination)
    require(_files(destination) == _files(backup), "PROOF_MISMATCH", "復元コピーが一致しません")
    report["steps"].append({"operation": "copy-restore", "artifactId": artifact, "matched": True})
    storage.write(args.root / "report.json", canonical(report))
    print("参照保護・容量不足拒否・選択artifact回収・通常コピー復元が成功。正式別媒体確認とMinecraft復元ではありません。")


if __name__ == "__main__":
    main()
