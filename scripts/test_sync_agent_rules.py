"""明示worktreeへの同期がprimaryや未指定repoを書き換えないことを確認する。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("sync_rules", Path(__file__).with_name("sync-agent-rules.py"))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class SyncAgentRulesTest(unittest.TestCase):
    def test_explicit_roots_do_not_include_primary(self):
        with tempfile.TemporaryDirectory(prefix="sushieric-sync-test-") as directory:
            workspace = Path(directory)
            target = workspace / "worktrees/task-a/Common/AGENTS.md"
            self.assertEqual([target.resolve()], sync.target_paths(
                workspace, ["Common=worktrees/task-a/Common"]))

    def test_invalid_duplicate_and_shared_roots_are_rejected(self):
        for pairs in (["Unknown=x"], ["Common="], ["Common=x", "Common=y"],
                      ["Common=x", "SushiEricServerMod=x"]):
            with self.assertRaises(ValueError):
                sync.target_paths(Path.cwd(), pairs)

    def test_only_common_block_is_replaced(self):
        text = f"before\n{sync.START}\nold\n{sync.END}\nafter"
        self.assertEqual(f"before\n{sync.START}\nnew\n{sync.END}\nafter",
                         sync.replace_common_block(text, "new\n"))

    def test_invalid_markers_are_rejected(self):
        for text in ("none", sync.START, sync.END + sync.START,
                     sync.START + sync.START + sync.END):
            with self.assertRaises(ValueError):
                sync.replace_common_block(text, "new")


if __name__ == "__main__":
    unittest.main()
