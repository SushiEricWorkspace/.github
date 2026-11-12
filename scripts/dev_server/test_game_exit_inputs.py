"""固定Dataの不変候補と保存原文の相関。保存/正規化/退出算法は合成しない。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from . import mod_control_runtime as target
from . import test_managed_save_evidence as saves
from . import runtime_control, supervisor
from .registry import RegistryError


def originals(session):
    message = target.wire.decode_frame_v3(session['save_request'])
    requested = target.wire._inner(message['payload'], 'requestJson', 'requestSha256')['status']
    previous = requested['state']
    completion = target.wire.decode_frame_v3(session['completion_response'])['payload']['completion']
    proof = dict(sourceStateSha256=requested['sha256'], worldUuid=previous['worldUuid'],
                 runUuid=previous['runUuid'], stateRevision=previous['stateRevision'],
                 owner=deepcopy(previous['payload']['owner']),
                 evidence=dict(worldSaveSha256='a' * 64, playerSaveSha256='b' * 64,
                               requiredWriterExits=[deepcopy(w['identity']) for w in completion['writers']]),
                 sealSha256='e' * 64, managementCompletion=deepcopy(completion))
    state = deepcopy(previous)
    state.update(stateRevision=previous['stateRevision'] + 1, previousStateSha256=requested['sha256'])
    state['payload'] = dict(type='io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldRunPhase.SavedPendingExit',
                            owner=deepcopy(previous['payload']['owner']),
                            management=deepcopy(previous['payload']['management']))
    return state, proof


def encode_original(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


class GameExitInputsTests(unittest.TestCase):
    def setUp(self):
        self.base = saves.SaveEvidenceTests()
        self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.base.serve(); self.assertIsNone(self.base.session['error'])
        self.f, self.session, self.managed = self.base.f, self.base.session, self.base.managed
        self.state, self.proof = originals(self.session)
        self.files = {}; self.reads = []
        self.directory = Path(target.wire.decode_descriptor(self.session['raw'])['worldRoot']) / 'sushieric/run-state'
        self.publish()
        self.after_read = lambda path: None
        self.enterContext(patch.object(target, 'regular_bytes', side_effect=self.read))
        self.paths = self.enterContext(patch.object(target, 'plain_path'))
        self.process = self.f.runtime.game_processes[self.session['run']][0]

    def publish(self):
        self.proof_raw = encode_original(self.proof)
        self.state['payload']['saveProofSha256'] = target.digest(self.proof_raw)
        self.state_raw = encode_original(self.state)
        self.candidate = {k: self.state[k] for k in ('worldUuid','runUuid','stateRevision')}
        self.candidate['stateSha256'] = target.digest(self.state_raw)
        self.state_path = self.directory / 'states' / (self.candidate['stateSha256'] + '.json')
        self.proof_path = self.directory / 'saveproofs' / (self.state['payload']['saveProofSha256'] + '.json')
        self.files = {self.state_path: self.state_raw, self.proof_path: self.proof_raw}

    def read(self, path):
        self.reads.append(path)
        result = self.files[path]
        self.after_read(path)
        return result

    def inputs(self):
        return self.managed.game_exit_inputs('slot-a', self.session, self.candidate, time.monotonic() + 10)

    def effects(self):
        return self.base.unchanged_effects(), self.base.parent(), dict(self.f.runtime.game_processes)

    def rejected(self, change, code='PROOF_MISMATCH'):
        change()
        before = self.effects()
        with self.assertRaises((RegistryError, ValueError)) as caught: self.inputs()
        if isinstance(caught.exception, RegistryError): self.assertEqual(code, caught.exception.code)
        self.assertEqual(before, self.effects())
        self.process.poll.assert_not_called(); self.process.members.assert_not_called(); self.process.close.assert_not_called()
        self.f.runtime.process_adapter.identity.assert_not_called()

    def test_original_nonempty_frames_and_bytes_supply_fixed_candidate_fields(self):
        original = self.session['save_request'], self.session['completion_response'], self.session['raw']
        expected = deepcopy(self.candidate)
        before = self.effects()
        frame, state, proof = self.inputs()
        self.assertEqual((self.state_raw, self.proof_raw), (state, proof))
        self.assertEqual(self.candidate, expected)
        self.assertEqual(original, (self.session['save_request'], self.session['completion_response'], self.session['raw']))
        self.assertTrue(json.loads(original[0][4:])['payload']['requestJson'])
        management = self.state['payload']['management']
        self.assertEqual({k:management[k] for k in ('leaseId','epoch','generation','drainSha256')},
                         {k:frame[k] for k in ('leaseId','epoch','generation','drainSha256')})
        self.assertEqual(self.state['payload']['owner'], frame['gameOwner'])
        self.assertEqual((expected['stateSha256'], expected['stateRevision']), (frame['expectedStateSha256'], frame['expectedRevision']))
        self.assertEqual(self.proof['sourceStateSha256'], frame['requestedStateSha256'])
        self.assertEqual(target.digest(proof), frame['saveProofSha256'])
        self.assertNotIn('ready', frame); self.assertNotIn('controller', frame); self.assertNotIn('owner', frame)
        self.assertEqual([self.state_path, self.proof_path], self.reads)
        self.assertEqual(self.reads, [c.args[0] for c in self.paths.call_args_list])
        self.assertEqual(before, self.effects())
        frame['gameOwner']['pid'] += 1
        self.assertEqual(self.state['payload']['owner'], self.inputs()[0]['gameOwner'])

    def test_current_and_ready_are_not_used_as_candidate(self):
        self.files[self.directory / 'current.json'] = b'{"phase":"Ready"}'
        self.assertEqual(self.state_raw, self.inputs()[1])
        self.assertNotIn(self.directory / 'current.json', self.reads)

    def test_state_raw_hash_mismatch(self):
        self.rejected(lambda: self.files.update({self.state_path: self.state_raw + b' '}))

    def test_proof_raw_hash_mismatch(self):
        self.rejected(lambda: self.files.update({self.proof_path: self.proof_raw + b' '}))

    def test_missing_original_does_not_fall_back_to_current(self):
        self.files[self.directory / 'current.json'] = self.state_raw
        self.files.pop(self.state_path)
        before = self.effects()
        with self.assertRaises(KeyError): self.inputs()
        self.assertEqual([self.state_path], self.reads)
        self.assertEqual(before, self.effects())

    def test_unrecorded_reference_rejects_before_file_read(self):
        def change(): self.session['save_evidence'] = dict(self.session['save_evidence'], requestBodySha256='0' * 64)
        self.rejected(change, 'STALE_OPERATION')
        self.assertEqual([], self.reads)

    def test_unfinished_session_rejects_before_file_read(self):
        self.rejected(lambda: self.session['done'].clear(), 'CONTROL_UNKNOWN')
        self.assertEqual([], self.reads)

    def test_unknown_session_rejects_before_file_read(self):
        self.rejected(lambda: self.session.update(error=EOFError('fixture 原文終端不明')), 'CONTROL_UNKNOWN')
        self.assertEqual([], self.reads)

    def test_final_source_change_rejects_without_effect(self):
        def change(path):
            if path == self.proof_path:
                process, raw = self.f.runtime.game_processes[self.session['run']]
                snapshot = json.loads(raw); snapshot['owner']['startId'] += '-changed'
                self.f.runtime.game_processes[self.session['run']] = process, target.canonical(snapshot)
        self.after_read = change
        before = self.base.unchanged_effects(), self.base.parent()
        with self.assertRaises(RegistryError) as caught: self.inputs()
        self.assertEqual('PROCESS_MISMATCH', caught.exception.code)
        self.assertEqual(before, (self.base.unchanged_effects(), self.base.parent()))

    def test_final_deadline_crossing_preserves_originals(self):
        clock = [0]
        original = self.managed.save_evidence
        calls = []
        def checked(*args):
            result = original(*args); calls.append(1)
            if len(calls) == 2: clock[0] = 10
            return result
        self.session['stop_deadline'] = 10
        before = self.effects()
        with patch.object(target.time, 'monotonic', lambda: clock[0]), patch.object(self.managed, 'save_evidence', side_effect=checked):
            with self.assertRaises(RegistryError) as caught: self.inputs()
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.assertEqual(before, self.effects())


    def test_expired_deadline_rejects_before_file_read(self):
        self.rejected(lambda: self.session.update(stop_deadline=0), 'STOP_TIMEOUT')
        self.assertEqual([], self.reads)

    def test_wrong_phase_rejects_with_matching_outer_hash(self):
        def change():
            self.state['payload']['type'] = 'io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldRunPhase.Running'
            self.publish()
        self.rejected(change)

    def test_session_becomes_unknown_during_read(self):
        self.after_read = lambda path: self.session.update(error=EOFError('fixture 原文読取り中の不明'))
        self.rejected(lambda: None, 'STALE_OPERATION')



def state_case(key, value):
    def test(self):
        def change():
            self.state[key] = value(self) if callable(value) else value
            self.publish()
        self.rejected(change)
    return test


for key, value in (
    ('worldUuid', '00000000-0000-0000-0000-000000000099'),
    ('runUuid', '00000000-0000-0000-0000-000000000099'), ('generation', 'different'),
    ('stateRevision', lambda s: s.state['stateRevision'] + 1), ('previousStateSha256', '0' * 64),
    ('paths', lambda s: dict(s.state['paths'], worldRoot=str(Path(s.state['paths']['worldRoot']) / 'other')))):
    setattr(GameExitInputsTests, 'test_state_' + key + '_mismatch_with_matching_outer_hash', state_case(key,value))


def actor_case(kind, field, value):
    def test(self):
        def change():
            self.state['payload'][kind][field] = value
            self.publish()
        self.rejected(change)
    return test


for kind, values in (
    ('owner', (('pid', 99999), ('startId', 'other'), ('hostHash', '0' * 64), ('bootId', 'other'))),
    ('management', (('leaseId','other'), ('epoch',99), ('generation','other'),
                    ('drainSha256','0' * 64),))):
    for field,value in values:
        setattr(GameExitInputsTests, 'test_state_' + kind + '_' + field + '_mismatch', actor_case(kind,field,value))


def controller_case(field, value):
    def test(self):
        def change():
            self.state['payload']['management']['controller'][field] = value
            self.publish()
        self.rejected(change)
    return test


for field,value in (('pid',99999),('startId','other'),('hostHash','0' * 64),('bootId','other')):
    setattr(GameExitInputsTests,'test_state_controller_'+field+'_mismatch',controller_case(field,value))


def expected_case(field, value):
    def test(self): self.rejected(lambda: self.candidate.update({field:value}))
    return test


for field,value in (('worldUuid','00000000-0000-0000-0000-000000000099'),
                    ('runUuid','00000000-0000-0000-0000-000000000099'),('stateRevision',99)):
    setattr(GameExitInputsTests,'test_expected_'+field+'_mismatch',expected_case(field,value))


def proof_case(key, value):
    def test(self):
        def change():
            self.proof[key] = value(self) if callable(value) else value
            self.publish()
        self.rejected(change)
    return test


for key,value in (('sourceStateSha256','0' * 64), ('worldUuid','00000000-0000-0000-0000-000000000099'),
                  ('runUuid','00000000-0000-0000-0000-000000000099'), ('stateRevision',99),
                  ('managementCompletion',lambda s:dict(s.proof['managementCompletion'],registryRevision=99))):
    setattr(GameExitInputsTests, 'test_proof_' + key + '_mismatch_with_matching_outer_hash', proof_case(key,value))


def proof_owner_case(field, value):
    def test(self):
        def change():
            self.proof['owner'][field] = value
            self.publish()
        self.rejected(change)
    return test


for field,value in (('pid',99999),('startId','other'),('hostHash','0' * 64),('bootId','other')):
    setattr(GameExitInputsTests,'test_proof_owner_'+field+'_mismatch',proof_owner_case(field,value))


class GameExitInputsRegistryTests(unittest.TestCase):
    def setUp(self):
        self.case = saves.SaveEvidenceRegistryTests()
        self.case.setUp(); self.addCleanup(self.case.doCleanups)
        self.base, self.runtime, self.f = self.case.base, self.case.runtime, self.case.f
        self.enterContext(patch.object(self.runtime, '_direct_stop', Mock()))
        self.session = self.base.start()
        self.base.stop_ready.set(); self.base.save_ready.set()
        self.case.join(self.session)
        self.assertIsNone(self.session['error']); self.assertTrue(self.session['done'].is_set())
        state, proof = originals(self.session)
        proof_raw = encode_original(proof); state['payload']['saveProofSha256'] = target.digest(proof_raw)
        state_raw = encode_original(state)
        self.candidate = {k:state[k] for k in ('worldUuid','runUuid','stateRevision')}
        self.candidate['stateSha256'] = target.digest(state_raw)
        world = Path(target.wire.decode_descriptor(self.session['raw'])['worldRoot'])
        self.assertTrue(world.is_relative_to(self.f.registry.root))
        directory = world / 'sushieric/run-state'
        self.state_path = directory / 'states' / (self.candidate['stateSha256'] + '.json')
        self.proof_path = directory / 'saveproofs' / (state['payload']['saveProofSha256'] + '.json')
        for p, raw in ((self.state_path,state_raw),(self.proof_path,proof_raw)):
            p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(raw)
        self.raw = state_raw, proof_raw

    def inputs(self):
        return self.base.managed.game_exit_inputs(self.case.instance, self.session, self.candidate, time.monotonic()+10)

    def overlapping_start(self, *, change=False):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original_read, original_lock = target.regular_bytes, supervisor.private_lock
        start_thread = []
        def read(path):
            raw = original_read(path)
            if path == self.proof_path:
                entered.set()
                if not release.wait(10): raise TimeoutError('試験の候補原本読取り')
            return raw
        def locking(path):
            if start_thread and threading.get_ident() == start_thread[0]: attempted.set()
            return original_lock(path)
        def start():
            start_thread.append(threading.get_ident())
            with self.runtime._lock_wait(time.monotonic()+10): return self.f.start('candidate-competing-start')
        before = self.case.query('SELECT * FROM reservations ORDER BY operation,reference')
        source = self.session['save_request'], self.session['completion_response']
        with patch.object(target,'regular_bytes',side_effect=read), patch.object(supervisor,'private_lock',side_effect=locking), ThreadPoolExecutor(max_workers=2) as pool:
            observed = pool.submit(self.inputs)
            competing = None
            try:
                self.assertTrue(entered.wait(10))
                competing = pool.submit(start); self.assertTrue(attempted.wait(10))
                with self.assertRaises(RegistryError): competing.result(10)
                if change == 'reference':
                    with self.runtime._transaction(self.case.instance) as connection:
                        row=connection.execute('SELECT response FROM runtime_operations WHERE id=?',(self.session['stop_operation'],)).fetchone()
                        value=json.loads(row[0]);value['managedSave']['responseBodySha256']='0' * 64
                        connection.execute('UPDATE runtime_operations SET response=? WHERE id=?',(json.dumps(value),self.session['stop_operation']))
                elif change:
                    process, raw = self.runtime.game_processes[self.session['run']]
                    snapshot=json.loads(raw); snapshot['owner']['startId'] += '-changed'
                    self.runtime.game_processes[self.session['run']] = process,target.canonical(snapshot)
            finally: release.set()
            if change:
                with self.assertRaises(RegistryError) as caught: observed.result(10)
                self.assertEqual('STALE_OPERATION' if change == 'reference' else 'PROCESS_MISMATCH',caught.exception.code)
            else: self.assertEqual(self.raw, observed.result(10)[1:])
            self.assertIsNotNone(competing)
        self.assertEqual(before,self.case.query('SELECT * FROM reservations ORDER BY operation,reference'))
        self.assertEqual(source,(self.session['save_request'],self.session['completion_response']))
        self.assertEqual(1,len(self.f.adapter.created));self.assertEqual(0,self.f.rcon_calls)
        self.assertEqual('STOPPING',self.f.state()['state']);self.assertEqual(1,self.f.state()['pending'])
        parent=self.case.query('SELECT * FROM runtime_operations WHERE id=?',(self.session['stop_operation'],))[0]
        self.assertEqual('PENDING',parent['status'])
        expected=dict(self.session['save_evidence'])
        if change == 'reference': expected['responseBodySha256']='0' * 64
        self.assertEqual(expected,json.loads(parent['response'])['managedSave'])
        self.assertFalse(self.runtime.game_processes[self.session['run']][0].closed)
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)

    def test_original_transaction_read_preserves_reservation_and_rejects_start(self):
        self.overlapping_start()

    def test_held_binding_change_during_read_rejects_and_preserves_pending(self):
        self.overlapping_start(change=True)


    def test_reference_change_during_read_rejects_and_preserves_changed_record(self):
        self.overlapping_start(change='reference')
