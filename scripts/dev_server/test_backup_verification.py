"""バックアップコピーの非上書き・集合照合・失敗時保全を確認する。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .backup_verification import copy_verified_tree, validate_trial_paths
from .contracts import ContractError
from .registry import RegistryError


class BackupVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "empty").mkdir()
        (self.source / "data").write_bytes(b"backup-data")
        self.target = self.root / "backup"

    def test_roundtrip_preserves_empty_directories_and_independent_files(self):
        copied = copy_verified_tree(self.source, self.target)
        restored = copy_verified_tree(self.target, self.root / "restored")
        self.assertEqual(copied, restored)
        self.assertEqual(copied["files"], 1)
        self.assertTrue((self.target / "empty").is_dir())
        (self.source / "data").write_bytes(b"changed")
        self.assertEqual((self.target / "data").read_bytes(), b"backup-data")

    def test_existing_destination_and_nested_destination_are_refused(self):
        self.target.mkdir()
        (self.target / "keep").write_bytes(b"keep")
        with self.assertRaises(RegistryError):
            copy_verified_tree(self.source, self.target)
        with self.assertRaises(RegistryError):
            copy_verified_tree(self.source, self.source / "nested")
        self.assertEqual((self.target / "keep").read_bytes(), b"keep")

    def test_no_space_has_no_copy_effect(self):
        with patch("dev_server.backup_verification.shutil.disk_usage", return_value=type("Usage", (), {"free": 0})()):
            with self.assertRaisesRegex(RegistryError, "空き容量"):
                copy_verified_tree(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_copy_failure_keeps_source_and_partial_destination(self):
        with patch("dev_server.backup_verification.shutil.copyfileobj", side_effect=OSError("コピー失敗")):
            with self.assertRaises(OSError):
                copy_verified_tree(self.source, self.target)
        self.assertEqual((self.source / "data").read_bytes(), b"backup-data")
        self.assertTrue(self.target.exists())

    def test_changed_source_is_not_reported_as_success(self):
        from . import backup_verification
        original = backup_verification._files
        def changed(path):
            if path == self.source and self.target.exists():
                (self.source / "data").write_bytes(b"changed")
            return original(path)
        with patch.object(backup_verification, "_files", side_effect=changed):
            with self.assertRaisesRegex(RegistryError, "一致"):
                copy_verified_tree(self.source, self.target)
        self.assertEqual((self.target / "data").read_bytes(), b"backup-data")

    def test_hardlink_is_refused(self):
        os.link(self.source / "data", self.source / "linked")
        with self.assertRaises((ContractError, RegistryError)):
            copy_verified_tree(self.source, self.target)

    def test_input_bundle_and_output_overlap_are_rejected_before_writes(self):
        for outputs in ([self.source / "backup"], [self.root],
                        [self.target, self.target / "evidence"], [self.target, self.target]):
            with self.subTest(outputs=outputs), self.assertRaises(RegistryError):
                validate_trial_paths(self.source, outputs)
        validate_trial_paths(self.source, [self.target, self.root / "evidence"])
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
