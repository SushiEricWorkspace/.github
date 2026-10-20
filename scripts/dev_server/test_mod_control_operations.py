"""既SQLite正本のMod原bytes照合。native受付・実プロセス作用は実行しない。"""
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from . import mod_control_operations as control
from .mod_control_wire import decode_frame
from .registry import Registry, RegistryError, SCHEMA
from .runtime_bundle import require_data_generation
from .contracts import ContractError, LAYOUT_ID
from .generation_control import GenerationControl
from .runtime_control import RuntimeControl
from .human_server import HumanServer

VECTORS = json.loads(Path(__file__).with_name("mod-control-vectors-v1.json").read_text(encoding="utf-8"))


def frame(value):
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
    return struct.pack(">I", len(raw)) + raw


class ControlOperationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO instances(id,state) VALUES ('slot-a','AVAILABLE')")
        self.db.execute("INSERT INTO instances(id,state) VALUES ('slot-b','AVAILABLE')")
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        self.request = bytes.fromhex(VECTORS["pairs"][0]["requestFrameHex"])
        self.response = bytes.fromhex(VECTORS["pairs"][0]["responseFrameHex"])

    def test_pending_readback_does_not_mutate_or_reexecute(self):
        self.assertIsNone(control.readback(self.db, "slot-a", self.request))
        self.assertIsNone(control.reserve(self.db, "slot-a", self.request))
        response = control.readback(self.db, "slot-a", self.request)
        self.assertEqual("PENDING", decode_frame(response)["payload"]["outcome"])
        changes = self.db.total_changes
        for _ in range(10):
            self.assertEqual(response, control.reserve(self.db, "slot-a", self.request))
            self.assertEqual(response, control.readback(self.db, "slot-a", self.request))
        self.assertEqual(changes, self.db.total_changes)
        self.assertEqual(1, self.db.execute("SELECT COUNT(*) FROM runtime_operations").fetchone()[0])

    def test_complete_returns_original_response_bytes_after_commit(self):
        control.reserve(self.db, "slot-a", self.request)
        spaced = frame(decode_frame(self.response))
        self.assertNotEqual(self.response, spaced)
        self.assertEqual(spaced, control.complete(self.db, "slot-a", self.request, spaced))
        self.db.commit()
        self.db.execute("BEGIN")
        changes = self.db.total_changes
        self.assertEqual(spaced, control.readback(self.db, "slot-a", self.request))
        self.assertEqual(spaced, control.reserve(self.db, "slot-a", self.request))
        self.assertEqual(spaced, control.unknown(self.db, "slot-a", self.request))
        self.assertEqual(changes, self.db.total_changes)
        with self.assertRaises(RegistryError):
            control.complete(self.db, "slot-a", self.request, self.response)

    def test_unknown_uses_existing_recovery_state_and_cannot_complete(self):
        control.reserve(self.db, "slot-a", self.request)
        response = control.unknown(self.db, "slot-a", self.request)
        self.assertEqual("UNKNOWN", decode_frame(response)["payload"]["outcome"])
        self.assertEqual("RECOVERY_REQUIRED", self.db.execute("SELECT status FROM runtime_operations").fetchone()[0])
        changes = self.db.total_changes
        for _ in range(10):
            self.assertEqual(response, control.unknown(self.db, "slot-a", self.request))
            self.assertEqual(response, control.reserve(self.db, "slot-a", self.request))
            self.assertEqual(response, control.readback(self.db, "slot-a", self.request))
        with self.assertRaises(RegistryError):
            control.complete(self.db, "slot-a", self.request, self.response)
        self.assertEqual(changes, self.db.total_changes)

    def test_same_id_reformatted_nonce_instance_and_descriptor_are_rejected(self):
        control.reserve(self.db, "slot-a", self.request)
        document = decode_frame(self.request)
        for other in (frame(document), frame(dict(document, requestNonce="other-nonce")),
                      frame(dict(document, descriptorSha256="9" * 64))):
            with self.subTest(other=other[:20]), self.assertRaises(RegistryError):
                control.reserve(self.db, "slot-a", other)
        with self.assertRaises(RegistryError):
            control.readback(self.db, "slot-b", self.request)
        record = json.loads(self.db.execute("SELECT response FROM runtime_operations").fetchone()[0])
        self.assertEqual(self.request[4:], record["requestBodyUtf8"].encode())

    def test_hash_alone_does_not_replace_exact_bytes(self):
        control.reserve(self.db, "slot-a", self.request)
        altered = frame(decode_frame(self.request))
        self.db.execute("UPDATE runtime_operations SET fingerprint=?", (control.sha256(altered[4:]),))
        with self.assertRaises(RegistryError):
            control.readback(self.db, "slot-a", altered)

    def test_other_runtime_command_and_other_operation_tables_conflict(self):
        operation = decode_frame(self.request)["operationId"]
        self.db.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                        (operation, control.sha256(self.request[4:]), "start", "slot-a", "PENDING", "{}"))
        with self.assertRaises(RegistryError):
            control.reserve(self.db, "slot-a", self.request)
        self.db.execute("DELETE FROM runtime_operations")
        statements = {
            "operations": "INSERT INTO operations VALUES (?, 'hash', '{}')",
            "reference_operations": "INSERT INTO reference_operations VALUES (?, 'hash', 'slot-a', 'lease', 1, 0)",
            "writer_operations": "INSERT INTO writer_operations VALUES (?, 'hash', 'session', 'writer-io', 'PENDING', '{}')",
            "snapshot_operations": "INSERT INTO snapshot_operations VALUES (?, 'hash', 'slot-a', 'PENDING', '{}')"}
        for table, statement in statements.items():
            self.db.execute(statement, (operation,))
            with self.subTest(table=table), self.assertRaises(RegistryError):
                control.reserve(self.db, "slot-a", self.request)
            self.db.execute("DELETE FROM " + table)
        self.assertIsNone(control.reserve(self.db, "slot-a", self.request))

    def test_reservation_rolls_back_with_callers_transaction(self):
        control.reserve(self.db, "slot-a", self.request)
        self.db.rollback()
        self.db.execute("BEGIN")
        self.assertIsNone(control.readback(self.db, "slot-a", self.request))
        self.db.commit()
        with self.assertRaises(RegistryError):
            control.reserve(self.db, "slot-a", self.request)

    def test_completion_requires_reservation_and_matching_terminal_response(self):
        with self.assertRaises(RegistryError):
            control.complete(self.db, "slot-a", self.request, self.response)
        control.reserve(self.db, "slot-a", self.request)
        pending = control.readback(self.db, "slot-a", self.request)
        for response in (pending, frame(dict(decode_frame(self.response), requestNonce="other-nonce"))):
            with self.subTest(response=response[:20]), self.assertRaises((RegistryError, ValueError)):
                control.complete(self.db, "slot-a", self.request, response)
        self.assertEqual(pending, control.readback(self.db, "slot-a", self.request))

    def test_stored_response_corruption_is_not_regenerated(self):
        control.reserve(self.db, "slot-a", self.request)
        record = json.loads(self.db.execute("SELECT response FROM runtime_operations").fetchone()[0])
        document = decode_frame(self.response)
        document["payload"]["requestBodySha256"] = "9" * 64
        record["responseBodyUtf8"] = json.dumps(document)
        self.db.execute("UPDATE runtime_operations SET status='COMPLETED',response=?", (json.dumps(record),))
        with self.assertRaises(ValueError):
            control.readback(self.db, "slot-a", self.request)


