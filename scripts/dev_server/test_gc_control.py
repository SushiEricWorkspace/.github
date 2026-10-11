"""合成runtimeとprivateな使い捨てFSでGCの境界・参照競合・容量障害を確認する。"""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from .gc_control import DEFAULT_POLICY
from .local_ipc import make_private
from .registry import RegistryError
from . import test_snapshot_control as fixtures
from . import test_gametest_control as game_fixtures


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSのprivate root試験")
class GcTests(unittest.TestCase):
    quota_bytes = 10000000
    command = fixtures.SnapshotTests.command
    acquire = fixtures.SnapshotTests.acquire
    assert_code = fixtures.SnapshotTests.assert_code
    prepare = fixtures.SnapshotTests.prepare
    start = fixtures.SnapshotTests.start
    stop = fixtures.SnapshotTests.stop
    rcon_stop = fixtures.SnapshotTests.rcon_stop
    state = fixtures.SnapshotTests.state
    create = fixtures.SnapshotTests.create

    def setUp(self):
        fixtures.SnapshotTests.setUp(self)
        for index in range(1, 5):
            self.payload["snapshotId"] = "snapshot-" + str(index)
            self.create("create-" + str(index))
            self.now += 1
        self.now += 15 * 86400

    def plan(self, operation="gc-plan"):
        return self.command("gc", {"action": "plan"}, operation)

    def apply(self, references, operation="gc-apply", dry=False):
        return self.command("gc", {"action": "apply", "planId": "gc-plan", "references": references,
                                   "approvedBy": self.inventory["operator"]}, operation, dry)

    def test_old_snapshot_only_and_default_three_retained(self):
        result = self.plan()
        self.assertEqual(["snapshot:snapshot-1"], [c["reference"] for c in result["candidates"]])
        self.assertNotIn("tree", result["candidates"][0])
        self.apply(["snapshot:snapshot-1"], dry=True)
        self.assertTrue((self.root / "snapshots/snapshot-1").exists())
        removed = self.apply(["snapshot:snapshot-1"])
        self.assertGreater(removed["bytes"], 0)
        self.assertFalse((self.root / "snapshots/snapshot-1").exists())
        self.assertTrue(self.apply(["snapshot:snapshot-1"])["replayed"])
        self.assertTrue(self.generation.exists())
        for index in range(2, 5):
            self.assertTrue((self.root / ("snapshots/snapshot-" + str(index))).exists())

    def test_pin_after_plan_blocks_apply(self):
        self.plan()
        self.command("gc", {"action": "pin", "reference": "snapshot:snapshot-1", "pinned": True}, "pin")
        self.assert_code("GC_CHANGED", lambda: self.apply(["snapshot:snapshot-1"]))
        self.assertTrue((self.root / "snapshots/snapshot-1").exists())

    def test_reservation_after_plan_blocks_apply(self):
        self.plan()
        with self.registry.transaction() as c:
            c.execute("INSERT INTO reservations VALUES (?,?,?,?,?)", ("copy", "snapshot:snapshot-1", "persistent-01", self.token["leaseId"], self.token["epoch"]))
        self.assert_code("GC_CHANGED", lambda: self.apply(["snapshot:snapshot-1"]))

    def test_missing_or_changed_content_never_deleted(self):
        self.plan()
        (self.root / "snapshots/snapshot-1/data/world/level.dat").write_bytes(b"changed")
        with self.assertRaises((RegistryError, ValueError)):
            self.apply(["snapshot:snapshot-1"])
        self.assertTrue((self.root / "snapshots/snapshot-1").exists())

    def test_gc_cannot_overlap_copy_and_copy_cannot_enter_gc(self):
        with self.registry.resource_use():
            self.assert_code("BUSY", self.plan)
        with self.registry.resource_use(exclusive=True):
            self.assert_code("BUSY", lambda: self.command("list", {}))

    def test_permission_failure_keeps_old_generation_and_holds_gc(self):
        self.plan()
        with patch("dev_server.gc_control.storage.move", side_effect=PermissionError("test")):
            self.assert_code("GC_FAILED", lambda: self.apply(["snapshot:snapshot-1"]))
        self.assertTrue(self.generation.exists())
        self.assertTrue((self.root / "snapshots/snapshot-1").exists())
        self.assert_code("RECOVERY_REQUIRED", lambda: self.apply(["snapshot:snapshot-1"]))
        self.assert_code("RECOVERY_REQUIRED", lambda: self.command("acquire", {"instanceId": "persistent-02", "owner": "test", "ttlSeconds": 10}, "new"))
        self.assert_code("RECOVERY_REQUIRED", lambda: self.registry.reserve_references(self.token, "new-copy", ["snapshot:snapshot-1"]))
        self.now = 104
        self.command("renew", dict(self.token, ttlSeconds=20), "renew-for-stop")
        self.assert_code("INVALID_INPUT", lambda: self.command("writer-close", {}, "close-for-stop"))

    def test_expired_lease_does_not_make_active_generation_collectable(self):
        self.assertLess(self.state()["expires"], self.now)
        self.assertFalse(any(c["reference"].startswith("generation:") for c in self.plan()["candidates"]))

    def test_immediate_previous_generation_is_always_protected(self):
        self.now = 104
        for index in (2, 3):
            generation = "generation-0" + str(index)
            payload = dict(self.token, artifactId=self.artifact, generationId=generation,
                           acceptEula=True, snapshotId="snapshot-4")
            self.command("restore", payload, "restore-" + str(index))
            self.token["expectedGeneration"] = generation
        references = [c["reference"] for c in self.plan()["candidates"]]
        self.assertNotIn("generation:persistent-01/generation-02", references)
        self.assertNotIn("generation:persistent-01/generation-03", references)

    def test_partial_unlink_failure_preserves_quarantine_and_blocks_new_gc(self):
        self.plan()
        original = Path.unlink
        deleted = []
        def unlink(path, *args, **kwargs):
            if path.is_relative_to(self.root / "quarantine"):
                if deleted:
                    raise PermissionError("synthetic-permission-failure")
                deleted.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            self.assert_code("GC_FAILED", lambda: self.apply(["snapshot:snapshot-1"]))
        self.assertEqual(1, len(deleted))
        self.assertTrue(self.generation.exists())
        self.assertTrue(any((self.root / "quarantine").iterdir()))
        self.assertTrue((self.root / "manager/operations/gc-gc-apply/requested.json").exists())
        self.assert_code("RECOVERY_REQUIRED", lambda: self.plan("next-plan"))

    def test_unknown_directory_is_not_deleted(self):
        path = self.root / "artifacts/unknown-data"
        path.mkdir()
        make_private(path, directory=True)
        (path / "keep").write_bytes(b"keep")
        self.plan()
        self.assertTrue((path / "keep").exists())

    def test_link_is_rejected_and_target_preserved(self):
        source = self.root / "snapshots/snapshot-1/data/world/level.dat"
        target = self.root / "keep"
        target.write_bytes(b"keep")
        source.unlink()
        try:
            os.symlink(target, source)
        except OSError as error:
            self.skipTest("OSが試験用symlink作成を拒否: " + str(error))
        with self.assertRaises((RegistryError, ValueError)):
            self.plan()
        self.assertEqual(b"keep", target.read_bytes())

    def test_old_pin_replay_cannot_claim_current_pin(self):
        payload = {"action": "pin", "reference": "snapshot:snapshot-1", "pinned": True}
        self.command("gc", payload, "pin")
        self.command("gc", dict(payload, pinned=False), "unpin")
        self.assert_code("STALE_OPERATION", lambda: self.command("gc", payload, "pin"))

    def test_policy_extension_preserves_plan_candidates(self):
        self.plan()
        self.command("gc", {"action": "policy", "policy": dict(DEFAULT_POLICY, snapshotSeconds=30 * 86400)}, "policy")
        self.assert_code("GC_CHANGED", lambda: self.apply(["snapshot:snapshot-1"]))

    def test_promoted_snapshot_does_not_consume_unpromoted_retention_count(self):
        path = self.root / "templates/template-01.json"
        path.write_text("{}")
        make_private(path)
        with self.registry.transaction(read_only=True) as connection:
            artifact = connection.execute("SELECT artifact FROM snapshots WHERE id='snapshot-4'").fetchone()[0]
        template = {"snapshotId": "snapshot-4", "artifactId": artifact, "validationId": "validation-01"}
        validation = {"validation": {"sourceInstanceId": "persistent-01", "restoredInstanceId": "persistent-02", "runId": "restored-run"}}
        with patch("dev_server.template_control.TemplateControl.registered", return_value=template), patch("dev_server.template_control.TemplateControl._completed", return_value=validation):
            self.assertEqual([], self.plan()["candidates"])

    def test_capacity_rechecks_free_space_and_never_collects_automatically(self):
        with patch("dev_server.runtime_control.shutil.disk_usage", return_value=type("Usage", (), {"free": 0})()):
            self.assert_code("NO_SPACE", lambda: self.registry.runtime.copy_space(1))
            self.assert_code("NO_SPACE", lambda: self.registry.runtime._space(1))
        self.assertTrue((self.root / "snapshots/snapshot-1").exists())


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSのprivate root試験")
class GameTestGcTests(unittest.TestCase):
    quota_bytes = 10000000
    command = game_fixtures.GameTestControlTests.command
    assert_code = game_fixtures.GameTestControlTests.assert_code
    run_test = game_fixtures.GameTestControlTests.run_test

    def setUp(self):
        game_fixtures.GameTestControlTests.setUp(self)

    def completed(self, *, failure=False):
        if failure:
            self.adapter.report = '<testsuite tests="1" failures="1"><testcase name="synthetic"><failure/></testcase></testsuite>'
        if failure:
            self.assert_code("GAMETEST_FAILED", self.run_test)
        else:
            self.run_test()
        self.adapter.identity = lambda pid: None
        self.adapter.recorded_members = lambda creation, nonce: []
        path = self.root / "temporary" / self.payload["runId"]
        (path / "world").mkdir(exist_ok=True)
        make_private(path / "world", directory=True)
        marker = path / "world/marker"
        marker.write_bytes(b"synthetic-world")
        make_private(marker)
        return path

    def candidates(self):
        return self.command("gc", {"action": "plan"}, "plan")["candidates"]

    def test_success_world_removed_but_result_retained(self):
        path = self.completed()
        references = [c["reference"] for c in self.candidates()]
        self.assertEqual(["world:" + self.payload["runId"]], references)
        self.command("gc", {"action": "apply", "planId": "plan", "references": references,
                            "approvedBy": self.inventory["operator"]}, "apply")
        self.assertFalse((path / "world").exists())
        self.assertTrue((path / "diagnostics/result.json").exists())
        self.assertEqual([], self.command("gc", {"action": "plan"}, "plan-again")["candidates"])

    def test_failure_world_retained_seven_days_results_thirty_days(self):
        self.completed(failure=True)
        self.assertEqual([], self.candidates())
        self.now += 8 * 86400
        self.assertEqual(["world:" + self.payload["runId"]],
                         [c["reference"] for c in self.command("gc", {"action": "plan"}, "day-eight")["candidates"]])
        self.now += 23 * 86400
        self.assertIn("run:" + self.payload["runId"],
                      [c["reference"] for c in self.command("gc", {"action": "plan"}, "day-thirty-one")["candidates"]])

    def test_pin_protects_reproduction_data(self):
        self.completed()
        self.command("gc", {"action": "pin", "reference": "run:" + self.payload["runId"], "pinned": True}, "pin")
        self.now += 31 * 86400
        self.assertEqual([], self.candidates())

    def test_changed_report_cannot_authorize_world_deletion(self):
        path = self.completed()
        (path / "report.xml").write_text('<testsuite tests="1"><testcase name="changed"/></testsuite>')
        self.assert_code("PROOF_MISMATCH", self.candidates)
        self.assertTrue((path / "world").exists())

    def test_pid_reuse_blocks_deletion(self):
        self.completed()
        self.adapter.identity = lambda pid: {"pid": pid, "startId": "other-process"}
        self.assert_code("PROCESS_MISMATCH", self.candidates)

    def test_verification_rejects_root_inside_source_bundle(self):
        from .runtime_bundle import verify_bundle
        bundle = self.root / "artifacts" / self.payload["artifactId"]
        nested = bundle / "new-trial"
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("verify_gc.py")),
                                 "--root", str(nested), "--bundle", str(bundle)], capture_output=True, timeout=30)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(nested.exists())
        verify_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
