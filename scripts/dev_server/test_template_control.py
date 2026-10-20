"""共有候補の独立コピー・設定正規化・障害保全を合成runtimeと実FSで確認する。"""
import sys
import json
import unittest
from unittest.mock import patch

from .build_inputs import canonical
from .registry import RegistryError
from .fixtures import FIXTURES
from .contracts import ROOT_FILES
from .snapshot_control import verify_snapshot, write
from .template_data import shared_data
from .snapshot_data import entry
from . import storage
from . import test_snapshot_control as snapshot_tests


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSのprivate root試験")
class CandidateTests(unittest.TestCase):
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
        for name in ROOT_FILES - {"server.properties"}:
            (self.generation / name).write_bytes(canonical([{"syntheticPrivateAccount": name}]))
        self.create()
        self.source_bytes = {file["path"]: (self.destination / "data" / file["path"]).read_bytes()
                             for file in verify_snapshot(self.destination)["files"]}
        self.candidate_payload = dict(self.token, action="candidate", snapshotId="shared-candidate", sourceSnapshotId="snapshot-01")

    def candidate(self, operation="candidate-01", dry=False):
        return self.command("snapshot", self.candidate_payload, operation, dry)

    def test_candidate_accounts_empty_original_retained_and_replay(self):
        self.candidate()
        target = self.root / "snapshots/shared-candidate"
        manifest = verify_snapshot(target)
        self.assertEqual("shared-template", manifest["visibility"])
        for name, data in self.source_bytes.items():
            copied = (target / "data" / name).read_bytes()
            self.assertEqual(b"[]\n" if name in ROOT_FILES - {"server.properties"} else data, copied)
            self.assertEqual(data, (self.destination / "data" / name).read_bytes())
            self.assertEqual(1, (target / "data" / name).stat().st_nlink)
        self.assertTrue(self.candidate()["replayed"])

    def test_candidate_dry_run_does_not_publish(self):
        self.assertTrue(self.candidate(dry=True)["dryRun"])
        self.assertFalse((self.root / "snapshots/shared-candidate").exists())

    def test_candidate_different_stop_run_rejected(self):
        self.start("new-start")
        self.stop("new-stop")
        self.assert_code("PROOF_MISMATCH", self.candidate)

    def test_candidate_copy_failure_retains_private_snapshot(self):
        def fail(path, data):
            if path.name == "opaque.bin":
                raise OSError("injected")
            write(path, data)
        with patch("dev_server.snapshot_control.write", side_effect=fail):
            self.assert_code("SNAPSHOT_FAILED", self.candidate)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertEqual("private", verify_snapshot(self.destination)["visibility"])

    def test_candidate_recovery_after_publication(self):
        def fail(path, data):
            if path.name == "published.json":
                raise OSError("injected")
            write(path, data)
        with patch("dev_server.snapshot_control.write", side_effect=fail):
            self.assert_code("SNAPSHOT_FAILED", self.candidate)
        payload = dict(self.token, action="resume", targetOperationId="candidate-01", ttlSeconds=120)
        self.command("recover", payload, "recover-candidate")
        manifest = self.registry.snapshot._registered("shared-candidate")
        self.assertEqual("shared-template", manifest["visibility"])


