"""SQLite memoryと合成wireだけのprivate startup。実OS/native/承認の受入ではない。"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import ctypes
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from .registry import Registry, RegistryError, SCHEMA
from .runtime_control import RuntimeControl
from . import runtime_control as runtime_module
from . import registry as registry_module
from .writer_control import WriterControl
from . import mod_control_startup as startup
from . import mod_control_wire as wire
from . import mod_control_operations as operations
from .mod_control_transport import ModControlTransport
from .test_mod_control_transport import FakeNative
from .test_managed_profile import fixture, observed as launch_observed
from .test_mod_control_wire_v2 import VECTORS, frame, data


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.depth = 0
        self.registry = Registry.__new__(Registry)
        original = deepcopy(VECTORS["contexts"]["Windows"])
        descriptor = wire.decode_descriptor(original["descriptorJson"].encode())
        self.registry.root = Path(descriptor["managementRoot"])
        descriptor["managementRoot"] = str(self.registry.root)
        self.registry.clock = lambda: 1000
        self.registry.inventory = {"instances": [{"instanceId": "slot-a"}]}
        self.registry.transaction = self.transaction
        self.runtime = RuntimeControl(self.registry, process_adapter=Mock())
        self.registry.runtime = self.runtime
        self.runtime._transaction = lambda instance, read_only=False: self.transaction(read_only=read_only)
        self.writer = WriterControl(self.registry, process_adapter=Mock())
        self.registry.writer = self.writer
        self.token = {"instanceId": "slot-a", "leaseId": descriptor["leaseId"], "epoch": descriptor["epoch"],
                      "expectedGeneration": descriptor["generationId"]}
        self.manifest_raw = b'{"fixture":"independent-runtime"}'
        descriptor["runtimeManifestHash"] = wire.sha256(self.manifest_raw)
        profile, launch, _ = fixture("Windows")
        generation = self.registry.root / "instances/slot-a/generations" / descriptor["generationId"]
        launch.update(bundle=str(generation / "runtime"), run=str(generation), auxiliary=str(generation / "aux"),
                      privateTmp=str(generation / "private"), controlDescriptor=wire.descriptor_path(descriptor),
                      runtimeManifest=str(generation / "runtime/runtime.json"), instanceId="slot-a",
                      generationId=descriptor["generationId"], startNonce=descriptor["startNonce"])
        self.profile = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode()
        self.context = {key: original[key] for key in ("descriptorJson", "owner", "management", "expectedEvidence")}
        self.context["expectedEvidence"]["profileSha256"] = wire.sha256(self.profile)
        self.context["descriptorJson"] = json.dumps(descriptor, separators=(",", ":"))
        raw_hash = wire.sha256(self.context["descriptorJson"].encode())
        launch["controlDescriptorSha256"] = raw_hash
        self.launch = launch
        self.observed = launch_observed(profile, launch)
        self.frames = []
        session = next(s for s in VECTORS["sessions"] if s["contextId"] == "Windows")
        for pair in session["pairs"][:2]:
            request, response = wire.decode_frame_v2(data(pair["request"])), wire.decode_frame_v2(data(pair["response"]))
            request["descriptorSha256"] = response["descriptorSha256"] = raw_hash
            evidence = json.loads(request["payload"]["profileEvidenceJson"])
            evidence.update(self.context["expectedEvidence"], descriptorSha256=raw_hash,
                            runtimeManifestHash=descriptor["runtimeManifestHash"])
            text = json.dumps(evidence, separators=(",", ":"))
            request["payload"].update(profileEvidenceJson=text, profileEvidenceSha256=wire.sha256(text.encode()))
            req = frame(request)
            response["payload"].update(requestBodySha256=wire.sha256(req[4:]), profileEvidenceSha256=wire.sha256(text.encode()))
            self.frames.append((req, frame(response)))
        start_fingerprint = runtime_module.digest(runtime_module.canonical({"command": "start", "payload": dict(self.token, timeoutSeconds=10)}))
        self.run = {"runId": descriptor["runtimeRunId"], "instanceId": "slot-a", "generationId": descriptor["generationId"],
                    "startOperationId": "startup-a", "startRequestHash": start_fingerprint,
                    "artifactId": descriptor["artifactId"], "nonce": descriptor["startNonce"], "manifestHash": descriptor["runtimeManifestHash"]}
        self.db.execute("INSERT INTO instances(id,state,generation,epoch,lease,expires,generation_bound,artifact,run_id,pending) VALUES (?,'STARTING',?,?,?,9999,1,?,?,1)",
            ("slot-a", descriptor["generationId"], descriptor["epoch"], descriptor["leaseId"], descriptor["artifactId"], descriptor["runtimeRunId"]))
        self.db.execute("INSERT INTO runtime_operations VALUES (?,?, 'start','slot-a','PENDING',?)",
            ('startup-a', start_fingerprint, json.dumps({'instanceId':'slot-a','pending':True})))
        self.db.execute("INSERT INTO reservations VALUES ('startup-a',?,'slot-a',?,?)",
            ('artifact:' + descriptor['artifactId'], descriptor['leaseId'], descriptor['epoch']))
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?)", (self.run["runId"], "slot-a", descriptor["generationId"], json.dumps(self.run)))
        self.db.execute("INSERT INTO run_admission VALUES (?,?,?,?,?,?,'CLOSED',0,NULL,'startup-a')",
            (self.run["runId"], "slot-a", descriptor["leaseId"], descriptor["epoch"], descriptor["generationId"], descriptor["startNonce"]))
        self.db.commit()
        self.native_calls = []
        self.channel = Mock()
        self.channel.receive_frame.side_effect = self.receive
        self.channel.send_frame.side_effect = self.send
        self.endpoint = Mock()
        self.endpoint.accept.side_effect = self.accept
        self.transport = Mock()
        self.transport.create.side_effect = self.create
        self.received = iter([self.frames[0][0], self.frames[1][0]])
        self.sent = []
        self.inputs = dict(profile_raw=self.profile, launch_context=self.launch, observed=self.observed, context=self.context,
                           revision=0, deadline=12.0)
        stack = self.enterContext(ExitStack())
        self.bundle_validator = stack.enter_context(patch.object(startup, "validate_bundle_managed_launch"))
        stack.enter_context(patch.object(startup, "regular_bytes", return_value=self.manifest_raw))
        stack.enter_context(patch.object(startup, "verify_bundle", return_value={"artifactId": self.run["artifactId"],
            "files": [{"path": "launch/managed-server-candidate.json", "size": len(self.profile), "sha256": wire.sha256(self.profile)}]}))
        self.spies = {}
        for obj, name in [(socket, "socket"), (socket, "create_connection"), (subprocess, "Popen"), (subprocess, "run"),
                          (ctypes, "CDLL"), (os, "open"), (os, "kill")]:
            self.spies[name] = stack.enter_context(patch.object(obj, name, side_effect=AssertionError("禁止OS作用:" + name)))
        if hasattr(ctypes, "WinDLL"):
            self.spies["WinDLL"] = stack.enter_context(patch.object(ctypes, "WinDLL", side_effect=AssertionError("禁止WinDLL")))

    @contextmanager
    def transaction(self, *, read_only=False):
        self.assertEqual(0, self.depth)
        self.depth += 1
        self.db.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self.depth -= 1

    def create(self, context, raw):
        self.assertEqual(0, self.depth)
        self.assertEqual(wire.sha256(raw), context["descriptorSha256"])
        self.native_calls.append("create")
        return self.endpoint

    def accept(self, identity, deadline):
        self.assertEqual(0, self.depth)
        self.assertEqual(self.context["owner"], identity)
        self.native_calls.append("accept")
        return self.channel

    def receive(self, deadline):
        self.assertEqual(0, self.depth)
        return next(self.received)

    def send(self, raw, deadline):
        self.assertEqual(0, self.depth)
        self.assertEqual("CLOSED", self.gate()["gate"])
        request = self.frames[len(self.sent)][0]
        with self.transaction(read_only=True) as db:
            self.assertEqual(raw, operations.readback(db, "slot-a", request))
        self.sent.append(raw)
        return len(raw)

    def row(self):
        return dict(self.db.execute("SELECT * FROM instances WHERE id='slot-a'").fetchone())

    def gate(self):
        return dict(self.db.execute("SELECT * FROM run_admission").fetchone())

    def start(self, **changes):
        return self.runtime._managed_startup(self.token, transport=self.transport, **dict(self.inputs, **changes))

    def writer_session(self, *, pending=0, status="OPEN"):
        self.db.execute("INSERT INTO writer_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("session-a", "slot-a", self.token["leaseId"],
            self.token["epoch"], self.token["expectedGeneration"], self.run["runId"], self.run["nonce"], 123, '{}', status, pending))
        self.db.execute("UPDATE instances SET writers=1 WHERE id='slot-a'")
        self.db.commit()
        return dict(self.token, sessionId="session-a", runId=self.run["runId"], nonce=self.run["nonce"])

    def stop(self):
        return self.runtime._reserve_managed_stop(dict(self.token, timeoutSeconds=10), operation_id="stop-a", revision=self.gate()["revision"])

    def test_handshake_saves_before_send_and_opens_after_both_sends(self):
        self.assertEqual(self.frames[1][1], self.start())
        self.assertEqual([self.frames[0][1], self.frames[1][1]], self.sent)
        self.assertEqual(("RUNNING", 0), (self.row()["state"], self.row()["pending"]))
        self.assertEqual("OPEN", self.gate()["gate"])
        self.bundle_validator.assert_called_once_with(Path(self.launch["bundle"]), self.launch, self.observed)
        for spy in self.spies.values(): spy.assert_not_called()

    def test_ack_send_failure_keeps_saved_bytes_and_closes_gate(self):
        error = OSError("synthetic")
        original = self.send
        def send(raw, deadline):
            result = original(raw, deadline)
            if len(self.sent) == 2: raise error
            return result
        self.channel.send_frame.side_effect = send
        with self.assertRaises(OSError) as caught: self.start()
        self.assertIs(error, caught.exception)
        self.assertEqual("BLOCKED", self.gate()["gate"])
        with self.transaction(read_only=True) as db:
            self.assertEqual(self.frames[1][1], operations.readback(db, "slot-a", self.frames[1][0]))
        calls = len(self.sent)
        with self.assertRaises(RegistryError): self.start()
        self.assertEqual(calls, len(self.sent))
        self.assertIs(self.channel, self.runtime._control.sessions["slot-a"]["connection"])

    def test_wrong_profile_and_observed_rejected_before_native(self):
        with self.assertRaises(RegistryError): self.start(profile_raw=self.profile + b" ")
        self.assertEqual([], self.native_calls)
        self.assertEqual("CLOSED", self.gate()["gate"])

    def test_stale_revision_and_bool_rejected(self):
        for revision in (True, 0.0, 1):
            self.runtime._control = None
            with self.subTest(revision=revision), self.assertRaises(RegistryError): self.start(revision=revision)
        self.assertEqual([], self.native_calls)

    def test_missing_gate_does_not_open_writer(self):
        self.db.execute("DELETE FROM run_admission"); self.db.execute("UPDATE instances SET state='RUNNING',pending=0"); self.db.commit()
        with self.transaction() as db, self.assertRaises(RegistryError): self.writer._bound(db, self.token)

    def test_open_writer_checks_run_nonce_and_revision(self):
        self.start()
        with self.transaction() as db:
            row = self.writer._bound(db, dict(self.token, runId=self.run["runId"], nonce=self.run["nonce"]))
            self.assertEqual(self.gate()["revision"], row["admission_revision"])
            with self.assertRaises(RegistryError): startup._require_open(db, row, revision=True)
        payload = self.writer_session()
        for key, wrong in (("runId", "other"), ("nonce", "other"), ("expectedGeneration", "other")):
            with self.transaction() as db, self.assertRaises(RegistryError): self.writer._session(db, dict(payload, **{key: wrong}))

    def test_stop_is_atomic_and_blocks_new_accept_but_preserves_existing_claim(self):
        self.start(); payload = self.writer_session(pending=1)
        self.db.execute("INSERT INTO writer_operations VALUES ('io-a','hash-a','session-a','writer-io','PENDING','{}')")
        self.db.execute("INSERT INTO reservations VALUES ('io-a','writer-call:hash-a','slot-a',?,?)", (self.token['leaseId'], self.token['epoch']))
        self.db.commit()
        self.stop()
        self.assertEqual(("STOPPING", 1, "BLOCKED"), (self.row()["state"], self.row()["pending"], self.gate()["gate"]))
        with self.transaction() as db:
            with self.assertRaises(RegistryError): self.writer._bound(db, self.token)
            self.writer._reserved(db, payload, "io-a", "hash-a", "writer-io")
        for key, wrong in (("nonce", "old"), ("runId", "old")):
            with self.transaction() as db, self.assertRaises(RegistryError): self.writer._reserved(db, dict(payload, **{key: wrong}), "io-a", "hash-a", "writer-io")
        self.db.execute("DELETE FROM reservations WHERE operation='io-a'"); self.db.commit()
        with self.transaction() as db, self.assertRaises(RegistryError): self.writer._reserved(db, payload, "io-a", "hash-a", "writer-io")

    def test_database_record_finishes_only_accepted_claim_after_block(self):
        self.start(); payload = self.writer_session(pending=1)
        self.db.execute("INSERT INTO writer_operations VALUES ('io-a','hash-a','session-a','writer-io','PENDING','{}')")
        self.db.execute("INSERT INTO reservations VALUES ('io-a','writer-call:hash-a','slot-a',?,?)", (self.token['leaseId'], self.token['epoch']))
        self.db.commit(); self.stop()
        result = self.writer._database_record(payload, {'status':'RESULT','operationId':'io-a','requestHash':'hash-a'}, phase='execute')
        self.assertTrue(result['completed'])
        self.assertEqual('BLOCKED', self.gate()['gate'])
        self.assertEqual(0, self.db.execute("SELECT pending FROM writer_sessions").fetchone()[0])
        with self.assertRaises(RegistryError): self.writer._database_accept(payload, 'new-a', 'new-hash', 'writer-io')

    def test_closed_session_cannot_be_reopened_during_stop(self):
        self.start(); payload = self.writer_session(status='CLOSED'); self.stop()
        with self.transaction() as db, self.assertRaises(RegistryError): self.writer._session(db, payload, closing=True)

    def test_admission_cas_rollback_and_other_instance_isolation(self):
        before = self.gate()
        with self.assertRaises(RuntimeError), self.transaction() as db:
            startup._change(db, self.row(), 0, 'BLOCKED')
            raise RuntimeError('rollback')
        self.assertEqual(before, self.gate())
        other = dict(self.row(), run_id='other')
        with self.transaction() as db, self.assertRaises(RegistryError): startup._admission(db, other)

    def test_same_original_request_readback_does_not_reopen_gate(self):
        self.start(); self.stop(); before = self.gate()
        with self.transaction() as db:
            self.assertEqual(self.frames[0][1], operations.readback(db, 'slot-a', self.frames[0][0]))
            self.assertEqual(self.frames[1][1], operations.unknown(db, 'slot-a', self.frames[1][0]))
        self.assertEqual(before, self.gate())

    def tearDown(self):
        for spy in self.spies.values(): spy.assert_not_called()

    def test_independent_observed_mismatch_before_native(self):
        actual = deepcopy(self.observed)
        actual['environment']['INJECTED'] = 'value'
        with self.assertRaises(ValueError): self.start(observed=actual)
        self.assertEqual([], self.native_calls)
        self.assertEqual('CLOSED', self.gate()['gate'])

    def test_native_failure_keeps_owner_and_original_error(self):
        error = OSError('native unknown')
        self.endpoint.accept.side_effect = error
        with self.assertRaises(OSError) as caught: self.start()
        self.assertIs(error, caught.exception)
        self.assertIs(self.endpoint, self.runtime._control.sessions['slot-a']['endpoint'])
        self.assertEqual('BLOCKED', self.gate()['gate'])
        self.transport.close.assert_not_called()

    def test_reentrant_same_slot_has_no_second_native_issue(self):
        initial = self.receive
        attempted = []
        def receive(deadline):
            if not attempted:
                attempted.append(True)
                with self.assertRaises(RegistryError): self.start()
            return initial(deadline)
        self.channel.receive_frame.side_effect = receive
        self.start()
        self.assertEqual(['create', 'accept'], self.native_calls)

    def test_open_commit_failure_does_not_erase_completed_ack(self):
        transaction = self.runtime._transaction
        error = OSError('commit unknown')
        failed = []
        @contextmanager
        def fail(instance, read_only=False):
            with transaction(instance, read_only=read_only) as db:
                yield db
                if not failed and len(self.sent) == 2 and self.gate()['gate'] == 'OPEN':
                    failed.append(True)
                    raise error
        self.runtime._transaction = fail
        with self.assertRaises(OSError) as caught: self.start()
        self.assertIs(error, caught.exception)
        self.assertEqual('BLOCKED', self.gate()['gate'])
        with self.transaction(read_only=True) as db:
            self.assertEqual(self.frames[1][1], operations.readback(db, 'slot-a', self.frames[1][0]))

    def test_database_new_accept_stale_revision_and_claimless_finish_rejected(self):
        self.start(); payload = self.writer_session()
        revision = self.gate()['revision']
        with self.transaction() as db: startup._touch(db, self.row())
        with self.assertRaises(RegistryError): self.writer._database_accept(payload, 'io-a', 'hash-a', 'writer-io', revision=revision)
        self.db.execute("UPDATE writer_sessions SET pending=1")
        self.db.execute("INSERT INTO writer_operations VALUES ('io-a','hash-a','session-a','writer-io','PENDING','{}')")
        self.db.commit(); self.stop()
        with self.assertRaises(RegistryError): self.writer._database_record(payload,
            {'status':'RESULT','operationId':'io-a','requestHash':'hash-a'}, phase='execute')
        self.assertEqual('PENDING', self.db.execute("SELECT status FROM writer_operations WHERE id='io-a'").fetchone()[0])

    def test_database_close_after_stop_finishes_only_same_accepted_session(self):
        self.start(); payload = self.writer_session()
        self.stop()
        self.writer._database_accept(payload, 'close-a', 'hash-a', 'writer-close')
        response = {'status':'CLOSED','operationId':'close-a','requestHash':'hash-a'}
        result = self.writer._database_record(payload, response, phase='close', exit_proof={'knownExited': True})
        self.assertTrue(result['closed'])
        self.assertEqual('CLOSED', self.db.execute("SELECT status FROM writer_sessions").fetchone()[0])
        self.assertEqual('BLOCKED', self.gate()['gate'])

    def test_normal_start_proof_opens_and_normal_stop_blocks_without_new_caller(self):
        self.db.execute('DELETE FROM run_admission'); self.db.execute('DELETE FROM runs')
        self.db.execute('DELETE FROM reservations'); self.db.execute('DELETE FROM runtime_operations')
        self.db.execute("UPDATE instances SET state='READY',pending=0,run_id=NULL")
        self.db.commit()
        self.runtime.refresh = Mock()
        self.runtime._active = Mock(return_value=Path(self.launch['run']))
        self.runtime._space = Mock()
        def start(row, payload, generation, endpoints, operation):
            self.run['startOperationId'] = operation
            self.run['startRequestHash'] = self.db.execute('SELECT fingerprint FROM runtime_operations WHERE id=?', (operation,)).fetchone()[0]
            self.runtime._record(self.run)
            with self.transaction() as db:
                db.execute('UPDATE instances SET run_id=?', (self.run['runId'],))
                self.assertEqual('CLOSED', self.gate()['gate'])
            return {'instanceId': 'slot-a', 'runId': self.run['runId']}
        self.runtime._start = Mock(side_effect=start)
        payload = dict(self.token, timeoutSeconds=10)
        with patch.object(runtime_module, 'verify_bundle', return_value={'schemaVersion': 1}), \
             patch.object(runtime_module, 'instance_endpoints', return_value={}):
            self.runtime.execute('start', payload, operation_id='ordinary-start')
            self.assertEqual('OPEN', self.gate()['gate'])
            self.assertIsNone(self.runtime._control)
            self.runtime.processes[self.run['runId']] = Mock()
            def stop(row, payload, generation):
                self.assertEqual('BLOCKED', self.gate()['gate'])
                self.assertEqual(0, self.depth)
                return {'instanceId': 'slot-a', 'runId': self.run['runId']}
            self.runtime._stop = Mock(side_effect=stop)
            self.runtime.execute('stop', payload, operation_id='ordinary-stop')
        self.assertEqual('LEASED_STOPPED', self.row()['state'])
        self.assertEqual('BLOCKED', self.gate()['gate'])
        self.assertEqual([], self.native_calls)

    def test_normal_stop_keeps_existing_pending_guard(self):
        self.start(); self.writer_session(pending=1)
        self.runtime.refresh = Mock()
        self.runtime._active = Mock(return_value=Path(self.launch['run']))
        self.runtime.processes[self.run['runId']] = Mock()
        with patch.object(runtime_module, 'instance_endpoints', return_value={}), self.assertRaises(RegistryError):
            self.runtime.execute('stop', dict(self.token, timeoutSeconds=10), operation_id='ordinary-stop')
        self.assertEqual('RUNNING', self.row()['state'])
        self.assertEqual('OPEN', self.gate()['gate'])

    def test_old_v4_marker_is_rejected_before_database_or_migration(self):
        class OldRoot:
            def __truediv__(self, name): return self
            def read_text(self, **kwargs): return 'registry-v4\n'
        with patch.object(registry_module, 'require_private'), \
             patch('dev_server.windows_security.require_local_volume'), \
             patch.object(sqlite3, 'connect') as connect, self.assertRaises(RegistryError):
            Registry(OldRoot())
        connect.assert_not_called()

    def test_real_transport_body_with_only_native_adapter_mock(self):
        native = FakeNative()
        native.transport = 'windows-pipe'
        native.chunk = 65536
        native.channel.incoming = self.frames[0][0] + self.frames[1][0]
        native.authenticate.side_effect = lambda *args: self.assertEqual(0, self.depth)
        operation = native.operation
        def outside(*args):
            self.assertEqual(0, self.depth)
            return operation(*args)
        native.operation = outside
        owner = ModControlTransport(['slot-a'], native=native, clock=lambda: 1)
        self.addCleanup(lambda: owner.close(12))
        result = self.runtime._managed_startup(self.token, transport=owner, **self.inputs)
        self.assertEqual(self.frames[1][1], result)
        self.assertEqual(self.frames[0][1] + self.frames[1][1], native.channel.sent)
        self.assertEqual('OPEN', self.gate()['gate'])
        self.assertTrue(all(op.released for op in native.operations))

    def test_accepted_io_finishes_and_session_closes_after_stop_reservation(self):
        self.start(); payload = self.writer_session()
        self.writer.process_adapter.identity.return_value = {}
        self.runtime._running = Mock()
        self.writer._preflight = Mock()
        def effect(row, request, operation):
            self.assertEqual(0, self.depth)
            self.stop()
            return {'written': True}
        self.writer._io = Mock(side_effect=effect)
        response = self.writer.execute('writer-io', dict(payload, request={'action':'write','path':'config.yml','text':'fixture'}), operation_id='io-a')
        self.assertTrue(response['written'])
        self.assertEqual('BLOCKED', self.gate()['gate'])
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM reservations WHERE reference LIKE 'writer-call:%'").fetchone()[0])
        self.assertEqual('COMPLETED', self.db.execute("SELECT status FROM writer_operations WHERE id='io-a'").fetchone()[0])
        self.writer._has_database = Mock(return_value=False)
        result = self.writer.execute('writer-close', dict(payload, drained=True, disconnected=True), operation_id='close-a')
        self.assertTrue(result['closed'])
        self.assertEqual('STOPPING', self.row()['state'])
        with self.assertRaises(RegistryError):
            self.writer.execute('writer-io', dict(payload, request={'action':'list','path':''}), operation_id='new-a')

    def test_incomplete_send_never_opens_gate(self):
        def send(raw, deadline):
            self.send(raw, deadline)
            return 0
        self.channel.send_frame.side_effect = send
        with self.assertRaises(RegistryError): self.start()
        self.assertEqual('BLOCKED', self.gate()['gate'])
        self.assertEqual('RECOVERY_REQUIRED', self.row()['state'])

    def test_responses_bind_received_operation_and_nonce_without_changing_p_nonce(self):
        changed = []
        for index, (request, response) in enumerate(self.frames):
            req, res = wire.decode_frame_v2(request), wire.decode_frame_v2(response)
            req['operationId'] = res['operationId'] = 'received-op-' + str(index)
            req['requestNonce'] = res['requestNonce'] = 'received-request-' + str(index)
            request = frame(req)
            res['payload']['requestBodySha256'] = wire.sha256(request[4:])
            changed.append((request, frame(res)))
        self.frames = changed
        self.received = iter([pair[0] for pair in changed])
        self.assertEqual(changed[1][1], self.start())
        self.assertEqual(self.run['nonce'], self.gate()['nonce'])
        self.assertEqual(self.run['nonce'], json.loads(self.db.execute('SELECT record FROM runs').fetchone()[0])['nonce'])

    def test_start_operation_completes_once_with_runtime_result_and_exact_reference_release(self):
        self.db.execute("INSERT INTO reservations VALUES ('keep-a','artifact:other','slot-a',?,?)", (self.token['leaseId'], self.token['epoch']))
        self.db.commit()
        self.start()
        op = self.db.execute("SELECT * FROM runtime_operations WHERE id='startup-a'").fetchone()
        self.assertEqual('COMPLETED', op['status'])
        self.assertEqual({'instanceId':'slot-a','generation':self.token['expectedGeneration'], 'runId':self.run['runId'],
                          'running':True,'snapshotClean':False}, json.loads(op['response']))
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM reservations WHERE operation='startup-a'").fetchone()[0])
        self.assertEqual(1, self.db.execute("SELECT COUNT(*) FROM reservations WHERE operation='keep-a'").fetchone()[0])
        before = self.db.total_changes
        with self.assertRaises(RegistryError): self.start()
        self.assertEqual(before, self.db.total_changes)
        self.assertEqual(2, len(self.sent))

    def test_invalid_origin_kind_fingerprint_status_and_artifact_binding_have_no_effect(self):
        for column, value in [('command','stop'),('fingerprint','wrong'),('status','COMPLETED'),('instance','slot-b')]:
            self.runtime._control = None
            old = self.db.execute('SELECT ' + column + " FROM runtime_operations WHERE id='startup-a'").fetchone()[0]
            self.db.execute('UPDATE runtime_operations SET ' + column + "=? WHERE id='startup-a'", (value,)); self.db.commit()
            before = self.db.total_changes; gate = self.gate()
            with self.subTest(column=column), self.assertRaises(RegistryError): self.start()
            self.assertEqual(before, self.db.total_changes); self.assertEqual(gate, self.gate())
            self.db.execute('UPDATE runtime_operations SET ' + column + "=? WHERE id='startup-a'", (old,)); self.db.commit()
        self.runtime._control = None
        self.db.execute("UPDATE reservations SET epoch=epoch+1 WHERE operation='startup-a'"); self.db.commit()
        before = self.db.total_changes
        with self.assertRaises(RegistryError): self.start()
        self.assertEqual(before, self.db.total_changes)
        self.assertEqual([], self.native_calls)

    def test_missing_origin_cannot_issue_native(self):
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.execute("DELETE FROM runtime_operations WHERE id='startup-a'"); self.db.commit()
        before = self.db.total_changes
        with self.assertRaises(RegistryError): self.start()
        self.assertEqual(before, self.db.total_changes)
        self.assertEqual([], self.native_calls)

    def test_authenticated_bound_readback_after_completed_start_has_no_state_changes(self):
        self.start()
        self.received = iter([self.frames[1][0]])
        self.channel.send_frame.side_effect = lambda raw, deadline: len(raw)
        before = self.db.total_changes; gate = self.gate()
        self.assertEqual(self.frames[1][1], self.runtime._managed_readback(self.token, deadline=12))
        self.assertEqual(before, self.db.total_changes); self.assertEqual(gate, self.gate())
        self.assertEqual('COMPLETED', self.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])

    def test_ack_send_unknown_preserves_completed_ack_and_same_origin_unknown_only(self):
        self.channel.send_frame.side_effect = OSError('send unknown')
        with self.assertRaises(OSError): self.start()
        self.assertEqual('RECOVERY_REQUIRED', self.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])
        self.assertEqual(self.run['startRequestHash'], self.db.execute("SELECT fingerprint FROM runtime_operations WHERE id='startup-a'").fetchone()[0])
        self.assertEqual(1, self.db.execute("SELECT COUNT(*) FROM reservations WHERE operation='startup-a'").fetchone()[0])

    def test_completed_commit_response_loss_does_not_downgrade_start_ack_or_gate(self):
        transaction = self.runtime._transaction
        lost = []
        error = OSError('reply lost after commit')
        @contextmanager
        def lose(instance, read_only=False):
            with transaction(instance, read_only=read_only) as db:
                yield db
            if not lost and len(self.sent) == 2 and self.gate()['gate'] == 'OPEN':
                lost.append(True)
                raise error
        self.runtime._transaction = lose
        with self.assertRaises(OSError) as caught: self.start()
        self.assertIs(error, caught.exception)
        self.assertEqual('OPEN', self.gate()['gate'])
        self.assertEqual('RUNNING', self.row()['state'])
        self.assertEqual('COMPLETED', self.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])
        self.received = iter([self.frames[1][0]])
        self.channel.send_frame.side_effect = lambda raw, deadline: len(raw)
        before = self.db.total_changes
        self.assertEqual(self.frames[1][1], self.runtime._managed_readback(self.token, deadline=12))
        self.assertEqual(before, self.db.total_changes)

    def test_malformed_hello_cannot_prevent_origin_unknown_preservation(self):
        self.channel.receive_frame.side_effect = lambda deadline: b'bad-frame'
        produced = []
        reply = startup._reply
        def capture(*args):
            try: return reply(*args)
            except BaseException as error:
                produced.append(error)
                raise
        with patch.object(startup, '_reply', side_effect=capture), self.assertRaises(ValueError) as caught:
            self.start()
        self.assertIs(produced[0], caught.exception)
        self.assertEqual(('BLOCKED', 'RECOVERY_REQUIRED'), (self.gate()['gate'], self.row()['state']))
        self.assertEqual('RECOVERY_REQUIRED', self.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])
        self.assertEqual([], self.sent)

    def test_bound_operation_collision_cannot_rollback_origin_unknown_preservation(self):
        request = wire.decode_frame_v2(self.frames[1][0]); request['operationId'] = 'startup-a'
        self.received = iter([self.frames[0][0], frame(request)])
        reserve = operations.reserve; produced = []
        def capture(*args):
            try: return reserve(*args)
            except BaseException as error:
                produced.append(error)
                raise
        with patch.object(operations, 'reserve', side_effect=capture), self.assertRaises(RegistryError) as caught:
            self.start()
        self.assertIs(produced[0], caught.exception)
        self.assertEqual('OPERATION_CONFLICT', caught.exception.code)
        self.assertEqual('RECOVERY_REQUIRED', self.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])
        self.assertEqual('BLOCKED', self.gate()['gate'])
        with self.transaction(read_only=True) as db:
            self.assertEqual(self.frames[0][1], operations.readback(db, 'slot-a', self.frames[0][0]))
        self.assertEqual(1, len(self.sent))

    def test_descriptor_commit_reply_loss_preserves_origin_before_native(self):
        transaction = self.runtime._transaction; lost = []; error = OSError('descriptor commit reply lost')
        @contextmanager
        def lose(instance, read_only=False):
            with transaction(instance, read_only=read_only) as db:
                yield db
            if not lost and self.gate()['descriptor_hash'] is not None and self.gate()['gate'] == 'CLOSED':
                lost.append(True)
                raise error
        self.runtime._transaction = lose
        with self.assertRaises(OSError) as caught: self.start()
        self.assertIs(error, caught.exception)
        self.assertEqual('BLOCKED', self.gate()['gate'])
        self.assertEqual('RECOVERY_REQUIRED', self.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])
        self.transport.create.assert_not_called()
        self.assertEqual(1, self.db.execute("SELECT COUNT(*) FROM reservations WHERE operation='startup-a'").fetchone()[0])

    def test_unknown_does_not_change_replaced_descriptor(self):
        error = OSError('ownership changed'); changes = []
        def accept(*args):
            self.db.execute("UPDATE run_admission SET descriptor_hash=?", ('f' * 64,)); self.db.commit()
            changes.append(self.db.total_changes)
            raise error
        self.endpoint.accept.side_effect = accept
        with self.assertRaises(OSError) as caught: self.start()
        self.assertIs(error, caught.exception)
        self.assertEqual(changes[0], self.db.total_changes)
        self.assertEqual('PENDING', self.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])

    def test_unknown_does_not_change_replaced_run(self):
        error = OSError('run changed'); changes = []
        def accept(*args):
            self.db.execute("UPDATE instances SET run_id='other-run'"); self.db.commit()
            changes.append(self.db.total_changes)
            raise error
        self.endpoint.accept.side_effect = accept
        with self.assertRaises(OSError) as caught: self.start()
        self.assertIs(error, caught.exception)
        self.assertEqual(changes[0], self.db.total_changes)
        self.assertEqual('PENDING', self.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])

    def test_non_database_close_readback_after_stopping_has_no_new_effect(self):
        self.start(); payload = self.writer_session(); self.stop()
        self.writer.process_adapter.identity.return_value = {}
        path = Mock(); path.exists.return_value = False
        self.writer._database_path = Mock(return_value=path)
        close = dict(payload, drained=True, disconnected=True)
        with patch('dev_server.writer_control.plain_path'):
            response = self.writer.execute('writer-close', close, operation_id='close-a')
            self.assertTrue(response['closed'])
            before = self.db.total_changes
            replay = self.writer.execute('writer-close', close, operation_id='close-a')
            self.assertTrue(replay['closed']); self.assertTrue(replay['replayed'])
            self.assertEqual(before, self.db.total_changes)
            with self.assertRaises(RegistryError): self.writer.execute('writer-close', close, operation_id='other-close')
        with self.transaction(read_only=True) as db, self.assertRaises(RegistryError):
            self.writer._session(db, payload, closing=True, completed_close=('close-a','wrong-fingerprint'))
        self.assertEqual('CLOSED', self.db.execute('SELECT status FROM writer_sessions').fetchone()[0])

    def test_database_close_readback_after_stopping_has_no_new_effect(self):
        self.start(); payload = self.writer_session(); self.stop()
        close = dict(payload, drained=True, disconnected=True)
        fingerprint = runtime_module.digest(runtime_module.canonical({'command':'writer-close','payload':close}))
        accepted = self.writer._database_accept(payload, 'close-a', fingerprint, 'writer-close')
        self.writer._database_record(payload, {'status':'CLOSED','operationId':'close-a','requestHash':fingerprint},
            phase='close', exit_proof={'knownExited':True})
        self.writer._release_call(accepted, payload, 'close-a', fingerprint)
        self.writer.process_adapter.identity.return_value = {}
        path = Mock(); path.exists.return_value = True
        self.writer._database_path = Mock(return_value=path)
        with patch('dev_server.writer_control.plain_path'):
            before = self.db.total_changes
            replay = self.writer.execute('writer-close', close, operation_id='close-a')
            self.assertTrue(replay['closed']); self.assertTrue(replay['replayed'])
            self.assertEqual(before, self.db.total_changes)
            with self.assertRaises(RegistryError): self.writer.execute('writer-close', close, operation_id='other-close')
        with self.transaction(read_only=True) as db, self.assertRaises(RegistryError):
            self.writer._session(db, payload, closing=True, completed_close=('close-a','wrong-fingerprint'))
        self.assertEqual('CLOSED', self.db.execute('SELECT status FROM writer_sessions').fetchone()[0])

    def test_readback_during_startup_does_not_consume_frame(self):
        receive = self.receive; blocked = []
        def guarded(deadline):
            before = self.channel.receive_frame.call_count
            with self.assertRaises(RegistryError) as caught:
                self.runtime._managed_readback(self.token, deadline=deadline)
            self.assertEqual('CONTROL_BUSY', caught.exception.code)
            self.assertEqual(before, self.channel.receive_frame.call_count)
            blocked.append(True)
            return receive(deadline)
        self.channel.receive_frame.side_effect = guarded
        self.start()
        self.assertEqual(2, len(blocked))
        self.assertEqual(2, self.channel.receive_frame.call_count)
        self.assertFalse(self.runtime._control.sessions['slot-a']['active'])

    def test_public_execute_finishes_claim_then_stop_close_and_database_readback(self):
        self.start(); payload = self.writer_session()
        self.writer.process_adapter.identity.return_value = {}
        self.runtime._running = Mock()
        self.writer._preflight = Mock()
        def io(*args):
            self.assertEqual(0, self.depth)
            return {'written': True}
        self.writer._io = Mock(side_effect=io)
        result = self.writer.execute('writer-io', dict(payload, request={'action':'write','path':'config.yml','text':'fixture'}), operation_id='io-a')
        self.assertTrue(result['written'])
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM reservations WHERE reference LIKE 'writer-call:%'").fetchone()[0])
        self.stop()
        proxy = Mock(); proxy.request_bytes = None
        def begin(request, deadline):
            self.assertEqual(0, self.depth)
            return {'status':'CLOSED','operationId':request['operationId'],'requestHash':request['requestHash']}
        proxy.begin.side_effect = begin
        proxy.finish_exit.return_value = {'knownExited': True}
        self.writer.database_proxies['session-a'] = proxy
        path = Mock(); path.exists.return_value = True
        self.writer._database_path = Mock(return_value=path)
        close = dict(payload, drained=True, disconnected=True)
        with patch('dev_server.writer_control.plain_path'):
            result = self.writer.execute('writer-close', close, operation_id='close-a')
            self.assertTrue(result['closed'])
            self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM reservations WHERE reference LIKE 'writer-call:%'").fetchone()[0])
            before = self.db.total_changes
            result = self.writer.execute('writer-close', close, operation_id='close-a')
            self.assertTrue(result['closed']); self.assertTrue(result['replayed'])
            self.assertEqual(before, self.db.total_changes)
            with self.assertRaises(RegistryError): self.writer.execute('writer-close', close, operation_id='other-close')
        proxy.begin.assert_called_once()
        proxy.finish_exit.assert_called_once()
        self.assertEqual('BLOCKED', self.gate()['gate'])

    def test_unknown_does_not_change_rebound_nonce(self):
        error = OSError('nonce changed'); changes = []
        def accept(*args):
            record = dict(self.run, nonce='replacement-nonce')
            self.db.execute("UPDATE runs SET record=?", (json.dumps(record),))
            self.db.execute("UPDATE run_admission SET nonce='replacement-nonce'"); self.db.commit()
            changes.append(self.db.total_changes)
            raise error
        self.endpoint.accept.side_effect = accept
        with self.assertRaises(OSError) as caught: self.start()
        self.assertIs(error, caught.exception)
        self.assertEqual(changes[0], self.db.total_changes)
        self.assertEqual('PENDING', self.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])


    def pending_database(self, *, bootstrap=False, held=False):
        self.start(); payload = self.writer_session()
        self.writer.process_adapter.identity.return_value = {}
        self.runtime._running = Mock()
        self.runtime._active_generation = Mock(return_value=Path('fixture-generation'))
        proxy = Mock(); proxy.started = proxy.bound = True; proxy.exited = False
        proxy.request_bytes = proxy.response_bytes = None
        def begin(request, deadline):
            self.assertEqual(0, self.depth)
            proxy.request_bytes = runtime_module.canonical(request)
            status = 'CLOSED' if request['command'] == 'close' else 'RESULT' if request['command'] == 'resolve' else 'PENDING'
            response = {key:request[key] for key in ('operationId','requestHash')} | {'status':status}
            proxy.response_bytes = runtime_module.canonical(response)
            return response
        proxy.begin.side_effect = begin
        proxy.receive.side_effect = lambda deadline: json.loads(proxy.response_bytes)
        def ack(response_hash):
            proxy.request_bytes = proxy.response_bytes = None
        proxy.acknowledge.side_effect = ack
        proxy.finish_exit.return_value = {'knownExited':True}
        self.writer.database_proxies['session-a'] = proxy
        path = Mock(); path.exists.return_value = True
        self.writer._database_path = Mock(return_value=path)
        request = dict(payload, request={'action':'database','message':{'type':'database_history_start'},'timeoutSeconds':2})
        self.assertFalse(self.writer.execute('writer-io', request, operation_id='io-a')['completed'])
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM reservations WHERE reference LIKE 'writer-call:%'").fetchone()[0])
        fingerprint = self.db.execute("SELECT fingerprint FROM writer_operations WHERE id='io-a'").fetchone()[0]
        if bootstrap:
            response = {'status':'OPEN','operationId':'io-a','requestHash':fingerprint}
            metadata = {'phase':'bootstrap','proxyStatus':'OPEN','completed':False,
                        'proxyResponseSha256':runtime_module.digest(runtime_module.canonical(response))}
            self.db.execute("UPDATE writer_operations SET response=? WHERE id='io-a'", (json.dumps(metadata),)); self.db.commit()
            proxy.request_bytes = json.dumps({'command':'open','operationId':'io-a','requestHash':fingerprint}).encode() if held else None
            proxy.response_bytes = runtime_module.canonical(response) if held else None
        return payload, proxy, fingerprint

    def test_close_drains_pending_io_after_public_finally_and_block(self):
        payload, proxy, fingerprint = self.pending_database(); self.stop()
        with patch('dev_server.writer_control.plain_path'):
            result = self.writer.execute('writer-close', dict(payload,drained=True,disconnected=True), operation_id='close-a')
        self.assertTrue(result['closed'])
        self.assertEqual('COMPLETED',self.db.execute("SELECT status FROM writer_operations WHERE id='io-a'").fetchone()[0])
        self.assertEqual(0,self.db.execute("SELECT COUNT(*) FROM reservations WHERE reference LIKE 'writer-call:%'").fetchone()[0])
        self.assertEqual('BLOCKED',self.gate()['gate'])
        self.assertEqual(['execute','resolve','close'],[call.args[0]['command'] for call in proxy.begin.call_args_list])
        with self.assertRaises(RegistryError):self.writer._database_accept(payload,'new-a','new-hash','writer-io',revision=self.gate()['revision'])

    def test_close_interrupts_unsent_bootstrap_after_block(self):
        payload, proxy, _ = self.pending_database(bootstrap=True); self.stop()
        with patch('dev_server.writer_control.plain_path'):
            result = self.writer.execute('writer-close',dict(payload,drained=True,disconnected=True),operation_id='close-a')
        self.assertTrue(result['closed'])
        value=json.loads(self.db.execute("SELECT response FROM writer_operations WHERE id='io-a'").fetchone()[0])
        self.assertEqual('DB_BOOTSTRAP_INTERRUPTED',value['code']);self.assertEqual('BLOCKED',self.gate()['gate'])

    def test_close_interrupts_held_open_and_completed_readback_has_no_ack(self):
        payload, proxy, fingerprint = self.pending_database(bootstrap=True,held=True); self.stop()
        self.writer._database_accept(payload,'close-a','close-hash','writer-close')
        value=self.writer._database_interrupt_bootstrap(payload,proxy,'io-a',fingerprint,close_call=('close-a','close-hash'))
        before=self.db.total_changes;acks=proxy.acknowledge.call_count
        self.assertEqual(value,self.writer._database_interrupt_bootstrap(payload,proxy,'io-a',fingerprint,close_call=('close-a','close-hash')))
        self.assertEqual(before,self.db.total_changes);self.assertEqual(acks,proxy.acknowledge.call_count)

    def test_drain_rejects_wrong_owner_request_binding_and_missing_claim(self):
        payload,proxy,fingerprint=self.pending_database(); self.stop()
        self.writer._database_accept(payload,'close-a','close-hash','writer-close')
        response={'status':'RESULT','operationId':'io-a','requestHash':fingerprint}
        request=json.loads(proxy.request_bytes)
        for owner,body,binding in ((('other','close-hash'),request,payload),(('close-a','wrong'),request,payload),
                (('close-a','close-hash'),dict(request,requestHash='wrong'),payload),
                (('close-a','close-hash'),request,dict(payload,runId='old'))):
            before=self.db.total_changes
            with self.assertRaises(RegistryError):self.writer._database_record(binding,response,phase='execute',close_call=owner,request=body)
            self.assertEqual(before,self.db.total_changes)
        self.db.execute("DELETE FROM reservations WHERE operation='close-a'");self.db.commit()
        with self.assertRaises(RegistryError):self.writer._database_record(payload,response,phase='execute',close_call=('close-a','close-hash'),request=request)
        self.assertEqual('PENDING',self.db.execute("SELECT status FROM writer_operations WHERE id='io-a'").fetchone()[0])

    def test_bootstrap_requires_active_owner_pending_command_and_pending_session(self):
        payload,proxy,fingerprint=self.pending_database(bootstrap=True)
        with self.assertRaises(RegistryError):self.writer._database_interrupt_bootstrap(payload,proxy,'io-a',fingerprint)
        proxy.bound=False
        with self.assertRaises(RegistryError):self.writer._database_interrupt_bootstrap(payload,proxy,'io-a',fingerprint)
        proxy.bound=True
        self.stop();self.writer._database_accept(payload,'close-a','close-hash','writer-close')
        for owner in (('other','close-hash'),('close-a','wrong')):
            before=self.db.total_changes
            with self.assertRaises(RegistryError):self.writer._database_interrupt_bootstrap(payload,proxy,'io-a',fingerprint,close_call=owner)
            self.assertEqual(before,self.db.total_changes)
        proxy.request_bytes=runtime_module.canonical({'command':'open','operationId':'io-a','requestHash':fingerprint})
        proxy.response_bytes=runtime_module.canonical({'status':'OPEN','operationId':'io-a','requestHash':'wrong'})
        with self.assertRaises(RegistryError):self.writer._database_interrupt_bootstrap(payload,proxy,'io-a',fingerprint,close_call=('close-a','close-hash'))
        proxy.response_bytes=runtime_module.canonical({'status':'OPEN','operationId':'io-a','requestHash':fingerprint,'extra':'not-recorded'})
        before=self.db.total_changes
        with self.assertRaises(RegistryError):self.writer._database_interrupt_bootstrap(payload,proxy,'io-a',fingerprint,close_call=('close-a','close-hash'))
        self.assertEqual(before,self.db.total_changes)
        proxy.request_bytes=proxy.response_bytes=None
        for sql,undo in (("UPDATE writer_operations SET command='writer-close' WHERE id='io-a'","UPDATE writer_operations SET command='writer-io' WHERE id='io-a'"),
                         ("UPDATE writer_sessions SET pending=0","UPDATE writer_sessions SET pending=1")):
            self.db.execute(sql);self.db.commit()
            with self.assertRaises(RegistryError):self.writer._database_interrupt_bootstrap(payload,proxy,'io-a',fingerprint,close_call=('close-a','close-hash'))
            self.db.execute(undo);self.db.commit()
        self.db.execute("DELETE FROM reservations WHERE operation='close-a'");self.db.commit()
        with self.assertRaises(RegistryError):self.writer._database_interrupt_bootstrap(payload,proxy,'io-a',fingerprint,close_call=('close-a','close-hash'))
        self.assertEqual(0,proxy.acknowledge.call_count)

    def test_unknown_drain_stays_pending_and_completed_io_is_not_downgraded(self):
        payload,proxy,fingerprint=self.pending_database();self.stop()
        self.writer._database_accept(payload,'close-a','close-hash','writer-close')
        request=json.loads(proxy.request_bytes)
        response={'status':'UNKNOWN','operationId':'io-a','requestHash':fingerprint}
        result=self.writer._database_record(payload,response,phase='execute',close_call=('close-a','close-hash'),request=request)
        self.assertFalse(result['completed'])
        self.assertEqual('PENDING',self.db.execute("SELECT status FROM writer_operations WHERE id='io-a'").fetchone()[0])
        response['status']='RESULT'
        result=self.writer._database_record(payload,response,phase='execute',close_call=('close-a','close-hash'),request=request)
        before=self.db.total_changes
        self.assertEqual(result,self.writer._database_record(payload,response,phase='execute'))
        self.assertEqual(before,self.db.total_changes)
        with self.assertRaises(RegistryError):self.writer._database_record(payload,dict(response,status='UNKNOWN'),phase='execute')
        self.assertEqual('COMPLETED',self.db.execute("SELECT status FROM writer_operations WHERE id='io-a'").fetchone()[0])

if __name__ == '__main__':
    unittest.main()
