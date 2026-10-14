"""小さな合成runtimeでコピー・パス除去・改変拒否を検証する。Minecraft起動試験とは区別する。"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_server.build_inputs import canonical, capture_sources, digest
from dev_server.contracts import ContractError
from dev_server.runtime_bundle import freeze, launch_command, verify_bundle


def saved_v1_bundle(store: Path) -> Path:
    """旧契約の保存済みbundleを再現する合成fixture。新freezeは使わない。"""
    import platform
    from dev_server.contracts import LAYOUT_ID
    from dev_server.fixtures import file_entries
    classpath = ["files/legacy-common.jar"]
    profile = {"mainClass": "synthetic.LegacyMain", "classpath": classpath,
               "jvmArgs": ["-Dfabric.dli.config=${run}/.launch/server-launch.cfg"], "args": ["nogui"]}
    profiles = {"server": profile, "gameTest": dict(profile, jvmArgs=profile["jvmArgs"] +
                                                   ["-Dsushieric.management.disabled=true"])}
    payload = {"files/legacy-common.jar": b"synthetic legacy Common",
               "jdk/bin/" + ("java.exe" if os.name == "nt" else "java"): b"synthetic legacy Java",
               "launch/profiles.json": canonical({"version": 1, "platform": platform.system(),
                                                   "machine": platform.machine(), "profiles": profiles})}
    for name in profiles:
        payload[f"launch/{name}-launch.cfg"] = b"commonProperties\n\tfabric.development=true\n"
        payload[f"launch/{name}-remap-classpath.txt"] = b"${bundle}/files/legacy-common.jar"
    manifest = {"schemaVersion": 1, "artifactId": "0" * 64, "layoutId": LAYOUT_ID, "development": True,
                "javaVersion": "21.0.9", "commonCoordinate":
                "io.github.sushiericworkspace:sushieric-common-mod-dev:0.1.0-dev.20261010123456789",
                "files": file_entries(payload), "sources": [{"repo": repo, "commit": "0" * 40,
                "dirtyPatchSha256": digest(b""), "untrackedInputs": []} for repo in ("Common", "SushiEricServerMod")],
                "tools": [{"name": name, "version": "synthetic-v1", "sha256": digest(b"legacy")}
                          for name in ("jdk", "gradle", "loom", "minecraft", "yarn", "fabric-loader", "fabric-api", "kotlin")],
                "launch": {key: profile[key] for key in ("mainClass", "classpath", "jvmArgs")}}
    manifest["artifactId"] = digest(canonical({key: value for key, value in manifest.items() if key != "artifactId"}))
    payload["runtime.json"] = canonical(manifest)
    payload["COMPLETE"] = manifest["artifactId"].encode()
    bundle = store / manifest["artifactId"]
    for name, data in payload.items():
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return bundle


class RuntimeBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        repos = {}
        for repo in ("SushiEricDataModel", "SushiEricServerMod"):
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
        self.data_model_coordinate = "io.github.sushiericworkspace:sushieric-datamodel-mod-dev:0.1.0-dev.20261010123456789.12345678-1234-1234-1234-123456789abc"
        artifacts = []
        for coordinate, name in ((self.data_model_coordinate, "datamodel.jar"),
                                  ("net.minecraft:minecraft:1.21.11", "minecraft.jar"),
                                  ("net.fabricmc:yarn:1.21.11+build.6", "yarn.jar"),
                                  ("net.fabricmc:fabric-loader:0.19.3", "loader.jar"),
                                  ("net.fabricmc.fabric-api:fabric-api:0.141.4+1.21.11", "api.jar"),
                                  ("org.jetbrains.kotlin:kotlin-stdlib:2.2.21", "kotlin.jar")):
            path = deps / name
            path.write_text(name)
            artifacts.append({"coordinate": coordinate, "path": str(path)})
        (deps / "datamodel.pom").write_text("synthetic POM")
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
        self.recipe = {"recipeVersion": 2, "development": True, "javaHome": str(java.parent.parent),
                       "javaVersion": "21.0.9", "gradleHome": str(gradle.parent), "gradleVersion": "9.5.0",
                       "dataModelCoordinate": self.data_model_coordinate, "loomJar": str(loom),
                       "minecraftVersion": "1.21.11", "yarnVersion": "1.21.11+build.6", "loaderVersion": "0.19.3",
                       "fabricApiVersion": "0.141.4+1.21.11", "kotlinVersion": "2.2.21",
                       "profiles": {"server": profile, "gameTest": game_test}, "artifacts": artifacts, "plugins": [],
                       "dependencyInputs": [{"path": item["path"], "sha256": digest(Path(item["path"]).read_bytes())}
                                            for item in artifacts] +
                                            [{"path": str(deps / "datamodel.pom"), "sha256": digest((deps / "datamodel.pom").read_bytes())}]}
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

    def test_same_generation_relaunch_uses_new_auxiliary_without_changing_cwd(self):
        bundle = self.freeze()
        generation = self.root / "generation"
        generation.mkdir()
        deployed = generation / "runtime"
        shutil.copytree(bundle, deployed)
        properties = {"instanceId": "persistent-01", "generationId": "generation-01", "startNonce": "nonce-01",
                      "proofPath": str(self.root / "proof.json"), "runtimeManifest": str(deployed / "runtime.json")}
        first = self.root / "diagnostics/run-01"
        second = self.root / "diagnostics/run-02"
        one = launch_command(deployed, "server", generation, auxiliary=first, jvm_properties=properties)
        preserved = (first / "java.args").read_bytes()
        two = launch_command(deployed, "server", generation, auxiliary=second,
                             jvm_properties=dict(properties, startNonce="nonce-02"))
        self.assertNotEqual(one, two)
        self.assertEqual(preserved, (first / "java.args").read_bytes())
        self.assertIn(str(second).replace("\\", "\\\\"), (second / "java.args").read_text())
        self.assertIn(str(second), (second / "server-launch.cfg").read_text())
        verify_bundle(deployed)
        with self.assertRaises(ContractError):
            launch_command(deployed, "server", deployed / "data", auxiliary=self.root / "invalid")
        with self.assertRaises(ContractError):
            launch_command(deployed, "server", generation, auxiliary=first)

    def test_unicode_classpath_uses_java_launcher_encoding(self):
        bundle = self.freeze()
        deployed = self.root / "日 空 ソ 表 bundle"
        shutil.copytree(bundle, deployed)
        run = self.root / "日 空 ソ 表 generation"
        run.mkdir()
        command = launch_command(deployed, "server", run)
        encoding = "mbcs" if os.name == "nt" else "utf-8"
        arguments = [json.loads(line.decode("latin-1")).encode("latin-1").decode(encoding)
                     for line in Path(command[1][1:]).read_bytes().splitlines()]
        classpath = arguments[arguments.index("-cp") + 1]
        self.assertTrue(all(Path(entry).exists() for entry in classpath.split(os.pathsep)))
        self.assertIn(str(run), arguments[0])

    def test_partial_management_properties_rejected_before_writing(self):
        bundle = self.freeze()
        run = self.root / "run"
        run.mkdir()
        with self.assertRaises(ContractError):
            launch_command(bundle, "server", run, jvm_properties={"startNonce": "incomplete"})
        self.assertFalse((run / ".launch").exists())

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

    def test_old_unknown_and_mixed_recipe_versions_are_not_new_output(self):
        for changes in ({"recipeVersion": 1}, {"recipeVersion": 99}, {"recipeVersion": True},
                        {"commonCoordinate": self.data_model_coordinate}):
            original = self.recipe.copy()
            self.recipe.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.freeze()
            self.assertFalse(self.store.exists())
            self.recipe = original

    def test_data_model_jar_pom_and_precompile_hash_are_required(self):
        from copy import deepcopy
        original = deepcopy(self.recipe)
        for count in (0, 2):
            self.recipe = deepcopy(original)
            self.recipe["artifacts"] = original["artifacts"][1:] + [original["artifacts"][0]] * count
            with self.subTest(count=count), self.assertRaises(ContractError):
                self.freeze()
        self.recipe = deepcopy(original)
        self.recipe["dependencyInputs"] = original["dependencyInputs"][:-1]
        with self.assertRaises(ContractError):
            self.freeze()
        self.recipe = original
        pom = Path(self.recipe["artifacts"][0]["path"]).with_suffix(".pom")
        before = pom.read_bytes()
        pom.write_bytes(b"modified POM after compilation")
        with self.assertRaises(ContractError):
            self.freeze()
        pom.unlink()
        with self.assertRaises(FileNotFoundError):
            self.freeze()
        pom.write_bytes(before)
        runtime = verify_bundle(self.freeze())
        self.assertEqual(2, runtime["schemaVersion"])
        self.assertEqual(self.data_model_coordinate, runtime["dataModelCoordinate"])
        self.assertNotIn("commonCoordinate", runtime)

    def test_changed_source_refused_before_publication(self):
        (self.sources / "SushiEricDataModel/source.txt").write_text("changed")
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
        self.store = self.sources / "SushiEricDataModel/artifacts"
        with self.assertRaises(ContractError):
            self.freeze()

    def test_changed_runtime_contract_is_rejected(self):
        bundle = self.freeze()
        manifest = json.loads((bundle / "runtime.json").read_bytes())
        manifest["dataModelCoordinate"] = self.data_model_coordinate + ".modified"
        (bundle / "runtime.json").write_bytes(canonical(manifest))
        with self.assertRaises(ContractError):
            verify_bundle(bundle)
