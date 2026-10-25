"""共有memory SQLiteの独立connectionによる受理済みDB drainの交差。実並行writeは扱わない。"""
from contextlib import contextmanager
import json
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from . import test_mod_control_startup as startup_tests
from .registry import RegistryError


class DatabaseDrainConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = startup_tests.StartupTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        uri = f"file:accepted-drain-{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.actor = sqlite3.connect(uri, uri=True)
        self.addCleanup(self.actor.close)
        self.fixture.db.backup(self.actor)
        self.actor.row_factory = sqlite3.Row
        self.actor.execute("PRAGMA foreign_keys=ON")
        self.observer = sqlite3.connect(uri, uri=True)
        self.addCleanup(self.observer.close)
        self.observer.row_factory = sqlite3.Row
        self.observer.execute("PRAGMA foreign_keys=ON")
        self.fixture.db = self.actor
        self.payload, self.proxy, self.fingerprint = self.fixture.pending_database()
        self.request = json.loads(self.proxy.request_bytes)
        self.fixture.stop()
        self.writer = self.fixture.writer

    def tearDown(self):
        self.fixture.tearDown()

    @contextmanager
    def observing(self):
        self.assertFalse(self.actor.in_transaction)
        self.assertFalse(self.observer.in_transaction)
        self.fixture.db = self.observer
        try:
            yield
        finally:
            self.fixture.db = self.actor

    def snapshot(self):
        return {table: [tuple(row) for row in self.observer.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("instances", "run_admission", "writer_sessions", "writer_operations", "reservations")}

    def response(self, status="RESULT"):
        return {"status": status, "operationId": "io-a", "requestHash": self.fingerprint}

    def accept_close(self):
        with self.observing():
            return self.writer._database_accept(self.payload, "close-a", "close-hash", "writer-close")

    def record(self):
        return self.writer._database_record(self.payload, self.response(), phase="execute",
            close_call=("close-a", "close-hash"), request=self.request)

    def test_close_drains_after_committed_stop_and_records_before_ack(self):
        self.assertEqual("BLOCKED", self.observer.execute("SELECT gate FROM run_admission").fetchone()[0])
        acknowledge = self.proxy.acknowledge.side_effect
        observed = []

        def ack(response_hash):
            self.assertFalse(self.actor.in_transaction)
            operation = self.observer.execute("SELECT status,response FROM writer_operations WHERE id='io-a'").fetchone()
            if json.loads(self.proxy.request_bytes)["command"] == "resolve":
                self.assertEqual("COMPLETED", operation["status"])
                self.assertTrue(json.loads(operation["response"])["completed"])
                self.assertEqual(1, self.observer.execute("SELECT COUNT(*) FROM reservations WHERE operation='close-a'").fetchone()[0])
                self.assertEqual(0, self.observer.execute("SELECT COUNT(*) FROM reservations WHERE operation='io-a'").fetchone()[0])
                observed.append("persisted")
            acknowledge(response_hash)

        self.proxy.acknowledge.side_effect = ack
        with patch("dev_server.writer_control.plain_path"):
            result = self.writer.execute("writer-close", dict(self.payload, drained=True, disconnected=True), operation_id="close-a")
        self.assertTrue(result["closed"])
        self.assertEqual(["persisted"], observed)
        self.assertEqual(("CLOSED", 0), tuple(self.observer.execute("SELECT status,pending FROM writer_sessions").fetchone()))
        self.assertEqual(0, self.observer.execute("SELECT COUNT(*) FROM reservations WHERE reference LIKE 'writer-call:%'").fetchone()[0])
        with self.observing(), self.assertRaises(RegistryError):
            self.writer._database_accept(self.payload, "new-io", "new-hash", "writer-io")

    def test_wrong_binding_owner_request_and_missing_claim_have_no_effect(self):
        self.accept_close()
        cases = [(self.payload, ("other", "close-hash"), self.request),
                 (self.payload, ("close-a", "wrong"), self.request),
                 (self.payload, ("close-a", "close-hash"), dict(self.request, operationId="other")),
                 (self.payload, ("close-a", "close-hash"), dict(self.request, requestHash="wrong"))]
        cases.extend((dict(self.payload, **{key: value}), ("close-a", "close-hash"), self.request)
                     for key, value in (("runId", "old-run"), ("nonce", "old-nonce"),
                                        ("expectedGeneration", "old-generation"), ("sessionId", "other-session"),
                                        ("leaseId", "old-lease"), ("epoch", self.payload["epoch"] + 1)))
        for payload, owner, request in cases:
            with self.subTest(payload=payload, owner=owner, request=request):
                before = self.snapshot()
                with self.assertRaises(RegistryError):
                    self.writer._database_record(payload, self.response(), phase="execute", close_call=owner, request=request)
                self.assertEqual(before, self.snapshot())
        with self.observing():
            self.observer.execute("DELETE FROM reservations WHERE operation='close-a'")
            self.observer.commit()
        before = self.snapshot()
        with self.assertRaises(RegistryError):
            self.record()
        self.assertEqual(before, self.snapshot())

    def test_failed_close_rolls_back_revision_operation_and_session(self):
        self.accept_close()
        before = self.snapshot()
        with self.assertRaises(RegistryError) as caught:
            self.writer._database_record(self.payload,
                {"status": "CLOSED", "operationId": "close-a", "requestHash": "close-hash"},
                phase="close", exit_proof={"knownExited": True})
        self.assertEqual("WRITER_PENDING", caught.exception.code)
        self.assertEqual(before, self.snapshot())
        self.record()
        with self.observing():
            self.writer._database_record(self.payload,
                {"status": "CLOSED", "operationId": "close-a", "requestHash": "close-hash"},
                phase="close", exit_proof={"knownExited": True})
        self.assertEqual("CLOSED", self.actor.execute("SELECT status FROM writer_sessions").fetchone()[0])

    def test_unknown_then_completed_cannot_be_downgraded_by_peer(self):
        self.accept_close()
        self.writer._database_record(self.payload, self.response("UNKNOWN"), phase="execute",
            close_call=("close-a", "close-hash"), request=self.request)
        self.assertEqual("PENDING", self.observer.execute("SELECT status FROM writer_operations WHERE id='io-a'").fetchone()[0])
        completed = self.record()
        before = self.snapshot()
        with self.observing():
            self.assertEqual(completed, self.writer._database_record(self.payload, self.response(), phase="execute"))
            with self.assertRaises(RegistryError):
                self.writer._database_record(self.payload, self.response("UNKNOWN"), phase="execute")
        self.assertEqual(before, self.snapshot())

    def test_closed_readback_has_no_claim_ack_or_database_effect(self):
        close = dict(self.payload, drained=True, disconnected=True)
        with patch("dev_server.writer_control.plain_path"):
            self.writer.execute("writer-close", close, operation_id="close-a")
            before = self.snapshot()
            calls = list(self.proxy.mock_calls)
            changes = self.observer.total_changes
            with self.observing():
                result = self.writer.execute("writer-close", close, operation_id="close-a")
                self.assertTrue(result["closed"])
                self.assertTrue(result["replayed"])
                with self.assertRaises(RegistryError):
                    self.writer.execute("writer-close", close, operation_id="other-close")
            self.assertEqual(changes, self.observer.total_changes)
            self.assertEqual(calls, self.proxy.mock_calls)
            self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
