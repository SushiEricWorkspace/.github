"""実データを使わず、一時Git repositoryでworktreeの分離と拒否条件を確認する。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dev_server.task_worktrees import TaskError, TaskWorktrees, git, worktrees, workspace_for
from dev_server.build_lock import build_lock


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

    def test_build_blocks_own_handoff_and_removal_but_not_other_tasks(self):
        self.create()
        self.create("task-b", 101)
        with build_lock(self.root / "worktrees/task-a/.build.lock"):
            with self.assertRaises(TaskError):
                self.manager.handoff("task-a", "codex-a", "claude-b")
            with self.assertRaises(TaskError):
                self.manager.remove("task-a", "codex-a")
            self.manager.handoff("task-b", "codex-a", "claude-b")
        self.manager.handoff("task-a", "codex-a", "claude-b")

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
        path = self.manager.target("task-a", "SushiEricServerMod")
        (path / "build").mkdir()
        target = path / "build/external"
        try:
            target.symlink_to(self.root / "Common", target_is_directory=True)
        except OSError as error:
            self.skipTest(f"このホストにはsymlink作成権限がありません: {error}")
        self.assertFalse(git(path, "status", "--porcelain", "--untracked-files=all"))
        with self.assertRaisesRegex(TaskError, "リンク"):
            self.manager.remove("task-a", "codex-a")
        self.assertEqual("initial\n", (self.root / "Common/source.txt").read_text())
        self.assertTrue(target.is_symlink())
        self.assertEqual("ACTIVE", self.manager.list()[0]["state"])

    def test_list_does_not_create_workspace_state(self):
        self.assertEqual([], self.manager.list())
        self.assertFalse((self.root / "worktrees").exists())

    def test_workspace_is_resolved_from_primary_git_directory(self):
        self.create()
        task = self.manager.target("task-a", "SushiEricServerMod")
        self.assertEqual(self.root.resolve(), workspace_for(task))
        self.assertEqual(self.root.resolve(), workspace_for(self.root / "SushiEricServerMod"))

    def set_state(self, state, *, removal_started=True):
        with self.manager.database() as db:
            if removal_started:
                db.execute("INSERT OR IGNORE INTO removals VALUES ('task-a')")
            db.execute("UPDATE tasks SET state=? WHERE task_id='task-a'", (state,))

    def removed_without_record(self, *, empty=False, residue=False):
        path = self.manager.target("task-a", "SushiEricServerMod")
        git(self.root / "SushiEricServerMod", "worktree", "remove", str(path))
        if empty or residue:
            path.mkdir()
        if residue:
            (path / "keep.txt").write_text("preserve", encoding="utf-8")
        self.set_state("RECOVERY_REQUIRED")
        return path

    def test_recovery_reconciles_absent_and_empty_after_interruption(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                self.create()
                path = self.removed_without_record(empty=empty)
                with self.assertRaises(TaskError):
                    self.manager.remove("task-a", "codex-a")
                result = self.manager.recover_remove("task-a", "codex-a")
                self.assertEqual("REMOVED", result[0]["state"])
                self.assertEqual(0, result[0]["repos"][0]["created"])
                self.assertFalse(path.exists())
                repo = self.root / "SushiEricServerMod"
                self.assertNotIn(path.resolve(), worktrees(repo))
                self.assertTrue(git(repo, "for-each-ref", "refs/heads/feature/issue-100"))
                if not empty:
                    # 次の独立した事例のため、一時registryの登録だけを片付ける。
                    with self.manager.database() as db:
                        db.execute("DELETE FROM removals WHERE task_id='task-a'")
                        db.execute("DELETE FROM repos WHERE task_id='task-a'")
                        db.execute("DELETE FROM tasks WHERE task_id='task-a'")
                    git(repo, "branch", "-d", "feature/issue-100")

    def test_recovery_preserves_nonempty_unregistered_directory(self):
        self.create(repos={"Common": None, "SushiEricServerMod": None})
        path = self.removed_without_record(residue=True)
        with self.assertRaisesRegex(TaskError, "非空"):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertEqual("preserve", (path / "keep.txt").read_text())
        self.assertTrue(self.manager.target("task-a", "Common").exists())
        self.assertEqual([1, 1], [p["created"] for p in self.manager.list()[0]["repos"]])

    def test_multiple_repo_failure_reports_and_resumes(self):
        self.create(repos={"Common": None, "SushiEricServerMod": None})
        def failing_git(repo, *args):
            if repo.name == "SushiEricServerMod" and "remove" in args:
                raise TaskError("Permission denied: synthetic")
            return git(repo, *args)
        with patch("dev_server.task_worktrees.git", side_effect=failing_git):
            with self.assertRaises(TaskError) as caught:
                self.manager.remove("task-a", "codex-a")
        message = str(caught.exception)
        for text in ("Permission denied: synthetic", "処理済みrepo: Common",
                     "未処理/要確認repo: SushiEricServerMod", "Git登録=True", ".git=True",
                     "原因は未確定", "sandbox", "--recover-remove"):
            self.assertIn(text, message)
        self.assertEqual([0, 1], [p["created"] for p in self.manager.list()[0]["repos"]])
        self.assertEqual("REMOVED", self.manager.recover_remove("task-a", "codex-a")[0]["state"])

    def test_git_partial_failure_reports_residue_and_preserves_it(self):
        self.create()
        path = self.manager.target("task-a", "SushiEricServerMod")
        def partial_git(repo, *args):
            if "remove" in args:
                git(repo, *args)
                path.mkdir()
                (path / "source.txt").write_text("preserve", encoding="utf-8")
                raise TaskError("Filename too long: synthetic")
            return git(repo, *args)
        with patch("dev_server.task_worktrees.git", side_effect=partial_git):
            with self.assertRaisesRegex(TaskError, "Git登録=False, 実体=True, .git=False"):
                self.manager.remove("task-a", "codex-a")
        self.assertEqual(1, self.manager.list()[0]["repos"][0]["created"])
        with self.assertRaisesRegex(TaskError, "非空"):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertEqual("preserve", (path / "source.txt").read_text())

    def test_git_success_before_record_interruption_can_be_recovered(self):
        self.create()
        def interrupted_git(repo, *args):
            result = git(repo, *args)
            if "remove" in args:
                raise KeyboardInterrupt("synthetic interruption")
            return result
        with patch("dev_server.task_worktrees.git", side_effect=interrupted_git):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.manager.remove("task-a", "codex-a")
        self.assertIn("Git登録=False", caught.exception.__notes__[0])
        self.assertEqual(1, self.manager.list()[0]["repos"][0]["created"])
        self.set_state("REMOVING")
        self.assertEqual("REMOVED", self.manager.recover_remove("task-a", "codex-a")[0]["state"])

    def test_git_success_with_residue_is_not_reported_as_removed(self):
        self.create()
        path = self.manager.target("task-a", "SushiEricServerMod")
        def incomplete_git(repo, *args):
            result = git(repo, *args)
            if "remove" in args:
                path.mkdir()
            return result
        with patch("dev_server.task_worktrees.git", side_effect=incomplete_git):
            with self.assertRaisesRegex(TaskError, "Git削除後"):
                self.manager.remove("task-a", "codex-a")
        self.assertEqual("RECOVERY_REQUIRED", self.manager.list()[0]["state"])
        self.assertEqual("REMOVED", self.manager.recover_remove("task-a", "codex-a")[0]["state"])

    def test_recovery_rejects_other_owner_and_unknown_or_creation_states(self):
        self.create()
        self.set_state("RECOVERY_REQUIRED")
        with self.assertRaisesRegex(TaskError, "owner"):
            self.manager.recover_remove("task-a", "other")
        for state in ("ACTIVE", "CREATING", "REMOVED", "UNKNOWN"):
            self.set_state(state)
            with self.assertRaisesRegex(TaskError, "状態"):
                self.manager.recover_remove("task-a", "codex-a")
        self.assertTrue(self.manager.target("task-a", "SushiEricServerMod").exists())

    def test_recovery_rejects_partial_creation(self):
        def failing_git(repo, *args):
            if repo.name == "SushiEricServerMod" and args[:2] == ("worktree", "add"):
                raise TaskError("synthetic creation failure")
            return git(repo, *args)
        with patch("dev_server.task_worktrees.git", side_effect=failing_git):
            with self.assertRaises(TaskError):
                self.create(repos={"Common": None, "SushiEricServerMod": None})
        with self.assertRaisesRegex(TaskError, "削除開始記録"):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertTrue(self.manager.target("task-a", "Common").exists())

    def test_creation_failure_after_branch_creation_is_not_removal_recovery(self):
        repo = self.root / "SushiEricServerMod"
        def failing_git(repo, *args):
            if args[:2] == ("worktree", "add"):
                git(repo, "branch", "feature/issue-100", args[-1])
                raise TaskError("synthetic failure after branch creation")
            return git(repo, *args)
        with patch("dev_server.task_worktrees.git", side_effect=failing_git):
            with self.assertRaises(TaskError):
                self.create()
        with self.assertRaisesRegex(TaskError, "削除開始記録"):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertTrue(git(repo, "for-each-ref", "refs/heads/feature/issue-100"))
        self.assertFalse(self.manager.list()[0]["removal_started"])

    def test_created_repos_without_deletion_intent_are_not_recovered(self):
        self.create()
        self.set_state("RECOVERY_REQUIRED", removal_started=False)
        with self.assertRaisesRegex(TaskError, "削除開始記録"):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertTrue(self.manager.target("task-a", "SushiEricServerMod").exists())
        self.assertEqual("RECOVERY_REQUIRED", self.manager.list()[0]["state"])

    def test_legacy_removing_can_be_recovered_and_old_records_are_preserved(self):
        self.create()
        path = self.manager.target("task-a", "SushiEricServerMod")
        git(self.root / "SushiEricServerMod", "worktree", "remove", str(path))
        with self.manager.database() as db:
            db.execute("UPDATE tasks SET state='REMOVING' WHERE task_id='task-a'")
            db.execute("DROP TABLE removals")
            db.execute("PRAGMA user_version=1")
        before = self.manager.list()[0]
        self.assertEqual("codex-a", before["owner"])
        self.assertEqual(1, before["repos"][0]["created"])
        self.assertFalse(before["removal_started"])
        result = self.manager.recover_remove("task-a", "codex-a")[0]
        self.assertEqual("REMOVED", result["state"])
        self.assertTrue(result["removal_started"])
        with self.manager.database() as db:
            self.assertEqual(2, db.execute("PRAGMA user_version").fetchone()[0])

    def test_recovery_rejects_dirty_ignored_data_and_branch_mismatch(self):
        self.create(repos={"Common": None, "SushiEricServerMod": None})
        self.set_state("RECOVERY_REQUIRED")
        path = self.manager.target("task-a", "SushiEricServerMod")
        new = path / "new.txt"
        new.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(TaskError, "未コミット"):
            self.manager.recover_remove("task-a", "codex-a")
        new.unlink()
        (path / "run").mkdir()
        data = path / "run/data"
        data.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(TaskError, "追跡外データ"):
            self.manager.recover_remove("task-a", "codex-a")
        data.unlink()
        (path / "run").rmdir()
        git(path, "checkout", "-b", "different")
        with self.assertRaisesRegex(TaskError, "branch"):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertTrue(self.manager.target("task-a", "Common").exists())
        self.assertEqual("RECOVERY_REQUIRED", self.manager.list()[0]["state"])

    def test_recovery_rejects_unknown_registry_and_invalid_record(self):
        self.create()
        self.set_state("RECOVERY_REQUIRED")
        with self.manager.database() as db:
            db.execute("UPDATE repos SET branch='main' WHERE task_id='task-a'")
        with self.assertRaisesRegex(TaskError, "管理記録"):
            self.manager.recover_remove("task-a", "codex-a")
        with self.manager.database() as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(TaskError, "未知"):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertTrue(self.manager.target("task-a", "SushiEricServerMod").exists())

    def test_recovery_and_removal_hold_build_lock_during_git_deletion(self):
        self.create()
        self.set_state("RECOVERY_REQUIRED")
        with build_lock(self.root / "worktrees/task-a/.build.lock"):
            with self.assertRaises(TaskError):
                self.manager.recover_remove("task-a", "codex-a")
        def locked_git(repo, *args):
            if "remove" in args:
                with self.assertRaisesRegex(RuntimeError, "操作中"):
                    with build_lock(self.root / "worktrees/task-a/.build.lock"):
                        self.fail("削除処理中のlockが解放されています")
            return git(repo, *args)
        with patch("dev_server.task_worktrees.git", side_effect=locked_git):
            self.manager.recover_remove("task-a", "codex-a")

    def test_windows_removal_uses_command_scoped_longpaths_without_force(self):
        self.create()
        repo = self.root / "SushiEricServerMod"
        git(repo, "config", "core.longpaths", "false")
        calls = []
        def record_git(repo, *args):
            calls.append(args)
            return git(repo, *args)
        with patch("dev_server.task_worktrees.os", wraps=os) as mock_os:
            mock_os.name = "nt"
            with patch("dev_server.task_worktrees.git", side_effect=record_git):
                self.manager.remove("task-a", "codex-a")
        removal = next(args for args in calls if "remove" in args)
        self.assertEqual(("-c", "core.longpaths=true", "worktree", "remove"), removal[:4])
        self.assertNotIn("--force", removal)
        for args in calls:
            if "status" in args or "ls-files" in args:
                self.assertEqual(("-c", "core.longpaths=true"), args[:2])
        self.assertEqual("false", git(repo, "config", "--local", "core.longpaths"))

    @unittest.skipUnless(os.name == "nt", "Windows実Gitの長いパス削除試験")
    def test_windows_real_git_removes_long_ignored_build_and_gradle_paths(self):
        self.create()
        repo = self.root / "SushiEricServerMod"
        git(repo, "config", "core.longpaths", "false")
        path = self.manager.target("task-a", "SushiEricServerMod")
        for directory in ("build", ".gradle"):
            output = path / directory / ("long-path-" * 8) / ("output-" * 10) / "result.txt"
            self.assertGreater(len(str(output)), 300)
            output.parent.mkdir(parents=True)
            output.write_text("disposable", encoding="utf-8")
        self.manager.remove("task-a", "codex-a")
        self.assertFalse(path.exists())
        self.assertNotIn(path.resolve(), worktrees(repo))
        self.assertEqual("false", git(repo, "config", "--local", "core.longpaths"))

    @unittest.skipUnless(os.name == "nt", "Windows実Gitの長い保存パス保全試験")
    def test_windows_long_ignored_runtime_data_is_preserved(self):
        self.create()
        repo = self.root / "SushiEricServerMod"
        git(repo, "config", "core.longpaths", "false")
        path = self.manager.target("task-a", "SushiEricServerMod")
        data = path / "run" / ("persistent-" * 8) / ("world-" * 12) / "keep.txt"
        self.assertGreater(len(str(data)), 300)
        data.parent.mkdir(parents=True)
        data.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(TaskError, "追跡外データ"):
            self.manager.remove("task-a", "codex-a")
        self.assertEqual("preserve", data.read_text())
        self.assertIn(path.resolve(), worktrees(repo))
        self.assertEqual("ACTIVE", self.manager.list()[0]["state"])

    def test_recovery_rejects_descendant_ref_after_branch_disappears(self):
        self.create()
        self.removed_without_record()
        repo = self.root / "SushiEricServerMod"
        git(repo, "branch", "-d", "feature/issue-100")
        git(repo, "branch", "feature/issue-100/other")
        with self.assertRaisesRegex(TaskError, "branch消失"):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertEqual("RECOVERY_REQUIRED", self.manager.list()[0]["state"])

    def test_recovery_rejects_symlink_and_special_residual_file(self):
        self.create()
        path = self.removed_without_record(empty=True)
        outside = self.root / "Common/source.txt"
        link = path / "external"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"このホストにはsymlink作成権限がありません: {error}")
        with self.assertRaises(TaskError):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertTrue(link.is_symlink())
        self.assertEqual("initial\n", outside.read_text())
        link.unlink()
        path.rmdir()
        path.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(TaskError, "特殊ファイル"):
            self.manager.recover_remove("task-a", "codex-a")
        self.assertEqual("preserve", path.read_text())

    def test_recovery_rechecks_empty_directory_before_removing(self):
        self.create()
        path = self.removed_without_record(empty=True)
        original = self.manager._removal_target
        calls = 0
        def changed_target(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                (path / "new.txt").write_text("preserve", encoding="utf-8")
            return original(*args, **kwargs)
        with patch.object(self.manager, "_removal_target", side_effect=changed_target):
            with self.assertRaisesRegex(TaskError, "非空"):
                self.manager.recover_remove("task-a", "codex-a")
        self.assertEqual("preserve", (path / "new.txt").read_text())

    def test_cli_recovery_requires_task_owner_and_excludes_other_modes(self):
        script = Path(__file__).resolve().parents[1] / "setup-worktrees.py"
        for args in (("--recover-remove",), ("--recover-remove", "--task-id", "task-a"),
                     ("--recover-remove", "--remove")):
            result = subprocess.run([sys.executable, "-B", str(script), "--workspace-root",
                                     str(self.root), *args], capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
        self.create()
        self.removed_without_record(empty=True)
        result = subprocess.run([sys.executable, "-B", str(script), "--workspace-root", str(self.root),
                                 "--recover-remove", "--task-id", "task-a", "--owner", "codex-a"],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("REMOVED", result.stdout)


if __name__ == "__main__":
    unittest.main()
