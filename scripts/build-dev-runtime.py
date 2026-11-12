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
from dev_server.runtime_bundle import _directory, _read_managed_profile
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


def prepared_directory(store: Path, build_id: str) -> Path:
    return store / ".prepared" / build_id


def recipe_classpath(recipe: dict) -> list[str]:
    profiles = recipe.get("profiles", {})
    return list(dict.fromkeys(entry for name in ("server", "gameTest")
                              for entry in profiles.get(name, {}).get("classpath", [])))


def save_prepared(store: Path, build_id: str, state: dict) -> Path:
    directory = prepared_directory(store, build_id)
    _directory(directory.parent)
    directory.parent.mkdir(exist_ok=True)
    _directory(directory.parent)
    directory.mkdir(parents=True, exist_ok=False)
    _directory(directory)
    lock = json.loads(regular_bytes(Path(state["sourceLock"])))
    files = [(Path(state["sourceLock"]), directory / "source-lock.json", state["sourceLockSha256"]),
             (Path(state["recipe"]), directory / "recipe.json", state["recipeSha256"])]
    files.extend((Path(item["path"]), directory / "publications" / f"{index:04d}-{Path(item['path']).name}",
                  item["sha256"]) for index, item in enumerate(lock.get("publications", []) + lock.get("publicationEvidence", [])))
    saved = []
    for source, destination, expected_hash in files:
        data = regular_bytes(source)
        if digest(data) != expected_hash:
            raise ContractError("prepare: 保存前に準備入力が変更されました")
        _directory(destination.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _directory(destination.parent)
        with destination.open("xb") as output:
            output.write(data)
        saved.append({"source": str(source), "path": str(destination), "sha256": expected_hash})
    state["savedFiles"] = saved
    path = directory / "prepared.json"
    with path.open("xb") as output:
        output.write(canonical(state))
    return path


def load_prepared(store: Path, build_id: str, task_id: str, owner: str) -> tuple[dict, Path]:
    if not re.fullmatch(r"[0-9a-f]{32}", build_id):
        raise ContractError("prepare: 準備IDは32桁のbuild IDが必要です")
    directory = prepared_directory(store, build_id)
    _directory(directory)
    path = directory / "prepared.json"
    state = json.loads(regular_bytes(path))
    if (state.get("preparedVersion") != 1 or state.get("state") != "PREPARED" or state.get("buildId") != build_id
            or state.get("taskId") != task_id or state.get("owner") != owner
            or state.get("store") != str(store.absolute())):
        raise ContractError("prepare: task/owner/store/IDが準備記録と一致しません")
    if (directory / "consumed").exists() or (directory / "consumed").is_symlink():
        raise ContractError("prepare: 準備記録は消費済みです")
    lock_path = Path(state["sourceLock"])
    recipe_path = Path(state["recipe"])
    lock_raw, recipe_raw = regular_bytes(lock_path), regular_bytes(recipe_path)
    if digest(lock_raw) != state["sourceLockSha256"] or digest(recipe_raw) != state["recipeSha256"]:
        raise ContractError("prepare: recipe/source lockが準備後に変更されています")
    lock = json.loads(lock_raw)
    recipe = json.loads(recipe_raw)
    if digest(canonical(lock)) != state["sourceLockSha256"]:
        raise ContractError("prepare: source lockのcanonical hashが一致しません")
    verify_sources(lock)
    for item in lock.get("publications", []) + lock.get("publicationEvidence", []):
        if digest(regular_bytes(Path(item["path"]))) != item["sha256"]:
            raise ContractError("prepare: 公開物が準備後に変更されています")
    expected_files = [(lock_path, directory / "source-lock.json", state["sourceLockSha256"]),
                      (recipe_path, directory / "recipe.json", state["recipeSha256"])]
    expected_files.extend((Path(item["path"]), directory / "publications" /
                           f"{index:04d}-{Path(item['path']).name}", item["sha256"])
                          for index, item in enumerate(lock.get("publications", []) + lock.get("publicationEvidence", [])))
    expected_saved = [{"source": str(source), "path": str(saved), "sha256": expected_hash}
                      for source, saved, expected_hash in expected_files]
    if state.get("savedFiles") != expected_saved:
        raise ContractError("prepare: 保存済みsnapshotの記録が一致しません")
    for _, saved, expected_hash in expected_files:
        if digest(regular_bytes(saved)) != expected_hash:
            raise ContractError("prepare: 保存済みsnapshotが変更されています")
    if recipe.get("dataModelCoordinate") != state["dataModelCoordinate"]:
        raise ContractError("prepare: recipeのGAVが準備記録と一致しません")
    return state, path


def invalidate_prepared(path: Path, error: BaseException) -> None:
    state = json.loads(regular_bytes(path))
    state["state"] = "INVALID"
    state["failure"] = str(error)
    temporary = path.with_suffix(".invalid")
    temporary.unlink(missing_ok=True)
    with temporary.open("xb") as output:
        output.write(canonical(state))
    os.replace(temporary, path)


def load_prepared_for_freeze(root: Path, store: Path, build_id: str, task_id: str, owner: str) -> tuple[dict, Path]:
    path = prepared_directory(store, build_id) / "prepared.json"
    identity = json.loads(regular_bytes(path))
    if (identity.get("taskId") != task_id or identity.get("owner") != owner
            or identity.get("store") != str(store.absolute()) or identity.get("buildId") != build_id):
        raise ContractError("prepare: task/owner/store/IDが準備記録と一致しません")
    if identity.get("state") == "INVALID":
        raise ContractError(f"prepare: 準備記録は失効済みです: {identity.get('failure', '')}")
    try:
        state, prepared_path = load_prepared(store, build_id, task_id, owner)
        registered_task(root, owner, set(state["repos"]))
        return state, prepared_path
    except Exception as error:
        try:
            invalidate_prepared(path, error)
        except Exception as save_error:
            error.add_note(f"準備の失効記録を保存できませんでした: {save_error}")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", choices=("prepare", "freeze"),
                        help="prepareはpublish/describeし、freezeは保存済み準備を一度だけexportします")
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--manager", action="store_true")
    parser.add_argument("--with-core", action="store_true", help="Coreを検証・発行する。Modへ未接続の依存を追加はしない")
    parser.add_argument("--managed-profile", type=Path, help="通常M用exact Profileの明示候補。承認はsuperviseで別途指定")
    parser.add_argument("--prepared-id", help="freezeするprepareのbuild ID")
    parser.add_argument("--release-input-lock", type=Path, help="Release原本と取得済み6成果物を含む正式監査入力")
    args = parser.parse_args()
    if args.release_input_lock is not None and (args.manager or args.with_core or args.stage == "freeze"):
        raise ContractError("release: 正式版監査入力はprepare/一括実行で指定し、manager/with-coreと併用しません")
    root = args.task_root.absolute()
    _directory(root)
    _directory(args.store.absolute())
    profile_raw = regular_bytes(args.managed_profile) if args.managed_profile is not None else None
    if profile_raw is not None:
        _read_managed_profile(profile_raw)
    names = {"SushiEricDataModel", "SushiEricServerMod", ".github"}
    if args.manager:
        names.add("SushiEricServerManager")
    if args.with_core:
        names.add("SushiEricCombatCore")
    github = task_github(root, Path(__file__))
    if args.stage == "freeze":
        if profile_raw is None or not args.prepared_id:
            raise ContractError("freeze: --prepared-idと独立供給された--managed-profileが必要です")
        if args.manager or args.with_core:
            raise ContractError("freeze: prepare時のbuild選択肢は準備記録から読み取ります")
        task_id = root.name
        records = TaskMaven(root.parent.parent)
        with build_lock(root / ".build.lock"):
            state, prepared_path = load_prepared_for_freeze(
                root, args.store.absolute(), args.prepared_id, task_id, args.owner)
            names = set(state["repos"])
            record_path = records.records / task_id / f"{args.prepared_id}.json"
            build_record = json.loads(regular_bytes(record_path))
            if (build_record.get("state") != "STARTED" or build_record.get("owner") != args.owner
                    or build_record.get("taskId") != task_id or build_record.get("buildId") != args.prepared_id
                    or build_record.get("sourceLock") != state["sourceLock"]):
                raise ContractError("freeze: task build記録が開始時のowner/taskと一致しません")
            directory = prepared_path.parent
            try:
                with (directory / "consumed").open("xb") as consumed:
                    consumed.write(canonical({"taskId": task_id, "owner": args.owner,
                                              "buildId": args.prepared_id}))
                source_lock_raw = regular_bytes(Path(state["sourceLock"]))
                recipe_raw = regular_bytes(Path(state["recipe"]))
                if (digest(canonical(json.loads(source_lock_raw))) != state["sourceLockSha256"]
                        or digest(recipe_raw) != state["recipeSha256"]):
                    raise ContractError("freeze: exporter開始直前のrecipe/source lock hashが一致しません")
                source_lock = json.loads(source_lock_raw)
                exporter = recorded_exporter(github, source_lock)
                if regular_bytes(args.managed_profile) != profile_raw:
                    raise ContractError("freeze: 明示Profileが開始前に変更されました")
                subprocess.run([sys.executable, "-B", str(exporter), "--recipe", state["recipe"],
                                "--source-lock", state["sourceLock"], "--store", str(args.store.absolute()),
                                "--managed-profile", str(args.managed_profile.absolute())], cwd=github, check=True)
                if regular_bytes(args.managed_profile) != profile_raw:
                    raise ContractError("freeze: 明示Profileがexport中に変更されました")
                verify_sources(source_lock)
                registered_task(root, args.owner, names)
                record = records.succeed(build_record, source_lock)
            except BaseException as error:
                try:
                    records.fail(build_record, error)
                except (TaskError, OSError, ValueError, KeyError, TypeError) as record_error:
                    error.add_note(f"失敗記録を保存できませんでした: {record_error}")
                raise
        print(json.dumps({"task": task_id, "dataModelCoordinate": state["dataModelCoordinate"],
                          "status": "exported", "buildId": args.prepared_id,
                          "artifactId": record["artifactId"]}))
        return
    if args.stage == "prepare" and (args.managed_profile is not None or args.prepared_id):
        raise ContractError("prepare: Profile/準備IDは受け取りません")
    if args.stage is None and args.prepared_id:
        raise ContractError("--prepared-idはfreezeで指定してください")
    with build_lock(root / ".build.lock"):
        plans = registered_task(root, args.owner, names)
        names |= plans.keys()
        manager = "SushiEricServerManager" in names
        core = "SushiEricCombatCore" in names
        if args.release_input_lock is None and not core:
            raise ContractError("build: 開発runtimeにはSushiEricCombatCoreの登録が必要です。新しいDataModel固定座標を参照するCoreを同じtaskで発行してください")
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
            if args.release_input_lock is not None:
                if manager:
                    raise ContractError("release: Managerを含むtaskは正式runtimeの対象外です")
                from dev_server.release_inputs import capture_release_inputs
                source_lock = capture_release_inputs(source_lock, args.release_input_lock, maven)
                selected = source_lock["librarySelection"]
                mod_coordinate = selected["dataModelCoordinate"]
                publications = source_lock["publications"]
                mod_args = [f"-PdataModelReleaseVersion={mod_coordinate.split(':')[2]}",
                            f"-PcombatCoreReleaseVersion={selected['combatCoreCoordinate'].split(':')[2]}"]
            else:
                data_model = repos["SushiEricDataModel"]
                gradle(data_model, "build", "publishDevelopment", "-PdataModelReleaseVersion=",
                       f"-PdataModelDevelopmentRepository={maven}")
                mod_coordinate = publication(data_model, "mod", "sushieric-datamodel-mod-dev")
                editor_coordinate = publication(data_model, "editor", "sushieric-datamodel-editor-dev")
                host_coordinate = publication(data_model, "host", "sushieric-datamodel-host-adapter-dev")
                publications = []

                def record_publication(coordinate, repository, suffixes=(".jar", ".pom")):
                    group, module, version = coordinate.split(":")
                    for suffix in suffixes:
                        path = repository / group.replace(".", "/") / module / version / f"{module}-{version}{suffix}"
                        publications.append({"coordinate": coordinate, "path": str(path),
                                             "sha256": digest(regular_bytes(path))})

                record_publication(mod_coordinate, maven)
                record_publication(editor_coordinate, maven)
                record_publication(host_coordinate, maven, ("-runtime.zip", ".pom"))
                mod_args = ["-PdataModelReleaseVersion=", "-PcombatCoreReleaseVersion=",
                            f"-PdataModelDevelopmentRepository={maven}", f"-PdataModelDevelopmentCoordinate={mod_coordinate}"]
                core_repo = maven / "core"
                gradle(repos["SushiEricCombatCore"], "build", "publishDevelopment", *mod_args,
                       f"-PcombatCoreDevelopmentRepository={core_repo}")
                core_coordinate = publication(repos["SushiEricCombatCore"], "core", "sushieric-combat-core-dev")
                record_publication(core_coordinate, core_repo)
                mod_args.extend([f"-PcombatCoreDevelopmentRepository={core_repo}",
                                 f"-PcombatCoreDevelopmentCoordinate={core_coordinate}"])
                if manager:
                    gradle(repos["SushiEricServerManager"], "build", "-PdataModelReleaseVersion=", f"-PdataModelDevelopmentRepository={maven}",
                           f"-PdataModelDevelopmentCoordinate={editor_coordinate}")
            verify_sources(source_lock)
            registered_task(root, args.owner, names)
            source_lock["publications"] = publications
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_bytes(canonical(source_lock))
            staged = args.stage == "prepare"
            gradle(repos["SushiEricServerMod"], "build", "describeDevRuntime" if (staged or args.managed_profile is not None) else "exportDevRuntime", "-x", "runGameTest", "-x", "runServer", *mod_args,
                   f"-PruntimeSourceLock={lock}", f"-PruntimeArtifactStore={args.store.absolute()}",
                   f"-PruntimePython={sys.executable}",
                   f"-PruntimeExporter={exporter}")
            if staged:
                recipe_path = repos["SushiEricServerMod"] / "build/dev-runtime/recipe.json"
                recipe = json.loads(regular_bytes(recipe_path))
                if not recipe.get("dataModelCoordinate"):
                    raise ContractError("prepare: describe結果にDataModel GAVがありません")
                state = {"preparedVersion": 1, "state": "PREPARED", "taskId": root.name,
                         "owner": args.owner, "store": str(args.store.absolute()), "buildId": build_id,
                         "repos": sorted(names), "sourceLock": str(lock),
                         "sourceLockSha256": digest(canonical(source_lock)), "recipe": str(recipe_path),
                         "recipeSha256": digest(regular_bytes(recipe_path)),
                         "dataModelCoordinate": recipe["dataModelCoordinate"]}
                save_prepared(args.store.absolute(), build_id, state)
                registered_task(root, args.owner, names)
                print(json.dumps({"task": root.name, "dataModelCoordinate": recipe["dataModelCoordinate"],
                                  "classpath": recipe_classpath(recipe), "status": "prepared", "buildId": build_id,
                                  "prepared": str(prepared_directory(args.store.absolute(), build_id))}))
                return
            if args.managed_profile is not None:
                if regular_bytes(args.managed_profile) != profile_raw:
                    raise ContractError("build: 明示Profileがビルド中に変更されました")
                subprocess.run([sys.executable, "-B", str(exporter),
                                "--recipe", str(repos["SushiEricServerMod"] / "build/dev-runtime/recipe.json"),
                                "--source-lock", str(lock), "--store", str(args.store.absolute()),
                                "--managed-profile", str(args.managed_profile.absolute())], cwd=github, check=True)
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
