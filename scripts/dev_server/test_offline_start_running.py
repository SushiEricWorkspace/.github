"""固定ACK/proofからの原子的な起動確定を実Registryで検証する。"""
from unittest.mock import patch
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from . import offline_control, mod_control_runtime
from .registry import RegistryError
from . import test_offline_start_proof as proofs


class StartRunningTests(unittest.TestCase):
    def setUp(self):
        self.f = proofs.StartProofTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.runtime = self.f.runtime
        self.b = self.f.b
        if not getattr(self, 'real_receiver', False):
            class Receiver:
                def __init__(self, **options):
                    self.options = options
                    self.started = False
                def start(self):
                    self.started = True
                def is_alive(self):
                    return self.started
            original = self.runtime.managed.listen
            def listen(*args, **kwargs):
                with patch.object(mod_control_runtime, 'threading', SimpleNamespace(Thread=Receiver)):
                    return original(*args, **kwargs)
            self.enterContext(patch.object(self.runtime.managed, 'listen', side_effect=listen))

    def record(self):
        with patch.object(self.runtime, '_offline_record_start_proof', self.runtime._offline_finish_start):
            return self.f.record()

    def test_atomic_running_keeps_same_resources_and_ports(self):
        result, packet = self.record()
        self.assertEqual('OFFLINE_START_RUNNING', result['status'])
        self.assertEqual(('RUNNING', 0), (self.b.row()['state'], self.b.row()['pending']))
        self.assertEqual(('OPEN', 2), tuple(self.f.saved()[1][k] for k in ('gate', 'revision')))
        self.assertEqual('COMPLETED', self.b.saved()[0]['status'])
        self.assertFalse(self.b.saved()[1])
        self.assertEqual(result, json.loads(self.b.saved()[0]['response']))
        self.assertIs(packet[0], self.f.held()['creation'][0])
        self.assertIs(packet[1], self.f.creator.m)
        self.assertTrue(self.f.held()['returned'])
        with self.runtime.registry.transaction(read_only=True) as db:
            ports = [dict(v) for v in db.execute('SELECT * FROM endpoint_reservations WHERE instance=?',
                                                (self.b.token['instanceId'],))]
        self.assertEqual(len(self.f.held()['creation_run']['endpoints']), len(ports))
        self.assertTrue(all(v['run'] == result['runId'] for v in ports))
        packet[3]()

    def test_control_token_change_inside_write_rolls_back(self):
        def change():
            self.f.held()['creation'][0]['token']['epoch'] += 1
        with self.boundary('inside', change):
            self.reject()

    def test_control_context_change_inside_write_rolls_back(self):
        def change():
            self.f.held()['creation'][0]['context']['owner']['startId'] = 'replaced-owner'
        with self.boundary('inside', change):
            self.reject()

    def test_control_process_registration_change_inside_write_rolls_back(self):
        def change():
            value = self.runtime.game_processes[self.f.held()['run']['runId']]
            self.runtime.game_processes[self.f.held()['run']['runId']] = tuple(list(value))
        with self.boundary('inside', change):
            self.reject()

    def receiver_observation(self, change):
        def observe():
            change()
            return True
        with self.boundary('after', lambda: setattr(self.f.held()['receiver'], 'is_alive', observe)):
            self.reject(committed=True)

    def test_final_receiver_current_change_refuses_return(self):
        self.receiver_observation(lambda: self.f.f.original.current_path.write_bytes(b'{}'))

    def test_final_receiver_proof_change_refuses_return(self):
        self.receiver_observation(lambda: Path(self.f.held()['creation_run']['proofPath']).write_bytes(b'{}'))

    def test_final_receiver_process_exit_refuses_return(self):
        self.receiver_observation(lambda: setattr(self.f.creator.m.poll, 'return_value', 0))

    def test_final_copy_rejects_proof_mutation(self):
        copy = offline_control.deepcopy
        def changed(value, *args, **kwargs):
            result = copy(value, *args, **kwargs)
            if isinstance(value, dict) and value.get('status') == 'OFFLINE_START_RUNNING':
                Path(self.f.held()['creation_run']['proofPath']).write_bytes(b'{}')
            return result
        with patch.object(offline_control, 'deepcopy', side_effect=changed):
            with self.assertRaises(RegistryError):
                self.record()
        self.f.retained()

    def retained(self, committed):
        held = self.f.held()
        self.assertFalse(held['returned'])
        self.assertIs(held['creation'][1], self.f.creator.m)
        self.f.creator.m.close.assert_not_called()
        self.f.f.f.f.peer.close.assert_not_called()
        self.f.f.f.channel.close.assert_not_called()
        connection = held['accepted'][0]
        self.assertIs(connection, held['startup'][0]['connection'])
        self.assertIs(connection, held['startup'][0]['endpoint'].connection)
        self.assertEqual(self.f.proof_raw, held['proof'][0])
        self.assertIsNotNone(self.b.base.p.reader.request)
        self.assertIsNotNone(self.b.base.p.reader.ready_check_response)
        self.assertEqual('COMPLETED' if committed else 'PENDING', self.b.saved()[0]['status'])
        self.assertEqual(not committed, bool(self.b.saved()[1]))
        self.assertEqual(('RUNNING', 0) if committed else ('STARTING', 1),
                         (self.b.row()['state'], self.b.row()['pending']))
        effects = self.f.creator.generated, bytes(self.f.f.f.channel.sent), len(self.f.f.f.native.operations)
        with self.assertRaises(RegistryError):
            self.record()
        self.assertEqual(effects, (self.f.creator.generated, bytes(self.f.f.f.channel.sent),
                                   len(self.f.f.f.native.operations)))
        self.assertIs(held, self.f.held())

    def reject(self, committed=False):
        with self.assertRaises((RegistryError, OSError)):
            self.record()
        self.retained(committed)

    @contextmanager
    def boundary(self, phase, change):
        original = self.runtime._transaction
        fired = []
        @contextmanager
        def transaction(instance, *, read_only=False):
            held = self.f.creator.held() if self.runtime._offline.start_records else {}
            target = not read_only and 'completion' in held and not fired
            if target and phase == 'before':
                fired.append(True)
                change()
            with original(instance, read_only=read_only) as db:
                if target and phase == 'inside':
                    fired.append(True)
                    change()
                yield db
            if target and phase == 'after':
                fired.append(True)
                change()
        with patch.object(self.runtime, '_transaction', transaction):
            yield
        self.assertEqual([True], fired)

    def commit(self, sql, args=()):
        def write():
            with self.runtime.registry.transaction() as db:
                db.execute(sql, args)
        self.b.base.pool.submit(write).result(10)

    def test_epoch_commit_before_write_refuses_completion(self):
        with self.boundary('before', lambda: self.commit('UPDATE instances SET epoch=epoch+1 WHERE id=?',
                                                       (self.b.token['instanceId'],))):
            self.reject()

    def test_run_original_commit_before_write_refuses_completion(self):
        with self.boundary('before', lambda: self.commit("UPDATE runs SET record='{}' WHERE id=?",
                                                       (self.f.held()['run']['runId'],))):
            self.reject()

    def test_ports_commit_before_write_refuses_completion(self):
        with self.boundary('before', lambda: self.commit('DELETE FROM endpoint_reservations WHERE instance=?',
                                                       (self.b.token['instanceId'],))):
            self.reject()

    def test_approval_change_inside_write_rolls_back(self):
        with self.boundary('inside', lambda: self.b.approval.update(changed=True)):
            self.reject()

    def test_helper_change_inside_write_rolls_back(self):
        with self.boundary('inside', lambda: setattr(self.b.base.p.reader.process, 'handle', 98765)):
            self.reject()

    def test_public_renew_before_and_after_commit_preserves_binding(self):
        def renew(name):
            self.b.base.pool.submit(self.runtime.registry.execute, 'renew',
                dict(self.b.token, ttlSeconds=600), operation_id=name).result(10)
        with self.boundary('before', lambda: renew('running-renew-before')):
            _, packet = self.record()
        renew('running-renew-after')
        packet[3]()
        self.assertEqual(self.b.base.f.deadline, self.f.held()['deadline'])

    def test_commit_response_loss_preserves_terminal_and_resources(self):
        def lost():
            raise OSError('合成commit応答喪失')
        with self.boundary('after', lost):
            self.reject(True)
        self.assertFalse(self.f.held()['committed'])

    def test_deadline_before_write_starts_no_write(self):
        with self.boundary('before', lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline)):
            self.reject()

    def test_deadline_after_commit_refuses_return(self):
        with self.boundary('after', lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline)):
            self.reject(True)

    def test_epoch_commit_after_completion_refuses_return(self):
        with self.boundary('after', lambda: self.commit('UPDATE instances SET epoch=epoch+1 WHERE id=?',
                                                      (self.b.token['instanceId'],))):
            self.reject(True)

    def test_post_commit_current_change_refuses_return(self):
        with self.boundary('after', lambda: self.f.f.original.current_path.write_bytes(b'{}')):
            self.reject(True)

    def test_post_commit_proof_change_refuses_return(self):
        with self.boundary('after', lambda: Path(self.f.held()['creation_run']['proofPath']).write_bytes(b'{}')):
            self.reject(True)

    def test_post_commit_process_change_refuses_return(self):
        with self.boundary('after', lambda: setattr(self.f.creator.m.poll, 'return_value', 0)):
            self.reject(True)

    def test_post_commit_helper_change_refuses_return(self):
        with self.boundary('after', lambda: setattr(self.b.base.p.reader, 'ready_check_deadline', self.b.base.f.deadline+1)):
            self.reject(True)

    def test_atomic_write_failure_rolls_back_all_terminal_rows(self):
        with self.runtime.registry.transaction() as db:
            db.execute("CREATE TRIGGER fail_running BEFORE UPDATE OF state ON instances "
                       "WHEN NEW.state='RUNNING' BEGIN SELECT RAISE(ABORT,'合成write失敗'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.record()
        self.retained(False)
        self.assertEqual(('CLOSED', 1), tuple(self.f.saved()[1][k] for k in ('gate', 'revision')))

    def test_final_gate_has_no_io_after_last_file_bundle(self):
        state = dict(phase=None, events=[])
        previous = sys.getprofile()
        def profile(frame, event, value):
            name = frame.f_code.co_name
            if event == 'call' and name == 'verify_running':
                state['phase'] = 'external'
            if state['phase'] is not None and name == 'proof_files' and frame.f_locals.get('final'):
                if event == 'call':
                    state['phase'] = 'files'
                elif event == 'return':
                    state['phase'] = 'pure'
            if event == 'call' and state['phase'] is not None and name in (
                    'regular_bytes', 'read_bytes', 'read_text', 'transaction', 'identity', 'poll', 'sockets', 'approved'):
                self.assertNotEqual('pure', state['phase'], name)
                state['events'].append((state['phase'], name))
        sys.setprofile(profile)
        sockets = self.runtime.process_adapter.sockets
        def observed(pid):
            self.assertNotEqual('pure', state['phase'], 'socket観測')
            return sockets(pid)
        try:
            with patch.object(self.runtime.process_adapter, 'sockets', side_effect=observed):
                self.record()
        finally:
            sys.setprofile(previous)
        self.assertEqual('pure', state['phase'])
        self.assertTrue(state['events'])

    def final_socket(self, effect):
        observed = []
        original = self.runtime.process_adapter.sockets
        def changed(pid):
            result = original(pid)
            if self.f.held().get('committed') and not observed:
                observed.append(True)
                if effect == 'current':
                    self.f.f.original.current_path.write_bytes(b'{}')
                elif effect == 'proof':
                    Path(self.f.held()['creation_run']['proofPath']).write_bytes(b'{}')
                else:
                    self.f.creator.m.poll.return_value = 0
            return result
        with patch.object(self.runtime.process_adapter, 'sockets', side_effect=changed):
            self.reject(True)
        self.assertEqual([True], observed)

    def test_final_socket_current_change_refuses_return(self):
        self.final_socket('current')

    def test_final_socket_proof_change_refuses_return(self):
        self.final_socket('proof')

    def test_final_socket_process_exit_refuses_return(self):
        self.final_socket('process')

    def test_post_commit_packet_replacement_refuses_return(self):
        with self.boundary('after', lambda: self.f.held().update(completion=tuple(list(self.f.held()['completion'])))):
            self.reject(True)

    def test_completed_callback_rejects_gate_change(self):
        _, packet = self.record()
        self.commit('UPDATE run_admission SET revision=revision+1 WHERE run=?', (self.f.held()['run']['runId'],))
        with self.assertRaises(RegistryError):
            packet[3]()

    def test_missing_initializing_started_uses_same_packet_to_open(self):
        self.f.f.hook = lambda *args: None
        sleeps = []
        def sleep(delay):
            sleeps.append(delay)
            self.b.base.f.clock += delay
            if len(sleeps) == 1:
                def initial(value):
                    value.update(phase='INITIALIZING', startup={})
                with patch.object(self.f, 'change', initial):
                    self.f.publish_proof()
            else:
                self.f.publish_proof()
        with patch.object(offline_control.time, 'sleep', side_effect=sleep):
            result, packet = self.record()
        self.assertEqual([0.1, 0.1], sleeps)
        self.assertEqual('OFFLINE_START_RUNNING', result['status'])
        self.assertIs(self.f.held()['proof_initializing'], self.f.held()['proof'][7])
        self.assertEqual('INITIALIZING', json.loads(self.f.held()['proof_initializing'][0])['phase'])
        packet[3]()

    def test_terminal_write_uses_only_sql_and_memory(self):
        original = self.runtime._transaction
        entered = []
        @contextmanager
        def transaction(instance, *, read_only=False):
            held = self.f.creator.held() if self.runtime._offline.start_records else {}
            active = not read_only and 'completion' in held
            with original(instance, read_only=read_only) as db:
                if not active:
                    yield db
                    return
                entered.append(True)
                previous = sys.getprofile()
                def observe(frame, event, value):
                    if event == 'call':
                        self.assertNotIn(frame.f_code.co_name, ('regular_bytes', 'plain_path', 'lstat',
                            'read_bytes', 'read_text', 'approved', 'poll', 'identity', 'sockets'))
                sys.setprofile(observe)
                try:
                    yield db
                finally:
                    sys.setprofile(previous)
        with patch.object(self.runtime, '_transaction', transaction):
            self.record()
        self.assertEqual([True], entered)

    def write_deadline(self, boundary):
        original = self.runtime._transaction
        state = dict(active=False, expired=False, later=[])
        def expire():
            state['expired'] = True
            self.b.base.f.clock = self.b.base.f.deadline
        def profile(frame, event, value):
            if event != 'return' or not state['active'] or state['expired']:
                return
            if frame.f_back.f_code.co_name != 'record_start':
                return
            if frame.f_code.co_name == boundary:
                expire()
        class Connection:
            def __init__(self, db):
                self.db = db
            @property
            def in_transaction(self):
                return self.db.in_transaction
            def execute(self, sql, *args):
                if state['expired']:
                    state['later'].append(sql)
                value = self.db.execute(sql, *args)
                if boundary == 'delete' and sql.startswith('DELETE FROM reservations'):
                    expire()
                return value
        @contextmanager
        def transaction(instance, *, read_only=False):
            held = self.f.creator.held() if self.runtime._offline.start_records else {}
            target = not read_only and 'completion' in held
            with original(instance, read_only=read_only) as db:
                if not target:
                    yield db
                    return
                state['active'] = True
                previous = sys.getprofile()
                sys.setprofile(profile)
                try:
                    yield Connection(db)
                finally:
                    sys.setprofile(previous)
                    state['active'] = False
        with patch.object(self.runtime, '_transaction', transaction):
            self.reject()
        self.assertTrue(state['expired'])
        self.assertEqual([], state['later'])

    def test_saved_deadline_starts_no_frame_readback(self):
        self.write_deadline('saved')

    def test_grant_readback_deadline_starts_no_ack_readback(self):
        self.write_deadline('readback')

    def test_artifact_delete_deadline_starts_no_post_state_read(self):
        self.write_deadline('delete')
