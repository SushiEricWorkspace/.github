"""世代公開に使うファイル操作の上書き・障害時保全を確認する。"""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from .local_ipc import make_private
from .registry import RegistryError
from . import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        make_private(self.root, directory=True)

    def test_write_and_replace_reference(self):
        active = self.root / "active.json"
        candidate = self.root / "candidate.json"
        storage.write(active, b"old")
        storage.write(candidate, b"new")
        storage.move(candidate, active, replace=True)
        self.assertEqual(active.read_bytes(), b"new")
        self.assertFalse(candidate.exists())

    def test_immutable_publication_preserves_existing(self):
        source = self.root / "source"
        destination = self.root / "destination"
        storage.write(source, b"new")
        storage.write(destination, b"old")
        with self.assertRaises(RegistryError):
            storage.move(source, destination)
        self.assertEqual(source.read_bytes(), b"new")
        self.assertEqual(destination.read_bytes(), b"old")

    def test_replace_refuses_directory(self):
        source = self.root / "source"
        destination = self.root / "destination"
        storage.mkdir(source)
        storage.mkdir(destination)
        with self.assertRaises(RegistryError):
            storage.move(source, destination, replace=True)
        self.assertTrue(source.is_dir())
        self.assertTrue(destination.is_dir())

    def test_failed_flush_preserves_unpublished_bytes(self):
        destination = self.root / "record.json"
        with patch.object(storage.os, "fsync", side_effect=OSError("同期失敗")):
            with self.assertRaises(OSError):
                storage.write(destination, b"evidence")
        self.assertFalse(destination.exists())
        self.assertEqual((self.root / "record.json.writing").read_bytes(), b"evidence")

    def test_failed_publish_preserves_previous_reference(self):
        active = self.root / "active.json"
        storage.write(active, b"old")
        with patch.object(storage, "move", side_effect=PermissionError("共有違反")):
            with self.assertRaises(PermissionError):
                storage.write(self.root / "candidate.json", b"new")
        self.assertEqual(active.read_bytes(), b"old")
        self.assertEqual((self.root / "candidate.json.writing").read_bytes(), b"new")

    @unittest.skipUnless(os.name == "nt", "NTFSの共有違反試験")
    def test_native_sharing_violation_keeps_both_files(self):
        active = self.root / "active.json"
        candidate = self.root / "candidate.json"
        storage.write(active, b"old")
        storage.write(candidate, b"new")
        with active.open("rb"):
            with self.assertRaises(OSError):
                storage.move(candidate, active, replace=True)
        self.assertEqual(active.read_bytes(), b"old")
        self.assertEqual(candidate.read_bytes(), b"new")
        storage.move(candidate, active, replace=True)
        self.assertEqual(active.read_bytes(), b"new")

    def test_long_path_publication(self):
        directory = self.root
        while len(str(directory)) < 290:
            directory = directory / "generation-validation"
            storage.mkdir(directory)
        path = directory / "record.json"
        storage.write(path, b"long-path")
        self.assertEqual(path.read_bytes(), b"long-path")

    def test_resume_complete_writing_and_preserve_partial_writing(self):
        target = self.root / "record.json"
        storage.write(self.root / "record.json.writing", b"partial")
        storage.write(target, b"complete", resume=True)
        self.assertEqual(target.read_bytes(), b"complete")
        self.assertEqual((self.root / "record.json.writing").read_bytes(), b"partial")
        storage.write(target, b"complete", resume=True)
        with self.assertRaises(RegistryError):
            storage.write(target, b"different", resume=True)

    @unittest.skipUnless(os.name != "nt", "Unixのumask試験")
    def test_writing_is_private_before_permission_setting(self):
        target = self.root / "record.json"
        previous = os.umask(0o022)
        try:
            with patch.object(storage, "make_private", side_effect=OSError("権限設定前")), self.assertRaises(OSError):
                storage.write(target, b"record")
        finally:
            os.umask(previous)
        self.assertEqual((self.root / "record.json.writing").stat().st_mode & 0o777, 0o600)
        storage.write(target, b"record", resume=True)
        self.assertEqual(target.read_bytes(), b"record")