class SharedSettingsTests(unittest.TestCase):
    def test_invalid_account_object_is_not_silently_cleared(self):
        from .registry import RegistryError
        with self.assertRaises(RegistryError):
            shared_data("ops.json", b"{}")

    def test_portable_settings_normalization_is_idempotent(self):
        private = b"level-name=world\nlevel-seed=123\nrcon.password=synthetic-secret\nserver-port=26085\n"
        normalized = shared_data("server.properties", private)
        self.assertNotIn(b"synthetic-secret", normalized)
        self.assertEqual(normalized, shared_data("server.properties", normalized))


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSのprivate root試験")
class TemplateTests(unittest.TestCase):
    quota_bytes = 10000000
    command = CandidateTests.command
    acquire = CandidateTests.acquire
    assert_code = CandidateTests.assert_code
    start = CandidateTests.start
    stop = CandidateTests.stop
    rcon_stop = CandidateTests.rcon_stop
    state = CandidateTests.state
    create = CandidateTests.create

    def prepare(self):
        self.prepare_payload["seed"] = FIXTURES[0]["seed"]
        return CandidateTests.prepare(self)

    def setUp(self):
        snapshot_tests.SnapshotTests.setUp(self)
        self.spec = FIXTURES[0]
        self.fixture_names = [self.spec["expected"][key] for key in ("vanillaPlayerFile", "modProfileFile", "itemDefinitionFile", "unknownRegularFile")]
        self.fixture_names += self.spec["expected"]["dimensionMarkers"]
        for name in self.fixture_names:
            path = self.generation / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic-fixture-for-fake-runtime\n")
        self.create()
        self.report("persistent-01", "create", self.generation)
        self.command("snapshot", dict(self.token, action="candidate", snapshotId="candidate-01", sourceSnapshotId="snapshot-01"), "candidate-01")
        self.other = self.acquire("persistent-02", "acquire-other")
        self.command("restore", dict(self.other, artifactId=self.artifact, generationId="00000000-0000-4000-8000-000000000012", acceptEula=True, snapshotId="candidate-01"), "restore-other")
        self.other["expectedGeneration"] = "00000000-0000-4000-8000-000000000012"
        self.restored = self.root / "instances/persistent-02/generations/00000000-0000-4000-8000-000000000012"
        self.command("start", dict(self.other, timeoutSeconds=2), "start-other")
        self.report("persistent-02", "verify", self.restored)
        self.command("stop", dict(self.other, timeoutSeconds=2), "stop-other")
        self.validation = dict(self.other, action="validate", snapshotId="candidate-01", fixture="smoke-v1", validationId="validation-01")
        self.promotion = dict(self.other, action="promote", snapshotId="candidate-01", templateId="smoke-v1", validationId="validation-01",
                              approvedBy=self.inventory["operator"], taskId="issue-534-test", purpose="使い捨て合成試験")

    def report(self, instance, action, generation):
        with self.registry.transaction(read_only=True) as connection:
            row = connection.execute("SELECT * FROM instances WHERE id=?", (instance,)).fetchone()
            run = json.loads(connection.execute("SELECT record FROM runs WHERE id=?", (row["run_id"],)).fetchone()[0])
        document = {"schemaVersion": 1, "fixture": "smoke-v1", "seed": self.spec["seed"], "action": action,
                    "instanceId": instance, "generationId": row["generation"], "nonce": run["nonce"], "oreRevivalSecond": 12,
                    "inventoryCount": 0, "money": 0, "internalId": self.spec["itemDefinition"]["internalId"],
                    "files": [entry(name, (generation / name).read_bytes()) for name in sorted(self.fixture_names)]}
        path = self.root / "instances" / instance / "diagnostics" / row["run_id"] / ("fixture-smoke-v1-" + action + ".json")
        storage.write(path, canonical(document))
        return path

    def validate(self, operation="validate-01", dry=False):
        return self.command("snapshot", self.validation, operation, dry)

    def promote(self, operation="promote-01", dry=False):
        return self.command("snapshot", self.promotion, operation, dry)

    def test_validated_approved_template_can_reset_and_sources_unchanged(self):
        original = (self.destination / "manifest.json").read_bytes()
        candidate = (self.root / "snapshots/candidate-01/manifest.json").read_bytes()
        self.validate()
        self.assertTrue(self.validate()["replayed"])
        self.promote()
        self.assertTrue(self.promote()["replayed"])
        request = json.loads((self.root / "manager/operations/template-promote-01/requested.json").read_bytes())
        self.assertEqual("codex-test", request["owner"])
        self.assert_code("EXISTING_DATA", lambda: self.promote("another-promote"))
        payload = dict(self.other, artifactId=self.artifact, generationId="00000000-0000-4000-8000-000000000013", templateId="smoke-v1", acceptEula=True)
        self.command("reset", payload, "reset-template")
        target = self.root / "instances/persistent-02/generations/00000000-0000-4000-8000-000000000013"
        self.assertEqual((self.restored / self.fixture_names[0]).read_bytes(), (target / self.fixture_names[0]).read_bytes())
        self.assertEqual(original, (self.destination / "manifest.json").read_bytes())
        self.assertEqual(candidate, (self.root / "snapshots/candidate-01/manifest.json").read_bytes())

    def test_no_validation_and_wrong_operator_are_rejected(self):
        with self.assertRaises((OSError, RegistryError)):
            self.promote()
        self.validate()
        self.assert_code("APPROVAL_REQUIRED", lambda: self.command("snapshot", dict(self.promotion, approvedBy="not-the-operator"), "bad-approval"))
        self.assertFalse((self.root / "templates/smoke-v1.json").exists())

    def test_same_slot_wrong_candidate_and_api_values_rejected(self):
        self.assert_code("VALIDATION_REQUIRED", lambda: self.command("snapshot", dict(self.validation, **self.token), "same-slot"))
        self.assert_code("TEMPLATE_MISMATCH", lambda: self.command("snapshot", dict(self.validation, snapshotId="snapshot-01"), "private-snapshot"))
        path = self.root / "instances/persistent-02/diagnostics"
        report = next(path.glob("*/fixture-smoke-v1-verify.json"))
        document = json.loads(report.read_bytes())
        document["money"] = 999
        report.write_bytes(canonical(document))
        self.assert_code("PROOF_MISMATCH", self.validate)

    def test_dry_run_does_not_create_records_and_operation_payload_is_fixed(self):
        self.assertTrue(self.validate(dry=True)["dryRun"])
        self.assertFalse((self.root / "manager/audit/validation-validation-01.json").exists())
        self.validate()
        self.assertTrue(self.promote(dry=True)["dryRun"])
        self.assertFalse((self.root / "templates/smoke-v1.json").exists())
        self.promote()
        self.assert_code("OPERATION_CONFLICT", lambda: self.command("snapshot", dict(self.promotion, purpose="別の用途"), "promote-01"))

    def test_validation_hash_tamper_and_restored_data_tamper_rejected(self):
        self.validate()
        path = self.root / "manager/audit/validation-validation-01.json"
        raw = path.read_bytes()
        document = json.loads(raw)
        document["manifestHash"] = "0" * 64
        path.write_bytes(canonical(document))
        self.assert_code("VALIDATION_REQUIRED", self.promote)
        path.write_bytes(raw)
        (self.restored / self.fixture_names[0]).write_bytes(b"tampered")
        self.assert_code("SOURCE_CHANGED", self.promote)

    def test_failed_promotion_requires_matching_resume_and_never_rolls_back(self):
        self.validate()
        write = storage.write
        def fail(path, data, **kwargs):
            if path.name == "smoke-v1.json":
                write(path, data, **kwargs)
                raise OSError("after-publication")
            write(path, data, **kwargs)
        with patch("dev_server.template_control.storage.write", side_effect=fail):
            self.assert_code("TEMPLATE_FAILED", self.promote)
        from .template_control import TemplateControl
        self.assert_code("TEMPLATE_MISMATCH", lambda: TemplateControl(self.registry).registered("smoke-v1"))
        payload = dict(self.other, action="rollback", targetOperationId="promote-01", ttlSeconds=120)
        self.assert_code("RECOVERY_REQUIRED", lambda: self.command("recover", payload, "recover-promote"))
        payload["action"] = "resume"
        self.assertTrue(self.command("recover", payload, "resume-promote")["recovered"])
        self.assertEqual("smoke-v1", TemplateControl(self.registry).registered("smoke-v1")["templateId"])

    def test_failed_validation_resume_checks_published_data_and_owner(self):
        write = storage.write
        def fail(path, data, **kwargs):
            write(path, data, **kwargs)
            if path.name == "validation-validation-01.json":
                raise OSError("after-publication")
        with patch("dev_server.template_control.storage.write", side_effect=fail):
            self.assert_code("TEMPLATE_FAILED", self.validate)
        payload = dict(self.other, action="resume", targetOperationId="validate-01", ttlSeconds=120)
        journal = self.root / "manager/operations/template-validate-01/requested.json"
        original = journal.read_bytes()
        document = json.loads(original)
        document["owner"] = "other-owner"
        journal.write_bytes(canonical(document))
        self.assert_code("JOURNAL_MISMATCH", lambda: self.command("recover", payload, "wrong-owner"))
        journal.write_bytes(original)
        report = next((self.root / "instances/persistent-02/diagnostics").glob("*/fixture-smoke-v1-verify.json"))
        original_report = report.read_bytes()
        report.write_bytes(canonical(dict(json.loads(original_report), money=999)))
        self.assert_code("PROOF_MISMATCH", lambda: self.command("recover", payload, "wrong-proof"))
        report.write_bytes(original_report)
        self.assertTrue(self.command("recover", payload, "resume-validation")["recovered"])
        self.assertTrue(self.promote()["promoted"])
