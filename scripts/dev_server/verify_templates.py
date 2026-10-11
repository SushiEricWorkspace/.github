"""承認した使い捨てrootだけで固定fixtureの生成・独立検証・明示昇格を往復確認する。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

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


def main():
    parser = argparse.ArgumentParser()
    for name in ("repo", "root", "bundle", "evidence"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--fixture", choices=[value["name"] for value in FIXTURES], required=True)
    args = parser.parse_args()
    for path in (args.root, args.bundle, args.evidence):
        plain_path(path)
        assert path.is_absolute(), "絶対パスを指定してください"
    assert not args.root.exists() and not args.evidence.exists(), "既存環境・証跡は上書きしません"
    bundle = verify_bundle(args.bundle)
    args.evidence.mkdir(parents=True)
    make_private(args.evidence, directory=True)
    report = {"testOnly": True, "backupConfirmedForProduction": False, "steps": []}

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
        assert endpoint["address"] == "127.0.0.1" and endpoint["port"] in {26095, 26096}
        return rcon_command(endpoint["address"], endpoint["port"], settings["rcon.password"], 30, text, verify=verify)

    def acquire(instance, operation):
        lease = call("acquire", {"instanceId": instance, "owner": args.task_id, "ttlSeconds": 7200}, operation)
        return {"instanceId": instance, "leaseId": lease["leaseId"], "epoch": lease["epoch"], "expectedGeneration": lease["generation"]}

    inventory = contract_documents("smoke-v1")[0]["inventory"]
    inventory.update(operator=args.approved_by, managementRoot=str(args.root), quotaBytes=32 * 1024**3, reserveBytes=1024**3)
    inventory["backup"] = {"destination": str(args.evidence / "synthetic-backup-not-real-medium"), "separateMediumConfirmedBy": args.approved_by}
    for index, declaration in enumerate(inventory["instances"]):
        declaration["ports"] = {"minecraft": 26085 + index, "rcon": 26095 + index, "management": 26100 + index}
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
            record(name + "-start", call("start", dict(source, timeoutSeconds=240), name + "-start"))
            reply = rcon(source, "se managedfixture create " + name)
            assert "fixture verified: " + name in reply, reply
            record(name + "-create-api", {"reply": reply})
            record(name + "-source-stop", call("stop", dict(source, timeoutSeconds=180), name + "-source-stop"))
            private_id, candidate_id, validation_id = name + "-private", name + "-candidate", name + "-validation"
            call("snapshot", dict(source, action="create", snapshotId=private_id), name + "-private")
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


if __name__ == "__main__":
    main()
