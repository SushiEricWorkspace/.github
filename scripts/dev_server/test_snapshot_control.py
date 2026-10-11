"""合成runtimeの正常停止証跡と実FSで、snapshotの隔離・公開失敗を検証する。"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from .contracts import ROOT_FILES
from .local_ipc import make_private
from .registry import RegistryError
from .snapshot_control import verify_snapshot, write
from .snapshot_data import collect, properties, transform
from .supervisor import dispatch, new_request
from . import test_runtime_control as runtime_tests


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSでのprivate root試験")
class SnapshotTests(unittest.TestCase):
    quota_bytes = 10000000
    command = runtime_tests.RuntimeControlTests.command
    acquire = runtime_tests.RuntimeControlTests.acquire
    assert_code = runtime_tests.RuntimeControlTests.assert_code
    prepare = runtime_tests.RuntimeControlTests.prepare
    start = runtime_tests.RuntimeControlTests.start
    stop = runtime_tests.RuntimeControlTests.stop
    rcon_stop = runtime_tests.RuntimeControlTests.rcon_stop
    state = runtime_tests.RuntimeControlTests.state

    def setUp(self):
        runtime_tests.RuntimeControlTests.setUp(self)
        self.generation = self.prepare()
        (self.generation / "mods").mkdir()
        (self.generation / "usercache.json").write_bytes(b"[]\n")
        self.payload = dict(self.token, action="create", snapshotId="snapshot-01")
        self.destination = self.root / "snapshots/snapshot-01"
        self.source_data = {
            "world/level.dat": b"synthetic-level-data-not-NBT",
            "world/DIM-1/marker": b"nether",
            "world/DIM1/marker": b"end",
            "world/dimensions/test/custom/marker": b"custom",
            "world/unknown/opaque.bin": b"unknown-regular-file",
            "world/session.lock": b"excluded",
            "config/SushiEricServerMod/config.yml": b"mining:\n  ore_revival_second: 12\nmoney:\n  history_save_interval_second: 30\n",
            "config/SushiEricServerMod/player_data/test/profile.yml": b"money: 1234\n",
            "logs/latest.log": b"private-diagnostic-secret",
        }
        self.source_data.update({name: b"[]\n" for name in ROOT_FILES - {"server.properties"}})
        for name, content in self.source_data.items():
            target = self.generation / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.start()
        self.stop()

    def create(self, operation="snapshot-create-01", dry=False):
        return self.command("snapshot", self.payload, operation, dry)

    def verify(self):
        return self.command("snapshot", {"action": "verify", "snapshotId": "snapshot-01"}, "snapshot-verify-01")

    def test_all_dimensions_unknown_files_accounts_and_secrets(self):
        original = (self.generation / "server.properties").read_bytes()
        secret = properties(original)["rcon.password"].encode()
        result = self.create()
        manifest = verify_snapshot(self.destination)
        names = {file["path"] for file in manifest["files"]}
        self.assertIn("world/dimensions/test/custom/marker", names)
        self.assertIn("world/unknown/opaque.bin", names)
        self.assertNotIn("world/session.lock", names)
        self.assertNotIn("logs/latest.log", names)
        self.assertTrue(ROOT_FILES <= names)
        for file in manifest["files"]:
            data = (self.destination / "data" / file["path"]).read_bytes()
            self.assertNotIn(secret, data)
            self.assertEqual(1, (self.destination / "data" / file["path"]).stat().st_nlink)
        portable = properties((self.destination / "data/server.properties").read_bytes())
        self.assertEqual("17", portable["level-seed"])
        self.assertEqual("true", portable["online-mode"])
        self.assertFalse({"server-port", "server-ip", "rcon.port", "rcon.password"} & portable.keys())
        self.assertEqual(b"{}\n", (self.destination / "data/config/SushiEricServerMod/management/config.yml").read_bytes())
        self.assertEqual(original, (self.generation / "server.properties").read_bytes())
        self.assertTrue(self.verify()["verified"])
        self.assertTrue(result["created"])
        self.assertTrue(self.create()["replayed"])
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM reservations").fetchone()[0])
        status = self.command("status", {"instanceId": "persistent-01"}, "status-clean")["instances"][0]
        self.assertTrue(status["snapshotClean"])

    def test_copy_is_independent_of_source(self):
        self.create()
        source = self.generation / "world/unknown/opaque.bin"
        source.write_bytes(b"changed-after-snapshot")
        self.assertEqual(b"unknown-regular-file", (self.destination / "data/world/unknown/opaque.bin").read_bytes())
        self.assertTrue(self.verify()["verified"])

    def test_missing_extra_and_modified_files(self):
        for mode in ("missing", "extra", "modified"):
            with self.subTest(mode=mode):
                self.create()
                target = self.destination / "data/world/unknown/opaque.bin"
                original = target.read_bytes()
                extra = self.destination / "data/world/extra"
                if mode == "missing":
                    target.unlink()
                elif mode == "extra":
                    extra.write_bytes(b"extra")
                else:
                    target.write_bytes(b"modified")
                self.assert_code("SNAPSHOT_MISMATCH", self.verify)
                if mode == "extra":
                    extra.unlink()
                else:
                    target.write_bytes(original)
                    make_private(target)

    def test_missing_complete_and_unregistered_directory_rejected(self):
        self.assert_code("NOT_FOUND", self.verify)
        self.create()
        (self.destination / "COMPLETE").unlink()
        self.assert_code("SNAPSHOT_MISMATCH", self.verify)

    def test_preflight_and_dry_run_do_not_write(self):
        before = list((self.root / "snapshots").iterdir())
        self.assertTrue(self.create(dry=True)["dryRun"])
        self.assertEqual(before, list((self.root / "snapshots").iterdir()))
        self.assertFalse((self.root / "manager/operations/snapshot-snapshot-create-01").exists())
        self.assertEqual("LEASED_STOPPED", self.state()["state"])

    def test_operation_conflicts_existing_snapshot_and_expired_lease(self):
        self.assert_code("OPERATION_CONFLICT", lambda: self.create("stop-01"))
        self.create()
        changed = dict(self.payload, snapshotId="snapshot-02")
        self.assert_code("OPERATION_CONFLICT", lambda: self.command("snapshot", changed, "snapshot-create-01"))
        self.assert_code("EXISTING_DATA", lambda: self.create("snapshot-create-02"))
        self.now = 200
        self.assert_code("LEASE_EXPIRED", self.create)

    def test_replay_after_new_run_is_stale(self):
        self.create()
        self.start("start-02")
        self.stop("stop-02")
        self.assert_code("STALE_OPERATION", self.create)
        self.assertFalse(self.command("status", {"instanceId": "persistent-01"}, "status-after-run")["instances"][0]["snapshotClean"])

    def test_source_mutation_during_copy_is_preserved(self):
        def change(path, data):
            write(path, data)
            if path.name == "opaque.bin":
                (self.generation / "world/unknown/opaque.bin").write_bytes(b"unexpected-writer")
        with patch("dev_server.snapshot_control.write", side_effect=change):
            self.assert_code("SOURCE_CHANGED", self.create)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertFalse(self.destination.exists())
        self.assertTrue((self.root / "snapshots/.staging-snapshot-create-01").exists())

    def test_partial_copy_failure_preserves_original_and_reservations(self):
        def fail(path, data):
            if path.name == "opaque.bin":
                raise OSError("synthetic-secret-do-not-print")
            write(path, data)
        with patch("dev_server.snapshot_control.write", side_effect=fail):
            self.assert_code("SNAPSHOT_FAILED", self.create)
        self.assertEqual(b"unknown-regular-file", (self.generation / "world/unknown/opaque.bin").read_bytes())
        self.assertFalse(self.destination.exists())
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(3, connection.execute("SELECT count(*) FROM reservations").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM snapshots").fetchone()[0])

    def test_published_before_registration_failure_is_not_listed(self):
        def fail(path, data):
            if path.name == "published.json":
                raise OSError("injected-after-publish")
            write(path, data)
        with patch("dev_server.snapshot_control.write", side_effect=fail):
            self.assert_code("SNAPSHOT_FAILED", self.create)
        self.assertTrue((self.destination / "COMPLETE").is_file())
        self.assert_code("NOT_FOUND", self.verify)
        self.assert_code("RECOVERY_REQUIRED", self.create)
        self.assertTrue((self.root / "manager/operations/snapshot-snapshot-create-01/prepared.json").is_file())

    def test_fsync_failure_prevents_publish(self):
        with patch("dev_server.snapshot_control.os.fsync", side_effect=OSError("flush-failed")):
            self.assert_code("SNAPSHOT_FAILED", self.create)
        self.assertFalse(self.destination.exists())

    def test_writer_and_runtime_operations_are_fenced_during_copy(self):
        reached, resume = threading.Event(), threading.Event()
        def pause(path, data):
            write(path, data)
            if path.name == "requested.json":
                reached.set()
                self.assertTrue(resume.wait(5))
        with patch("dev_server.snapshot_control.write", side_effect=pause), ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(dispatch, self.registry, new_request("snapshot", self.payload, operation_id="snapshot-create-01"))
            self.assertTrue(reached.wait(5))
            try:
                response = dispatch(self.registry, new_request("start", dict(self.token, timeoutSeconds=2), operation_id="competing-start"))
                self.assertEqual("LOCK_BUSY", response["error"]["code"])
                with self.registry.transaction(read_only=True) as connection:
                    self.assertEqual("SNAPSHOTTING", self.registry._instance(connection, "persistent-01")["state"])
            finally:
                resume.set()
            self.assertTrue(pending.result()["ok"])

    def test_running_unproven_and_unknown_writer_rejected(self):
        for field, value in (("runtime_proof", None), ("state", "RUNNING"), ("children", 1), ("pending", 1)):
            with self.subTest(field=field):
                previous = self.state()[field]
                with self.registry.transaction() as connection:
                    connection.execute(f"UPDATE instances SET {field}=? WHERE id='persistent-01'", (value,))
                self.assert_code("PROOF_REQUIRED", self.create)
                with self.registry.transaction() as connection:
                    connection.execute(f"UPDATE instances SET {field}=? WHERE id='persistent-01'", (previous,))
        with self.registry.transaction() as connection:
            connection.execute("UPDATE instances SET writers=1 WHERE id='persistent-01'")
        self.assert_code("RECOVERY_REQUIRED", self.create)

    def test_nonce_and_proof_file_tampering_rejected(self):
        run = self.registry.runtime._load_run(self.state())
        path = Path(run["proofPath"])
        proof = json.loads(path.read_bytes())
        proof["startNonce"] = "different-nonce"
        path.write_text(json.dumps(proof), encoding="utf-8")
        self.assert_code("PROOF_MISMATCH", self.create)

    def test_reused_pid_and_capacity_failure_are_preflight(self):
        with patch.object(self.adapter, "identity", return_value={"pid": 10001}):
            self.assert_code("PROCESS_MISMATCH", self.create)
        with patch.object(self.registry.runtime, "_space", side_effect=RegistryError("NO_SPACE", "空きなし")):
            self.assert_code("NO_SPACE", self.create)
        self.assertEqual("LEASED_STOPPED", self.state()["state"])

    def test_unknown_config_db_external_root_and_properties_rejected(self):
        for name in ("config/other-mod/config.yml", "config/SushiEricServerMod/secrets.yml", "world/external.sqlite", "external/marker"):
            with self.subTest(name=name):
                path = self.generation / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"unknown")
                with self.assertRaises(RegistryError):
                    self.create()
                path.unlink()
                if name.startswith("external/"):
                    path.parent.rmdir()
        for data in (b"level-name=../outside\n", b"level-name=world\nsecret=hidden\n", b"level-name=world\nenable-query=true\n"):
            with self.assertRaises((RegistryError, ValueError)):
                properties(data) if b"../" not in data else transform("server.properties", data)

    def test_hardlink_is_not_shared(self):
        source = self.generation / "world/unknown/opaque.bin"
        link = self.generation / "world/linked"
        os.link(source, link)
        self.assert_code("UNSUPPORTED_FILE", self.create)
        link.unlink()

    def test_symlink_is_not_followed(self):
        source = self.generation / "world/unknown/opaque.bin"
        link = self.generation / "world/linked"
        try:
            link.symlink_to(source)
        except OSError:
            self.skipTest("symlink作成権限がありません")
        with self.assertRaises(OSError):
            collect(self.generation)

    def test_unreadable_directory_is_not_silently_skipped(self):
        scan = os.scandir
        def denied(path):
            if Path(path).name == "unreadable":
                raise PermissionError("injected-directory-denial")
            return scan(path)
        directory = self.generation / "world/unreadable"
        directory.mkdir()
        (directory / "unexpected").write_bytes(b"must-not-disappear")
        with patch("dev_server.snapshot_data.os.scandir", side_effect=denied):
            with self.assertRaises(PermissionError):
                collect(self.generation)
        (directory / "unexpected").unlink()
        directory.rmdir()
        self.create()
        directory = self.destination / "data/world/unreadable"
        directory.mkdir()
        (directory / "unexpected").write_bytes(b"must-not-disappear")
        with patch("dev_server.runtime_bundle.os.scandir", side_effect=denied):
            with self.assertRaises(PermissionError):
                self.verify()

    def test_sqlite_with_non_database_extension_rejected(self):
        (self.generation / "world/unknown/opaque.bin").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
        self.assert_code("ROOT_MISMATCH", self.create)

    def test_unknown_writer_is_not_cleared(self):
        row = self.state()
        run = self.registry.runtime._load_run(row)
        with self.registry.transaction() as connection:
            connection.execute("INSERT INTO writer_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               ("unknown-session", row["id"], row["lease"], row["epoch"], row["generation"], row["run_id"],
                                run["nonce"], 99999, "{}", "UNKNOWN", 1))
            connection.execute("UPDATE instances SET writers=1 WHERE id=?", (row["id"],))
        self.assert_code("PROOF_REQUIRED", self.create)
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual("UNKNOWN", connection.execute("SELECT status FROM writer_sessions WHERE id='unknown-session'").fetchone()[0])


class SettingsTests(unittest.TestCase):
    def test_numeric_yaml_does_not_carry_secret_comments(self):
        value = transform("config/SushiEricServerMod/config.yml", b"# private-secret\nmining:\n  ore_revival_second: 12\n")
        self.assertEqual(b"mining:\n  ore_revival_second: 12\n", value)

    def test_unknown_duplicate_and_yaml_extensions_rejected(self):
        for value in (b"secret: 1\n", b"mining:\n  ore_revival_second: 12\n  ore_revival_second: 13\n", b"mining: &alias\n", b"mining:\n  ore_revival_second: !!int 12\n"):
            with self.subTest(value=value), self.assertRaises(RegistryError):
                transform("config/SushiEricServerMod/config.yml", value)
