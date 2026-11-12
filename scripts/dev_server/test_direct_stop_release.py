"""直接stopの単発解放と公共合流・確定後失敗を実Registryで検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import linecache
import os
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import database_proxy_pipe, runtime_control
from . import test_game_exit_stop as stops
from . import test_game_ready_open as ready
from . import test_runtime_control as runtimes
from .registry import RegistryError


class DirectStopReleaseTests(unittest.TestCase):
    def setUp(self):
        self.case = stops.StopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.runtime, self.managed, self.f = self.case.runtime, self.case.managed, self.case.f
        self.parent_go, self.close_go = threading.Event(), threading.Event()
        self.parent_ready, self.close_ready = threading.Event(), threading.Event()
        self.commit_ready, self.commit_go = threading.Event(), threading.Event()
        self.addCleanup(self.commit_go.set)
        self.poll_go = [threading.Event(), threading.Event()]
        self.poll_ready = [threading.Event(), threading.Event()]
        self.local = threading.local()
        self.addCleanup(self.parent_go.set)
        self.addCleanup(self.close_go.set)
        for event in self.poll_go:
            self.addCleanup(event.set)
        self.real_sleep = time.sleep
        self.order = []

    def query(self, sql, args=()):
        return self.case.source.query(sql, args)

    def slot(self):
        return self.runtime._game_exit(self.session['run'], self.session['stop_operation'])[0]

    def effects(self):
        return (len(self.f.adapter.created), self.f.rcon_calls,
                self.case.base.channel.send_frame.call_count, len(self.case.peer.adapter.processes),
                sum(pipe.effects for pipe in self.case.peer.pipes),
                self.case.m.close.call_count, self.slot()['observer_process'].close.call_count)

    def replay(self, operation='joined-0'):
        return self.f.command('stop', dict(self.f.token, timeoutSeconds=10), operation)

    def run_case(self, mode='ok', count=1):
        self.session = self.case.base.start()
        self.case.session = self.session
        stop = self.runtime._stop
        join = self.runtime._join_direct_stop
        stop_release = self.runtime._stop_handle_release
        self.release_returned = False
        release = self.runtime._release_game_handles
        transaction = self.f.registry.transaction
        release_session = self.managed.release

        def held(*args):
            self.parent_thread = threading.get_ident()
            self.parent_ready.set()
            if not self.parent_go.wait(10):
                raise TimeoutError('親stopの観測前境界')
            return stop(*args)

        self.enterContext(patch.object(self.runtime, '_stop', side_effect=held))
        self.case.base.stop_ready.set()
        self.case.base.save_ready.set()
        self.assertTrue(self.parent_ready.wait(10))
        self.case.publish()
        self.case.observe()
        slot = self.session['game_exit']
        pipe = database_proxy_pipe.ProxyPipe()
        pipe.close_child_ends()
        slot['observer'].pipe = pipe
        self.addCleanup(pipe.close_after_exit)
        self.fd_numbers = pipe.input, pipe.output
        self.originals = tuple(self.session[key] for key in ('raw', 'stop_request', 'save_request', 'completion_response'))
        close_m = self.case.m.close

        def close():
            self.close_ready.set()
            if not self.close_go.wait(10):
                raise TimeoutError('元M解放中の境界')
            self.order.append('m')
            if mode == 'm_failure':
                raise OSError('元M解放失敗')
            close_m()
            if mode == 'm_deadline':
                self.case.clock = self.case.deadline + 1

        def close_c():
            self.order.append('c')
            if mode == 'c_failure':
                raise OSError('観測C解放失敗')

        self.case.m.close = Mock(side_effect=close)
        slot['observer_process'].close = Mock(side_effect=close_c)

        def auto_release(token, **values):
            self.release_id = values['operation_id']
            self.assertEqual(self.session['stop_operation'], values['stop_operation_id'])
            self.assertEqual(self.case.deadline, values['deadline'])
            self.assertNotIn(self.case.instance, self.managed.sessions)
            self.assertIs(slot, self.slot())
            if mode == 'before':
                self.case.clock = self.case.deadline + 1
            if mode == 'start_first':
                self.runtime.managed = runtimes.FakeManaged(self.runtime)
                self.runtime.process_adapter = self.f.adapter
                self.assertTrue(self.f.start('start-before-direct-release')['running'])
            def publication(frame, event, arg):
                if (event == 'line' and frame.f_code.co_name == 'release_game_handles'
                        and linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip() == "progress['completed'] = True"):
                    self.commit_ready.set()
                    if not self.commit_go.wait(10):
                        raise TimeoutError('解放DB確定後の完了公開境界')
                return publication

            previous_trace = sys.gettrace()
            if mode == 'commit_gap':
                sys.settrace(publication)
            try:
                value = release(token, **values)
            finally:
                sys.settrace(previous_trace)
            self.release_returned = True
            if mode == 'lost':
                raise EOFError('直接stopの解放応答喪失')
            if mode == 'after':
                self.case.clock = self.case.deadline + 1
            if mode == 'response_binding':
                with self.runtime._transaction(self.case.instance) as db:
                    db.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                               (json.dumps(dict(value, runId='other-run')), self.release_id))
            return value

        def session_release(*args):
            release_session(*args)
            if mode == 'session_deadline':
                self.case.clock = self.case.deadline + 1

        @contextmanager
        def traced(*args, **kwargs):
            with transaction(*args, **kwargs) as db:
                def trace(sql):
                    if sql.startswith("UPDATE runtime_operations SET status='COMPLETED',response="):
                        if mode == 'stop_commit_deadline' and 'gameExit' in sql or mode == 'release_commit_deadline' and 'handlesReleased' in sql:
                            self.case.clock = self.case.deadline + 1
                db.set_trace_callback(trace)
                yield db
            if mode == 'read_deadline' and kwargs.get('read_only') and self.release_returned and threading.get_ident() == self.parent_thread:
                self.case.clock = self.case.deadline + 1

        def joining(*args):
            if mode == 'late':
                index = self.local.index
                self.poll_ready[index].set()
                if not self.poll_go[index].wait(10):
                    raise TimeoutError('遅延合流の受付前境界')
                self.poll_go[index].clear()
            return join(*args)

        def checked_release(*args, **kwargs):
            value = stop_release(*args, **kwargs)
            if mode == 'join_return_deadline' and kwargs.get('replay') and hasattr(self.local, 'index'):
                self.case.clock = self.case.deadline + 1
            return value

        def sleep(seconds):
            if sys._getframe(1).f_code.co_name != '_join_direct_stop':
                return self.real_sleep(seconds)
            index = self.local.index
            self.poll_ready[index].set()
            if not self.poll_go[index].wait(10):
                raise TimeoutError('公共合流の読取境界')
            self.poll_go[index].clear()

        self.enterContext(patch.object(self.runtime, '_join_direct_stop', side_effect=joining))
        self.enterContext(patch.object(self.runtime, '_stop_handle_release', side_effect=checked_release))
        self.enterContext(patch.object(self.runtime, '_release_game_handles', side_effect=auto_release))
        self.enterContext(patch.object(self.managed, 'release', side_effect=session_release))
        self.enterContext(patch.object(self.f.registry, 'transaction', side_effect=traced))
        self.enterContext(patch.object(runtime_control, 'time', SimpleNamespace(monotonic=lambda:self.case.clock, sleep=sleep)))
        if mode == 'expired':
            self.f.now = self.f.state()['expires']

        def call(index):
            self.local.index = index
            try:
                return self.replay('joined-' + str(index))
            finally:
                self.poll_ready[index].set()

        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(call, index) for index in range(count)]
            try:
                for index in range(count):
                    self.assertTrue(self.poll_ready[index].wait(10))
                    self.assertFalse(futures[index].done())
                    self.poll_ready[index].clear()
                self.parent_go.set()
                if mode not in ('before', 'session_deadline', 'stop_commit_deadline', 'start_first'):
                    self.assertTrue(self.close_ready.wait(10), repr(self.session['error']))
                    self.assertEqual(1, self.f.state()['pending'])
                    self.assertEqual('LEASED_STOPPED', self.f.state()['state'])
                    self.assertEqual(1, len(self.query('SELECT * FROM reservations')))
                    self.assertEqual('PENDING', self.query('SELECT status FROM runtime_operations WHERE id=?', (self.release_id,))[0]['status'])
                    if mode.startswith('pending_'):
                        field = mode.removeprefix('pending_')
                        with self.runtime._transaction(self.case.instance) as db:
                            db.execute('UPDATE runtime_operations SET ' + field + '=? WHERE id=?',
                                       ('other' if field != 'status' else 'RECOVERY_REQUIRED', self.release_id))
                    for index in range(count):
                        if mode != 'late':
                            self.assertEqual('COMPLETED', self.query('SELECT status FROM runtime_operations WHERE id=?', ('joined-' + str(index),))[0]['status'])
                        self.poll_go[index].set()
                        self.assertTrue(self.poll_ready[index].wait(10))
                        if mode.startswith('pending_'):
                            with self.assertRaises(RegistryError):
                                futures[index].result(10)
                        else:
                            self.assertFalse(futures[index].done(), '解放前に公共合流が正常返却しました')
                        self.poll_ready[index].clear()
                    refs = self.query('SELECT * FROM reservations')
                    with self.assertRaises(RegistryError):
                        self.f.start('start-during-direct-release')
                    self.assertEqual(refs, self.query('SELECT * FROM reservations'))
                self.close_go.set()
                if mode == 'commit_gap':
                    self.assertTrue(self.commit_ready.wait(10))
                    self.assertIsNot(self.slot()['handle_release'].get('completed'), True)
                    self.assertEqual('COMPLETED', self.query('SELECT status FROM runtime_operations WHERE id=?', (self.release_id,))[0]['status'])
                    self.assertEqual(0, self.f.state()['pending'])
                    self.assertEqual([], self.query('SELECT * FROM reservations'))
                    before = self.effects()
                    for index in range(count):
                        self.poll_go[index].set()
                        try:
                            value = futures[index].result(10)
                        except RegistryError as error:
                            self.fail('DB確定後の正常合流を拒否しました: ' + error.code)
                        self.assertTrue(value['gameHandleReleaseOperationId'] == self.release_id)
                    self.assertEqual(before, self.effects())
                    self.assertIsNot(self.slot()['handle_release'].get('completed'), True)
                    self.commit_go.set()
                self.case.source.join(self.session)
                if mode in ('ok', 'expired', 'late', 'join_return_deadline', 'commit_gap'):
                    self.assertIsNone(self.session['error'])
                else:
                    self.assertIsNotNone(self.session['error'])
                    if mode in ('before', 'session_deadline', 'stop_commit_deadline', 'm_deadline', 'after', 'release_commit_deadline', 'read_deadline'):
                        self.assertEqual('STOP_TIMEOUT', self.session['error'].code)
                for event in self.poll_go:
                    event.set()
                self.results = []
                for future in futures:
                    if mode in ('ok', 'expired', 'late', 'lost', 'commit_gap'):
                        self.results.append(future.result(10))
                    else:
                        with self.assertRaises(RegistryError):
                            future.result(10)
            finally:
                self.commit_go.set()
                self.parent_go.set()
                self.close_go.set()
                for event in self.poll_go:
                    event.set()
        self.proof = json.loads(self.query('SELECT response FROM runtime_operations WHERE id=?', (self.session['stop_operation'],))[0]['response'])
        self.release_id = self.proof['gameHandleReleaseOperationId']
        self.assertEqual(self.originals, tuple(self.session[key] for key in ('raw', 'stop_request', 'save_request', 'completion_response')))
        if mode != 'start_first':
            self.assertEqual('LEASED_STOPPED', self.f.state()['state'])
            self.assertEqual(self.proof, json.loads(self.f.state()['runtime_proof']))
            self.assertNotIn(self.case.instance, self.managed.sessions)
        self.assertEqual(1, len(self.f.adapter.created) if mode != 'start_first' else len(self.f.adapter.created) - 1)
        self.assertEqual(0, self.f.rcon_calls)
        self.assertEqual(1, len(self.case.peer.adapter.processes))
        self.assertEqual(1, sum(pipe.effects for pipe in self.case.peer.pipes))
        self.assertIs(self.runtime._transaction.__func__, runtime_control.RuntimeControl._transaction)

    def test_direct_stop_releases_before_join_and_replay_then_explicit_ready_opens(self):
        self.run_case()
        self.assertEqual([self.proof], self.results)
        self.assertEqual(['m', 'c'], self.order)
        self.assertTrue(self.slot()['handle_release']['completed'])
        for fd in self.fd_numbers:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertIsNone(self.slot()['observer'].pipe.input)
        self.assertIsNone(self.slot()['observer'].pipe.output)
        before = self.effects()
        self.assertEqual(dict(self.proof, replayed=True, dryRun=False), self.replay())
        self.assertEqual(before, self.effects())
        base = ready.GameReadyTests()
        self.addCleanup(base.doCleanups)
        base.configure(self.case)
        self.assertEqual('OPEN', base.open()['status'])

    def test_join_reads_committed_release_before_memory_completion_publication(self):
        self.run_case('commit_gap', count=2)
        self.assertEqual([self.proof, self.proof], self.results)
        self.assertEqual(['m', 'c'], self.order)
        before = self.effects()
        self.assertTrue(self.replay()['replayed'])
        self.assertEqual(before, self.effects())

    def test_multiple_waiters_share_one_release(self):
        self.run_case(count=2)
        self.assertEqual([self.proof, self.proof], self.results)
        self.assertEqual(['m', 'c'], self.order)

    def test_expired_lease_completion_and_replay(self):
        self.run_case('expired')
        before = self.effects()
        self.assertTrue(self.replay()['replayed'])
        self.assertEqual(before, self.effects())

    def refused_replay(self, mode):
        self.run_case(mode)
        self.case.clock = 0
        before = self.effects(), self.query('SELECT * FROM runtime_operations'), self.query('SELECT * FROM reservations')
        with self.assertRaises(RegistryError):
            self.replay()
        self.assertEqual(before, (self.effects(), self.query('SELECT * FROM runtime_operations'), self.query('SELECT * FROM reservations')))

    def test_m_failure_preserves_stop_and_pending_release_without_retry(self):
        self.refused_replay('m_failure')
        self.assertEqual(['m'], self.order)
        self.assertEqual('UNKNOWN', self.slot()['handle_release']['m'])

    def test_c_failure_preserves_released_m_and_pending_without_retry(self):
        self.refused_replay('c_failure')
        self.assertEqual(['m', 'c'], self.order)
        self.assertEqual('RELEASED', self.slot()['handle_release']['m'])
        self.assertEqual('UNKNOWN', self.slot()['handle_release']['c'])

    def test_m_deadline_keeps_c_and_pending_without_retry(self):
        self.refused_replay('m_deadline')
        self.assertEqual(['m'], self.order)

    def test_timeout_after_stop_commit_does_not_start_release_or_rewrite_stop(self):
        self.refused_replay('stop_commit_deadline')
        self.assertEqual([], self.order)
        self.assertNotIn('handle_release', self.slot())

    def test_timeout_after_slot_release_does_not_start_close_or_rewrite_stop(self):
        self.refused_replay('session_deadline')
        self.assertEqual([], self.order)

    def test_timeout_before_release_reservation_has_close_effect_zero(self):
        self.refused_replay('before')
        self.assertEqual([], self.order)

    def completed(self, mode):
        self.run_case(mode)
        self.assertEqual(['m', 'c'], self.order)
        self.assertTrue(self.slot()['handle_release']['completed'])
        self.case.clock = 0
        before = self.effects()
        self.assertTrue(self.replay()['replayed'])
        self.assertEqual(before, self.effects())

    def test_release_commit_deadline_preserves_complete_without_normal_response(self):
        self.completed('release_commit_deadline')

    def test_release_return_deadline_preserves_complete_without_normal_response(self):
        self.completed('after')

    def test_lost_release_response_is_only_read_back_without_reclosing(self):
        self.completed('lost')

    def test_changed_release_response_is_rejected_without_reclosing(self):
        self.refused_replay('response_binding')

    def test_start_before_release_keeps_new_run_without_closing_old_handles(self):
        self.run_case('start_first')
        self.assertEqual('RUNNING', self.f.state()['state'])
        self.assertNotEqual(self.session['run'], self.f.state()['run_id'])
        self.assertEqual([], self.order)

    def test_preflighted_late_join_waits_for_existing_release(self):
        self.run_case('late')
        self.assertEqual([self.proof], self.results)
        self.assertEqual(['m', 'c'], self.order)

    def test_final_release_read_deadline_preserves_complete_without_response(self):
        self.completed('read_deadline')

    def test_join_final_return_deadline_has_no_normal_response_or_extra_close(self):
        self.completed('join_return_deadline')

    def test_pending_release_instance_mismatch_rejects_join(self):
        self.refused_replay('pending_instance')

    def test_pending_release_command_mismatch_rejects_join(self):
        self.refused_replay('pending_command')

    def test_pending_release_status_mismatch_rejects_join(self):
        self.refused_replay('pending_status')
