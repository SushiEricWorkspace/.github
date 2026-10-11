"""承認した使い捨てrootだけで固定fixtureの生成・独立検証・明示昇格を往復確認する。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_server import runtime_process
from dev_server.fixtures import FIXTURES, contract_documents
from dev_server.local_ipc import request, make_private, plain_path
from dev_server.registry import initialize, Registry
from dev_server.runtime_bundle import verify_bundle
from dev_server.runtime_ports import instance_endpoints, prebind, require_listeners
from dev_server.runtime_rcon import command as rcon_command
from dev_server.snapshot_control import verify_snapshot
from dev_server.supervisor import new_request


def verify_restart(created, verified, first_stop, second_stop):
    """同世代の別nonceで現行APIが再読込した値・ファイルと、両runの正常停止を照合する。"""
    from dev_server.registry import require
    require(created["action"] == "create" and verified["action"] == "verify"
            and created["nonce"] != verified["nonce"], "PROOF_MISMATCH", "別起動のfixture証跡が必要です")
    for key in ("schemaVersion", "fixture", "seed", "instanceId", "generationId", "oreRevivalSecond",
                "inventoryCount", "money", "internalId", "files"):
        require(created[key] == verified[key], "ROUNDTRIP_MISMATCH", "再起動後のfixtureが一致しません: " + key)
    for fixture, stopped in ((created, first_stop), (verified, second_stop)):
        require(stopped["nonce"] == fixture["nonce"] and stopped["instanceId"] == fixture["instanceId"]
                and stopped["generation"] == fixture["generationId"] and stopped["runtimeStopped"] is True
                and stopped["modSaveCompleted"] is True and stopped["stopRequested"] is True
                and stopped["stopAcknowledged"] is True and stopped["jvmAndChildrenExited"] is True
                and stopped["exitCode"] == 0, "SAVE_FAILED", "fixtureの正常停止証跡が一致しません")


def verify_overlap(runs):
    """実OS開始時刻と監督の終了確認時刻から、3つの所有JVMの実際の併走を照合する。"""
    from dev_server.registry import require
    require(len(runs) == 3 and all(run["jvmAndChildrenExited"] is True for run in runs),
            "PROOF_MISMATCH", "永続枠と2つのGameTestの終了記録が必要です")
    started = max(run["identity"]["startMillis"] / 1000 for run in runs)
    exited = min(run["exitedAt"] for run in runs)
    require(started < exited, "CONCURRENCY_NOT_OBSERVED", "3つのJVMの併走を確認できませんでした")
    return {"overlapStartedAt": started, "overlapEndedAt": exited}


def main():
    parser = argparse.ArgumentParser()
    for name in ("repo", "root", "bundle", "evidence"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--fixture", choices=[value["name"] for value in FIXTURES], required=True)
    parser.add_argument("--restart-only", action="store_true", help="snapshot昇格をせず同世代の停止・再起動を検証する")
    parser.add_argument("--game-test-runs", type=int, choices=(0, 2), default=0, help="restart-onlyで固定bundleのGameTestを2実行併走させる")
    parser.add_argument("--minecraft-base-port", type=int, default=26085)
    parser.add_argument("--rcon-base-port", type=int, default=26095)
    parser.add_argument("--management-base-port", type=int, default=26100)
    parser.add_argument("--backup-destination", type=Path, help="新しい別媒体保存先で停止rootのコピー・復元を確認する。昇格はしない")
    args = parser.parse_args()
    assert args.game_test_runs == 0 or args.restart_only, "GameTest併走はrestart-onlyの試験に限定します"
    assert args.backup_destination is None or not args.restart_only, "別媒体試験はsnapshot作成が必要です"
    for path in (args.root, args.bundle, args.evidence):
        plain_path(path)
        assert path.is_absolute(), "絶対パスを指定してください"
    assert not args.root.exists() and not args.evidence.exists(), "既存環境・証跡は上書きしません"
    from dev_server.backup_verification import validate_trial_paths
    outputs = [args.root, args.evidence]
    if args.backup_destination is not None:
        outputs.extend([args.backup_destination, args.root.with_name(args.root.name + "-before-backup-restore")])
    validate_trial_paths(args.bundle, outputs)
    if args.backup_destination is not None:
        plain_path(args.backup_destination)
        assert args.backup_destination.is_absolute() and not args.backup_destination.exists()
        assert args.backup_destination.parent.is_dir()
        assert args.root.parent.stat().st_dev != args.backup_destination.parent.stat().st_dev, "別volumeの保存先が必要です。物理媒体の確認は運用者が別途行います"
        assert not args.root.with_name(args.root.name + "-before-backup-restore").exists()
    bundle = verify_bundle(args.bundle)
    args.evidence.mkdir(parents=True)
    make_private(args.evidence, directory=True)
    report = {"testOnly": True, "backupConfirmedForProduction": False, "steps": []}
    pool = None

    def record(step, value):
        report["steps"].append({"step": step, "result": value})
        path = args.evidence / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        make_private(path)
        print(json.dumps({"step": step, "result": value}, ensure_ascii=False), flush=True)

    def call(command, payload, operation):
        response = request(args.root, new_request(command, payload, operation_id=operation), timeout=900)
        if not response["ok"]:
            record("failed-request", {"command": command, "operationId": operation, "error": response["error"]})
            raise RuntimeError(response["error"]["code"])
        return response["result"]

    def rcon(token, text):
        registry = Registry(args.root)
        with registry.transaction(read_only=True) as connection:
            row = dict(connection.execute("SELECT * FROM instances WHERE id=?", (token["instanceId"],)).fetchone())
            run = json.loads(connection.execute("SELECT record FROM runs WHERE id=?", (row["run_id"],)).fetchone()[0])
        generation = args.root / "instances" / row["id"] / "generations" / row["generation"]
        def verify():
            runtime_process.require_identity(run["identity"], runtime_process.identity(run["identity"]["pid"]))
            proof = registry.runtime._proof(run, generation)
            require_listeners(run["endpoints"], proof["startup"]["listeners"], runtime_process.sockets(run["identity"]["pid"]))
        from dev_server.snapshot_data import properties
        settings = properties((generation / "server.properties").read_bytes())
        endpoint = run["endpoints"]["rcon"]
        declared = next(value for value in registry.inventory["instances"] if value["instanceId"] == row["id"])
        assert endpoint == instance_endpoints(declared)["rcon"]
        return rcon_command(endpoint["address"], endpoint["port"], settings["rcon.password"], 30, text, verify=verify)

    def acquire(instance, operation):
        lease = call("acquire", {"instanceId": instance, "owner": args.task_id, "ttlSeconds": 7200}, operation)
        return {"instanceId": instance, "leaseId": lease["leaseId"], "epoch": lease["epoch"], "expectedGeneration": lease["generation"]}

    def runs(ids):
        registry = Registry(args.root)
        with registry.transaction(read_only=True) as connection:
            values = [connection.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone() for run_id in ids]
        return [json.loads(value[0]) if value is not None else None for value in values]

    inventory = contract_documents("smoke-v1")[0]["inventory"]
    inventory.update(operator=args.approved_by, managementRoot=str(args.root), quotaBytes=32 * 1024**3, reserveBytes=1024**3)
    inventory["gameTestConcurrency"] = 2 if args.game_test_runs else 1
    inventory["backup"] = {"destination": str(args.evidence / "synthetic-backup-not-real-medium"), "separateMediumConfirmedBy": args.approved_by}
    if args.backup_destination is not None:
        inventory["backup"]["destination"] = str(args.backup_destination)
    for index, declaration in enumerate(inventory["instances"]):
        declaration["ports"] = {"minecraft": args.minecraft_base_port + index, "rcon": args.rcon_base_port + index,
                                "management": args.management_base_port + index}
        with prebind(instance_endpoints(declaration)):
            pass
    initialize(inventory, operation_id="init-534")
    shutil.copytree(args.bundle, args.root / "artifacts" / bundle["artifactId"])
    verify_bundle(args.root / "artifacts" / bundle["artifactId"])
    record("initialized", {"artifactId": bundle["artifactId"], "approvedBy": args.approved_by, "taskId": args.task_id})
    with (args.evidence / "supervisor.log").open("xb") as log:
        process = subprocess.Popen([sys.executable, "-B", str(args.repo / "scripts/dev-server.py"), "supervise", "--root", str(args.root)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    record("supervisor", {"pid": process.pid})
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, "supervisorが終了しました"
            try:
                call("doctor", {}, "doctor-534")
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.2)
        for spec in (value for value in FIXTURES if value["name"] == args.fixture):
            name = spec["name"]
            source = acquire("persistent-01", name + "-acquire-source")
            other = acquire("persistent-02", name + "-acquire-other")
            source_generation = name + "-source"
            call("prepare", dict(source, artifactId=bundle["artifactId"], generationId=source_generation, seed=spec["seed"], acceptEula=True), name + "-prepare")
            source["expectedGeneration"] = source_generation
            first_start = call("start", dict(source, timeoutSeconds=240), name + "-start")
            record(name + "-start", first_start)
            pool = ThreadPoolExecutor(max_workers=2) if args.game_test_runs else None
            game_tests = [pool.submit(call, "gametest", {"runId": f"gametest-{index}", "owner": args.task_id,
                          "artifactId": bundle["artifactId"], "timeoutSeconds": 240, "acceptEula": True}, f"gametest-{index}")
                          for index in range(args.game_test_runs)]
            if game_tests:
                deadline = time.monotonic() + 240
                while True:
                    records = runs([f"gametest-{index}" for index in range(args.game_test_runs)])
                    if all(value is not None and value["identity"] is not None for value in records):
                        for value in records:
                            runtime_process.require_identity(value["identity"], runtime_process.identity(value["identity"]["pid"]))
                        record("gametest-jvms-running", {"runIds": [value["runId"] for value in records]})
                        break
                    assert not any(future.done() for future in game_tests), "併走前にGameTestが終了・失敗しました"
                    assert time.monotonic() < deadline, "GameTestの併走開始期限を超過しました"
                    time.sleep(0.1)
            reply = rcon(source, "se managedfixture create " + name)
            assert "fixture verified: " + name in reply, reply
            record(name + "-create-api", {"reply": reply})
            first_stop = call("stop", dict(source, timeoutSeconds=180), name + "-source-stop")
            record(name + "-source-stop", first_stop)
            if args.restart_only:
                second_start = call("start", dict(source, timeoutSeconds=240), name + "-restart")
                record(name + "-restart", second_start)
                reply = rcon(source, "se managedfixture verify " + name)
                assert "fixture verified: " + name in reply, reply
                second_stop = call("stop", dict(source, timeoutSeconds=180), name + "-restart-stop")
                record(name + "-restart-stop", second_stop)
                diagnostics = args.root / "instances" / source["instanceId"] / "diagnostics"
                created = json.loads((diagnostics / first_start["runId"] / f"fixture-{name}-create.json").read_text())
                verified = json.loads((diagnostics / second_start["runId"] / f"fixture-{name}-verify.json").read_text())
                verify_restart(created, verified, first_stop, second_stop)
                record(name + "-restart-verified", {"api": True, "sameGeneration": True, "separateNonce": True,
                       "fixture": created, "firstStop": first_stop, "secondStop": second_stop})
                try:
                    for index, future in enumerate(game_tests):
                        record(f"gametest-{index}", future.result())
                finally:
                    if pool is not None:
                        pool.shutdown(wait=True)
                        pool = None
                if game_tests:
                    overlap = verify_overlap(runs([first_start["runId"], "gametest-0", "gametest-1"]))
                    record("three-jvms-overlapped", overlap)
                call("release", source, name + "-release-source")
                call("release", other, name + "-release-other")
                continue
            if pool is not None:
                pool.shutdown(wait=True)
                pool = None
            private_id, candidate_id, validation_id = name + "-private", name + "-candidate", name + "-validation"
            call("snapshot", dict(source, action="create", snapshotId=private_id), name + "-private")
            if args.backup_destination is not None:
                from dev_server.backup_verification import copy_verified_tree
                from dev_server import storage
                call("shutdown", {}, "backup-supervisor-shutdown")
                process.wait(timeout=30)
                assert process.returncode == 0
                for declaration in inventory["instances"]:
                    with prebind(instance_endpoints(declaration)):
                        pass
                started = time.monotonic()
                backup = copy_verified_tree(args.root, args.backup_destination)
                record("separate-volume-backup", dict(backup, seconds=time.monotonic() - started,
                       destination=str(args.backup_destination), supervisorExited=True))
                preserved = args.root.with_name(args.root.name + "-before-backup-restore")
                storage.move(args.root, preserved)
                started = time.monotonic()
                restored_copy = copy_verified_tree(args.backup_destination, args.root)
                assert backup == restored_copy
                record("offline-root-restored", dict(restored_copy, seconds=time.monotonic() - started,
                       preservedSource=str(preserved)))
                with (args.evidence / "restored-supervisor.log").open("xb") as log:
                    process = subprocess.Popen([sys.executable, "-B", str(args.repo / "scripts/dev-server.py"), "supervise", "--root", str(args.root)],
                                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                record("restored-supervisor", {"pid": process.pid})
                deadline = time.monotonic() + 30
                while True:
                    assert process.poll() is None, "復元supervisorが終了しました"
                    try:
                        call("doctor", {}, "restored-doctor")
                        break
                    except OSError:
                        assert time.monotonic() < deadline
                        time.sleep(0.2)
                for token in (source, other):
                    record("backup-recover-" + token["instanceId"], call("recover",
                           dict(token, action="resume", targetOperationId=None, ttlSeconds=7200), "backup-recover-" + token["instanceId"]))
                call("snapshot", {"action": "verify", "snapshotId": private_id}, "backup-snapshot-verify")
                started = time.monotonic()
                call("restore", dict(other, artifactId=bundle["artifactId"], generationId=name + "-backup-restored", snapshotId=private_id, acceptEula=True), "backup-restore-other")
                other["expectedGeneration"] = name + "-backup-restored"
                record("backup-snapshot-restored", {"seconds": time.monotonic() - started})
                started = time.monotonic()
                other_start = call("start", dict(other, timeoutSeconds=240), "backup-start-other")
                record("backup-restored-start", dict(other_start, seconds=time.monotonic() - started))
                reply = rcon(other, "se managedfixture verify " + name)
                assert "fixture verified: " + name in reply, reply
                other_stop = call("stop", dict(other, timeoutSeconds=180), "backup-stop-other")
                record("backup-restored-api", {"reply": reply, "stop": other_stop})
                diagnostics = args.root / "instances" / other["instanceId"] / "diagnostics"
                verified = json.loads((diagnostics / other_start["runId"] / f"fixture-{name}-verify.json").read_text())
                assert verified["instanceId"] == other["instanceId"] and verified["generationId"] == other["expectedGeneration"]
                assert verified["nonce"] == other_stop["nonce"] and other_stop["runtimeStopped"] is True and other_stop["modSaveCompleted"] is True
                record("separate-medium-roundtrip-verified", {"fixture": verified, "backupHash": backup["treeHash"],
                       "backupPreserved": True, "originalRootPreserved": True, "productionEnabled": False})
                call("release", source, "backup-release-source")
                call("release", other, "backup-release-other")
                continue
            call("snapshot", dict(source, action="candidate", snapshotId=candidate_id, sourceSnapshotId=private_id), name + "-candidate")
            private = verify_snapshot(args.root / "snapshots" / private_id)
            candidate = verify_snapshot(args.root / "snapshots" / candidate_id)
            call("restore", dict(other, artifactId=bundle["artifactId"], generationId=name + "-restored", snapshotId=candidate_id, acceptEula=True), name + "-restore")
            other["expectedGeneration"] = name + "-restored"
            call("start", dict(other, timeoutSeconds=240), name + "-start-other")
            reply = rcon(other, "se managedfixture verify " + name)
            assert "fixture verified: " + name in reply, reply
            record(name + "-verify-api", {"reply": reply})
            call("stop", dict(other, timeoutSeconds=180), name + "-other-stop")
            record(name + "-validated", call("snapshot", dict(other, action="validate", snapshotId=candidate_id, fixture=name, validationId=validation_id), name + "-validate"))
            record(name + "-promoted", call("snapshot", dict(other, action="promote", snapshotId=candidate_id, templateId=name, validationId=validation_id,
                   approvedBy=args.approved_by, taskId=args.task_id, purpose="承認済み使い捨て検証root内の試験用昇格"), name + "-promote"))
            call("reset", dict(other, artifactId=bundle["artifactId"], generationId=name + "-reset", templateId=name, acceptEula=True), name + "-reset")
            other["expectedGeneration"] = name + "-reset"
            call("start", dict(other, timeoutSeconds=240), name + "-reset-start")
            reply = rcon(other, "se managedfixture verify " + name)
            assert "fixture verified: " + name in reply, reply
            call("stop", dict(other, timeoutSeconds=180), name + "-reset-stop")
            assert private == verify_snapshot(args.root / "snapshots" / private_id)
            assert candidate == verify_snapshot(args.root / "snapshots" / candidate_id)
            record(name + "-reset-verified", {"api": True, "privateUnchanged": True, "candidateUnchanged": True})
            call("release", source, name + "-release-source")
            call("release", other, name + "-release-other")
        call("shutdown", {}, "shutdown-534")
        process.wait(timeout=30)
        assert process.returncode == 0
        record("finished", {"supervisorExitCode": process.returncode, "testOnly": True})
    except BaseException as error:
        record("failure-preserved", {"type": type(error).__name__, "message": str(error), "automaticKillOrRetry": False})
        raise
    finally:
        if pool is not None:
            pool.shutdown(wait=True)


if __name__ == "__main__":
    main()
