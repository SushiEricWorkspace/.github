"""同じテストを各対象OS上で実行する。別OSの成功へ読み替えない。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from .build_lock import build_lock
from .fixtures import contract_documents
from .local_ipc import decode, encode, request, require_private
from .registry import Registry, RegistryError, initialize
from .supervisor import dispatch, new_request

SCRIPT = Path(__file__).parents[1] / "dev-server.py"
SUPPORTED = sys.platform in ("darwin", "win32")


class RegistryFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sed-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "dev"
        self.inventory = contract_documents("smoke-v1")[0]["inventory"]
        self.inventory["managementRoot"] = str(self.root)
        self.inventory["backup"]["destination"] = str(Path(self.temporary.name) / "synthetic-backup-not-real-medium")
        initialize(self.inventory, operation_id="init-test")
        self.now = 100
        self.registry = Registry(self.root, clock=lambda: self.now)
        self.registry.begin_supervision()

    def command(self, command, payload, operation="op-test", dry=False):
        return self.registry.execute(command, payload, operation_id=operation, dry_run=dry)

    def acquire(self, instance="persistent-01", operation="acquire-test"):
        result = self.command("acquire", {"instanceId": instance, "owner": "codex-test", "ttlSeconds": 20}, operation)
        return {"instanceId": instance, "leaseId": result["leaseId"], "epoch": result["epoch"], "expectedGeneration": result["generation"]}

    def assert_code(self, code, action):
        with self.assertRaises(RegistryError) as error:
            action()
        self.assertEqual(code, error.exception.code)


@unittest.skipUnless(SUPPORTED, "対象OS（macOS/Windows）のnative適合試験です")
class RegistryTests(RegistryFixture, unittest.TestCase):

    def test_init_replay_conflict_and_dry_run(self):
        self.assertTrue(initialize(self.inventory, operation_id="init-test")["replayed"])
        changed = deepcopy(self.inventory)
        changed["quotaBytes"] += 100
        self.assert_code("OPERATION_CONFLICT", lambda: initialize(changed, operation_id="init-test"))
        self.assert_code("EXISTING_DATA", lambda: initialize(self.inventory, operation_id="another-init"))
        other = deepcopy(self.inventory)
        other["managementRoot"] = str(Path(self.temporary.name) / "dry")
        self.assertTrue(initialize(other, operation_id="dry-init", dry_run=True)["dryRun"])
        self.assertFalse(Path(other["managementRoot"]).exists())

    def test_epoch_increases_old_lease_and_old_replay_rejected(self):
        first = self.acquire()
        self.command("release", first, "release-first")
        second = self.acquire(operation="acquire-second")
        self.assertEqual(first["epoch"] + 1, second["epoch"])
        self.assert_code("STALE_LEASE", lambda: self.command("release", first, "release-old"))
        self.assert_code("STALE_LEASE", lambda: self.acquire())
        self.assert_code("STALE_LEASE", lambda: self.command("release", first, "release-first"))

    def test_expiration_does_not_release_or_allow_renew(self):
        token = self.acquire()
        self.now = 121
        self.assert_code("LEASE_EXPIRED", lambda: self.command("renew", dict(token, ttlSeconds=10), "renew-expired"))
        self.assert_code("UNAVAILABLE", lambda: self.acquire(operation="second-owner"))
        status = self.command("status", {"instanceId": "persistent-01"})["instances"][0]
        self.assertTrue(status["leaseExpired"])
        self.assertEqual("LEASED_STOPPED", status["state"])

    def test_replay_same_payload_and_reject_different_payload(self):
        first = self.acquire()
        self.assertEqual(first, self.acquire())
        self.assert_code("OPERATION_CONFLICT", lambda: self.acquire("persistent-02"))
        renewed = self.command("renew", dict(first, ttlSeconds=30), "renew-first")
        self.now += 1
        self.assertEqual(renewed["expiresAt"], self.command("renew", dict(first, ttlSeconds=30), "renew-first")["expiresAt"])

    def test_dry_run_has_no_lease_or_operation(self):
        self.command("acquire", {"instanceId": "persistent-01", "owner": "test-owner", "ttlSeconds": 10}, "dry-acquire", True)
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM operations").fetchone()[0])
            self.assertIsNone(connection.execute("SELECT lease FROM instances WHERE id='persistent-01'").fetchone()[0])

    def test_shutdown_conflict_with_lease_and_reference_operations(self):
        token = self.acquire()
        self.registry.reserve_references(token, "reserve-test", ["artifact:test"])
        for operation in ("acquire-test", "reserve-test"):
            response = dispatch(self.registry, new_request("shutdown", {}, operation_id=operation))
            self.assertEqual("OPERATION_CONFLICT", response["error"]["code"])
        self.assertEqual("LEASED_STOPPED", self.command("status", {"instanceId": "persistent-01"})["instances"][0]["state"])

    def test_shutdown_replay_is_bound_to_supervisor_boot(self):
        first = self.command("shutdown", {}, "shutdown-test")
        self.assertTrue(first["shutdown"])
        repeated = self.command("shutdown", {}, "shutdown-test")
        self.assertEqual(first["bootId"], repeated["bootId"])
        self.assertTrue(repeated["replayed"])
        preview = self.command("shutdown", {}, "shutdown-test", True)
        self.assertFalse(preview["shutdown"])
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM audit WHERE command='shutdown'").fetchone()[0])
        self.registry.begin_supervision()
        self.assert_code("STALE_SUPERVISOR", lambda: self.command("shutdown", {}, "shutdown-test"))

    def test_shutdown_dry_run_does_not_change_state_or_operations(self):
        self.acquire()
        before = self.command("list", {})
        preview = self.command("shutdown", {}, "shutdown-preview", True)
        self.assertTrue(preview["dryRun"])
        self.assertFalse(preview.get("shutdown", False))
        self.assertEqual(before, self.command("list", {}))
        with self.registry.transaction(read_only=True) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM operations WHERE id='shutdown-preview'").fetchone())

    def test_restart_marks_all_frames_recovery(self):
        token = self.acquire()
        self.registry.begin_supervision()
        self.assert_code("RECOVERY_REQUIRED", lambda: self.command("release", token))
        self.assert_code("UNAVAILABLE", lambda: self.acquire("persistent-02", "new-owner"))
        self.assertEqual({"RECOVERY_REQUIRED"}, {row["state"] for row in self.command("list", {})["instances"]})

    def test_stale_generation_and_no_old_generation_start(self):
        token = self.acquire()
        self.assert_code("STALE_GENERATION", lambda: self.command("release", dict(token, expectedGeneration="old")))
        self.assert_code("NOT_IMPLEMENTED", lambda: self.command("start", token))
        self.assertFalse(self.command("status", {"instanceId": "persistent-01"})["instances"][0]["generationBound"])

    def test_release_rejects_writers_children_pending_and_unproven_data(self):
        token = self.acquire()
        for field in ("writers", "children", "pending"):
            with self.registry.transaction() as connection:
                connection.execute(f"UPDATE instances SET {field}=1 WHERE id='persistent-01'")
            self.assert_code("BUSY", lambda: self.command("release", token))
            with self.registry.transaction() as connection:
                connection.execute(f"UPDATE instances SET {field}=0 WHERE id='persistent-01'")
        directory = self.root / "instances" / "persistent-01"
        directory.mkdir()
        (directory / "unknown-data").write_bytes(b"preserve")
        self.assert_code("PROOF_REQUIRED", lambda: self.command("release", token))
        self.assertEqual(b"preserve", (directory / "unknown-data").read_bytes())

    def test_reference_reservations_replay_and_release_block(self):
        one = self.acquire()
        two = self.acquire("persistent-02", "acquire-two")
        refs = ["snapshot:abc", "artifact:def"]
        self.registry.reserve_references(one, "reserve-one", refs)
        self.registry.reserve_references(one, "reserve-one", list(reversed(refs)))
        self.registry.reserve_references(two, "reserve-two", list(reversed(refs)))
        self.assert_code("OPERATION_CONFLICT", lambda: self.registry.reserve_references(one, "reserve-one", ["artifact:def"]))
        self.assert_code("BUSY", lambda: self.command("release", one))
        self.assert_code("STALE_LEASE", lambda: self.registry.finish_reservation(two, "reserve-one"))
        self.registry.finish_reservation(one, "reserve-one")
        self.registry.finish_reservation(one, "reserve-one")
        self.assert_code("STALE_LEASE", lambda: self.registry.finish_reservation(two, "reserve-one"))
        self.registry.reserve_references(one, "reserve-one", refs)
        self.assert_code("OPERATION_CONFLICT", lambda: self.command("renew", dict(one, ttlSeconds=30), "reserve-one"))
        self.assertTrue(self.command("release", one)["released"])

    def test_db_failure_and_lock_conflict_are_classified(self):
        with self.registry.transaction():
            response = dispatch(self.registry, new_request("acquire", {"instanceId": "persistent-01", "owner": "test-owner", "ttlSeconds": 10}))
            self.assertEqual("DB_FAILURE", response["error"]["code"])
        lock = self.root / "manager" / "locks" / "instance-persistent-01.lock"
        with build_lock(lock):
            response = dispatch(self.registry, new_request("acquire", {"instanceId": "persistent-01", "owner": "test-owner", "ttlSeconds": 10}))
            self.assertEqual("LOCK_BUSY", response["error"]["code"])

    def test_invalid_message_and_unimplemented_never_success(self):
        for message in ({}, dict(new_request("list", {}), version=True), dict(new_request("list", {}), dryRun="false")):
            self.assertFalse(dispatch(self.registry, message)["ok"])
        for command in ("start", "stop", "prepare", "restore", "snapshot", "recover", "gc"):
            response = dispatch(self.registry, new_request(command, {"unknown-secret": "must-not-print"}))
            self.assertEqual("NOT_IMPLEMENTED", response["error"]["code"])
            self.assertNotIn("must-not-print", str(response))

    def test_inventory_port_tampering_requires_recovery(self):
        with self.registry.transaction() as connection:
            connection.execute("UPDATE ports SET port=25888 WHERE port=25665")
        self.assert_code("RECOVERY_REQUIRED", lambda: Registry(self.root))

    def test_native_security_rejects_broader_permissions(self):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes as w
            from .windows_security import advapi, kernel, current_sid, make_private
            descriptor = w.LPVOID()
            sddl = f"O:{current_sid()}D:P(A;;FA;;;{current_sid()})(A;;FR;;;WD)"
            self.assertTrue(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
            path = self.root / "manager" / "registry.sqlite"
            try:
                self.assertTrue(advapi.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor))
                with self.assertRaises(PermissionError):
                    require_private(path)
            finally:
                kernel.LocalFree(descriptor)
                make_private(path)
        else:
            path = self.root / "manager"
            path.chmod(0o755)
            try:
                with self.assertRaises(PermissionError):
                    require_private(path, directory=True)
            finally:
                path.chmod(0o700)

    @unittest.skipUnless(os.name == "nt", "Windowsのmapped drive拒否試験です")
    def test_windows_network_drive_mapping_is_rejected(self):
        from .windows_security import kernel, require_local_volume
        with patch.object(kernel, "GetDriveTypeW", return_value=4):
            with self.assertRaises(OSError):
                require_local_volume(self.root)

    def test_json_only_duplicate_keys_and_nonfinite_rejected(self):
        for value in (b'{"x":1,"x":2}', b'{"x":NaN}', b'[]', b'not-a-pickle',
                      b'{"x":' + b'[' * 30000 + b']' * 30000 + b'}'):
            with self.assertRaises(ValueError):
                decode(value)
        with self.assertRaises(ValueError):
            encode({"x": "a" * 65536})


@unittest.skipUnless(SUPPORTED, "対象OS（macOS/Windows）のnative IPCプロセス試験です")
class SupervisorProcessTests(RegistryFixture, unittest.TestCase):
    def _start(self):
        process = subprocess.Popen([sys.executable, "-B", str(SCRIPT), "supervise", "--root", str(self.root)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.addCleanup(self._stop, process)
        # 接続できることを確認し、stdoutのreadlineで無期限に待たない。
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("supervisorの起動に失敗: " + process.communicate()[0])
            try:
                response = request(self.root, new_request("doctor", {}), timeout=0.2)
                if response["ok"]:
                    return process
            except (OSError, EOFError, ValueError):
                time.sleep(0.05)
        self.fail("supervisorが起動完了しません")

    def _stop(self, process):
        if process.poll() is None:
            try:
                request(self.root, new_request("shutdown", {}), timeout=2)
            except (OSError, EOFError, ValueError):
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # このテストが生成したPython childだけ。Minecraftや管理外processへ触れない。
                process.terminate()
                process.wait(timeout=5)
        process.communicate(timeout=5)

    def setUp(self):
        super().setUp()
        # 最初のnative supervisorが初回として起動できるfixture。
        with self.registry.transaction() as connection:
            connection.execute("DELETE FROM meta WHERE key='supervisorBoot'")

    def test_native_two_acquires_and_cli_exit(self):
        process = self._start()
        payload = {"instanceId": "persistent-01", "owner": "owner-test", "ttlSeconds": 60}
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: request(self.root, new_request("acquire", payload)), range(2)))
        self.assertEqual(1, sum(result["ok"] for result in results))
        self.assertEqual("UNAVAILABLE", next(result["error"]["code"] for result in results if not result["ok"]))
        cli = subprocess.run([sys.executable, "-B", str(SCRIPT), "status", "--root", str(self.root), "--payload", '{"instanceId":"persistent-01"}'],
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(0, cli.returncode, cli.stdout)
        self.assertEqual("LEASED_STOPPED", json.loads(cli.stdout)["result"]["instances"][0]["state"])
        self.assertIsNone(process.poll())

    def test_native_many_read_requests_do_not_exhaust_pipe_instances(self):
        process = self._start()
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: request(self.root, new_request("list", {})), range(32)))
        self.assertTrue(all(result["ok"] for result in results))
        self.assertIsNone(process.poll())

    def test_native_deep_json_is_rejected_without_stopping_supervisor(self):
        process = self._start()
        payload = b'{"x":' + b'[' * 30000 + b']' * 30000 + b'}'
        with patch("dev_server.local_ipc.encode", return_value=payload):
            with self.assertRaises((OSError, EOFError)):
                request(self.root, new_request("list", {}))
        self.assertTrue(request(self.root, new_request("doctor", {}))["ok"])
        self.assertIsNone(process.poll())

    def test_native_old_shutdown_does_not_stop_restarted_supervisor(self):
        first = self._start()
        message = new_request("shutdown", {}, operation_id="shutdown-first")
        self.assertTrue(request(self.root, message)["ok"])
        first.wait(timeout=5)
        second = self._start()
        response = request(self.root, message)
        self.assertEqual("STALE_SUPERVISOR", response["error"]["code"])
        self.assertTrue(request(self.root, new_request("doctor", {}))["ok"])
        self.assertIsNone(second.poll())

    def test_native_singleton_and_restart_recovery(self):
        first = self._start()
        duplicate = subprocess.run([sys.executable, "-B", str(SCRIPT), "supervise", "--root", str(self.root)], capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertNotEqual(0, duplicate.returncode)
        self.assertEqual("LOCK_BUSY", json.loads(duplicate.stdout)["error"]["code"])
        self._stop(first)
        second = self._start()
        response = request(self.root, new_request("list", {}))
        self.assertEqual({"RECOVERY_REQUIRED"}, {row["state"] for row in response["result"]["instances"]})
        self.assertIsNone(second.poll())

    def test_native_permissions_and_unavailable_no_autostart(self):
        require_private(self.root, directory=True)
        require_private(self.root / "manager", directory=True)
        require_private(self.root / "manager" / "registry.sqlite")
        with self.assertRaises(OSError):
            request(self.root, new_request("list", {}), timeout=0.1)
        process = self._start()
        response = request(self.root, new_request("start", {}))
        self.assertEqual("NOT_IMPLEMENTED", response["error"]["code"])
        self.assertIsNone(process.poll())

    def test_native_unexpected_death_recovery_and_stale_replay(self):
        first = self._start()
        acquired = request(self.root, new_request("acquire", {"instanceId": "persistent-01", "owner": "owner-test", "ttlSeconds": 60}, operation_id="native-acquire"))
        self.assertTrue(acquired["ok"])
        # 障害注入の対象はこのテストが生成したPython supervisorだけ。
        first.terminate()
        first.wait(timeout=5)
        self._start()
        repeated = request(self.root, new_request("acquire", {"instanceId": "persistent-01", "owner": "owner-test", "ttlSeconds": 60}, operation_id="native-acquire"))
        self.assertEqual("RECOVERY_REQUIRED", repeated["error"]["code"])

    def test_native_dry_run_and_release_replay(self):
        self._start()
        payload = {"instanceId": "persistent-01", "owner": "owner-test", "ttlSeconds": 60}
        before = {entry.relative_to(self.root) for entry in self.root.rglob("*")}
        preview = request(self.root, new_request("acquire", payload, operation_id="preview", dry_run=True))
        self.assertTrue(preview["ok"])
        self.assertNotIn("leaseId", preview["result"])
        self.assertEqual(before, {entry.relative_to(self.root) for entry in self.root.rglob("*")})
        acquired = request(self.root, new_request("acquire", payload, operation_id="native-acquire"))["result"]
        token = {"instanceId": "persistent-01", "leaseId": acquired["leaseId"], "epoch": acquired["epoch"], "expectedGeneration": acquired["generation"]}
        first = request(self.root, new_request("release", token, operation_id="native-release"))
        replay = request(self.root, new_request("release", token, operation_id="native-release"))
        self.assertTrue(first["ok"] and replay["ok"])
        self.assertTrue(replay["result"]["replayed"])

    def test_native_cli_payload_file_and_json_errors(self):
        self._start()
        payload = Path(self.temporary.name) / "request.json"
        payload.write_text(json.dumps({"instanceId": "persistent-01", "owner": "owner-test", "ttlSeconds": 30}), encoding="utf-8")
        cli = subprocess.run([sys.executable, "-B", str(SCRIPT), "acquire", "--root", str(self.root), "--operation-id", "payload-file",
                              "--payload-file", str(payload)], capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(0, cli.returncode, cli.stdout)
        self.assertTrue(json.loads(cli.stdout)["ok"])
        cli = subprocess.run([sys.executable, "-B", str(SCRIPT), "unknown-secret-argument"], capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertNotEqual(0, cli.returncode)
        self.assertEqual("INVALID_INPUT", json.loads(cli.stdout)["error"]["code"])
        self.assertNotIn("unknown-secret-argument", cli.stdout + cli.stderr)


if __name__ == "__main__":
    unittest.main()
