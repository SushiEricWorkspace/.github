"""元ACKと起動proofのCAS記録を合成M・実Registryで検証する。"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from . import offline_control, mod_control_wire as wire
from .build_inputs import canonical, digest
from .registry import RegistryError
from . import test_offline_start_acknowledge as acknowledgements


class StartProofTests(unittest.TestCase):
    final_actions = {'read': 28, 'approval': 3, 'identity': 7, 'host_boot': 3,
                     'transaction': 13, 'poll': 1, 'sockets': 1}

    def setUp(self):
        self.f = acknowledgements.StartAcknowledgeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.b, self.runtime = self.f.b, self.f.runtime
        self.creator = self.f.f.f.f.f
        self.creator.m.poll.side_effect = None
        self.creator.m.poll.return_value = None
        self.sockets = []
        self.enterContext(patch.object(self.runtime.process_adapter, 'sockets',
                                      side_effect=lambda pid: deepcopy(self.sockets), create=True))
        identity = self.runtime.process_adapter.identity

        def identified(pid):
            value = identity(pid)
            if pid == 98765:
                value['startMillis'] = pid
            return value

        self.enterContext(patch.object(self.runtime.process_adapter, 'identity', side_effect=identified))
        self.proof_raw = None
        self.change = lambda proof: None

        def hook(kind, stage, op):
            if (kind, stage, op.round) == ('write', 'finished', 'bound'):
                self.publish_proof()

        self.f.hook = hook

    def held(self):
        return self.creator.held()

    def publish_proof(self):
        run = self.held()['creation_run']
        generation = Path(run['cwd'])
        listeners = [dict(service=service, **{k: value[k] for k in ('protocol','address','port')})
                     for service, value in run['endpoints'].items()]
        self.sockets = deepcopy(listeners)
        proof = dict(schemaVersion=1, instanceId=run['instanceId'], generationId=run['generationId'],
            startNonce=run['nonce'], phase='STARTED', requiresSupervisorProcessExit=True,
            requiresExternalWriterDrain=True,
            identity=dict(pid=run['identity']['pid'],
                processStartedAt=datetime.fromtimestamp(run['identity']['startMillis']/1000, timezone.utc).isoformat(),
                cwd=run['cwd'], artifactId=run['artifactId'], runtimeManifestSha256=run['manifestHash'],
                runtimeManifest=str(generation/'runtime/runtime.json')),
            startup=dict(ready=True, managementEnabled=True, managementStarted=True, unknownWriters=[],
                roots=dict(world=str(generation/'world'), config=str(generation/'config'),
                           modConfig=str(generation/'config/SushiEricServerMod')), listeners=listeners))
        self.change(proof)
        self.proof_raw = canonical(proof)
        Path(run['proofPath']).write_bytes(self.proof_raw)

    def record(self):
        from .test_offline_start_accept import preparation
        records = preparation.creation.descriptors.records
        return self.runtime._offline_record_start_proof(self.b.token, operation_id=records.reservations.START,
            read_operation_id=records.reservations.readers.OPERATION,
            open_operation_id=self.f.original.base.opened['operationId'], ready=self.f.original.ready,
            deadline=self.b.base.f.deadline)

    def saved(self):
        with self.runtime._transaction(self.b.token['instanceId'], read_only=True) as db:
            run_id = self.held()['run']['runId']
            record = db.execute('SELECT record FROM runs WHERE id=?', (run_id,)).fetchone()[0]
            gate = dict(db.execute('SELECT * FROM run_admission WHERE run=?', (run_id,)).fetchone())
        return json.loads(record), gate

    def retained(self):
        held = self.held()
        self.assertFalse(held['returned'])
        self.assertIs(self.creator.m, held['creation'][1])
        startup = held['startup'][0]
        connection = held['accepted'][0]
        self.assertIs(connection, startup['connection'])
        self.assertIs(connection, startup['endpoint'].connection)
        self.f.f.f.peer.close.assert_not_called()
        self.f.f.channel.close.assert_not_called()
        self.creator.m.close.assert_not_called()
        self.assertIsNotNone(self.b.base.p.reader.request)
        self.assertIsNotNone(self.b.base.p.reader.ready_check_response)
        self.assertEqual('PENDING', self.b.saved()[0]['status'])
        self.assertTrue(self.b.saved()[1])
        self.assertEqual(('STARTING', 1), (self.b.row()['state'], self.b.row()['pending']))
        effects = self.creator.generated, bytes(self.f.f.channel.sent), len(self.f.f.native.operations)
        with self.assertRaises(RegistryError):
            self.record()
        self.assertEqual(effects, (self.creator.generated, bytes(self.f.f.channel.sent), len(self.f.f.native.operations)))
        self.assertIs(held, self.held())

    def reject(self):
        with self.assertRaises((RegistryError, ValueError, OSError)):
            self.record()
        self.retained()

    def final_copy(self, change):
        def copy(value, *args, **kwargs):
            result = deepcopy(value, *args, **kwargs)
            if isinstance(value, dict) and value.get('status') == 'OFFLINE_START_PROOF_RECORDED':
                change()
            return result
        with patch.object(offline_control, 'deepcopy', side_effect=copy):
            self.reject()

    @contextmanager
    def observe_final_actions(self, target=None, change=None):
        state = dict(phase=None, events=[], fired=False, finished=False)

        def observed(kind, detail=''):
            phase = state['phase']
            if phase is None:
                return
            self.assertNotEqual('pure', phase, (kind, detail))
            if phase != 'external':
                return
            index = sum(item[0] == kind for item in state['events']) + 1
            state['events'].append((kind, index, detail))
            if target == (kind, index):
                self.assertFalse(state['fired'])
                state['fired'] = True
                change()

        def profile(frame, event, value):
            if state['phase'] is None:
                return
            name = frame.f_code.co_name
            filename = frame.f_code.co_filename.replace('\\', '/').rsplit('/', 1)[-1]
            if name == 'proof_files' and frame.f_locals.get('final'):
                if event == 'call':
                    state['phase'] = 'final'
                elif event == 'return':
                    state['phase'] = 'pure'
                    state['finished'] = True
            if event == 'return':
                if (filename == 'build_inputs.py' and name == 'regular_bytes'
                        or filename == 'pathlib.py' and name in ('read_bytes', 'read_text')
                        and frame.f_back.f_code.co_name != 'regular_bytes'):
                    path = frame.f_locals.get('path', frame.f_locals.get('self'))
                    observed('read', str(path))
                if filename == 'registry.py' and name == 'transaction' and value is None:
                    observed('transaction')
                if name == 'verify_start_proof':
                    state['phase'] = None

        original_copy = offline_control.deepcopy
        def copy(value, *args, **kwargs):
            result = original_copy(value, *args, **kwargs)
            if isinstance(value, dict) and value.get('status') == 'OFFLINE_START_PROOF_RECORDED':
                state['phase'] = 'external'
                sys.setprofile(profile)
            return result

        def wrap(original, kind):
            def call(*args, **kwargs):
                result = original(*args, **kwargs)
                observed(kind)
                return result
            return call

        previous = sys.getprofile()
        with ExitStack() as stack:
            objects = (self.runtime.process_adapter, self.f.original.c.adapter, self.b.base.p.reader.adapter)
            seen = set()
            for adapter in objects:
                if id(adapter) not in seen:
                    seen.add(id(adapter))
                    stack.enter_context(patch.object(adapter, 'identity', side_effect=wrap(adapter.identity, 'identity')))
            native = self.f.original.c.native
            stack.enter_context(patch.object(native, 'host_boot', side_effect=wrap(native.host_boot, 'host_boot')))
            stack.enter_context(patch.object(self.creator.m, 'poll', side_effect=wrap(self.creator.m.poll, 'poll')))
            stack.enter_context(patch.object(self.runtime.process_adapter, 'sockets',
                                             side_effect=wrap(self.runtime.process_adapter.sockets, 'sockets')))
            stack.enter_context(patch.object(self.runtime.managed, 'approved',
                                             side_effect=wrap(self.runtime.managed.approved, 'approval')))
            stack.enter_context(patch.object(offline_control, 'deepcopy', side_effect=copy))
            try:
                yield state
            finally:
                sys.setprofile(previous)

    def test_final_gate_action_catalog_and_pure_return(self):
        with self.observe_final_actions() as state:
            self.record()
        counts = {kind: sum(item[0] == kind for item in state['events']) for kind in self.final_actions}
        print('FINAL_ACTION_COUNTS', counts)
        self.assertEqual(self.final_actions, counts)
        self.assertEqual(set(self.final_actions), {item[0] for item in state['events']})
        self.assertTrue(state['finished'])

    def final_action_change(self, kind, index, effect):
        poll = self.creator.m.poll
        def change():
            if effect == 'current':
                self.f.original.current_path.write_bytes(b'{}')
            elif effect == 'proof':
                Path(self.held()['creation_run']['proofPath']).write_bytes(b'{}')
            else:
                poll.return_value = 0
        with self.observe_final_actions((kind, index), change) as state:
            self.reject()
        self.assertTrue(state['fired'], (kind, index, effect, state['events']))
        self.assertEqual(self.proof_raw, self.held()['proof'][0])
        self.assertEqual(self.proof_raw.decode(), self.saved()[0]['startupProofJson'])

    def test_current_change_after_final_sockets_refuses_return(self):
        self.final_action_change('sockets', 1, 'current')

    def test_proof_change_after_final_sockets_refuses_return(self):
        self.final_action_change('sockets', 1, 'proof')

    def test_ack_proof_and_original_running_are_recorded_with_closed_reservations(self):
        result, (session, process, startup, verify) = self.record()
        record, gate = self.saved()
        self.assertEqual('OFFLINE_START_PROOF_RECORDED', result['status'])
        self.assertEqual(self.proof_raw.decode(), record['startupProofJson'])
        self.assertEqual(digest(self.proof_raw), record['startupProofSha256'])
        self.assertEqual(digest(verify().encode()), result['recordSha256'])
        proof = record['startupAcknowledgement']
        self.assertEqual(result['ackBodySha256'], proof['ackBodySha256'])
        self.assertEqual(self.f.running_raw, canonical(json.loads(proof['statusJson'])['state']))
        self.assertEqual(('CLOSED',1), (gate['gate'],gate['revision']))
        self.assertEqual('PENDING', self.b.saved()[0]['status'])
        self.assertIs(process, self.creator.m)
        self.assertIs(startup['connection'], session['endpoint'].connection)
        self.assertEqual(1, self.creator.generated)
        self.assertTrue(self.held()['returned'])

    def test_invalid_nonce_has_no_proof_record(self):
        self.change = lambda proof: proof.update(startNonce='wrong')
        self.reject()
        self.assertNotIn('startupProofJson', self.saved()[0])

    def test_nonstarted_phase_has_no_proof_record(self):
        self.change = lambda proof: proof.update(phase='STOPPED')
        self.reject()

    def test_listener_mismatch_has_no_proof_record(self):
        self.change = lambda proof: proof['startup']['listeners'][0].update(port=65000)
        self.reject()

    def test_process_exit_has_no_proof_record(self):
        self.creator.m.poll.return_value = 0
        self.reject()

    def test_epoch_commit_during_proof_read_has_no_proof_record(self):
        original = offline_control.regular_bytes
        def read(path, *args, **kwargs):
            raw = original(path, *args, **kwargs)
            if path.name == 'proof.json':
                self.b.commit('epoch')
            return raw
        with patch.object(offline_control, 'regular_bytes', side_effect=read):
            self.reject()
        self.assertNotIn('startupProofJson', self.saved()[0])

    def test_read_crossing_deadline_has_no_proof_record(self):
        original = offline_control.regular_bytes
        def read(path, *args, **kwargs):
            raw = original(path, *args, **kwargs)
            if path.name == 'proof.json':
                self.b.base.f.clock = self.b.base.f.deadline
            return raw
        with patch.object(offline_control, 'regular_bytes', side_effect=read):
            self.reject()

    def test_final_copy_rejects_changed_proof_file(self):
        self.final_copy(lambda: Path(self.held()['creation_run']['proofPath']).write_bytes(b'{}'))

    def test_final_copy_rejects_changed_record_object(self):
        self.final_copy(lambda: self.held()['proof'][1].update(startupProofSha256='0'*64))

    def test_final_copy_rejects_replaced_packet(self):
        self.final_copy(lambda: self.held().update(proof=tuple(list(self.held()['proof']))))

    def test_final_copy_rejects_inherited_reader_mutation(self):
        self.final_copy(lambda: self.b.base.p.reader.ready_check_release.update(pipe='UNKNOWN'))

    def test_final_copy_rejects_ack_raw_mutation(self):
        self.final_copy(lambda: self.held()['startup'][0].update(ack=b'changed'))

    def test_final_copy_rejects_deadline_after_commit(self):
        self.final_copy(lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline))
        self.assertIn('startupProofJson', self.saved()[0])

    def test_epoch_commit_after_final_read_prevents_return(self):
        transaction = self.runtime._transaction
        fired = []
        # 再帰する観測は元transactionで行う。
        def saved():
            with transaction(self.b.token['instanceId'], read_only=True) as db:
                return json.loads(db.execute('SELECT record FROM runs WHERE id=?', (self.held()['run']['runId'],)).fetchone()[0])
        @contextmanager
        def boundary(instance, *, read_only=False):
            with transaction(instance, read_only=read_only) as db:
                yield db
            held = self.b.offline.start_records.get(self.b.key, {})
            if read_only and 'proof' in held and not fired and 'startupProofJson' in saved():
                fired.append(True)
                self.b.commit('epoch')
        with patch.object(self.runtime, '_transaction', boundary):
            self.reject()
        self.assertEqual([True], fired)


    def at_record(self, change):
        original = self.runtime._record
        def record(value, **kwargs):
            if 'startupProofJson' in value:
                change(value, kwargs)
            return original(value, **kwargs)
        return patch.object(self.runtime, '_record', side_effect=record)

    def test_competing_run_commit_is_not_overwritten(self):
        def change(value, kwargs):
            def commit():
                with self.runtime.registry.transaction() as db:
                    record = json.loads(kwargs['expected'])
                    record['competitor'] = True
                    db.execute('UPDATE runs SET record=? WHERE id=?', (json.dumps(record),value['runId']))
            self.b.base.pool.submit(commit).result(10)
        with self.at_record(change):
            self.reject()
        self.assertTrue(self.saved()[0]['competitor'])
        self.assertNotIn('startupProofJson', self.saved()[0])

    def test_epoch_before_proof_write_has_no_proof_record(self):
        with self.at_record(lambda value, kwargs: self.b.commit('epoch')):
            self.reject()
        self.assertNotIn('startupProofJson', self.saved()[0])

    def test_deadline_before_proof_write_has_no_proof_record(self):
        with self.at_record(lambda value, kwargs: setattr(self.b.base.f, 'clock', self.b.base.f.deadline)):
            self.reject()
        self.assertNotIn('startupProofJson', self.saved()[0])

    def test_proof_change_after_serialization_rolls_back(self):
        def change(value, kwargs):
            verify = kwargs['verify']
            def changed(db, encoded):
                value['startupProofSha256'] = '0'*64
                verify(db, encoded)
            kwargs['verify'] = changed
        with self.at_record(change):
            self.reject()
        self.assertNotIn('startupProofJson', self.saved()[0])

    def test_commit_response_loss_keeps_record_and_resources(self):
        original = self.runtime._record
        def record(value, **kwargs):
            result = original(value, **kwargs)
            if 'startupProofJson' in value:
                raise OSError('正本commit後の応答喪失')
            return result
        with patch.object(self.runtime, '_record', side_effect=record):
            self.reject()
        self.assertIn('startupProofJson', self.saved()[0])

    def test_public_renew_before_write_and_after_return_are_allowed(self):
        def renew(operation):
            self.b.base.pool.submit(self.runtime.registry.execute, 'renew',
                dict(self.b.token, ttlSeconds=600), operation_id=operation).result(10)
        with self.at_record(lambda value, kwargs: renew('proof-renew')):
            _, (_, _, _, verify) = self.record()
        renew('proof-renew-after')
        self.assertIn('startupProofJson', json.loads(verify()))

    def test_wrong_process_proof_has_no_proof_record(self):
        self.change = lambda proof: proof['identity'].update(pid=12345)
        self.reject()
        self.assertNotIn('startupProofJson', self.saved()[0])

    def test_final_copy_rejects_changed_running_current(self):
        self.final_copy(lambda: self.f.original.current_path.write_bytes(b'{}'))

    def final_approval(self, change):
        original = self.runtime.managed.approved
        armed, fired = [], []
        def approved(*args, **kwargs):
            result = original(*args, **kwargs)
            if armed and not fired:
                fired.append(True)
                change()
            return result
        with patch.object(self.runtime.managed, 'approved', side_effect=approved):
            self.final_copy(lambda: armed.append(True))
        self.assertEqual([True], fired)
        self.assertEqual(self.proof_raw, self.held()['proof'][0])
        self.assertEqual(self.proof_raw.decode(), self.saved()[0]['startupProofJson'])

    def test_proof_change_after_final_approval_refuses_return(self):
        self.final_approval(lambda: Path(self.held()['creation_run']['proofPath']).write_bytes(b'{}'))

    def test_process_exit_after_final_approval_refuses_return(self):
        self.final_approval(lambda: setattr(self.creator.m.poll, 'return_value', 0))

    def final_proof_read(self, change):
        original = offline_control.regular_bytes
        armed, fired = [], []
        def read(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name == 'proof.json' and armed and not fired:
                fired.append(True)
                self.b.base.pool.submit(change).result(10)
            return result
        with patch.object(offline_control, 'regular_bytes', side_effect=read):
            self.final_copy(lambda: armed.append(True))
        self.assertEqual([True], fired)

    def test_epoch_commit_during_final_proof_read_refuses_return(self):
        def commit():
            with self.runtime.registry.transaction() as db:
                db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?', (self.b.token['instanceId'],))
        self.final_proof_read(commit)

    def test_ack_commit_during_final_proof_read_refuses_return(self):
        def commit():
            request = self.held()['acknowledged'][0]
            with self.runtime.registry.transaction() as db:
                db.execute('DELETE FROM runtime_operations WHERE id=?', (wire.decode_frame_v3(request)['operationId'],))
        self.final_proof_read(commit)

    def test_proof_write_has_no_native_or_file_read(self):
        transaction = self.runtime._transaction
        active = []
        @contextmanager
        def boundary(instance, *, read_only=False):
            with transaction(instance, read_only=read_only) as db:
                held = self.b.offline.start_records.get(self.b.key, {})
                writing = not read_only and 'proof' in held
                if writing:
                    active.append(True)
                try:
                    yield db
                finally:
                    if writing:
                        active.pop()
        def observed(original):
            def call(*args, **kwargs):
                self.assertFalse(active)
                return original(*args, **kwargs)
            return call
        with patch.object(self.runtime, '_transaction', boundary), \
             patch.object(offline_control, 'regular_bytes', side_effect=observed(offline_control.regular_bytes)), \
             patch.object(self.creator.m, 'poll', side_effect=observed(self.creator.m.poll)), \
             patch.object(self.runtime.process_adapter, 'identity', side_effect=observed(self.runtime.process_adapter.identity)), \
             patch.object(self.runtime.process_adapter, 'sockets', side_effect=observed(self.runtime.process_adapter.sockets)):
            self.record()[1][3]()


def action_test(kind, index, effect):
    def test(self):
        self.final_action_change(kind, index, effect)
    return test


for action, count in StartProofTests.final_actions.items():
    for index in range(1, count + 1):
        for effect in ('current', 'proof', 'exit'):
            if action == 'sockets' and effect in ('current', 'proof'):
                continue
            setattr(StartProofTests, f'test_final_action_{action}_{index:02d}_{effect}', action_test(action, index, effect))


if __name__ == '__main__':
    unittest.main()
