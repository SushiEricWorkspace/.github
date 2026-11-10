"""直接stopの確定と公共合流予約の終端を実Registryで重ねる。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
import sys
import traceback
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import runtime_control
from . import test_runtime_control as runtimes
from . import test_game_exit_stop as games
from .registry import RegistryError


class DirectStopJoinTests(unittest.TestCase):
    def setUp(self):
        self.case = games.StopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.source, self.base, self.f = self.case.source, self.case.base, self.case.f
        self.runtime, self.managed = self.case.runtime, self.case.managed
        self.instance = self.case.instance
        self.clock = time.monotonic()
        self.parent_go, self.waiter_go = threading.Event(), threading.Event()
        self.addCleanup(self.parent_go.set)
        self.addCleanup(self.waiter_go.set)
        self.ready = threading.Event()
        self.local = threading.local()
        self.paused = [threading.Event(), threading.Event()]
        self.release = self.enterContext(patch.object(self.runtime, '_release_game_handles', wraps=self.runtime._release_game_handles))

    def query(self, sql, args=()):
        return self.source.query(sql, args)

    def effects(self):
        return (len(self.f.adapter.created), self.f.rcon_calls, self.base.channel.send_frame.call_count,
                len(self.case.peer.adapter.processes), sum(p.effects for p in self.case.peer.pipes))

    def overlap(self, *, count=1, pending_change=None, completed_change=None,
                timeout=False, read_timeout=False, parent_timeout=False, expired=False, late=False, unrelated=False, restart=False):
        session = self.base.start()
        self.case.session = session
        native_stop = self.runtime._stop
        native_join = self.runtime._join_direct_stop
        transaction = self.f.registry.transaction
        final_read = [False]

        def stop(*args):
            self.ready.set()
            if not self.parent_go.wait(30):
                raise TimeoutError('直接stopの終了観測前境界')
            return native_stop(*args)

        def sleep(seconds):
            if sys._getframe(1).f_code.co_name != '_join_direct_stop':
                return time.sleep(seconds)
            index = self.local.index
            self.paused[index].set()
            if not self.waiter_go.wait(30):
                raise TimeoutError('公共stop合流の読取境界')

        def join(*args):
            if late:
                self.paused[self.local.index].set()
                if not self.waiter_go.wait(30):
                    raise TimeoutError('遅延合流の予約前境界')
            return native_join(*args)

        @contextmanager
        def traced(*args, **kwargs):
            seen = [False]
            with transaction(*args, **kwargs) as db:
                def trace(sql):
                    if parent_timeout and sql.startswith('SELECT r.*,o.instance AS operation_instance'):
                        self.clock = session['stop_deadline'] + 1
                    if final_read[0] and kwargs.get('read_only') and sql.startswith('SELECT * FROM runtime_operations WHERE id=') and 'joined-0' in sql:
                        seen[0] = True
                db.set_trace_callback(trace)
                yield db
            if seen[0]:
                self.clock += 11

        def call(index):
            self.local.index = index
            return self.f.command('stop', dict(self.f.token, timeoutSeconds=10), 'joined-' + str(index))

        self.enterContext(patch.object(self.runtime, '_stop', side_effect=stop))
        self.enterContext(patch.object(self.runtime, '_join_direct_stop', side_effect=join))
        self.enterContext(patch.object(runtime_control, 'time', SimpleNamespace(monotonic=lambda: self.clock, sleep=sleep)))
        self.enterContext(patch.object(self.f.registry, 'transaction', side_effect=traced))
        self.base.stop_ready.set(); self.base.save_ready.set()
        self.assertTrue(self.ready.wait(30), repr(session["error"]))
        self.parent = session['stop_operation']
        original_bytes = tuple(session[k] for k in ('raw', 'stop_request', 'save_request', 'completion_response'))
        if expired:
            self.f.now = self.f.state()['expires']
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(call, index) for index in range(count)]
            for index, future in enumerate(futures):
                future.add_done_callback(lambda done, index=index: self.paused[index].set())
            try:
                for index, event in enumerate(self.paused[:count]):
                    self.assertTrue(event.wait(30))
                    if futures[index].done():
                        try:
                            futures[index].result()
                        except BaseException:
                            self.fail(traceback.format_exc())
                        self.fail('待機境界より前に合流が返却されました')
                refs = self.query('SELECT * FROM reservations ORDER BY operation,reference')
                self.assertEqual(count + 1 if not late else 1, len(refs))
                self.assertEqual(1, self.f.state()['pending'])
                with self.assertRaises(RegistryError):
                    self.f.start('competing-start')
                self.assertEqual(refs, self.query('SELECT * FROM reservations ORDER BY operation,reference'))
                if pending_change:
                    with self.runtime._transaction(self.instance) as db:
                        pending_change(db, self)
                if unrelated:
                    with self.runtime._transaction(self.instance) as db:
                        db.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                                   ('unrelated', 'other-reference', self.instance, self.f.token['leaseId'], self.f.token['epoch']))
                pending_rows = self.query("SELECT * FROM runtime_operations WHERE id LIKE 'joined-%' ORDER BY id")
                before = self.effects()
                self.parent_go.set()
                self.source.join(session)
                parent = self.query('SELECT * FROM runtime_operations WHERE id=?', (self.parent,))[0]
                if pending_change or parent_timeout:
                    self.assertIsInstance(session['error'], RegistryError)
                    self.assertEqual('STOP_TIMEOUT' if parent_timeout else 'STALE_OPERATION', session['error'].code)
                    self.assertEqual('PENDING', parent['status'])
                    self.assertEqual(pending_rows, self.query("SELECT * FROM runtime_operations WHERE id LIKE 'joined-%' ORDER BY id"))
                    self.assertEqual(count + 1, len(self.query('SELECT * FROM reservations')))
                else:
                    self.assertIsNone(session['error'])
                    self.assertEqual('COMPLETED', parent['status'])
                    self.result = json.loads(parent['response'])
                    self.assertEqual(self.result, json.loads(self.f.state()['runtime_proof']))
                    if not late:
                        # 待機呼出元はまだ停止中。親transactionだけで予約を終端する。
                        for index in range(count):
                            value = self.query('SELECT * FROM runtime_operations WHERE id=?', ('joined-' + str(index),))[0]
                            self.assertEqual('COMPLETED', value['status'])
                            self.assertEqual(parent['response'], value['response'])
                    refs = self.query('SELECT * FROM reservations')
                    self.assertEqual(['unrelated'] if unrelated else [], [r['operation'] for r in refs])
                    if restart:
                        self.runtime.managed = runtimes.FakeManaged(self.runtime)
                        self.runtime.process_adapter = self.f.adapter
                        self.assertTrue(self.f.start('start-before-join-read')['running'])
                        self.assertNotEqual(session['run'], self.f.state()['run_id'])
                        before = self.effects()
                    if completed_change:
                        with self.runtime._transaction(self.instance) as db:
                            completed_change(db, self)
                    if timeout:
                        self.clock += 11
                    final_read[0] = read_timeout
                state_before_return = self.query('SELECT * FROM runtime_operations ORDER BY id')
                self.waiter_go.set()
                results = []
                for future in futures:
                    if pending_change or parent_timeout or completed_change or timeout or read_timeout or restart:
                        with self.assertRaises(RegistryError) as caught:
                            try:
                                future.result(30)
                            except TypeError:
                                self.fail(traceback.format_exc())
                        if timeout or read_timeout:
                            self.assertEqual('STOP_TIMEOUT', caught.exception.code)
                    else:
                        try:
                            value = future.result(30)
                        except RegistryError:
                            self.fail(traceback.format_exc())
                        self.assertEqual(self.result, value)
                        results.append(value)
                self.assertEqual(before, self.effects())
                self.assertEqual(original_bytes, tuple(session[k] for k in ('raw', 'stop_request', 'save_request', 'completion_response')))
                if not late:
                    self.assertEqual(state_before_return, self.query('SELECT * FROM runtime_operations ORDER BY id'))
                self.assertIs(self.runtime._transaction.__func__, runtime_control.RuntimeControl._transaction)
                if not (pending_change or parent_timeout or completed_change or unrelated or restart):
                    self.clock = time.monotonic()
                    final_read[0] = False
                    before_replay = self.effects(), self.query('SELECT * FROM runtime_operations ORDER BY id'), self.query('SELECT * FROM reservations')
                    replay = self.f.command('stop', dict(self.f.token, timeoutSeconds=10), 'joined-0')
                    self.assertEqual(dict(self.result, replayed=True, dryRun=False), replay)
                    self.assertEqual(before_replay, (self.effects(), self.query('SELECT * FROM runtime_operations ORDER BY id'), self.query('SELECT * FROM reservations')))
                self.assertEqual(0, self.f.rcon_calls)
                self.release.assert_not_called()
                return results
            finally:
                self.parent_go.set(); self.waiter_go.set()

    def test_parent_transaction_finishes_paused_waiter_and_reference(self):
        self.overlap()

    def test_parent_transaction_finishes_all_paused_waiters(self):
        self.overlap(count=2)

    def test_late_join_records_parent_result_without_reservation(self):
        self.overlap(late=True)

    def test_expired_lease_completion_and_replay_keep_existing_contract(self):
        self.overlap(expired=True)

    def test_waiter_timeout_keeps_parent_result_without_additional_effect(self):
        self.overlap(timeout=True)

    def test_final_read_crosses_deadline_without_success_or_effect(self):
        self.overlap(read_timeout=True)

    def test_parent_final_deadline_keeps_pending_waiters_and_references(self):
        self.overlap(parent_timeout=True)

    def test_unrelated_reference_is_preserved(self):
        self.overlap(unrelated=True)


    def test_new_start_before_join_read_preserves_new_run(self):
        self.overlap(restart=True)

    def test_return_rejects_residual_reservation(self):
        def change(db, case):
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                       ('joined-0', 'stop:' + case.parent, case.instance, case.f.token['leaseId'], case.f.token['epoch']))
        self.overlap(completed_change=change)


    def test_late_join_rejects_missing_proof_without_reservation(self):
        self.overlap(late=True, completed_change=lambda db, case: db.execute('UPDATE instances SET runtime_proof=NULL'))
        self.assertEqual([], self.query("SELECT * FROM runtime_operations WHERE id='joined-0'"))
        self.assertEqual([], self.query('SELECT * FROM reservations'))


def pending_case(sql, params):
    def test(self):
        self.overlap(pending_change=lambda db, case: db.execute(sql, params(case) if callable(params) else params))
    return test


for name, sql, params in (
    ('lease', "UPDATE reservations SET lease='other' WHERE operation='joined-0'", ()),
    ('epoch', "UPDATE reservations SET epoch=epoch+1 WHERE operation='joined-0'", ()),
    ('instance', "UPDATE runtime_operations SET instance='other' WHERE id='joined-0'", ()),
    ('command', "UPDATE runtime_operations SET command='start' WHERE id='joined-0'", ()),
    ('status', "UPDATE runtime_operations SET status='RECOVERY_REQUIRED' WHERE id='joined-0'", ()),
    ('run', "UPDATE runtime_operations SET response=? WHERE id='joined-0'", lambda s:(json.dumps({'runId':'other', 'stopOperationId':s.parent}),)),
    ('stop', "UPDATE runtime_operations SET response=? WHERE id='joined-0'", lambda s:(json.dumps({'runId':s.managed.sessions[s.instance]['run'], 'stopOperationId':'other'}),)),
):
    setattr(DirectStopJoinTests, 'test_parent_rejects_waiter_' + name + '_mismatch', pending_case(sql, params))


def completed_case(sql, params):
    def test(self):
        self.overlap(completed_change=lambda db, case: db.execute(sql, params))
    return test


for name, sql, params in (
    ('instance', "UPDATE runtime_operations SET instance='other' WHERE id='joined-0'", ()),
    ('command', "UPDATE runtime_operations SET command='start' WHERE id='joined-0'", ()),
    ('fingerprint', "UPDATE runtime_operations SET fingerprint='other' WHERE id='joined-0'", ()),
    ('status', "UPDATE runtime_operations SET status='PENDING' WHERE id='joined-0'", ()),
    ('response', "UPDATE runtime_operations SET response='{}' WHERE id='joined-0'", ()),
    ('proof', "UPDATE instances SET runtime_proof='{}'", ()),
    ('missing_proof', "UPDATE instances SET runtime_proof=NULL", ()),
    ('missing_waiter', "DELETE FROM runtime_operations WHERE id='joined-0'", ()),
):
    setattr(DirectStopJoinTests, 'test_return_rejects_' + name + '_mismatch', completed_case(sql, params))


def result_case(key):
    def test(self):
        def change(db, case):
            value = dict(case.result, **{key:'other'})
            raw = json.dumps(value)
            db.execute('UPDATE runtime_operations SET response=? WHERE id IN (?,?)', (raw, case.parent, 'joined-0'))
            db.execute('UPDATE instances SET runtime_proof=?', (raw,))
        self.overlap(completed_change=change)
    return test


for key in ('runId', 'stopOperationId'):
    setattr(DirectStopJoinTests, 'test_return_rejects_' + key + '_with_matching_outer_proofs', result_case(key))
