"""合成Host配布の固定と差替え拒否。実JVM・Minecraft・管理rootは起動しない。"""
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from .build_inputs import canonical, digest
from .contracts import ContractError
from .database_adapter import DESCRIPTOR, HOST_MAIN, host_adapter_command, verify_host_adapter
from .runtime_bundle import _files, verify_bundle
from . import test_runtime_bundle as bundle_tests


class DatabaseAdapterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = bundle_tests.RuntimeBundleTests("test_launch_has_no_source_cache_or_gradle_dependency")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def relock(self):
        value = json.loads(self.fixture.lock.read_bytes())
        for item in value["publications"]:
            item["sha256"] = digest(Path(item["path"]).read_bytes())
        self.fixture.lock.write_bytes(canonical(value))

    def rewrite_archive(self, change):
        with zipfile.ZipFile(self.fixture.host_zip) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        change(entries)
        with zipfile.ZipFile(self.fixture.host_zip, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        self.relock()

    def relabel_bundle(self, bundle):
        manifest = json.loads((bundle / "runtime.json").read_bytes())
        manifest["files"] = [item for item in _files(bundle) if item["path"] not in {"runtime.json", "COMPLETE"}]
        manifest["artifactId"] = digest(canonical({key: value for key, value in manifest.items() if key != "artifactId"}))
        (bundle / "runtime.json").write_bytes(canonical(manifest))
        (bundle / "COMPLETE").write_text(manifest["artifactId"])

    def test_fixed_adapter_uses_only_bundle_jdk_and_copied_runtime(self):
        bundle = self.fixture.freeze()
        descriptor = verify_host_adapter(bundle)
        self.assertEqual(self.fixture.host_coordinate, descriptor["coordinate"])
        self.fixture.sources.rename(self.fixture.root / "preserved-sources")
        (self.fixture.root / "mutable-cache").rename(self.fixture.root / "preserved-cache")
        command = host_adapter_command(bundle)
        self.assertEqual(HOST_MAIN, command[-1])
        self.assertEqual(descriptor["dataModelJar"], descriptor["classpath"][0])
        self.assertTrue(all(str(self.fixture.root / "mutable-cache") not in part for part in command))
        self.assertIn(str(bundle / "jdk/bin"), command[0])
        self.assertEqual(descriptor, verify_host_adapter(bundle))

    def test_missing_host_publication_is_not_replaced_by_mod_jar(self):
        value = json.loads(self.fixture.lock.read_bytes())
        value["publications"] = []
        self.fixture.lock.write_bytes(canonical(value))
        with self.assertRaises(ContractError):
            self.fixture.freeze()

    def test_changed_zip_after_publication_lock_is_rejected(self):
        with self.fixture.host_zip.open("ab") as output:
            output.write(b"changed")
        with self.assertRaises(ContractError):
            self.fixture.freeze()

    def test_other_host_version_is_rejected(self):
        value = json.loads(self.fixture.lock.read_bytes())
        for item in value["publications"]:
            item["coordinate"] += ".other"
        self.fixture.lock.write_bytes(canonical(value))
        with self.assertRaises(ContractError):
            self.fixture.freeze()

    def test_wrong_pom_coordinate_is_rejected_even_with_matching_file_hash(self):
        self.fixture.host_pom.write_text(self.fixture.host_pom.read_text().replace("host-adapter-dev", "other-dev"))
        self.relock()
        with self.assertRaises(ContractError):
            self.fixture.freeze()

    def test_zip_path_escape_is_rejected_before_publication(self):
        self.rewrite_archive(lambda entries: entries.update({"../../outside": b"escape"}))
        with self.assertRaises(ContractError):
            self.fixture.freeze()
        self.assertFalse((self.fixture.root / "outside").exists())

    def test_host_data_model_bytes_must_match_mod_input(self):
        self.rewrite_archive(lambda entries: entries.update({"lib/datamodel.jar": b"other DataModel"}))
        with self.assertRaises(ContractError):
            self.fixture.freeze()

    def test_descriptor_cannot_select_unknown_jar_after_manifest_relabel(self):
        bundle = self.fixture.freeze()
        descriptor = json.loads((bundle / DESCRIPTOR).read_bytes())
        descriptor["classpath"].append("files/unknown.jar")
        (bundle / DESCRIPTOR).write_bytes(canonical(descriptor))
        self.relabel_bundle(bundle)
        with self.assertRaises(ContractError):
            verify_bundle(bundle)

    def test_different_os_does_not_execute_adapter(self):
        bundle = self.fixture.freeze()
        with patch("dev_server.database_adapter.platform.system", return_value="other-os"):
            with self.assertRaises(ContractError):
                host_adapter_command(bundle)

    def test_extracted_jar_change_is_rejected(self):
        bundle = self.fixture.freeze()
        descriptor = verify_host_adapter(bundle)
        (bundle / descriptor["dataModelJar"]).write_bytes(b"replaced")
        with self.assertRaises(ContractError):
            host_adapter_command(bundle)

    def test_classpath_separator_in_library_name_is_rejected(self):
        self.rewrite_archive(lambda entries: entries.update({"lib/other;outside.jar": entries["lib/kotlin.jar"]}))
        with self.assertRaisesRegex(ContractError, "classpath"):
            self.fixture.freeze()

    def test_manifest_extra_classpath_is_rejected_before_publication(self):
        for header in (b"Class-Path: ../../outside.jar", b"class-path: file:/outside.jar",
                       b"CLASS-PATH: https://example.invalid/outside.jar", b"Class-Path: \r\n outside.jar"):
            with self.subTest(header=header):
                library = io.BytesIO()
                with zipfile.ZipFile(library, "w") as jar:
                    jar.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\r\n" + header + b"\r\n\r\n")
                self.rewrite_archive(lambda entries: entries.update({"lib/kotlin.jar": library.getvalue()}))
                with self.assertRaisesRegex(ContractError, "classpath"):
                    self.fixture.freeze()

    def test_manifest_extra_classpath_is_rejected_before_command_even_when_relabelled(self):
        bundle = self.fixture.freeze()
        descriptor = json.loads((bundle / DESCRIPTOR).read_bytes())
        archive_path = bundle / descriptor["archive"]
        with zipfile.ZipFile(archive_path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        library = io.BytesIO()
        with zipfile.ZipFile(library, "w") as jar:
            jar.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\r\nClass-Path: ../../outside.jar\r\n\r\n")
        entries["lib/kotlin.jar"] = library.getvalue()
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        (bundle / "adapter/host/lib/kotlin.jar").write_bytes(library.getvalue())
        descriptor["files"] = [{"path": "adapter/host/" + name, "kind": "regular", "size": len(data),
                                "sha256": digest(data)} for name, data in sorted(entries.items())]
        (bundle / DESCRIPTOR).write_bytes(canonical(descriptor))
        self.relabel_bundle(bundle)
        with self.assertRaisesRegex(ContractError, "classpath"):
            host_adapter_command(bundle)

    def reject_parent_separator(self, separator):
        bundle = self.fixture.freeze()
        parent = self.fixture.root / ("artifact" + separator + "other")
        parent.mkdir()
        target = parent / bundle.name
        bundle.rename(target)
        with self.assertRaisesRegex(ContractError, "classpath区切り文字"):
            host_adapter_command(target)

    @unittest.skipUnless(os.name == "nt", "Windowsのsemicolon親pathを検証します")
    def test_windows_parent_classpath_separator_is_rejected(self):
        self.reject_parent_separator(";")

    @unittest.skipUnless(os.name != "nt", "POSIXのcolon親pathを検証します")
    def test_posix_parent_classpath_separator_is_rejected(self):
        self.reject_parent_separator(":")
