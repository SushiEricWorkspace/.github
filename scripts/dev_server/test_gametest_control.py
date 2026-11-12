"""合成bundle・自己終了adapterで予約と結果保全を確認する。実Minecraft試験とは区別する。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
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

    def test_private_cwd_is_outside_markers_and_keeps_diagnostic_anchor(self):
        self.run_test()
        cwd = self.registry.gametest._area() / self.payload["runId"]
        anchor = self.root / "temporary" / self.payload["runId"]
        self.assertFalse(cwd.is_relative_to(self.root))
        self.assertEqual(str(cwd), self.adapter.process.identity["cwd"])
        self.assertTrue((cwd / "report.xml").is_file())
        self.assertTrue((anchor / "diagnostics/proof.json").is_file())
        self.assertFalse((anchor / "world").exists())
        from dev_server.local_ipc import require_private
        require_private(cwd, directory=True)
        require_private(cwd.parent, directory=True)

    def test_sibling_and_ancestor_markers_reject_before_reservation(self):
        for relative in ("manager/INITIALIZED", "manager/registry.sqlite", "manager/inventory.json"):
            with self.subTest(relative=relative):
                marker = self.root.parent / relative
                marker.parent.mkdir(exist_ok=True)
                marker.write_bytes(b"marker")
                try:
                    self.assert_code("ROOT_MISMATCH", self.run_test)
                    self.assertIsNone(self.adapter.process)
                finally:
                    marker.unlink()
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM runtime_operations").fetchone()[0])

    def test_space_never_treats_unreadable_or_reparse_area_as_absent(self):
        area = self.registry.gametest._area()
        original = Path.lstat
        for error in (PermissionError("synthetic-denied"), OSError("synthetic-io")):
            with self.subTest(error=type(error).__name__):
                def lstat(path, *args, **kwargs):
                    if path == area:
                        raise error
                    return original(path, *args, **kwargs)
                with patch.object(Path, "lstat", lstat), self.assertRaises(type(error)) as caught:
                    self.registry.runtime._space()
                self.assertIs(error, caught.exception)
        from types import SimpleNamespace
        def reparse(path, *args, **kwargs):
            if path == area:
                return SimpleNamespace(st_mode=0o040700, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", reparse), self.assertRaises(OSError):
            self.registry.runtime._space()

    def test_normal_space_does_not_require_an_unused_gt_area(self):
        marker = self.root.parent / "manager/INITIALIZED"
        marker.parent.mkdir()
        marker.write_bytes(b"ancestor-marker")
        self.registry.runtime._space()
        self.assert_code("ROOT_MISMATCH", self.run_test)

    def test_existing_private_run_is_not_reused(self):
        from dev_server.local_ipc import make_private
        area = self.registry.gametest._area()
        area.mkdir()
        make_private(area, directory=True)
        (area / self.payload["runId"]).mkdir()
        self.assert_code("EXISTING_DATA", self.run_test)
        self.assertIsNone(self.adapter.process)

    def test_private_area_link_is_rejected(self):
        target = self.root.parent / "link-target"
        target.mkdir()
        try:
            os.symlink(target, self.registry.gametest._area(), target_is_directory=True)
        except OSError as error:
            self.skipTest("OSが試験用symlink作成を拒否: " + str(error))
        with self.assertRaises(OSError):
            self.run_test()
        self.assertIsNone(self.adapter.process)
        self.assertEqual([], list(target.iterdir()))

    def test_private_area_counts_toward_quota_and_physical_reserve(self):
        from dev_server.local_ipc import make_private
        area = self.registry.gametest._area()
        area.mkdir()
        make_private(area, directory=True)
        data = area / "reserved"
        data.write_bytes(b"x" * 2000)
        size = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        self.registry.inventory["quotaBytes"] = size + self.registry.inventory["reserveBytes"] + 1999
        self.assert_code("NO_SPACE", self.run_test)
        self.assertIsNone(self.adapter.process)
        self.registry.inventory["quotaBytes"] = 10000000
        usage = shutil.disk_usage
        with patch("dev_server.runtime_control.shutil.disk_usage",
                   side_effect=lambda path: type("Usage", (), {"free": 0})() if path == area else usage(path)):
            self.assert_code("NO_SPACE", self.run_test)

    def test_initial_area_checks_its_parent_free_space_before_reservation(self):
        area = self.registry.gametest._area()
        usage = shutil.disk_usage
        with patch("dev_server.gametest_control.shutil.disk_usage",
                   side_effect=lambda path: type("Usage", (), {"free": 0})() if path == area.parent else usage(path)) as checked:
            self.assert_code("NO_SPACE", self.run_test)
        self.assertIn(unittest.mock.call(area.parent), checked.call_args_list)
        self.assertFalse(area.exists())
        self.assertIsNone(self.adapter.process)
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM runtime_operations").fetchone()[0])

    def _malformed_identity_proof(self, fault):
        self.adapter.fault = fault
        error = PermissionError(299, "original-identity-error")
        original = self.adapter.identity
        calls = 0
        def identity(pid):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(pid)
            raise error
        with patch.object(self.adapter, "identity", side_effect=identity), patch.object(Process, "poll", return_value=None):
            with self.assertRaises(RegistryError) as caught:
                self.run_test()
        self.assertIs(error, caught.exception.__cause__)
        self.assertFalse(self.adapter.closed)
        self.assertIs(self.adapter.process, self.registry.runtime.processes[self.payload["runId"]])
        with self.registry.transaction(read_only=True) as connection:
            row = connection.execute("SELECT status,response FROM runtime_operations WHERE id='run-test'").fetchone()
        self.assertEqual("RECOVERY_REQUIRED", row["status"])
        failure = json.loads(row["response"])
        self.assertEqual("PermissionError", failure["errorType"])
        self.assertEqual(299, failure["nativeError"])

    def test_null_proof_identity_keeps_original_native_cause(self):
        self._malformed_identity_proof(lambda proof: proof.update(identity=None))

    def test_malformed_proof_phase_keeps_original_native_cause(self):
        self._malformed_identity_proof(lambda proof: proof.update(phase=[]))

    def test_null_proof_roots_keeps_original_native_cause(self):
        self._malformed_identity_proof(lambda proof: proof["startup"].update(roots=None))

    def test_initializing_proof_keeps_original_identity_error_until_owned_exit(self):
        self.adapter.fault = lambda proof: proof.update(phase="INITIALIZING", startup={})
        self.adapter.exit_code = 1
        error = PermissionError(5, "original-identity-error")
        original = self.adapter.identity
        calls = 0
        def identity(pid):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(pid)
            raise error
        with patch.object(self.adapter, "identity", side_effect=identity), \
                patch.object(Process, "poll", side_effect=[None, None, 1, 1, 1]):
            self.assert_code("GAMETEST_FAILED", self.run_test)
        self.assertTrue(self.adapter.closed)
        with self.registry.transaction(read_only=True) as connection:
            row = connection.execute("SELECT status,response FROM runtime_operations WHERE id='run-test'").fetchone()
            run = json.loads(connection.execute("SELECT record FROM runs WHERE id=?", (self.payload["runId"],)).fetchone()[0])
        self.assertEqual("FAILED", row["status"])
        self.assertTrue(run["jvmAndChildrenExited"])
        self.assertEqual("INITIALIZING", run["proofPhaseAtIdentityFailure"])
        self.assertEqual("PermissionError", run["identityReadFailure"]["errorType"])
        self.assertFalse(json.loads(row["response"])["runtimeStopped"])

    def test_initializing_proof_does_not_hide_live_identity_cause(self):
        self.adapter.fault = lambda proof: proof.update(phase="INITIALIZING", startup={})
        error = PermissionError(5, "original-identity-error")
        original = self.adapter.identity
        calls = 0
        def identity(pid):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(pid)
            raise error
        with patch.object(self.adapter, "identity", side_effect=identity), \
                patch.object(Process, "poll", return_value=None), \
                patch("dev_server.gametest_control.time.monotonic", side_effect=[0, 4]):
            with self.assertRaises(RegistryError) as caught:
                self.run_test()
        self.assertIs(error, caught.exception.__cause__)
        self.assertFalse(self.adapter.closed)
        self.assert_code("RECOVERY_REQUIRED", self.run_test)
        self.assert_code("BUSY", lambda: self.command("gc", {"action": "plan"}, "held-plan"))

    def test_legacy_layout_is_kept_and_unknown_layout_rejected(self):
        anchor = self.root / "temporary" / "old-run"
        run = {"runId": "old-run", "cwd": str(anchor), "proofPath": str(anchor / "diagnostics/proof.json")}
        self.assertEqual((anchor, anchor), self.registry.gametest._paths(run))
        self.assert_code("ROOT_MISMATCH", lambda: self.registry.gametest._paths(dict(run, layout="unknown")))
        self.assert_code("ROOT_MISMATCH", lambda: self.registry.gametest._paths(dict(run, cwd=str(self.root))))

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

    def test_exit_during_identity_read_requires_owned_process_exit(self):
        for fault in (PermissionError(5, "process exiting"), None):
            with self.subTest(fault=fault):
                calls = 0
                original = self.adapter.identity
                def identity(pid):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return original(pid)
                    if fault is not None:
                        raise fault
                    return None
                run_id = "exit-" + str(int(fault is None))
                with patch.object(self.adapter, "identity", side_effect=identity):
                    result = self.run_test(dict(self.payload, runId=run_id), run_id)
                self.assertTrue(result["runtimeStopped"])
                self.assertTrue(self.adapter.closed)

    def test_identity_read_failure_while_alive_is_held(self):
        self.adapter.fault = lambda proof: proof.update(phase="RUNNING")
        original = self.adapter.identity
        calls = 0
        def identity(pid):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(pid)
            raise PermissionError(5, "still alive")
        with patch.object(self.adapter, "identity", side_effect=identity), patch.object(Process, "poll", return_value=None):
            self.assert_code("GAMETEST_FAILED", self.run_test)
        self.assertFalse(self.adapter.closed)
        self.assert_code("RECOVERY_REQUIRED", self.run_test)

    def test_identity_unavailable_during_shutdown_waits_for_exit(self):
        original = self.adapter.identity
        calls = 0
        def identity(pid):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(pid)
            raise PermissionError(5, "process exiting")
        with patch.object(self.adapter, "identity", side_effect=identity), \
                patch.object(Process, "poll", side_effect=[None, None, None, None, 0, 0, 0]):
            result = self.run_test()
        self.assertTrue(result["runtimeStopped"])
        self.assertTrue(self.adapter.closed)
        with self.registry.transaction(read_only=True) as connection:
            document = connection.execute("SELECT record FROM runs WHERE id=?", (self.payload["runId"],)).fetchone()[0]
        self.assertEqual("PermissionError", json.loads(document)["identityReadFailure"]["errorType"])

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
