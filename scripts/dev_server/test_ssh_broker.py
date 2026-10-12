"""合成runtime・実IPC/native子プロセス・使い捨てWebSocketによるSSH境界試験。"""
import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from .connection_profiles import ConnectionStore, validate_profile
from .local_ipc import MAX_MESSAGE, encode, decode, listener, require_private
from .registry import RegistryError
from .ssh_broker import Broker
from .ssh_connection import SshConnection, remote_command, ssh_arguments
from .ssh_protocol import read_frame, write_frame
from .supervisor import dispatch, new_request
from .test_connection_profiles import example_profile
from . import test_runtime_control as runtime_tests
from .test_writer_control import ApiPeer
from .writer_api import Monitor

SCRIPT = Path(__file__).parents[1] / "dev-server.py"


class SshProtocolTests(unittest.TestCase):
    def test_frame_fragmentation_unicode_and_size_bound(self):
        stream = io.BytesIO()
        document = {"日本語": "空白 \n Unicode"}
        write_frame(stream, document)
        stream.seek(0)
        self.assertEqual(document, read_frame(stream))
        for value in (b"", b"\0\0\0\x10{}", (MAX_MESSAGE + 1).to_bytes(4, "big")):
            with self.assertRaises((EOFError, RegistryError)):
                read_frame(io.BytesIO(value))
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(b'\0\0\0\x0d{"x":1,"x":2}'))

    def test_fixed_command_quotes_paths_without_shell_injection(self):
        profile = example_profile("macos", "ssh")
        profile["cli"] = "/Users/example/引用 ' $(touch bad); ツール/dev-server.py"
        words = shlex.split(remote_command(profile))
        self.assertEqual(["cd", "--", str(PurePosixPath(profile["cli"]).parent), "&&", "exec", profile["python"], "-B", profile["cli"], "ssh-broker", "--root", profile["managementRoot"]], words)
        profile = example_profile("windows", "ssh")
        script = base64.b64decode(remote_command(profile).split()[-1]).decode("utf-16-le")
        self.assertIn(profile["python"], script)
        self.assertIn("CopyToAsync", script)
        self.assertNotIn("Invoke-Expression", script)

    def test_host_key_forwarding_and_login_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            hosts = Path(directory) / "known hosts"
            hosts.write_text("approved test key")
            profile = example_profile("macos", "ssh")
            args = ssh_arguments(profile, hosts)
        for option in ("StrictHostKeyChecking=yes", "BatchMode=yes", "ForwardAgent=no", "ClearAllForwardings=yes",
                       "PermitLocalCommand=no", "ControlPath=none", "KnownHostsCommand=none"):
            self.assertIn(option, args)
        self.assertEqual(profile["sshUser"], args[args.index("-l") + 1])
        for user in ("", "-option", "user;whoami", "SECRET-MARKER\n"):
            profile["sshUser"] = user
            with self.assertRaises(RegistryError) as error:
                validate_profile(profile)
            self.assertNotIn("SECRET-MARKER", str(error.exception))

    @unittest.skipUnless(sys.platform == "win32", "Windows native標準stream試験")
    def test_windows_launcher_preserves_binary_stream_and_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "引用 ' 空白"
            source.mkdir()
            script = source / "dev-server.py"
            script.write_text("import sys\nb=sys.stdin.buffer.read()\nsys.stdout.buffer.write(b)\n", encoding="utf-8")
            profile = example_profile("windows", "ssh")
            profile.update(python=sys.executable, cli=str(script))
            command = remote_command(profile).split()
            body = b"\x00\xff\r\n" + "日本語".encode()
            result = subprocess.run(command, input=body, capture_output=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(body, result.stdout)


class MonitorPeer(ApiPeer):
    def run(self):
        self.requests = []
        try:
            with self.socket, self.socket.accept()[0] as stream:
                stream.settimeout(3)
                def read(size):
                    result = bytearray()
                    while len(result) < size:
                        part = stream.recv(size - len(result))
                        if not part:
                            raise EOFError()
                        result.extend(part)
                    return bytes(result)
                def receive():
                    first, second = read(2)
                    length = second & 127
                    if length == 126:
                        length = struct.unpack('!H', read(2))[0]
                    mask = read(4)
                    return first & 15, bytes(value ^ mask[index % 4] for index, value in enumerate(read(length)))
                def send(opcode, body):
                    assert len(body) < 126
                    stream.sendall(bytes([128 | opcode, len(body)]) + body)
                header = bytearray()
                while not header.endswith(b'\r\n\r\n'):
                    header.extend(read(1))
                key = next(line.split(':', 1)[1].strip() for line in header.decode().split('\r\n') if line.startswith('Sec-WebSocket-Key:'))
                import hashlib
                accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                stream.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode())
                for subscribed in (True, False):
                    opcode, body = receive()
                    message = decode(body)
                    self.requests.append(message['type'])
                    assert opcode == 1 and message['type'] == ('monitor_subscribe' if subscribed else 'monitor_unsubscribe')
                    send(1, encode({'type': 'monitor_subscription', 'subscribed': subscribed, 'nonce': message['nonce']}))
                    if subscribed:
                        send(1, encode({'type': 'monitor_update', 'server': {'players': 1}, 'jvm': {}}))
                opcode, body = receive()
                assert opcode == 8
                if self.mode != 'lost-close':
                    send(8, body)
        except BaseException as error:
            self.error = error


