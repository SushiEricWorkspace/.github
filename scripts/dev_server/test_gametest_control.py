"""合成bundle・自己終了adapterで予約と結果保全を確認する。実Minecraft試験とは区別する。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import threading
import unittest
from unittest.mock import patch

from dev_server.build_inputs import digest
from dev_server.gametest_control import GameTestControl
from dev_server.registry import RegistryError
from dev_server.runtime_process import command_hash
from dev_server import test_runtime_bundle as bundle_tests
from dev_server.test_supervisor import RegistryFixture


class Process:
    def __init__(self, adapter):
        self.adapter = adapter
        self.pid = self.group = None
        self.stage = "NOT_CREATED"
        self.polls = 0

    def start(self, arguments, cwd, log, nonce, created):
        self.pid = self.group = 535
        self.stage = "CREATED"
        self.identity = {"pid": self.pid, "startMillis": 535, "cwd": str(cwd.resolve()),
                         "executable": arguments[0], "commandHash": command_hash(arguments)}
        self.adapter.process = self
        created()
        self.stage = "RUNNING"
        values = [json.loads(line) for line in Path(arguments[1][1:]).read_text().splitlines()]
        props = dict(value.removeprefix("-Dsushieric.managed.").split("=", 1)
                     for value in values if value.startswith("-Dsushieric.managed."))
        manifest = Path(props["runtimeManifest"])
        proof = {"schemaVersion": 1, "instanceId": props["instanceId"], "generationId": props["generationId"],
                 "startNonce": nonce, "phase": "STOPPED", "saveScope": "gametest-shutdown",
                 "identity": {"pid": self.pid, "processStartedAt": datetime.fromtimestamp(.535, timezone.utc).isoformat(),
                              "cwd": str(cwd), "artifactId": manifest.parent.name,
                              "runtimeManifest": str(manifest), "runtimeManifestSha256": digest(manifest.read_bytes())},
                 "startup": {"ready": True, "managementEnabled": False, "managementStarted": False,
                             "unknownWriters": [], "listeners": [], "seed": 0,
                             "roots": {"world": str(cwd / "world"), "config": str(cwd / "config"),
                                       "modConfig": str(cwd / "config/SushiEricServerMod")}},
                 "gameTestShutdownSaveCompleted": True, "vanillaShutdownTailReached": True,
                 "requiresSupervisorProcessExit": True, "requiresExternalWriterDrain": True,
                 "saves": {"failures": [], "pendingWriters": 0, "drained": True, "success": True}}
        if self.adapter.fault:
            self.adapter.fault(proof)
        Path(props["proofPath"]).write_text(json.dumps(proof))
        if self.adapter.report is not None:
            (cwd / "report.xml").write_text(self.adapter.report)

    def poll(self):
        self.polls += 1
        return None if self.polls == 1 else self.adapter.exit_code

    def members(self):
        return [536] if self.adapter.child and (not self.adapter.late_child or self.polls >= 2) else []

    def close(self):
        self.adapter.closed = True


class Adapter:
    def __init__(self):
        self.process = None
        self.closed = False
        self.fault = None
        self.report = '<testsuite tests="1"><testcase name="synthetic"/></testsuite>'
        self.exit_code = 0
        self.listeners = []
        self.child = False
        self.late_child = False

    def OwnedProcess(self):
        return Process(self)

    def identity(self, pid):
        return self.process.identity

    def sockets(self, pid):
        if pid == 536:
            return [{"protocol": "tcp", "port": 25702}]
        return self.listeners


@unittest.skipUnless(sys.platform in ("win32", "darwin"), "対象OSのprivate rootを使用する試験")
class GameTestControlTests(RegistryFixture, unittest.TestCase):
    quota_bytes = 10000000

    def setUp(self):
        RegistryFixture.setUp(self)
        fixture = bundle_tests.RuntimeBundleTests("test_launch_has_no_source_cache_or_gradle_dependency")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.freeze()
        shutil.copytree(source, self.root / "artifacts" / source.name)
        self.payload = {"runId": "gametest-535", "owner": "codex-test", "artifactId": source.name,
                        "timeoutSeconds": 3, "acceptEula": True}
        self.adapter = Adapter()
        self.registry.runtime.process_adapter = self.adapter

    def run_test(self, payload=None, operation="run-test", dry=False):
        return self.command("gametest", payload or self.payload, operation, dry)

    def test_completed_result_replays_without_relaunching(self):
        result = self.run_test()
        self.assertTrue(result["testsPassed"])
        self.assertTrue(result["runtimeStopped"])
        self.assertFalse(result["snapshotClean"])
        self.assertEqual(0, result["seed"])
        self.assertTrue(self.adapter.closed)
        first = self.adapter.process
        self.assertTrue(self.run_test()["replayed"])
        self.assertIs(first, self.adapter.process)
        self.assert_code("EXISTING_DATA", lambda: self.run_test(operation="another-operation"))
        self.assert_code("OPERATION_CONFLICT", lambda: self.run_test(dict(self.payload, owner="other")))

    def test_dry_run_creates_no_operation_or_world(self):
        self.assertTrue(self.run_test(dry=True)["dryRun"])
        self.assertFalse((self.root / "temporary" / self.payload["runId"]).exists())
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM runtime_operations").fetchone()[0])

    def test_pin_is_owned_and_does_not_change_completed_result(self):
        self.run_test()
        path = self.root / "temporary" / self.payload["runId"] / "diagnostics/result.json"
        before = path.read_bytes()
        pin = {"action": "pin", "runId": self.payload["runId"], "owner": self.payload["owner"], "pinned": True}
        self.assert_code("OWNER_MISMATCH", lambda: self.run_test(dict(pin, owner="other"), "wrong-pin"))
        self.assertTrue(self.run_test(pin, "pin")["pinned"])
        self.assertTrue(self.run_test(pin, "pin")["replayed"])
        self.run_test(dict(pin, pinned=False), "unpin")
        self.assert_code("STALE_OPERATION", lambda: self.run_test(pin, "pin"))
        self.assertEqual(before, path.read_bytes())

    def test_supervisor_restart_holds_unfinished_game_test(self):
        with patch.object(GameTestControl, "_run", side_effect=RegistryError("GAMETEST_TIMEOUT", "synthetic")):
            self.assert_code("GAMETEST_TIMEOUT", self.run_test)
        self.registry.begin_supervision()
        self.assert_code("RECOVERY_REQUIRED", self.run_test)
        self.assert_code("BUSY", lambda: self.run_test(dict(self.payload, runId="new-run"), "new-run"))

    def test_native_failure_keeps_diagnostic_without_exception_message(self):
        with patch.object(GameTestControl, "_run", side_effect=OSError(299, "secret-input")):
            self.assert_code("GAMETEST_FAILED", self.run_test)
        with self.registry.transaction(read_only=True) as connection:
            row = connection.execute("SELECT status,response FROM runtime_operations WHERE id='run-test'").fetchone()
        self.assertEqual("RECOVERY_REQUIRED", row["status"])
        failure = json.loads(row["response"])
        self.assertEqual("OSError", failure["errorType"])
        self.assertEqual(299, failure["nativeError"])
        self.assertTrue(failure["frames"])
        self.assertNotIn("secret-input", row["response"])
        self.assert_code("RECOVERY_REQUIRED", self.run_test)

    def test_failures_preserve_world_report_and_failed_replay(self):
        for report, exit_code in ((None, 0), ("broken", 0), ('<testsuite><testcase><failure/></testcase></testsuite>', 1),
                                  ('<testsuite><testcase/></testsuite>', 1)):
            with self.subTest(report=report, exit_code=exit_code):
                self.adapter.report, self.adapter.exit_code = report, exit_code
                payload = dict(self.payload, runId="failed-" + str(exit_code) + "-" + str(len(str(report))))
                operation = payload["runId"]
                self.assert_code("GAMETEST_FAILED", lambda: self.run_test(payload, operation))
                first = self.adapter.process
                self.assert_code("GAMETEST_FAILED", lambda: self.run_test(payload, operation))
                self.assertIs(first, self.adapter.process)
                self.assertTrue((self.root / "temporary" / payload["runId"] / "diagnostics/result.json").is_file())

    def test_save_failure_and_wrong_nonce_are_not_success(self):
        for name, fault in (("save", lambda proof: proof.update(gameTestShutdownSaveCompleted=False)),
                            ("nonce", lambda proof: proof.update(startNonce="wrong"))):
            with self.subTest(name=name):
                self.adapter.fault = fault
                self.assert_code("GAMETEST_FAILED", lambda: self.run_test(dict(self.payload, runId=name), name))
                result = json.loads((self.root / "temporary" / name / "diagnostics/result.json").read_text())
                self.assertTrue(result["testsPassed"])
                self.assertFalse(result["runtimeStopped"])
                self.assertEqual(self.now + 7 * 86400, result["worldRetentionUntil"])

    def test_unreserved_listener_preserves_ownership_and_blocks_replay(self):
        self.adapter.listeners = [{"protocol": "tcp", "port": 25701}]
        self.assert_code("LISTENER_MISMATCH", self.run_test)
        self.assertFalse(self.adapter.closed)
        self.assert_code("RECOVERY_REQUIRED", self.run_test)
        self.assert_code("BUSY", lambda: self.command("shutdown", {}, "shutdown-held"))

    def test_child_listener_is_rejected_before_and_after_parent_exit(self):
        self.adapter.child = True
        self.registry.inventory["gameTestConcurrency"] = 2
        for late in (False, True):
            with self.subTest(late=late):
                self.adapter.late_child = late
                self.adapter.closed = False
                self.assert_code("LISTENER_MISMATCH", lambda: self.run_test(dict(self.payload, runId=f"child-{int(late)}"), f"child-{int(late)}"))
                self.assertFalse(self.adapter.closed)

    def test_two_runs_reserve_two_slots_and_leave_persistent_instance_untouched(self):
        self.registry.inventory["gameTestConcurrency"] = 2
        entered = [threading.Event(), threading.Event()]
        finish = threading.Event()
        def wait(payload, bundle):
            entered[int(payload["runId"][-1])].set()
            self.assertTrue(finish.wait(5))
            return {"runId": payload["runId"], "testsPassed": True, "runtimeStopped": True}
        with patch.object(GameTestControl, "_run", side_effect=wait), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_test, dict(self.payload, runId=f"parallel-{index}"), f"parallel-{index}") for index in range(2)]
            try:
                self.assertTrue(all(event.wait(5) for event in entered))
                self.assert_code("BUSY", lambda: self.run_test(dict(self.payload, runId="third"), "third"))
                self.assert_code("BUSY", lambda: self.command("shutdown", {}, "shutdown-busy"))
                token = self.acquire()
                self.assertEqual("persistent-01", token["instanceId"])
            finally:
                finish.set()
            self.assertTrue(all(future.result()["testsPassed"] for future in futures))


if __name__ == "__main__":
    unittest.main()
