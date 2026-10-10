"""小さな合成runtimeでコピー・パス除去・改変拒否を検証する。Minecraft起動試験とは区別する。"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_server.build_inputs import canonical, capture_sources, digest
from dev_server.contracts import ContractError
from dev_server.runtime_bundle import freeze, launch_command, verify_bundle


class RuntimeBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        repos = {}
        for repo in ("Common", "SushiEricServerMod"):
            path = self.sources / repo
            path.mkdir(parents=True)
            for args in (("init", "-q"), ("config", "user.name", "test"),
                         ("config", "user.email", "test@example.invalid")):
                subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
            (path / "source.txt").write_text("source")
            for args in (("add", "."), ("commit", "-qm", "fixture")):
                subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
            repos[repo] = path
        self.lock = self.root / "source-lock.json"
        self.lock.write_bytes(canonical(capture_sources(repos)))
        deps = self.root / "mutable-cache"
        deps.mkdir()
        self.classes = deps / "classes"
        self.classes.mkdir()
        (self.classes / "Test.class").write_bytes(b"synthetic")
        (self.classes / "fabric.mod.json").write_text('{"id":"test"}')
        self.common_coordinate = "io.github.sushiericworkspace:sushieric-common-mod-dev:0.1.0-dev.20261010123456789"
        artifacts = []
        for coordinate, name in ((self.common_coordinate, "common.jar"),
                                  ("net.minecraft:minecraft:1.21.11", "minecraft.jar"),
                                  ("net.fabricmc:yarn:1.21.11+build.6", "yarn.jar"),
                                  ("net.fabricmc:fabric-loader:0.19.3", "loader.jar"),
                                  ("net.fabricmc.fabric-api:fabric-api:0.141.4+1.21.11", "api.jar"),
                                  ("org.jetbrains.kotlin:kotlin-stdlib:2.2.21", "kotlin.jar")):
            path = deps / name
            path.write_text(name)
            artifacts.append({"coordinate": coordinate, "path": str(path)})
        (deps / "common.pom").write_text("synthetic POM")
        loom = deps / "fabric-loom-1.17.21.jar"
        loom.write_text("synthetic Loom")
        java = deps / "jdk/bin" / ("java.exe" if os.name == "nt" else "java")
        java.parent.mkdir(parents=True)
        java.write_text("synthetic Java")
        gradle = deps / "gradle/lib"
        gradle.mkdir(parents=True)
        (gradle / "gradle-launcher.jar").write_text("synthetic Gradle")
        log4j = deps / "log4j.xml"
        log4j.write_text("<Configuration/>")
        remap = deps / "remap.txt"
        remap.write_text(os.pathsep.join(item["path"] for item in artifacts))
        config = deps / "launch.cfg"
        config.write_text(f"commonProperties\n\tfabric.development=true\n\tlog4j.configurationFile={log4j}\n"
                          f"\tfabric.remapClasspathFile={remap}\n\tfabric.classPathGroups={self.classes}\n"
                          "clientArgs\n\t--assetsDir\n\t/never-copy-client-assets\n")
        cp = [str(self.classes), *[item["path"] for item in artifacts]]
        argfile = deps / "args.txt"
        argfile.write_text('-classpath\n"' + os.pathsep.join(cp).replace("\\", "\\\\") + '"\n')
        profile = {"mainClass": "net.fabricmc.devlaunchinjector.Main", "classpath": cp,
                   "jvmArgs": [f"-Dfabric.dli.config={config}", "-Dfabric.dli.env=server", f"@{argfile}"],
                   "args": ["nogui"], "environment": {}, "runDirectory": str(self.root / "old-run")}
        game_test = dict(profile, jvmArgs=profile["jvmArgs"] + ["-Dfabric-api.gametest",
                                                              "-Dsushieric.management.disabled=true"])
        self.recipe = {"recipeVersion": 1, "development": True, "javaHome": str(java.parent.parent),
                       "javaVersion": "21.0.9", "gradleHome": str(gradle.parent), "gradleVersion": "9.5.0",
                       "commonCoordinate": self.common_coordinate, "loomJar": str(loom),
                       "minecraftVersion": "1.21.11", "yarnVersion": "1.21.11+build.6", "loaderVersion": "0.19.3",
                       "fabricApiVersion": "0.141.4+1.21.11", "kotlinVersion": "2.2.21",
                       "profiles": {"server": profile, "gameTest": game_test}, "artifacts": artifacts, "plugins": [],
                       "dependencyInputs": [{"path": item["path"], "sha256": digest(Path(item["path"]).read_bytes())}
                                            for item in artifacts]}
        self.recipe_path = self.root / "recipe.json"
        self.store = self.root / "artifacts"

    def freeze(self):
        self.recipe_path.write_bytes(canonical(self.recipe))
        return freeze(self.recipe_path, self.lock, self.store)

    def test_launch_has_no_source_cache_or_gradle_dependency(self):
        bundle = self.freeze()
        self.sources.rename(self.root / "retired-sources")
        (self.root / "mutable-cache").rename(self.root / "retired-cache")
        verify_bundle(bundle)
        run = self.root / "new-run"
        run.mkdir()
        command = launch_command(bundle, "gameTest", run)
        self.assertEqual(str(bundle / "jdk/bin" / ("java.exe" if os.name == "nt" else "java")), command[0])
        self.assertIn("-Dsushieric.management.disabled=true", (run / ".launch/java.args").read_text())
        self.assertTrue(all("mutable-cache" not in arg and "sources" not in arg for arg in command))
        self.assertNotIn("/never-copy-client-assets", (run / ".launch/gameTest-launch.cfg").read_text())

    def test_unquoted_java_argfile_preserves_backslashes(self):
        argfile = Path(next(arg[1:] for arg in self.recipe["profiles"]["server"]["jvmArgs"] if arg.startswith("@")))
        argfile.write_text("-classpath\n" + os.pathsep.join(self.recipe["profiles"]["server"]["classpath"]) + "\n")
        verify_bundle(self.freeze())

    def test_actual_remapped_fabric_api_is_recorded(self):
        item = next(item for item in self.recipe["artifacts"] if item["coordinate"].startswith("net.fabricmc.fabric-api:fabric-api:"))
        item["coordinate"] = "remapped.net.fabricmc.fabric-api:fabric-api-16c8840d:0.141.4+1.21.11"
        tools = verify_bundle(self.freeze())["tools"]
        self.assertEqual("0.141.4+1.21.11", next(tool["version"] for tool in tools if tool["name"] == "fabric-api"))

    def test_complete_marker_missing_or_modified_is_rejected(self):
        bundle = self.freeze()
        (bundle / "COMPLETE").unlink()
        with self.assertRaises(FileNotFoundError):
            verify_bundle(bundle)
        (bundle / "COMPLETE").write_text("wrong")
        with self.assertRaises(ContractError):
            verify_bundle(bundle)

    def test_missing_modified_extra_files_are_rejected(self):
        bundle = self.freeze()
        target = bundle / "inputs/dependencies.json"
        original = target.read_bytes()
        target.write_bytes(b"tampered")
        with self.assertRaises(ContractError):
            verify_bundle(bundle)
        target.write_bytes(original)
        (bundle / "extra.txt").write_text("extra")
        with self.assertRaises(ContractError):
            verify_bundle(bundle)
        (bundle / "extra.txt").unlink()
        target.unlink()
        with self.assertRaises(ContractError):
            verify_bundle(bundle)

    def test_changed_source_refused_before_publication(self):
        (self.sources / "Common/source.txt").write_text("changed")
        with self.assertRaises(ContractError):
            self.freeze()
        self.assertFalse(self.store.exists())

    def test_changed_dependency_after_compilation_is_rejected(self):
        Path(self.recipe["artifacts"][0]["path"]).write_text("changed dependency")
        with self.assertRaises(ContractError):
            self.freeze()

    def test_unclassified_paths_or_environment_are_rejected(self):
        self.recipe["profiles"]["server"]["jvmArgs"].append("-Dunknown=/external/path")
        with self.assertRaises(ContractError):
            self.freeze()

    def test_same_artifact_not_overwritten(self):
        first = self.freeze()
        with self.assertRaises(ContractError):
            self.freeze()
        verify_bundle(first)

    def test_bundle_under_worktree_is_rejected(self):
        self.store = self.sources / "Common/artifacts"
        with self.assertRaises(ContractError):
            self.freeze()

    def test_changed_runtime_contract_is_rejected(self):
        bundle = self.freeze()
        manifest = json.loads((bundle / "runtime.json").read_bytes())
        manifest["commonCoordinate"] = self.common_coordinate + ".modified"
        (bundle / "runtime.json").write_bytes(canonical(manifest))
        with self.assertRaises(ContractError):
            verify_bundle(bundle)
