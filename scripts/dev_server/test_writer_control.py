"""合成runtimeとnative Manager識別によるwriter境界。実Minecraft試験ではありません。"""
from concurrent.futures import ThreadPoolExecutor
import json
import base64
import hashlib
import os
from pathlib import Path
import threading
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from . import runtime_process
from .registry import Registry, RegistryError
from .supervisor import dispatch, new_request
from . import test_runtime_control as runtime_tests
from .writer_api import exchange


class ApiPeer:
    """実TCP/WebSocketで応答とCloseの欠落を注入する使い捨てpeer。"""
    def __init__(self, mode):
        self.mode = mode
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.endpoint = {"address": "127.0.0.1", "port": self.socket.getsockname()[1], "protocol": "tcp"}
        self.error = None
        self.request = None
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        try:
            with self.socket, self.socket.accept()[0] as stream:
                stream.settimeout(3)
                def read(size):
                    value = bytearray()
                    while len(value) < size:
                        part = stream.recv(size - len(value))
                        if not part:
                            raise EOFError()
                        value.extend(part)
                    return bytes(value)
                def frame():
                    first, second = read(2)
                    length = second & 127
                    if length == 126:
                        length = struct.unpack("!H", read(2))[0]
                    mask = read(4)
                    return first & 15, bytes(value ^ mask[index % 4] for index, value in enumerate(read(length)))
                header = bytearray()
                while not header.endswith(b"\r\n\r\n"):
                    header.extend(read(1))
                self.assert_path = header.startswith(b"GET /management HTTP/1.1\r\n")
                key = next(line.split(":", 1)[1].strip() for line in header.decode().split("\r\n") if line.startswith("Sec-WebSocket-Key:"))
                accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                stream.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
                opcode, body = frame()
                assert opcode == 1
                self.request = json.loads(body)
                if self.mode == "lost-response":
                    return
                response = json.dumps({"type": "money_history_saved", "nonce": self.request["nonce"], "success": False}).encode()
                stream.sendall(bytes([0x81, len(response)]) + response)
                opcode, body = frame()
                assert opcode == 8
                if self.mode == "lost-close":
                    return
                stream.sendall(bytes([0x88, len(body)]) + body)
        except BaseException as error:
            self.error = error

    def finish(self):
        self.thread.join(4)
        if self.thread.is_alive():
            raise AssertionError("合成peerが終了していません")
        if self.error:
            raise self.error


class WriterControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_tests.RuntimeControlTests("test_prepare_start_stop_replay_restart_and_no_snapshot_clean")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.generation = self.fixture.prepare()
        self.fixture.start()
        self.registry = self.fixture.registry
        status = self.fixture.command("status", {"instanceId": "persistent-01"})["instances"][0]
        native = runtime_process.identity(os.getpid())
        self.payload = dict(self.fixture.token, sessionId="session-01", runId=status["runId"], nonce=status["nonce"],
                            pid=native["pid"], startMillis=native["startMillis"], cwd=native["cwd"])
        self.binding = {key: value for key, value in self.payload.items() if key not in {"pid", "startMillis", "cwd"}}
        self.sequence = 0

    def command(self, command, payload, operation=None):
        self.sequence += 1
        response = dispatch(self.registry, new_request(command, payload, operation_id=operation or f"writer-op-{self.sequence}"))
        return response

    def open(self, operation="writer-open-01"):
        response = self.command("writer-open", self.payload, operation)
        self.assertTrue(response["ok"], response)
        return response

    def io(self, action="write", path="item_data/stats/shared.yml", **kwargs):
        request = dict(action=action, path=path, **kwargs)
        response = self.command("writer-io", dict(self.binding, request=request))
        return response

    def status(self):
        return self.fixture.command("status", {"instanceId": "persistent-01"})["instances"][0]

    def test_status_file_io_and_actual_api_do_not_enumerate_unrelated_tree(self):
        target = self.generation / "config/SushiEricServerMod/item_data/shared.yml"
        target.parent.mkdir(parents=True)
        target.write_text("value: retained", encoding="utf-8")
        (self.generation / "world/unrelated/deep").mkdir(parents=True)
        peer = ApiPeer("complete")
        def invoke(endpoint, message, operation_id, timeout, *, verify):
            return exchange(peer.endpoint, message, operation_id, timeout, verify=verify)
        try:
            with patch.object(Path, "rglob", side_effect=AssertionError("常用照合で無関係なtreeを走査した")):
                self.open()
                self.assertEqual("RUNNING", self.status()["state"])
                self.assertEqual("value: retained", self.io("read", path="item_data/shared.yml")["result"]["text"])
                self.assertEqual("shared.yml", self.io("list", path="item_data")["result"]["entries"][0]["name"])
                self.assertTrue(self.io("move", path="item_data/shared.yml", destination="item_data/moved.yml")["ok"])
                self.assertTrue(self.io("mkdir", path="item_data/empty")["ok"])
                self.assertTrue(self.io("delete-directory", path="item_data/empty")["ok"])
                self.assertTrue(self.io("server-read", path="config/SushiEricServerMod/manager-compat.json")["error"]["rejectedBeforeEffect"])
                payload = dict(self.binding, request={"action": "api", "message": {"type": "money_history_save"}, "timeoutSeconds": 2})
                with patch("dev_server.writer_api.exchange", side_effect=invoke):
                    response = self.command("writer-io", payload)
                self.assertTrue(response["ok"], response)
                self.assertTrue(response["result"]["completed"])
                self.assertFalse(response["result"]["apiResponse"]["success"])
                self.assertTrue(self.command("writer-close", dict(self.binding, drained=True, disconnected=True))["ok"])
        finally:
            peer.finish()
        self.assertEqual("value: retained", target.with_name("moved.yml").read_text())

    def test_running_fast_path_retains_metadata_identity_proof_and_socket_fences(self):
        control = self.registry.runtime
        row = self.fixture.state()
        process = self.fixture.adapter.created[-1]
        root = self.generation.parents[1]
        for path, key, value in ((root / "active.json", "artifactId", "wrong"),
                                 (self.generation / "generation.json", "layoutId", "wrong")):
            original = path.read_bytes()
            record = json.loads(original)
            record[key] = value
            path.write_text(json.dumps(record))
            try:
                with self.assertRaises(RegistryError):
                    control._running(row)
            finally:
                path.write_bytes(original)
        committed = self.registry.root / "manager/operations/generation-prepare-01/committed.json"
        original = committed.read_bytes()
        record = json.loads(original)
        record["instanceId"] = "different-instance"
        committed.write_text(json.dumps(record))
        try:
            with self.assertRaises(RegistryError):
                control._running(row)
        finally:
            committed.write_bytes(original)
        for key in ("pid", "startId", "startMillis", "bootId", "cwd", "executable", "commandHash", "owner"):
            original = process.identity[key]
            process.identity[key] = original + 1 if type(original) is int else "mismatch"
            try:
                with self.assertRaises(RegistryError):
                    control._running(row)
            finally:
                process.identity[key] = original
        original = process.proof["startNonce"]
        process.proof["startNonce"] = "stale"
        process.save_proof()
        with self.assertRaises(RegistryError):
            control._running(row)
        process.proof["startNonce"] = original
        original = process.proof["startup"]["roots"]["world"]
        process.proof["startup"]["roots"]["world"] = str(self.generation / "another-world")
        process.save_proof()
        with self.assertRaises(RegistryError):
            control._running(row)
        process.proof["startup"]["roots"]["world"] = original
        process.save_proof()
        self.fixture.adapter.socket_fault = lambda sockets: sockets.pop()
        with self.assertRaises(RegistryError):
            control._running(row)
        self.fixture.adapter.socket_fault = None
        control._running(row)

    def test_start_stop_keep_full_tree_and_file_ancestor_inspection(self):
        row = self.fixture.state()
        with patch.object(Path, "rglob", side_effect=OSError("injected deep link")):
            self.assertEqual("INVALID_STATE", self.command("start", dict(self.fixture.token, timeoutSeconds=2), "start-01")["error"]["code"])
            self.assertEqual("INVALID_STATE", self.command("stop", dict(self.fixture.token, timeoutSeconds=2))["error"]["code"])
        self.open()
        from .local_ipc import plain_path
        ancestor = self.generation / "config/SushiEricServerMod/item_data"
        def check(path):
            if path == ancestor or ancestor in path.parents:
                raise OSError("injected ancestor reparse point")
            plain_path(path)
        with patch("dev_server.writer_control.plain_path", side_effect=check):
            self.assertFalse(self.io("read")["ok"])
            self.assertFalse(self.io("move", destination="item_data/new.yml")["ok"])
        self.assertEqual("RUNNING", self.status()["state"])
        self.assertEqual(row["run_id"], self.fixture.state()["run_id"])

    def test_fs_save_measures_large_tree_and_rechecks_live_quota(self):
        self.open()
        directory = self.generation / "world/region/deep"
        directory.mkdir(parents=True)
        for number in range(2000):
            (directory / f"{number}.mca").write_bytes(b"retained" * 8)
        started = time.monotonic()
        with patch.object(Path, "rglob", side_effect=AssertionError("quota照合で祖先反復のtree走査へ戻った")):
            response = self.io(text="id: saved\n")
        elapsed = time.monotonic() - started
        print(f"FS save: unrelatedFiles=2000 elapsedSeconds={elapsed:.3f}", flush=True)
        self.assertTrue(response["ok"], response)
        self.assertLess(elapsed, 15, "合成fixtureでも既存CLI期限を超過した")
        control = self.registry.runtime
        used = sum(path.stat().st_size for path in self.registry.root.rglob("*") if path.is_file())
        quota = self.registry.inventory["quotaBytes"]
        self.registry.inventory["quotaBytes"] = used + self.registry.inventory["reserveBytes"]
        try:
            control._space()
            (directory / "new.mca").write_bytes(b"new growth")
            with self.assertRaises(RegistryError) as caught:
                control._space()
            self.assertEqual("NO_SPACE", caught.exception.code)
            refused = self.io(text="must not save")
            self.assertEqual("NO_SPACE", refused["error"]["code"])
            self.assertTrue(refused["error"]["rejectedBeforeEffect"])
            self.assertEqual("id: saved\n", (self.generation / "config/SushiEricServerMod/item_data/stats/shared.yml").read_text())
        finally:
            self.registry.inventory["quotaBytes"] = quota
        self.assertTrue(self.command("writer-close", dict(self.binding, drained=True, disconnected=True))["ok"])

    def test_native_registration_replay_close_and_closed_gate(self):
        self.open()
        self.assertTrue(self.open()["result"]["replayed"])
        self.assertEqual(1, self.status()["writers"])
        self.assertTrue(self.io(text="id: shared\n")["ok"])
        self.assertEqual("id: shared\n", self.io("read")["result"]["text"])
        close = dict(self.binding, drained=True, disconnected=True)
        self.assertTrue(self.command("writer-close", close, "close-01")["ok"])
        self.assertTrue(self.command("writer-close", close, "close-01")["result"]["replayed"])
        self.assertEqual(0, self.status()["writers"])
        self.assertFalse(self.io(text="late")["ok"])
        self.assertEqual("id: shared\n", (self.generation / "config/SushiEricServerMod/item_data/stats/shared.yml").read_text())

    def test_known_pre_effect_rejection_preserves_open_writer(self):
        self.open()
        self.assertTrue(self.io(text="source")["ok"])
        self.assertTrue(self.io(path="item_data/stats/destination.yml", text="destination")["ok"])
        collision = self.io("move", destination="item_data/stats/destination.yml")
        self.assertEqual("ALREADY_EXISTS", collision["error"]["code"])
        self.assertTrue(collision["error"]["rejectedBeforeEffect"])
        denied = self.io(path="management/config.yml", text="secret")
        self.assertTrue(denied["error"]["rejectedBeforeEffect"])
        self.assertTrue(self.io(text="still-open")["ok"])
        self.assertTrue(self.command("writer-close", dict(self.binding, drained=True, disconnected=True))["ok"])

    def test_actual_websocket_response_and_close_loss_hold_pending(self):
        self.open()
        for mode in ("lost-response", "lost-close"):
            with self.subTest(mode=mode):
                peer = ApiPeer(mode)
                def invoke(endpoint, message, operation_id, timeout, *, verify):
                    return exchange(peer.endpoint, message, operation_id, timeout, verify=verify)
                payload = dict(self.binding, request={"action": "api", "message": {"type": "money_history_save"}, "timeoutSeconds": 2})
                with patch("dev_server.writer_api.exchange", side_effect=invoke):
                    response = self.command("writer-io", payload)
                peer.finish()
                self.assertFalse(response["ok"])
                self.assertNotIn("rejectedBeforeEffect", response["error"])
                with self.registry.transaction(read_only=True) as connection:
                    session = connection.execute("SELECT * FROM writer_sessions WHERE id=?", (self.binding["sessionId"],)).fetchone()
                    self.assertEqual(("UNKNOWN", 1), (session["status"], session["pending"]))
                self.assertFalse(self.command("writer-close", dict(self.binding, drained=True, disconnected=True))["ok"])
                # 後半も別の実sessionを使い、保留sessionを解除しない。
                self.payload["sessionId"] = self.binding["sessionId"] = "session-02"
                if mode == "lost-response":
                    self.open("writer-open-02")

    def test_id_rewrite_move_is_one_guarded_operation_and_partial_failure_stays_pending(self):
        self.open()
        self.assertTrue(self.io(text="id: old\n")["ok"])
        moved = self.io("move", destination="item_data/stats/new.yml", text="id: new\n")
        self.assertTrue(moved["ok"], moved)
        self.assertFalse((self.generation / "config/SushiEricServerMod/item_data/stats/shared.yml").exists())
        self.assertEqual("id: new\n", self.io("read", path="item_data/stats/new.yml")["result"]["text"])
        with patch.object(Path, "unlink", side_effect=PermissionError("injected after destination write")):
            failed = self.io("move", path="item_data/stats/new.yml", destination="item_data/stats/partial.yml", text="id: partial\n")
        self.assertFalse(failed["ok"])
        self.assertNotIn("rejectedBeforeEffect", failed["error"])
        self.assertTrue((self.generation / "config/SushiEricServerMod/item_data/stats/new.yml").is_file())
        self.assertTrue((self.generation / "config/SushiEricServerMod/item_data/stats/partial.yml").is_file())
        self.assertFalse(self.command("writer-close", dict(self.binding, drained=True, disconnected=True))["ok"])

    def test_history_is_read_only_and_lease_expiration_does_not_release_writer(self):
        self.open()
        relative = "player_data/00000000-0000-0000-0000-000000000529/money_history/2026-10-11.yml"
        target = self.generation / "config/SushiEricServerMod" / relative
        target.parent.mkdir(parents=True)
        target.write_text("entries: []\n", encoding="utf-8")
        self.assertEqual("entries: []\n", self.io("read", path=relative)["result"]["text"])
        rejected = self.io("write", path=relative, text="overwrite")
        self.assertTrue(rejected["error"]["rejectedBeforeEffect"])
        self.assertEqual("entries: []\n", target.read_text())
        self.fixture.now += 100
        self.assertEqual("LEASE_EXPIRED", self.io(text="late")["error"]["code"])
        self.assertEqual(1, self.status()["writers"])
        self.assertFalse(self.command("writer-close", dict(self.binding, drained=True, disconnected=True))["ok"])


    def test_native_start_mismatch_dry_run_and_identity_change(self):
        wrong = dict(self.payload, startMillis=self.payload["startMillis"] + 1)
        self.assertEqual("PROCESS_MISMATCH", self.command("writer-open", wrong)["error"]["code"])
        response = dispatch(self.registry, new_request("writer-open", self.payload, operation_id="dry-writer", dry_run=True))
        self.assertTrue(response["ok"])
        self.assertEqual(0, self.status()["writers"])
        self.open()
        with patch("dev_server.writer_control.runtime_process.identity", return_value=None):
            self.assertEqual("PROCESS_MISMATCH", self.io(text="late")["error"]["code"])
        self.assertEqual(1, self.status()["writers"])

    def test_stale_generation_epoch_nonce_and_secret_paths_are_rejected(self):
        self.open()
        for key, value in (("leaseId", "old-lease"), ("epoch", 2), ("expectedGeneration", "generation-old"), ("nonce", "old-nonce"), ("runId", "old-run")):
            payload = dict(self.binding, request={"action": "write", "path": "item_data/stats/shared.yml", "text": "late"})
            payload[key] = value
            self.assertFalse(self.command("writer-io", payload)["ok"])
        for path in ("../server.properties", "management/config.yml", "player_data/user.yml", "item_data/../../secret", "item_data/CON.yml"):
            self.assertFalse(self.io(path=path, text="secret")["ok"])
        self.assertFalse((self.generation / "config/SushiEricServerMod/item_data/stats/shared.yml").exists())

    def test_stopped_writer_remains_and_restart_rejects_old_queue(self):
        self.open()
        self.fixture.stop()
        status = self.status()
        self.assertTrue(status["runtimeStopProof"])
        self.assertFalse(status["cleanEligible"])
        self.assertFalse(status["externalWritersVerified"])
        self.assertFalse(self.io(text="late")["ok"])
        self.fixture.assert_code("NOT_STOPPED", lambda: self.fixture.start("blocked-start"))
        self.assertTrue(self.command("writer-close", dict(self.binding, drained=True, disconnected=True))["ok"])
        self.assertTrue(self.status()["cleanEligible"])
        self.assertFalse(self.status()["snapshotClean"])
        self.fixture.start("start-new")
        self.assertFalse(self.io(text="old-run")["ok"])

    def test_native_writer_blocks_snapshot_and_restore_until_close(self):
        self.open()
        self.fixture.stop()
        snapshot = self.command("snapshot", dict(self.fixture.token, action="create", snapshotId="blocked-snapshot"))
        restore = self.command("restore", dict(self.fixture.token, artifactId=self.fixture.state()["artifact"],
                                               generationId="blocked-generation", snapshotId="absent-snapshot", acceptEula=True))
        for response in (snapshot, restore):
            self.assertFalse(response["ok"], response)
            self.assertEqual("PROOF_REQUIRED", response["error"]["code"])
        self.assertEqual(1, self.status()["writers"])
        self.assertFalse(self.status()["cleanEligible"])
        self.assertTrue(self.command("writer-close", dict(self.binding, drained=True, disconnected=True))["ok"])
        self.assertTrue(self.status()["cleanEligible"])

    def test_api_write_and_stop_share_lock_and_api_response_not_persisted(self):
        self.open()
        entered, finish = threading.Event(), threading.Event()
        def exchange(*args, **kwargs):
            kwargs["verify"]()
            entered.set()
            self.assertTrue(finish.wait(5))
            return {"type": "command_result", "nonce": "command-test", "detail": "secret-output"}
        payload = dict(self.binding, request={"action": "api", "message": {"type": "command_execute", "command": "say secret-input", "nonce": "command-test"}, "timeoutSeconds": 2})
        with patch("dev_server.writer_api.exchange", side_effect=exchange), ThreadPoolExecutor() as workers:
            future = workers.submit(self.command, "writer-io", payload, "api-op")
            self.assertTrue(entered.wait(5))
            stop = dispatch(self.registry, new_request("stop", dict(self.fixture.token, timeoutSeconds=2), operation_id="stop-race"))
            self.assertEqual("LOCK_BUSY", stop["error"]["code"])
            finish.set()
            self.assertTrue(future.result()["ok"])
        with self.registry.transaction(read_only=True) as connection:
            record = connection.execute("SELECT * FROM writer_operations WHERE id='api-op'").fetchone()
            self.assertNotIn("secret", json.dumps(dict(record)))
        replay = self.command("writer-io", payload, "api-op")
        self.assertTrue(replay["result"]["replayed"])
        self.assertNotIn("apiResponse", replay["result"])

    def test_io_failure_and_supervisor_restart_never_drop_writer(self):
        self.open()
        with patch.object(self.registry.writer, "_io", side_effect=OSError("injected")):
            self.assertFalse(self.io(text="pending")["ok"])
        self.assertEqual(1, self.status()["writers"])
        self.assertFalse(self.command("writer-close", dict(self.binding, drained=True, disconnected=True))["ok"])
        self.fixture.assert_code("WRITER_PENDING", self.fixture.stop)
        restarted = Registry(self.registry.root, clock=lambda: self.fixture.now)
        restarted.begin_supervision()
        self.assertEqual(1, restarted.execute("status", {"instanceId": "persistent-01"}, operation_id="status-restart")["instances"][0]["writers"])
        self.assertFalse(dispatch(restarted, new_request("writer-close", dict(self.binding, drained=True, disconnected=True)))["ok"])

    def test_cross_table_operation_id_race_allows_only_one_registration(self):
        self.open()
        with ThreadPoolExecutor() as workers:
            writer = workers.submit(self.command, "writer-io", dict(self.binding, request={"action": "write", "path": "config.yml", "text": "value: 1"}), "same-op")
            renewal = workers.submit(dispatch, self.registry, new_request("renew", dict(self.fixture.token, ttlSeconds=30), operation_id="same-op"))
            results = [writer.result(), renewal.result()]
        self.assertEqual(1, sum(item["ok"] for item in results))
        self.assertEqual("OPERATION_CONFLICT", next(item for item in results if not item["ok"])["error"]["code"])

    def test_recursive_delete_rejects_link_before_listing_child(self):
        self.open()
        target = self.generation / "config/SushiEricServerMod/item_data/remove"
        target.mkdir(parents=True)
        child = target / "external"
        child.mkdir()
        sentinel = child / "keep.yml"
        sentinel.write_text("preserve")
        from .local_ipc import plain_path
        def check(path):
            if path == child:
                raise OSError("simulated reparse point")
            plain_path(path)
        with patch("dev_server.writer_control.plain_path", side_effect=check), patch.object(Path, "iterdir", autospec=True, wraps=Path.iterdir) as iterator:
            # 実装の階層検査後だけ列挙する境界を確認する。
            iterator.side_effect = lambda path: iter([child]) if path == target else (_ for _ in ()).throw(AssertionError("リンク配下を列挙した"))
            self.assertFalse(self.io("delete-directory", path="item_data/remove")["ok"])
        self.assertEqual("preserve", sentinel.read_text())


class WriterApiTests(unittest.TestCase):
    def test_cli_stdin_rejects_non_payload_commands_before_resources(self):
        script = Path(__file__).parents[1] / "dev-server.py"
        with tempfile.TemporaryDirectory(prefix="writer-cli-") as directory:
            root = Path(directory) / "not-created"
            for command in ("init", "supervise"):
                response = subprocess.run([sys.executable, "-B", str(script), command, "--root", str(root), "--payload-stdin"],
                                          input="{}", capture_output=True, text=True, encoding="utf-8", timeout=5)
                self.assertNotEqual(0, response.returncode)
                self.assertEqual("INVALID_INPUT", json.loads(response.stdout)["error"]["code"])
                self.assertFalse(root.exists())

    def test_success_false_requires_response_and_peer_close(self):
        peer = ApiPeer("complete")
        checks = []
        response = exchange(peer.endpoint, {"type": "money_history_save"}, "transport-operation", 2, verify=lambda: checks.append(True))
        peer.finish()
        self.assertFalse(response["success"])
        self.assertEqual("transport-operation", response["nonce"])
        self.assertTrue(peer.assert_path)
        self.assertEqual(2, len(checks))
