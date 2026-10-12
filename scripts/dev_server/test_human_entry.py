"""OS入口と合成SSH仲介による失敗保全。実SSH試験は別に記録する。"""
from copy import deepcopy
import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from .connection_profiles import profile_hash
from .human_remote import RemoteHuman
from .human_server import HumanStore, LocalTarget
from .registry import RegistryError
from .local_ipc import MAX_MESSAGE, encode
from .ssh_broker import Broker
from . import test_human_server as fixtures

SCRIPTS = Path(__file__).parents[1]

@unittest.skipUnless(sys.platform in ("win32", "darwin"), "対象OSの入口")
class WrapperTests(unittest.TestCase):
    def test_arguments_streams_and_exit_code(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp" if sys.platform == "darwin" else None) as directory:
            parent = Path(directory) / "日本語 空白"
            parent.mkdir()
            suffix = "ps1" if sys.platform == "win32" else "sh"
            script = parent / ("human-server." + suffix)
            shutil.copyfile(SCRIPTS / script.name, script)
            (parent / "human-server.py").write_text("import json,sys\nsys.stdout.reconfigure(encoding='utf-8')\nsys.stderr.reconfigure(encoding='utf-8')\nprint(json.dumps(sys.argv[1:],ensure_ascii=False))\nprint('日本語 エラー',file=sys.stderr)\nsys.exit(7)\n", encoding="utf-8")
            command = (["powershell.exe", "-NoLogo", "-NoProfile", "-File", str(script), "-Python", sys.executable]
                       if sys.platform == "win32" else ["sh", str(script), "--python", sys.executable])
            arguments = ["--json", "日本語 引数", '引用"符', "", "末尾\\"]
            def invoke(values):
                if sys.platform == "win32":
                    quoted = ["'" + value.replace("'", "''") + "'" for value in [str(script), sys.executable] + values]
                    body = "$ProgressPreference = 'SilentlyContinue'; & " + quoted[0] + " -Python " + quoted[1] + " " + " ".join(quoted[2:]) + "; exit $LASTEXITCODE"
                    call = ["powershell.exe", "-NoLogo", "-NoProfile", "-OutputFormat", "Text", "-EncodedCommand", base64.b64encode(body.encode("utf-16-le")).decode()]
                else:
                    call = command + values
                return subprocess.run(call, capture_output=True, timeout=20)
            result = invoke(arguments)
            self.assertEqual(7, result.returncode, result.stderr)
            self.assertEqual(arguments, json.loads(result.stdout))
            self.assertEqual("日本語 エラー", result.stderr.decode("utf-8").strip())
            result = invoke([])
            self.assertEqual(["--human-readable"], json.loads(result.stdout))

@unittest.skipUnless(sys.platform in ("win32", "darwin"), "対象OSのprivate記録")
class HumanEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HumanServerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.profile = self.fixture.store, deepcopy(self.fixture.profile)
        self.profile.update(transport="ssh", sshUser="approved-user")
        self.store.connections.save_profile(self.profile, replace=True)
        self.connections, self.brokers, self.lost = [], [], None
        test = self
        class Loopback:
            def __init__(self, profile, instance, client, **kwargs):
                if test.lost == "hello":
                    test.lost = None
                    raise RegistryError("SSH_UNAVAILABLE", "合成接続失敗")
                self.broker = Broker(test.fixture.fixture.root, profile, instance, client)
                test.brokers.append(self.broker)
                test.connections.append(self)
                self.unknown = False
            def request(self, command, payload, *, operation_id, timeout):
                answer = self.broker.execute({"version": 1, "command": command, "payload": payload, "operationId": operation_id})
                if test.lost == command:
                    test.lost = None
                    raise RegistryError("SSH_UNAVAILABLE", "合成応答喪失")
                return answer
            def _unknown(self):
                self.unknown = True
                self.broker.abandon()
        for probe in (patch("dev_server.human_remote.LocalTarget", side_effect=lambda profile, **kwargs: fixtures.SyntheticTarget(profile, self.fixture.registry)),
                      patch("dev_server.human_remote.SshConnection", Loopback)):
            probe.start()
            self.addCleanup(probe.stop)
        self.remote, self.options = self.client(), self.fixture.options
    def client(self):
        return RemoteHuman(self.store, self.profile, "persistent-01", "human-test", known_hosts=Path("known_hosts"))
    def host_store(self):
        return HumanStore(self.fixture.fixture.root / "manager/human-clients" / self.remote.client_id)
    def error(self, code, call):
        with self.assertRaises(RegistryError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)
    def test_start_restart_save_stop_share_generation(self):
        started = self.remote.run("start", self.options, action_id="start")
        restarted = self.remote.run("restart", self.options, action_id="restart")
        self.assertEqual(started["generation"], restarted["generation"])
        stopped = self.remote.run("stop", dict(self.options, withoutSnapshot=True), action_id="stop")
        self.assertIsNone(stopped["expiresAt"])
        self.assertTrue((self.fixture.fixture.root / "instances/persistent-01/generations" / started["generation"]).exists())
        self.assertEqual(2, self.fixture.fixture.rcon_calls)
        self.assertTrue(all(item.state == "CLOSED" for item in self.brokers))
        self.assertNotIn("leaseId", repr(started))
    def test_large_invalid_utf8_log_tail_stays_within_json_frame(self):
        self.remote.run("start", self.options, action_id="start")
        run_id = self.fixture.target.status("persistent-01")["instances"][0]["runId"]
        path = self.fixture.fixture.root / "instances/persistent-01/diagnostics" / run_id / "console.log"
        path.write_bytes(b"\xff\n" * 60000)
        result = self.remote.read("logs", self.options)
        self.assertIn("\ufffd", result["text"])
        self.assertLess(len(encode({"ok": True, "result": result})), MAX_MESSAGE)

    def test_lost_response_same_id_resume_without_second_jvm(self):
        self.lost = "human"
        self.error("SSH_UNAVAILABLE", lambda: self.remote.run("start", self.options, action_id="start"))
        self.assertEqual("SENDING", self.store.read("actions", "start")["state"])
        self.assertEqual("COMPLETED", self.host_store().read("actions", "start")["state"])
        self.error("ACTION_PENDING", lambda: self.client().run("start", self.options, action_id="start"))
        self.client().run("start", self.options, action_id="start", resume=True)
        self.assertEqual(1, len(self.fixture.fixture.adapter.created))
        self.assertTrue(self.connections[0].unknown)
    def test_lost_hello_resume_can_send_the_same_id_for_first_host_acceptance(self):
        self.lost = "hello"
        self.error("SSH_UNAVAILABLE", lambda: self.remote.run("start", self.options, action_id="start"))
        self.assertFalse((self.fixture.fixture.root / "manager/human-clients").exists())
        self.assertEqual("SENDING", self.store.read("actions", "start")["state"])
        self.client().run("start", self.options, action_id="start", resume=True)
        self.assertEqual(1, len(self.fixture.fixture.adapter.created))

    def test_lost_close_preserves_intent_and_stable_client(self):
        self.lost = "close"
        self.error("SSH_UNAVAILABLE", lambda: self.remote.run("start", self.options, action_id="start"))
        second = self.client()
        self.assertEqual(self.remote.client_id, second.client_id)
        second.run("start", self.options, action_id="start", resume=True)
        self.assertEqual(1, len(self.fixture.fixture.adapter.created))
    def test_pending_host_blocks_new_request_and_status_shows_operation(self):
        original = fixtures.SyntheticTarget.request
        def lost_acquire(target, command, *args, **kwargs):
            result = original(target, command, *args, **kwargs)
            if command == "acquire":
                raise RegistryError("UNAVAILABLE", "合成喪失")
            return result
        with patch.object(fixtures.SyntheticTarget, "request", lost_acquire):
            self.error("UNAVAILABLE", lambda: self.remote.run("start", self.options, action_id="start"))
        status = self.remote.read("status", self.options)
        self.assertEqual("start", status["pendingActionId"])
        self.assertIsNotNone(status["pendingRequest"]["operationId"])
        self.error("ACTION_PENDING", lambda: self.remote.run("start", self.options, action_id="other"))
        self.assertFalse(self.fixture.fixture.adapter.created)
    def test_client_unknown_restart_is_visible_and_blocks_new_id_and_handoff(self):
        self.remote.run("start", self.options, action_id="start")
        self.lost = "human"
        self.error("SSH_UNAVAILABLE", lambda: self.remote.run("restart", self.options, action_id="restart"))
        status = self.remote.read("status", self.options)
        self.assertIsNone(status["pendingActionId"])
        self.assertEqual("restart", status["clientPendingRequests"][0]["actionId"])
        count = len(self.connections)
        self.error("ACTION_PENDING", lambda: self.remote.run("restart", self.options, action_id="another"))
        self.error("ACTION_PENDING", lambda: self.remote.handoff("manager-test", self.options))
        self.assertEqual(count, len(self.connections))
        self.remote.run("restart", self.options, action_id="restart", resume=True)
        self.assertEqual(2, len(self.fixture.fixture.adapter.created))
        self.assertEqual([], self.remote.read("status", self.options)["clientPendingRequests"])

    def test_recover_resolves_client_request_only_after_host_confirmation(self):
        original = fixtures.SyntheticTarget.request
        def lost_prepare(target, command, *args, **kwargs):
            result = original(target, command, *args, **kwargs)
            if command == "prepare":
                raise RegistryError("UNAVAILABLE", "合成応答喪失")
            return result
        with patch.object(fixtures.SyntheticTarget, "request", lost_prepare):
            self.error("UNAVAILABLE", lambda: self.remote.run("start", self.options, action_id="start"))
        action = self.host_store().read("actions", "start")
        step = next(item for item in action["steps"] if item["command"] == "prepare")
        self.fixture.registry.clock = lambda: time.time() + 4000
        self.lost = "human"
        options = dict(self.options, targetOperationId=step["operationId"])
        self.error("SSH_UNAVAILABLE", lambda: self.remote.run("recover", options, action_id="recover"))
        self.assertEqual("SENDING", self.store.read("actions", "start")["state"])
        self.remote.run("recover", options, action_id="recover", resume=True)
        self.assertEqual("RESOLVED", self.store.read("actions", "start")["state"])
        self.error("ACTION_RESOLVED", lambda: self.remote.run("start", self.options, action_id="start", resume=True))
        self.assertEqual("RESOLVED", self.store.read("actions", "start")["state"])
        self.remote.run("start", dict(self.options, mode="keep", acquire=False), action_id="kept")
        self.assertEqual(1, len(self.fixture.fixture.adapter.created))

    def test_changed_intent_and_profile_reject_before_connection(self):
        self.remote.run("start", self.options, action_id="start")
        count = len(self.connections)
        self.error("OPERATION_CONFLICT", lambda: self.remote.run("start", dict(self.options, seed=18), action_id="start"))
        self.profile["host"] = "another-host"
        self.error("PROFILE_CHANGED", self.client)
        self.assertEqual(count, len(self.connections))
    def test_handoff_has_only_one_private_offer(self):
        self.remote.run("start", self.options, action_id="start")
        offered = self.remote.handoff("manager-test", self.options)
        self.assertNotIn("leaseId", repr(offered))
        record = self.store.read("handoffs", offered["handoffId"])
        self.assertEqual("manager-test", record["targetClient"])
        self.assertEqual("human-test", record["owner"])
        self.assertEqual([], list((self.host_store().directory / "handoffs").glob("*.json")))
        self.assertEqual(1, len(list((self.store.directory / "handoffs").glob("*.json"))))
    def test_writer_rejects_human_before_host_store_creation(self):
        broker = Broker(self.fixture.fixture.root, self.profile, "persistent-01", "writer-test")
        broker.binding = {"test": True}
        answer = broker.execute({"version": 1, "command": "human", "operationId": "request", "payload": {
            "command": "start", "owner": "human-test", "options": self.options, "actionId": "request", "resume": False, "targetClient": None}})
        self.assertEqual("WRITER_PENDING", answer["error"]["code"])
        self.assertTrue(answer["error"]["rejectedBeforeEffect"])
        self.assertFalse((self.fixture.fixture.root / "manager/human-clients/writer-test").exists())
    def test_local_target_rejects_ssh_without_broker(self):
        self.error("HOST_MISMATCH", lambda: LocalTarget(self.profile))
    def test_python_probe_timeout_returns_actionable_error(self):
        profile = deepcopy(self.fixture.profile)
        with patch("dev_server.human_server.subprocess.run", side_effect=subprocess.TimeoutExpired([], 10)):
            self.error("UNAVAILABLE", lambda: LocalTarget(profile))

    def test_doctor_rejects_fixed_bundle_without_jdk_21(self):
        with patch.object(self.fixture.target, "artifact", return_value={"tools": [{"name": "jdk", "version": "20.0.2"}]}):
            self.error("JDK_REQUIRED", lambda: self.fixture.target.doctor("persistent-01", self.options["artifactId"]))
        self.assertFalse(self.fixture.fixture.adapter.created)

    def test_setup_saves_defaults_without_connection_or_acquire(self):
        known_hosts = Path(self.fixture.fixture.temporary.name) / "既確認 hosts"
        known_hosts.write_text("", encoding="utf-8")
        command = [sys.executable, "-B", str(SCRIPTS / "human-server.py"), "setup", "--directory", str(self.store.directory),
                   "--profile", self.profile["displayName"], "--instance", "永続枠 1", "--owner", "human-test", "--known-hosts", str(known_hosts)]
        result = subprocess.run(command, capture_output=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        saved = self.store.connections._read(self.store.directory / "launch.json")
        self.assertEqual(profile_hash(self.profile), saved["profileHash"])
        self.assertEqual("persistent-01", saved["instanceId"])
        self.assertNotIn("token", repr(saved))
        self.assertFalse(self.connections)
        self.assertFalse(self.fixture.fixture.adapter.created)

if __name__ == "__main__":
    unittest.main()
