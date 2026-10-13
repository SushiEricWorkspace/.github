"""持込元の不変性、排他、独立復元・昇格を合成runtimeと実FSで検証する。"""
import json
from pathlib import Path
import platform
import subprocess
import sys
import unittest
from unittest.mock import patch

from .contracts import ROOT_FILES
from .import_data import IMPORTED_TEMPLATES, IMPORT_CHECKS
from .snapshot_data import properties
from . import test_runtime_control as runtime_tests
from .registry import RegistryError
from . import storage


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSのprivate root試験")
class ImportTests(unittest.TestCase):
    quota_bytes = 10000000
    command = runtime_tests.RuntimeControlTests.command
    acquire = runtime_tests.RuntimeControlTests.acquire
    assert_code = runtime_tests.RuntimeControlTests.assert_code
    start = runtime_tests.RuntimeControlTests.start
    stop = runtime_tests.RuntimeControlTests.stop
    rcon_stop = runtime_tests.RuntimeControlTests.rcon_stop
    state = runtime_tests.RuntimeControlTests.state

    def setUp(self):
        runtime_tests.RuntimeControlTests.setUp(self)
        self.source = Path(self.temporary.name) / "human-run"
        data = {
            "server.properties": b"level-name=world\nlevel-seed=1234\nserver-port=25565\nrcon.password=original-secret\n",
            "world/level.dat": b"synthetic-not-NBT",
            "world/session.lock": b"original-lock",
            "world/dimensions/custom/example/opaque": b"custom-world",
            "world/playerdata/player.dat": b"existing-progress",
            "config/SushiEricServerMod/player_data/player/profile.yml": b"money: 1234\n",
            "config/SushiEricServerMod/config.yml": b"mining:\n  ore_revival_second: 12\nmoney:\n  history_save_interval_second: 30\n",
            "config/SushiEricServerMod/management/config.yml": b"port: 25580\n",
            "logs/latest.log": b"excluded-secret",
            "crash-reports/crash.txt": b"excluded-crash",
            "usercache.json": b"[]",
            ".DS_Store": b"metadata",
            "config/.DS_Store": b"metadata",
        }
        data.update({name: b'[{"name":"private-account"}]' for name in ROOT_FILES - {"server.properties"}})
        for name, value in data.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (self.source / "resources").mkdir()
        self.original = self.source_bytes()
        self.payload = dict(self.token, artifactId=self.artifact, generationId="imported-01", acceptEula=True,
                            sourceDirectory=str(self.source), sourceHash=None, sourceStopped=True,
                            approvedBy=self.inventory["operator"], taskId="issue-31-test", purpose="持込データの合成試験")
        self.template = next(name for name, system in IMPORTED_TEMPLATES.items() if system == platform.system())

    def source_bytes(self):
        return {path.relative_to(self.source).as_posix(): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}

    def imported(self):
        result = self.command("import", self.payload, "import-source", True)
        self.payload["sourceHash"] = result["sourceHash"]
        result = self.command("import", self.payload, "import-source")
        self.token["expectedGeneration"] = result["generation"]
        self.generation = self.root / "instances/persistent-01/generations/imported-01"
        return result

    def validated(self):
        self.imported()
        self.start()
        self.stop()
        self.command("snapshot", dict(self.token, action="create", snapshotId="private-01"), "snapshot-source")
        self.command("snapshot", dict(self.token, action="candidate", snapshotId="candidate-01", sourceSnapshotId="private-01"), "candidate-source")
        self.other = self.acquire("persistent-02", "acquire-other")
        self.command("restore", dict(self.other, artifactId=self.artifact, generationId="restored-01", acceptEula=True,
                                     snapshotId="candidate-01"), "restore-other")
        self.other["expectedGeneration"] = "restored-01"
        self.command("start", dict(self.other, timeoutSeconds=2), "start-other")
        self.command("stop", dict(self.other, timeoutSeconds=2), "stop-other")
        self.validation = dict(self.other, action="validate", snapshotId="candidate-01", fixture=self.template, validationId="validation-01")
        return self.command("snapshot", self.validation, "validate-import")

    def test_copy_normalizes_settings_and_preserves_source(self):
        self.assertTrue(self.imported()["prepared"])
        self.assertEqual(self.original, self.source_bytes())
        self.assertEqual(b"existing-progress", (self.generation / "world/playerdata/player.dat").read_bytes())
        self.assertEqual(b"money: 1234\n", (self.generation / "config/SushiEricServerMod/player_data/player/profile.yml").read_bytes())
        for name in ROOT_FILES - {"server.properties"}:
            self.assertEqual(b"[]\n", (self.generation / name).read_bytes())
        for name in ("world/session.lock", "logs", "crash-reports", "usercache.json", ".DS_Store", "config/.DS_Store", "resources"):
            self.assertFalse((self.generation / name).exists())
        settings = properties((self.generation / "server.properties").read_bytes())
        self.assertNotEqual("original-secret", settings["rcon.password"])
        self.assertTrue(self.command("import", self.payload, "import-source")["replayed"])

    def test_completed_replay_does_not_reopen_source(self):
        self.imported()
        with patch("dev_server.generation_control.source_guard", side_effect=RegistryError("SOURCE_BUSY", "再起動中")):
            self.assertTrue(self.command("import", self.payload, "import-source")["replayed"])
        preserved = self.source.with_name("human-run-moved")
        self.source.rename(preserved)
        self.assertTrue(self.command("import", self.payload, "import-source")["replayed"])

    def test_dry_run_is_read_only_and_requires_matching_hash(self):
        result = self.command("import", self.payload, "import-source", True)
        self.assertEqual(platform.system(), result["platform"])
        self.assertEqual(self.original, self.source_bytes())
        self.assertFalse((self.root / "instances/persistent-01").exists())
        self.assert_code("SOURCE_CHANGED", lambda: self.command("import", self.payload, "import-source"))
        self.payload["sourceHash"] = result["sourceHash"]
        (self.source / "world/level.dat").write_bytes(b"changed")
        self.assert_code("SOURCE_CHANGED", lambda: self.command("import", self.payload, "import-source"))
        self.assertEqual("LEASED_STOPPED", self.state()["state"])

    def test_operator_stop_confirmation_and_payload_required(self):
        for changes in ({"sourceStopped": False}, {"approvedBy": "another"}, {"purpose": " "}):
            self.assert_code("APPROVAL_REQUIRED", lambda: self.command("import", dict(self.payload, **changes), "bad", True))
        payload = dict(self.payload)
        del payload["sourceDirectory"]
        self.assert_code("INVALID_INPUT", lambda: self.command("import", payload, "bad", True))

    def test_unknown_resources_settings_database_and_metadata_rejected(self):
        for name, data, code in (("resources/custom", b"unknown", "ROOT_MISMATCH"),
                                 ("config/unknown.yml", b"secret: 123", "SETTINGS_UNSUPPORTED"),
                                 ("world/state.sqlite", b"database", "ROOT_MISMATCH")):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.assert_code(code, lambda: self.command("import", self.payload, "bad", True))
            path.unlink()
        (self.source / ".DS_Store").unlink()
        (self.source / ".DS_Store").mkdir()
        self.assert_code("UNSUPPORTED_FILE", lambda: self.command("import", self.payload, "bad", True))

    def test_source_lock_conflict_does_not_mutate(self):
        script = '''import os,sys
f=open(sys.argv[1],"r+b")
if os.name=="nt":
 import msvcrt
 msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
else:
 import fcntl
 fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
print("locked",flush=True)
sys.stdin.readline()
'''
        process = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.source / "world/session.lock")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual("locked\n", process.stdout.readline())
            self.assert_code("SOURCE_BUSY", lambda: self.command("import", self.payload, "busy", True))
        finally:
            process.communicate("done\n", timeout=10)
        self.assertEqual(self.original, self.source_bytes())

    def test_source_change_during_copy_keeps_partial_generation(self):
        result = self.command("import", self.payload, "import-source", True)
        self.payload["sourceHash"] = result["sourceHash"]
        original = self.registry.generation._write_tree
        def change(root, name, data):
            original(root, name, data)
            if name == "world/level.dat":
                (self.source / "world/level.dat").write_bytes(b"changed-during-copy")
        with patch.object(self.registry.generation, "_write_tree", side_effect=change):
            self.assert_code("SOURCE_CHANGED", lambda: self.command("import", self.payload, "import-source"))
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertTrue((self.root / "instances/persistent-01/staging/import-source/world/level.dat").exists())

    def test_import_into_stopped_slot_preserves_previous_generation(self):
        old = runtime_tests.RuntimeControlTests.prepare(self)
        previous = (old / "generation.json").read_bytes()
        self.start("old-start")
        self.stop("old-stop")
        self.payload["expectedGeneration"] = self.token["expectedGeneration"]
        self.imported()
        self.assertEqual(previous, (old / "generation.json").read_bytes())
        self.assertEqual(self.original, self.source_bytes())

    def test_prepared_import_recovery_rechecks_source_and_resumes(self):
        self.payload["sourceHash"] = self.command("import", self.payload, "import-source", True)["sourceHash"]
        original = storage.write
        def fail(path, data, **kwargs):
            if path.name == "switched.json":
                raise OSError("injected")
            original(path, data, **kwargs)
        with patch("dev_server.generation_control.storage.write", side_effect=fail):
            self.assert_code("GENERATION_FAILED", lambda: self.command("import", self.payload, "import-source"))
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        recovery = dict(self.token, action="resume", targetOperationId="import-source", ttlSeconds=120)
        source_file = self.source / "world/level.dat"
        original_data = source_file.read_bytes()
        source_file.write_bytes(b"changed")
        self.assert_code("SOURCE_CHANGED", lambda: self.command("recover", recovery, "recover-import", True))
        source_file.write_bytes(original_data)
        self.command("recover", recovery, "recover-import", True)
        result = self.command("recover", recovery, "recover-import")
        self.assertEqual("imported-01", result["generation"])
        self.assertEqual(self.original, self.source_bytes())

    def test_committed_import_recovery_completes_journal(self):
        self.payload["sourceHash"] = self.command("import", self.payload, "import-source", True)["sourceHash"]
        original = storage.write
        def fail(path, data, **kwargs):
            if path.name == "committed.json":
                raise OSError("injected")
            original(path, data, **kwargs)
        with patch("dev_server.generation_control.storage.write", side_effect=fail):
            self.assert_code("GENERATION_FAILED", lambda: self.command("import", self.payload, "import-source"))
        self.token["expectedGeneration"] = "imported-01"
        recovery = dict(self.token, action="resume", targetOperationId="import-source", ttlSeconds=120)
        self.command("recover", recovery, "recover-import", True)
        self.command("recover", recovery, "recover-import")
        self.assertTrue((self.root / "manager/operations/generation-import-source/committed.json").exists())
        self.assertEqual(self.original, self.source_bytes())

    def test_imported_template_independent_restore_promote_reset(self):
        self.validated()
        audit = json.loads((self.root / "manager/audit/validation-validation-01.json").read_bytes())
        self.assertEqual(IMPORT_CHECKS, audit["validation"]["passedChecks"])
        promotion = dict(self.other, action="promote", snapshotId="candidate-01", templateId=self.template,
                         validationId="validation-01", approvedBy=self.inventory["operator"], taskId="issue-31-test", purpose="resetの初期world")
        self.command("snapshot", promotion, "promote-import")
        self.command("reset", dict(self.other, artifactId=self.artifact, generationId="reset-01", acceptEula=True,
                                   templateId=self.template), "reset-import")
        reset = self.root / "instances/persistent-02/generations/reset-01"
        self.assertEqual(b"existing-progress", (reset / "world/playerdata/player.dat").read_bytes())
        self.assertEqual(self.original, self.source_bytes())

    def test_imported_world_cannot_impersonate_standard_fixture_or_other_os(self):
        self.validated()
        other_os = next(name for name in IMPORTED_TEMPLATES if name != self.template)
        self.assert_code("PROOF_MISMATCH", lambda: self.command("snapshot", dict(self.validation, fixture=other_os, validationId="wrong-os"), "wrong-os"))
        with self.assertRaises((RegistryError, OSError)):
            self.command("snapshot", dict(self.validation, fixture="smoke-v1", validationId="wrong-standard"), "wrong-standard")

    def test_import_journal_tampering_rejected(self):
        self.validated()
        path = self.root / "manager/operations/generation-import-source/requested.json"
        request = json.loads(path.read_bytes())
        request["importSource"]["files"] = []
        path.write_text(json.dumps(request), encoding="utf-8")
        self.assert_code("PROOF_MISMATCH", lambda: self.command("snapshot", dict(self.validation, validationId="changed-source"), "changed-source"))


if __name__ == "__main__":
    unittest.main()
