"""合成runtimeと実FSで世代隔離・journal障害・復旧の冪等性を確認する。"""
import json
import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from .build_inputs import canonical, digest
from .registry import RegistryError
from .runtime_control import read_json
from . import test_snapshot_control as snapshot_tests
from . import storage


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSのprivate root試験")
class GenerationTests(unittest.TestCase):
    quota_bytes = 10000000
    command = snapshot_tests.SnapshotTests.command
    acquire = snapshot_tests.SnapshotTests.acquire
    assert_code = snapshot_tests.SnapshotTests.assert_code
    prepare = snapshot_tests.SnapshotTests.prepare
    start = snapshot_tests.SnapshotTests.start
    stop = snapshot_tests.SnapshotTests.stop
    rcon_stop = snapshot_tests.SnapshotTests.rcon_stop
    state = snapshot_tests.SnapshotTests.state
    create = snapshot_tests.SnapshotTests.create

    def setUp(self):
        snapshot_tests.SnapshotTests.setUp(self)
        self.create()
        generation = ("generation-02" if getattr(self, "bundle_version", 2) == 1
                      else "00000000-0000-4000-8000-000000000002")
        self.restore = dict(self.token, artifactId=self.artifact, generationId=generation,
                            acceptEula=True, snapshotId="snapshot-01")
        self.new = self.generation.parent / self.restore["generationId"]

    def recover(self, target="restore-01", action="resume", dry=False, operation="recover-01"):
        payload = dict(self.token, action=action, targetOperationId=target, ttlSeconds=120)
        return self.command("recover", payload, operation, dry)

    def test_restore_preserves_old_injects_ports_and_has_no_process(self):
        original = (self.generation / "world/level.dat").read_bytes()
        self.command("restore", self.restore, "restore-01")
        self.assertEqual(original, (self.new / "world/level.dat").read_bytes())
        self.assertEqual(original, (self.generation / "world/level.dat").read_bytes())
        self.assertNotEqual((self.new / "server.properties").read_bytes(), (self.generation / "server.properties").read_bytes())
        self.assertFalse((self.new / "world/session.lock").exists())
        self.assertEqual(1, len(self.adapter.created))
        self.assertEqual(self.restore["generationId"], self.state()["generation"])
        self.assertTrue(self.command("restore", self.restore, "restore-01")["replayed"])
        self.token["expectedGeneration"] = self.restore["generationId"]
        self.start("new-start")
        self.stop("new-stop")

    def test_dry_run_leaves_active_and_journal_unchanged(self):
        before = list(self.root.rglob("*"))
        self.command("restore", self.restore, "restore-01", True)
        self.assertEqual(before, list(self.root.rglob("*")))

    @unittest.skipUnless(sys.platform == "darwin", "Unix実行権限のコピー試験")
    def test_restore_preserves_private_runtime_execution_permission(self):
        java = self.root / "artifacts" / self.artifact / "jdk/bin/java"
        java.chmod(0o755)
        self.command("restore", self.restore, "restore-01")
        copied = self.new / "runtime/jdk/bin/java"
        self.assertEqual(0o700, copied.stat().st_mode & 0o777)
        self.assertTrue(os.access(copied, os.X_OK))
        self.assertEqual(java.read_bytes(), copied.read_bytes())

    def test_reset_unregistered_template_is_rejected_before_changes(self):
        payload = dict(self.restore)
        del payload["snapshotId"]
        payload["templateId"] = "smoke-v1"
        with self.assertRaises((OSError, RegistryError)):
            self.command("reset", payload, "reset-01")
        self.assertEqual(self.prepare_payload["generationId"], self.state()["generation"])
        self.assertFalse(self.new.exists())

    def test_reset_unregistered_reference_is_not_operator_approval(self):
        manifest = self.registry.snapshot._registered("snapshot-01")
        template = {"schemaVersion": 1, "templateId": "smoke-v1", "snapshotId": "snapshot-01",
                    "manifestHash": digest(canonical(manifest)), "artifactId": self.artifact, "validationId": "synthetic-validation"}
        storage.write(self.root / "templates/smoke-v1.json", canonical(template))
        payload = dict(self.restore)
        del payload["snapshotId"]
        payload["templateId"] = "smoke-v1"
        self.assert_code("INVALID_INPUT", lambda: self.command("reset", payload, "reset-01"))
        self.assertEqual(self.prepare_payload["generationId"], self.state()["generation"])
        self.assertFalse(self.new.exists())

    def test_expiry_during_copy_is_fenced_and_explicit_recovery_renews(self):
        original = self.registry.generation._build
        def expire(*args):
            original(*args)
            self.now += 30
        with patch.object(self.registry.generation, "_build", side_effect=expire):
            self.assert_code("LEASE_EXPIRED", lambda: self.command("restore", self.restore, "restore-01"))
        self.recover()
        self.assertEqual(self.restore["generationId"], self.state()["generation"])

    def test_old_expected_generation_and_changed_recovery_action_rejected(self):
        self.command("restore", self.restore, "restore-01")
        self.assert_code("STALE_GENERATION", lambda: self.command("restore", dict(self.restore, generationId="00000000-0000-4000-8000-000000000003"), "restore-stale"))
        self.token["expectedGeneration"] = self.restore["generationId"]
        self.registry.begin_supervision()
        self.registry.begin_supervision()
        self.recover(target=None)
        self.assert_code("OPERATION_CONFLICT", lambda: self.recover(target=None, action="rollback"))

    def test_prepared_switch_and_commit_boundaries_resume(self):
        # subTestごとに新しいrootを作り、前の故障状態を再利用しない。
        for phase in ("prepared", "switched", "committed"):
            with self.subTest(phase=phase):
                if phase != "prepared":
                    self.doCleanups()
                    self.setUp()
                original = self.registry.generation._marker
                def fail(journal, name, value):
                    original(journal, name, value)
                    if name == phase:
                        raise OSError("journal境界障害")
                with patch.object(self.registry.generation, "_marker", side_effect=fail):
                    self.assert_code("GENERATION_FAILED", lambda: self.command("restore", self.restore, "restore-01"))
                current = self.state()["generation"]
                self.token["expectedGeneration"] = current
                self.recover(dry=True)
                result = self.recover()
                self.assertEqual(self.restore["generationId"], result["generation"])
                self.assertTrue(self.recover()["replayed"])
                self.assertTrue(self.generation.exists())
                self.assertTrue(self.new.exists())

    def test_atomic_replace_failure_preserves_previous_and_resume(self):
        original = storage.move
        def fail(source, destination, *, replace=False):
            if replace:
                raise PermissionError("共有違反")
            return original(source, destination, replace=replace)
        with patch.object(storage, "move", side_effect=fail):
            self.assert_code("GENERATION_FAILED", lambda: self.command("restore", self.restore, "restore-01"))
        self.assertEqual(self.prepare_payload["generationId"], read_json(self.generation.parent.parent / "active.json")["generationId"])
        self.assertEqual(self.restore["generationId"], self.recover()["generation"])

    def test_partial_copy_can_only_rollback_and_keeps_bytes(self):
        original = self.registry.generation._write_tree
        count = 0
        def fail(root, name, data):
            nonlocal count
            original(root, name, data)
            count += 1
            if count == 3:
                raise OSError("コピー障害")
        with patch.object(self.registry.generation, "_write_tree", side_effect=fail):
            self.assert_code("GENERATION_FAILED", lambda: self.command("restore", self.restore, "restore-01"))
        with self.assertRaises((OSError, RegistryError)):
            self.recover(dry=True)
        stage = self.generation.parent.parent / "staging/restore-01"
        files = list(stage.rglob("*"))
        self.recover(action="rollback")
        self.assertEqual(files, list(stage.rglob("*")))
        self.assertEqual(self.prepare_payload["generationId"], self.state()["generation"])

    def test_corrupt_new_generation_cannot_resume(self):
        with patch.object(self.registry.generation, "_commit", side_effect=OSError("切替前停止")):
            self.assert_code("GENERATION_FAILED", lambda: self.command("restore", self.restore, "restore-01"))
        stage = self.generation.parent.parent / "staging/restore-01/world/level.dat"
        stage.write_bytes(b"changed")
        self.assert_code("GENERATION_MISMATCH", lambda: self.recover())
        self.assertTrue(stage.exists())

    def test_restart_fences_and_requires_explicit_recover(self):
        self.registry.begin_supervision()
        self.registry.begin_supervision()
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.recover(target=None)
        self.assertEqual("LEASED_STOPPED", self.state()["state"])

    def test_alive_reused_pid_and_unknown_writer_are_not_cleared(self):
        with patch.object(self.adapter, "identity", return_value={"pid": 10001}):
            self.assert_code("PROCESS_MISMATCH", lambda: self.recover(target=None))
        with patch.object(self.adapter, "recorded_members", return_value=[123]):
            self.assert_code("PROCESS_MISMATCH", lambda: self.recover(target=None))
        with self.registry.transaction() as connection:
            connection.execute("UPDATE instances SET children=1 WHERE id='persistent-01'")
        self.assert_code("PROOF_REQUIRED", lambda: self.recover(target=None))

    def test_snapshot_publication_before_database_can_resume(self):
        # 別のsnapshotを公開した直後の障害を注入する。
        self.payload["snapshotId"] = "snapshot-02"
        original = self.registry.snapshot._clean
        def fail(payload, *, copying=False):
            if (self.root / "snapshots/snapshot-02").exists():
                raise OSError("DB登録前停止")
            return original(payload, copying=copying)
        with patch.object(self.registry.snapshot, "_clean", side_effect=fail):
            self.assert_code("SNAPSHOT_FAILED", lambda: self.create("snapshot-create-02"))
        self.recover(target="snapshot-create-02")
        result = self.command("snapshot", {"action": "verify", "snapshotId": "snapshot-02"}, "verify-recovered")
        self.assertTrue(result["verified"])

    def test_committed_partial_writing_can_resume_without_losing_evidence(self):
        original = storage.write
        def fail(path, data, **kwargs):
            if path.name == "committed.json":
                original(path.with_name(path.name + ".writing"), data[:10])
                raise OSError("COMMITTED書込み途中")
            return original(path, data, **kwargs)
        with patch.object(storage, "write", side_effect=fail):
            self.assert_code("GENERATION_FAILED", lambda: self.command("restore", self.restore, "restore-01"))
        self.token["expectedGeneration"] = self.restore["generationId"]
        self.recover()
        journal = self.root / "manager/operations/generation-restore-01"
        self.assertTrue((journal / "committed.json.writing").exists())
        self.assertTrue((journal / "committed.json").exists())

    def test_initial_partial_prepare_rollback_preserves_and_reuses_slot(self):
        token = self.acquire("persistent-02", "acquire-second")
        payload = dict(token, artifactId=self.artifact, generationId="00000000-0000-4000-8000-000000000004", seed=1, acceptEula=True)
        with patch.object(self.registry.generation, "_write_tree", side_effect=OSError("初回コピー障害")):
            self.assert_code("GENERATION_FAILED", lambda: self.command("prepare", payload, "initial-prepare"))
        self.command("recover", dict(token, action="rollback", targetOperationId="initial-prepare", ttlSeconds=120), "initial-rollback")
        self.assertTrue((self.root / "quarantine/persistent-02-initial-prepare").exists())
        self.assertTrue(self.command("prepare", payload, "initial-prepare-retry")["prepared"])

    def test_initial_journal_failure_before_root_can_rollback(self):
        token = self.acquire("persistent-02", "acquire-second")
        payload = dict(token, artifactId=self.artifact, generationId="00000000-0000-4000-8000-000000000004", seed=1, acceptEula=True)
        with patch.object(storage, "mkdir", side_effect=OSError("journal作成前")):
            self.assert_code("GENERATION_FAILED", lambda: self.command("prepare", payload, "initial-prepare"))
        self.command("recover", dict(token, action="rollback", targetOperationId="initial-prepare", ttlSeconds=120), "initial-rollback")
        self.assertTrue(self.command("prepare", payload, "initial-prepare-retry")["prepared"])

    def test_snapshot_rollback_journal_failure_keeps_request_for_replay(self):
        self.payload["snapshotId"] = "snapshot-02"
        with patch("dev_server.snapshot_control.write", side_effect=OSError("requested公開前")):
            self.assert_code("SNAPSHOT_FAILED", lambda: self.create("snapshot-create-02"))
        original = self.registry.generation._marker
        def fail(journal, name, value):
            if name == "rolled-back":
                raise OSError("rollback確定前")
            return original(journal, name, value)
        with patch.object(self.registry.generation, "_marker", side_effect=fail), self.assertRaises(OSError):
            self.recover(target="snapshot-create-02", action="rollback")
        self.recover(target="snapshot-create-02", action="rollback")
        self.assertFalse(self.state()["pending"])

    def test_initial_rollback_database_gap_keeps_request_for_replay(self):
        token = self.acquire("persistent-02", "acquire-second")
        payload = dict(token, artifactId=self.artifact, generationId="00000000-0000-4000-8000-000000000004", seed=1, acceptEula=True)
        with patch.object(storage, "mkdir", side_effect=OSError("journal作成前")):
            self.assert_code("GENERATION_FAILED", lambda: self.command("prepare", payload, "initial-prepare"))
        original = self.registry.transaction
        @contextmanager
        def interrupted(*args, **kwargs):
            completed = False
            with original(*args, **kwargs) as connection:
                yield connection
                target = connection.execute("SELECT status FROM runtime_operations WHERE id='initial-prepare'").fetchone()
                recovery = connection.execute("SELECT status FROM runtime_operations WHERE id='initial-rollback'").fetchone()
                completed = target["status"] == "COMPLETED" and recovery is not None and recovery["status"] == "PENDING"
            if completed:
                raise OSError("対象DB確定直後")
        recovery = dict(token, action="rollback", targetOperationId="initial-prepare", ttlSeconds=120)
        with patch.object(self.registry, "transaction", side_effect=interrupted), self.assertRaises(OSError):
            self.command("recover", recovery, "initial-rollback")
        self.command("recover", recovery, "initial-rollback")
        self.assertTrue(self.command("prepare", payload, "initial-prepare-retry")["prepared"])

    def test_requested_snapshot_failure_can_rollback(self):
        self.payload["snapshotId"] = "snapshot-02"
        original = snapshot_tests.write
        def fail(path, data):
            if path.name == "requested.json":
                raise OSError("snapshot要求公開前")
            return original(path, data)
        with patch("dev_server.snapshot_control.write", side_effect=fail):
            self.assert_code("SNAPSHOT_FAILED", lambda: self.create("snapshot-create-02"))
        self.recover(target="snapshot-create-02", action="rollback")
        self.assertEqual("LEASED_STOPPED", self.state()["state"])
        self.assertFalse(self.state()["pending"])

    def test_unleased_slot_survives_supervisor_restart(self):
        self.registry.begin_supervision()
        self.registry.begin_supervision()
        payload = {"instanceId": "persistent-02", "leaseId": None, "epoch": 0, "expectedGeneration": None,
                   "action": "resume", "targetOperationId": None, "ttlSeconds": 120}
        self.command("recover", payload, "recover-empty", True)
        self.command("recover", payload, "recover-empty")
        self.assertTrue(self.command("recover", payload, "recover-empty")["replayed"])
        self.assertTrue(self.acquire("persistent-02", "acquire-recovered")["leaseId"])
