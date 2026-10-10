"""実データを使わず、一時Git repositoryでworktreeの分離と拒否条件を確認する。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dev_server.task_worktrees import TaskError, TaskWorktrees, git, worktrees


class TaskWorktreesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sushieric-worktree-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "検証 workspace"
        self.root.mkdir()
        self.manager = TaskWorktrees(self.root)
        for name, branch in (("Common", "develop"), ("SushiEricServerMod", "main")):
            repo = self.root / name
            repo.mkdir()
            git(repo, "init", "-b", branch)
            git(repo, "config", "user.name", "worktree-test")
            git(repo, "config", "user.email", "worktree-test@example.invalid")
            (repo / ".gitignore").write_text("build/\n.gradle/\nrun/\n", encoding="utf-8")
            (repo / "source.txt").write_text("initial\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "test fixture")
            git(repo, "remote", "add", "origin", str(repo))
            git(repo, "fetch", "origin")
            git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
            (repo / "run").mkdir()
            (repo / "run" / "private.txt").write_text("never copy", encoding="utf-8")

    def create(self, task="task-a", issue=100, owner="codex-a", repos=None):
        return self.manager.create(task, issue, owner, repos or {"SushiEricServerMod": None}, fetch=False)

    def test_two_tasks_source_index_and_build_are_independent(self):
        self.create()
        self.create("task-b", 101)
        a = self.manager.target("task-a", "SushiEricServerMod")
        b = self.manager.target("task-b", "SushiEricServerMod")
        primary = self.root / "SushiEricServerMod"
        (a / "source.txt").write_text("changed", encoding="utf-8")
        git(a, "add", "source.txt")
        for target, value in ((a, "a"), (b, "b")):
            for directory in ("build", ".gradle"):
                (target / directory).mkdir()
                (target / directory / "output").write_text(value, encoding="utf-8")
        self.assertTrue(git(a, "diff", "--cached"))
        self.assertFalse(git(b, "diff", "--cached"))
        self.assertFalse(git(primary, "diff", "--cached"))
        self.assertEqual("initial\n", (b / "source.txt").read_text())
        self.assertEqual("a", (a / "build/output").read_text())
        self.assertEqual("b", (b / "build/output").read_text())
        self.assertEqual("main", git(primary, "branch", "--show-current"))
        self.assertFalse((a / "run").exists())

    def test_repo_default_and_explicit_dependency_commit(self):
        common = self.root / "Common"
        base = git(common, "rev-parse", "HEAD")
        result = self.create(repos={"Common": base, "SushiEricServerMod": None})
        self.assertEqual(base, result[0]["repos"][0]["base_commit"])
        self.assertEqual(base, git(self.manager.target("task-a", "Common"), "rev-parse", "HEAD"))
        self.assertEqual("develop", git(common, "branch", "--show-current"))

    def test_create_is_idempotent_for_same_inputs(self):
        first = self.create()
        self.assertEqual(first, self.create())

    def test_handoff_reuses_tree_and_rejects_old_owner(self):
        self.create()
        path = self.manager.target("task-a", "SushiEricServerMod")
        before = git(path, "rev-parse", "HEAD")
        result = self.manager.handoff("task-a", "codex-a", "claude-b")
        self.assertEqual("claude-b", result[0]["owner"])
        self.assertEqual(before, git(path, "rev-parse", "HEAD"))
        with self.assertRaisesRegex(TaskError, "owner"):
            self.manager.remove("task-a", "codex-a")

    def test_double_owner_and_branch_collision_are_rejected(self):
        self.create()
        with self.assertRaisesRegex(TaskError, "owner"):
            self.create(owner="claude")
        with self.assertRaisesRegex(TaskError, "branch"):
            self.create("task-b", 100)
        self.assertFalse(self.manager.target("task-b", "SushiEricServerMod").exists())

    def test_existing_branch_even_if_not_checked_out_is_not_overwritten(self):
        git(self.root / "SushiEricServerMod", "branch", "feature/issue-100")
        with self.assertRaisesRegex(TaskError, "branch"):
            self.create()

    def test_existing_remote_branch_is_not_recreated(self):
        repo = self.root / "SushiEricServerMod"
        git(repo, "update-ref", "refs/remotes/origin/feature/issue-100", git(repo, "rev-parse", "HEAD"))
        with self.assertRaisesRegex(TaskError, "branch"):
            self.create()

    def test_dirty_handoff_and_removal_are_rejected(self):
        self.create()
        path = self.manager.target("task-a", "SushiEricServerMod")
        (path / "new.txt").write_text("unsaved", encoding="utf-8")
        for operation in (lambda: self.manager.handoff("task-a", "codex-a", "claude"),
                          lambda: self.manager.remove("task-a", "codex-a")):
            with self.assertRaisesRegex(TaskError, "未コミット"):
                operation()
        self.assertEqual("ACTIVE", self.manager.list()[0]["state"])
        self.assertTrue(path.exists())

    def test_all_repos_are_preflighted_before_creation(self):
        git(self.root / "SushiEricServerMod", "branch", "feature/issue-100")
        with self.assertRaises(TaskError):
            self.create(repos={"Common": None, "SushiEricServerMod": None})
        self.assertFalse(self.manager.target("task-a", "Common").exists())
        self.assertFalse(git(self.root / "Common", "for-each-ref", "refs/heads/feature/issue-100"))

    def test_primary_outside_paths_and_invalid_names_are_rejected(self):
        for task in ("../Common", "Common", "", "task/primary"):
            with self.assertRaises(TaskError):
                self.create(task)
        for repo in ("../Common", "SushiEricServerMod-codex"):
            with self.assertRaises(TaskError):
                self.create(repos={repo: None})
        self.assertFalse((self.root / "worktrees").exists())

    def test_all_repos_are_preflighted_before_removal(self):
        self.create(repos={"Common": None, "SushiEricServerMod": None})
        mod = self.manager.target("task-a", "SushiEricServerMod")
        (mod / "source.txt").write_text("unsaved", encoding="utf-8")
        with self.assertRaises(TaskError):
            self.manager.remove("task-a", "codex-a")
        self.assertTrue(self.manager.target("task-a", "Common").exists())

    def test_ignored_user_data_prevents_removal(self):
        self.create()
        path = self.manager.target("task-a", "SushiEricServerMod")
        (path / "run").mkdir()
        (path / "run/private").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(TaskError, "追跡外データ"):
            self.manager.remove("task-a", "codex-a")
        self.assertTrue((path / "run/private").exists())

    def test_removal_preserves_runtime_primary_other_tasks_and_legacy(self):
        repo = self.root / "SushiEricServerMod"
        legacy = self.root / "SushiEricServerMod-claude"
        git(repo, "worktree", "add", "--detach", str(legacy), "HEAD")
        self.create()
        self.create("task-b", 101)
        sentinels = []
        for area in ("snapshots", "instances", "artifacts"):
            path = self.root / "minecraft-dev" / area
            path.mkdir(parents=True)
            sentinel = path / "keep"
            sentinel.write_text("protected", encoding="utf-8")
            sentinels.append(sentinel)
        a = self.manager.target("task-a", "SushiEricServerMod")
        (a / "build").mkdir()
        (a / "build/output").write_text("disposable", encoding="utf-8")
        self.manager.remove("task-a", "codex-a")
        self.assertFalse(a.exists())
        self.assertEqual("REMOVED", self.manager.list("task-a")[0]["state"])
        self.assertTrue(self.manager.target("task-b", "SushiEricServerMod").exists())
        self.assertTrue(legacy.resolve() in worktrees(repo))
        self.assertTrue(all(path.read_text() == "protected" for path in sentinels))
        self.assertEqual("main", git(repo, "branch", "--show-current"))
        self.assertTrue(git(repo, "for-each-ref", "refs/heads/feature/issue-100"))

    def test_creation_failure_records_partial_tree_without_deleting_it(self):
        def failing_git(repo, *args):
            if repo.name == "SushiEricServerMod" and args[:2] == ("worktree", "add"):
                raise TaskError("synthetic Git failure")
            return git(repo, *args)
        with patch("dev_server.task_worktrees.git", side_effect=failing_git):
            with self.assertRaisesRegex(TaskError, "synthetic"):
                self.create(repos={"Common": None, "SushiEricServerMod": None})
        self.assertEqual("RECOVERY_REQUIRED", self.manager.list()[0]["state"])
        self.assertTrue(self.manager.target("task-a", "Common").exists())
        self.manager.remove("task-a", "codex-a")
        self.assertFalse(self.manager.target("task-a", "Common").exists())

    def test_concurrent_owners_do_not_share_same_task(self):
        with self.manager.database():
            pass
        def create(owner):
            try:
                self.create(owner=owner)
                return True
            except TaskError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([False, True], sorted(pool.map(create, ("codex", "claude"))))
        self.assertEqual("ACTIVE", self.manager.list()[0]["state"])

    def test_symlink_is_never_followed(self):
        self.create()
        target = self.manager.target("task-a", "SushiEricServerMod") / "build"
        try:
            target.symlink_to(self.root / "Common", target_is_directory=True)
        except OSError as error:
            self.skipTest(f"このホストにはsymlink作成権限がありません: {error}")
        with self.assertRaisesRegex(TaskError, "リンク"):
            self.manager.remove("task-a", "codex-a")
        self.assertTrue((self.root / "Common/source.txt").exists())

    def test_list_does_not_create_workspace_state(self):
        self.assertEqual([], self.manager.list())
        self.assertFalse((self.root / "worktrees").exists())


if __name__ == "__main__":
    unittest.main()
