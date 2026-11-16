"""認証済みBOUNDのData遷移とACKを、合成Mod・実Registryで検証する。"""
from copy import deepcopy
import ctypes
import inspect
import json
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from . import mod_control_operations as operations, mod_control_wire as wire, mod_control_transport, offline_control
from .build_inputs import canonical, digest
from .registry import RegistryError
from .test_mod_control_wire_v3 import VECTORS, frame
from . import test_offline_start_grant as grants


class StartAcknowledgeTests(unittest.TestCase):
    def setUp(self):
        self.f = grants.StartGrantTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.b, self.runtime = self.f.b, self.f.runtime
        self.original = self.b.base.o
        self.granted = False
        self.publish = True
        self.change = lambda state: None
        self.hook = lambda kind, stage, op: None
        self.bound = self.running_raw = None

        def observe(kind, stage, op):
            if stage == 'created':
                op.round = 'bound' if self.granted else 'hello'
            if (kind == 'write' and stage == 'finished' and op.round == 'hello'
                    and len(self.f.channel.sent) == len(self.f.f.candidate()['grant'])):
                self.granted = True
                self.prepare_bound()
            self.hook(kind, stage, op)

        self.f.io_hook = observe

    def prepare_bound(self):
        startup = self.f.f.candidate()
        state = json.loads(self.original.raw)
        state.update(runUuid=str(uuid.uuid4()), stateRevision=state['stateRevision'] + 1,
                     previousStateSha256=digest(self.original.raw))
        state['payload'] = dict(
            type='io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldRunPhase.Running',
            owner=deepcopy(startup['context']['owner']), management=deepcopy(startup['context']['management']))
        self.change(state)
        self.running_raw = canonical(state)
        immutable = self.original.data / 'run-state/states' / (digest(self.running_raw) + '.json')
        immutable.write_bytes(self.running_raw)
        if self.publish:
            self.original.current_path.write_bytes(self.running_raw)
        bound = wire.decode_frame_v3(bytes.fromhex(VECTORS['sessions'][0]['pairs'][1]['request']['frameHex']))
        evidence = json.loads(wire.decode_frame_v3(startup['hello'])['payload']['profileEvidenceJson'])
        evidence['checkpoint'] = 'BOUND'
        evidence_raw = canonical(evidence)
        status_raw = canonical(dict(sha256=digest(self.running_raw), state=state))
        bound['descriptorSha256'] = digest(startup['context']['descriptorJson'].encode())
        bound['payload'].update(owner=startup['context']['owner'], management=startup['context']['management'],
            profileEvidenceJson=evidence_raw.decode(), profileEvidenceSha256=digest(evidence_raw),
            statusJson=status_raw.decode(), statusJsonSha256=digest(status_raw))
        self.bound = self.f.channel.incoming = frame(bound)

    def acknowledge(self):
        from .test_offline_start_accept import preparation
        records = preparation.creation.descriptors.records
        return self.runtime._offline_acknowledge_start(self.b.token, operation_id=records.reservations.START,
            read_operation_id=records.reservations.readers.OPERATION,
            open_operation_id=self.original.base.opened['operationId'], ready=self.original.ready,
            deadline=self.b.base.f.deadline)

    def retained(self):
        self.f.retained()
        before = bytes(self.f.channel.sent), len(self.f.native.operations)
        with self.assertRaises(RegistryError):
            self.acknowledge()
        self.assertEqual(before, (self.f.channel.sent, len(self.f.native.operations)))
        operation, refs = self.b.saved()
        self.assertEqual('PENDING', operation['status'])
        self.assertTrue(refs)

    def reject(self):
        with self.assertRaises((RegistryError, ValueError, OSError)):
            self.acknowledge()
        self.retained()

    def fault(self, kind, stage, change):
        fired = []
        def hook(actual, at, op):
            if (actual, at, op.round) == (kind, stage, 'bound') and not fired:
                fired.append(True)
                change(op)
        self.hook = hook
        self.reject()
        self.assertEqual([True], fired)

    def test_running_bound_and_ack_keep_original_connection_and_closed_reservation(self):
        result, (session, process, startup, check) = self.acknowledge()
        self.assertEqual('OFFLINE_START_ACKNOWLEDGED', result['status'])
        self.assertIs(startup['connection'], session['endpoint'].connection)
        self.assertIs(process, self.f.f.f.f.m)
        self.assertEqual(self.bound, startup['request'])
        self.assertEqual(startup['grant'] + startup['ack'], self.f.channel.sent)
        wire.correlate_response_v3(self.bound, startup['ack'])
        self.assertEqual(digest(self.bound[4:]), result['boundBodySha256'])
        self.assertEqual(digest(startup['ack'][4:]), result['ackBodySha256'])
        self.assertEqual(['accept','read','read','write','read','read','write'], self.f.native.started)
        self.assertTrue(all(op.released for op in self.f.native.operations))
        with self.runtime._transaction(self.b.token['instanceId'], read_only=True) as connection:
            self.assertEqual(startup['ack'], operations.readback(connection, self.b.token['instanceId'], self.bound))
            gate = connection.execute('SELECT * FROM run_admission WHERE run=?', (result['runId'],)).fetchone()
            self.assertEqual(('CLOSED', 1), (gate['gate'], gate['revision']))
        self.assertIsInstance(check(), str)
        self.assertEqual('PENDING', self.b.saved()[0]['status'])
        self.f.f.peer.close.assert_not_called()
        self.f.channel.close.assert_not_called()

    def test_exact_ready_current_is_allowed_after_authenticated_bound(self):
        self.publish = False
        self.acknowledge()[1][3]()

    def test_other_run_current_refuses_ack(self):
        def hook(kind, stage, op):
            if (kind, stage, op.round) == ('read', 'created', 'bound'):
                other = json.loads(self.running_raw)
                other['runUuid'] = str(uuid.uuid4())
                self.original.current_path.write_bytes(canonical(other))
        self.hook = hook
        self.reject()

    def test_other_generation_bound_refuses_ack(self):
        self.change = lambda state: state.update(generation=str(uuid.uuid4()))
        self.reject()

    def test_revision_regression_refuses_ack(self):
        self.change = lambda state: state.update(stateRevision=self.original.ready['stateRevision'] - 1)
        self.reject()

    def test_previous_ready_hash_mismatch_refuses_ack(self):
        self.change = lambda state: state.update(previousStateSha256='e' * 64)
        self.reject()

    def test_running_management_mismatch_refuses_ack(self):
        self.change = lambda state: state['payload']['management'].update(epoch=999)
        self.reject()

    def test_bound_without_published_immutable_refuses_ack(self):
        def hook(kind, stage, op):
            if (kind, stage, op.round) == ('read', 'created', 'bound'):
                path = self.original.data / 'run-state/states' / (digest(self.running_raw) + '.json')
                if path.exists():
                    path.unlink()
        self.hook = hook
        self.reject()

    def test_running_without_bound_is_retained(self):
        def hook(kind, stage, op):
            if (kind, stage, op.round) == ('read', 'created', 'bound'):
                self.f.channel.incoming = self.f.hello
        self.hook = hook
        self.reject()

    def test_running_before_grant_issues_zero_write(self):
        def hook(kind, stage, op):
            if (kind, stage, op.round) == ('write', 'prepared', 'hello'):
                self.prepare_bound()
        self.hook = hook
        self.reject()
        self.assertNotIn('write', self.f.native.started)

    def transient_current(self, change, *, restore_ready=False):
        observed, changed, restored = [], [], []
        read = offline_control.regular_bytes

        def reading(path, *args, **kwargs):
            raw = read(path, *args, **kwargs)
            if path == self.original.current_path:
                observed.append(raw)
            return raw

        def hook(kind, stage, op):
            if (kind, stage, op.round) == ('write', 'finished', 'hello'):
                raw = change(self.running_raw)
                changed.append(raw)
                self.original.current_path.write_bytes(raw)
                if restore_ready:
                    (self.original.data / 'run-state/states' / (digest(raw) + '.json')).write_bytes(raw)
            if (kind, stage, op.round) == ('read', 'finished', 'bound') and not restored:
                self.original.current_path.write_bytes(self.original.raw if restore_ready else self.running_raw)
                restored.append(True)

        self.hook = hook
        with patch.object(offline_control, 'regular_bytes', side_effect=reading):
            self.reject()
        raw, = changed
        self.assertIn(raw, observed)
        self.assertEqual([True] if restore_ready else [], restored)
        startup = self.f.f.candidate()
        self.assertEqual(startup['grant'], self.f.channel.sent)
        self.assertEqual(1, self.f.native.started.count('write'))
        self.assertNotIn('ack', startup)
        self.assertNotIn('acknowledged', startup)
        with self.runtime._transaction(self.b.token['instanceId'], read_only=True) as connection:
            self.assertIsNone(operations.readback(connection, self.b.token['instanceId'], self.bound))
        packet = self.f.f.f.f.held()['packet']
        inputs = inspect.getclosurevars(packet[1]).nonlocals['inputs']
        current = inspect.getclosurevars(inputs).nonlocals['current_check']
        self.assertEqual((raw, digest(raw)), inspect.getclosurevars(current).nonlocals['current_candidate'])
        self.original.current_path.write_bytes(self.running_raw)
        with self.assertRaises(RegistryError):
            packet[1]()
        self.assertEqual((raw, digest(raw)), inspect.getclosurevars(current).nonlocals['current_candidate'])
        self.assertEqual(self.original.raw,
            (self.original.data / 'run-state/states' / (digest(self.original.raw) + '.json')).read_bytes())
        self.retained()

    def test_invalid_current_after_grant_cannot_be_repaired_during_bound(self):
        self.transient_current(lambda raw: b'{}')

    def test_other_generation_current_after_grant_cannot_be_repaired_during_bound(self):
        def change(raw):
            state = json.loads(raw)
            state['generation'] = str(uuid.uuid4())
            return canonical(state)
        self.transient_current(change)

    def test_observed_running_candidate_must_match_authenticated_bound(self):
        def change(raw):
            state = json.loads(raw)
            state['runUuid'] = str(uuid.uuid4())
            return canonical(state)
        self.transient_current(change, restore_ready=True)

    def test_epoch_after_bound_preparation_issues_zero_bound_read(self):
        self.fault('read', 'prepared', lambda op: self.b.commit('epoch'))
        self.assertEqual(['accept','read','read','write'], self.f.native.started)

    def test_deadline_after_ack_preparation_issues_zero_ack(self):
        self.fault('write', 'prepared', lambda op: setattr(self.b.base.f, 'clock', self.b.base.f.deadline))
        self.assertEqual(1, self.f.native.started.count('write'))

    def test_epoch_after_bound_finish_prevents_ack(self):
        self.fault('read', 'finished', lambda op: self.b.commit('epoch'))
        self.assertEqual(1, self.f.native.started.count('write'))

    def test_epoch_after_ack_finish_refuses_return(self):
        self.fault('write', 'finished', lambda op: self.b.commit('epoch'))

    def test_epoch_after_ack_release_refuses_return(self):
        self.fault('write', 'released', lambda op: self.b.commit('epoch'))

    def test_bound_release_failure_keeps_pending_and_connection(self):
        from unittest.mock import Mock
        def change(op):
            op.release = Mock(side_effect=OSError('合成event解放失敗'))
        self.fault('read', 'finished', change)
        connection = self.f.f.candidate()['connection']
        self.assertIs(connection.pending, self.f.native.operations[-1])
        self.assertTrue(connection.close_unknown)
        connection.pending.release.assert_called_once()

    def test_partial_ack_failure_keeps_original_bytes_and_connection(self):
        self.f.native.chunk = 256
        def hook(kind, stage, op):
            if (kind, stage, op.round) == ('write', 'prepared', 'bound'):
                if len(self.f.channel.sent) > len(self.f.f.candidate()['grant']):
                    raise OSError('合成ACK部分送信失敗')
        self.hook = hook
        self.reject()
        startup = self.f.f.candidate()
        transferred = len(self.f.channel.sent) - len(startup['grant'])
        self.assertGreater(transferred, 0)
        self.assertLess(transferred, len(startup['ack']))

    def actual_preparation(self, kind, *, windows):
        from unittest.mock import Mock
        change = (lambda: self.b.commit('epoch')) if kind == 'read' else (
            lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline))
        make = self.f.native.operation.side_effect
        if windows:
            class Overlapped(ctypes.Structure):
                _fields_ = [('hEvent', ctypes.c_void_p)]
            kernel = Mock()
            kernel.CreateEventW.side_effect = lambda *args: (change(), 82)[1]
            backend = SimpleNamespace(Overlapped=Overlapped, kernel=kernel, close_handle=Mock(),
                                      check=lambda value: self.assertTrue(value))
        else:
            stream = SimpleNamespace(fileno=lambda: 77, recv=Mock(return_value=b'\0\0\0\0'), send=Mock(return_value=1))
            self.f.channel.stream = stream

        def operation(channel, actual, value):
            if not self.granted or actual != kind:
                return make(channel, actual, value)
            if windows:
                op = mod_control_transport._WinOperation(backend, channel, actual, value)
            else:
                op = mod_control_transport._MacOperation(channel, actual, value)
                start = op.start
                def prepared(**kwargs):
                    change()
                    return start(**kwargs)
                op.start = prepared
            self.f.native.operations.append(op)
            return op

        self.f.native.operation.side_effect = operation
        self.reject()
        if windows:
            kernel.CreateEventW.assert_called_once()
            kernel.ReadFile.assert_not_called()
            kernel.WriteFile.assert_not_called()
            backend.close_handle.assert_called_once_with(82)
        else:
            stream.recv.assert_not_called()
            stream.send.assert_not_called()

    def test_actual_windows_bound_preparation_epoch_issues_zero_read(self):
        self.actual_preparation('read', windows=True)

    def test_actual_windows_ack_preparation_deadline_issues_zero_write(self):
        self.actual_preparation('write', windows=True)

    def test_actual_macos_bound_preparation_epoch_issues_zero_recv(self):
        self.actual_preparation('read', windows=False)

    def test_actual_macos_ack_preparation_deadline_issues_zero_send(self):
        self.actual_preparation('write', windows=False)

    def test_ack_buffer_changed_after_preparation_issues_zero_ack(self):
        self.fault('write', 'prepared', lambda op: op.buffer.__setitem__(0, op.buffer[0] ^ 1))
        self.assertEqual(1, self.f.native.started.count('write'))

    def test_bound_buffer_changed_after_finish_refuses_ack(self):
        self.fault('read', 'finished', lambda op: op.buffer.__setitem__(0, op.buffer[0] ^ 1))

    def test_public_renew_during_ack_preserves_deadline(self):
        def hook(kind, stage, op):
            if (kind, stage, op.round) == ('write', 'prepared', 'bound'):
                self.b.base.pool.submit(self.runtime.registry.execute, 'renew', dict(self.b.token, ttlSeconds=600),
                    operation_id='ack-renew').result(10)
        self.hook = hook
        self.acknowledge()[1][3]()

    def test_approval_change_inside_ack_write_rolls_back(self):
        complete = operations.complete
        def changed(*args):
            result = complete(*args)
            if self.bound is not None and args[2] == self.bound:
                self.f.f.managed.sessions[self.b.token['instanceId']]['approval']['expectedEvidence']['profileSha256'] = 'e' * 64
            return result
        with patch.object(operations, 'complete', side_effect=changed):
            self.reject()
        with self.runtime._transaction(self.b.token['instanceId'], read_only=True) as connection:
            self.assertIsNone(operations.readback(connection, self.b.token['instanceId'], self.bound))

    def final_copy(self, change):
        copy = offline_control.deepcopy
        def mutate(value):
            result = copy(value)
            if isinstance(value, dict) and value.get('status') == 'OFFLINE_START_ACKNOWLEDGED':
                change()
            return result
        with patch.object(offline_control, 'deepcopy', side_effect=mutate):
            self.reject()

    def test_final_copy_ack_replacement_refuses_return(self):
        self.final_copy(lambda: self.f.f.candidate().update(ack=b'changed'))

    def test_final_copy_original_grant_replacement_refuses_return(self):
        self.final_copy(lambda: self.f.f.candidate().update(grant=b'changed'))

    def test_final_copy_io_release_change_refuses_return(self):
        self.final_copy(lambda: setattr(self.f.native.operations[-1], 'released', False))

    def test_final_copy_deadline_refuses_return(self):
        self.final_copy(lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline))

    def test_final_copy_completed_packet_replacement_refuses_return(self):
        def change():
            held = self.f.f.f.f.held()
            held['acknowledged'] = tuple(list(held['acknowledged']))
        self.final_copy(change)

    def test_final_copy_current_replacement_refuses_return(self):
        self.final_copy(lambda: self.original.current_path.write_bytes(b'{}'))
