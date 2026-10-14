"""明示worktreeへの同期がprimaryや未指定repoを書き換えないことを確認する。"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("sync_rules", Path(__file__).with_name("sync-agent-rules.py"))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class SyncAgentRulesTest(unittest.TestCase):
    def run_cli(self, workspace: Path | None, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-X", "utf8", "-B", str(Path(sync.__file__))]
        if workspace is not None:
            command.extend(("--workspace-root", str(workspace)))
        return subprocess.run(
            [*command, *arguments],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )

    def make_workspace(self, workspace: Path) -> dict[str, Path]:
        targets = {}
        for relative in sync.TARGETS:
            name = Path(relative).parts[0]
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"before\n{sync.START}\nold\n{sync.END}\nafter\n", encoding="utf-8")
            targets[name] = target
        return targets

    def test_writes_require_explicit_repo_roots_before_reading_targets(self):
        with tempfile.TemporaryDirectory(prefix="sushieric-sync-test-") as directory:
            workspace = Path(directory)
            targets = self.make_workspace(workspace)
            before = {name: (path.read_bytes(), path.stat().st_mtime_ns) for name, path in targets.items()}

            result = self.run_cli(None)
            workspace_only = self.run_cli(workspace)

            for failure in (result, workspace_only):
                self.assertNotEqual(0, failure.returncode)
                self.assertIn("--repo-root REPO=PATH", failure.stderr)
            self.assertEqual(before, {
                name: (path.read_bytes(), path.stat().st_mtime_ns) for name, path in targets.items()
            })

    def test_explicit_roots_update_only_selected_worktrees(self):
        with tempfile.TemporaryDirectory(prefix="sushieric-sync-test-") as directory:
            workspace = Path(directory)
            targets = self.make_workspace(workspace)
            selected = workspace / "worktrees/task-a/Common"
            selected.mkdir(parents=True)
            (selected / "AGENTS.md").write_text(
                f"before\n{sync.START}\nold\n{sync.END}\nafter\n", encoding="utf-8"
            )
            second = workspace / "worktrees/task-a/SushiEricServerMod"
            second.mkdir(parents=True)
            (second / "AGENTS.md").write_text(
                f"before\n{sync.START}\nold\n{sync.END}\nafter\n", encoding="utf-8"
            )
            untouched = {name: path.read_bytes() for name, path in targets.items()}

            result = self.run_cli(
                workspace,
                "--repo-root", "Common=worktrees/task-a/Common",
                "--repo-root", "SushiEricServerMod=worktrees/task-a/SushiEricServerMod",
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("old", targets["Common"].read_text(encoding="utf-8"))
            self.assertNotIn("old", (selected / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertIn("Organization共通ルール", (selected / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertNotIn("old", (second / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertIn("<!-- COMMON-RULES:START -->\n", (selected / "AGENTS.md").read_text(encoding="utf-8"))
            for name, path in targets.items():
                self.assertEqual(untouched[name], path.read_bytes())

    def test_check_without_roots_reads_all_targets_and_preserves_mtime(self):
        with tempfile.TemporaryDirectory(prefix="sushieric-sync-test-") as directory:
            workspace = Path(directory)
            targets = self.make_workspace(workspace)
            before = {name: path.stat().st_mtime_ns for name, path in targets.items()}

            result = self.run_cli(workspace, "--check")

            self.assertEqual(1, result.returncode)
            for name, path in targets.items():
                self.assertIn(str(path), result.stderr)
                self.assertEqual(before[name], path.stat().st_mtime_ns)

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
