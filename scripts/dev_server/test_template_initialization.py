"""初期テンプレートのroot・ビルド境界と失敗時の保全を検証する。"""
import json
import platform
import shutil
import sys
import unittest
from unittest.mock import patch

from .build_lock import BuildLockError, build_lock
from .template_initialization import check_runtime
from .contracts import ContractError
from .runtime_bundle import verify_bundle
from . import test_import_control as imports
from . import test_runtime_control as runtimes


@unittest.skipUnless(sys.platform in ("win32", "darwin"), "対象OSのprivate root試験")
class TemplateInitializationTests(unittest.TestCase):
    def setUp(self):
        self.source = imports.ImportTests("test_imported_template_independent_restore_promote_reset")
        self.source.setUp()
        self.addCleanup(self.source.doCleanups)
        self.source.validated()
        promotion = dict(self.source.other, action="promote", snapshotId="candidate-01",
                         templateId=self.source.template, validationId="validation-01",
                         approvedBy=self.source.inventory["operator"], taskId="issue-35-test", purpose="初期コピー")
        self.source.command("snapshot", promotion, "promote-import")
        self.target = runtimes.RuntimeControlTests("test_prepare_start_stop_replay_restart_and_no_snapshot_clean")
        self.target.setUp()
        self.addCleanup(self.target.doCleanups)
        # 別のModクラスを固定したbundleを作り、異なるartifactを使用する。
        self.target.bundle_fixture.classes.joinpath("Test.class").write_bytes(b"new-mod-build")
        bundle = self.target.bundle_fixture.freeze()
        shutil.copytree(bundle, self.target.root / "artifacts" / bundle.name)
        self.payload = dict(self.target.token, artifactId=bundle.name, generationId="initial-35", acceptEula=True,
                            sourceRoot=str(self.source.root), templateId=self.source.template, templateHash=None,
                            approvedBy=self.target.inventory["operator"], taskId="issue-35-test", purpose="新ビルドの実機確認")

    def initialize(self, operation="template-init-35", dry=False):
        return self.target.command("template-init", self.payload, operation, dry)

    def confirm(self):
        result = self.initialize(dry=True)
        self.payload["templateHash"] = result["templateHash"]
        return result

    def test_other_root_new_artifact_preserves_world_and_starts(self):
        self.confirm()
        self.assertNotEqual(self.source.artifact, self.payload["artifactId"])
        manifest = (self.source.root / "snapshots/candidate-01/manifest.json").read_bytes()
        self.initialize()
        self.target.token["expectedGeneration"] = "initial-35"
        generation = self.target.root / "instances/persistent-01/generations/initial-35"
        self.assertEqual(b"existing-progress", (generation / "world/playerdata/player.dat").read_bytes())
        self.assertEqual(self.source.original, self.source.source_bytes())
        self.assertEqual(manifest, (self.source.root / "snapshots/candidate-01/manifest.json").read_bytes())
        self.target.start()
        self.target.stop()
        self.assertEqual("LEASED_STOPPED", self.target.state()["state"])
        self.target.registry.execute("gc", {"action": "plan"}, operation_id="gc-after-init")

    def test_missing_template_and_wrong_os_never_prepare(self):
        self.payload["templateId"] = "smoke-v1"
        self.target.assert_code("TEMPLATE_MISMATCH", self.initialize)
        self.assertIsNone(self.target.state()["generation"])

    def test_hash_mismatch_and_unconfirmed_copy_have_no_effect(self):
        self.target.assert_code("SOURCE_CHANGED", self.initialize)
        self.confirm()
        self.payload["templateHash"] = "0" * 64
        self.target.assert_code("SOURCE_CHANGED", self.initialize)
        self.assertIsNone(self.target.state()["generation"])

    def test_minecraft_mismatch_is_rejected(self):
        runtime = verify_bundle(self.target.root / "artifacts" / self.payload["artifactId"])
        source = json.loads(json.dumps(runtime))
        next(tool for tool in source["tools"] if tool["name"] == "minecraft")["version"] = "other"
        self.target.assert_code("ARTIFACT_MISMATCH", lambda: check_runtime(
            {"sourceRuntime": source, "layout": {"layoutId": runtime["layoutId"]}}, runtime, self.target.root / "artifacts" / self.payload["artifactId"]))

    def test_source_lock_blocks_copy_and_gc(self):
        self.confirm()
        with build_lock(self.source.root / "manager/locks/deployment.lock"):
            with self.assertRaises(BuildLockError):
                self.initialize()
            with self.assertRaises(BuildLockError):
                self.source.registry.execute("gc", {"action": "plan"}, operation_id="gc-35")
        self.assertIsNone(self.target.state()["generation"])

    def test_completed_replay_does_not_reopen_source(self):
        self.confirm()
        self.initialize()
        with patch("dev_server.generation_control.template_guard", side_effect=AssertionError("source reopened")):
            self.assertTrue(self.initialize()["replayed"])

    def test_copy_failure_preserves_source_and_rolls_back(self):
        self.confirm()
        write = self.target.registry.generation._write_tree
        def fail(root, name, data):
            if name == "world/playerdata/player.dat":
                raise OSError("copy failed")
            return write(root, name, data)
        with patch.object(self.target.registry.generation, "_write_tree", side_effect=fail):
            self.target.assert_code("GENERATION_FAILED", self.initialize)
        recovery = dict(self.target.token, action="rollback", targetOperationId="template-init-35", ttlSeconds=120)
        self.target.command("recover", recovery, "rollback-35")
        self.assertEqual(self.source.original, self.source.source_bytes())
        self.assertIsNone(self.target.state()["generation"])

    def test_prepared_copy_recovers_with_same_template(self):
        self.confirm()
        with patch.object(self.target.registry.generation, "_commit", side_effect=OSError("before commit")):
            self.target.assert_code("GENERATION_FAILED", self.initialize)
        recovery = dict(self.target.token, action="resume", targetOperationId="template-init-35", ttlSeconds=120)
        self.assertTrue(self.target.command("recover", recovery, "resume-35", True)["dryRun"])
        self.target.command("recover", recovery, "resume-35")
        self.assertEqual("initial-35", self.target.state()["generation"])

    def test_reset_still_rejects_new_artifact(self):
        self.payload["sourceRoot"] = str(self.target.root)
        self.target.assert_code("NOT_FOUND", self.initialize)
        shutil.copytree(self.target.root / "artifacts" / self.payload["artifactId"],
                        self.source.root / "artifacts" / self.payload["artifactId"])
        with self.assertRaisesRegex(ContractError, "compatibility"):
            self.source.command("reset", dict(
                self.source.other, artifactId=self.payload["artifactId"], generationId="invalid-reset", acceptEula=True,
                templateId=self.source.template), "reset-35")

    def test_wrong_os_bundle_is_rejected_before_copy(self):
        opposite = "Darwin" if platform.system() == "Windows" else "Windows"
        with patch("dev_server.runtime_bundle.platform.system", return_value=opposite):
            bundle = self.target.bundle_fixture.freeze()
        shutil.copytree(bundle, self.target.root / "artifacts" / bundle.name)
        self.payload["artifactId"] = bundle.name
        self.target.assert_code("ARTIFACT_MISMATCH", self.confirm)
        self.assertIsNone(self.target.state()["generation"])