@unittest.skipUnless(sys.platform in ('darwin', 'win32'), '対象OSのprivate証跡')
class SshClientFailureTests(unittest.TestCase):
    def test_response_loss_wrong_id_and_auth_failure_are_not_retried(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp' if sys.platform == 'darwin' else None) as temporary:
            directory = Path(temporary)
            private = ConnectionStore(directory / 'client-private')
            known_hosts = directory / 'known_hosts'
            known_hosts.write_text('synthetic')
            profile = example_profile('macos', 'ssh')
            original = subprocess.Popen
            for mode in ('lost-response', 'wrong-id', 'auth-failure', 'blocked-write'):
                code = ('import sys\nsys.path.insert(0,' + repr(str(SCRIPT.parent)) + ')\n'
                        'from dev_server.ssh_protocol import read_frame,write_frame\n'
                        'from dev_server.connection_profiles import profile_hash\n'
                        'if ' + repr(mode) + '=="auth-failure":\n sys.stderr.write("SECRET-MARKER");sys.exit(255)\n'
                        'h=read_frame(sys.stdin.buffer)\nwrite_frame(sys.stdout.buffer,{"ok":True,"operationId":h["operationId"],"result":{"profileHash":profile_hash(h["payload"]["profile"])}})\n'
                        'if ' + repr(mode) + '=="blocked-write":\n import time;time.sleep(3);sys.stdin.buffer.read();sys.exit(0)\n'
                        'r=read_frame(sys.stdin.buffer)\n'
                        'if ' + repr(mode) + '=="wrong-id":\n write_frame(sys.stdout.buffer,{"ok":True,"operationId":"wrong-id","result":{}})\n'
                        'sys.stdin.buffer.read()\n')
                children = []
                def launch(*args, **kwargs):
                    child = original([sys.executable, '-B', '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                    children.append(child)
                    return child
                connection = None
                try:
                    with patch('dev_server.ssh_connection.subprocess.Popen', side_effect=launch):
                        if mode == 'auth-failure':
                            with self.assertRaises(RegistryError) as error:
                                SshConnection(profile, 'persistent-01', 'manager-01', known_hosts=known_hosts, journal_parent=private.directory)
                            self.assertNotIn('SECRET-MARKER', str(error.exception))
                            continue
                        connection = SshConnection(profile, 'persistent-01', 'manager-01', known_hosts=known_hosts, journal_parent=private.directory)
                    started = time.monotonic()
                    with self.assertRaises(RegistryError):
                        if mode == 'blocked-write':
                            connection.request('io', {'request': {'action': 'write', 'path': 'item_data/blocked.yml', 'text': 'x' * 60000}}, operation_id='lost-request', timeout=1)
                        else:
                            connection.request('status', {'instanceId': 'persistent-01'}, operation_id='lost-request', timeout=1)
                    if mode == 'blocked-write':
                        self.assertLess(time.monotonic() - started, 2.5)
                    self.assertTrue(connection.unknown)
                    with self.assertRaises(RegistryError):
                        connection.request('status', {'instanceId': 'persistent-01'}, operation_id='second-request')
                    self.assertFalse((connection.journal.directory / 'second-request.json').exists())
                    self.assertEqual('UNKNOWN', json.loads((connection.journal.directory / 'connection.json').read_bytes())['state'])
                finally:
                    for child in children:
                        child.wait(timeout=5)


@unittest.skipUnless(sys.platform in ("win32", "darwin"), "対象OSのnative/IPC/private root試験")
class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_tests.RuntimeControlTests("test_prepare_start_stop_replay_restart_and_no_snapshot_clean")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.generation = self.fixture.prepare()
        self.fixture.start()
        started = self.fixture.command("status", {"instanceId": "persistent-01"})["instances"][0]
        self.profile = example_profile("windows" if os.name == "nt" else "macos", "ssh")
        self.profile.update(managementRoot=str(self.fixture.root), python=sys.executable, cli=str(SCRIPT))
        self.open_payload = dict(self.fixture.token, owner="codex-test", sessionId="ssh-session-01",
                                 runId=started["runId"], nonce=started["nonce"])
        self.call = lambda command, payload, operation: dispatch(self.fixture.registry, new_request(command, payload, operation_id=operation))
        self.broker = Broker(self.fixture.root, self.profile, "persistent-01", "manager-01", call=self.call)
        self.number = 0

    def request(self, command, payload, operation=None, broker=None):
        self.number += 1
        return (broker or self.broker).execute({"version": 1, "command": command, "operationId": operation or f"ssh-{self.number}", "payload": payload})

    def open(self, broker=None, payload=None):
        response = self.request("open", payload or self.open_payload, broker=broker)
        self.assertTrue(response["ok"], response)

    def test_native_self_writer_io_replay_and_normal_close(self):
        self.open()
        with self.fixture.registry.transaction(read_only=True) as db:
            self.assertEqual(os.getpid(), db.execute("SELECT pid FROM writer_sessions").fetchone()[0])
        body = {"request": {"action": "write", "path": "item_data/共有.yml", "text": "id: 一つ目"}}
        self.assertTrue(self.request("io", body, "save-once")["ok"])
        self.assertEqual("REQUEST_REPLAY", self.request("io", body, "save-once")["error"]["code"])
        self.assertEqual("id: 一つ目", self.request("io", {"request": {"action": "read", "path": "item_data/共有.yml"}})["result"]["text"])
        self.assertTrue(self.request("close", {})["ok"])
        self.assertEqual("CLOSED", self.broker.state)
        with self.fixture.registry.transaction(read_only=True) as db:
            self.assertEqual(0, db.execute("SELECT writers FROM instances WHERE id='persistent-01'").fetchone()[0])
        self.assertEqual("LEASE_EXPIRED", self._expire())

    def test_handoff_renews_and_opens_a_fresh_writer_without_new_lease(self):
        self.open()
        renewed = self.request("renew", dict(self.fixture.token, ttlSeconds=40))
        self.assertTrue(renewed["ok"], renewed)
        checked = self.request("lease-check", dict(self.fixture.token, owner="codex-test"))["result"]
        store = ConnectionStore(Path(self.fixture.temporary.name) / "handoff-private")
        offered = store.offer(self.profile, self.fixture.token, checked, owner="codex-test",
                              source_client="manager-01", target_client="manager-02", now=100)
        self.assertTrue(self.request("close", {})["ok"])
        second = Broker(self.fixture.root, self.profile, "persistent-01", "manager-02", call=self.call)
        claimed = store.claim(self.profile, offered, owner="codex-test", target_client="manager-02",
                              lease_check=lambda payload: self.request("lease-check", payload, broker=second)["result"],
                              clock=lambda: 100)
        self.open(second, dict(claimed["token"], owner=claimed["owner"], sessionId=claimed["sessionId"],
                               runId=claimed["runId"], nonce=claimed["nonce"]))
        self.assertTrue(self.request("close", {}, broker=second)["ok"])
        self.assertEqual(self.fixture.token, claimed["token"])
        with self.assertRaises(RegistryError):
            store.claim(self.profile, offered, owner="codex-test", target_client="manager-02",
                        lease_check=lambda payload: checked, clock=lambda: 100)

    def test_closed_writer_cannot_open_monitor_and_check_has_no_write(self):
        self.open()
        database = self.fixture.root / "manager/registry.sqlite"
        before = database.read_bytes()
        self.assertEqual({"sessionId": "ssh-session-01", "open": True},
                         self.call("writer-check", self.broker.binding, "check-native")["result"])
        self.assertEqual(before, database.read_bytes())
        self.assertTrue(self.call("writer-close", dict(self.broker.binding, drained=True, disconnected=True), "external-close")["ok"])
        with patch("dev_server.ssh_broker.Monitor") as monitor:
            response = self.request("monitor-open", {"intervalTicks": 20})
        self.assertFalse(response["ok"], response)
        monitor.assert_not_called()

    def _expire(self):
        self.fixture.now = 120
        response = self.call("lease-check", dict(self.fixture.token, owner="codex-test"), "after-close-expired")
        return response["error"]["code"]

    def test_foreign_pid_slot_root_secret_and_shell_are_rejected(self):
        self.assertTrue(self.request("open", dict(self.open_payload, pid=999))["error"]["rejectedBeforeEffect"])
        for command, payload in (("shell", {"command": "SECRET-MARKER"}), ("status", {"instanceId": "persistent-02"})):
            self.assertFalse(self.request(command, payload)["ok"])
        self.open()
        for path in ("../server.properties", "/tmp/secret", "server.properties", "player_data/private.yml"):
            response = self.request("io", {"request": {"action": "write", "path": path, "text": "SECRET-MARKER"}})
            self.assertTrue(response["error"]["rejectedBeforeEffect"], response)
        records = list(self.broker.journal.directory.glob("*.json"))
        for path in records:
            require_private(path)
            self.assertNotIn("SECRET-MARKER", path.read_text(encoding="utf-8"))
            self.assertNotIn(self.fixture.token["leaseId"], path.read_text(encoding="utf-8"))

    def test_stale_token_run_owner_and_lost_response_hold_writer(self):
        for changes in ({"epoch": 999}, {"expectedGeneration": "old-generation"}, {"runId": "old-run"}, {"owner": "other"}):
            broker = Broker(self.fixture.root, self.profile, "persistent-01", "manager-02", call=self.call)
            response = self.request("open", dict(self.open_payload, **changes), broker=broker)
            self.assertFalse(response["ok"], response)
        self.open()
        original = self.broker.call
        def lose(command, payload, operation):
            response = original(command, payload, operation)
            if command == "writer-io":
                raise TimeoutError()
            return response
        self.broker.call = lose
        body = {"request": {"action": "write", "path": "item_data/once.yml", "text": "id: once"}}
        self.assertFalse(self.request("io", body, "lost-save")["ok"])
        self.assertEqual("UNKNOWN", self.broker.state)
        self.assertFalse(self.request("close", {})["ok"])
        with self.fixture.registry.transaction(read_only=True) as db:
            self.assertEqual(1, db.execute("SELECT writers FROM instances WHERE id='persistent-01'").fetchone()[0])
        self.assertEqual("UNKNOWN", json.loads((self.broker.journal.directory / "lost-save.json").read_bytes())["state"])

    def test_two_connections_two_slots_do_not_mix_save_or_response(self):
        token = self.fixture.acquire("persistent-02", "second-acquire")
        prepared = self.fixture.command("prepare", dict(token, artifactId=self.fixture.artifact, generationId="generation-02", seed=18, acceptEula=True), "second-prepare")
        token["expectedGeneration"] = prepared["generation"]
        self.fixture.command("start", dict(token, timeoutSeconds=2), "second-start")
        started = self.fixture.command("status", {"instanceId": "persistent-02"})["instances"][0]
        profile = dict(self.profile, instances=[*self.profile["instances"], {"instanceId": "persistent-02", "displayName": "2"}])
        second = Broker(self.fixture.root, profile, "persistent-02", "manager-02", call=self.call)
        self.open()
        self.open(second, dict(token, owner="codex-test", sessionId="ssh-session-02", runId=started["runId"], nonce=started["nonce"]))
        for broker, text in ((self.broker, "first"), (second, "second")):
            self.assertTrue(self.request("io", {"request": {"action": "write", "path": "item_data/same.yml", "text": text}}, broker=broker)["ok"])
        for broker, text in ((self.broker, "first"), (second, "second")):
            self.assertEqual(text, self.request("io", {"request": {"action": "read", "path": "item_data/same.yml"}}, broker=broker)["result"]["text"])
        self.assertNotEqual(self.broker.journal.directory, second.journal.directory)

    def test_peer_close_failure_prevents_writer_close(self):
        self.open()
        monitor = Mock()
        monitor.close.side_effect = TimeoutError()
        self.broker.monitor = monitor
        self.assertFalse(self.request("close", {})["ok"])
        self.assertEqual("UNKNOWN", self.broker.state)
        with self.fixture.registry.transaction(read_only=True) as db:
            self.assertEqual("OPEN", db.execute("SELECT status FROM writer_sessions").fetchone()[0])
        self.broker.abandon()
        monitor.abort.assert_called_once()

    def test_command_api_preserves_success_false_after_peer_close(self):
        from .writer_api import exchange
        self.open()
        peer = ApiPeer("complete")
        def invoke(endpoint, message, operation_id, timeout, *, verify):
            return exchange(peer.endpoint, message, operation_id, timeout, verify=verify)
        with patch("dev_server.writer_api.exchange", side_effect=invoke):
            response = self.request("io", {"request": {"action": "api", "message": {"type": "money_history_save"}, "timeoutSeconds": 2}})
        peer.finish()
        self.assertTrue(response["ok"], response)
        self.assertFalse(response["result"]["apiResponse"]["success"])

    def test_monitor_is_read_only_and_requires_peer_close_before_writer_close(self):
        self.open()
        peer = MonitorPeer('complete')
        original = Monitor
        with patch('dev_server.ssh_broker.Monitor', side_effect=lambda endpoint, verify: original(peer.endpoint, verify=verify)):
            self.assertTrue(self.request('monitor-open', {'intervalTicks': 20})['ok'])
        event = self.request('monitor-read', {'timeoutSeconds': 1})
        self.assertEqual('monitor_update', event['result']['events'][0]['type'])
        self.assertTrue(self.request('close', {})['ok'])
        peer.finish()
        self.assertEqual(['monitor_subscribe', 'monitor_unsubscribe'], peer.requests)

    def test_monitor_peer_close_missing_keeps_registered_writer(self):
        self.open()
        peer = MonitorPeer('lost-close')
        original = Monitor
        with patch('dev_server.ssh_broker.Monitor', side_effect=lambda endpoint, verify: original(peer.endpoint, verify=verify)):
            self.assertTrue(self.request('monitor-open', {'intervalTicks': 20})['ok'])
        self.assertFalse(self.request('close', {})['ok'])
        peer.finish()
        self.assertEqual('UNKNOWN', self.broker.state)
        with self.fixture.registry.transaction(read_only=True) as db:
            self.assertEqual('OPEN', db.execute('SELECT status FROM writer_sessions').fetchone()[0])
        self.broker.abandon()

    def test_native_child_over_private_ipc_registers_its_own_pid(self):
        stop = threading.Event()
        server_context = listener(self.fixture.root)
        server = server_context.__enter__()
        def serve():
            try:
                while not stop.is_set():
                    connection = server.accept(timeout=0.1)
                    if connection is None:
                        continue
                    try:
                        response = dispatch(self.fixture.registry, decode(connection.recv_bytes(MAX_MESSAGE)))
                        connection.send_bytes(encode(response))
                    finally:
                        connection.close()
            finally:
                server_context.__exit__(None, None, None)
        worker = threading.Thread(target=serve)
        worker.start()
        original = subprocess.Popen
        def launch(*args, **kwargs):
            return original([sys.executable, "-B", str(SCRIPT), "ssh-broker", "--root", str(self.fixture.root)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        private = ConnectionStore(Path(self.fixture.temporary.name) / "client-private")
        hosts = Path(self.fixture.temporary.name) / "known_hosts"
        hosts.write_text("synthetic")
        connection = None
        try:
            for mode in ('normal', 'eof', 'crash'):
                with self.subTest(mode=mode):
                    with patch("dev_server.ssh_connection.subprocess.Popen", side_effect=launch):
                        connection = SshConnection(self.profile, "persistent-01", "manager-01", known_hosts=hosts, journal_parent=private.directory)
                    payload = dict(self.open_payload, sessionId='child-' + mode)
                    opened = connection.request("open", payload, operation_id="child-open-" + mode)
                    self.assertTrue(opened["ok"], opened)
                    with self.fixture.registry.transaction(read_only=True) as db:
                        pid = db.execute("SELECT pid FROM writer_sessions WHERE id=?", (payload['sessionId'],)).fetchone()[0]
                        self.assertEqual(connection.process.pid, pid)
                        self.assertNotEqual(os.getpid(), pid)
                    if mode == 'normal':
                        self.assertTrue(connection.request("close", {}, operation_id="child-close")["ok"])
                        continue
                    # 試験だけでEOF・異常終了を注入する。製品側はkillや登録解除を行わない。
                    if mode == 'eof':
                        connection.process.stdin.close()
                    else:
                        connection.process.terminate()
                    self.assertNotEqual(0, connection.process.wait(timeout=5))
                    with self.assertRaises(RegistryError):
                        connection.request('status', {'instanceId': 'persistent-01'}, operation_id='after-' + mode)
                    self.assertTrue(connection.unknown)
                    with self.fixture.registry.transaction(read_only=True) as db:
                        self.assertEqual('OPEN', db.execute('SELECT status FROM writer_sessions WHERE id=?', (payload['sessionId'],)).fetchone()[0])
                        self.assertEqual(self.fixture.token['leaseId'], db.execute("SELECT lease FROM instances WHERE id='persistent-01'").fetchone()[0])
                    rejected = self.call('writer-io', dict({key: value for key, value in payload.items() if key != 'owner'},
                                         request={'action': 'write', 'path': 'item_data/dead.yml', 'text': 'never'}), 'dead-' + mode)
                    self.assertFalse(rejected['ok'], rejected)
        finally:
            if connection is not None and connection.process.poll() is None:
                connection.process.stdin.close()
                connection.process.wait(timeout=5)
            stop.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
