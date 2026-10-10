"""Git入力lockの拒否試験。実サーバーの受入試験ではない。"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_server.build_inputs import capture_source, verify_sources
from dev_server.contracts import ContractError


class BuildInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "source.txt").write_text("before")
        (self.root / ".gitignore").write_text("build/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def lock(self):
        return {"sourceLockVersion": 1, "sources": [capture_source(self.root, "Common")]}

    def test_ignored_outputs_do_not_change_source(self):
        lock = self.lock()
        (self.root / "build").mkdir()
        (self.root / "build/result").write_text("generated")
        verify_sources(lock)

    def test_dirty_changes_and_new_untracked_inputs_are_rejected(self):
        lock = self.lock()
        (self.root / "source.txt").write_text("after")
        with self.assertRaises(ContractError):
            verify_sources(lock)
        dirty = self.lock()
        (self.root / "new.txt").write_text("new input")
        with self.assertRaises(ContractError):
            verify_sources(dirty)
        untracked = self.lock()
        (self.root / "new.txt").write_text("modified input")
        with self.assertRaises(ContractError):
            verify_sources(untracked)

    def test_deleted_file_and_changed_commit_are_rejected(self):
        lock = self.lock()
        (self.root / "source.txt").unlink()
        with self.assertRaises(ContractError):
            verify_sources(lock)
        self.git("add", "-u")
        self.git("commit", "-qm", "changed")
        with self.assertRaises(ContractError):
            verify_sources(lock)

    def test_untracked_bytes_and_patch_are_recorded(self):
        (self.root / "source.txt").write_text("dirty")
        (self.root / "new.txt").write_text("new")
        source = self.lock()["sources"][0]
        self.assertTrue(source["patchBase64"])
        self.assertEqual("new.txt", source["record"]["untrackedInputs"][0]["path"])
