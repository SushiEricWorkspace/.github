"""架空profile・private記録・合成Registryによる引継ぎ。実SSH/Manager試験ではない。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from .build_lock import BuildLockError
from .connection_profiles import ConnectionStore, local_request, validate_profile
from .local_ipc import decode, encode, require_private
from .registry import RegistryError
from .supervisor import dispatch, new_request
from .test_supervisor import RegistryFixture

SCRIPT = Path(__file__).parents[1] / "connection-profile.py"


def example_profile(target_os="windows", transport="local"):
    paths = {"windows": ("C:/開発 環境/root", "C:/Python312/python.exe", "C:/管理 tools/dev-server.py"),
             "macos": ("/Users/example/開発 環境/root", "/opt/python3.12", "/Users/example/管理 tools/dev-server.py")}[target_os]
    return {"version": 1, "profileId": "test-dev", "displayName": "開発 環境", "transport": transport,
            "targetOs": target_os, "host": "approved-dev", "managementRoot": paths[0], "python": paths[1],
            "cli": paths[2], "instances": [{"instanceId": "persistent-01", "displayName": "永続枠 1"}],
            "approval": {"operator": "test-operator", "reference": "架空の承認参照"}}


class ProfileContractTests(unittest.TestCase):
    def test_both_host_path_formats_are_independent_of_client_os(self):
        for target_os in ("windows", "macos"):
            for transport in ("local", "ssh"):
                validate_profile(example_profile(target_os, transport))

    def test_secret_fields_and_live_identity_are_not_fixed_configuration(self):
        for key in ("password", "privateKey", "passphrase", "leaseId", "epoch", "generation", "runId", "nonce", "sessionId"):
            profile = dict(example_profile(), **{key: "SECRET-MARKER"})
            with self.assertRaises(RegistryError) as error:
                validate_profile(profile)
            self.assertNotIn("SECRET-MARKER", str(error.exception))

    def test_ssh_alias_options_host_os_and_duplicate_instances_are_rejected(self):
        for change in ({"host": "-oProxyCommand=SECRET-MARKER"}, {"host": "user:secret@host"},
                       {"targetOs": "macos"}, {"managementRoot": "../root"},
                       {"instances": example_profile()["instances"] * 2}):
            with self.assertRaises((RegistryError, ValueError)):
                validate_profile(dict(example_profile(), **change))

    def test_local_payload_is_only_on_stdin_and_rejected_output_is_not_echoed(self):
        profile = example_profile("windows" if sys.platform == "win32" else "macos")
        token = {"leaseId": "SECRET-MARKER"}
        response = {"ok": True, "operationId": "check-test", "result": {"owner": "test-owner"}}
        process = Mock(returncode=0, stdout=encode(response), stderr=b"SECRET-MARKER")
        with patch("dev_server.connection_profiles.subprocess.run", return_value=process) as run:
            self.assertEqual(response["result"], local_request(profile, "lease-check", token, operation_id="check-test"))
            args, kwargs = run.call_args
            self.assertNotIn("SECRET-MARKER", repr(args))
            self.assertEqual(token, decode(kwargs["input"]))
            self.assertEqual(profile["cli"], args[0][2])
            process.returncode = 1
            with self.assertRaises(RegistryError) as error:
                local_request(profile, "lease-check", token, operation_id="check-test")
            self.assertNotIn("SECRET-MARKER", str(error.exception))
        with patch("dev_server.connection_profiles.subprocess.run", side_effect=subprocess.TimeoutExpired(["SECRET-MARKER"], 1)):
            with self.assertRaises(RegistryError) as error:
                local_request(profile, "lease-check", token, operation_id="check-test")
            self.assertNotIn("SECRET-MARKER", str(error.exception))

    def test_ssh_does_not_fallback_to_local_execution(self):
        with patch("dev_server.connection_profiles.subprocess.run") as run:
            with self.assertRaises(RegistryError):
                local_request(example_profile(transport="ssh"), "lease-check", {}, operation_id="test-check")
            run.assert_not_called()


@unittest.skipUnless(sys.platform in ("win32", "darwin"), "対象OSのprivate保存試験")
class ConnectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="profiles-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "接続 記録"
        self.store = ConnectionStore(self.directory)
        self.profile = example_profile()
        self.token = {"instanceId": "persistent-01", "leaseId": "secret-lease", "epoch": 2, "expectedGeneration": "generation-01"}
        self.checked = {"instanceId": "persistent-01", "owner": "task-owner", "epoch": 2, "generation": "generation-01",
                        "generationBound": True, "runId": "run-01", "nonce": "nonce-01", "expiresAt": 200,
                        "state": "RUNNING", "pending": 0, "writerSessions": []}

    def offer(self):
        return self.store.offer(self.profile, self.token, self.checked, owner="task-owner",
                                source_client="launcher-01", target_client="manager-01", now=100)

    def claim(self, handoff_id, callback=None, **kwargs):
        return self.store.claim(self.profile, handoff_id, owner=kwargs.get("owner", "task-owner"),
                                target_client=kwargs.get("target_client", "manager-01"),
                                lease_check=callback or (lambda payload: self.checked), clock=lambda: kwargs.get("now", 100))

    def test_private_files_separate_profiles_and_once_only_handoff(self):
        self.store.save_profile(self.profile)
        handoff_id = self.offer()
        callback = Mock(return_value=self.checked)
        result = self.claim(handoff_id, callback)
        self.assertEqual("CLAIMED", result["state"])
        self.assertEqual("launcher-01", result["sourceClient"])
        callback.assert_called_once_with(dict(self.token, owner="task-owner"))
        with self.assertRaises(RegistryError):
            self.claim(handoff_id, callback)
        self.assertEqual(1, callback.call_count)
        self.assertNotIn("secret-lease", (self.directory / "profiles/test-dev.json").read_text(encoding="utf-8"))
        for path in self.directory.rglob("*.json"):
            require_private(path)
        self.assertEqual(self.profile, self.store.profile("test-dev"))
        with self.assertRaises(RegistryError):
            self.store.save_profile(self.profile)

    def test_other_owner_client_and_expired_record_do_not_call_cli(self):
        handoff_id = self.offer()
        callback = Mock(return_value=self.checked)
        for kwargs in ({"owner": "other-owner"}, {"target_client": "other-manager"}, {"now": 200}):
            with self.assertRaises(RegistryError):
                self.claim(handoff_id, callback, **kwargs)
        callback.assert_not_called()

    def test_live_old_epoch_generation_run_expired_and_unknown_are_preserved(self):
        for change in ({"epoch": 3}, {"generation": "generation-02"}, {"runId": "run-02"}, {"nonce": "nonce-02"},
                       {"expiresAt": 100}, {"owner": "other-owner"}, {"generationBound": False},
                       {"state": "RECOVERY_REQUIRED"}, {"pending": 1}, {"writerSessions": [{"status": "UNKNOWN"}]}):
            with self.subTest(change=change):
                handoff_id = self.offer()
                callback = Mock(return_value=dict(self.checked, **change))
                with self.assertRaises(RegistryError):
                    self.claim(handoff_id, callback)
                record = json.loads((self.directory / "handoffs" / (handoff_id + ".json")).read_bytes())
                self.assertEqual("UNKNOWN", record["state"])
                with self.assertRaises(RegistryError):
                    self.claim(handoff_id, callback)
                self.assertEqual(1, callback.call_count)

    def test_missing_response_and_interrupted_claim_cannot_be_claimed_again(self):
        handoff_id = self.offer()
        callback = Mock(side_effect=OSError("response lost"))
        with self.assertRaises(OSError):
            self.claim(handoff_id, callback)
        with self.assertRaises(RegistryError):
            self.claim(handoff_id, callback)
        self.assertEqual(1, callback.call_count)
        handoff_id = self.offer()
        path = self.directory / "handoffs" / (handoff_id + ".json")
        record = json.loads(path.read_bytes())
        record["state"] = "CLAIMING"
        self.store._write(path, record, replace=True)
        with self.assertRaises(RegistryError):
            self.claim(handoff_id, callback)

    def test_profile_edit_invalidates_old_handoff(self):
        self.store.save_profile(self.profile)
        handoff_id = self.offer()
        self.profile["managementRoot"] = "C:/別の承認 root"
        self.store.save_profile(self.profile, replace=True)
        callback = Mock()
        with self.assertRaises(RegistryError):
            self.claim(handoff_id, callback)
        callback.assert_not_called()

    def test_concurrent_claim_is_rejected_by_os_lock(self):
        handoff_id = self.offer()
        entered, finish = threading.Event(), threading.Event()
        results = []
        def check(payload):
            entered.set()
            if not finish.wait(5):
                raise TimeoutError()
            return self.checked
        def claim():
            try:
                results.append(self.claim(handoff_id, check))
            except BaseException as error:
                results.append(error)
        worker = threading.Thread(target=claim)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(BuildLockError):
                self.claim(handoff_id)
        finally:
            finish.set()
            worker.join(5)
        self.assertEqual("CLAIMED", results[0]["state"])

    def test_git_storage_and_link_are_rejected(self):
        repo = Path(self.temporary.name) / "repo"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: example")
        with self.assertRaises(RegistryError):
            ConnectionStore(repo / "private")
        with self.assertRaises(RegistryError):
            self.store.profile("../other-owner")
        profile_path = self.directory / "profiles/test-dev.json"
        self.store.save_profile(self.profile)
        link = self.directory / "linked.json"
        os.link(profile_path, link)
        with self.assertRaises(RegistryError):
            self.store.profile("test-dev")

    def test_cli_registration_check_and_rejection_do_not_output_secret(self):
        command = [sys.executable, "-B", str(SCRIPT)]
        response = subprocess.run(command + ["register", "--directory", str(self.directory)], input=encode(self.profile), capture_output=True)
        self.assertEqual(0, response.returncode, response.stderr)
        response = subprocess.run(command + ["check", "--directory", str(self.directory), "--profile-id", "test-dev"], capture_output=True)
        self.assertEqual("format-only", decode(response.stdout)["checked"])
        for body in (encode(dict(self.profile, password="SECRET-MARKER")), b'{"password":"SECRET-MARKER","password":2}'):
            response = subprocess.run(command + ["register", "--directory", str(self.directory)], input=body, capture_output=True)
            self.assertEqual(1, response.returncode)
            self.assertNotIn(b"SECRET-MARKER", response.stdout + response.stderr)

    def test_documented_profile_matches_registration_cli(self):
        guide = SCRIPT.parent / "dev_server/connection-profile-guide.md"
        example = guide.read_text(encoding="utf-8").split("```json\n", 1)[1].split("```", 1)[0]
        response = subprocess.run([sys.executable, "-B", str(SCRIPT), "register", "--directory", str(self.directory)],
                                  input=example.encode(), capture_output=True)
        self.assertEqual(0, response.returncode, response.stderr)
        self.assertEqual("dev-mac", decode(response.stdout)["profileId"])

    def test_non_private_record_is_rejected(self):
        self.store.save_profile(self.profile)
        path = self.directory / "profiles/test-dev.json"
        if os.name == "nt":
            result = subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:(R)"], capture_output=True)
            self.assertEqual(0, result.returncode)
        else:
            path.chmod(0o644)
        with self.assertRaises((OSError, RegistryError)):
            self.store.profile("test-dev")


@unittest.skipUnless(sys.platform in ("win32", "darwin"), "対象OSのRegistry試験")
class LeaseCheckTests(RegistryFixture, unittest.TestCase):
    def test_token_proof_has_no_renew_release_or_token_in_response(self):
        token = self.acquire()
        checked = self.command("lease-check", dict(token, owner="codex-test"), "check-test")
        self.assertEqual(120, checked["expiresAt"])
        self.assertNotIn(token["leaseId"], json.dumps(checked))
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM operations").fetchone()[0])
            row = connection.execute("SELECT * FROM instances WHERE id='persistent-01'").fetchone()
            self.assertEqual(token["leaseId"], row["lease"])
            self.assertEqual(120, row["expires"])

    def test_wrong_token_owner_epoch_generation_expiry_and_dry_are_rejected(self):
        token = self.acquire()
        for code, change in (("STALE_LEASE", {"leaseId": "old-lease"}), ("STALE_LEASE", {"epoch": 2}),
                             ("STALE_GENERATION", {"expectedGeneration": "old-generation"}),
                             ("OWNER_MISMATCH", {"owner": "other-owner"})):
            self.assert_code(code, lambda: self.command("lease-check", dict(token, owner="codex-test") | change))
        self.assert_code("INVALID_INPUT", lambda: self.command("lease-check", dict(token, owner="codex-test"), dry=True))
        self.now = 120
        self.assert_code("LEASE_EXPIRED", lambda: self.command("lease-check", dict(token, owner="codex-test")))

    def test_dispatch_lease_check_uses_instance_lock(self):
        from .supervisor import private_lock
        token = self.acquire()
        with private_lock(self.root / "manager/locks/instance-persistent-01.lock"):
            response = dispatch(self.registry, new_request("lease-check", dict(token, owner="codex-test")))
        self.assertEqual("LOCK_BUSY", response["error"]["code"])


@unittest.skipUnless(sys.platform in ("win32", "darwin"), "対象OSの合成runtime照合試験")
class RunningHandoffTests(unittest.TestCase):
    def test_running_token_proof_and_private_claim_use_actual_registry(self):
        from .test_runtime_control import RuntimeControlTests
        fixture = RuntimeControlTests("test_prepare_start_stop_replay_restart_and_no_snapshot_clean")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare()
        fixture.start()
        profile = example_profile("windows" if os.name == "nt" else "macos")
        profile["managementRoot"] = str(fixture.root)
        store = ConnectionStore(Path(fixture.temporary.name) / "connection-private")
        checked = fixture.command("lease-check", dict(fixture.token, owner="codex-test"), "check-running")
        handoff_id = store.offer(profile, fixture.token, checked, owner="codex-test", source_client="launcher",
                                 target_client="manager", now=fixture.now)
        record = store.claim(profile, handoff_id, owner="codex-test", target_client="manager",
                             lease_check=lambda payload: fixture.command("lease-check", payload, "check-claim"),
                             clock=lambda: fixture.now)
        self.assertEqual(checked["nonce"], record["nonce"])
        self.assertEqual(fixture.token, record["token"])
        fixture.adapter.created[-1].exit_code = 1
        fixture.assert_code("PROCESS_MISMATCH", lambda: fixture.command("lease-check", dict(fixture.token, owner="codex-test")))
