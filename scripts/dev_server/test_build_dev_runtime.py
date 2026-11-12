"""固定runtime helperが登録taskの入力だけを使うことを検証する。"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from dev_server.build_inputs import canonical, capture_sources, digest
from dev_server.contracts import ContractError
from dev_server.task_worktrees import TaskError, TaskWorktrees, git
from dev_server.task_maven import write_receipt
from dev_server.test_task_maven import synthetic_bundle

spec = importlib.util.spec_from_file_location("build_dev_runtime", SCRIPTS / "build-dev-runtime.py")
build_dev_runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_dev_runtime)


class BuildDevRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_primary_other_task_and_copied_helpers_are_rejected_before_build(self):
        task = self.root / "worktrees/task-41"
        task.mkdir(parents=True)
        store = self.root / "store"
        store.mkdir()
        wrong_helpers = (
            self.root / ".github/scripts/build-dev-runtime.py",
            self.root / "worktrees/task-42/.github/scripts/build-dev-runtime.py",
            task / "build-dev-runtime-copy.py",
        )
        for helper in wrong_helpers:
            with self.subTest(helper=helper):
                with patch.object(build_dev_runtime, "__file__", str(helper)), \
                        patch.object(sys, "argv", [str(helper), "--task-root", str(task),
                                                    "--owner", "owner", "--store", str(store)]), \
                        patch.object(build_dev_runtime, "gradle") as gradle:
                    with self.assertRaises(ContractError) as raised:
                        build_dev_runtime.main()
                self.assertIn(str(task / ".github/scripts/build-dev-runtime.py"),
                              str(raised.exception))
                gradle.assert_not_called()
                self.assertFalse((task / ".build.lock").exists())
                self.assertFalse((task / "maven").exists())

    def registered_fixture(self, change="SushiEricServerMod"):
        names = {"SushiEricDataModel", "SushiEricServerMod", ".github", "SushiEricServerManager", "SushiEricCombatCore"}
        commits = {}
        for name in names:
            repo = self.root / name
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "test")
            git(repo, "config", "user.email", "test@example.invalid")
            (repo / "source.txt").write_text("fixture")
            (repo / ".gitignore").write_text("build/\n.gradle/\n")
            if name == ".github":
                (repo / "scripts").mkdir()
                (repo / "scripts/export-dev-runtime.py").write_text("# fixture\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "fixture")
            commits[name] = git(repo, "rev-parse", "HEAD")
        registry = TaskWorktrees(self.root)
        registry.create("task-40", 40, "owner", {change: commits.pop(change)},
                        dependencies=commits, fetch=False)
        return registry, self.root / "worktrees/task-40", names

    def test_registered_task_accepts_change_repo_and_fixed_dependencies(self):
        for change in ("SushiEricServerMod", "SushiEricDataModel", "SushiEricServerManager", ".github"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                registry, task, names = self.registered_fixture(change)
                plans = build_dev_runtime.registered_task(task, "owner", names)
                self.assertEqual("branch", plans[change]["kind"])
                self.assertTrue(all(p["kind"] == "dependency" for n, p in plans.items() if n != change))
                lock = capture_sources({name: task / name for name in plans})
                self.assertEqual(names, {source["record"]["repo"] for source in lock["sources"]})
                exporter = build_dev_runtime.recorded_exporter(task / ".github", lock)
                self.assertEqual(task / ".github/scripts/export-dev-runtime.py", exporter)
                with self.assertRaisesRegex(TaskError, "owner"):
                    build_dev_runtime.registered_task(task, "other", names)
                with self.assertRaisesRegex(ContractError, "必要なrepo"):
                    build_dev_runtime.registered_task(task, "owner", names | {"missing"})
                dependency = "SushiEricDataModel" if change != "SushiEricDataModel" else "SushiEricServerMod"
                (task / dependency / "source.txt").write_text("changed")
                with self.assertRaisesRegex(TaskError, "未コミット"):
                    build_dev_runtime.registered_task(task, "owner", names)

    def test_exporter_is_from_and_recorded_in_task_github(self):
        repos = {}
        for name in ("SushiEricDataModel", "SushiEricServerMod", ".github"):
            repo = self.root / "worktrees/task-41" / name
            repo.mkdir(parents=True)
            for args in (("init", "-q"), ("config", "user.name", "test"),
                         ("config", "user.email", "test@example.invalid")):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            (repo / "source.txt").write_text("fixture")
            repos[name] = repo
        exporter = repos[".github"] / "scripts/export-dev-runtime.py"
        exporter.parent.mkdir()
        exporter.write_text("# task exporter\n")
        for repo in repos.values():
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"],
                           check=True, capture_output=True)

        source_lock = capture_sources(repos)
        actual = build_dev_runtime.recorded_exporter(repos[".github"], source_lock)
        self.assertEqual(exporter.resolve(), actual)
        github_source = next(source for source in source_lock["sources"]
                             if source["record"]["repo"] == ".github")
        self.assertIn("scripts/export-dev-runtime.py",
                      {item["path"] for item in github_source["inputs"]})
        github_source["inputs"] = [item for item in github_source["inputs"]
                                   if item["path"] != "scripts/export-dev-runtime.py"]
        with self.assertRaises(ContractError):
            build_dev_runtime.recorded_exporter(repos[".github"], source_lock)

    def main_fixture(self, extras=(), failure=None, *, managed=False, stage=None):
        task = self.root / "worktrees/task-41"
        data_model = task / "SushiEricDataModel"
        mod = task / "SushiEricServerMod"
        github = task / ".github"
        names = {"SushiEricDataModel", "SushiEricServerMod", ".github"} | set(extras)
        for name in extras:
            (task / name).mkdir(parents=True)
        data_model.mkdir(parents=True)
        mod.mkdir(parents=True)
        exporter = github / "scripts/export-dev-runtime.py"
        exporter.parent.mkdir(parents=True)
        exporter.write_text("# fixture\n")
        store = self.root / "store"
        store.mkdir()
        profile = self.root / 'explicit-profile.json'
        if managed:
            from .test_managed_profile import fixture
            profile.write_bytes(canonical(fixture()[0]))

        mod_coordinate = (
            "io.github.sushiericworkspace:sushieric-datamodel-mod-dev:"
            "0.1.0-dev.20261010123456789.12345678-1234-1234-1234-123456789abc")
        editor_coordinate = (
            "io.github.sushiericworkspace:sushieric-datamodel-editor-dev:"
            "0.1.0-dev.20261010123456789.12345678-1234-1234-1234-123456789abc")
        host_coordinate = mod_coordinate.replace("sushieric-datamodel-mod-dev", "sushieric-datamodel-host-adapter-dev")
        build = data_model / "build"
        build.mkdir()
        (build / "development-publication.properties").write_text(
            f"mod={mod_coordinate}\neditor={editor_coordinate}\nhost={host_coordinate}\n")
        build_id = "a" * 32
        maven = task / "maven" / build_id
        core_coordinate = editor_coordinate.replace("sushieric-datamodel-editor-dev", "sushieric-combat-core-dev")
        if "SushiEricCombatCore" in extras:
            core_build = task / "SushiEricCombatCore/build"
            core_build.mkdir()
            (core_build / "development-publication.properties").write_text(f"core={core_coordinate}\n")

        def gradle_side_effect(repo, *args):
            if failure is not None:
                raise failure
            if repo == mod and "describeDevRuntime" in args:
                recipe_path = mod / "build/dev-runtime/recipe.json"
                recipe_path.parent.mkdir(parents=True, exist_ok=True)
                recipe_path.write_bytes(canonical({"dataModelCoordinate": mod_coordinate,
                                                   "profiles": {"server": {"classpath": ["first.jar"]},
                                                                "gameTest": {"classpath": [mod_coordinate]}}}))
            if repo == mod and 'exportDevRuntime' in args:
                lock_path = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("-PruntimeSourceLock=")))
                bundle = synthetic_bundle(store, json.loads(lock_path.read_bytes()))
                write_receipt(lock_path, bundle)
            if repo == data_model or repo.name == "SushiEricCombatCore":
                coordinates = (mod_coordinate, editor_coordinate, host_coordinate) if repo == data_model else (core_coordinate,)
                repository = maven if repo == data_model else maven / "core"
                for coordinate in coordinates:
                    group, module, version = coordinate.split(":")
                    directory = repository / group.replace(".", "/") / module / version
                    directory.mkdir(parents=True)
                    artifact = f"{module}-{version}-runtime.zip" if coordinate == host_coordinate else f"{module}-{version}.jar"
                    (directory / artifact).write_bytes(b"artifact")
                    (directory / f"{module}-{version}.pom").write_bytes(b"pom")

        def export(command, *, cwd, check):
            self.assertEqual(github, cwd)
            self.assertTrue(check)
            self.assertEqual(str(exporter.resolve()), command[2])
            self.assertEqual(str(profile.absolute()), command[command.index('--managed-profile') + 1])
            lock_path = Path(command[command.index('--source-lock') + 1])
            self.assertFalse(lock_path.with_suffix('.receipt.json').exists())
            write_receipt(lock_path, synthetic_bundle(store, json.loads(lock_path.read_bytes())))

        source_lock = {"sourceLockVersion": 1, "sources": [
            {"root": str(task / name), "record": {"repo": name, "commit": "0" * 40,
             "dirtyPatchSha256": digest(b""), "untrackedInputs": []},
             "inputs": [{"path": "scripts/export-dev-runtime.py"}] if name == ".github" else [],
             "patchBase64": ""} for name in sorted(names)]}
        argv = (["build-dev-runtime.py"] + ([stage] if stage else []) + ["--task-root", str(task),
                "--owner", "owner", "--store", str(store)] +
                (["--managed-profile", str(profile)] if managed else []))
        with patch.object(build_dev_runtime, "__file__", str(github / "scripts/build-dev-runtime.py")), \
                patch.object(sys, "argv", argv), \
                patch.object(build_dev_runtime, "registered_task", return_value={name: {} for name in names}) as registered, \
                patch.object(build_dev_runtime, "capture_sources", return_value=source_lock), \
                patch.object(build_dev_runtime, "verify_sources"), \
                patch.object(build_dev_runtime.uuid, "uuid4", return_value=type("Id", (), {"hex": build_id})()), \
                patch.object(build_dev_runtime, "gradle", side_effect=gradle_side_effect) as gradle, \
                patch.object(build_dev_runtime.subprocess, "run", side_effect=export) as exporter_run:
            build_dev_runtime.main()

        self.assertEqual(3, registered.call_count)
        registered.assert_called_with(task.absolute(), "owner", names)
        expected_task = "describeDevRuntime" if managed or stage == "prepare" else "exportDevRuntime"
        export_call = next(call for call in gradle.call_args_list if expected_task in call.args)
        if managed:
            exporter_run.assert_called_once()
        else:
            exporter_run.assert_not_called()
        self.assertEqual(mod, export_call.args[0])
        self.assertIn(f"-PruntimeExporter={exporter.resolve()}", export_call.args)
        self.assertIn("runGameTest", export_call.args)
        self.assertIn("runServer", export_call.args)
        self.assertEqual(2, export_call.args.count("-x"))
        host_publications = [item for item in source_lock["publications"] if item["coordinate"] == host_coordinate]
        self.assertEqual(2, len(host_publications))
        self.assertEqual({".zip", ".pom"}, {Path(item["path"]).suffix for item in host_publications})
        core_arguments = [arg for arg in export_call.args[1:] if arg.startswith("-PcombatCoreDevelopment")]
        if "SushiEricCombatCore" in extras:
            self.assertEqual([f"-PcombatCoreDevelopmentRepository={maven / 'core'}",
                              f"-PcombatCoreDevelopmentCoordinate={core_coordinate}"], core_arguments)
            recorded_core = [item for item in source_lock["publications"] if item["coordinate"] == core_coordinate]
            self.assertEqual(2, len(recorded_core))
            self.assertTrue(all(Path(item["path"]).is_relative_to(maven / "core") for item in recorded_core))
        else:
            self.assertEqual([], core_arguments)
        for call in gradle.call_args_list:
            if call.args[0] != mod:
                self.assertFalse(any(arg.startswith("-PcombatCoreDevelopmentCoordinate=") for arg in call.args[1:]))
        record = json.loads((task.parent / ".build-records/task-41" / f"{build_id}.json").read_bytes())
        self.assertEqual("STARTED" if stage == "prepare" else "SUCCEEDED", record["state"])
        if stage != "prepare":
            self.assertTrue(record["artifactId"])
        self.task, self.store, self.profile, self.exporter = task, store, profile, exporter
        return gradle.call_args_list

    def test_main_passes_recorded_task_exporter_to_gradle(self):
        self.main_fixture()

    def test_managed_profile_uses_describe_then_official_exporter_without_old_receipt(self):
        self.main_fixture(managed=True)

    def test_prepare_describes_and_saves_recipe_without_export(self):
        calls = self.main_fixture(stage="prepare")
        self.assertTrue(any("describeDevRuntime" in call.args for call in calls))
        state = json.loads((self.store / ".prepared" / ("a" * 32) / "prepared.json").read_bytes())
        self.assertEqual("PREPARED", state["state"])
        self.assertEqual("task-41", state["taskId"])
        self.assertEqual("owner", state["owner"])
        self.assertEqual(["first.jar", state["dataModelCoordinate"]],
                         build_dev_runtime.recipe_classpath(json.loads(Path(state["recipe"]).read_bytes())))
        record = json.loads((self.task.parent / ".build-records/task-41" / ("a" * 32 + ".json")).read_bytes())
        self.assertEqual("STARTED", record["state"])

    def test_prepare_final_output_failure_records_failed_from_started_record(self):
        with patch("builtins.print", side_effect=RuntimeError("final output failed")):
            with self.assertRaisesRegex(RuntimeError, "final output failed"):
                self.main_fixture(stage="prepare")
        record_path = self.root / "worktrees/.build-records/task-41" / ("a" * 32 + ".json")
        self.assertEqual("FAILED", json.loads(record_path.read_bytes())["state"])

    def test_freeze_exports_prepared_recipe_once_without_rebuilding(self):
        self.main_fixture(stage="prepare")
        from .test_managed_profile import fixture
        self.profile.write_bytes(canonical(fixture()[0]))
        argv = ["build-dev-runtime.py", "freeze", "--task-root", str(self.task), "--owner", "owner",
                "--store", str(self.store), "--prepared-id", "a" * 32,
                "--managed-profile", str(self.profile)]
        helper = str(self.task / ".github/scripts/build-dev-runtime.py")
        with patch.object(build_dev_runtime, "__file__", helper), \
                patch.object(sys, "argv", argv), \
                patch.object(build_dev_runtime, "registered_task", return_value={".github": {}}), \
                patch.object(build_dev_runtime, "verify_sources"), \
                patch.object(build_dev_runtime, "recorded_exporter", return_value=self.exporter.resolve()), \
                patch.object(build_dev_runtime.subprocess, "run") as export, \
                patch.object(build_dev_runtime.TaskMaven, "succeed", return_value={"artifactId": "bundle-id"}) as succeed:
            build_dev_runtime.main()
            export.assert_called_once()
            command = export.call_args.args[0]
            self.assertIn(str(self.task / "SushiEricServerMod/build/dev-runtime/recipe.json"), command)
            self.assertIn(str(self.task / "SushiEricServerMod" / "build/dev-runtime" /
                              ("source-lock-" + "a" * 32 + ".json")), command)
            self.assertIn(str(self.profile.absolute()), command)
            succeed.assert_called_once()
        with patch.object(build_dev_runtime, "__file__", helper), \
                patch.object(sys, "argv", argv), patch.object(build_dev_runtime.subprocess, "run") as export:
            with self.assertRaisesRegex(ContractError, "消費済み"):
                build_dev_runtime.main()
            export.assert_not_called()

    def test_freeze_exporter_failure_records_failed_and_preserves_exception(self):
        self.main_fixture(stage="prepare")
        from .test_managed_profile import fixture
        self.profile.write_bytes(canonical(fixture()[0]))
        argv = ["build-dev-runtime.py", "freeze", "--task-root", str(self.task), "--owner", "owner",
                "--store", str(self.store), "--prepared-id", "a" * 32,
                "--managed-profile", str(self.profile)]
        helper = str(self.task / ".github/scripts/build-dev-runtime.py")
        failure = subprocess.CalledProcessError(7, "exporter")
        with patch.object(build_dev_runtime, "__file__", helper), \
                patch.object(sys, "argv", argv), \
                patch.object(build_dev_runtime, "registered_task", return_value={".github": {}}), \
                patch.object(build_dev_runtime, "verify_sources"), \
                patch.object(build_dev_runtime, "recorded_exporter", return_value=self.exporter.resolve()), \
                patch.object(build_dev_runtime.subprocess, "run", side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                build_dev_runtime.main()
        self.assertIs(failure, raised.exception)
        record_path = self.task.parent / ".build-records/task-41" / ("a" * 32 + ".json")
        self.assertEqual("FAILED", json.loads(record_path.read_bytes())["state"])
        self.assertTrue((self.store / ".prepared" / ("a" * 32) / "consumed").is_file())

    def test_freeze_registered_task_rejection_invalidates_matching_preparation(self):
        store, directory, _, _, _ = self.prepared_fixture()
        root = self.root / "worktrees/task-41"
        with patch.object(build_dev_runtime, "verify_sources"), \
                patch.object(build_dev_runtime, "registered_task", side_effect=TaskError("task registration changed")):
            with self.assertRaisesRegex(TaskError, "task registration changed"):
                build_dev_runtime.load_prepared_for_freeze(root, store, "a" * 32, "task-41", "owner")
        state = json.loads((directory / "prepared.json").read_bytes())
        self.assertEqual("INVALID", state["state"])
        self.assertEqual("task registration changed", state["failure"])

    def test_freeze_other_owner_cannot_invalidate_preparation(self):
        store, directory, _, _, _ = self.prepared_fixture()
        with patch.object(build_dev_runtime, "registered_task") as registered:
            with self.assertRaisesRegex(ContractError, "task/owner/store/ID"):
                build_dev_runtime.load_prepared_for_freeze(
                    self.root / "worktrees/task-41", store, "a" * 32, "task-41", "other-owner")
        registered.assert_not_called()
        self.assertEqual("PREPARED", json.loads((directory / "prepared.json").read_bytes())["state"])

    def test_freeze_consumed_marker_write_failure_records_failed(self):
        self.main_fixture(stage="prepare")
        from .test_managed_profile import fixture
        self.profile.write_bytes(canonical(fixture()[0]))
        argv = ["build-dev-runtime.py", "freeze", "--task-root", str(self.task), "--owner", "owner",
                "--store", str(self.store), "--prepared-id", "a" * 32,
                "--managed-profile", str(self.profile)]
        helper = str(self.task / ".github/scripts/build-dev-runtime.py")
        original_open = Path.open

        class BrokenMarker:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, data):
                raise OSError("marker write failed")

        def open_path(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return BrokenMarker(stream) if path.name == "consumed" else stream

        with patch.object(build_dev_runtime, "__file__", helper), \
                patch.object(sys, "argv", argv), \
                patch.object(build_dev_runtime, "registered_task", return_value={".github": {}}), \
                patch.object(build_dev_runtime, "verify_sources"), \
                patch.object(Path, "open", autospec=True, side_effect=open_path), \
                patch.object(build_dev_runtime.subprocess, "run") as export:
            with self.assertRaisesRegex(OSError, "marker write failed"):
                build_dev_runtime.main()
        export.assert_not_called()
        self.assertTrue((self.store / ".prepared" / ("a" * 32) / "consumed").is_file())
        record_path = self.task.parent / ".build-records/task-41" / ("a" * 32 + ".json")
        self.assertEqual("FAILED", json.loads(record_path.read_bytes())["state"])

    def test_invalidation_save_failure_keeps_original_and_stale_temp_is_replaced(self):
        store, directory, _, recipe, _ = self.prepared_fixture()
        recipe.write_bytes(recipe.read_bytes() + b" ")
        temporary = directory / "prepared.invalid"
        temporary.write_text("stale", encoding="utf-8")
        with patch.object(build_dev_runtime, "invalidate_prepared", side_effect=OSError("invalidation write failed")):
            with self.assertRaises(ContractError) as raised:
                build_dev_runtime.load_prepared_for_freeze(
                    self.root / "worktrees/task-41", store, "a" * 32, "task-41", "owner")
        self.assertRegex(str(raised.exception), "recipe/source lock")
        self.assertTrue(any("invalidation write failed" in note for note in raised.exception.__notes__))
        build_dev_runtime.invalidate_prepared(directory / "prepared.json", raised.exception)
        self.assertFalse(temporary.exists())
        self.assertEqual("INVALID", json.loads((directory / "prepared.json").read_bytes())["state"])

    def test_invalid_preparation_keeps_first_failure_reason(self):
        store, directory, _, _, _ = self.prepared_fixture()
        path = directory / "prepared.json"
        state = json.loads(path.read_bytes())
        state.update(state="INVALID", failure="original validation reason")
        path.write_bytes(canonical(state))
        with patch.object(build_dev_runtime, "invalidate_prepared") as invalidate:
            with self.assertRaisesRegex(ContractError, "original validation reason"):
                build_dev_runtime.load_prepared_for_freeze(
                    self.root / "worktrees/task-41", store, "a" * 32, "task-41", "owner")
        invalidate.assert_not_called()

    def test_freeze_interrupt_does_not_invalidate_preparation(self):
        store, directory, _, _, _ = self.prepared_fixture()
        interrupt = KeyboardInterrupt()
        with patch.object(build_dev_runtime, "load_prepared", side_effect=interrupt), \
                patch.object(build_dev_runtime, "registered_task"), \
                patch.object(build_dev_runtime, "invalidate_prepared") as invalidate:
            with self.assertRaises(KeyboardInterrupt) as raised:
                build_dev_runtime.load_prepared_for_freeze(
                    self.root / "worktrees/task-41", store, "a" * 32, "task-41", "owner")
        self.assertIs(interrupt, raised.exception)
        invalidate.assert_not_called()
        self.assertEqual("PREPARED", json.loads((directory / "prepared.json").read_bytes())["state"])

    def test_freeze_rechecks_recipe_hash_immediately_before_exporter(self):
        self.main_fixture(stage="prepare")
        from .test_managed_profile import fixture
        self.profile.write_bytes(canonical(fixture()[0]))
        recipe = self.task / "SushiEricServerMod/build/dev-runtime/recipe.json"
        argv = ["build-dev-runtime.py", "freeze", "--task-root", str(self.task), "--owner", "owner",
                "--store", str(self.store), "--prepared-id", "a" * 32,
                "--managed-profile", str(self.profile)]
        helper = str(self.task / ".github/scripts/build-dev-runtime.py")

        def replace_recipe(*args):
            recipe.write_bytes(recipe.read_bytes() + b" ")
            return {".github": {}}

        with patch.object(build_dev_runtime, "__file__", helper), \
                patch.object(sys, "argv", argv), \
                patch.object(build_dev_runtime, "registered_task", side_effect=replace_recipe), \
                patch.object(build_dev_runtime, "verify_sources"), \
                patch.object(build_dev_runtime, "recorded_exporter", return_value=self.exporter.resolve()), \
                patch.object(build_dev_runtime.subprocess, "run") as export:
            with self.assertRaisesRegex(ContractError, "直前のrecipe/source lock hash"):
                build_dev_runtime.main()
        export.assert_not_called()
        record_path = self.task.parent / ".build-records/task-41" / ("a" * 32 + ".json")
        self.assertEqual("FAILED", json.loads(record_path.read_bytes())["state"])

    def test_build_failure_is_recorded_and_maven_is_kept(self):
        with self.assertRaisesRegex(RuntimeError, "fixture Gradle failure"):
            self.main_fixture(failure=RuntimeError("fixture Gradle failure"))
        record_path = self.root / "worktrees/.build-records/task-41" / ("a" * 32 + ".json")
        record = json.loads(record_path.read_bytes())
        self.assertEqual("FAILED", record["state"])
        self.assertTrue(Path(record["maven"]).exists())
        self.assertIsNone(record["artifactId"])

    def test_failure_record_task_error_keeps_original_exception_and_note(self):
        original = RuntimeError("original build failure")
        with patch.object(build_dev_runtime.TaskMaven, "fail", side_effect=TaskError("record path changed")):
            with self.assertRaises(RuntimeError) as raised:
                self.main_fixture(failure=original)
        self.assertIs(original, raised.exception)
        self.assertTrue(any("record path changed" in note for note in original.__notes__))

    def test_registered_core_is_passed_to_mod_without_manager(self):
        self.main_fixture(("SushiEricCombatCore",))

    def test_registered_manager_and_core_are_built_without_extra_flags(self):
        calls = self.main_fixture(("SushiEricServerManager", "SushiEricCombatCore"))
        self.assertTrue(any(call.args[0].name == "SushiEricServerManager" for call in calls))
        self.assertTrue(any(call.args[0].name == "SushiEricCombatCore" and "publishDevelopment" in call.args
                            for call in calls))

    def prepared_fixture(self):
        store = self.root / "store"
        store.mkdir()
        lock = self.root / "source-lock.json"
        publication = self.root / "publication.jar"
        publication.write_bytes(b"original")
        lock.write_bytes(canonical({"sourceLockVersion": 1, "sources": [], "publications": [
            {"path": str(publication), "sha256": digest(b"original")}]}))
        recipe = self.root / "recipe.json"
        coordinate = "io.github.sushiericworkspace:sushieric-datamodel-mod-dev:0.1.0-dev.20261010123456789.12345678-1234-1234-1234-123456789abc"
        recipe.write_bytes(canonical({"dataModelCoordinate": coordinate,
                                      "profiles": {"server": {"classpath": [coordinate]},
                                                   "gameTest": {"classpath": []}}}))
        state = {"preparedVersion": 1, "state": "PREPARED", "taskId": "task-41", "owner": "owner",
                 "store": str(store.absolute()), "buildId": "a" * 32, "repos": [".github"],
                 "sourceLock": str(lock), "sourceLockSha256": digest(lock.read_bytes()),
                 "recipe": str(recipe), "recipeSha256": digest(recipe.read_bytes()),
                 "dataModelCoordinate": coordinate}
        build_dev_runtime.save_prepared(store, "a" * 32, state)
        directory = build_dev_runtime.prepared_directory(store, "a" * 32)
        return store, directory, lock, recipe, publication

    def test_prepared_state_rechecks_identity_hashes_source_and_single_use(self):
        store, directory, lock, recipe, _ = self.prepared_fixture()
        with patch.object(build_dev_runtime, "verify_sources") as verify:
            state, _ = build_dev_runtime.load_prepared(store, "a" * 32, "task-41", "owner")
        self.assertEqual("PREPARED", state["state"])
        verify.assert_called_once()
        with self.assertRaisesRegex(ContractError, "task/owner/store/ID"):
            build_dev_runtime.load_prepared(store, "a" * 32, "task-41", "another-owner")
        recipe.write_bytes(recipe.read_bytes() + b" ")
        with self.assertRaisesRegex(ContractError, "recipe/source lock"):
            build_dev_runtime.load_prepared(store, "a" * 32, "task-41", "owner")
        recipe.write_bytes(canonical({"dataModelCoordinate": state["dataModelCoordinate"],
                                      "profiles": {"server": {"classpath": []},
                                                   "gameTest": {"classpath": [state["dataModelCoordinate"]]}}}))
        (directory / "consumed").write_bytes(b"consumed")
        with self.assertRaisesRegex(ContractError, "消費済み"):
            build_dev_runtime.load_prepared(store, "a" * 32, "task-41", "owner")

    def test_prepared_state_rejects_changed_sources_and_publication(self):
        store, _, _, _, publication = self.prepared_fixture()
        with patch.object(build_dev_runtime, "verify_sources", side_effect=ContractError("source changed")):
            with self.assertRaisesRegex(ContractError, "source changed"):
                build_dev_runtime.load_prepared(store, "a" * 32, "task-41", "owner")
        publication.write_bytes(b"changed")
        with patch.object(build_dev_runtime, "verify_sources"):
            with self.assertRaisesRegex(ContractError, "公開物"):
                build_dev_runtime.load_prepared(store, "a" * 32, "task-41", "owner")

    def test_prepared_publication_snapshot_is_bound(self):
        store, directory, _, _, _ = self.prepared_fixture()
        copied_publication = next((directory / "publications").iterdir())
        copied_publication.write_bytes(b"tampered")
        with patch.object(build_dev_runtime, "verify_sources"):
            with self.assertRaisesRegex(ContractError, "snapshotが変更"):
                build_dev_runtime.load_prepared(store, "a" * 32, "task-41", "owner")

    def test_integrity_failure_invalidates_preparation_after_input_is_restored(self):
        store, directory, _, recipe, _ = self.prepared_fixture()
        original = recipe.read_bytes()
        recipe.write_bytes(original + b" ")
        with patch.object(build_dev_runtime, "verify_sources"):
            with patch.object(build_dev_runtime, "registered_task", return_value={".github": {}}):
                with self.assertRaisesRegex(ContractError, "recipe/source lock"):
                    build_dev_runtime.load_prepared_for_freeze(
                        self.root / "worktrees/task-41", store, "a" * 32, "task-41", "owner")
            recipe.write_bytes(original)
            with self.assertRaisesRegex(ContractError, "recipe/source lock"):
                build_dev_runtime.load_prepared_for_freeze(
                    self.root / "worktrees/task-41", store, "a" * 32, "task-41", "owner")


if __name__ == "__main__":
    unittest.main()
