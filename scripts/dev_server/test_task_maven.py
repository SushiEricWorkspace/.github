"""一時FSと合成bundleでMavenの記録・明示回収を検証する。実データは回収しない。"""

import ctypes
from ctypes import wintypes
import json
import itertools
import stat
import struct
from types import SimpleNamespace
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dev_server.build_inputs import canonical, digest, portable_lock
from dev_server.contracts import ContractError, LAYOUT_ID
from dev_server.runtime_bundle import _files, verify_bundle
from dev_server.task_maven import TaskMaven, write_receipt
from dev_server import task_maven_delete
from dev_server.task_worktrees import TaskError
from dev_server.build_lock import build_lock


def synthetic_bundle(store: Path, lock: dict) -> Path:
    """manifestとpublicationコピーを持つ小さい完成bundle。JVM・Gradleは実行しない。"""
    staging = store / "staging"
    staging.mkdir(parents=True)
    copied = []
    for item in lock["publications"]:
        original = Path(item["path"])
        relative = f"files/{item['sha256']}/{original.name}"
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original.read_bytes())
        copied.append({"coordinate": item["coordinate"], "sha256": item["sha256"], "file": relative})
    (staging / "inputs").mkdir()
    (staging / "inputs/publications.json").write_bytes(canonical(copied))
    (staging / "inputs/source-lock.json").write_bytes(canonical(portable_lock(lock)))
    manifest = {"schemaVersion": 2, "artifactId": "0" * 64, "layoutId": LAYOUT_ID,
                "development": True, "javaVersion": "21.0.9", "dataModelCoordinate": lock["publications"][0]["coordinate"],
                "files": _files(staging), "sources": [s["record"] for s in lock["sources"]],
                "tools": [{"name": name, "version": "1", "sha256": digest(b"fixture")}
                          for name in ("jdk", "gradle", "loom", "minecraft", "yarn", "fabric-loader", "fabric-api", "kotlin")],
                "launch": {"mainClass": "fixture.Main", "classpath": [copied[0]["file"]], "jvmArgs": []}}
    manifest["artifactId"] = digest(canonical({k: v for k, v in manifest.items() if k != "artifactId"}))
    (staging / "runtime.json").write_bytes(canonical(manifest))
    (staging / "COMPLETE").write_text(manifest["artifactId"])
    bundle = store / manifest["artifactId"]
    staging.rename(bundle)
    verify_bundle(bundle)
    return bundle


class TaskMavenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.manager = TaskMaven(self.workspace)
        with self.manager.registry.database() as db:
            db.execute("INSERT INTO tasks VALUES ('task-a',43,'publisher','REMOVED')")
            db.execute("INSERT INTO tasks VALUES ('task-b',44,'other','REMOVED')")
        self.store = self.root / "artifacts"
        self.build_id = "a" * 32

    def completed(self, build_id=None, host_zip=False):
        build_id = build_id or self.build_id
        maven, path = self.manager.paths("task-a", build_id)
        lock_path = self.root / f"source-lock-{build_id}.json"
        record = self.manager.start("task-a", build_id, "builder", lock_path)
        maven.mkdir(parents=True)
        publications = []
        for suffix in ("jar", "pom"):
            target = maven / "group/module/version" / f"data_model-{build_id}.{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"{build_id}-{suffix}".encode())
            publications.append({"coordinate": "io.github.sushiericworkspace:sushieric-datamodel-mod-dev:0.1.0-dev.20261010123456789.12345678-1234-1234-1234-123456789abc",
                                 "path": str(target), "sha256": digest(target.read_bytes())})
        if host_zip:
            coordinate = publications[0]["coordinate"].replace("sushieric-datamodel-mod-dev", "sushieric-datamodel-host-adapter-dev")
            version = coordinate.split(":")[2]
            target = maven / "host" / f"sushieric-datamodel-host-adapter-dev-{version}-runtime.zip"
            target.parent.mkdir()
            target.write_bytes(b"synthetic fixed Host ZIP")
            publications.append({"coordinate": coordinate, "path": str(target), "sha256": digest(target.read_bytes())})
        for name in ("module.claim", "maven-metadata.xml", "module.sha512", "module.module"):
            (maven / name).write_text("generated")
        (maven / "empty").mkdir()
        lock = {"sourceLockVersion": 1, "sources": [{"root": str(self.root / "old-source" / name),
                "record": {"repo": name, "commit": "0" * 40, "dirtyPatchSha256": digest(b""),
                           "untrackedInputs": []}, "inputs": [], "patchBase64": ""}
                for name in ("SushiEricDataModel", "SushiEricServerMod")], "publications": publications}
        lock_path.write_bytes(canonical(lock))
        bundle = synthetic_bundle(self.store, lock)
        write_receipt(lock_path, bundle)
        completed = self.manager.succeed(record, lock)
        return completed, maven, path, bundle, lock

    def candidate(self, build_id=None):
        build_id = build_id or self.build_id
        return next(row for row in self.manager.dry_run("task-a", "publisher") if row["buildId"] == build_id)

    def test_fixed_host_zip_is_kept_in_publication_copy_and_inventory(self):
        _, maven, _, bundle, lock = self.completed(host_zip=True)
        self.assertTrue(self.candidate()["eligible"])
        copies = json.loads((bundle / "inputs/publications.json").read_bytes())
        host = [item for item in copies if "sushieric-datamodel-host-adapter-dev" in item["coordinate"]]
        self.assertEqual(1, len(host))
        self.assertEqual(lock["publications"][-1]["sha256"], host[0]["sha256"])
        self.assertTrue(maven.exists())

    def test_only_selected_success_uuid_is_collected_after_handoff(self):
        first, maven, path, bundle, _ = self.completed()
        _, other_maven, _, other_bundle, _ = self.completed("b" * 32)
        protected = []
        for name in ("run", "snapshots", "registry-backup"):
            target = self.root / name
            target.write_bytes(b"protected")
            protected.append(target)
        bundle_before = _files(bundle)
        row = self.candidate()
        self.assertTrue(row["eligible"])
        self.assertTrue(maven.exists())
        self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
        self.assertFalse(maven.exists())
        self.assertTrue(other_maven.exists())
        self.assertTrue((maven.parent.parent / ".build.lock").exists())
        self.assertTrue(self.manager.registry.root.exists())
        self.assertEqual("COLLECTED", json.loads(path.read_bytes())["state"])
        self.assertEqual(bundle_before, _files(bundle))
        verify_bundle(bundle)
        verify_bundle(other_bundle)
        self.assertTrue(all(p.read_bytes() == b"protected" for p in protected))
        self.assertEqual("builder", first["owner"])

    def test_owner_state_other_task_unknown_uuid_and_hash_are_rejected(self):
        _, maven, _, _, _ = self.completed()
        row = self.candidate()
        for args in (("task-a", "builder", self.build_id, row["candidateHash"]),
                     ("task-b", "other", self.build_id, row["candidateHash"]),
                     ("task-a", "publisher", "c" * 32, row["candidateHash"]),
                     ("task-a", "publisher", self.build_id, "0" * 64)):
            with self.subTest(args=args), self.assertRaises((ContractError, TaskError, OSError)):
                self.manager.collect(*args)
            self.assertTrue(maven.exists())
        with self.manager.registry.database() as db:
            db.execute("UPDATE tasks SET state='ACTIVE' WHERE task_id='task-a'")
        self.assertFalse(self.candidate()["eligible"])
        with self.assertRaises(TaskError):
            self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])

    def test_failed_started_and_legacy_outputs_are_kept(self):
        for build_id, state in (("a" * 32, "STARTED"), ("b" * 32, "FAILED"), ("c" * 32, "legacy")):
            maven, _ = self.manager.paths("task-a", build_id)
            if state != "legacy":
                record = self.manager.start("task-a", build_id, "builder", self.root / "lock.json")
                if state == "FAILED":
                    self.manager.fail(record, RuntimeError("fixture failure"))
            maven.mkdir(parents=True)
            (maven / "keep").write_text(state)
        rows = self.manager.dry_run("task-a", "publisher")
        self.assertEqual(3, len(rows))
        self.assertTrue(all(not row["eligible"] and row["reason"] for row in rows))
        for row in rows:
            with self.assertRaises((ContractError, OSError)):
                self.manager.collect("task-a", "publisher", row["buildId"], "0" * 64)

    def test_bundle_missing_tampered_and_unknown_files_are_kept(self):
        _, maven, _, bundle, _ = self.completed()
        row = self.candidate()
        (maven / "unknown").write_text("preserve")
        self.assertFalse(self.candidate()["eligible"])
        with self.assertRaisesRegex(ContractError, "未知"):
            self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
        (maven / "unknown").unlink()
        manifest = (bundle / "runtime.json").read_bytes()
        (bundle / "runtime.json").write_bytes(b"{}")
        self.assertFalse(self.candidate()["eligible"])
        (bundle / "runtime.json").write_bytes(manifest)
        missing = bundle.with_name("missing")
        bundle.rename(missing)
        self.assertFalse(self.candidate()["eligible"])
        self.assertTrue(maven.exists())

    def test_publication_change_and_record_change_reject_old_candidate(self):
        _, maven, path, _, _ = self.completed()
        row = self.candidate()
        original = path.read_bytes()
        record = json.loads(original)
        record["owner"] = "changed-audit-owner"
        path.write_bytes(canonical(record))
        with self.assertRaisesRegex(ContractError, "候補hash"):
            self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
        path.write_bytes(original)
        jar = next(maven.rglob("*.jar"))
        jar.write_bytes(b"changed")
        self.assertFalse(self.candidate()["eligible"])
        self.assertTrue(jar.exists())

    def test_bundle_inside_candidate_with_lexical_parent_is_rejected(self):
        record, maven, path, bundle, _ = self.completed()
        inside = maven / "artifacts" / bundle.name
        inside.parent.mkdir()
        shutil.copytree(bundle, inside)
        (maven / "alias").mkdir()
        record["bundle"] = str(maven / "alias" / ".." / "artifacts" / bundle.name)
        path.write_bytes(canonical(record))
        manager = TaskMaven(self.workspace / ".." / "workspace")
        rows = manager.dry_run("task-a", "publisher")
        self.assertIn("包含", rows[0]["reason"])
        self.assertTrue(inside.exists())

    def test_build_lock_and_last_moment_owner_record_and_files_are_rechecked(self):
        _, maven, path, _, _ = self.completed()
        row = self.candidate()
        with build_lock(maven.parent.parent / ".build.lock"):
            with self.assertRaises(TaskError):
                self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
        original_delete = self.manager._delete
        for kind in ("owner", "record", "file"):
            with self.subTest(kind=kind):
                before = path.read_bytes()
                def changed(*args):
                    if kind == "owner":
                        with self.manager.registry.database() as db:
                            db.execute("UPDATE tasks SET owner='changed' WHERE task_id='task-a'")
                    elif kind == "record":
                        data = json.loads(path.read_bytes())
                        data["owner"] = "changed"
                        path.write_bytes(canonical(data))
                    else:
                        (maven / "unknown").write_text("preserve")
                    original_delete(*args)
                with patch.object(self.manager, "_delete", side_effect=changed):
                    with self.assertRaises((ContractError, TaskError)):
                        self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
                self.assertTrue(next(maven.rglob("*.jar")).exists())
                self.assertEqual("COLLECTING", json.loads(path.read_bytes())["state"])
                path.write_bytes(before)
                with self.manager.registry.database() as db:
                    db.execute("UPDATE tasks SET owner='publisher' WHERE task_id='task-a'")
                if (maven / "unknown").exists():
                    (maven / "unknown").unlink()

    def test_link_is_rejected_without_touching_target(self):
        _, maven, _, _, _ = self.completed()
        outside = self.root / "outside"
        outside.write_text("preserve")
        link = maven / "link"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlink作成権限なし: {error}")
        self.assertFalse(self.candidate()["eligible"])
        self.assertEqual("preserve", outside.read_text())

    def test_receipt_mismatch_and_existing_receipt_are_rejected(self):
        record, maven, _, bundle, lock = self.completed()
        lock_path = Path(record["sourceLock"])
        with self.assertRaises(FileExistsError):
            write_receipt(lock_path, bundle)
        data = json.loads(lock_path.with_suffix(".receipt.json").read_bytes())
        data["sourceLockSha256"] = "0" * 64
        lock_path.with_suffix(".receipt.json").write_bytes(canonical(data))
        with self.assertRaisesRegex(ContractError, "receipt"):
            self.manager.succeed(dict(record, state="STARTED"), lock)
        self.assertTrue(maven.exists())

    @unittest.skipUnless(os.name == "nt", "Windows handleの実FS競合試験")
    def test_parent_swap_before_open_and_after_handle_hash_keeps_outside_jar(self):
        _, maven, _, _, _ = self.completed()
        outside = self.root / "outside"
        shutil.copytree(maven / "group", outside)
        outside_before = _files(outside)
        for phase in ("before-open", "after-hash"):
            with self.subTest(phase=phase):
                row = self.candidate()
                original_record = (self.manager.records / "task-a" / f"{self.build_id}.json").read_bytes()
                def swap():
                    (maven / "group").rename(maven / "original-group")
                    subprocess.run(["cmd", "/c", "mklink", "/J", str(maven / "group"), str(outside)],
                                   check=True, capture_output=True)
                original_open = task_maven_delete._WindowsDelete.open
                original_hash = task_maven_delete._WindowsDelete.hash_file
                def changed_open(api, path, **kwargs):
                    if not kwargs["directory"] and path.suffix == ".jar":
                        swap()
                    return original_open(api, path, **kwargs)
                def changed_hash(api, handle):
                    value = original_hash(api, handle)
                    swap()
                    return value
                method = "open" if phase == "before-open" else "hash_file"
                changed = changed_open if phase == "before-open" else changed_hash
                with patch.object(task_maven_delete._WindowsDelete, method, new=changed):
                    with self.assertRaises(OSError):
                        self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
                self.assertEqual(outside_before, _files(outside))
                self.assertTrue(next(maven.rglob("*.jar")).exists())
                (self.manager.records / "task-a" / f"{self.build_id}.json").write_bytes(original_record)

    @unittest.skipUnless(os.name == "nt", "Windows handleの普通file差替え試験")
    def test_ordinary_file_swap_before_open_is_kept_and_after_hash_is_blocked(self):
        _, maven, path, _, _ = self.completed()
        row = self.candidate()
        jar = next(maven.rglob("*.jar"))
        original = jar.read_bytes()
        original_record = path.read_bytes()
        original_open = task_maven_delete._WindowsDelete.open
        def changed_open(api, target, **kwargs):
            if not kwargs["directory"] and target == jar:
                target.rename(target.with_suffix(".saved"))
                target.write_bytes(b"unknown replacement")
            return original_open(api, target, **kwargs)
        with patch.object(task_maven_delete._WindowsDelete, "open", new=changed_open):
            with self.assertRaisesRegex(ContractError, "hash"):
                self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
        self.assertEqual(b"unknown replacement", jar.read_bytes())
        self.assertEqual(original, jar.with_suffix(".saved").read_bytes())
        jar.unlink()
        jar.with_suffix(".saved").rename(jar)
        path.write_bytes(original_record)
        original_hash = task_maven_delete._WindowsDelete.hash_file
        def changed_hash(api, handle):
            value = original_hash(api, handle)
            jar.rename(jar.with_suffix(".saved"))
            return value
        with patch.object(task_maven_delete._WindowsDelete, "hash_file", new=changed_hash):
            with self.assertRaises(OSError):
                self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
        self.assertEqual(original, jar.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows inplace junctionの実FS試験")
    def test_inplace_junction_after_hash_is_rejected_before_disposition(self):
        _, maven, _, _, _ = self.completed()
        row = self.candidate()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.jar").write_bytes(b"outside")
        original_hash = task_maven_delete._WindowsDelete.hash_file
        attached = False
        def changed_hash(api, handle):
            nonlocal attached
            value = original_hash(api, handle)
            if not attached:
                attached = True
                target = maven / "empty"
                h = api.api.CreateFileW(str(target), 0x100, 7, None, 3, 0x2200000, None)
                if h == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                substitute = ("\\??\\" + str(outside)).encode("utf-16-le")
                printed = str(outside).encode("utf-16-le")
                names = substitute + b"\0\0" + printed + b"\0\0"
                data = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printed)) + names
                raw = struct.pack("<IHH", 0xA0000003, len(data), 0) + data
                buffer = ctypes.create_string_buffer(raw)
                returned = wintypes.DWORD()
                api.api.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                    wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
                api.api.DeviceIoControl.restype = wintypes.BOOL
                try:
                    if not api.api.DeviceIoControl(h, 0x900A4, buffer, len(raw), None, 0, ctypes.byref(returned), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    api.close(h)
            return value
        with patch.object(task_maven_delete._WindowsDelete, "hash_file", new=changed_hash):
            with self.assertRaisesRegex(ContractError, "リンク/種別"):
                self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
        self.assertTrue(attached)
        self.assertEqual(b"outside", (outside / "keep.jar").read_bytes())
        self.assertTrue(next((maven / "group").rglob("*.jar")).exists())

    def test_posix_replaced_inode_is_quarantined_without_unlink_mock(self):
        root = self.root / "maven"
        inventory = {"files": [{"path": "file.jar", "sha256": "a" * 64}], "directories": []}
        counter = itertools.count(10)
        fake_file = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=10)
        replaced = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=11)
        with patch.object(task_maven_delete.os, "O_DIRECTORY", 0x10000, create=True), \
                patch.object(task_maven_delete.os, "O_NOFOLLOW", 0x20000, create=True), \
                patch.object(task_maven_delete.os, "O_NONBLOCK", 0x40000, create=True), \
                patch.object(task_maven_delete.os, "open", side_effect=lambda *a, **k: next(counter)) as opened, \
                patch.object(task_maven_delete.os, "close"), patch.object(task_maven_delete.os, "mkdir"), \
                patch.object(task_maven_delete.os, "fchmod", create=True), patch.object(task_maven_delete.os, "fstat", return_value=fake_file), \
                patch.object(task_maven_delete.os, "stat", return_value=replaced), \
                patch.object(task_maven_delete.os, "rename") as moved, \
                patch.object(task_maven_delete.os, "unlink") as removed, \
                patch.object(task_maven_delete, "_hash_fd", return_value="a" * 64):
            with self.assertRaisesRegex(ContractError, "保全"):
                task_maven_delete._posix(root, inventory)
            self.assertTrue(opened.call_args_list[-1].args[1] & 0x40000)
            self.assertIn("src_dir_fd", moved.call_args.kwargs)
            self.assertIn("dst_dir_fd", moved.call_args.kwargs)
            removed.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows junctionの実FS試験")
    def test_junction_added_just_before_delete_is_preserved(self):
        _, maven, _, _, _ = self.completed()
        row = self.candidate()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_bytes(b"preserve")
        original = self.manager._delete
        def changed(*args):
            subprocess.run(["cmd", "/c", "mklink", "/J", str(maven / "junction"), str(outside)],
                           check=True, capture_output=True)
            original(*args)
        with patch.object(self.manager, "_delete", side_effect=changed):
            with self.assertRaisesRegex(ContractError, "通常ディレクトリ"):
                self.manager.collect("task-a", "publisher", self.build_id, row["candidateHash"])
        self.assertEqual(b"preserve", (outside / "keep").read_bytes())
        self.assertTrue(next(maven.rglob("*.jar")).exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOはWindowsで未対応")
    def test_special_file_is_rejected(self):
        _, maven, _, _, _ = self.completed()
        os.mkfifo(maven / "special")
        self.assertFalse(self.candidate()["eligible"])

    def test_exporter_entrypoint_writes_receipt_only_after_freeze_success(self):
        from dev_server import test_runtime_bundle as fixtures
        fixture = fixtures.RuntimeBundleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.recipe_path.write_bytes(canonical(fixture.recipe))
        script = Path(__file__).resolve().parents[1] / "export-dev-runtime.py"
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        arguments = [sys.executable, "-B", str(script), "--recipe", str(fixture.recipe_path),
                     "--source-lock", str(fixture.lock), "--store", str(fixture.store)]
        result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertEqual(0, result.returncode, result.stderr)
        output = json.loads(result.stdout)
        receipt_path = fixture.lock.with_suffix(".receipt.json")
        receipt = json.loads(receipt_path.read_bytes())
        self.assertEqual(output["artifactId"], receipt["artifactId"])
        verify_bundle(Path(output["bundle"]))
        first = receipt_path.read_bytes()
        result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(first, receipt_path.read_bytes())

    def test_malformed_receipt_does_not_prevent_failure_record(self):
        record = self.manager.start("task-a", self.build_id, "builder", self.root / "lock.json")
        Path(record["sourceLock"]).with_suffix(".receipt.json").write_bytes(b"invalid")
        self.manager.fail(record, RuntimeError("failed export"))
        _, path = self.manager.paths("task-a", self.build_id)
        failed = json.loads(path.read_bytes())
        self.assertEqual("FAILED", failed["state"])
        self.assertIn("receiptError", failed)

    def test_cli_requires_exact_request_and_collects_only_requested_uuid(self):
        _, maven, _, bundle, _ = self.completed()
        script = Path(__file__).resolve().parents[1] / "collect-task-maven.py"
        prefix = [sys.executable, "-B", str(script), "--workspace-root", str(self.workspace),
                  "--task-id", "task-a", "--owner", "publisher"]
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run([*prefix, "--dry-run"], capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertEqual(0, result.returncode, result.stderr)
        fingerprint = json.loads(result.stdout)[0]["candidateHash"]
        result = subprocess.run([*prefix, "--build-id", self.build_id], capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(maven.exists())
        result = subprocess.run([*prefix, "--build-id", self.build_id, "--candidate-hash", fingerprint],
                                capture_output=True, text=True, encoding="utf-8", env=environment)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(maven.exists())
        verify_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
