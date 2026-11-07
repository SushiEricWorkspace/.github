"""game退出CのOS/IPCだけを合成し、Data保存/hash算法は実装しない。"""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import offline_process as target
from . import test_offline_process as fixture
from .registry import RegistryError


class GamePipe(fixture.Pipe):
    def send(self, request, deadline, verify):
        verify()
        self.sent.append(deepcopy(request))
        if self.current.failure == 'partial':
            raise TimeoutError('fixture 送信途中')
        assert self.current.data_phase == 'SavedPendingExit'
        self.effects += 1
        self.current.data_phase = 'Ready'
        self.current.c.process.code = self.current.observer_code
        self.current.after_send()

    def receive(self, deadline, verify):
        verify()
        if self.current.failure == 'lost':
            raise EOFError('fixture Ready公開後の応答喪失')
        request = self.sent[-1]
        response = {k: request[k] for k in ('operationId','requestHash','sessionId','nonce','saveProofSha256')}
        response.update(status='GAME_EXITED', candidateStateSha256=request['expectedStateSha256'],
                        **self.current.ready)
        if self.current.response_change:
            key, value = self.current.response_change
            if key == 'missing': response.pop(value)
            else: response[key] = value
        self.current.after_receive()
        return response


class OfflineGameExitProcessTests(unittest.TestCase):
    helper = fixture.OfflineProcessTests.helper

    def setUp(self):
        fixture.OfflineProcessTests.setUp(self)
        self.clock = 0
        self.enterContext(patch.object(target.time, 'monotonic', lambda: self.clock))
        self.enterContext(patch.object(target.time, 'sleep', side_effect=AssertionError('実sleep禁止')))
        GamePipe.current = self
        self.enterContext(patch.object(target, 'ProxyPipe', GamePipe))
        self.bindings = dict(lease='lease', epoch=1, generation='generation', reservation='original',
                             gate='BLOCKED', drain='COMPLETE', writers='CLOSED', requested='b' * 64,
                             completion='e' * 64, candidate='c' * 64)
        self.expected_bindings = deepcopy(self.bindings)
        self.m = SimpleNamespace(pid=90000, group='original-m-group', code=0, extra=[], polls=0, member_calls=0)
        self.m_identity = dict(self.adapter.parent, pid=self.m.pid, startId='m-start')
        self.m.poll = self.m_poll
        self.m.members = self.m_members
        self.identity_calls = []
        original_identity = self.adapter.identity
        def identity(pid):
            self.identity_calls.append(pid)
            if hasattr(self, 'c') and pid == self.c.process.pid and self.c.process.code == 0:
                raise AssertionError('終了後Cの旧PID再解決禁止')
            return deepcopy(self.m_identity) if pid == self.m.pid else original_identity(pid)
        self.adapter.identity = identity
        self.c = self.helper()
        self.c.process.members = lambda: ([self.c.process.pid] if self.c.process.code is None else []) + self.c.process.extra
        self.observer_code = 0
        self.after_receive = lambda: None
        self.data_phase = 'SavedPendingExit'
        self.game = dict(hostHash='a' * 64, bootId='boot', pid=self.m.pid, startId='m-start')
        self.input = fixture.frame()
        self.input.update(worldUuid=fixture.WORLD, runUuid=fixture.RUN, expectedRevision=3,
                          expectedStateSha256='c' * 64, gameOwner=self.game,
                          saveProofSha256='f' * 64, requestedStateSha256='b' * 64)
        self.ready = dict(worldUuid=fixture.WORLD, runUuid=fixture.RUN, stateRevision=4, stateSha256='d' * 64)

    def verify(self):
        if not self.valid or self.bindings != self.expected_bindings:
            raise RegistryError('STALE_OPERATION', 'fixture 元正本/認証済保存要求/全writer不一致')

    def m_poll(self):
        self.m.polls += 1
        return self.m.code

    def m_members(self):
        self.m.member_calls += 1
        return ([self.m.pid] if self.m.code is None else []) + self.m.extra

    def observe(self):
        return self.c.observe_game_exit(self.input, game_process=self.m, game_identity=self.m_identity,
                                        game_group='original-m-group', deadline=10)

    def effects(self):
        return (sum(p.starts for p in self.adapter.processes), len(self.pipes),
                sum(len(p.sent) for p in self.pipes), sum(p.effects for p in self.pipes))

    def no_retry(self):
        before = self.effects(), self.m.polls, self.m.member_calls
        with self.assertRaises(RegistryError): self.observe()
        with self.assertRaises(RegistryError): self.c.start(fixture.frame(), 10)
        self.assertEqual(before, (self.effects(), self.m.polls, self.m.member_calls))

    def unknown(self):
        self.assertIsNone(self.c.game_exit_result)
        self.assertFalse(self.c.ready)
        self.no_retry()

    def test_normal_exit_transparently_delegates_original_candidate_and_observes_ready(self):
        original = deepcopy(self.input)
        result = self.observe()
        request = self.pipes[0].sent[0]
        self.assertEqual('observe-game-exit', request['command'])
        self.assertEqual(original, {k:request[k] for k in original})
        self.assertEqual(self.c.parent_data, request['controller'])
        self.assertEqual(self.c.data_identity, request['owner'])
        self.assertEqual((1, 1, 1, 1), self.effects())
        self.assertEqual('Ready', self.data_phase)
        self.assertEqual(self.ready, result['ready'])
        self.assertEqual(self.game, result['gameOwner'])
        self.assertTrue(result['allChildrenExited'])
        self.assertEqual(self.c.process.group, result['observerGroup'])
        self.assertIsNotNone(self.c.game_exit_result)
        self.assertEqual(self.input, original)
        result['ready']['stateRevision'] = 100
        self.assertEqual(4, json.loads(self.c.game_exit_result)['ready']['stateRevision'])
        records = list(self.c.diagnostics.glob('creation-*.json'))
        journal = ''.join(p.read_text(encoding='utf-8') for p in records)
        for secret in (self.c.session, self.c.nonce): self.assertNotIn(secret, journal)
        self.assertNotIn('worldRoot', journal)
        self.assertNotIn(self.m.pid, self.identity_calls)
        self.assertGreaterEqual(self.identity_calls.count(self.c.process.pid), 2)
        self.no_retry()

    def test_live_observer_is_verified_until_its_normal_exit(self):
        self.observer_code = None
        def wait(_): self.c.process.code = 0
        with patch.object(target.time, 'sleep', side_effect=wait): self.observe()
        self.assertIsNotNone(self.c.game_exit_result)
        self.assertGreater(self.identity_calls.count(self.c.process.pid), 2)

    def test_m_and_children_are_waited_for_without_treating_intent_as_exit(self):
        self.m.code = None
        waits = []
        def wait(_):
            waits.append(1)
            if len(waits) == 1: self.m.code, self.m.extra = 0, [90001]
            else: self.m.extra = []
        with patch.object(target.time, 'sleep', side_effect=wait): self.observe()
        self.assertEqual(2, len(waits))
        self.assertIn(self.m.pid, self.identity_calls)
        self.assertIsNotNone(self.c.game_exit_result)

    def test_observer_child_survives_root_exit_until_group_empty(self):
        self.after_send = lambda: self.c.process.extra.append(123456)
        seen = []
        def wait(_):
            seen.append(self.c.game_exit_result)
            self.c.process.extra = []
        with patch.object(target.time, 'sleep', side_effect=wait): self.observe()
        self.assertEqual([None], seen)

    def test_each_authority_binding_rejects_before_generation_or_data_effect(self):
        for key in self.bindings:
            with self.subTest(key=key):
                self.bindings = deepcopy(self.expected_bindings)
                self.bindings[key] = 'different'
                self.c = self.helper()
                with self.assertRaises(RegistryError): self.observe()
                self.assertEqual((0, 0, 0, 0), self.effects())
                self.unknown()

    def test_each_original_m_identity_field_is_checked_before_effect(self):
        for key, value in [('pid', 90001), ('startId', 'different'), ('hostHash', 'e'*64), ('bootId', 'different')]:
            with self.subTest(key=key):
                self.c = self.helper()
                self.input['gameOwner'] = dict(self.game, **{key:value})
                with self.assertRaises(RegistryError): self.observe()
                self.assertEqual((0, 0, 0, 0), self.effects())
                self.unknown()

    def test_live_m_start_change_is_rejected_without_rebinding_pid(self):
        self.m.code = None
        self.game['startId'] = self.input['gameOwner']['startId'] = 'different'
        self.m_identity['startId'] = 'different'
        original = self.adapter.identity
        self.adapter.identity = lambda pid: dict(original(pid), startId='reused') if pid == self.m.pid else original(pid)
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((0, 0, 0, 0), self.effects())
        self.unknown()

    def test_original_group_change_rejects_before_effect(self):
        self.m.group = 'different'
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((0, 0, 0, 0), self.effects())
        self.unknown()

    def test_m_nonzero_or_invalid_exit_rejects_before_effect(self):
        for code in (1, -1, False, '0'):
            with self.subTest(code=code):
                self.c = self.helper(); self.m.code = code
                with self.assertRaises(RegistryError): self.observe()
                self.assertEqual((0, 0, 0, 0), self.effects())
                self.unknown()

    def test_ready_observation_does_not_open_an_owner_or_advance_pipe(self):
        result = self.observe()
        self.assertEqual(self.ready, result['ready'])
        self.assertFalse(self.c.ready)
        self.assertEqual(['observe-game-exit'], [frame['command'] for frame in self.pipes[0].sent])
        self.no_retry()

    def test_actual_ready_hash_is_observed_without_precomputing_it(self):
        self.ready['stateSha256'] = 'e' * 64
        result = self.observe()
        self.assertEqual('e' * 64, result['ready']['stateSha256'])
        self.assertEqual('c' * 64, self.pipes[0].sent[0]['expectedStateSha256'])
        self.assertFalse(self.c.ready)
        self.no_retry()

    def test_partial_send_preserves_intent_without_data_effect(self):
        self.failure = 'partial'
        with self.assertRaises(TimeoutError): self.observe()
        self.assertEqual((1,1,1,0), self.effects())
        self.unknown()

    def test_response_loss_after_ready_preserves_original_response_intent(self):
        self.failure = 'lost'
        with self.assertRaises(EOFError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.assertEqual('Ready', self.data_phase)
        self.assertIsNotNone(self.c.request)
        self.unknown()

    def test_observer_nonzero_exit_after_effect_does_not_record_success(self):
        self.observer_code = 7
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.assertNotIn('gameExit', self.c.record)
        self.unknown()

    def deadline_failure(self):
        with self.assertRaises(RegistryError) as error: self.observe()
        self.assertEqual('STOP_TIMEOUT', error.exception.code)
        if self.c.record: self.assertNotIn('gameExit', self.c.record)
        self.unknown()

    def test_m_members_crossing_deadline_does_not_create_observer(self):
        def members(): self.clock = 11; return []
        self.m.members = members
        self.deadline_failure()
        self.assertEqual((0,0,0,0), self.effects())

    def test_m_poll_crossing_deadline_does_not_create_observer(self):
        def poll(): self.clock = 11; return 0
        self.m.poll = poll
        self.deadline_failure()
        self.assertEqual((0,0,0,0), self.effects())

    def test_response_completion_crossing_deadline_preserves_ready_without_success(self):
        self.after_receive = lambda: setattr(self, 'clock', 11)
        self.deadline_failure()
        self.assertEqual((1,1,1,1), self.effects())
        self.assertIsNotNone(self.c.game_exit_response)

    def test_observer_members_crossing_deadline_does_not_record_success(self):
        normal = self.c.process.members
        def members():
            if self.c.process.code == 0: self.clock = 11; return []
            return normal()
        self.c.process.members = members
        self.deadline_failure()
        self.assertEqual((1,1,1,1), self.effects())

    def test_last_authority_check_crossing_deadline_does_not_record_success(self):
        original = self.c.verify_owner
        def verify():
            original()
            if self.c.game_exit_response is not None and self.m.member_calls >= 1:
                self.clock = 11
        self.c.verify_owner = verify
        self.deadline_failure()

    def test_unknown_m_group_does_not_prove_exit(self):
        self.m.members = lambda: None
        with patch.object(target.time, 'sleep', side_effect=lambda _:setattr(self,'clock',11)):
            self.deadline_failure()
        self.assertEqual((0,0,0,0), self.effects())

    def test_observer_child_timeout_keeps_ready_and_no_success(self):
        self.after_send = lambda:self.c.process.extra.append(123456)
        with patch.object(target.time, 'sleep', side_effect=lambda _:setattr(self,'clock',11)):
            self.deadline_failure()
        self.assertEqual((1,1,1,1), self.effects())
        self.assertEqual('Ready', self.data_phase)

    def test_group_change_after_response_preserves_candidate_and_ready(self):
        self.after_receive = lambda:setattr(self.c.process,'group','different')
        with self.assertRaises(RegistryError): self.observe()
        self.assertNotIn('gameExit', self.c.record)
        self.unknown()

    def test_authority_change_after_effect_does_not_advance(self):
        self.after_send = lambda:self.bindings.update(reservation='different')
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.unknown()

    def test_local_result_write_failure_keeps_intent_and_no_retry(self):
        original = self.c._record
        def record():
            if 'gameExit' in self.c.record: raise OSError('fixture 記録失敗')
            original()
        self.c._record = record
        with self.assertRaises(OSError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.unknown()


    def test_unknown_observer_group_does_not_prove_exit(self):
        original = self.c.process.members
        self.c.process.members = lambda: None if self.c.process.code == 0 else original()
        with patch.object(target.time, 'sleep', side_effect=lambda _:setattr(self,'clock',11)):
            self.deadline_failure()
        self.assertEqual((1,1,1,1), self.effects())

    def test_shared_original_m_and_observer_group_rejects_before_send(self):
        original = self.c.process.start
        def start(arguments, cwd, log, name, created, **stdio):
            original(arguments, cwd, log, self.m.group, created, **stdio)
        self.c.process.start = start
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((1,1,0,0), self.effects())
        self.unknown()

    def test_parent_boot_change_after_effect_prevents_result(self):
        self.after_send = lambda:setattr(self.native, 'boot', 'different')
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.unknown()

    def test_final_record_completion_crossing_deadline_is_not_accepted(self):
        original = self.c._record
        def record():
            original()
            if 'gameExit' in self.c.record: self.clock = 11
        self.c._record = record
        with self.assertRaises(RegistryError) as error: self.observe()
        self.assertEqual('STOP_TIMEOUT', error.exception.code)
        self.assertIn('gameExit', self.c.record)
        self.unknown()

    def test_real_private_pipe_keeps_request_and_response_buffers_without_ack(self):
        import os
        import struct
        from .database_proxy_pipe import ProxyPipe
        from .local_ipc import encode
        case = self
        class RealPipe(ProxyPipe):
            def __init__(pipe):
                super().__init__()
                pipe.sent, pipe.effects = [], 0
                pipe.reader, pipe.writer = os.dup(pipe.child_input), os.dup(pipe.child_output)
                case.addCleanup(os.close, pipe.reader)
                case.addCleanup(os.close, pipe.writer)
                case.addCleanup(pipe.close_after_exit)
                case.pipes.append(pipe)
            def send(pipe, request, deadline, verify):
                super().send(request, deadline, verify)
                raw = os.read(pipe.reader, 65536)
                case.assertTrue(raw)
                pipe.sent.append(deepcopy(request)); pipe.effects += 1
                case.data_phase = 'Ready'
                response = {k:request[k] for k in ('operationId','requestHash','sessionId','nonce','saveProofSha256')}
                response.update(status='GAME_EXITED', candidateStateSha256=request['expectedStateSha256'],
                                **case.ready)
                body = encode(response)
                os.write(pipe.writer, struct.pack('!I',len(body)) + body)
                case.c.process.code = 0
        with patch.object(target, 'ProxyPipe', RealPipe): self.observe()
        self.assertEqual('Ready', self.data_phase)
        self.assertIsNotNone(self.c.pipe.outgoing)
        self.assertTrue(self.c.pipe.incoming)
        self.assertIsNotNone(self.c.pipe.input)
        self.no_retry()


    def test_observer_reusing_exited_m_pid_keeps_distinct_start_identity(self):
        self.m.pid = 100000
        self.m_identity['pid'] = self.game['pid'] = self.m.pid
        self.adapter.identity = lambda pid: fixture.Adapter.identity(self.adapter, pid)
        result = self.observe()
        self.assertEqual(result['gameOwner']['pid'], result['observer']['pid'])
        self.assertNotEqual(result['gameOwner']['startId'], result['observer']['startId'])
        self.no_retry()

    def test_same_parent_identity_cannot_be_original_m(self):
        import os
        self.m.pid = os.getpid()
        self.m_identity = deepcopy(self.adapter.parent)
        self.input['gameOwner'] = dict(hostHash='a'*64, bootId='boot', pid=self.m.pid, startId='parent-start')
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((0,0,0,0), self.effects())
        self.unknown()

    def test_member_query_failure_keeps_original_handle_without_generation(self):
        def members(): raise OSError('fixture group観測不明')
        self.m.members = members
        with self.assertRaises(OSError): self.observe()
        self.assertEqual((0,0,0,0), self.effects())
        self.unknown()

    def test_post_effect_failure_keeps_ready_without_extra_action(self):
        def fail(): raise OSError('fixture Data作用後失敗')
        self.after_send = fail
        with self.assertRaises(OSError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.assertEqual('Ready', self.data_phase)
        self.unknown()



    def test_native_observer_identity_is_pinned_after_exit(self):
        self.after_receive = lambda:self.c.identity.update(startId='different')
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.assertNotIn('gameExit', self.c.record)
        self.unknown()

    def test_identity_change_during_final_record_is_not_accepted(self):
        original = self.c._record
        def record():
            original()
            if 'gameExit' in self.c.record: self.c.data_identity['hostHash'] = 'e' * 64
        self.c._record = record
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.assertIn('gameExit', self.c.record)
        self.unknown()


    def exit_race(self, role, stage, code=0, mismatch=False):
        process = self.m if role == 'm' else self.c.process
        process.code = None
        self.observer_code = None if role == 'c' else 0
        armed = role == 'm'
        fired = False
        queries = 0
        original_identity, original_poll, original_members = self.adapter.identity, process.poll, process.members
        def arm():
            nonlocal armed
            armed = True
        if role == 'c': self.after_send = arm
        def poll():
            nonlocal fired
            if armed and not fired and stage == 'poll':
                fired = True; process.code = code
                return None
            return original_poll()
        def identity(pid):
            nonlocal fired, queries
            if pid != process.pid or not armed: return original_identity(pid)
            queries += 1
            if stage == 'poll' and fired:
                return None
            boundary = 1 if stage == 'native' else 2 if stage == 'data' else None
            if boundary is not None and queries == boundary:
                fired = True
                if mismatch:
                    actual = original_identity(pid)
                    process.code = code
                    return dict(actual, startId='conflicting-start')
                process.code = code
                return None
            self.assertFalse(fired, '正常終了確認後の旧PID再照会')
            return original_identity(pid)
        def members():
            nonlocal fired
            if armed and not fired and stage == 'group':
                fired = True; process.code = code
                return []
            return original_members()
        process.poll, process.members, self.adapter.identity = poll, members, identity
        return lambda:(fired,queries)

    def normal_race(self):
        try: return self.observe()
        except RegistryError as error: self.fail('正常終了との競合を誤拒否: ' + error.code)


def _observer_identity_case(key, value):
    def test(self):
        self.after_receive = lambda:self.c.data_identity.update({key:value})
        with self.assertRaises(RegistryError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.assertNotIn('gameExit', self.c.record)
        self.unknown()
    return test


for _key, _value in [('hostHash','e' * 64), ('bootId','different'), ('pid',90001), ('startId','different')]:
    setattr(OfflineGameExitProcessTests, 'test_observer_identity_after_exit_' + _key, _observer_identity_case(_key,_value))


def _response_case(key, value):
    def test(self):
        self.response_change = key, value
        with self.assertRaises(ValueError if key == 'stateSha256' else RegistryError): self.observe()
        self.assertEqual((1,1,1,1), self.effects())
        self.assertNotIn('gameExit', self.c.record)
        self.unknown()
    return test


for _key, _value in [('status','OPEN'), ('operationId',fixture.WORLD), ('requestHash','e'*64),
                     ('sessionId','different'), ('nonce','different'), ('stateSha256','invalid'),
                     ('stateRevision',5), ('stateRevision',True), ('worldUuid',fixture.RUN),
                     ('runUuid',fixture.WORLD), ('candidateStateSha256','e'*64), ('saveProofSha256','e'*64),
                     ('extra',True), ('missing','saveProofSha256')]:
    _suffix = str(_value) if _key == 'stateRevision' else ''
    setattr(OfflineGameExitProcessTests, 'test_response_mismatch_' + _key + _suffix, _response_case(_key,_value))


def _exit_race_case(role, stage, code=0, mismatch=False, success=True):
    def test(self):
        state = self.exit_race(role, stage, code, mismatch)
        if success:
            result = self.normal_race()
            self.assertEqual('GAME_EXIT_OBSERVED', result['status'])
            self.assertIsNotNone(self.c.game_exit_result)
            self.assertEqual((1,1,1,1), self.effects())
            self.no_retry()
        else:
            with self.assertRaises(RegistryError): self.observe()
            self.assertEqual((0,0,0,0) if role == 'm' else (1,1,1,1), self.effects())
            self.unknown()
        self.assertTrue(state()[0])
        self.assertEqual({'poll':1,'native':1,'data':2,'group':2}[stage], state()[1])
    return test


for _role in ('m','c'):
    for _stage in ('poll','native','data','group'):
        setattr(OfflineGameExitProcessTests, 'test_normal_exit_race_' + _role + '_' + _stage,
                _exit_race_case(_role,_stage))
    for _stage in ('native','data'):
        setattr(OfflineGameExitProcessTests, 'test_conflicting_identity_race_' + _role + '_' + _stage,
                _exit_race_case(_role,_stage,mismatch=True,success=False))
    for _label,_code in [('nonzero',7),('unknown',None),('boolean',False)]:
        setattr(OfflineGameExitProcessTests, 'test_absent_identity_race_' + _role + '_' + _label,
                _exit_race_case(_role,'native',code=_code,success=False))
