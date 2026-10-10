"""登録済みtaskでCommonを発行し、固定座標でconsumerをビルドしてbundleを出力する。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from dev_server.build_inputs import canonical, capture_sources, digest, regular_bytes, verify_sources
from dev_server.contracts import ContractError
from dev_server.runtime_bundle import _directory
from dev_server.task_worktrees import TaskWorktrees, worktrees
from dev_server.build_lock import build_lock


def gradle(root: Path, *args: str):
    wrapper = root / ("gradlew.bat" if os.name == "nt" else "gradlew")
    subprocess.run([str(wrapper), *args, "--console=plain"], cwd=root, check=True)


def publication(root: Path, key: str, module: str) -> str:
    values = dict(line.split("=", 1) for line in (root / "build/development-publication.properties")
                  .read_text(encoding="utf-8").splitlines())
    coordinate = values[key]
    if not re.fullmatch(r"io\.github\.sushiericworkspace:" + module +
                        r":0\.1\.0-dev\.[0-9]{17}\.[0-9a-f-]{36}", coordinate):
        raise ContractError("publication: 完全な一意座標が必要です")
    return coordinate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--manager", action="store_true")
    parser.add_argument("--with-core", action="store_true", help="Coreを検証・発行する。Modへ未接続の依存を追加はしない")
    args = parser.parse_args()
    root = args.task_root.absolute()
    _directory(root)
    _directory(args.store.absolute())
    if root.parent.name != "worktrees":
        raise ContractError("build: 登録済みtaskのrootを指定してください")
    registry = TaskWorktrees(root.parent.parent)
    if not (registry.root / "registry.sqlite").is_file():
        raise ContractError("build: worktree registryがありません")
    names = {"Common", "SushiEricServerMod", ".github"}
    if args.manager:
        names.add("SushiEricServerManager")
    if args.with_core:
        names.add("SushiEricCombatCore")
    with build_lock(root / ".build.lock"):
        with registry.database() as db:
            db.execute("BEGIN IMMEDIATE")
            plans = {row["name"]: row for row in registry.owned(db, root.name, args.owner, ("ACTIVE",))}
            if not names <= plans.keys():
                raise ContractError("build: 必要なrepoがtaskに登録されていません")
            for name in names:
                path = root / name
                if worktrees(path).get(path.resolve()) != f"refs/heads/{plans[name]['branch']}":
                    raise ContractError("build: 登録branch/worktreeと一致しません")
        repos = {name: root / name for name in names}
        source_lock = capture_sources(repos)
        build_id = uuid.uuid4().hex
        maven = root / "maven" / build_id
        _directory(maven)
        maven.mkdir(parents=True, exist_ok=False)
        common = repos["Common"]
        gradle(common, "build", "publishDevelopment", f"-PcommonDevelopmentRepository={maven}")
        mod_coordinate = publication(common, "mod", "sushieric-common-mod-dev")
        editor_coordinate = publication(common, "editor", "sushieric-common-editor-dev")
        publications = []

        def record_publication(coordinate, repository):
            group, module, version = coordinate.split(":")
            for suffix in ("jar", "pom"):
                path = repository / group.replace(".", "/") / module / version / f"{module}-{version}.{suffix}"
                publications.append({"coordinate": coordinate, "path": str(path),
                                     "sha256": digest(regular_bytes(path))})

        record_publication(mod_coordinate, maven)
        record_publication(editor_coordinate, maven)
        mod_args = [f"-PcommonDevelopmentRepository={maven}", f"-PcommonDevelopmentCoordinate={mod_coordinate}"]
        if args.with_core:
            core_repo = maven / "core"
            gradle(repos["SushiEricCombatCore"], "build", "publishDevelopment", *mod_args,
                   f"-PcombatCoreDevelopmentRepository={core_repo}")
            record_publication(publication(repos["SushiEricCombatCore"], "core", "sushieric-combat-core-dev"), core_repo)
        if args.manager:
            gradle(repos["SushiEricServerManager"], "build", f"-PcommonDevelopmentRepository={maven}",
                   f"-PcommonDevelopmentCoordinate={editor_coordinate}")
        verify_sources(source_lock)
        source_lock["publications"] = publications
        lock = repos["SushiEricServerMod"] / "build/dev-runtime" / f"source-lock-{build_id}.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(canonical(source_lock))
        gradle(repos["SushiEricServerMod"], "build", "exportDevRuntime", *mod_args,
               f"-PruntimeSourceLock={lock}", f"-PruntimeArtifactStore={args.store.absolute()}",
               f"-PruntimePython={sys.executable}", f"-PruntimeExporter={Path(__file__).with_name('export-dev-runtime.py')}")
        verify_sources(source_lock)
        print(json.dumps({"task": root.name, "commonCoordinate": mod_coordinate, "status": "exported"}))


if __name__ == "__main__":
    main()
