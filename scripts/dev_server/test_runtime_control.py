"""合成bundle/Mod証跡の失敗注入。実Minecraft保存往復とは区別する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .build_inputs import digest
from .local_ipc import make_private, UnixListener
from .registry import Registry, RegistryError
from .runtime_control import RuntimeControl
from .runtime_ports import instance_endpoints
from .runtime_process import command_hash
from .supervisor import dispatch, new_request
from . import test_runtime_bundle as bundle_tests
from .test_supervisor import RegistryFixture


class FakeProcess:
    def __init__(self, adapter):
        self.adapter = adapter
        self.pid = self.group = None
        self.stage = "NOT_CREATED"

    def start(self, arguments, cwd, log, nonce, created):
        adapter = self.adapter
        self.pid = adapter.next_pid
        self.group = nonce
        self.stage = "CREATED"
        adapter.next_pid += 1
        self.exit_code = None
        self.children = []
        self.closed = False
        values = [json.loads(line) for line in Path(arguments[1][1:]).read_text().splitlines()]
        self.props = dict(value.removeprefix("-Dsushieric.managed.").split("=", 1)
                          for value in values if value.startswith("-Dsushieric.managed."))
        self.identity = {"pid": self.pid, "startId": str(self.pid), "startMillis": self.pid,
                         "bootId": "native-boot", "cwd": str(cwd.resolve()), "executable": arguments[0],
                         "commandHash": command_hash(arguments), "owner": "native-owner"}
        self.endpoints = instance_endpoints(next(item for item in adapter.inventory["instances"]
                                                 if item["instanceId"] == self.props["instanceId"]))
        self.proof = {"schemaVersion": 1, "instanceId": self.props["instanceId"], "generationId": self.props["generationId"],
                      "startNonce": nonce, "phase": "STARTED",
                      "identity": {"pid": self.pid, "processStartedAt": datetime.fromtimestamp(self.pid / 1000, timezone.utc).isoformat(),
                                   "cwd": str(cwd), "artifactId": cwd.joinpath("runtime").name,
                                   "runtimeManifestSha256": digest((cwd / "runtime/runtime.json").read_bytes()),
                                   "runtimeManifest": str(cwd / "runtime/runtime.json")},
                      "startup": {"ready": True, "managementEnabled": True, "managementStarted": True, "unknownWriters": [],
                                  "roots": {"world": str(cwd / "world"), "config": str(cwd / "config"),
                                            "modConfig": str(cwd / "config/SushiEricServerMod")},
                                  "listeners": [dict(service=service, **{key: value[key] for key in ("protocol", "address", "port")})
                                                for service, value in self.endpoints.items()]},
                      "requiresSupervisorProcessExit": True, "requiresExternalWriterDrain": True}
        self.proof["identity"]["artifactId"] = json.loads((cwd / "runtime/runtime.json").read_text())["artifactId"]
        adapter.created.append(self)
        created()
        self.stage = "VERIFIED"
        if adapter.creation_fault:
            adapter.creation_fault(self)
        self.stage = "RUNNING"
        if adapter.start_fault:
            adapter.start_fault(self)
        self.save_proof()

    def save_proof(self):
        Path(self.props["proofPath"]).write_text(json.dumps(self.proof), encoding="utf-8")

    def poll(self):
        return self.exit_code

    def members(self):
        return self.children[:]

    def close(self):
        self.closed = True

    def stopped_proof(self):
        self.proof.update(phase="STOPPED", modSaveCompleted=True, vanillaShutdownTailReached=True,
                          saves={"failures": [], "pendingWriters": 0, "drained": True, "success": True,
                                 "results": {key: True for key in ("management-drain", "money-history", "shop-history",
                                                                  "shop-stocks", "player-data")}})
        self.save_proof()


class FakeAdapter:
    def __init__(self, inventory):
        self.inventory = inventory
        self.next_pid = 10001
        self.created = []
        self.start_fault = None
        self.creation_fault = None
        self.identity_fault = None
        self.socket_fault = None

    def OwnedProcess(self):
        return FakeProcess(self)

    def identity(self, pid):
        process = next((item for item in self.created if item.pid == pid), None)
        if process is None or process.poll() is not None:
            return None
        result = process.identity.copy()
        if self.identity_fault:
            self.identity_fault(result)
        return result

    def recorded_members(self, creation, nonce):
        process = next(item for item in self.created if item.pid == creation["pid"])
        return process.members()

    def sockets(self, pid):
        process = next(item for item in self.created if item.pid == pid)
        result = deepcopy(list(process.endpoints.values()))
        if self.socket_fault:
            self.socket_fault(result)
        return result


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSのprivate rootを使う試験")
class RuntimeControlTests(RegistryFixture, unittest.TestCase):
    quota_bytes = 10000000

    def setUp(self):
        RegistryFixture.setUp(self)
        self.bundle_fixture = bundle_tests.RuntimeBundleTests("test_launch_has_no_source_cache_or_gradle_dependency")
        self.bundle_fixture.setUp()
        self.addCleanup(self.bundle_fixture.doCleanups)
        source = (bundle_tests.saved_v1_bundle(self.bundle_fixture.store)
                  if getattr(self, "bundle_version", 2) == 1 else self.bundle_fixture.freeze())
        self.artifact = source.name
        shutil.copytree(source, self.root / "artifacts" / self.artifact)
        self.adapter = FakeAdapter(self.inventory)
        self.registry.runtime = RuntimeControl(self.registry, process_adapter=self.adapter)
        self.token = self.acquire()
        self.prepare_payload = dict(self.token, artifactId=self.artifact, generationId="generation-01", seed=17, acceptEula=True)
        self.stop_fault = None
        self.rcon_calls = 0
        self.rcon = patch("dev_server.runtime_control.runtime_rcon.stop", side_effect=self.rcon_stop)
        self.rcon.start()
        self.addCleanup(self.rcon.stop)
        probes = patch("dev_server.runtime_control.prebind", side_effect=lambda endpoints: nullcontext())
        probes.start()
        self.addCleanup(probes.stop)

    def rcon_stop(self, address, port, password, timeout, *, verify):
        verify()
        self.rcon_calls += 1
        self.assertNotEqual("", password)
        process = next(item for item in self.adapter.created if item.endpoints["rcon"]["port"] == port and item.poll() is None)
        process.stopped_proof()
        process.exit_code = 0
        if self.stop_fault:
            self.stop_fault(process)

    def prepare(self):
        result = self.command("prepare", self.prepare_payload, "prepare-01")
        self.token["expectedGeneration"] = result["generation"]
        return self.root / "instances/persistent-01/generations/generation-01"

    def start(self, operation="start-01"):
        return self.command("start", dict(self.token, timeoutSeconds=2), operation)

    def stop(self, operation="stop-01"):
        return self.command("stop", dict(self.token, timeoutSeconds=2), operation)

    def state(self):
        with self.registry.transaction(read_only=True) as connection:
            return dict(connection.execute("SELECT * FROM instances WHERE id='persistent-01'").fetchone())

    def test_prepare_start_stop_replay_restart_and_no_snapshot_clean(self):
        generation = self.prepare()
        saved = (generation / "server.properties").read_bytes()
        self.assertTrue(self.command("prepare", self.prepare_payload, "prepare-01")["replayed"])
        first = self.start()
        self.assertTrue(self.start()["replayed"])
        self.assertEqual(1, len(self.adapter.created))
        stopped = self.stop()
        self.assertTrue(stopped["jvmAndChildrenExited"])
        self.assertFalse(stopped["snapshotClean"])
        self.assertFalse(stopped["externalWritersVerified"])
        self.assertTrue(self.stop()["replayed"])
        self.assertEqual(1, self.rcon_calls)
        self.assert_code("STALE_OPERATION", self.start)
        second = self.start("start-02")
        self.assertNotEqual(first["runId"], second["runId"])
        self.assertEqual(saved, (generation / "server.properties").read_bytes())
        self.assertEqual(self.adapter.created[0].identity["cwd"], self.adapter.created[1].identity["cwd"])
        self.assertEqual(2, len(list((generation.parents[1] / "diagnostics").iterdir())))
        self.stop("stop-02")
        self.assertTrue(self.state()["runtime_proof"])
        self.assertTrue(self.command("release", self.token, "release-prepared")["released"])
        self.token = self.acquire(operation="acquire-next-owner")
        self.assert_code("INVALID_STATE", lambda: self.start("start-next-owner"))

    def test_prepare_preserves_existing_data_and_requires_empty_slot(self):
        root = self.root / "instances/persistent-01"
        root.mkdir()
        (root / "human-data").write_bytes(b"preserve")
        self.assert_code("EXISTING_DATA", self.prepare)
        self.assertEqual(b"preserve", (root / "human-data").read_bytes())
        self.assertEqual("LEASED_STOPPED", self.state()["state"])

    def test_dry_run_no_database_files_reservation_or_process_side_effect(self):
        def counts():
            with self.registry.transaction(read_only=True) as connection:
                return [connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                        for table in ("runtime_operations", "runs", "reservations", "endpoint_reservations")]
        before = counts()
        self.command("prepare", self.prepare_payload, "prepare-dry", True)
        self.assertFalse((self.root / "instances/persistent-01").exists())
        generation = self.prepare()
        entries = list(generation.rglob("*"))
        before_start = counts()
        with patch("dev_server.runtime_control.prebind") as probe:
            self.command("start", dict(self.token, timeoutSeconds=2), "start-dry", True)
            probe.assert_not_called()
        self.assertEqual(before_start, counts())
        self.assertEqual(entries, list(generation.rglob("*")))
        self.assertFalse(self.adapter.created)
        self.assertEqual([0, 0, 0, 0], before)

    def test_prepare_partial_failure_is_fenced_and_never_overwritten(self):
        with patch.object(self.registry.generation, "_write_tree", side_effect=OSError("secret-path")):
            self.assert_code("GENERATION_FAILED", self.prepare)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertTrue((self.root / "instances/persistent-01/staging/prepare-01").exists())
        self.assert_code("RECOVERY_REQUIRED", self.prepare)
        self.assert_code("UNAVAILABLE", lambda: self.acquire(operation="reacquire"))
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(2, connection.execute("SELECT count(*) FROM reservations").fetchone()[0])

    def test_lease_epoch_generation_and_operation_payload_fences(self):
        self.prepare()
        for fields, code in (({"leaseId": "old"}, "STALE_LEASE"), ({"epoch": 0}, "STALE_LEASE"),
                             ({"expectedGeneration": "old"}, "STALE_GENERATION")):
            self.assert_code(code, lambda: self.command("start", dict(self.token, timeoutSeconds=2, **fields), "fenced-start"))
        self.assert_code("OPERATION_CONFLICT", lambda: self.command("prepare", dict(self.prepare_payload, seed=18), "prepare-01"))
        self.now = 121
        self.assert_code("LEASE_EXPIRED", self.start)
        self.assertFalse(self.adapter.created)

    def test_start_nonce_mismatch_is_not_running(self):
        self.prepare()
        self.adapter.start_fault = lambda process: process.proof.update(startNonce="previous-run")
        self.assert_code("PROOF_MISMATCH", self.start)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertEqual(1, self.rcon_calls)
        self.assertTrue(self.adapter.created[0].closed)
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])

    def test_effective_root_mismatch_is_not_running(self):
        self.prepare()
        self.adapter.start_fault = lambda process: process.proof["startup"]["roots"].update(world=str(self.root / "wrong-world"))
        self.assert_code("ROOT_MISMATCH", self.start)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertEqual(1, self.rcon_calls)

    def test_management_bind_failure_is_not_running(self):
        self.prepare()
        self.adapter.start_fault = lambda process: process.proof["startup"].update(ready=False, managementStarted=False)
        self.assert_code("START_REJECTED", self.start)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertEqual(1, self.rcon_calls)

    def test_runtime_hash_mismatch_is_not_running(self):
        self.prepare()
        self.adapter.start_fault = lambda process: process.proof["identity"].update(runtimeManifestSha256="0" * 64)
        self.assert_code("PROOF_MISMATCH", self.start)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])

    def test_quota_refusal_creates_no_partial_generation(self):
        self.registry.inventory["quotaBytes"] = 1
        self.assert_code("NO_SPACE", self.prepare)
        self.assertFalse((self.root / "instances/persistent-01").exists())
        self.assertEqual("LEASED_STOPPED", self.state()["state"])

    def test_space_accepts_only_private_supervisor_socket(self):
        control = self.registry.runtime
        ipc = self.root / "manager/supervisor.sock"
        ipc.write_bytes(b"fixture")
        original = Path.lstat
        info = SimpleNamespace(st_mode=0o140600, st_uid=17)
        def lstat(path):
            return info if path == ipc else original(path)
        with patch("dev_server.runtime_control.os", SimpleNamespace(name="posix", getuid=lambda: 17, scandir=os.scandir)), \
                patch.object(Path, "lstat", lstat):
            control._space()
            for mode, owner in ((0o140666, 17), (0o140600, 18), (0o120600, 17), (0o100600, 17)):
                info.st_mode, info.st_uid = mode, owner
                self.assert_code("IPC_UNVERIFIED", control._space)
            info.st_mode, info.st_uid = 0o140600, 17
            other = self.root / "instances/unknown.sock"
            other.write_bytes(b"preserve")
            def unknown(path):
                return info if path in (ipc, other) else original(path)
            with patch.object(Path, "lstat", unknown), self.assertRaises(OSError):
                control._space()

    @unittest.skipUnless(sys.platform == "darwin", "macOS実Unix socketのquota走査")
    def test_live_supervisor_socket_does_not_block_prepare_or_start(self):
        listener = UnixListener(str(self.root / "manager/supervisor.sock"))
        try:
            self.prepare()
            self.start()
            self.stop()
        finally:
            listener.close()

    def test_operation_id_renew_race_is_rejected_at_registration(self):
        entered, proceed = threading.Event(), threading.Event()
        original = self.registry.runtime._space
        def space(required_bytes=0):
            original(required_bytes)
            entered.set()
            if not proceed.wait(3):
                raise AssertionError("試験同期期限超過")
        with patch.object(self.registry.runtime, "_space", side_effect=space), ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(dispatch, self.registry, new_request("prepare", self.prepare_payload, operation_id="shared-operation"))
            self.assertTrue(entered.wait(2))
            try:
                result = dispatch(self.registry, new_request("renew", dict(self.token, ttlSeconds=60), operation_id="shared-operation"))
                self.assertTrue(result["ok"], result)
            finally:
                proceed.set()
            self.assertEqual("OPERATION_CONFLICT", pending.result()["error"]["code"])
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM runtime_operations").fetchone()[0])
        self.assertFalse((self.root / "instances/persistent-01").exists())
        self.assertEqual("LEASED_STOPPED", self.state()["state"])

    def test_operation_id_reference_race_is_rejected_at_registration(self):
        original = self.registry.runtime._space
        def space(required_bytes=0):
            original(required_bytes)
            self.registry.reserve_references(self.token, "prepare-01", ["artifact:fixture"])
        with patch.object(self.registry.runtime, "_space", side_effect=space):
            self.assert_code("OPERATION_CONFLICT", self.prepare)
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM runtime_operations").fetchone()[0])
        self.assertFalse((self.root / "instances/persistent-01").exists())

    def assert_creation_preserved(self, stage):
        self.assert_code("RUNTIME_FAILED", self.start)
        run = self.registry.runtime._load_run(self.state())
        process = self.registry.runtime.processes[run["runId"]]
        self.assertIs(process, self.adapter.created[0])
        self.assertEqual(stage, process.stage)
        self.assertEqual(process.pid, run["creation"]["pid"])
        self.assertEqual(stage, run["creation"]["stage"])
        self.assertEqual(0, self.rcon_calls)
        self.assertFalse(process.closed)
        self.assertFalse(run["jvmAndChildrenExited"])
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(3, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])

    def test_identity_read_failure_preserves_created_owner(self):
        self.prepare()
        def fail(identity):
            raise OSError("識別取得失敗")
        self.adapter.identity_fault = fail
        self.assert_creation_preserved("CREATED")

    def test_creation_record_failure_preserves_created_owner(self):
        self.prepare()
        with patch("dev_server.runtime_control.write_json", side_effect=OSError("記録失敗")):
            self.assert_creation_preserved("CREATED")

    def test_creation_db_failure_preserves_created_owner(self):
        self.prepare()
        record = self.registry.runtime._record
        failed = []
        def fail_once(run):
            if run.get("creation") and not failed:
                failed.append(True)
                raise OSError("作成記録DB失敗")
            record(run)
        with patch.object(self.registry.runtime, "_record", side_effect=fail_once):
            self.assert_creation_preserved("CREATED")

    def test_persistent_creation_db_failure_keeps_memory_owner_and_reservations(self):
        self.prepare()
        record = self.registry.runtime._record
        def fail(run):
            if run.get("creation"):
                raise OSError("作成記録DB継続失敗")
            record(run)
        with patch.object(self.registry.runtime, "_record", side_effect=fail):
            self.assert_code("RUNTIME_FAILED", self.start)
        run = self.registry.runtime._load_run(self.state())
        process = self.registry.runtime.processes[run["runId"]]
        self.assertIs(process, self.adapter.created[0])
        self.assertEqual("CREATED", process.stage)
        self.assertFalse(process.closed)
        creation = json.loads((Path(run["proofPath"]).parent / "creation.json").read_text())
        self.assertEqual({"pid": process.pid, "stage": "CREATED", "ownerGroup": process.group}, creation)
        self.assertEqual(0, self.rcon_calls)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(3, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])

    def test_job_assignment_failure_preserves_verified_owner(self):
        self.prepare()
        def fail(process):
            raise OSError("Job割当失敗")
        self.adapter.creation_fault = fail
        self.assert_creation_preserved("VERIFIED")

    def test_start_timeout_stops_only_identified_owned_rcon(self):
        self.prepare()
        self.adapter.start_fault = lambda process: process.proof.update(phase="INITIALIZING")
        with patch("dev_server.runtime_control.time.monotonic", side_effect=[0, 3, 3]):
            self.assert_code("START_TIMEOUT", self.start)
        self.assertEqual(1, self.rcon_calls)
        self.assertTrue(self.adapter.created[0].closed)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])

    def test_crash_does_not_replay_fake_running(self):
        self.prepare()
        self.start()
        self.adapter.created[0].exit_code = 1
        before = self.state()
        self.assert_code("PROCESS_MISMATCH", lambda: self.command("start", dict(self.token, timeoutSeconds=2), "start-01", True))
        self.assertEqual(before, self.state())
        self.assert_code("RECOVERY_REQUIRED", self.start)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])

    def test_bind_race_unknown_socket_does_not_stop_unrelated_rcon(self):
        self.prepare()
        self.adapter.socket_fault = lambda values: values.clear()
        self.assert_code("LISTENER_MISMATCH", self.start)
        self.assertEqual(0, self.rcon_calls)
        self.assertIsNone(self.adapter.created[0].poll())
        self.assertFalse(self.adapter.created[0].closed)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(3, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])

    def test_save_boolean_failure_rejects_stopped_proof(self):
        self.prepare()
        self.start()
        def failed(process):
            process.proof["saves"]["results"]["player-data"] = False
            process.save_proof()
        self.stop_fault = failed
        self.assert_code("SAVE_FAILED", self.stop)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertIsNone(self.state()["runtime_proof"])
        self.assertTrue(self.registry.runtime._load_run(self.state())["jvmAndChildrenExited"])
        self.assertFalse(self.registry.runtime._load_run(self.state())["runtimeSaveCompleted"])

    def test_nonzero_exit_is_not_saved(self):
        self.prepare()
        self.start()
        def failed(process):
            process.exit_code = 1
        self.stop_fault = failed
        self.assert_code("SAVE_FAILED", self.stop)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertIsNone(self.state()["runtime_proof"])

    def test_save_exception_rejects_zero_exit_and_completed_flag(self):
        self.prepare()
        self.start()
        def failed(process):
            process.proof["saves"]["failures"].append("vanilla-player-json")
            process.save_proof()
        self.stop_fault = failed
        self.assert_code("SAVE_FAILED", self.stop)
        self.assertEqual(0, self.adapter.created[0].poll())
        self.assertIsNone(self.state()["runtime_proof"])

    def test_pending_background_writer_rejects_zero_exit_and_completed_flag(self):
        self.prepare()
        self.start()
        def failed(process):
            process.proof["saves"]["pendingWriters"] = 1
            process.save_proof()
        self.stop_fault = failed
        self.assert_code("SAVE_FAILED", self.stop)
        self.assertEqual(0, self.adapter.created[0].poll())
        self.assertIsNone(self.state()["runtime_proof"])

    def test_rcon_failure_keeps_live_process_and_port_reservation(self):
        self.prepare()
        self.start()
        with patch("dev_server.runtime_control.runtime_rcon.stop", side_effect=RegistryError("RCON_FAILED", "認証失敗")):
            self.assert_code("RCON_FAILED", self.stop)
        self.assertIsNone(self.adapter.created[0].poll())
        self.assertFalse(self.adapter.created[0].closed)
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(3, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])

    def test_stop_timeout_keeps_child_reservation_and_never_kills(self):
        self.prepare()
        self.start()
        self.stop_fault = lambda process: process.children.append(22001)
        with patch("dev_server.runtime_control.time.monotonic", side_effect=[0, 3]):
            self.assert_code("STOP_TIMEOUT", self.stop)
        self.assertFalse(self.adapter.created[0].closed)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(3, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])
        self.assertFalse(self.registry.runtime._load_run(self.state())["jvmAndChildrenExited"])

    def test_live_identity_mismatch_and_crash_replay_require_recovery(self):
        self.prepare()
        self.start()
        self.adapter.identity_fault = lambda identity: identity.update(bootId="another-boot")
        self.assert_code("RECOVERY_REQUIRED", self.stop)
        self.assertEqual(0, self.rcon_calls)
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])

    def test_supervisor_restart_keeps_generation_and_reservations_without_reloan(self):
        self.prepare()
        self.start()
        restarted = Registry(self.root, clock=lambda: self.now)
        restarted.begin_supervision()
        self.assert_code("RECOVERY_REQUIRED", lambda: restarted.execute("start", dict(self.token, timeoutSeconds=2), operation_id="start-02"))
        self.assertEqual("RECOVERY_REQUIRED", self.state()["state"])
        self.assertTrue((self.root / "instances/persistent-01/active.json").exists())
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(3, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])

    def test_long_operation_has_no_db_lock_and_can_renew_and_read_other_slot(self):
        entered, proceed = threading.Event(), threading.Event()
        original = self.registry.generation._build
        def prepare(request, bundle, manifest, staging):
            entered.set()
            if not proceed.wait(3):
                raise AssertionError("試験同期期限超過")
            return original(request, bundle, manifest, staging)
        with patch.object(self.registry.generation, "_build", side_effect=prepare), ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(dispatch, self.registry, new_request("prepare", self.prepare_payload, operation_id="prepare-01"))
            self.assertTrue(entered.wait(2))
            try:
                renewed = dispatch(self.registry, new_request("renew", dict(self.token, ttlSeconds=60), operation_id="renew-during-prepare"))
                self.assertTrue(renewed["ok"], renewed)
                self.assertTrue(dispatch(self.registry, new_request("status", {"instanceId": "persistent-02"}))["ok"])
                busy = dispatch(self.registry, new_request("prepare", self.prepare_payload, operation_id="prepare-other"))
                self.assertEqual("LOCK_BUSY", busy["error"]["code"])
                second_token = self.acquire("persistent-02", "acquire-second-slot")
                second = dict(second_token, artifactId=self.artifact, generationId="generation-02", seed=17, acceptEula=True)
                competing = dispatch(self.registry, new_request("prepare", second, operation_id="prepare-second-slot"))
                self.assertEqual("LOCK_BUSY", competing["error"]["code"])
                self.assertEqual("BUSY", dispatch(self.registry, new_request("shutdown", {}))["error"]["code"])
            finally:
                proceed.set()
            self.assertTrue(pending.result()["ok"])
