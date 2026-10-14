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
    options = ("-c", "core.longpaths=true") if os.name == "nt" else ()
    require(not git(target, *options, "status", "--porcelain", "--untracked-files=all"),
            f"未コミット変更があります: {target}")
    if not deleting:
        return
    ignored = git(target, *options, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    for name in filter(None, ignored.split("\0")):
        require(name.startswith(("build/", ".gradle/")),
                f"削除できない追跡外データがあります: {target / name}")
    # Gitに削除を委ねる前に、リンクや特殊ファイルをたどらないことを確認する。
    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(target, followlinks=False, onerror=walk_error):
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
            version = db.execute("PRAGMA user_version").fetchone()[0]
            require(version in (0, 1, 2), "未知のworktree registry形式です")
            db.execute("PRAGMA foreign_keys = ON")
            if version != 2:
                self._initialize_database(db)
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _initialize_database(db):
        """初回作成と既存形式の移行だけを書込みトランザクションで行う。"""
        with db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            require(version in (0, 1, 2), "未知のworktree registry形式です")
            if version == 2:
                return
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, issue INTEGER NOT NULL,
                owner TEXT NOT NULL, state TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS repos (
                task_id TEXT NOT NULL REFERENCES tasks(task_id), name TEXT NOT NULL,
                branch TEXT NOT NULL, base_commit TEXT NOT NULL, created INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (task_id, name))""")
            db.execute("""CREATE TABLE IF NOT EXISTS removals (
                task_id TEXT PRIMARY KEY REFERENCES tasks(task_id))""")
            db.execute("PRAGMA user_version = 2")

    @staticmethod
    def _snapshot(db, task_id: str):
        task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        plans = db.execute("SELECT * FROM repos WHERE task_id=? ORDER BY name", (task_id,)).fetchall()
        removal = db.execute("SELECT 1 FROM removals WHERE task_id=?", (task_id,)).fetchone()
        return (dict(task) if task is not None else None, [dict(p) for p in plans], removal is not None)

    def _read_snapshot(self, task_id: str, owner: str, states: tuple[str, ...]):
        with self.database() as db:
            db.execute("BEGIN")
            self.owned(db, task_id, owner, states)
            return self._snapshot(db, task_id)

    def _recheck(self, db, task_id: str, snapshot):
        require(self._snapshot(db, task_id) == snapshot,
                f"検査中にtaskの管理記録が変わりました。実体を保全してください: {task_id}")

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

    def verify(self, task_id: str, plan) -> tuple[Path, Path]:
        repo = self.primary(plan["name"])
        target = self.target(task_id, plan["name"])
        require(worktrees(repo).get(target.resolve()) == f"refs/heads/{plan['branch']}",
                f"登録とGitのworktree/branchが一致しません: {target}")
        require(git(target, "rev-parse", "--show-toplevel") == target.as_posix(),
                f"worktreeの実体が一致しません: {target}")
        return repo, target

    def create(self, task_id: str, issue: int, owner: str, bases: dict[str, str | None],
               *, fetch: bool = True) -> list[dict]:
        with self.build_guard(task_id):
            return self._create(task_id, issue, owner, bases, fetch=fetch)

    def _create(self, task_id: str, issue: int, owner: str, bases: dict[str, str | None],
                *, fetch: bool) -> list[dict]:
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
            db.execute("BEGIN")
            snapshot = self._snapshot(db, task_id)
            existing, saved, _ = snapshot
            if existing is not None:
                self.owned(db, task_id, owner, ("ACTIVE",))
        if existing is not None:
            require(existing["issue"] == issue, "同一taskのissueは変更できません")
            require({(p["name"], p["base_commit"]) for p in saved}
                    == {(name, commit) for name, _, _, commit in plans},
                    "同一taskのrepo/基準commitは変更できません")
            for plan in saved:
                _, target = self.verify(task_id, plan)
                clean(target)
        else:
            for _, repo, target, _ in plans:
                require(not target.exists() and target.resolve() not in worktrees(repo),
                        f"worktreeのパスは既に存在します: {target}")
                refs = git(repo, "for-each-ref", "--format=%(refname)",
                           f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")
                require(not refs, f"既存branchは上書きできません: {branch}")
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recheck(db, task_id, snapshot)
            if existing is None:
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
        with self.build_guard(task_id):
            snapshot = self._read_snapshot(task_id, old_owner, ("ACTIVE",))
            for plan in snapshot[1]:
                self.verify(task_id, plan)
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                self._recheck(db, task_id, snapshot)
                db.execute("UPDATE tasks SET owner=? WHERE task_id=?", (owner, task_id))
        return self.list(task_id)

    def _removal_target(self, task_id: str, plan, *, recovering: bool) -> tuple[Path, Path, str]:
        repo = self.primary(plan["name"])
        target = self.target(task_id, plan["name"])
        registered = target.resolve() in worktrees(repo)
        plain_path(target / ".git")
        if not plan["created"]:
            require(not target.exists() and not registered,
                    f"未確定のworktreeが残っています。手動確認が必要です: {target}")
            return repo, target, "absent"
        if registered:
            require((target / ".git").is_file(), f"worktreeの.gitがありません: {target}")
            self.verify(task_id, plan)
            clean(target, deleting=True)
            common = Path(git(target, "rev-parse", "--git-common-dir"))
            require((target / common).resolve() == (repo / ".git").resolve(),
                    f"worktreeのGit管理先が一致しません: {target}")
            return repo, target, "registered"
        require(recovering, f"登録とGitのworktree/branchが一致しません: {target}")
        require(not (target / ".git").exists(), f"Git登録のない.gitが残っています: {target}")
        if not target.exists():
            return repo, target, "absent"
        require(target.is_dir() and not any(target.iterdir()),
                f"Git登録のない非空ディレクトリまたは特殊ファイルは保全してください: {target}")
        return repo, target, "empty"

    def remove(self, task_id: str, owner: str) -> list[dict]:
        """全repoの削除前検査を通して削除する。登録不一致は自動修復しない。"""
        return self._remove(task_id, owner, recovering=False)

    def recover_remove(self, task_id: str, owner: str) -> list[dict]:
        """同一task・現在ownerの中断した削除を照合し、消失・空の実体だけ整合させる。

        非空の未登録ディレクトリや作成未完了のrepoは保全して拒否する。
        正常な未処理repoは通常の削除前検査を通して削除する。branchと履歴は残す。
        """
        return self._remove(task_id, owner, recovering=True)

    def _remove(self, task_id: str, owner: str, *, recovering: bool) -> list[dict]:
        with self.build_guard(task_id):
            states = ("RECOVERY_REQUIRED", "REMOVING") if recovering else ("ACTIVE", "RECOVERY_REQUIRED")
            snapshot = self._read_snapshot(task_id, owner, states)
            task, plans, removal_started = snapshot
            if recovering:
                require(task["state"] == "REMOVING" or removal_started,
                        "削除開始記録がなく、作成失敗と区別できません。実体を保全して個別確認してください")
            require(bool(plans), "repoの管理記録がありません")
            targets = []
            for plan in plans:
                require(plan["created"] in (0, 1)
                        and plan["branch"] == f"feature/issue-{task['issue']}",
                        f"repoの管理記録が不正です: {plan['name']}")
                repo, target, kind = self._removal_target(task_id, plan, recovering=recovering)
                if recovering:
                    require(git(repo, "for-each-ref", "--format=%(refname)",
                                f"refs/heads/{plan['branch']}").splitlines()
                            == [f"refs/heads/{plan['branch']}"],
                            f"作成未完了またはbranch消失のため復旧できません: {target}")
                    require(git(repo, "rev-parse", "--verify", f"{plan['base_commit']}^{{commit}}")
                            == plan["base_commit"], f"基準commitが不正です: {target}")
                targets.append((plan, repo, target, kind))
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                self._recheck(db, task_id, snapshot)
                db.execute("INSERT OR IGNORE INTO removals VALUES (?)", (task_id,))
                db.execute("UPDATE tasks SET state='REMOVING' WHERE task_id=?", (task_id,))
            completed = []
            try:
                for plan, repo, target, kind in targets:
                    # 削除直前にも再検査し、Gitのdirty検査を無効化するforceは使わない。
                    require(self._removal_target(task_id, plan, recovering=recovering)[2] == kind,
                            f"削除前検査後にworktreeの状態が変わりました: {target}")
                    if kind == "registered":
                        options = ("-c", "core.longpaths=true") if os.name == "nt" else ()
                        git(repo, *options, "worktree", "remove", str(target))
                    elif kind == "empty":
                        # rmdirは再検査後にファイルが増えた場合も非空の実体を削除しない。
                        target.rmdir()
                    require(not target.exists() and target.resolve() not in worktrees(repo),
                            f"Git削除後もworktreeの実体または登録が残っています: {target}")
                    with self.database() as db:
                        db.execute("UPDATE repos SET created=0 WHERE task_id=? AND name=?",
                                   (task_id, plan["name"]))
                    completed.append(plan["name"])
                with self.database() as db:
                    db.execute("UPDATE tasks SET state='REMOVED' WHERE task_id=?", (task_id,))
            except BaseException as error:
                with self.database() as db:
                    db.execute("UPDATE tasks SET state='RECOVERY_REQUIRED' WHERE task_id=?", (task_id,))
                details = [f"削除中断: task={task_id}, owner={owner}, state=RECOVERY_REQUIRED",
                           f"原文: {error}", f"処理済みrepo: {', '.join(completed) or 'なし'}",
                           "未処理/要確認repo: " + ", ".join(p["name"] for p in plans
                                                            if p["name"] not in completed)]
                for plan, repo, target, _ in targets:
                    try:
                        registered = target.resolve() in worktrees(repo)
                        details.append(f"{plan['name']}: path={target}, Git登録={registered}, "
                                       f"実体={target.exists()}, .git={(target / '.git').exists()}")
                    except (TaskError, OSError) as diagnostic_error:
                        details.append(f"{plan['name']}: path={target}, 照合不能: {diagnostic_error}")
                if "permission denied" in str(error).lower() or "access is denied" in str(error).lower():
                    details.append("アクセス拒否の原因は未確定です。対象をcwdにする端末・IDE、利用中プロセス、"
                                   "ファイル/親のアクセス権、sandboxの実行権限を確認してください。自動killは行いません。")
                details.append("実体を保全して確認し、削除復旧の許可範囲内で--recover-removeを実行してください。")
                message = "\n".join(details)
                if isinstance(error, Exception):
                    raise TaskError(message) from error
                error.add_note(message)
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
                record["removal_started"] = db.execute(
                    "SELECT 1 FROM removals WHERE task_id=?", (row["task_id"],)).fetchone() is not None
                record["repos"] = [dict(p, path=str(self.target(row["task_id"], p["name"])))
                                   for p in db.execute("SELECT * FROM repos WHERE task_id=? ORDER BY name",
                                                       (row["task_id"],))]
                result.append(record)
            return result
