"""正式版の原本照合とfreezeの改ざん拒否を検証する。"""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from .build_inputs import canonical, digest, portable_lock, verify_sources
from .contracts import ContractError
from .release_inputs import capture_release_inputs, expected_files, portable_publication, release_original, selection, verify_libraries
from .runtime_bundle import _files, verify_bundle
from . import test_runtime_bundle


class ReleaseInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime_bundle.RuntimeBundleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.selected = selection("0.3.0", "0.1.1")
        self.lock = json.loads(self.fixture.lock.read_bytes())
        self.lock.update(sourceLockVersion=2, librarySelection=self.selected, publications=[], publicationEvidence=[])
        model = Path(self.fixture.recipe["artifacts"][0]["path"]).read_bytes()
        import io
        import zipfile
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("README.md", b"guide")
            z.writestr("lib/model.jar", model)
        for item in expected_files(self.selected):
            coordinate = item["coordinate"]
            if item["extension"] == "pom":
                group, module, version = coordinate.split(":")
                dependency = ""
                if module == "sushieric-combat-core":
                    dependency = "<dependencies><dependency><groupId>io.github.sushiericworkspace</groupId><artifactId>sushieric-datamodel</artifactId><version>0.3.0</version></dependency></dependencies>"
                data = (f'<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>{group}</groupId>'
                        f'<artifactId>{module}</artifactId><version>{version}</version>{dependency}</project>').encode()
            else:
                data = archive.getvalue() if item["extension"] == "zip" else model
            path = self.root / "published" / item["filename"]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            self.lock["publications"].append({key: value for key, value in item.items() if key != "filename"} |
                                             dict(path=str(path), sha256=digest(data)))
        self.originals = {}
        for repo, version, commit in (("SushiEricDataModel", "0.3.0", "a" * 40), ("SushiEricCombatCore", "0.1.1", "b" * 40)):
            publications = [item for item in self.lock["publications"] if
                            (item["coordinate"] == self.selected["combatCoreCoordinate"]) == (repo == "SushiEricCombatCore")]
            destination = "https://maven.pkg.github.com/sushiericworkspace/" + repo
            record = dict(repository="SushiEricWorkspace/" + repo, version=version, commit=commit,
                          workingTreeClean=True, destination=destination, remoteVerification=dict(result="verified"),
                          artifacts=[dict(coordinate=item["coordinate"] +
                            (":runtime@zip" if item["extension"] == "zip" else ""),
                            file=Path(item["path"]).name, sha256=item["sha256"]) for item in publications])
            data = canonical(record)
            path = self.root / repo / "input-lock.json"
            path.parent.mkdir()
            path.write_bytes(data)
            evidence_hash = digest(data)
            self.lock["publicationEvidence"].append(dict(path=str(path), sha256=evidence_hash))
            for item in publications:
                item["origin"] = dict(mode="release", repository=destination, sourceRepository=record["repository"],
                                      sourceCommit=commit, evidenceSha256=evidence_hash)
            self.originals[(record["repository"], version)] = (data, commit)
        self.online = patch("dev_server.release_inputs.release_original", side_effect=lambda repo, version: self.originals[(repo, version)])
        self.online.start()
        self.addCleanup(self.online.stop)

    def test_capture_preserves_originals_and_portable_paths(self):
        path = self.root / "input.json"
        path.write_bytes(canonical(self.lock))
        captured = capture_release_inputs(self.lock, path, self.root / "maven/id")
        verify_sources(captured)
        for entry in captured["publications"] + captured["publicationEvidence"]:
            self.assertIn(self.root / "maven/id", Path(entry["path"]).parents)
        portable = portable_lock(captured)
        self.assertNotIn(str(self.root), json.dumps(portable))

    def test_self_consistent_fake_commit_rejected_against_release(self):
        self.replace_evidence(lambda record: record.update(commit="c" * 40))
        verify_libraries(self.lock)
        with self.assertRaisesRegex(ContractError, "Release添付原本"):
            verify_libraries(self.lock, online=True)

    def test_tag_mismatch_rejected(self):
        key = ("SushiEricWorkspace/SushiEricDataModel", "0.3.0")
        self.originals[key] = (self.originals[key][0], "d" * 40)
        with self.assertRaisesRegex(ContractError, "タグ参照先"):
            verify_sources(self.lock)

    def test_capture_rejects_before_copy_when_tag_changes(self):
        key = ("SushiEricWorkspace/SushiEricDataModel", "0.3.0")
        self.originals[key] = (self.originals[key][0], "d" * 40)
        path = self.root / "input.json"
        path.write_bytes(canonical(self.lock))
        destination = self.root / "rejected"
        with self.assertRaisesRegex(ContractError, "タグ参照先"):
            capture_release_inputs(self.lock, path, destination)
        self.assertFalse(destination.exists())

    def test_fake_commit_cannot_reach_compile(self):
        from .test_build_dev_runtime import build_dev_runtime as builder
        import sys
        self.replace_evidence(lambda record: record.update(commit="c" * 40))
        path = self.root / "fake-input.json"
        path.write_bytes(canonical(self.lock))
        task = self.root / "worktrees/task-113"
        task.mkdir(parents=True)
        names = {"SushiEricDataModel", "SushiEricServerMod", ".github"}
        with patch.object(builder, "task_github", return_value=task / ".github"), \
                patch.object(builder, "registered_task", return_value={name: {} for name in names}), \
                patch.object(builder, "capture_sources", return_value=deepcopy(self.lock)), \
                patch.object(builder, "recorded_exporter", return_value=task / ".github/scripts/export-dev-runtime.py"), \
                patch.object(builder, "TaskMaven"), patch.object(builder, "gradle") as compile_step, \
                patch.object(sys, "argv", ["build-dev-runtime.py", "prepare", "--task-root", str(task),
                    "--store", str(self.root / "store"), "--owner", "owner", "--release-input-lock", str(path)]):
            with self.assertRaisesRegex(ContractError, "Release添付原本"):
                builder.main()
            compile_step.assert_not_called()

    def replace_evidence(self, change):
        entry = self.lock["publicationEvidence"][0]
        old_hash = entry["sha256"]
        record = json.loads(Path(entry["path"]).read_bytes())
        change(record)
        Path(entry["path"]).write_bytes(canonical(record))
        entry["sha256"] = digest(canonical(record))
        for publication in self.lock["publications"]:
            if publication["origin"]["evidenceSha256"] == old_hash:
                publication["origin"].update(evidenceSha256=entry["sha256"], sourceCommit=record["commit"])

    def test_tampering_missing_evidence_and_mixed_selection_rejected(self):
        for mutation in (lambda lock: lock["publicationEvidence"].pop(),
                         lambda lock: lock["publications"].pop(),
                         lambda lock: lock["publications"][0].update(sha256="0" * 64),
                         lambda lock: lock["publications"][0]["origin"].update(sourceCommit="e" * 40),
                         lambda lock: lock["librarySelection"].update(combatCoreCoordinate="x:y:0.1.1")):
            lock = deepcopy(self.lock)
            mutation(lock)
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                verify_libraries(lock)

    def test_old_versions_rejected(self):
        for versions in (("0.2.0", "0.1.1"), ("0.3.0", "0.1.0")):
            with self.assertRaises(ContractError):
                selection(*versions)

    def test_escaped_secret_in_original_rejected(self):
        self.replace_evidence(lambda record: record.update(extra={"password": "dummy"}))
        with self.assertRaisesRegex(ContractError, "認証情報"):
            verify_libraries(self.lock)

    def test_undefined_metadata_is_rejected_and_excluded_from_public_data(self):
        for field in ("publications", "publicationEvidence"):
            for key, value in (("authorization", "dummy"), ("privatePath", "/home/example/private")):
                lock = deepcopy(self.lock)
                lock[field][0][key] = value
                with self.subTest(field=field, key=key):
                    with self.assertRaisesRegex(ContractError, "未定義"):
                        verify_libraries(lock)
                    self.assertNotIn(key, portable_publication(lock[field][0]))

    def test_host_different_version_rejected(self):
        lock = deepcopy(self.lock)
        lock["librarySelection"]["hostAdapterCoordinate"] = lock["librarySelection"]["hostAdapterCoordinate"].replace("0.3.0", "0.3.1")
        with self.assertRaisesRegex(ContractError, "選択GAV"):
            verify_libraries(lock)

    def test_core_pom_model_mismatch_rejected(self):
        core = next(entry for entry in self.lock["publications"] if
                    entry["coordinate"] == self.selected["combatCoreCoordinate"] and entry["extension"] == "pom")
        path = Path(core["path"])
        path.write_bytes(path.read_bytes().replace(b"<version>0.3.0</version>", b"<version>0.3.1</version>"))
        core["sha256"] = digest(path.read_bytes())
        evidence = next(entry for entry in self.lock["publicationEvidence"] if entry["sha256"] == core["origin"]["evidenceSha256"])
        record = json.loads(Path(evidence["path"]).read_bytes())
        next(item for item in record["artifacts"] if item["file"] == path.name)["sha256"] = core["sha256"]
        Path(evidence["path"]).write_bytes(canonical(record))
        evidence["sha256"] = digest(canonical(record))
        for item in self.lock["publications"]:
            if item["coordinate"] == self.selected["combatCoreCoordinate"]:
                item["origin"]["evidenceSha256"] = evidence["sha256"]
        with self.assertRaisesRegex(ContractError, "Core POMのDataModel"):
            verify_libraries(self.lock)

    def test_release_original_tracks_lightweight_and_annotated_tags(self):
        commit = "e" * 40
        for annotated in (False, True):
            ref = dict(type="tag", sha="f" * 40) if annotated else dict(type="commit", sha=commit)
            replies = [canonical(dict(draft=False, tag_name="v0.3.0", assets=[dict(name="input-lock.json", id=7)])),
                       b"original bytes", canonical(dict(object=ref))]
            if annotated:
                replies.append(canonical(dict(object=dict(type="commit", sha=commit))))
            with self.subTest(annotated=annotated), patch("dev_server.release_inputs._gh", side_effect=replies) as gh:
                self.assertEqual((b"original bytes", commit), release_original("owner/repo", "0.3.0"))
                self.assertEqual(4 if annotated else 3, gh.call_count)
                if annotated:
                    self.assertIn("git/tags/" + "f" * 40, gh.call_args.args[1])

    def test_release_original_rejects_tag_without_commit(self):
        replies = [canonical(dict(draft=False, tag_name="v0.3.0", assets=[dict(name="input-lock.json", id=7)])),
                   b"original bytes", canonical(dict(object=dict(type="tree", sha="f" * 40)))]
        with patch("dev_server.release_inputs._gh", side_effect=replies):
            with self.assertRaisesRegex(ContractError, "参照先がcommit"):
                release_original("owner/repo", "0.3.0")

    def release_recipe(self):
        recipe = self.fixture.recipe
        recipe.update(recipeVersion=3, dataModelCoordinate=self.selected["dataModelCoordinate"])
        for key in ("librarySelection", "publications", "publicationEvidence"):
            recipe[key] = self.lock[key]
        recipe["artifacts"][0]["coordinate"] = self.selected["dataModelCoordinate"]
        core = next(item for item in self.lock["publications"] if item["coordinate"] == self.selected["combatCoreCoordinate"] and item["extension"] == "jar")
        recipe["artifacts"].append(dict(coordinate=core["coordinate"], path=core["path"]))
        recipe["dependencyInputs"] += [dict(path=item["path"], sha256=item["sha256"])
                                      for item in self.lock["publications"] + self.lock["publicationEvidence"]]
        self.fixture.lock.write_bytes(canonical(self.lock))

    def test_release_freeze_and_offline_verification(self):
        self.release_recipe()
        bundle = self.fixture.freeze()
        with patch("dev_server.release_inputs.release_original", side_effect=AssertionError("offline")):
            manifest = verify_bundle(bundle)
        self.assertEqual(self.selected["dataModelCoordinate"], manifest["dataModelCoordinate"])
        portable = json.loads((bundle / "inputs/source-lock.json").read_bytes())
        verify_libraries(portable, bundle)
        self.assertEqual(6, len(portable["publications"]))

    def test_rehashed_bundle_rejects_unaudited_host_archive_and_pom(self):
        self.release_recipe()
        bundle = self.fixture.freeze()
        descriptor_path = bundle / "adapter/host.json"
        original = descriptor_path.read_bytes()
        for field in ("archive", "pom"):
            descriptor = json.loads(original)
            data = (bundle / descriptor[field]).read_bytes()
            target = "adapter/" + ("other-runtime.zip" if field == "archive" else "other.pom")
            (bundle / target).write_bytes(data)
            descriptor[field] = target
            descriptor_path.write_bytes(canonical(descriptor))
            manifest = json.loads((bundle / "runtime.json").read_bytes())
            manifest["files"] = [entry for entry in _files(bundle) if entry["path"] not in {"runtime.json", "COMPLETE"}]
            manifest["artifactId"] = digest(canonical({key: value for key, value in manifest.items() if key != "artifactId"}))
            (bundle / "runtime.json").write_bytes(canonical(manifest))
            (bundle / "COMPLETE").write_text(manifest["artifactId"], encoding="utf-8")
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, "実使用Hostと監査済み"):
                verify_bundle(bundle)

    def test_recipe_omission_and_legacy_release_rejected(self):
        self.release_recipe()
        self.fixture.recipe["publicationEvidence"] = []
        with self.assertRaisesRegex(ContractError, "監査入力"):
            self.fixture.freeze()
        self.fixture.recipe["recipeVersion"] = 2
        with self.assertRaisesRegex(ContractError, "版が一致"):
            self.fixture.freeze()

    def test_prepare_saves_evidence_and_rejects_snapshot_change(self):
        from .test_build_dev_runtime import build_dev_runtime as builder
        self.release_recipe()
        recipe = self.root / "recipe.json"
        recipe.write_bytes(canonical(self.fixture.recipe))
        store = self.root / "prepared-store"
        store.mkdir()
        build_id = "a" * 32
        state = dict(preparedVersion=1, state="PREPARED", buildId=build_id, taskId="task", owner="owner",
                     store=str(store), sourceLock=str(self.fixture.lock), sourceLockSha256=digest(self.fixture.lock.read_bytes()),
                     recipe=str(recipe), recipeSha256=digest(recipe.read_bytes()), dataModelCoordinate=self.selected["dataModelCoordinate"])
        saved_path = builder.save_prepared(store, build_id, state)
        builder.load_prepared(store, build_id, "task", "owner")
        saved = json.loads(saved_path.read_bytes())["savedFiles"]
        evidence = next(entry for entry in saved if entry["source"] == self.lock["publicationEvidence"][0]["path"])
        Path(evidence["path"]).write_bytes(b"changed snapshot")
        with self.assertRaisesRegex(ContractError, "snapshot"):
            builder.load_prepared(store, build_id, "task", "owner")
