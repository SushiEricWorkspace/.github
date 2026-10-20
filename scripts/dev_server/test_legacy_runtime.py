"""保存済v1の起動・snapshot復元互換を合成bundle/プロセスで確認する。"""
import shutil
import sys
import unittest

from dev_server.build_inputs import digest
from dev_server.contracts import ContractError
from dev_server.runtime_bundle import verify_bundle
from dev_server import test_generation_control as generation_tests


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSのprivate root試験")
class LegacyRuntimeTests(unittest.TestCase):
    bundle_version = 1
    quota_bytes = 10000000
    command = generation_tests.GenerationTests.command
    acquire = generation_tests.GenerationTests.acquire
    assert_code = generation_tests.GenerationTests.assert_code
    prepare = generation_tests.GenerationTests.prepare
    start = generation_tests.GenerationTests.start
    stop = generation_tests.GenerationTests.stop
    rcon_stop = generation_tests.GenerationTests.rcon_stop
    state = generation_tests.GenerationTests.state
    create = generation_tests.GenerationTests.create
    setUp = generation_tests.GenerationTests.setUp

    def bundle_hashes(self):
        bundle = self.root / "artifacts" / self.artifact
        return {p.relative_to(bundle).as_posix(): digest(p.read_bytes())
                for p in bundle.rglob("*") if p.is_file()}

    def test_saved_v1_start_snapshot_restore_keeps_identity_and_bytes(self):
        bundle = self.root / "artifacts" / self.artifact
        before = self.bundle_hashes()
        self.assertEqual(1, verify_bundle(bundle)["schemaVersion"])
        self.command("restore", self.restore, "legacy-restore")
        self.assertEqual((self.generation / "world/level.dat").read_bytes(), (self.new / "world/level.dat").read_bytes())
        self.assertEqual(self.artifact, verify_bundle(self.new / "runtime")["artifactId"])
        self.token["expectedGeneration"] = self.restore["generationId"]
        self.start("legacy-start")
        self.stop("legacy-stop")
        self.assertEqual(before, self.bundle_hashes())
        self.assertEqual(self.artifact, (bundle / "COMPLETE").read_text())

    def test_v1_snapshot_is_not_converted_into_new_v2_artifact(self):
        before = self.bundle_hashes()
        newer = self.bundle_fixture.freeze()
        shutil.copytree(newer, self.root / "artifacts" / newer.name)
        self.assertEqual(2, verify_bundle(newer)["schemaVersion"])
        request = dict(self.restore, artifactId=newer.name, generationId="00000000-0000-4000-8000-000000000002")
        with self.assertRaisesRegex(ContractError, "compatibility"):
            self.command("restore", request, "wrong-artifact")
        self.assertFalse((self.generation.parent / request["generationId"]).exists())
        self.assertEqual(before, self.bundle_hashes())
