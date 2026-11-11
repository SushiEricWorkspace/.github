"""game退出由来Cの寿命と操作期限を実Registry/Eventで検証する。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import offline_control, offline_process, runtime_control
from . import test_game_ready_open as ready_tests
from . import test_offline_owner_close as close_tests
from . import test_offline_prepare as prepared_tests
from . import test_offline_process as processes
from . import test_offline_repeat as repeat_tests
from .registry import RegistryError


class EditPipe(ready_tests.ReadyPipe):
    def __init__(self):
        super().__init__()
        self.acks = 0

    def send(self, frame, deadline, verify):
        if frame['command'] == 'open':
            return super().send(frame, deadline, verify)
        return processes.Pipe.send(self, frame, deadline, verify)

    def receive(self, deadline, verify):
        if self.sent[-1]['command'] == 'open':
            return super().receive(deadline, verify)
        return processes.Pipe.receive(self, deadline, verify)


class GameOwnerCloseDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.game = ready_tests.GameReadyTests()
        self.game.setUp()
        self.addCleanup(self.game.doCleanups)
        self.game.operation = 'offline-prepare-owner'
        self.game.case.clock = 11
        self.enterContext(patch.object(offline_process, 'ProxyPipe', EditPipe))
        self.enterContext(patch.object(processes.Pipe, 'acknowledged',
                                      prepared_tests.OfflinePrepareTests.acknowledged, create=True))

    def open(self):
        self.opened = self.game.open()
        self.runtime, self.token = self.game.runtime, self.game.token
        self.c = self.runtime._offline.controllers[self.token['instanceId']]
        opened = json.loads(self.c.request)
        writer = dict(sessionId=self.opened['runUuid'], owner=deepcopy(self.c.data_identity),
                      management={key: opened[key] for key in ('leaseId', 'epoch', 'generation', 'drainSha256')})
        writer['management']['controller'] = deepcopy(self.c.parent_data)
        self.current = dict(phase='Idle', hash=self.opened['stateSha256'],
                            revision=self.opened['stateRevision'], run=self.opened['runUuid'],
                            previous=opened['expectedStateSha256'], world=opened['worldUuid'],
                            generation=opened['generation'], writer=writer)
        self.prepared = prepared_tests.OfflinePrepareTests()
        self.prepared.runtime, self.prepared.token = self.runtime, self.token
        self.assertIn('gameExit', json.loads(self.game.f.state()['runtime_proof']))
        self.assertIs(self.runtime._transaction.__func__, runtime_control.RuntimeControl._transaction)

    def edit_fixture(self, clock):
        self.game.case.clock = clock
        edit = repeat_tests.OfflineRepeatTests()
        self.addCleanup(edit.doCleanups)
        edit.runtime, edit.token, edit.c = self.runtime, self.token, self.c
        edit.operation = processes.frame(True)['operationId']
        edit.current, edit.writer = self.current, deepcopy(self.current['writer'])
        edit.effects = dict(assigns=0, begins=0, dbOpens=0, edits=0, dbCloses=0, finishes=0, completes=0)
        edit.requests = []
        edit.failure = edit.change = edit.response = edit.replay = None
        edit.before = edit.after = lambda command: None
        repeat_tests.install_edit_pipe(edit)
        request = deepcopy(processes.frame(True)['request'])
        request['worldUuid'] = self.current['world']
        self.assertTrue(request['action']['products'])
        edit.request = request
        return edit

    def edit(self, clock):
        edit = self.edit_fixture(clock)
        request = edit.request
        result = self.prepare_call(edit)
        self.assertEqual('PREPARED', result['status'])
        edit.members()
        for stage in ('assign', 'admit', 'observe_worker_exit', 'complete'):
            result = edit.call(stage, deadline=30)
        self.assertEqual('COMPLETED', result['status'])
        self.assertEqual(dict.fromkeys(edit.effects, 1), edit.effects)
        self.assertEqual([request, request, request], edit.requests)
        self.assertEqual('Idle', edit.current['phase'])
        return edit

    def close_fixture(self, clock=21, deadline=30):
        case = close_tests.OfflineOwnerCloseTests()
        self.addCleanup(case.doCleanups)
        fixture = SimpleNamespace(runtime=self.runtime, token=self.token, c=self.c)
        case.configure(fixture, self.prepared, self.game.peer, self.current, self.current)
        case.clock, case.deadline = clock, deadline
        return case

    def success(self, case):
        try:
            result = case.close()
        except RegistryError as error:
            self.fail('現在のclose期限内で拒否された ' + error.code)
        self.assertEqual(('OWNER_CLOSED', 1), (result['status'], case.closes))
        self.assertEqual(self.opened['runUuid'], result['runUuid'])
        case.pending()
        case.no_retry()
        return result

    def test_new_close_deadline_after_unacknowledged_open(self):
        self.open()
        self.assertFalse(self.c.startup_acknowledged)
        case = self.close_fixture()
        self.success(case)
        self.assertEqual(1, self.c.pipe.acks)

    def test_nonempty_edit_before_old_deadline_then_new_close(self):
        self.open()
        self.edit(12)
        self.assertTrue(self.c.completion_acknowledged)
        self.success(self.close_fixture())

    def test_nonempty_edit_after_old_deadline_then_new_close(self):
        self.open()
        self.edit(21)
        self.success(self.close_fixture(clock=31, deadline=40))

    def test_event_during_binding_uses_current_close_deadline(self):
        self.open()
        case = self.close_fixture()
        entered, proceed = threading.Event(), threading.Event()
        self.addCleanup(proceed.set)
        original_identity = self.game.peer.adapter.identity
        original_verify = self.c.verify_owner

        def identity(pid):
            result = original_identity(pid)
            if pid == self.c.parent['pid'] and not entered.is_set():
                entered.set()
                self.assertTrue(proceed.wait(10))
            return result

        with patch.object(self.game.peer.adapter, 'identity', identity), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.success, case)
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(0, case.closes)
                self.assertIs(original_verify, self.c.verify_owner)
                case.clock = 25
            finally:
                proceed.set()
            self.assertEqual('OWNER_CLOSED', future.result(10)['status'])

    def test_short_deadline_crossing_startup_ack_keeps_effect_zero(self):
        self.open()
        case = self.close_fixture(clock=12, deadline=15)
        original = self.game.peer.adapter.identity

        def identity(pid):
            result = original(pid)
            if pid == self.c.parent['pid']:
                case.clock = 16
            return result

        with patch.object(self.game.peer.adapter, 'identity', identity):
            with self.assertRaises(RegistryError) as caught:
                case.close()
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.assertEqual((0, 0), (case.closes, self.c.pipe.acks))
        self.assertFalse(self.c.startup_acknowledged)
        self.assertNotIn('startupAcknowledgedSha256', self.c.record)
        self.assertIsNone(self.c.close_request)
        case.no_retry()

    def test_current_deadline_expired_before_intent_is_not_consumed(self):
        self.open()
        case = self.close_fixture(clock=31)
        before = case.saved(), len(self.game.peer.adapter.processes), len(self.c.pipe.sent)
        with self.assertRaises(RegistryError) as caught:
            case.close()
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.assertEqual(0, case.closes)
        self.assertEqual(before, (case.saved(), len(self.game.peer.adapter.processes), len(self.c.pipe.sent)))
        self.assertNotIn('ownerClose', case.saved())
        case.clock = 21
        self.success(case)

    def test_current_deadline_crossing_response_preserves_one_effect(self):
        self.open()
        case = self.close_fixture()
        case.after_receive = lambda: setattr(case, 'clock', 31)
        with self.assertRaises(RegistryError) as caught:
            case.close()
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.assertEqual(1, case.closes)
        self.assertNotIn('responseSha256', case.saved()['ownerClose'])
        case.no_retry()

    def test_expired_lease_after_open_deadline_rejects_before_effect(self):
        self.open()
        case = self.close_fixture()
        self.prepared.mutate('UPDATE instances SET expires=0 WHERE id=?', (self.token['instanceId'],))
        before = case.saved(), len(self.c.pipe.sent)
        with self.assertRaises(RegistryError):
            case.close()
        self.assertEqual(0, case.closes)
        self.assertEqual(before, (case.saved(), len(self.c.pipe.sent)))

    def test_changed_game_original_after_open_deadline_rejects_before_effect(self):
        self.open()
        case = self.close_fixture()
        self.game.slot['result'] += b' '
        with self.assertRaises(RegistryError):
            case.close()
        self.assertEqual((0, 0), (case.closes, self.c.pipe.acks))
        self.assertIn('ownerClose', case.saved())
        case.no_retry()

    def test_open_preparation_crossing_deadline_does_not_spawn(self):
        original = offline_process.offline_controller_command

        def command(bundle):
            result = original(bundle)
            self.game.case.clock = 21
            return result

        with patch.object(offline_process, 'offline_controller_command', command):
            with self.assertRaises(RegistryError) as caught:
                self.game.open()
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.assertEqual((0, 0, 0), self.game.effects())
        self.game.pending()
        self.game.no_retry()

    def test_open_response_crossing_deadline_has_no_local_success(self):
        self.game.after_receive = lambda: setattr(self.game.case, 'clock', 21)
        with self.assertRaises(RegistryError) as caught:
            self.game.open()
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        controller = self.game.runtime._offline.controllers[self.game.token['instanceId']]
        self.assertFalse(controller.ready)
        self.assertNotIn('startupResponseSha256', controller.record)
        self.assertEqual((1, 1, 1), self.game.effects())
        self.game.pending()
        self.game.no_retry()


    def prepare_case(self, previous):
        self.open()
        edit = self.edit(12) if previous else self.edit_fixture(21)
        if previous:
            edit.operation = repeat_tests.THIRD
            edit.request = deepcopy(edit.request)
            edit.request['requestId'] = edit.operation
        self.game.case.clock = 21
        return edit

    def prepare_call(self, edit, operation=None):
        request = deepcopy(edit.request)
        operation = edit.operation if operation is None else operation
        request['requestId'] = operation
        return self.runtime._offline_prepare(self.token, operation_id=operation,
                                             request=request, deadline=30)

    def expect_prepare_timeout(self, edit):
        originals = self.c.request, self.c.response, self.game.slot['result'], self.game.slot['observer_snapshot']
        previous = self.runtime._offline.workers.get(self.token['instanceId'])
        old_raw = None if previous is None else (previous.request, previous.response,
                                                 previous.candidate_response, previous.exit_response)
        effects = deepcopy(edit.effects)
        with self.assertRaises((RegistryError, TimeoutError)) as caught:
            self.prepare_call(edit)
        if isinstance(caught.exception, RegistryError):
            self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.assertEqual(effects, edit.effects)
        self.assertEqual(originals, (self.c.request, self.c.response, self.game.slot['result'],
                                     self.game.slot['observer_snapshot']))
        if previous is not None:
            self.assertEqual(old_raw, (previous.request, previous.response,
                                       previous.candidate_response, previous.exit_response))
        self.prepared.pending()
        self.c.process.close.assert_not_called()
        self.game.case.c.process.close.assert_not_called()
        self.assertFalse(self.game.case.m.closed)
        self.game.case.clock = 21
        before = self.prepare_snapshot()
        for operation in (edit.operation, '00000000-0000-4000-8000-000000000009'):
            with self.assertRaises(RegistryError):
                self.prepare_call(edit, operation)
        self.assertEqual(before, self.prepare_snapshot())
        return self.runtime._offline.workers.get(self.token['instanceId'])

    def prepare_snapshot(self):
        return (self.prepared.saved(), [process.starts for process in self.game.peer.adapter.processes],
                [(len(pipe.sent), pipe.acks) for pipe in self.game.pipes])

    def worker_generation_deadline(self, boundary, previous):
        edit = self.prepare_case(previous)
        original_identity = self.game.peer.adapter.identity
        original_command = offline_process.offline_worker_command
        original_private = offline_process.make_private
        building = [False]

        def command(bundle):
            result = original_command(bundle)
            building[0] = True
            if boundary == 'command':
                self.game.case.clock = 31
            return result

        def identity(pid):
            result = original_identity(pid)
            if building[0] and boundary == 'identity' and pid == self.c.parent['pid']:
                self.game.case.clock = 31
            return result

        def private(path, **kwargs):
            result = original_private(path, **kwargs)
            if building[0] and boundary == 'stderr' and path.name == 'stderr.log':
                self.game.case.clock = 31
            return result

        original_send = processes.Pipe.send

        def sending(pipe, frame, deadline, verify):
            verify()
            if self.game.case.clock >= deadline:
                raise TimeoutError('実pipeと同じI/O前期限拒否')
            return original_send(pipe, frame, deadline, verify)

        with patch.object(offline_process, 'offline_worker_command', command), patch.object(
                self.game.peer.adapter, 'identity', identity), patch.object(
                offline_process, 'make_private', private), patch.object(processes.Pipe, 'send', sending):
            worker = self.expect_prepare_timeout(edit)
        self.assertEqual(0, worker.process.starts)
        self.assertIsNone(worker.request)
        self.assertFalse(worker.ready)
        self.assertNotIn('startupResponseSha256', self.prepared.saved()['worker'])

    def startup_ack_deadline(self, role, boundary, previous):
        edit = self.prepare_case(previous)
        original_ack = offline_process.OfflineProcess.acknowledge_startup
        original_identity = self.game.peer.adapter.identity
        original_record = offline_process.OfflineProcess._record
        original_notice = processes.Pipe.acknowledged
        active = [None]
        calls = []

        def acknowledged(process, value, *, deadline=None):
            target = process is self.c if role == 'c' else process.controller is not None
            if target:
                active[0] = process
                calls.append(deadline)
                if boundary == 'entry':
                    self.game.case.clock = 31
            try:
                return original_ack(process, value, deadline=deadline)
            finally:
                active[0] = None

        def identity(pid):
            result = original_identity(pid)
            if active[0] is not None and boundary == 'identity':
                expected_pid = self.c.parent['pid'] if role == 'c' else active[0].process.pid
                if pid == expected_pid:
                    self.game.case.clock = 31
            return result

        def recorded(process):
            result = original_record(process)
            if process is active[0] and boundary == 'journal':
                self.game.case.clock = 31
            return result

        def noticed(pipe):
            result = original_notice(pipe)
            if active[0] is not None and pipe is active[0].pipe and boundary == 'notice':
                self.game.case.clock = 31
            return result

        with patch.object(offline_process.OfflineProcess, 'acknowledge_startup', acknowledged), patch.object(
                self.game.peer.adapter, 'identity', identity), patch.object(
                offline_process.OfflineProcess, '_record', recorded), patch.object(
                processes.Pipe, 'acknowledged', noticed):
            worker = self.expect_prepare_timeout(edit)
        self.assertEqual([30], calls)
        target = self.c if role == 'c' else worker
        notified = boundary == 'notice'
        self.assertEqual(notified, target.startup_acknowledged)
        self.assertEqual(int(notified), target.pipe.acks)
        if boundary in ('journal', 'notice'):
            self.assertIn('startupAcknowledgedSha256', target.record)
        else:
            self.assertNotIn('startupAcknowledgedSha256', target.record)
        if role == 'c':
            self.assertIsNone(worker)
        else:
            self.assertTrue(worker.ready)
            self.assertIn('startupResponseSha256', self.prepared.saved()['worker'])

    def prepare_record_deadline(self, boundary, previous):
        edit = self.prepare_case(previous)
        old_worker = self.runtime._offline.workers.get(self.token['instanceId'])
        original_opened = self.runtime._offline._opened
        original_dumps = offline_control.json.dumps
        original_transaction = self.runtime.registry.transaction
        original_ack = offline_process.OfflineProcess.acknowledge_startup
        ack_calls = []

        def opened(connection, *args, **kwargs):
            result = original_opened(connection, *args, **kwargs)
            worker = self.runtime._offline.workers.get(self.token['instanceId'])
            if (boundary == 'compare' and worker is not None and worker is not old_worker
                    and worker.ready and not connection.execute('PRAGMA query_only').fetchone()[0]):
                self.game.case.clock = 31
            return result

        def dumped(value, *args, **kwargs):
            result = original_dumps(value, *args, **kwargs)
            if (boundary == 'serialize' and type(value) is dict and type(value.get('worker')) is dict
                    and value['worker'].get('operationId') == edit.operation
                    and 'startupResponseSha256' in value['worker']):
                self.game.case.clock = 31
            return result

        @contextmanager
        def transaction(*args, **kwargs):
            with original_transaction(*args, **kwargs) as connection:
                def trace(sql):
                    worker = self.runtime._offline.workers.get(self.token['instanceId'])
                    if (boundary == 'commit' and worker is not None and worker is not old_worker
                            and worker.ready and sql.startswith('UPDATE runtime_operations SET response=')):
                        self.game.case.clock = 31
                connection.set_trace_callback(trace)
                yield connection

        def acknowledged(process, value, *, deadline=None):
            if process.controller is not None:
                ack_calls.append(deadline)
            return original_ack(process, value, deadline=deadline)

        with patch.object(self.runtime._offline, '_opened', opened), patch.object(
                offline_control.json, 'dumps', dumped), patch.object(
                self.runtime.registry, 'transaction', transaction), patch.object(
                offline_process.OfflineProcess, 'acknowledge_startup', acknowledged):
            worker = self.expect_prepare_timeout(edit)
        self.assertTrue(worker.ready)
        self.assertFalse(worker.startup_acknowledged)
        self.assertEqual([], ack_calls)
        self.assertEqual(0, worker.pipe.acks)
        self.assertEqual(boundary == 'commit', 'startupResponseSha256' in self.prepared.saved()['worker'])

    def test_prepare_expiry_does_not_limit_following_operation_deadlines(self):
        edit = self.prepare_case(False)
        self.assertEqual('PREPARED', self.prepare_call(edit)['status'])
        edit.members()
        self.game.case.clock = 31
        for stage in ('assign', 'admit', 'observe_worker_exit', 'complete'):
            result = edit.call(stage, deadline=40)
        self.assertEqual('COMPLETED', result['status'])
        self.assertEqual(dict.fromkeys(edit.effects, 1), edit.effects)
        self.assertEqual([edit.request] * 3, edit.requests)
        self.success(self.close_fixture(clock=41, deadline=50))


def generation_boundary(boundary, previous):
    def test(case):
        case.worker_generation_deadline(boundary, previous)
    return test


for boundary, previous in (('command', False), ('command', True), ('identity', False), ('stderr', True)):
    suffix = 'next' if previous else 'first'
    setattr(GameOwnerCloseDeadlineTests, 'test_n1_worker_' + boundary + '_' + suffix,
            generation_boundary(boundary, previous))


def acknowledgement_boundary(role, boundary, previous):
    def test(case):
        case.startup_ack_deadline(role, boundary, previous)
    return test


for role, boundaries, variants in (('c', ('entry', 'identity', 'journal'), (False,)),
                                   ('w', ('entry', 'identity', 'journal', 'notice'), (False, True))):
    for boundary in boundaries:
        for previous in variants:
            suffix = 'next' if previous else 'first'
            setattr(GameOwnerCloseDeadlineTests, 'test_n2_' + role + '_ack_' + boundary + '_' + suffix,
                    acknowledgement_boundary(role, boundary, previous))


def record_boundary(boundary, previous):
    def test(case):
        case.prepare_record_deadline(boundary, previous)
    return test


for boundary in ('compare', 'serialize', 'commit'):
    for previous in (False, True):
        suffix = 'next' if previous else 'first'
        setattr(GameOwnerCloseDeadlineTests, 'test_n2_registry_' + boundary + '_' + suffix,
                record_boundary(boundary, previous))
