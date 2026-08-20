#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

START = "<!-- COMMON-RULES:START -->"
END = "<!-- COMMON-RULES:END -->"
TARGETS = (
    "Common/AGENTS.md",
    "SushiEricDataEditor/AGENTS.md",
    "SushiEricServerMod/AGENTS.md",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SushiEricWorkspace共通AIルールを各AGENTS.mdへ同期する。"
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="Common等のリポジトリが配置された親ディレクトリ。省略時は.githubの親を使用する。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="ファイルを書き換えず、同期ずれがあれば終了コード1を返す。",
    )
    return parser.parse_args()


def replace_common_block(text: str, common_rules: str) -> str:
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("COMMON-RULESマーカーが一組だけ存在する必要があります。")

    start_index = text.index(START)
    end_index = text.index(END, start_index) + len(END)
    replacement = f"{START}\n{common_rules.rstrip()}\n{END}"
    return text[:start_index] + replacement + text[end_index:]


def main() -> int:
    args = parse_args()
    github_repo_root = Path(__file__).resolve().parents[1]
    workspace_root = (
        args.workspace_root.resolve()
        if args.workspace_root
        else github_repo_root.parent
    )
    common_rules = (github_repo_root / "AI_GUIDELINES.md").read_text(encoding="utf-8")

    drifted: list[Path] = []
    for relative_target in TARGETS:
        target = workspace_root / relative_target
        if not target.is_file():
            raise FileNotFoundError(f"対象AGENTS.mdが見つかりません: {target}")

        current = target.read_text(encoding="utf-8")
        updated = replace_common_block(current, common_rules)
        if updated == current:
            continue

        drifted.append(target)
        if not args.check:
            target.write_text(updated, encoding="utf-8")
            print(f"updated: {target}")

    if args.check and drifted:
        for target in drifted:
            print(f"out-of-sync: {target}", file=sys.stderr)
        return 1

    if not drifted:
        print("all AGENTS.md files are in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
