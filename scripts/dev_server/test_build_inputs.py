"""Git入力lockの拒否試験。実サーバーの受入試験ではない。"""

import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dev_server.build_inputs import capture_source, regular_bytes, verify_sources
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
        return {"sourceLockVersion": 1, "sources": [capture_source(self.root, "SushiEricDataModel")]}

    def test_new_inputs_require_data_model_name(self):
        from dev_server.build_inputs import capture_sources
        repos = {"SushiEricDataModel": self.root, "SushiEricServerMod": self.root}
        self.assertEqual(set(repos), {s["record"]["repo"] for s in capture_sources(repos)["sources"]})
        with self.assertRaises(ContractError):
            capture_sources({"Common": self.root, "SushiEricServerMod": self.root})

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

    def test_limited_read_preserves_default_and_exact_limit(self):
        path = self.root / 'source.txt'
        raw = b'x' * 257
        path.write_bytes(raw)
        self.assertEqual(raw, regular_bytes(path))
        self.assertEqual(raw, regular_bytes(path, maximum=len(raw)))

    def test_limited_read_rejects_oversize_original_without_full_read(self):
        path = self.root / 'source.txt'
        path.write_bytes(b'x' * 292)
        original, reads = Path.open, []

        @contextmanager
        def opened(path, *args, **kwargs):
            with original(path, *args, **kwargs) as source:
                def read(size=-1):
                    raw = source.read(size)
                    reads.append((size, len(raw), kwargs.get('buffering', -1)))
                    return raw
                yield SimpleNamespace(read=read)

        with patch.object(Path, 'open', opened), self.assertRaises(ContractError):
            regular_bytes(path, maximum=128)
        self.assertEqual([(129, 129, 0)], reads)
