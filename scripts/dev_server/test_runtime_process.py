"""native識別・実socket・残存childを自己生成のPython親子で確認する。"""
import json
import ctypes
from contextlib import nullcontext
import importlib.util
import os
import socket
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from . import runtime_process
from .registry import RegistryError


@unittest.skipUnless(sys.platform in ("win32", "darwin"), "Windows/macOSのnative試験")
class NativeProcessTests(unittest.TestCase):
    def test_native_identity_sockets_and_child_survives_parent(self):
        with tempfile.TemporaryDirectory(prefix="runtime-native-") as directory:
            root = Path(directory).resolve()
            arguments = [sys.executable, "-B", str(Path(__file__).with_name("runtime_test_process.py")), "parent", str(root)]
            recorded = []
            with (root / "console.log").open("xb") as log:
                process = runtime_process.OwnedProcess()
                process.start(arguments, root, log, os.urandom(16).hex(),
                              lambda: recorded.append(runtime_process.identity(process.pid)))
                try:
                    deadline = time.monotonic() + 8
                    while not (root / "child.json").exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.03)
                    parent = json.loads((root / "parent.json").read_text())
                    child = json.loads((root / "child.json").read_text())
                    actual = runtime_process.identity(process.pid)
                    runtime_process.require_identity(recorded[0], actual)
                    self.assertEqual(str(root), actual["cwd"])
                    self.assertEqual(runtime_process.command_hash(arguments), actual["commandHash"])
                    self.assertIn(child["pid"], process.members())
                    self.assertEqual(
                        {("tcp", "127.0.0.1", parent["tcp"]), ("udp", "127.0.0.1", parent["udp"])},
                        {(item["protocol"], item["address"], item["port"]) for item in runtime_process.sockets(process.pid)})
                    for key in ("pid", "startId", "bootId", "cwd", "executable", "commandHash", "owner"):
                        with self.subTest(field=key), self.assertRaises(RegistryError):
                            runtime_process.require_identity(dict(actual, **{key: "unrelated"}), actual)
                    (root / "parent-exit").touch()
                    while process.poll() is None:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.03)
                    self.assertIsNone(runtime_process.identity(process.pid))
                    self.assertIn(child["pid"], process.members())
                    self.assertIsNotNone(runtime_process.identity(child["pid"]))
                finally:
                    (root / "parent-exit").touch(exist_ok=True)
                    (root / "child-exit").touch(exist_ok=True)
                    deadline = time.monotonic() + 18
                    while process.poll() is None or process.members():
                        if time.monotonic() >= deadline:
                            self.fail("自己生成の親子が期限内に自己終了しません。強制停止しません")
                        time.sleep(0.03)
                    process.close()

    def test_proof_time_uses_integer_milliseconds(self):
        self.assertEqual(1001, runtime_process.proof_start_millis("1970-01-01T00:00:01.001999Z"))
        with self.assertRaises(RegistryError):
            runtime_process.proof_start_millis("2026-01-01T00:00:00")

    @unittest.skipUnless(socket.has_ipv6, "OSのIPv6 socketが必要です")
    def test_native_ipv6_tcp_udp_and_ipv4_mapped_tables(self):
        with socket.socket(socket.AF_INET6) as tcp, socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as udp, socket.socket(socket.AF_INET6) as mapped:
            tcp.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            udp.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            mapped.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            tcp.bind(("::1", 0))
            tcp.listen()
            udp.bind(("::1", 0))
            mapped.bind(("::ffff:127.0.0.1", 0))
            mapped.listen()
            actual = {(value["protocol"], value["address"], value["port"]) for value in runtime_process.sockets(os.getpid())}
            self.assertTrue({("tcp", "::1", tcp.getsockname()[1]), ("udp", "::1", udp.getsockname()[1]),
                             ("tcp", "127.0.0.1", mapped.getsockname()[1])} <= actual, actual)


class MacAdapterFaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("dev_server.mac_process_fault_test", runtime_process.__file__)
        cls.adapter = importlib.util.module_from_spec(spec)
        with patch("os.name", "posix"):
            spec.loader.exec_module(cls.adapter)

    def test_listpids_errno_failure_never_proves_empty_children(self):
        adapter = self.adapter
        process = adapter.OwnedProcess()
        process.pid = 123
        for failing_call in (1, 2):
            calls = []
            def listpids(kind, pid, data, size):
                self.assertEqual(0, ctypes.get_errno())
                calls.append(size)
                if len(calls) == failing_call:
                    ctypes.set_errno(5)
                return 0
            lib = Mock(proc_listpids=Mock(side_effect=listpids))
            with self.subTest(call=failing_call), patch.object(adapter, "library", return_value=lib), self.assertRaises(RegistryError):
                process.members()
            self.assertEqual(failing_call, len(calls))
        ctypes.set_errno(5)
        with patch.object(adapter, "library", return_value=Mock(proc_listpids=Mock(return_value=0))):
            self.assertEqual([], process.members())

    def test_callback_failure_retains_macos_process_without_signals(self):
        process = self.adapter.OwnedProcess()
        native = Mock(pid=123)
        with patch.object(self.adapter.subprocess, "Popen", return_value=native), \
                self.assertRaises(OSError):
            process.start(["python"], Path.cwd(), None, "nonce", lambda: (_ for _ in ()).throw(OSError("記録失敗")))
        self.assertIs(native, process.process)
        self.assertEqual((123, 123, "CREATED"), (process.pid, process.group, process.stage))
        native.terminate.assert_not_called()
        native.kill.assert_not_called()
        native.wait.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows native生成APIの故障注入")
class WindowsCreationFaultTests(unittest.TestCase):
    def test_identity_record_and_job_assignment_failure_retain_suspended_handles(self):
        adapter = runtime_process
        for failure in ("identity", "record", "job"):
            process = adapter.OwnedProcess()
            def created():
                self.assertEqual((123, 124, 125, "CREATED"), (process.handle, process.thread, process.pid, process.stage))
                if failure != "job":
                    raise OSError("識別失敗" if failure == "identity" else "記録失敗")
            with self.subTest(failure=failure), patch.object(adapter, "security_descriptor", return_value=nullcontext(None)), \
                    patch.object(adapter.kernel, "CreateJobObjectW", return_value=126), \
                    patch.object(adapter.kernel, "AssignProcessToJobObject", return_value=False) as assign, \
                    patch.object(adapter.kernel, "ResumeThread") as resume, \
                    patch.object(adapter._winapi, "CreateProcess", return_value=(123, 124, 125, 127)), \
                    patch.object(adapter._winapi, "CloseHandle") as close, \
                    patch.object(adapter.os, "set_handle_inheritable"), patch.object(adapter.msvcrt, "get_osfhandle", return_value=1), \
                    patch.object(adapter.ctypes, "get_last_error", return_value=5), self.assertRaises(OSError):
                with tempfile.TemporaryFile() as log:
                    process.start([sys.executable], Path.cwd(), log, "fixture", created)
            self.assertEqual((123, 124, 125, 126), (process.handle, process.thread, process.pid, process.job))
            self.assertEqual("VERIFIED" if failure == "job" else "CREATED", process.stage)
            self.assertEqual(1 if failure == "job" else 0, assign.call_count)
            resume.assert_not_called()
            close.assert_not_called()
