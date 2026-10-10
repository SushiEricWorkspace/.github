"""合成fixtureに対する契約試験。A08等の実サーバー受入試験とは区別する。"""

from copy import deepcopy
import unittest

from dev_server.contracts import (CONTRACT, ContractError, require_compatible, validate,
                                  validate_portable_settings, validate_relative_path, verify_file_set)
from dev_server.fixtures import FIXTURES, contract_documents, file_entries, settings_expectations


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.documents, self.payload = contract_documents("save-roundtrip-v1")

    def test_all_contracts_have_valid_fixture(self):
        self.assertEqual(1, CONTRACT["contractFormat"])
        for fixture in FIXTURES:
            documents, payload = contract_documents(fixture["name"])
            self.assertEqual(set(CONTRACT["contracts"]), set(documents))
            for kind, value in documents.items():
                with self.subTest(fixture=fixture["name"], kind=kind):
                    validate(kind, value)
            verify_file_set(documents["manifest"]["files"], payload)
            require_compatible(documents["manifest"], documents["runtime"])

    def test_fixture_data_is_independent(self):
        self.documents["fixture"]["expected"]["money"] = 99
        documents, _ = contract_documents("save-roundtrip-v1")
        self.assertEqual(1234, documents["fixture"]["expected"]["money"])

    def test_missing_required_init_inputs(self):
        for key in ("operator", "managementRoot", "quotaBytes", "reserveBytes", "backup", "instances"):
            value = deepcopy(self.documents["inventory"])
            del value[key]
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate("inventory", value)

    def test_unknown_fields_and_versions(self):
        for kind, value in self.documents.items():
            for key, invalid in (("schemaVersion", 2), ("unclassified", "hidden-secret")):
                with self.subTest(kind=kind, key=key), self.assertRaises(ContractError):
                    validate(kind, dict(deepcopy(value), **{key: invalid}))

    def test_integer_is_not_boolean(self):
        value = deepcopy(self.documents["inventory"])
        value["quotaBytes"] = True
        with self.assertRaises(ContractError):
            validate("inventory", value)

    def test_capacity_and_backup(self):
        for reserve in (0, 100000, 100001):
            value = deepcopy(self.documents["inventory"])
            value["reserveBytes"] = reserve
            with self.subTest(reserve=reserve), self.assertRaises(ContractError):
                validate("inventory", value)
        for destination in ("relative", "/", "/synthetic/minecraft-dev/backups", "/synthetic"):
            value = deepcopy(self.documents["inventory"])
            value["backup"]["destination"] = destination
            with self.subTest(destination=destination), self.assertRaises(ContractError):
                validate("inventory", value)
        value = deepcopy(self.documents["inventory"])
        value["backup"]["separateMediumConfirmedBy"] = ""
        with self.assertRaises(ContractError):
            validate("inventory", value)

    def test_duplicate_fixed_ports_and_ids(self):
        for key in ("ports", "instanceId"):
            value = deepcopy(self.documents["inventory"])
            value["instances"][1][key] = deepcopy(value["instances"][0][key])
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate("inventory", value)

    def test_listener_constraints(self):
        for key, invalid in (("managementBind", "0.0.0.0"), ("queryEnabled", True), ("jdwpEnabled", True)):
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate("instance", dict(self.documents["instance"], **{key: invalid}))
        value = deepcopy(self.documents["inventory"])
        value["networkTestPortRange"] = [25799, 25700]
        with self.assertRaises(ContractError):
            validate("inventory", value)

    def test_windows_absolute_paths_and_backup_overlap(self):
        value = deepcopy(self.documents["inventory"])
        value["managementRoot"] = "C:/minecraft-dev"
        value["backup"]["destination"] = "D:/backup"
        validate("inventory", value)
        value["backup"]["destination"] = "c:\\MINECRAFT-DEV\\backup"
        with self.assertRaises(ContractError):
            validate("inventory", value)

    def test_unsafe_absolute_paths(self):
        from dev_server.contracts import absolute_path
        for path in ("C:relative", "C:/", "C:/data/../outside", "C:/data/CON.txt", "C:/data/LPT¹",
                     "C:/data/file.", "C:/data/file ", "C:/data/file:stream", "C:/data/*",
                     "//server/share/data", "\\\\server\\share\\data", "/", "/data/../outside"):
            with self.subTest(path=path), self.assertRaises(ContractError):
                absolute_path(path)

    def test_macos_backup_case_and_unicode_overlap_rejected(self):
        for root, backup in (("/synthetic/Root", "/synthetic/root/backup"),
                             ("/synthetic/é", "/synthetic/e\u0301/backup")):
            value = deepcopy(self.documents["inventory"])
            value["managementRoot"] = root
            value["backup"]["destination"] = backup
            with self.subTest(root=root), self.assertRaises(ContractError):
                validate("inventory", value)

    def test_a08_same_uuid_and_generation(self):
        fixture = deepcopy(self.documents["fixture"])
        fixture["expected"]["modProfileFile"] = "config/SushiEricServerMod/player_data/other/profile.yml"
        with self.assertRaises(ContractError):
            validate("fixture", fixture)
        manifest = deepcopy(self.documents["manifest"])
        manifest["cleanStop"]["generation"] = "old-generation"
        with self.assertRaises(ContractError):
            validate("manifest", manifest)

    def test_a08_world_and_mod_cannot_be_split(self):
        for root in ("world", "config"):
            manifest = deepcopy(self.documents["manifest"])
            manifest["files"] = [file for file in manifest["files"] if not file["path"].startswith(root + "/")]
            with self.subTest(root=root), self.assertRaises(ContractError):
                validate("manifest", manifest)

    def test_a09_all_dimensions_and_unknown_regular_files(self):
        expected = self.documents["fixture"]["expected"]
        for path in expected["dimensionMarkers"] + [expected["unknownRegularFile"]]:
            self.assertIn(path, self.payload)
        payload = dict(self.payload, **{"world/dimensions/new/unknown/not-listed-before.bin": b"extra"})
        manifest = dict(deepcopy(self.documents["manifest"]), files=file_entries(payload))
        validate("manifest", manifest)
        verify_file_set(manifest["files"], payload)

    def test_a09_external_root_or_database_rejected(self):
        for key in ("externalRoots", "databaseAdapters"):
            value = dict(deepcopy(self.documents["dataLayout"]), **{key: ["unregistered"]})
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate("dataLayout", value)
        payload = dict(self.payload, **{"external-player-db/state.bin": b"unknown"})
        manifest = dict(deepcopy(self.documents["manifest"]), files=file_entries(payload))
        with self.assertRaises(ContractError):
            validate("manifest", manifest)

    def test_missing_extra_and_modified_files(self):
        missing = dict(self.payload)
        missing.pop(next(iter(missing)))
        extra = dict(self.payload, **{"world/extra.txt": b"extra"})
        modified = dict(self.payload)
        modified[next(iter(modified))] = b"modified"
        for observed in (missing, extra, modified):
            with self.subTest(observed=list(observed)), self.assertRaises(ContractError):
                verify_file_set(self.documents["manifest"]["files"], observed)

    def test_a20_unknown_runtime_layout_or_coordinate(self):
        for key, invalid in (("artifactId", "a" * 64), ("layoutId", "unknown-layout"),
                             ("commonCoordinate", "io.github.sushiericworkspace:sushieric-common-mod-dev:0.1.0-dev.+"),
                             ("javaVersion", "25.0.1"), ("development", False)):
            runtime = dict(deepcopy(self.documents["runtime"]), **{key: invalid})
            with self.subTest(key=key), self.assertRaises(ContractError):
                require_compatible(self.documents["manifest"], runtime)

    def test_runtime_must_record_sources_tools_and_classpath(self):
        for key in ("sources", "tools", "files"):
            runtime = dict(deepcopy(self.documents["runtime"]), **{key: []})
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate("runtime", runtime)
        runtime = deepcopy(self.documents["runtime"])
        runtime["launch"]["classpath"] = ["worktree/build/classes"]
        with self.assertRaises(ContractError):
            validate("runtime", runtime)

    def test_a23_known_portable_settings(self):
        validate_portable_settings({"server.properties": {"level-name": "world", "level-seed": 52452502},
                                    "config/SushiEricServerMod/config.yml": {"mining.ore_revival_second": 12},
                                    "config/SushiEricServerMod/management/config.yml": {}})

    def test_a23_transform_fixture_expectations(self):
        settings = settings_expectations()
        validate_portable_settings(settings["portable"])
        with self.assertRaises(ContractError):
            validate_portable_settings(settings["source"])
        portable = settings["portable"]["server.properties"]
        restored = settings["restored"]["server.properties"]
        ports = self.documents["inventory"]["instances"][1]["ports"]
        self.assertEqual(ports["minecraft"], restored["server-port"])
        self.assertEqual(ports["rcon"], restored["rcon.port"])
        self.assertEqual(ports["management"], settings["restored"]["config/SushiEricServerMod/management/config.yml"]["port"])
        self.assertTrue(all(restored[key] == value for key, value in portable.items()))
        self.assertNotIn("rcon.password", portable)
        self.assertNotEqual(settings["source"]["server.properties"]["rcon.password"], restored["rcon.password"])

    def test_a23_unknown_secrets_ports_and_machine_paths(self):
        for key in ("rcon.password", "server-port", "server-ip", "unknown-secret", "machine-path"):
            with self.subTest(key=key), self.assertRaises(ContractError) as caught:
                validate_portable_settings({"server.properties": {key: "do-not-print-this-secret"}})
            self.assertNotIn("do-not-print-this-secret", str(caught.exception))
        with self.assertRaises(ContractError):
            validate_portable_settings({"config/unknown-mod.yml": {"token": "secret"}})
        with self.assertRaises(ContractError):
            validate_portable_settings({"config/SushiEricServerMod/management/config.yml": {"port": 25680}})

    def test_a24_unsafe_paths(self):
        for path in ("", "/etc/passwd", "../outside", "world/../../outside", "world//file", "world/./file",
                     "C:/outside", "world\\file", "world/file\x00", "world/file\n", "world/"):
            with self.subTest(path=repr(path)), self.assertRaises(ContractError):
                validate_relative_path(path)

    def test_a24_link_and_special_file_metadata(self):
        for kind in ("symlink", "fifo", "socket", "device", "directory"):
            manifest = deepcopy(self.documents["manifest"])
            manifest["files"][0]["kind"] = kind
            with self.subTest(kind=kind), self.assertRaises(ContractError):
                validate("manifest", manifest)

    def test_duplicate_and_parent_file_collision(self):
        for extra in (deepcopy(self.documents["manifest"]["files"][0]), file_entries({"world": b"file"})[0]):
            manifest = deepcopy(self.documents["manifest"])
            manifest["files"].append(extra)
            with self.assertRaises(ContractError):
                validate("manifest", manifest)

    def test_case_and_unicode_path_collisions(self):
        for paths in (("world/A.txt", "world/a.txt"), ("world/é.txt", "world/e\u0301.txt"),
                      ("world/Parent", "world/parent/file.txt")):
            entries = file_entries({path: b"synthetic" for path in paths})
            with self.subTest(paths=paths), self.assertRaises(ContractError):
                verify_file_set(entries, {path: b"synthetic" for path in paths})
        layout = deepcopy(self.documents["dataLayout"])
        layout["roots"]["world"] = "Config"
        with self.assertRaises(ContractError):
            validate("dataLayout", layout)

    def test_runtime_classpath_directory(self):
        runtime = deepcopy(self.documents["runtime"])
        runtime["files"] = file_entries({"classes/example/Test.class": b"synthetic-class"})
        runtime["launch"]["classpath"] = ["classes"]
        validate("runtime", runtime)

    def test_duplicate_source_and_tool_records(self):
        for key in ("sources", "tools"):
            runtime = deepcopy(self.documents["runtime"])
            runtime[key].append(deepcopy(runtime[key][0]))
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate("runtime", runtime)

    def test_excluded_and_missing_account_files(self):
        manifest = deepcopy(self.documents["manifest"])
        manifest["files"].extend(file_entries({"world/session.lock": b"lock"}))
        with self.assertRaises(ContractError):
            validate("manifest", manifest)
        manifest = deepcopy(self.documents["manifest"])
        manifest["files"] = [file for file in manifest["files"] if file["path"] != "ops.json"]
        with self.assertRaises(ContractError):
            validate("manifest", manifest)

    def test_failed_or_incomplete_stop_not_clean(self):
        for key in ("stopRequested", "jvmAndChildrenExited", "externalWritersExited", "backgroundWritersDrained"):
            manifest = deepcopy(self.documents["manifest"])
            manifest["cleanStop"][key] = False
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate("manifest", manifest)
        for key, invalid in (("success", False), ("pendingCount", 1)):
            manifest = deepcopy(self.documents["manifest"])
            manifest["cleanStop"]["saveResults"][0][key] = invalid
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate("manifest", manifest)

    def test_validation_requires_independent_instance_and_nonce(self):
        for key, invalid in (("restoredInstanceId", "persistent-01"), ("nonce", "different-nonce"), ("runId", "old-run")):
            validation = dict(deepcopy(self.documents["validation"]), **{key: invalid})
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate("validation", validation)


if __name__ == "__main__":
    unittest.main()
