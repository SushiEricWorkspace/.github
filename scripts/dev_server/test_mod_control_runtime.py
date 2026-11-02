"""通常controlの予約・停止順・原応答保存と専用進行を合成peerで検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from . import mod_control_operations as operations
from . import mod_control_wire as wire
from . import storage
from .mod_control_runtime import ManagedRuntime, approved_input
from .local_ipc import make_private
from .mod_control_startup import _Startup, _change
from .registry import RegistryError
from . import test_mod_control_startup as startup_tests
from . import test_runtime_control as runtime_tests
from .build_inputs import canonical
from .build_lock import BuildLockError, build_lock
from .test_mod_control_wire_v3 import VECTORS, frame


class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = startup_tests.StartupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.channel.wait_peer_close.return_value = True
        db = sqlite3.connect(':memory:', check_same_thread=False)
        db.row_factory = sqlite3.Row
        f.db.backup(db)
        self.addCleanup(db.close)
        f.db = db
        for i, (request, response) in enumerate(f.frames):
            request, response = wire.decode_frame_v2(request), wire.decode_frame_v2(response)
            request['version'] = response['version'] = 3
            response['payload']['requestBodySha256'] = wire.sha256(frame(request)[4:])
            f.frames[i] = frame(request), frame(response)
        f.received = iter([pair[0] for pair in f.frames])
        self.managed = ManagedRuntime(f.runtime, {}, transport=f.transport)
        self.session = dict(run=f.run['runId'], raw=f.context['descriptorJson'].encode(), token=f.token,
                            context=f.context, endpoint=f.endpoint, stop=threading.Event(), done=threading.Event(),
                            error=None, close_ids={}, drain_lock=threading.Lock())
        self.managed.sessions['slot-a'] = self.session
        f.runtime.managed = self.managed

    def handshake(self):
        f = self.fixture
        self.managed.startup.start(f.token, **f.inputs, endpoint=f.endpoint, defer_ready=True)

    def running(self):
        self.handshake()
        f = self.fixture
        with f.transaction() as connection:
            row = f.registry._instance(connection, 'slot-a')
            self.managed.ready(row, connection)
            _change(connection, row, f.gate()['revision'], 'OPEN')
            connection.execute("UPDATE instances SET state='RUNNING',pending=0 WHERE id='slot-a'")
            connection.execute("UPDATE runtime_operations SET status='COMPLETED' WHERE id='startup-a'")
            connection.execute("DELETE FROM reservations WHERE operation='startup-a'")
        return f

    def stop_frames(self):
        f = self.fixture
        result = []
        for pair in VECTORS['sessions'][0]['pairs'][2:4]:
            value = wire.decode_frame_v3(bytes.fromhex(pair['request']['frameHex']))
            value['descriptorSha256'] = wire.sha256(self.session['raw'])
            value['payload']['management'] = f.context['management']
            if value['phase'] == 'SAVE_REQUESTED':
                requested = json.loads(value['payload']['requestJson'])
                state = requested['status']['state']
                bound = wire._inner(wire.decode_frame_v3(f.frames[1][0])['payload'], 'statusJson', 'statusJsonSha256')['state']
                for key in ('worldUuid', 'runUuid', 'generation', 'paths'):
                    state[key] = deepcopy(bound[key])
                state['stateRevision'] = bound['stateRevision'] + 1
                state['payload'].update(owner=f.context['owner'], management=f.context['management'])
                raw = json.dumps(requested, separators=(',', ':'))
                value['payload'].update(requestJson=raw, requestSha256=wire.sha256(raw.encode()))
            result.append(frame(value))
        return result

    def test_ack_does_not_open_or_complete_start_before_ready(self):
        self.handshake()
        f = self.fixture
        self.assertEqual(('STARTING', 1), (f.row()['state'], f.row()['pending']))
        self.assertEqual('CLOSED', f.gate()['gate'])
        self.assertEqual('PENDING', f.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])
        self.assertEqual(1, f.db.execute("SELECT count(*) FROM reservations WHERE operation='startup-a'").fetchone()[0])
        with f.transaction() as connection:
            operations.unknown(connection, 'slot-a', f.frames[1][0])
        with f.transaction(read_only=True) as connection:
            self.assertEqual(f.frames[1][1], operations.readback(connection, 'slot-a', f.frames[1][0]))

    def test_handshake_rejects_pid_reuse_before_grant(self):
        f = self.fixture
        self.session['descriptor'] = wire.decode_descriptor(self.session['raw'])
        f.run['identity'] = {'pid': f.context['owner']['pid'], 'startId': 'original-start'}
        with patch('dev_server.mod_control_runtime.process_identity', return_value=f.context['owner']), \
                patch.object(self.managed.startup, 'start') as start:
            with self.assertRaises(RegistryError) as error:
                self.managed.handshake(f.token, f.run, self.session, 10)
            self.assertEqual('PROCESS_MISMATCH', error.exception.code)
            start.assert_not_called()

    def test_same_run_stop_merges_without_reopening_or_new_revision(self):
        f = self.running()
        f.stop()
        revision = f.gate()['revision']
        self.managed.reserve_stop('slot-a', self.session)
        self.assertEqual(revision, f.gate()['revision'])
        self.assertEqual('BLOCKED', f.gate()['gate'])


        self.session['run'] = 'different-run'
        with self.assertRaises(RegistryError):
            self.managed.reserve_stop('slot-a', self.session)

    def test_shared_worker_starvation_still_persists_stop_and_requested(self):
        f = self.running()
        requests = iter(self.stop_frames())
        trace = []
        f.channel.receive_frame.side_effect = lambda *args, **kwargs: next(requests)

        def send(raw, deadline):
            message = wire.decode_frame_v3(raw)
            request = self.stop_frames()[0 if message['phase'] == 'BLOCKED' else 1]
            with f.transaction(read_only=True) as connection:
                self.assertEqual(raw, operations.readback(connection, 'slot-a', request))
            trace.append(message['phase'])
            return len(raw)

        f.channel.send_frame.side_effect = send
        f.endpoint.close.return_value = True
        f.runtime._direct_stop = Mock()
        release = threading.Event()
        entered = [threading.Event() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as workers:
            for event in entered:
                workers.submit(lambda event=event: (event.set(), release.wait(5)))
            for event in entered:
                self.assertTrue(event.wait(2))
            try:
                self.managed.listen('slot-a')
                self.assertTrue(self.session['done'].wait(2))
                self.session['thread'].join(2)
                self.assertIsNone(self.session['error'])
                self.assertEqual(['BLOCKED', 'COMPLETION'], trace)
                self.assertEqual('BLOCKED', f.gate()['gate'])
                f.runtime._direct_stop.assert_called_once()
            finally:
                release.set()

    def test_unknown_writer_prevents_blocked_and_does_not_reopen_gate(self):
        f = self.running()
        f.writer_session(pending=1, status='UNKNOWN')
        f.channel.receive_frame.side_effect = lambda *args, **kwargs: self.stop_frames()[0]
        f.channel.send_frame.reset_mock()
        f.channel.send_frame.side_effect = lambda raw, deadline: len(raw)
        self.managed._serve('slot-a', self.session)
        self.assertIsInstance(self.session['error'], RegistryError)
        self.assertFalse(self.session['done'].is_set())
        f.channel.send_frame.assert_not_called()
        self.assertEqual('BLOCKED', f.gate()['gate'])
        self.assertEqual('UNKNOWN', f.db.execute('SELECT status FROM writer_sessions').fetchone()[0])

    def test_drain_wait_does_not_prevent_stop_receipt(self):
        f = self.running()
        received = threading.Event()
        release = threading.Event()
        original = self.managed.drain
        self.session['drain_lock'].acquire()
        requests = iter(self.stop_frames())

        def receive(*args, **kwargs):
            value = next(requests)
            received.set()
            return value

        f.channel.receive_frame.side_effect = receive
        f.channel.send_frame.side_effect = lambda raw, deadline: len(raw)
        f.endpoint.close.return_value = True
        f.runtime._direct_stop = Mock()
        self.managed.listen('slot-a')
        try:
            self.assertTrue(received.wait(2))
            self.assertTrue(self.session['stop'].wait(2))
            self.assertFalse(self.session['done'].is_set())
        finally:
            self.session['drain_lock'].release()
            self.session['thread'].join(3)
        self.assertTrue(self.session['done'].is_set())

    def test_received_stop_drains_original_rejected_result_and_actual_exit_before_blocked(self):
        f = self.fixture
        f.start = self.running
        payload, proxy, fingerprint = f.pending_database()
        original = json.loads(proxy.request_bytes)
        response = {key: original[key] for key in ('operationId', 'requestHash')} | {'status': 'REJECTED'}
        proxy.response_bytes = json.dumps(response, separators=(',', ':')).encode()
        trace = []
        actor = dict(f.context['owner'], pid=300, startId='proxy-start')

        def exited(deadline):
            self.assertEqual('COMPLETED', f.db.execute("SELECT status FROM writer_operations WHERE id='io-a'").fetchone()[0])
            self.assertIn('REJECTED', f.db.execute("SELECT response FROM writer_operations WHERE id='io-a'").fetchone()[0])
            trace.append('external-writers-closed')
            return dict(proxyExited=True, proxyChildrenExited=True, proxyIdentity=actor)

        proxy.finish_exit.side_effect = exited
        requests = iter(self.stop_frames())

        def receive(*args, **kwargs):
            request = next(requests)
            trace.append(wire.decode_frame_v3(request)['phase'])
            return request

        def send(raw, deadline):
            message = wire.decode_frame_v3(raw)
            self.assertEqual('CLOSED', f.db.execute('SELECT status FROM writer_sessions').fetchone()[0])
            if message['phase'] == 'COMPLETION':
                self.assertEqual([{'identity': actor, 'state': 'CLOSED', 'children': []}], message['payload']['completion']['writers'])
                trace.append('latest-completion')
            else:
                trace.append(message['phase'])
            return len(raw)

        f.channel.receive_frame.side_effect = receive
        f.channel.send_frame.side_effect = send
        f.endpoint.close.side_effect = lambda deadline: (trace.append('control-close') or True)
        f.runtime._direct_stop = Mock()
        with patch('dev_server.writer_control.plain_path'):
            self.managed._serve('slot-a', self.session)
        self.assertIsNone(self.session['error'])
        expected = json.loads((Path(__file__).parent / 'management-stop-trace.json').read_bytes())['normal']
        self.assertEqual([item for item in expected if item in trace], trace)
        self.assertTrue(self.session['done'].is_set())

    def test_child_exit_unknown_keeps_closed_result_pending_and_sends_no_blocked(self):
        f = self.fixture
        f.start = self.running
        payload, proxy, fingerprint = f.pending_database()
        proxy.finish_exit.side_effect = TimeoutError('実child終了不明')
        f.channel.receive_frame.side_effect = lambda *args, **kwargs: self.stop_frames()[0]
        f.channel.send_frame.reset_mock()
        with patch('dev_server.writer_control.plain_path'):
            self.managed._serve('slot-a', self.session)
        self.assertIsInstance(self.session['error'], TimeoutError)
        self.assertFalse(self.session['done'].is_set())
        f.channel.send_frame.assert_not_called()
        self.assertNotEqual('CLOSED', f.db.execute('SELECT status FROM writer_sessions').fetchone()[0])
        self.assertEqual('BLOCKED', f.gate()['gate'])

    def test_completion_delivery_close_timeout_keeps_original_reply_and_run_reserved(self):
        f = self.running()
        requests = iter(self.stop_frames())
        f.channel.receive_frame.side_effect = lambda *args, **kwargs: next(requests)
        f.channel.send_frame.side_effect = lambda raw, deadline: len(raw)
        f.channel.wait_peer_close.side_effect = TimeoutError('元MのEOF不明')
        f.runtime._direct_stop = Mock()
        self.managed._serve('slot-a', self.session)
        self.assertIsInstance(self.session['error'], TimeoutError)
        self.assertFalse(self.session['done'].is_set())
        f.endpoint.close.assert_not_called()
        f.runtime._direct_stop.assert_not_called()
        self.assertEqual('RECOVERY_REQUIRED', f.row()['state'])
        self.assertEqual('BLOCKED', f.gate()['gate'])
        with f.transaction(read_only=True) as connection:
            saved = operations.readback(connection, 'slot-a', self.stop_frames()[1])
        self.assertEqual('COMPLETION', wire.decode_frame_v3(saved)['phase'])

    def test_requested_timeout_after_blocked_keeps_stop_reserved(self):
        f = self.running()
        f.channel.receive_frame.side_effect = [self.stop_frames()[0], TimeoutError('Requested未到達')]
        sent = []
        f.channel.send_frame.side_effect = lambda raw, deadline: (sent.append(wire.decode_frame_v3(raw)['phase']) or len(raw))
        self.managed._serve('slot-a', self.session)
        self.assertEqual(['BLOCKED'], sent)
        self.assertNotIn('idle', f.channel.receive_frame.call_args.kwargs)
        self.assertIsInstance(self.session['error'], TimeoutError)
        self.assertFalse(self.session['done'].is_set())
        self.assertEqual('BLOCKED', f.gate()['gate'])
        with f.transaction(read_only=True) as connection:
            saved = operations.readback(connection, 'slot-a', self.stop_frames()[0])
        self.assertEqual('BLOCKED', wire.decode_frame_v3(saved)['phase'])

    def test_ready_failure_preserves_ack_and_readback_cannot_reopen(self):
        self.handshake()
        f = self.fixture
        self.managed.startup._unknown_start(self.managed.startup.sessions['slot-a'])
        requests = iter([f.frames[1][0], self.stop_frames()[0]])
        f.channel.receive_frame.side_effect = lambda *args, **kwargs: next(requests)
        sent = []
        f.channel.send_frame.side_effect = lambda raw, deadline: (sent.append(raw) or len(raw))
        self.managed._serve('slot-a', self.session)
        self.assertEqual([f.frames[1][1]], sent)
        self.assertEqual('BLOCKED', f.gate()['gate'])
        self.assertEqual('RECOVERY_REQUIRED', f.row()['state'])
        self.assertEqual('RECOVERY_REQUIRED', f.db.execute("SELECT status FROM runtime_operations WHERE id='startup-a'").fetchone()[0])
        with f.transaction(read_only=True) as connection:
            self.assertEqual(f.frames[1][1], operations.readback(connection, 'slot-a', f.frames[1][0]))

    def test_direct_observer_after_public_completion_does_not_complete_twice(self):
        f = self.running()
        f.db.execute("UPDATE instances SET state='LEASED_STOPPED',pending=0,runtime_proof='{}'")
        f.db.commit()
        before = f.db.total_changes
        f.runtime._direct_stop('slot-a', self.session)
        self.assertEqual(before, f.db.total_changes)
        self.assertEqual('LEASED_STOPPED', f.row()['state'])

    def test_public_stop_joins_direct_reservation_without_second_exit_or_gate_change(self):
        f = self.running()
        requests = iter(self.stop_frames())
        f.channel.receive_frame.side_effect = lambda *args, **kwargs: next(requests)
        f.channel.send_frame.side_effect = lambda raw, deadline: len(raw)
        f.endpoint.close.return_value = True
        f.runtime._direct_stop = Mock()
        self.managed._serve('slot-a', self.session)
        parent = self.session['stop_operation']
        self.assertTrue(self.session['direct_stop'])
        revision = f.gate()['revision']
        expected = {'runId': self.session['run'], 'runtimeStopped': True, 'stopOperationId': parent,
                    'controlOperationId': self.session['control_operation']}

        def finished(_):
            with f.transaction() as connection:
                connection.execute("UPDATE instances SET state='LEASED_STOPPED',pending=0")
                connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(expected), parent))

        with patch('dev_server.runtime_control.time.sleep', side_effect=finished):
            result = f.runtime._join_direct_stop(dict(f.row()), dict(f.token, timeoutSeconds=2), 'public-stop', 'a' * 64, self.session)
        self.assertEqual(expected, result)
        self.assertEqual(revision, f.gate()['revision'])
        saved = f.db.execute("SELECT status,response FROM runtime_operations WHERE id='public-stop'").fetchone()
        self.assertEqual(('COMPLETED', expected), (saved[0], json.loads(saved[1])))
        self.assertIsNone(f.db.execute("SELECT 1 FROM reservations WHERE operation='public-stop'").fetchone())

    def test_control_stop_keeps_public_parent_and_does_not_finalize_it(self):
        f = self.running()
        f.stop()
        requests = iter(self.stop_frames())
        f.channel.receive_frame.side_effect = lambda *args, **kwargs: next(requests)
        f.channel.send_frame.side_effect = lambda raw, deadline: len(raw)
        f.endpoint.close.return_value = True
        with patch.object(f.runtime, '_stop') as stop:
            self.managed._serve('slot-a', self.session)
            stop.assert_not_called()
        self.assertIsNone(self.session['error'])
        self.assertEqual('stop-a', self.session['stop_operation'])
        self.assertFalse(self.session['direct_stop'])
        parent = f.db.execute("SELECT reference FROM reservations WHERE operation=?", (self.session['control_operation'],)).fetchone()
        self.assertEqual('stop:stop-a', parent[0])
        self.assertEqual('PENDING', f.db.execute("SELECT status FROM runtime_operations WHERE id='stop-a'").fetchone()[0])

    def test_requested_different_run_never_gets_completion(self):
        f = self.running()
        stop, requested = self.stop_frames()
        value = wire.decode_frame_v3(requested)
        inner = json.loads(value['payload']['requestJson'])
        inner['status']['state']['runUuid'] = '55555555-5555-4555-8555-555555555555'
        raw = json.dumps(inner, separators=(',', ':'))
        value['payload'].update(requestJson=raw, requestSha256=wire.sha256(raw.encode()))
        requests = iter([stop, frame(value)])
        f.channel.receive_frame.side_effect = lambda *args, **kwargs: next(requests)
        sent = []
        f.channel.send_frame.side_effect = lambda raw, deadline: (sent.append(wire.decode_frame_v3(raw)['phase']) or len(raw))
        self.managed._serve('slot-a', self.session)
        self.assertEqual(['BLOCKED'], sent)
        self.assertIsInstance(self.session['error'], RegistryError)
        self.assertFalse(self.session['done'].is_set())

    def test_direct_reservation_between_public_preflight_and_registration_joins(self):
        f = self.running()
        f.runtime.refresh = Mock()
        f.runtime.processes[self.session['run']] = Mock()

        def direct_stop(row):
            self.managed.reserve_stop('slot-a', self.session)
            f.db.execute("INSERT INTO runtime_operations VALUES ('direct-parent','hash','stop','slot-a','PENDING','{}')")
            f.db.commit()
            self.session.update(direct_stop=True, stop_operation='direct-parent')
            return Path('synthetic-generation')

        f.runtime._active = Mock(side_effect=direct_stop)
        with patch('dev_server.runtime_control.instance_endpoints', return_value={}), \
                patch.object(f.runtime, '_join_direct_stop', return_value={'joined': True}) as join:
            result = f.runtime.execute('stop', dict(f.token, timeoutSeconds=2), operation_id='public-race')
        self.assertEqual({'joined': True}, result)
        join.assert_called_once()
        self.assertIsNone(f.db.execute("SELECT 1 FROM runtime_operations WHERE id='public-race'").fetchone())
        self.assertEqual('BLOCKED', f.gate()['gate'])


@unittest.skipUnless(sys.platform in ('win32', 'darwin'), '通常世代のprivate rootを使用する')
class ManagedRuntimeIntegrationTests(unittest.TestCase):
    """実prepare/launch/startupとSQLiteを通し、OS本人とMだけを合成する。"""
    def setUp(self):
        self.f = runtime_tests.RuntimeControlTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.generation = self.f.prepare()
        self.runtime = self.f.registry.runtime
        evidence = deepcopy(VECTORS['contexts']['Windows']['expectedEvidence'])
        profile = (self.generation / 'runtime/launch/managed-server-candidate.json').read_bytes()
        evidence['profileSha256'] = wire.sha256(profile)
        approval = dict(instanceId='persistent-01', artifactId=self.f.artifact,
                        runtimeManifestHash=wire.sha256((self.generation / 'runtime/runtime.json').read_bytes()),
                        expectedEvidence=evidence)
        self.channel = Mock()
        self.endpoint = Mock()
        self.endpoint.close.return_value = True
        self.transport = Mock()
        self.transport.native.transport = 'windows-pipe'
        self.transport.create.side_effect = self.create
        self.endpoint.accept.side_effect = self.accept
        self.channel.receive_frame.side_effect = self.receive
        self.channel.send_frame.side_effect = self.send
        self.channel.wait_peer_close.side_effect = self.exit
        self.managed = ManagedRuntime(self.runtime, {'persistent-01': approval}, transport=self.transport)
        self.runtime.managed = self.managed
        self.stop_ready = threading.Event()
        self.save_ready = threading.Event()
        self.blocked = threading.Event()
        self.trace = []
        self.requests = []
        self.index = 0
        self.identity_patch = patch('dev_server.mod_control_runtime.process_identity', side_effect=self.identity)
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.addCleanup(self.finish)

    def finish(self):
        self.stop_ready.set()
        self.save_ready.set()
        session = self.managed.sessions.get('persistent-01')
        if session and 'thread' in session:
            session['thread'].join(5)

    def identity(self, native, adapter, pid):
        return dict(hostHash='1' * 64, bootId='native-boot', pid=pid,
                    startId='controller-start' if pid == os.getpid() else str(pid))

    def create(self, context, raw):
        self.descriptor = wire.decode_descriptor(raw)
        path = Path(wire.descriptor_path(self.descriptor))
        path.parent.mkdir(parents=True)
        make_private(path.parent, directory=True)
        path.write_bytes(raw)
        make_private(path)
        return self.endpoint

    def accept(self, owner, deadline):
        session = self.managed.sessions['persistent-01']
        d = self.descriptor
        management = dict(leaseId=d['leaseId'], epoch=d['epoch'], generation=d['generationId'],
                          controller=d['controller'], drainSha256=wire.sha256(canonical({'run': d['runtimeRunId'], 'writers': []})))
        for pair in VECTORS['sessions'][0]['pairs']:
            value = wire.decode_frame_v3(bytes.fromhex(pair['request']['frameHex']))
            value['descriptorSha256'] = wire.sha256(session['raw'])
            payload = value['payload']
            if 'owner' in payload:
                payload['owner'] = owner
            if 'management' in payload:
                payload['management'] = management
            if 'profileEvidenceJson' in payload:
                evidence = json.loads(payload['profileEvidenceJson'])
                evidence.update(session['approval']['expectedEvidence'], owner=owner,
                                descriptorSha256=value['descriptorSha256'], runtimeManifestHash=d['runtimeManifestHash'],
                                runtimeRunId=d['runtimeRunId'], startNonce=d['startNonce'])
                raw = canonical(evidence)
                payload.update(profileEvidenceJson=raw.decode(), profileEvidenceSha256=wire.sha256(raw))
            for key, hash_key in [('statusJson', 'statusJsonSha256'), ('requestJson', 'requestSha256')]:
                if key not in payload:
                    continue
                inner = json.loads(payload[key])
                state = (inner if key == 'statusJson' else inner['status'])['state']
                state.update(generation=d['generationId'])
                # Data deb13317: BASE_ROOT=config/${MOD_NAME}、Shop=shop_data。
                state['paths'].update(worldRoot=str(self.generation / 'world'),
                    shopDirectory=str(self.generation / 'config' / 'SushiEricServerMod' / 'shop_data'))
                state['payload'].update(owner=owner, management=management)
                raw = canonical(inner)
                payload.update({key: raw.decode(), hash_key: wire.sha256(raw)})
            self.requests.append(frame(value))
        return self.channel

    def receive(self, deadline, **kwargs):
        index = self.index
        self.index += 1
        if index >= 2:
            event = self.stop_ready if index == 2 else self.save_ready
            if not event.wait(5):
                raise TimeoutError('試験peerの解放待機期限')
            if getattr(self, 'receive_error', None) is not None:
                raise self.receive_error
        return self.requests[index]

    def send(self, raw, deadline):
        message = wire.decode_frame_v3(raw)
        phase = message['phase']
        request = self.requests[{'GRANT': 0, 'ACK': 1, 'BLOCKED': 2, 'COMPLETION': 3}[phase]]
        with self.runtime._lock_wait(min(deadline, time.monotonic() + 1)), \
                self.runtime._transaction('persistent-01', read_only=True) as connection:
            self.assertEqual(raw, operations.readback(connection, 'persistent-01', request))
        self.trace.append(phase)
        if phase == 'BLOCKED':
            self.blocked.set()
        return len(raw)

    def exit(self, deadline):
        self.f.adapter.created[0].stopped_proof()
        self.f.adapter.created[0].exit_code = 0
        return True

    def start(self):
        self.f.start()
        self.session = self.managed.sessions['persistent-01']
        self.assertEqual(['GRANT', 'ACK'], self.trace)
        return self.session

    def test_real_prepare_descriptor_matches_data_paths_for_existing_generation(self):
        self.start()
        self.assertTrue(self.generation.is_dir())
        expected = self.generation / 'config' / 'SushiEricServerMod' / 'shop_data'
        self.assertEqual(str(expected), self.descriptor['shopDirectory'])
        bound = wire._inner(wire.decode_frame_v3(self.requests[1])['payload'], 'statusJson', 'statusJsonSha256')
        self.assertEqual(str(expected), bound['state']['paths']['shopDirectory'])

    def test_fresh_prepare_creates_shop_before_process_launch(self):
        shop = self.generation / 'config/SushiEricServerMod/shop_data'
        self.assertFalse(shop.exists())
        original = runtime_tests.FakeProcess.start
        launches = []

        def launch(process, *args, **kwargs):
            self.assertTrue(shop.is_dir())
            self.assertEqual(shop, Path(self.descriptor['shopDirectory']))
            self.assertEqual(shop, shop.resolve(strict=True))
            launches.append(shop)
            return original(process, *args, **kwargs)

        with patch.object(runtime_tests.FakeProcess, 'start', launch):
            self.start()
        self.assertEqual([shop], launches)
        self.assertEqual([], list(shop.iterdir()))

    def test_managed_prepare_preserves_existing_shop(self):
        shop = self.generation / 'config/SushiEricServerMod/shop_data'
        shop.mkdir()
        make_private(shop, directory=True)
        saved = shop / 'existing-shop.json'
        saved.write_bytes(b'{"stock":17}\n')
        make_private(saved)
        before = [(p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_size) for p in (shop, saved)]
        with patch('dev_server.mod_control_runtime.storage.mkdir', wraps=storage.mkdir) as mkdir:
            self.start()
            mkdir.assert_not_called()
        after = [(p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_size) for p in (shop, saved)]
        self.assertEqual(before, after)
        self.assertEqual(b'{"stock":17}\n', saved.read_bytes())
        self.assertEqual([saved], list(shop.iterdir()))
        self.assertEqual(shop, Path(self.descriptor['shopDirectory']))

    def test_mod_strict_shop_real_path_before_bound(self):
        resolved = []

        def accept(owner, deadline):
            self.assertEqual([], self.trace)
            shop = Path(self.descriptor['shopDirectory']).resolve(strict=True)
            self.assertTrue(shop.is_dir())
            self.assertEqual(self.generation / 'config/SushiEricServerMod/shop_data', shop)
            resolved.append(shop)
            return self.accept(owner, deadline)

        self.endpoint.accept.side_effect = accept
        self.start()
        self.assertEqual([Path(self.descriptor['shopDirectory'])], resolved)

    def test_managed_prepare_rejects_shop_file_before_launch(self):
        shop = self.generation / 'config/SushiEricServerMod/shop_data'
        shop.write_bytes(b'keep')
        make_private(shop)
        with self.assertRaises(RegistryError) as error:
            self.f.start()
        self.assertEqual('ROOT_MISMATCH', error.exception.code)
        self.assertEqual([], self.f.adapter.created)
        self.transport.create.assert_not_called()
        self.assertEqual(b'keep', shop.read_bytes())

    def test_managed_prepare_rejects_shop_link_escape(self):
        shop = self.generation / 'config/SushiEricServerMod/shop_data'
        outside = self.f.root / 'outside-shop'
        outside.mkdir()
        if os.name == 'nt':
            result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(shop), str(outside)],
                                    capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.addCleanup(shop.rmdir)
        else:
            shop.symlink_to(outside, target_is_directory=True)
            self.addCleanup(shop.unlink)
        with self.assertRaises(OSError):
            self.managed.prepare(self.f.state(), {}, self.generation)
        self.assertEqual([], list(outside.iterdir()))
        self.assertEqual([], self.f.adapter.created)
        self.transport.create.assert_not_called()

    def stop_with_contention(self, *, observe=False):
        session = self.start()
        if observe:
            process = self.f.adapter.created[0]
            process.proof['phase'] = 'STOPPING'
            process.save_proof()
            for command, payload in [('status', {'instanceId': 'persistent-01'}), ('list', {}), ('doctor', {})]:
                self.f.command(command, payload, 'observe-' + command)
                self.assertEqual('STOPPING', self.f.state()['state'])
            with self.assertRaises(RegistryError):
                self.runtime._running(self.f.state())
            with self.f.registry.transaction(read_only=True) as connection:
                from .mod_control_startup import _require_open
                with self.assertRaises(RegistryError):
                    _require_open(connection, self.f.state())
        contended = threading.Event()

        @contextmanager
        def checked_lock(path):
            try:
                with build_lock(path):
                    yield
            except BuildLockError:
                contended.set()
                raise

        path = self.f.root / 'manager/locks/instance-persistent-01.lock'
        with patch('dev_server.supervisor.build_lock', checked_lock):
            with build_lock(path):
                self.stop_ready.set()
                self.assertTrue(contended.wait(2), '実instance lock競合を通ること')
            self.assertTrue(self.blocked.wait(3), repr(session['error']))
        self.assertIsNone(session['error'])
        joined = threading.Event()
        original = self.runtime._join_direct_stop

        def join(*args):
            joined.set()
            return original(*args)

        with patch.object(self.runtime, '_join_direct_stop', side_effect=join), ThreadPoolExecutor(max_workers=1) as workers:
            public = workers.submit(self.f.stop, 'public-stop')
            try:
                self.assertTrue(joined.wait(2))
            finally:
                self.save_ready.set()
            result = public.result(timeout=5)
        session['thread'].join(3)
        self.assertIsNone(session['error'])
        self.assertNotIn('preservationError', session)
        self.assertEqual(['GRANT', 'ACK', 'BLOCKED', 'COMPLETION'], self.trace)
        self.assertEqual(4, self.index)
        self.assertEqual(0, self.f.rcon_calls)
        self.assertEqual('LEASED_STOPPED', self.f.state()['state'])
        self.assertEqual(session['stop_operation'], result['stopOperationId'])
        self.assertEqual(session['control_operation'], result['controlOperationId'])
        with self.f.registry.transaction(read_only=True) as connection:
            operations_count = connection.execute("SELECT count(*) FROM runtime_operations WHERE command LIKE 'mod-control/%' AND status='COMPLETED'").fetchone()[0]
            self.assertEqual(4, operations_count)
            for request in self.requests[2:]:
                self.assertIsNotNone(operations.readback(connection, 'persistent-01', request))

    def test_same_stop_block_survives_real_instance_lock_contention(self):
        self.stop_with_contention()

    def test_stopping_proof_status_stop_block_and_public_stop_merge(self):
        self.stop_with_contention(observe=True)

    def test_receive_failure_preservation_waits_for_real_instance_lock(self):
        session = self.start()
        contended = threading.Event()
        self.receive_error = TimeoutError('元peerの受信不明')

        @contextmanager
        def checked_lock(path):
            try:
                with build_lock(path):
                    yield
            except BuildLockError:
                contended.set()
                raise

        with patch('dev_server.supervisor.build_lock', checked_lock):
            with build_lock(self.f.root / 'manager/locks/instance-persistent-01.lock'):
                self.stop_ready.set()
                self.assertTrue(contended.wait(2))
            session['thread'].join(3)
        self.assertFalse(session['thread'].is_alive())
        self.assertIs(self.receive_error, session['error'])
        self.assertNotIn('preservationError', session)
        self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])
        with self.f.registry.transaction(read_only=True) as connection:
            self.assertEqual('BLOCKED', connection.execute('SELECT gate FROM run_admission').fetchone()[0])
        self.assertEqual(['GRANT', 'ACK'], self.trace)

    def test_lock_wait_has_finite_original_deadline_and_does_not_run_transaction(self):
        path = self.f.root / 'manager/locks/instance-persistent-01.lock'
        before = time.monotonic()
        with build_lock(path), self.runtime._lock_wait(before + 0.05):
            with self.assertRaises(RegistryError) as caught:
                with self.runtime._transaction('persistent-01'):
                    self.fail('競合中はtransaction本体を実行しない')
        self.assertEqual('CONTROL_LOCK', caught.exception.code)
        self.assertLess(time.monotonic() - before, 1)
        self.assertEqual({}, self.managed.sessions)

    def test_stopping_proof_with_other_owner_cannot_enter_stop_admission(self):
        self.start()
        process = self.f.adapter.created[0]
        process.proof['phase'] = 'STOPPING'
        process.save_proof()
        self.session['context']['owner']['startId'] = 'other-start'
        self.f.command('status', {'instanceId': 'persistent-01'}, 'stale-owner-status')
        self.assertEqual('RECOVERY_REQUIRED', self.f.state()['state'])


@unittest.skipUnless(sys.platform in ('win32', 'darwin'), '承認fileのnative private権限を使用する')
class ApprovedInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_private(self.root, directory=True)
        self.path = self.root / 'approved.json'
        self.inventory = {'instances': [{'instanceId': 'slot-a'}]}
        self.value = {'managementRoot': str(self.root), 'instances': [dict(instanceId='slot-a', artifactId='a' * 64,
                      runtimeManifestHash='b' * 64, expectedEvidence=deepcopy(VECTORS['contexts']['Windows']['expectedEvidence']))]}

    def write(self, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        self.path.write_bytes(raw)
        make_private(self.path)
        return raw, wire.sha256(raw)

    def test_same_handle_private_input_and_independent_hash(self):
        raw, digest = self.write(self.value)
        original, result = approved_input(self.path, digest, self.root, self.inventory)
        self.assertEqual(raw, original)
        self.assertEqual(self.value['instances'][0], result['slot-a'])
        self.write(dict(self.value, instances=[]))
        self.assertEqual(self.value['instances'][0], result['slot-a'])
        with self.assertRaises(RegistryError):
            approved_input(self.path, digest, self.root, self.inventory)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows ctypes宣言の分離')
    def test_native_initialization_preserves_supervisor_pipe_signature(self):
        from . import local_ipc
        from .mod_control_transport import _WindowsNative
        before = list(local_ipc.kernel.CreateNamedPipeW.argtypes)
        _WindowsNative()
        self.assertEqual(before, local_ipc.kernel.CreateNamedPipeW.argtypes)
        server = local_ipc.WindowsListener(local_ipc.endpoint(self.root))
        server.close()

    def test_unknown_duplicate_root_and_budget_are_rejected(self):
        values = [dict(self.value, managementRoot=str(self.root / 'other')),
                  dict(self.value, instances=self.value['instances'] * 2),
                  dict(self.value, instances=[dict(self.value['instances'][0], instanceId='other')]),
                  dict(self.value, extra='unknown')]
        for value in values:
            raw, digest = self.write(value)
            with self.subTest(value=value.keys()), self.assertRaises(RegistryError):
                approved_input(self.path, digest, self.root, self.inventory)
        self.path.write_bytes(b' ' * 65537)
        with self.assertRaises(RegistryError):
            approved_input(self.path, wire.sha256(self.path.read_bytes()), self.root, self.inventory)

if __name__ == '__main__':
    unittest.main()
