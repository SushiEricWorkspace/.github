"""合成Registry/保存証跡で一括操作の失敗保全を検証する。実Mod試験は別に行う。"""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from .build_lock import build_lock, BuildLockError
from .human_server import HumanServer, HumanStore, LocalTarget
from .local_ipc import decode, encode, require_private
from .registry import RegistryError
from .supervisor import dispatch, new_request
from .test_connection_profiles import example_profile
from .connection_profiles import profile_hash
from . import test_runtime_control as runtime_tests


class SyntheticTarget(LocalTarget):
    def __init__(self, profile, registry):
        self.profile, self.registry, self.root = profile, registry, registry.root
        self.calls = []
        self.lost_command = None

    def request(self, command, payload, operation_id, *, timeout=900, dry_run=False):
        self.calls.append((command, deepcopy(payload), operation_id, dry_run))
        response = dispatch(self.registry, new_request(command, payload, operation_id=operation_id, dry_run=dry_run))
        if self.lost_command == command:
            self.lost_command = None
            raise RegistryError("UNAVAILABLE", "合成応答喪失")
        if not response["ok"]:
            raise RegistryError(response["error"]["code"], "合成CLI拒否")
        return response["result"]

    def ensure_supervisor(self, store, action):
        pass


@unittest.skipUnless(sys.platform in ("win32", "darwin"), "対象OSのprivate保存試験")
class HumanServerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_tests.RuntimeControlTests("test_prepare_start_stop_replay_restart_and_no_snapshot_clean")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.registry = self.fixture.registry
        self.fixture.command("release", self.fixture.token, "release-fixture")
        self.registry.clock = time.time
        self.store = HumanStore(Path(self.fixture.temporary.name) / "人間 記録")
        self.profile = example_profile("windows" if sys.platform == "win32" else "macos")
        self.profile.update(host=socket.gethostname(), managementRoot=str(self.fixture.root),
                            python=sys.executable, cli=str(Path(__file__).parents[1] / "dev-server.py"))
        self.profile["approval"]["operator"] = self.registry.inventory["operator"]
        self.store.connections.save_profile(self.profile)
        self.target = SyntheticTarget(self.profile, self.registry)
        self.server = HumanServer(self.store, self.profile["profileId"], "human-test", "persistent-01", target=self.target)
        self.options = {"acquire": True, "artifactId": self.fixture.artifact, "mode": "prepare",
                        "templateId": None, "snapshotId": None, "withoutSnapshot": False, "acceptEula": True,
                        "seed": 17, "ttlSeconds": 3600, "timeoutSeconds": 2,
                        "recoveryAction": "resume", "targetOperationId": None}
        probe = patch("dev_server.human_server.prebind", side_effect=lambda endpoints: nullcontext())
        probe.start()
        self.addCleanup(probe.stop)

    def run_start(self, action="human-start", **options):
        return self.server.run("start", dict(self.options, **options), action_id=action)

    def error(self, code, action):
        with self.assertRaises(RegistryError) as caught:
            action()
        self.assertEqual(code, caught.exception.code)

    def test_prepare_restart_stop_and_release_preserve_generation(self):
        summary = self.run_start()
        self.assertEqual("RUNNING", self.server.status()["state"])
        generation = summary["generation"]
        restarted = self.server.run("restart", self.options, action_id="restart")
        self.assertEqual(generation, restarted["generation"])
        stopped = self.server.run("stop", dict(self.options, withoutSnapshot=True), action_id="stop")
        self.assertIsNone(stopped["expiresAt"])
        status = self.server.status()
        self.assertEqual("AVAILABLE", status["state"])
        self.assertEqual(generation, status["generation"])
        self.assertTrue((self.fixture.root / "instances/persistent-01/generations" / generation).exists())
        self.assertNotIn("leaseId", repr(summary))
        self.assertEqual(2, self.fixture.rcon_calls)

    def test_lost_acquire_stops_and_explicit_same_id_resume_does_not_acquire_twice(self):
        self.target.lost_command = "acquire"
        self.error("UNAVAILABLE", self.run_start)
        action = self.store.read("actions", "human-start")
        self.assertEqual("SENDING", action["steps"][0]["state"])
        self.assertFalse(self.fixture.adapter.created)
        self.error("ACTION_PENDING", lambda: self.run_start("new-id"))
        result = self.server.run("start", self.options, action_id="human-start", resume=True)
        self.assertEqual("COMPLETED", result["state"])
        acquires = [item for item in self.target.calls if item[0] == "acquire"]
        self.assertEqual(acquires[0][1:3], acquires[1][1:3])
        self.assertEqual(1, len(self.fixture.adapter.created))

    def test_lost_start_stop_release_replay_is_explicit(self):
        self.target.lost_command = "start"
        self.error("UNAVAILABLE", self.run_start)
        self.server.run("start", self.options, action_id="human-start", resume=True)
        options = dict(self.options, withoutSnapshot=True)
        self.target.lost_command = "stop"
        self.error("UNAVAILABLE", lambda: self.server.run("stop", options, action_id="stop"))
        self.assertIsNotNone(self.store.read("sessions", self.server.key)["token"])
        self.target.lost_command = "release"
        self.error("UNAVAILABLE", lambda: self.server.run("stop", options, action_id="stop", resume=True))
        self.server.run("stop", options, action_id="stop", resume=True)
        self.assertEqual(1, self.fixture.rcon_calls)

    def test_same_id_different_payload_rejected_before_send(self):
        self.run_start()
        count = len(self.target.calls)
        self.error("OPERATION_CONFLICT", lambda: self.run_start(seed=18))
        self.assertEqual(count, len(self.target.calls))
        self.assertEqual("COMPLETED", self.run_start()["state"])

    def test_reborrow_never_starts_old_generation_as_side_effect(self):
        self.run_start()
        self.server.run("stop", dict(self.options, withoutSnapshot=True), action_id="stop")
        self.error("RESTORE_REQUIRED", lambda: self.run_start("keep", mode="keep"))
        self.error("RESTORE_REQUIRED", lambda: self.run_start("prepare-old"))
        self.assertEqual(1, sum(item[0] == "acquire" for item in self.target.calls))

    def test_other_owner_is_not_adopted_even_when_expired(self):
        self.fixture.command("acquire", {"instanceId": "persistent-01", "owner": "other-owner", "ttlSeconds": 1}, "other")
        self.registry.clock = lambda: time.time() + 5
        self.error("UNAVAILABLE", self.run_start)
        self.assertFalse(any(item[0] == "acquire" for item in self.target.calls))

    def test_unknown_and_active_writers_block_stop_and_release(self):
        self.run_start()
        with patch.object(self.server, "checked", return_value={"generationBound": True, "state": "RUNNING",
                                                              "writers": 1, "pending": 0, "writerSessions": [{"status": "UNKNOWN"}]}):
            self.error("WRITER_PENDING", lambda: self.server.run("stop", dict(self.options, withoutSnapshot=True), action_id="stop"))
        self.assertFalse(any(item[0] in ("stop", "release") for item in self.target.calls))

    def test_save_failure_does_not_snapshot_or_release(self):
        self.run_start()
        def fail(process):
            process.proof["saves"]["success"] = False
            process.save_proof()
        self.fixture.stop_fault = fail
        self.error("SAVE_FAILED", lambda: self.server.run("stop", dict(self.options, snapshotId="save"), action_id="stop"))
        self.assertFalse(any(item[0] in ("snapshot", "release") for item in self.target.calls))
        self.assertIsNotNone(self.store.read("sessions", self.server.key)["token"])

    def test_port_collision_and_missing_eula_do_not_acquire(self):
        with patch("dev_server.human_server.prebind", side_effect=OSError("port競合")):
            with self.assertRaises(OSError):
                self.run_start("collision")
        self.error("EULA_REQUIRED", lambda: self.run_start("eula", acceptEula=False))
        self.assertFalse(any(item[0] == "acquire" for item in self.target.calls))

    def test_handoff_uses_current_token_without_double_acquire_and_renew_owner_is_explicit(self):
        self.run_start()
        handed = self.server.handoff("manager-test")
        record = self.store.connections._read(self.store.connections._path("handoffs", handed["handoffId"]))
        self.assertEqual("human-test", handed["renewOwner"])
        self.assertEqual("manager-test", record["targetClient"])
        self.assertEqual(1, sum(item[0] == "acquire" for item in self.target.calls))
        self.server.run("renew", self.options, action_id="renew")
        self.assertGreater(self.server.status()["expiresAt"], time.time())
        self.assertNotIn(record["token"]["leaseId"], repr(handed))

    def test_changed_profile_owner_and_private_permissions_rejected(self):
        self.run_start()
        other = HumanServer(self.store, self.profile["profileId"], "other-owner", "persistent-01", target=self.target)
        self.error("OWNER_MISMATCH", other.status)
        self.profile["displayName"] = "編集後"
        self.store.connections.save_profile(self.profile, replace=True)
        changed = HumanServer(self.store, self.profile["profileId"], "human-test", "persistent-01", target=self.target)
        self.error("PROFILE_CHANGED", changed.status)
        for path in self.store.directory.rglob("*.json"):
            require_private(path)
        self.assertNotIn("leaseId", self.store.path("profiles", self.profile["profileId"]).read_text(encoding="utf-8"))

    def test_competing_human_operation_does_not_write(self):
        with build_lock(self.store.lock):
            with self.assertRaises(BuildLockError):
                self.run_start()

    def test_supervisor_shutdown_is_bound_and_rejects_other_lease(self):
        with self.registry.transaction(read_only=True) as db:
            boot = db.execute("SELECT value FROM meta WHERE key='supervisorBoot'").fetchone()[0]
        self.error("STALE_SUPERVISOR", lambda: self.target.request("shutdown-idle", {"expectedBootId": "wrong"}, "wrong"))
        self.fixture.command("acquire", {"instanceId": "persistent-02", "owner": "other-owner", "ttlSeconds": 10}, "other")
        self.error("BUSY", lambda: self.target.request("shutdown-idle", {"expectedBootId": boot}, "idle"))
        with self.registry.transaction(read_only=True) as db:
            self.assertEqual("AVAILABLE", db.execute("SELECT state FROM instances WHERE id='persistent-01'").fetchone()[0])

    def test_planning_interruption_resumes_before_any_lease_request(self):
        with patch.object(self.target, "ensure_supervisor", side_effect=RegistryError("UNAVAILABLE", "合成監督未確認")):
            self.error("UNAVAILABLE", self.run_start)
        self.assertEqual("PLANNING", self.store.read("actions", "human-start")["state"])
        self.assertFalse(any(item[0] == "acquire" for item in self.target.calls))
        self.server.run("start", self.options, action_id="human-start", resume=True)
        self.assertEqual(1, sum(item[0] == "acquire" for item in self.target.calls))

    def test_committed_generation_lost_reply_recovers_after_expiry(self):
        self.target.lost_command = "prepare"
        self.error("UNAVAILABLE", self.run_start)
        pending = self.store.read("actions", "human-start")
        step = next(item for item in pending["steps"] if item["command"] == "prepare")
        self.registry.clock = lambda: time.time() + 4000
        self.error("LEASE_EXPIRED", lambda: self.server.run("start", self.options, action_id="human-start", resume=True))
        recovered = self.server.run("recover", dict(self.options, targetOperationId=step["operationId"]), action_id="recover")
        self.assertEqual(step["payload"]["generationId"], recovered["generation"])
        self.assertEqual("RESOLVED", self.store.read("actions", "human-start")["state"])
        self.run_start("kept", mode="keep", acquire=False)
        self.assertEqual(1, sum(item[0] == "acquire" for item in self.target.calls))

    def test_confirmed_supervisor_exit_before_private_commit_is_reconciled(self):
        with self.registry.transaction(read_only=True) as db:
            boot = db.execute("SELECT value FROM meta WHERE key='supervisorBoot'").fetchone()[0]
        record = {"profileHash": profile_hash(self.profile),
                  "state": "READY", "bootId": boot, "pid": 12345, "identity": {"pid": 12345},
                  "shutdown": {"operationId": "shutdown-record", "payload": {"expectedBootId": boot}}}
        self.store.write("supervisors", self.profile["profileId"], record)
        self.target.request("shutdown-idle", record["shutdown"]["payload"], "shutdown-record")
        with patch("dev_server.human_server.runtime_process.identity", return_value=None):
            result = self.target.shutdown(self.store)
        self.assertEqual("STOPPED", result["state"])
        self.assertEqual("STOPPED", self.store.read("supervisors", self.profile["profileId"])["state"])

    def test_macos_python_launcher_transition_requires_native_start_and_exact_command(self):
        target = self.target
        target.python_executable = "/approved/Python.app/Contents/MacOS/Python"
        launcher = {"pid": 12345, "startId": "native-start", "bootId": "os-boot", "owner": "owner",
                    "cwd": "/approved", "executable": "/approved/bin/python3.12", "commandHash": "launcher"}
        arguments = [target.python_executable, "-B", target.profile["cli"], "supervise", "--root", str(target.root)]
        from .runtime_process import command_hash
        actual = dict(launcher, executable=target.python_executable, commandHash=command_hash(arguments))
        with patch("dev_server.human_server.sys.platform", "darwin"):
            with patch("dev_server.human_server.runtime_process.identity", return_value=actual):
                record = {"pid": 12345, "identity": launcher}
                target.bind_supervisor(record)
                self.assertEqual(actual, record["identity"])
            for changed in (dict(actual, startId="reused"), dict(actual, commandHash="other-command"),
                            dict(actual, executable="/other/Python")):
                with patch("dev_server.human_server.runtime_process.identity", return_value=changed):
                    self.error("PROCESS_MISMATCH", lambda: target.bind_supervisor({"pid": 12345, "identity": launcher}))

    def test_reachable_creating_supervisor_is_bound_to_saved_native_identity_and_boot(self):
        with self.registry.transaction(read_only=True) as db:
            boot = db.execute("SELECT value FROM meta WHERE key='supervisorBoot'").fetchone()[0]
        identity = {"pid": 12345, "startId": "native-start"}
        record = {"profileHash": profile_hash(self.profile), "state": "CREATING", "bootId": None,
                  "pid": 12345, "identity": identity, "actionId": "planning"}
        self.store.write("supervisors", self.profile["profileId"], record)
        from . import storage
        log = self.store.directory / "supervisors" / (self.profile["profileId"] + ".log")
        storage.write(log, encode({"result": {"supervisor": "ready", "pid": 12345, "bootId": boot}}) + b"\n")
        with patch("dev_server.human_server.runtime_process.identity", return_value=identity):
            LocalTarget.ensure_supervisor(self.target, self.store, {"actionId": "planning"})
        self.assertEqual("READY", self.store.read("supervisors", self.profile["profileId"])["state"])
        self.assertEqual(boot, self.store.read("supervisors", self.profile["profileId"])["bootId"])

    def test_idle_shutdown_excludes_inflight_operation_and_new_requests(self):
        with self.registry.transaction(read_only=True) as db:
            boot = db.execute("SELECT value FROM meta WHERE key='supervisorBoot'").fetchone()[0]
        with self.registry.resource_use():
            self.error("BUSY", lambda: self.target.request("shutdown-idle", {"expectedBootId": boot}, "shutdown-idle"))
        self.target.request("shutdown-idle", {"expectedBootId": boot}, "shutdown-idle")
        self.error("BUSY", lambda: self.target.request("acquire", {"instanceId": "persistent-01",
                                                                 "owner": "new-owner", "ttlSeconds": 10}, "new-acquire"))

    def test_local_request_secret_only_on_stdin_and_timeout_does_not_kill(self):
        target = object.__new__(LocalTarget)
        target.profile = self.profile
        target.root = self.fixture.root
        target.children = []
        process = Mock(returncode=0)
        process.communicate.return_value = (encode({"ok": True, "operationId": "request", "result": {}}), b"SECRET")
        with patch("dev_server.human_server.subprocess.Popen", return_value=process) as popen:
            target.request("renew", {"leaseId": "SECRET"}, "request")
            self.assertNotIn("SECRET", repr(popen.call_args))
            self.assertEqual({"leaseId": "SECRET"}, decode(process.communicate.call_args.args[0]))
            process.communicate.side_effect = subprocess.TimeoutExpired(["SECRET"], 1)
            self.error("UNAVAILABLE", lambda: target.request("renew", {"leaseId": "SECRET"}, "request"))
            process.kill.assert_not_called()
            process.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
