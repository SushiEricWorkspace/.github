"""登録済みtaskでSushiEricDataModelを発行し、固定座標でconsumerをビルドしてbundleを出力する。"""

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
from dev_server.task_worktrees import TaskError, TaskWorktrees
from dev_server.task_maven import TaskMaven
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


def task_github(root: Path, script: Path) -> Path:
    root = root.absolute()
    expected = root / ".github/scripts/build-dev-runtime.py"
    if root.parent.name != "worktrees":
        raise ContractError(f"build: task worktreeのrootを指定してください。実行helper: {expected}")
    github = root / ".github"
    if script.is_symlink() or script.absolute() != expected.absolute():
        raise ContractError(f"build: 登録済みtaskの.githubから実行してください: {expected}")
    return github


def registered_task(root: Path, owner: str, names: set[str]):
    registry = TaskWorktrees(root.parent.parent)
    if not (registry.root / "registry.sqlite").is_file():
        raise ContractError("build: worktree registryがありません")
    snapshot = registry._read_snapshot(root.name, owner, ("ACTIVE",))
    plans = {row["name"]: row for row in snapshot[1]}
    if not names <= plans.keys():
        raise ContractError("build: 必要なrepoがtaskに登録されていません")
    for plan in plans.values():
        registry.verify(root.name, plan)
    with registry.database() as db:
        db.execute("BEGIN")
        registry._recheck(db, root.name, snapshot)
    return plans


def recorded_exporter(github: Path, source_lock: dict) -> Path:
    source = next(item for item in source_lock["sources"] if item["record"]["repo"] == ".github")
    exporter = (github / "scripts/export-dev-runtime.py").resolve()
    relative = exporter.relative_to(Path(source["root"]).resolve()).as_posix()
    if not any(item["path"] == relative and not item.get("deleted") for item in source["inputs"]):
        raise ContractError("build: task .githubのexporterが入力記録にありません")
    return exporter


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
    names = {"SushiEricDataModel", "SushiEricServerMod", ".github"}
    if args.manager:
        names.add("SushiEricServerManager")
    if args.with_core:
        names.add("SushiEricCombatCore")
    github = task_github(root, Path(__file__))
    with build_lock(root / ".build.lock"):
        plans = registered_task(root, args.owner, names)
        names |= plans.keys()
        manager = "SushiEricServerManager" in names
        core = "SushiEricCombatCore" in names
        repos = {name: root / name for name in names}
        source_lock = capture_sources(repos)
        exporter = recorded_exporter(github, source_lock)
        build_id = uuid.uuid4().hex
        maven = root / "maven" / build_id
        _directory(maven)
        lock = repos["SushiEricServerMod"] / "build/dev-runtime" / f"source-lock-{build_id}.json"
        records = TaskMaven(root.parent.parent)
        record = records.start(root.name, build_id, args.owner, lock)
        try:
            maven.mkdir(parents=True, exist_ok=False)
            data_model = repos["SushiEricDataModel"]
            gradle(data_model, "build", "publishDevelopment", f"-PdataModelDevelopmentRepository={maven}")
            mod_coordinate = publication(data_model, "mod", "sushieric-datamodel-mod-dev")
            editor_coordinate = publication(data_model, "editor", "sushieric-datamodel-editor-dev")
            publications = []

            def record_publication(coordinate, repository):
                group, module, version = coordinate.split(":")
                for suffix in ("jar", "pom"):
                    path = repository / group.replace(".", "/") / module / version / f"{module}-{version}.{suffix}"
                    publications.append({"coordinate": coordinate, "path": str(path),
                                         "sha256": digest(regular_bytes(path))})

            record_publication(mod_coordinate, maven)
            record_publication(editor_coordinate, maven)
            mod_args = [f"-PdataModelDevelopmentRepository={maven}", f"-PdataModelDevelopmentCoordinate={mod_coordinate}"]
            if core:
                core_repo = maven / "core"
                gradle(repos["SushiEricCombatCore"], "build", "publishDevelopment", *mod_args,
                       f"-PcombatCoreDevelopmentRepository={core_repo}")
                core_coordinate = publication(repos["SushiEricCombatCore"], "core", "sushieric-combat-core-dev")
                record_publication(core_coordinate, core_repo)
                mod_args.extend([f"-PcombatCoreDevelopmentRepository={core_repo}",
                                 f"-PcombatCoreDevelopmentCoordinate={core_coordinate}"])
            if manager:
                gradle(repos["SushiEricServerManager"], "build", f"-PdataModelDevelopmentRepository={maven}",
                       f"-PdataModelDevelopmentCoordinate={editor_coordinate}")
            verify_sources(source_lock)
            registered_task(root, args.owner, names)
            source_lock["publications"] = publications
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_bytes(canonical(source_lock))
            gradle(repos["SushiEricServerMod"], "build", "exportDevRuntime", *mod_args,
                   f"-PruntimeSourceLock={lock}", f"-PruntimeArtifactStore={args.store.absolute()}",
                   f"-PruntimePython={sys.executable}",
                   f"-PruntimeExporter={exporter}")
            verify_sources(source_lock)
            registered_task(root, args.owner, names)
            record = records.succeed(record, source_lock)
        except BaseException as error:
            try:
                records.fail(record, error)
            except (TaskError, OSError, ValueError, KeyError, TypeError) as record_error:
                error.add_note(f"失敗記録を保存できませんでした: {record_error}")
            raise
        print(json.dumps({"task": root.name, "dataModelCoordinate": mod_coordinate, "status": "exported",
                          "buildId": build_id, "artifactId": record["artifactId"]}))


if __name__ == "__main__":
    main()
