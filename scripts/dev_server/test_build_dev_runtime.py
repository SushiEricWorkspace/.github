"""固定runtime helperが登録taskの入力だけを使うことを検証する。"""

from contextlib import contextmanager
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from dev_server.build_inputs import capture_sources
from dev_server.contracts import ContractError

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

    def test_registered_task_requires_github_with_same_branch(self):
        task = self.root / "worktrees/task-41"
        names = {"Common", "SushiEricServerMod", ".github"}
        for name in names:
            (task / name / "scripts").mkdir(parents=True)
        helper = task / ".github/scripts/build-dev-runtime.py"
        helper.touch()
        (task.parent.parent / "worktrees/registry.sqlite").touch()
        rows = [{"name": name, "branch": "feature/issue-41"} for name in names]
        calls = []

        class Registry:
            root = task.parent.parent / "worktrees"

            @contextmanager
            def database(self):
                yield object()

            def owned(self, db, task_id, owner, states):
                calls.append((task_id, owner, states))
                return rows

            def target(self, task_id, name):
                return task / name

        with patch.object(build_dev_runtime, "TaskWorktrees", return_value=Registry()), \
                patch.object(build_dev_runtime, "worktrees", side_effect=lambda path: {
                    path.resolve(): "refs/heads/feature/issue-41"}):
            plans = build_dev_runtime.registered_task(task, "owner", names)
        self.assertEqual(names, plans.keys())
        self.assertEqual(task / ".github", build_dev_runtime.task_github(task, helper))
        self.assertEqual([("task-41", "owner", ("ACTIVE",))], calls)

    def test_exporter_is_from_and_recorded_in_task_github(self):
        repos = {}
        for name in ("Common", "SushiEricServerMod", ".github"):
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

    def test_main_passes_recorded_task_exporter_to_gradle(self):
        task = self.root / "worktrees/task-41"
        common = task / "Common"
        mod = task / "SushiEricServerMod"
        github = task / ".github"
        common.mkdir(parents=True)
        mod.mkdir(parents=True)
        exporter = github / "scripts/export-dev-runtime.py"
        exporter.parent.mkdir(parents=True)
        exporter.write_text("# fixture\n")
        store = self.root / "store"
        store.mkdir()

        mod_coordinate = (
            "io.github.sushiericworkspace:sushieric-common-mod-dev:"
            "0.1.0-dev.20261010123456789.12345678-1234-1234-1234-123456789abc")
        editor_coordinate = (
            "io.github.sushiericworkspace:sushieric-common-editor-dev:"
            "0.1.0-dev.20261010123456789.12345678-1234-1234-1234-123456789abc")
        build = common / "build"
        build.mkdir()
        (build / "development-publication.properties").write_text(
            f"mod={mod_coordinate}\neditor={editor_coordinate}\n")
        build_id = "fixture-build"
        maven = task / "maven" / build_id

        def gradle_side_effect(repo, *args):
            if repo == common:
                for coordinate in (mod_coordinate, editor_coordinate):
                    group, module, version = coordinate.split(":")
                    directory = maven / group.replace(".", "/") / module / version
                    directory.mkdir(parents=True)
                    (directory / f"{module}-{version}.jar").write_bytes(b"jar")
                    (directory / f"{module}-{version}.pom").write_bytes(b"pom")

        source_lock = {"sources": [{"root": str(github), "record": {"repo": ".github"},
                                    "inputs": [{"path": "scripts/export-dev-runtime.py"}]}]}
        with patch.object(build_dev_runtime, "__file__", str(github / "scripts/build-dev-runtime.py")), \
                patch.object(sys, "argv", ["build-dev-runtime.py", "--task-root", str(task),
                                            "--owner", "owner", "--store", str(store)]), \
                patch.object(build_dev_runtime, "registered_task") as registered, \
                patch.object(build_dev_runtime, "capture_sources", return_value=source_lock), \
                patch.object(build_dev_runtime, "verify_sources"), \
                patch.object(build_dev_runtime.uuid, "uuid4", return_value=type("Id", (), {"hex": build_id})()), \
                patch.object(build_dev_runtime, "gradle", side_effect=gradle_side_effect) as gradle:
            build_dev_runtime.main()

        registered.assert_called_once_with(task.absolute(), "owner", {"Common", "SushiEricServerMod", ".github"})
        export_call = next(call for call in gradle.call_args_list if "exportDevRuntime" in call.args)
        self.assertEqual(mod, export_call.args[0])
        self.assertIn(f"-PruntimeExporter={exporter.resolve()}", export_call.args)


if __name__ == "__main__":
    unittest.main()
