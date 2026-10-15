"""外部で正常終了した合成runを、成功の自己申告なしに正規復旧する。実rootは変更しない。"""
from contextlib import contextmanager
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

from .build_inputs import digest
from .registry import RegistryError
from . import test_runtime_control as runtime_tests


@unittest.skipUnless(sys.platform in ("win32", "darwin"), "private rootを使う合成試験")
class ObservedStopTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_tests.RuntimeControlTests("test_prepare_start_stop_replay_restart_and_no_snapshot_clean")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.generation = self.fixture.prepare()
        self.fixture.start()
        self.registry = self.fixture.registry
        self.process = self.fixture.adapter.created[0]
        self.process.stopped_proof()
        self.process.exit_code = 0
        self.registry.begin_supervision()
        self.fixture.now = 121
        self.run = self.registry.runtime._load_run(self.fixture.state())
        self.proof_path = self.generation.parents[1] / "diagnostics" / self.run["runId"] / "proof.json"
        self.payload = dict(self.fixture.token, action="observe-stop", targetOperationId=None, ttlSeconds=60,
                            expectedRunId=self.run["runId"], expectedProofHash=digest(self.proof_path.read_bytes()))
        self.native_error = None
        self.observations = 0
        self.handles = 0
        @contextmanager
        def observe(expected, creation, nonce):
            self.observations += 1
            self.handles += 1
            try:
                if self.native_error:
                    raise RegistryError(self.native_error, "native失敗注入")
                if self.process.poll() is None or self.process.members():
                    raise RegistryError("PROCESS_MISMATCH", "未終了または子残存")
                if self.process.poll() != 0:
                    raise RegistryError("SAVE_FAILED", "非正常exit")
                yield {"pid": expected["pid"], "startId": expected["startId"], "bootId": expected["bootId"], "exitCode": 0}
            finally:
                self.handles -= 1
        self.observe_patch = patch.object(self.fixture.adapter, "observe_exit", new=observe, create=True)
        self.observe_patch.start()
        self.addCleanup(self.observe_patch.stop)

    def observe(self, operation="observe-01", dry=False, **values):
        return self.fixture.command("recover", dict(self.payload, **values), operation, dry)

    def database(self):
        with self.registry.transaction(read_only=True) as connection:
            return {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                    for table in ("instances", "runs", "endpoint_reservations", "runtime_operations", "audit")}

    def assert_rejected_unchanged(self, code, **values):
        before = self.database()
        self.fixture.assert_code(code, lambda: self.observe(**values))
        self.assertEqual(before, self.database())
        self.assertEqual(0, self.handles)

    def test_observation_commits_origin_and_replays_without_renewing_or_rcon(self):
        before = self.fixture.state()
        result = self.observe()
        self.assertEqual("observe-stop", result["action"])
        self.assertFalse(result["snapshotClean"])
        row = self.fixture.state()
        self.assertEqual("LEASED_STOPPED", row["state"])
        self.assertEqual(181, row["expires"])
        self.assertEqual(before["owner"], row["owner"])
        self.assertEqual(before["epoch"], row["epoch"])
        run = self.registry.runtime._load_run(row)
        self.assertEqual(self.run.get("stopRequested"), run.get("stopRequested"))
        self.assertEqual(self.run.get("stopAcknowledged"), run.get("stopAcknowledged"))
        self.assertTrue(run["runtimeSaveCompleted"])
        self.assertEqual(0, run["exitCode"])
        self.assertEqual(run["observedStop"], json.loads(row["runtime_proof"])["observedStop"])
        self.assertIsNone(row["clean_proof"])
        with self.registry.transaction(read_only=True) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])
            self.assertEqual("recover-observe-stop", connection.execute("SELECT command FROM audit ORDER BY sequence DESC").fetchone()[0])
        self.fixture.now = 130
        self.assertTrue(self.observe()["replayed"])
        self.assertEqual(181, self.fixture.state()["expires"])
        self.assertEqual(1, self.observations)
        self.assertEqual(0, self.fixture.rcon_calls)
        self.assertEqual(0, self.handles)
        self.registry.generation._stopped(row)
        self.fixture.command("release", self.fixture.token, "release-observed")
        self.assertEqual("AVAILABLE", self.fixture.state()["state"])
        self.fixture.assert_code("STALE_LEASE", self.observe)

    def test_dry_run_verifies_everything_but_does_not_mutate(self):
        before = self.database()
        self.assertTrue(self.observe(dry=True)["dryRun"])
        self.assertEqual(before, self.database())
        self.assertEqual(1, self.observations)
        self.assertEqual(0, self.handles)

    def test_stale_tokens_run_generation_hash_and_payload_are_rejected(self):
        for values, code in (({"leaseId": "old"}, "STALE_LEASE"), ({"epoch": 0}, "STALE_LEASE"),
                             ({"expectedGeneration": "old"}, "STALE_GENERATION"), ({"expectedRunId": "old"}, "STALE_RUN"),
                             ({"expectedProofHash": "0" * 64}, "PROOF_MISMATCH"), ({"expectedProofHash": "bad"}, "INVALID_INPUT"),
                             ({"targetOperationId": "unconfirmed"}, "INVALID_INPUT"), ({"success": True}, "INVALID_INPUT")):
            with self.subTest(values=values):
                self.assert_rejected_unchanged(code, **values)
        self.assertEqual(0, self.observations)

    def test_live_nonzero_and_child_residual_never_become_stop_proofs(self):
        self.process.exit_code = None
        self.assert_rejected_unchanged("PROCESS_MISMATCH")
        self.process.exit_code = 2
        self.assert_rejected_unchanged("SAVE_FAILED")
        self.process.exit_code = 0
        self.process.children = [22001]
        self.assert_rejected_unchanged("PROCESS_MISMATCH")

    def test_native_unavailable_mismatch_and_unsupported_are_not_receipt_fallbacks(self):
        for error in ("PROCESS_UNVERIFIED", "PROCESS_MISMATCH", "UNSUPPORTED_PROCESS"):
            self.native_error = error
            with self.subTest(error=error):
                self.assert_rejected_unchanged(error)
        run = dict(self.run, identity=None)
        with self.registry.transaction() as connection:
            connection.execute("UPDATE runs SET record=? WHERE id=?", (json.dumps(run), self.run["runId"]))
        self.assert_rejected_unchanged("PROCESS_UNVERIFIED")

    def test_mod_save_drain_nonce_and_root_failures_are_not_adopted(self):
        original = self.proof_path.read_bytes()
        for fault, code in (("save", "SAVE_FAILED"), ("drain", "SAVE_FAILED"), ("nonce", "PROOF_MISMATCH"), ("root", "ROOT_MISMATCH")):
            proof = json.loads(original)
            if fault == "save": proof["saves"]["results"]["player-data"] = False
            if fault == "drain": proof["saves"]["drained"] = False
            if fault == "nonce": proof["startNonce"] = "unrelated"
            if fault == "root": proof["startup"]["roots"]["world"] = str(self.generation / "another-world")
            self.proof_path.write_text(json.dumps(proof), encoding="utf-8")
            with self.subTest(fault=fault):
                self.assert_rejected_unchanged(code, expectedProofHash=digest(self.proof_path.read_bytes()))
        self.proof_path.write_bytes(original)

    def test_active_and_manifest_tampering_are_rejected(self):
        manifest = self.generation / "runtime/runtime.json"
        original = manifest.read_bytes()
        manifest.write_bytes(original + b"\n")
        self.assert_rejected_unchanged("PROOF_MISMATCH")
        manifest.write_bytes(original)
        active = self.generation.parents[1] / "active.json"
        record = json.loads(active.read_text())
        record["generationId"] = "other"
        active.write_text(json.dumps(record))
        self.assert_rejected_unchanged("RECOVERY_REQUIRED")

    def test_pending_instance_reference_and_operation_are_preserved(self):
        for table in ("instances", "reservations", "runtime_operations", "snapshot_operations"):
            with self.registry.transaction() as connection:
                if table == "instances": connection.execute("UPDATE instances SET pending=1 WHERE id='persistent-01'")
                if table == "reservations": connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)", ("unknown", "artifact:test", "persistent-01", self.fixture.token["leaseId"], 1))
                if table == "runtime_operations": connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)", ("unknown", "hash", "stop", "persistent-01", "PENDING", "{}"))
                if table == "snapshot_operations": connection.execute("INSERT INTO snapshot_operations VALUES (?,?,?,?,?)", ("unknown", "hash", "persistent-01", "PENDING", "{}"))
            with self.subTest(table=table): self.assert_rejected_unchanged("RECOVERY_REQUIRED")
            with self.registry.transaction() as connection:
                if table == "instances": connection.execute("UPDATE instances SET pending=0 WHERE id='persistent-01'")
                else: connection.execute("DELETE FROM " + table + " WHERE " + ("operation" if table == "reservations" else "id") + "='unknown'")

    def test_writer_and_pending_writer_are_not_closed_by_observation(self):
        with self.registry.transaction() as connection:
            connection.execute("INSERT INTO writer_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("writer", "persistent-01", self.fixture.token["leaseId"], 1,
                self.run["generationId"], self.run["runId"], self.run["nonce"], 123, "{}", "OPEN", 0))
            connection.execute("UPDATE instances SET writers=1 WHERE id='persistent-01'")
        self.assert_rejected_unchanged("PROOF_REQUIRED")
        with self.registry.transaction() as connection:
            connection.execute("UPDATE writer_sessions SET status='CLOSED',pending=1 WHERE id='writer'")
            connection.execute("UPDATE instances SET writers=0 WHERE id='persistent-01'")
        self.assert_rejected_unchanged("PROOF_REQUIRED")

    def test_endpoint_reservation_must_match_entire_run(self):
        with self.registry.transaction() as connection:
            connection.execute("UPDATE endpoint_reservations SET run='unrelated' WHERE service='rcon'")
        self.assert_rejected_unchanged("PROOF_MISMATCH")
        self.assertEqual(0, self.observations)
        with self.registry.transaction() as connection:
            connection.execute("UPDATE endpoint_reservations SET run=? WHERE service='rcon'", (self.run["runId"],))
            connection.execute("INSERT INTO endpoint_reservations VALUES (?,?,?,?,?,?,?,?)",
                               (self.run["runId"], "persistent-02", "unexpected", "localhost", "tcp", "127.0.0.1", 26440, 0))
        self.assert_rejected_unchanged("PROOF_MISMATCH")
        self.assertEqual(0, self.observations)

    def test_commit_failure_rolls_back_all_changes_and_same_request_can_retry(self):
        original = self.registry.transaction
        before = self.database()
        @contextmanager
        def failing(*, read_only=False):
            with original(read_only=read_only) as connection:
                if read_only:
                    yield connection
                else:
                    class Connection:
                        def execute(self, sql, args=()):
                            if sql.startswith("INSERT INTO runtime_operations"):
                                raise sqlite3.OperationalError("commit境界前の失敗注入")
                            return connection.execute(sql, args)
                    yield Connection()
        with patch.object(self.registry, "transaction", side_effect=failing), self.assertRaises(sqlite3.OperationalError):
            self.observe()
        self.assertEqual(before, self.database())
        self.assertEqual(0, self.handles)
        self.assertTrue(self.observe()["recovered"])

    def test_replay_payload_conflict_hash_change_and_new_run_are_rejected(self):
        self.observe()
        self.fixture.assert_code("OPERATION_CONFLICT", lambda: self.observe(ttlSeconds=61))
        original = self.proof_path.read_bytes()
        self.proof_path.write_bytes(original + b"\n")
        self.fixture.assert_code("PROOF_MISMATCH", self.observe)
        self.proof_path.write_bytes(original)
        self.fixture.start("next-run")
        self.fixture.assert_code("STALE_RUN", self.observe)

    def test_regular_resume_uses_observed_proof_without_rcon_flags(self):
        self.fixture.assert_code("PROOF_REQUIRED", lambda: self.fixture.command("recover", dict(self.fixture.token, action="resume", targetOperationId=None, ttlSeconds=60), "before-observation"))
        self.observe()
        result = self.fixture.command("recover", dict(self.fixture.token, action="resume", targetOperationId=None, ttlSeconds=60), "after-observation")
        self.assertTrue(result["recovered"])
        self.assertFalse(self.registry.runtime._load_run(self.fixture.state())["stopRequested"])

    def test_response_loss_after_commit_replays_recorded_result_without_reobservation(self):
        original = self.registry.transaction
        @contextmanager
        def lost_response(*, read_only=False):
            with original(read_only=read_only) as connection:
                yield connection
            if not read_only:
                raise OSError("commit後の応答欠落")
        with patch.object(self.registry, "transaction", side_effect=lost_response), self.assertRaises(OSError):
            self.observe()
        self.assertEqual("LEASED_STOPPED", self.fixture.state()["state"])
        self.fixture.now = 130
        self.assertTrue(self.observe()["replayed"])
        self.assertEqual(181, self.fixture.state()["expires"])
        self.assertEqual(1, self.observations)

    def test_observed_proof_cannot_be_relabelled_as_rcon_or_changed_independently(self):
        self.observe()
        row = self.fixture.state()
        original_run = self.registry.runtime._load_run(row)
        proof = json.loads(row["runtime_proof"])
        proof["observedStop"]["startId"] = "unrelated"
        with self.registry.transaction() as connection:
            connection.execute("UPDATE instances SET runtime_proof=? WHERE id=?", (json.dumps(proof), row["id"]))
        self.fixture.assert_code("PROOF_MISMATCH", lambda: self.registry.generation._stopped(self.fixture.state()))
        run = dict(original_run)
        run.pop("observedStop")
        proof.pop("observedStop")
        with self.registry.transaction() as connection:
            connection.execute("UPDATE runs SET record=? WHERE id=?", (json.dumps(run), row["run_id"]))
            connection.execute("UPDATE instances SET runtime_proof=? WHERE id=?", (json.dumps(proof), row["id"]))
        self.fixture.assert_code("PROOF_MISMATCH", lambda: self.registry.generation._stopped(self.fixture.state()))
