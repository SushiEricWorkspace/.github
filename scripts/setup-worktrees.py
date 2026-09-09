#!/usr/bin/env python3
"""AIエージェントごとのgit worktreeを作成・削除する。

複数のAIを同時に運用するとき、同じ作業ディレクトリを共有するとGitの状態が
衝突する。エージェントごとにworktreeを分けることで、ブランチの切り替えと
作業ツリーを独立させる。

想定する配置は次のとおり。

    <workspace>/
    ├ .github/                       このスクリプトの置き場所
    ├ SushiEricServerMod/            人間・IDE用（primary worktree）
    ├ SushiEricServerMod-claude/     Claude Code用
    └ SushiEricServerMod-codex/      Codex用
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORIES = (
    "Common",
    "SushiEricServerManager",
    "SushiEricServerMod",
)

DEFAULT_AGENTS = ("claude", "codex")

# gitの追跡外だがworktreeでも必要になるディレクトリ。存在する場合だけ複製する。
COPY_ON_CREATE = ("run",)

SKILL_RELATIVE = Path("skills/worktree/SKILL.md")
SKILL_INSTALL_RELATIVE = Path(".claude/skills/worktree/SKILL.md")


def run_git(args: list[str], cwd: Path, capture: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
    )
    return (result.stdout or "").strip()


def ensure_primary(repo: Path) -> None:
    """primary worktree以外を対象にした場合は中断する。"""
    # gitはリポジトリ直下で実行すると相対パス".git"を返すため、repo基準で解決する。
    raw = Path(run_git(["rev-parse", "--git-common-dir"], repo))
    common = raw if raw.is_absolute() else (repo / raw)
    common = common.resolve()
    if common.name != ".git" or common.parent != repo:
        sys.exit(f"primary worktreeではない: {repo}")


def default_branch(repo: Path) -> str:
    """originの既定ブランチ名を返す。

    ローカルの`refs/remotes/origin/HEAD`はclone時点の値のまま古くなることがあるため、
    リモートへ問い合わせた結果を優先する。取得できない場合だけローカルの値へ退避する。
    """
    try:
        for line in run_git(["ls-remote", "--symref", "origin", "HEAD"], repo).splitlines():
            if line.startswith("ref:"):
                # 例: "ref: refs/heads/develop\tHEAD"
                return line.split()[1].removeprefix("refs/heads/")
    except subprocess.CalledProcessError:
        pass

    try:
        ref = run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo)
        return ref.split("/", 1)[1]
    except subprocess.CalledProcessError:
        return run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo)


def existing_worktrees(repo: Path) -> set[Path]:
    paths: set[Path] = set()
    for line in run_git(["worktree", "list", "--porcelain"], repo).splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line[len("worktree ") :]).resolve())
    return paths


def copy_untracked(repo: Path, target: Path) -> None:
    for name in COPY_ON_CREATE:
        source = repo / name
        if not source.is_dir() or (target / name).exists():
            continue
        print(f"    {name}/ を複製中...")
        shutil.copytree(source, target / name)


def install_skill(github_root: Path, repo: Path) -> None:
    source = github_root / SKILL_RELATIVE
    if not source.is_file():
        print(f"    スキル原本が見つからない: {source}")
        return
    destination = repo / SKILL_INSTALL_RELATIVE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    print(f"    スキルを配置: {destination.relative_to(repo)}")


def create(repo: Path, agents: tuple[str, ...], base: str, copy: bool) -> None:
    existing = existing_worktrees(repo)
    run_git(["fetch", "origin", "--prune"], repo)

    for agent in agents:
        target = (repo.parent / f"{repo.name}-{agent}").resolve()
        print(f"  [{agent}] {target}")

        if target in existing:
            print("    既にworktreeとして登録済み。スキップする")
            continue
        if target.exists():
            print(f"    パスが既に存在する。手動で確認する")
            continue

        # ブランチは1つのworktreeでしかチェックアウトできないため、
        # 作成時点ではdetached HEADにする。作業時にfeature/issue-<番号>を作る。
        run_git(
            ["worktree", "add", "--detach", str(target), f"origin/{base}"],
            repo,
            capture=False,
        )
        if copy:
            copy_untracked(repo, target)


def remove(repo: Path, agents: tuple[str, ...]) -> None:
    existing = existing_worktrees(repo)
    for agent in agents:
        target = (repo.parent / f"{repo.name}-{agent}").resolve()
        print(f"  [{agent}] {target}")
        if target not in existing:
            print("    worktreeとして登録されていない。スキップする")
            continue
        run_git(["worktree", "remove", str(target)], repo, capture=False)
    run_git(["worktree", "prune"], repo)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AIエージェントごとのgit worktreeを作成・削除する。"
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="各リポジトリが配置された親ディレクトリ。省略時は.githubの親を使用する。",
    )
    parser.add_argument(
        "--repos",
        default=",".join(REPOSITORIES),
        help=f"対象リポジトリをカンマ区切りで指定する。既定は{','.join(REPOSITORIES)}。",
    )
    parser.add_argument(
        "--agents",
        default=",".join(DEFAULT_AGENTS),
        help=f"対象エージェントをカンマ区切りで指定する。既定は{','.join(DEFAULT_AGENTS)}。",
    )
    parser.add_argument("--base", help="worktreeの起点ブランチ。省略時はoriginの既定ブランチ。")
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="git追跡外ディレクトリ（run/など）を複製しない。",
    )
    parser.add_argument(
        "--install-skill",
        action="store_true",
        help="worktreeスキルを各リポジトリの.claude/skills/へ配置する。",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="作成済みworktreeを削除する。未コミットの変更があると失敗する。",
    )
    parser.add_argument("--list", action="store_true", help="worktree一覧を表示して終了する。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    github_root = Path(__file__).resolve().parents[1]
    workspace_root = (
        args.workspace_root.resolve() if args.workspace_root else github_root.parent
    )

    repositories = tuple(r.strip() for r in args.repos.split(",") if r.strip())
    agents = tuple(a.strip() for a in args.agents.split(",") if a.strip())
    if not repositories or not agents:
        sys.exit("--repos と --agents は空にできない。")

    for name in repositories:
        repo = (workspace_root / name).resolve()
        if not (repo / ".git").exists():
            print(f"{name}: gitリポジトリが見つからないためスキップする")
            continue

        print(f"=== {name} ===")
        ensure_primary(repo)

        if args.list:
            print(run_git(["worktree", "list"], repo))
            continue

        if args.remove:
            remove(repo, agents)
        else:
            base = args.base or default_branch(repo)
            print(f"  起点ブランチ: origin/{base}")
            create(repo, agents, base, copy=not args.no_copy)
            if args.install_skill:
                install_skill(github_root, repo)

        print(run_git(["worktree", "list"], repo))
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
