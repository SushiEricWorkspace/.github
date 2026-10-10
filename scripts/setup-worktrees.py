#!/usr/bin/env python3
"""Issueとタスク単位のworktreeを管理する。使い方はdev_server/worktrees-guide.mdを参照。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from dev_server.task_worktrees import REPOSITORIES, TaskError, TaskWorktrees, git, require, workspace_for


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, help="primary repositoryが並ぶworkspace")
    parser.add_argument("--task-id", help="小文字英数字とハイフンのタスク識別子")
    parser.add_argument("--issue", type=int, help="feature/issue-<番号>の番号")
    parser.add_argument("--owner", help="同時書込担当。例: codex-session-a")
    parser.add_argument("--repos", help="対象repoのカンマ区切り。作成時は必須")
    parser.add_argument("--base", action="append", default=[], metavar="REPO=REF",
                        help="repoごとの起点。省略repoはoriginの開発基準。複数指定可")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="タスク登録一覧（task-idで限定可）")
    mode.add_argument("--list-legacy", action="store_true", help="既存worktreeを読み取り専用で一覧")
    mode.add_argument("--handoff-from", metavar="OWNER", help="現在のownerから担当を引き継ぐ")
    mode.add_argument("--remove", action="store_true", help="登録taskのworktreeだけを削除")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manager = TaskWorktrees(args.workspace_root or workspace_for(Path(__file__).resolve().parents[1]))
        if args.list_legacy:
            result = {name: git(manager.primary(name), "worktree", "list")
                      for name in REPOSITORIES if (manager.workspace / name / ".git").is_dir()}
        elif args.list:
            result = manager.list(args.task_id)
        else:
            require(bool(args.task_id) and bool(args.owner), "--task-idと--ownerが必要です")
            if args.remove:
                result = manager.remove(args.task_id, args.owner)
            elif args.handoff_from:
                result = manager.handoff(args.task_id, args.handoff_from, args.owner)
            else:
                require(args.issue is not None and args.issue > 0 and bool(args.repos),
                        "作成には--issueと--reposが必要です")
                names = args.repos.split(",")
                require(len(names) == len(set(names)), "repoを重複指定できません")
                bases = dict.fromkeys(names)
                specified = set()
                for pair in args.base:
                    name, separator, ref = pair.partition("=")
                    require(bool(separator) and bool(ref) and name in bases and name not in specified,
                            "--baseは対象REPO=REFを一度ずつ指定してください")
                    bases[name] = ref
                    specified.add(name)
                result = manager.create(args.task_id, args.issue, args.owner, bases)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TaskError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