class DataUuidBoundaryTests(unittest.TestCase):
    generation = "00000000-0000-0000-0000-000000000001"

    def test_only_data_runtime_requires_canonical_uuid(self):
        require_data_generation({"schemaVersion": 2}, self.generation)
        for invalid in ("generation-01", self.generation.replace("-", ""), self.generation.upper().replace("1", "A"),
                        "{" + self.generation + "}", None, True):
            with self.subTest(value=invalid), self.assertRaises(ContractError):
                require_data_generation({"schemaVersion": 2}, invalid)
        require_data_generation({"schemaVersion": 1}, "generation-01")

    def test_generation_rejects_before_source_copy_and_operation_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SimpleNamespace(root=Path(directory), runtime=Mock())
            registry.runtime._paths.return_value = Path(directory) / "slot-a"
            registry.transaction = Mock()
            generation = GenerationControl(registry)
            generation._row = Mock(return_value=dict(id="slot-a", generation=None, state="LEASED_STOPPED", pending=0, run_id=None))
            generation._stopped = Mock()
            generation._source = Mock()
            payload = dict(instanceId="slot-a", generationId="generation-01", artifactId="0" * 64)
            artifact = dict(schemaVersion=2, artifactId="0" * 64, layoutId=LAYOUT_ID)
            with patch("dev_server.generation_control.verify_bundle", return_value=artifact):
                with self.assertRaises(ContractError):
                    generation._create("prepare", payload, operation_id="prepare-a", dry_run=False, fingerprint="hash")
            generation._source.assert_not_called()
            registry.runtime._space.assert_not_called()
            registry.transaction.assert_not_called()

    def test_start_rejects_data_generation_before_runtime_reservation(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        registry = SimpleNamespace(transaction=lambda **_: nullcontext(db))
        runtime = RuntimeControl(registry, process_adapter=Mock())
        runtime.refresh = Mock()
        runtime._row = Mock(return_value=dict(id="slot-a", generation="generation-01", generation_bound=1,
                                              state="READY", pending=0, writers=0, children=0, run_id=None))
        runtime._active = Mock(return_value=Path("C:/synthetic-not-opened"))
        runtime._space = Mock()
        runtime._start = Mock()
        payload = dict(instanceId="slot-a", leaseId=self.generation, epoch=1, expectedGeneration="generation-01", timeoutSeconds=1)
        with patch("dev_server.runtime_control.verify_bundle", return_value={"schemaVersion": 2}):
            with self.assertRaises(ContractError):
                runtime.execute("start", payload, operation_id="start-a", dry_run=True)
        runtime._start.assert_not_called()
        runtime._space.assert_not_called()
        self.assertEqual(0, db.execute("SELECT COUNT(*) FROM runtime_operations").fetchone()[0])

    def test_new_lease_is_uuid_and_cached_lease_is_not_rewritten(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        db.execute("INSERT INTO instances(id,state) VALUES ('slot-a','AVAILABLE')")
        registry = Registry.__new__(Registry)
        registry.clock = lambda: 100
        registry.transaction = lambda **_: nullcontext(db)
        request = dict(instanceId="slot-a", owner="codex", ttlSeconds=20)
        result = registry._execute("acquire", request, operation_id="acquire-a", dry_run=False)
        self.assertEqual(str(uuid.UUID(result["leaseId"])), result["leaseId"])
        self.assertEqual(36, len(result["leaseId"]))
        cached = registry._execute("acquire", request, operation_id="acquire-a", dry_run=False)
        self.assertEqual(result["leaseId"], cached["leaseId"])
        # 既存の旧token自体を読み書きで暗黙変換しない。
        db.execute("UPDATE instances SET lease=? WHERE id='slot-a'", ("1" * 32,))
        registry._token(dict(db.execute("SELECT * FROM instances").fetchone()),
                        dict(leaseId="1" * 32, epoch=1, expectedGeneration=None))
        self.assertEqual("1" * 32, db.execute("SELECT lease FROM instances").fetchone()[0])

    def test_automatic_generation_is_uuid_without_restricting_template_names(self):
        server = HumanServer.__new__(HumanServer)
        server.instance = "slot-a"
        server.owner = "codex"
        server.session = {"token": None}
        server.target = Mock()
        server.row = Mock(return_value=dict(owner=None, state="AVAILABLE", generation=None))
        options = dict(mode="prepare", acquire=True, artifactId="0" * 64, ttlSeconds=20, acceptEula=True, seed=17, timeoutSeconds=2)
        steps = server.plan("start", options)
        generation = next(payload["generationId"] for name, payload, _ in steps if name == "prepare")
        self.assertEqual(str(uuid.UUID(generation)), generation)
        self.assertEqual(36, len(generation))
        server.row = Mock(return_value=dict(owner=None, state="AVAILABLE", generation="old-generation"))
        options.update(mode="reset", templateId="initial-world-windows-v1")
        steps = server.plan("start", options)
        reset = next(payload for name, payload, _ in steps if name == "reset")
        self.assertEqual("initial-world-windows-v1", reset["templateId"])
