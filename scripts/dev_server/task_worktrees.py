"""タスクのownerとGit worktreeを管理する。サーバー実行領域には触れない。"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

from .build_lock import BuildLockError, build_lock

REPOSITORIES = ("Common", "SushiEricServerManager", "SushiEricServerMod",
                "SushiEricCombatCore", ".github")
DEVELOPMENT_BRANCHES = {"Common": "develop", "SushiEricServerManager": "develop",
                        "SushiEricCombatCore": "develop", "SushiEricServerMod": "main",
                        ".github": "main"}


class TaskError(RuntimeError):
    """安全条件を満たさない操作。既存のworktreeは自動修復しない。"""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TaskError(message)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8")
    require(result.returncode == 0, f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def plain_path(path: Path) -> None:
    """既存の祖先を含め、symlinkとWindowsのreparse pointを拒否する。"""
    for candidate in (path, *path.parents):
        if not candidate.exists() and not candidate.is_symlink():
            continue
        info = candidate.lstat()
        require(not stat.S_ISLNK(info.st_mode)
                and not (getattr(info, "st_file_attributes", 0) & 0x400),
                f"リンク/reparse pointは操作できません: {candidate}")


def worktrees(repo: Path) -> dict[Path, str | None]:
    result = {}
    current = None
    for line in git(repo, "worktree", "list", "--porcelain", "-z").split("\0"):
        if line.startswith("worktree "):
            current = Path(line[9:]).resolve()
            result[current] = None
        elif line.startswith("branch ") and current is not None:
            result[current] = line[7:]
    return result


def workspace_for(repo: Path) -> Path:
    """スクリプト自身がタスクworktreeにある場合もprimaryの親をworkspaceにする。"""
    common = Path(git(repo, "rev-parse", "--git-common-dir"))
    return (repo / common).resolve().parent.parent


def clean(target: Path, *, deleting: bool = False) -> None:
    require(not git(target, "status", "--porcelain", "--untracked-files=all"),
            f"未コミット変更があります: {target}")
    if not deleting:
        return
    ignored = git(target, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    for name in filter(None, ignored.split("\0")):
        require(name.startswith(("build/", ".gradle/")),
                f"削除できない追跡外データがあります: {target / name}")
    # Gitに削除を委ねる前に、リンクや特殊ファイルをたどらないことを確認する。
    for directory, dirs, files in os.walk(target, followlinks=False):
        for name in (*dirs, *files):
            path = Path(directory) / name
            plain_path(path)
            require(stat.S_ISREG(path.lstat().st_mode) or stat.S_ISDIR(path.lstat().st_mode),
                    f"特殊ファイルは削除できません: {path}")


class TaskWorktrees:
    """同一taskの作成・引継ぎ・削除をSQLiteのowner契約で排他する。

    Gitの共有ref操作はGit自身のlockに委ねる。途中失敗は記録と実体を残して
    RECOVERY_REQUIREDにし、他のworktreeやブランチを推測して削除しない。
    ownerは同一ユーザーの協調契約であり、OSのアクセス制御ではない。
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace.absolute()
        self.root = self.workspace / "worktrees"
        plain_path(self.root)

    @contextmanager
    def database(self):
        plain_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("registry.sqlite", "registry.sqlite-journal", "registry.sqlite-wal", "registry.sqlite-shm"):
            plain_path(self.root / name)
        db = sqlite3.connect(self.root / "registry.sqlite", timeout=15)
        db.row_factory = sqlite3.Row
        try:
            require(db.execute("PRAGMA user_version").fetchone()[0] in (0, 1),
                    "未知のworktree registry形式です")
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, issue INTEGER NOT NULL,
                    owner TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS repos (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id), name TEXT NOT NULL,
                    branch TEXT NOT NULL, base_commit TEXT NOT NULL, created INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (task_id, name));
                PRAGMA user_version = 1;
            """)
            with db:
                yield db
        finally:
            db.close()

    def target(self, task_id: str, name: str) -> Path:
        require(bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", task_id)),
                "task-idは小文字英数字と区切りのハイフンで指定してください")
        require(name in REPOSITORIES, f"対象外のrepoです: {name}")
        target = self.root / task_id / name
        plain_path(target)
        require(target.resolve().parents[1] == self.root.resolve(), "管理範囲外のパスです")
        return target

    def primary(self, name: str) -> Path:
        require(name in REPOSITORIES, f"対象外のrepoです: {name}")
        repo = self.workspace / name
        plain_path(repo)
        plain_path(repo / ".git")
        require((repo / ".git").is_dir(), f"primary repositoryがありません: {repo}")
        common = Path(git(repo, "rev-parse", "--git-common-dir"))
        require((repo / common).resolve() == (repo / ".git").resolve(),
                f"primary worktreeではありません: {repo}")
        return repo

    @staticmethod
    def owned(db, task_id: str, owner: str, states: tuple[str, ...]):
        task = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        require(task is not None, f"未登録taskです: {task_id}")
        require(task["owner"] == owner, f"ownerが異なります: {task_id}")
        require(task["state"] in states, f"操作できない状態です: {task['state']}")
        return db.execute("SELECT * FROM repos WHERE task_id = ? ORDER BY name", (task_id,)).fetchall()

    def verify(self, task_id: str, plan, *, deleting: bool = False) -> tuple[Path, Path]:
        repo = self.primary(plan["name"])
        target = self.target(task_id, plan["name"])
        require(worktrees(repo).get(target.resolve()) == f"refs/heads/{plan['branch']}",
                f"登録とGitのworktree/branchが一致しません: {target}")
        require(git(target, "rev-parse", "--show-toplevel") == target.as_posix(),
                f"worktreeの実体が一致しません: {target}")
        clean(target, deleting=deleting)
        return repo, target

    def create(self, task_id: str, issue: int, owner: str, bases: dict[str, str | None],
               *, fetch: bool = True) -> list[dict]:
        require(issue > 0 and bool(owner.strip()) and bool(bases), "issue、owner、repoが必要です")
        branch = f"feature/issue-{issue}"
        plans = []
        for name, ref in bases.items():
            target = self.target(task_id, name)
            repo = self.primary(name)
            if fetch:
                git(repo, "fetch", "origin")
            if ref is None:
                # origin/HEADがリリースbranchでも、AGENTS.mdの開発基準を使う。
                ref = "origin/" + DEVELOPMENT_BRANCHES[name]
            require(not ref.startswith("-"), "不正な基準refです")
            commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
            plans.append((name, repo, target, commit))
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if existing is not None:
                saved = self.owned(db, task_id, owner, ("ACTIVE",))
                require(existing["issue"] == issue, "同一taskのissueは変更できません")
                require({(p["name"], p["base_commit"]) for p in saved}
                        == {(name, commit) for name, _, _, commit in plans},
                        "同一taskのrepo/基準commitは変更できません")
                for plan in saved:
                    self.verify(task_id, plan)
            else:
                for _, repo, target, _ in plans:
                    require(not target.exists() and target.resolve() not in worktrees(repo),
                            f"worktreeのパスは既に存在します: {target}")
                    refs = git(repo, "for-each-ref", "--format=%(refname)",
                               f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")
                    require(not refs, f"既存branchは上書きできません: {branch}")
                db.execute("INSERT INTO tasks VALUES (?, ?, ?, 'CREATING')", (task_id, issue, owner))
                db.executemany("INSERT INTO repos (task_id,name,branch,base_commit) VALUES (?,?,?,?)",
                               [(task_id, name, branch, commit) for name, _, _, commit in plans])
        if existing is not None:
            return self.list(task_id)
        try:
            for name, repo, target, commit in plans:
                plain_path(target)
                git(repo, "worktree", "add", "-b", branch, str(target), commit)
                with self.database() as db:
                    db.execute("UPDATE repos SET created=1 WHERE task_id=? AND name=?", (task_id, name))
            with self.database() as db:
                db.execute("UPDATE tasks SET state='ACTIVE' WHERE task_id=?", (task_id,))
        except BaseException:
            with self.database() as db:
                db.execute("UPDATE tasks SET state='RECOVERY_REQUIRED' WHERE task_id=?", (task_id,))
            raise
        return self.list(task_id)

    @contextmanager
    def build_guard(self, task_id: str):
        """ビルド中の担当引継ぎ・削除を拒否する。未作成taskの領域は生成しない。"""
        root = self.target(task_id, "SushiEricServerMod").parent
        if not root.exists():
            yield
            return
        try:
            with build_lock(root / ".build.lock"):
                yield
        except BuildLockError as error:
            raise TaskError(str(error)) from error

    def handoff(self, task_id: str, old_owner: str, owner: str) -> list[dict]:
        require(bool(owner.strip()) and owner != old_owner, "新しいownerを指定してください")
        with self.build_guard(task_id), self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            for plan in self.owned(db, task_id, old_owner, ("ACTIVE",)):
                self.verify(task_id, plan)
            db.execute("UPDATE tasks SET owner=? WHERE task_id=?", (owner, task_id))
        return self.list(task_id)

    def remove(self, task_id: str, owner: str) -> list[dict]:
        with self.build_guard(task_id), self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            plans = self.owned(db, task_id, owner, ("ACTIVE", "RECOVERY_REQUIRED"))
            targets = []
            for plan in plans:
                repo = self.primary(plan["name"])
                target = self.target(task_id, plan["name"])
                if plan["created"]:
                    self.verify(task_id, plan, deleting=True)
                    targets.append((plan["name"], repo, target))
                else:
                    require(not target.exists() and target.resolve() not in worktrees(repo),
                            f"未確定のworktreeが残っています。手動確認が必要です: {target}")
            db.execute("UPDATE tasks SET state='REMOVING' WHERE task_id=?", (task_id,))
        try:
            for name, repo, target in targets:
                # 削除直前にも再検査し、Gitのdirty検査を無効化するforceは使わない。
                plan = next(p for p in plans if p["name"] == name)
                self.verify(task_id, plan, deleting=True)
                git(repo, "worktree", "remove", str(target))
                with self.database() as db:
                    db.execute("UPDATE repos SET created=0 WHERE task_id=? AND name=?", (task_id, name))
            with self.database() as db:
                db.execute("UPDATE tasks SET state='REMOVED' WHERE task_id=?", (task_id,))
        except BaseException:
            with self.database() as db:
                db.execute("UPDATE tasks SET state='RECOVERY_REQUIRED' WHERE task_id=?", (task_id,))
            raise
        return self.list(task_id)

    def list(self, task_id: str | None = None) -> list[dict]:
        if not (self.root / "registry.sqlite").exists():
            return []
        with self.database() as db:
            rows = db.execute("SELECT * FROM tasks WHERE (? IS NULL OR task_id=?) ORDER BY task_id",
                              (task_id, task_id)).fetchall()
            result = []
            for row in rows:
                record = dict(row)
                record["repos"] = [dict(p, path=str(self.target(row["task_id"], p["name"])))
                                   for p in db.execute("SELECT * FROM repos WHERE task_id=? ORDER BY name",
                                                       (row["task_id"],))]
                result.append(record)
            return result
