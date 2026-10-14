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

    def main_fixture(self, extras=(), failure=None):
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

        mod_coordinate = (
            "io.github.sushiericworkspace:sushieric-datamodel-mod-dev:"
            "0.1.0-dev.20261010123456789.12345678-1234-1234-1234-123456789abc")
        editor_coordinate = (
            "io.github.sushiericworkspace:sushieric-datamodel-editor-dev:"
            "0.1.0-dev.20261010123456789.12345678-1234-1234-1234-123456789abc")
        build = data_model / "build"
        build.mkdir()
        (build / "development-publication.properties").write_text(
            f"mod={mod_coordinate}\neditor={editor_coordinate}\n")
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
            if repo == mod:
                lock_path = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("-PruntimeSourceLock=")))
                bundle = synthetic_bundle(store, json.loads(lock_path.read_bytes()))
                write_receipt(lock_path, bundle)
            if repo == data_model or repo.name == "SushiEricCombatCore":
                coordinates = (mod_coordinate, editor_coordinate) if repo == data_model else (core_coordinate,)
                repository = maven if repo == data_model else maven / "core"
                for coordinate in coordinates:
                    group, module, version = coordinate.split(":")
                    directory = repository / group.replace(".", "/") / module / version
                    directory.mkdir(parents=True)
                    (directory / f"{module}-{version}.jar").write_bytes(b"jar")
                    (directory / f"{module}-{version}.pom").write_bytes(b"pom")

        source_lock = {"sourceLockVersion": 1, "sources": [
            {"root": str(task / name), "record": {"repo": name, "commit": "0" * 40,
             "dirtyPatchSha256": digest(b""), "untrackedInputs": []},
             "inputs": [{"path": "scripts/export-dev-runtime.py"}] if name == ".github" else [],
             "patchBase64": ""} for name in sorted(names)]}
        with patch.object(build_dev_runtime, "__file__", str(github / "scripts/build-dev-runtime.py")), \
                patch.object(sys, "argv", ["build-dev-runtime.py", "--task-root", str(task),
                                            "--owner", "owner", "--store", str(store)]), \
                patch.object(build_dev_runtime, "registered_task", return_value={name: {} for name in names}) as registered, \
                patch.object(build_dev_runtime, "capture_sources", return_value=source_lock), \
                patch.object(build_dev_runtime, "verify_sources"), \
                patch.object(build_dev_runtime.uuid, "uuid4", return_value=type("Id", (), {"hex": build_id})()), \
                patch.object(build_dev_runtime, "gradle", side_effect=gradle_side_effect) as gradle:
            build_dev_runtime.main()

        self.assertEqual(3, registered.call_count)
        registered.assert_called_with(task.absolute(), "owner", names)
        export_call = next(call for call in gradle.call_args_list if "exportDevRuntime" in call.args)
        self.assertEqual(mod, export_call.args[0])
        self.assertIn(f"-PruntimeExporter={exporter.resolve()}", export_call.args)
        record = json.loads((task.parent / ".build-records/task-41" / f"{build_id}.json").read_bytes())
        self.assertEqual("SUCCEEDED", record["state"])
        self.assertTrue(record["artifactId"])
        return gradle.call_args_list

    def test_main_passes_recorded_task_exporter_to_gradle(self):
        self.main_fixture()

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

    def test_registered_manager_and_core_are_built_without_extra_flags(self):
        calls = self.main_fixture(("SushiEricServerManager", "SushiEricCombatCore"))
        self.assertTrue(any(call.args[0].name == "SushiEricServerManager" for call in calls))
        self.assertTrue(any(call.args[0].name == "SushiEricCombatCore" and "publishDevelopment" in call.args
                            for call in calls))


if __name__ == "__main__":
    unittest.main()
