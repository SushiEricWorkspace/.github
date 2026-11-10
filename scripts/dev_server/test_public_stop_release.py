"""公共stopの自己予約から単発解放・成功応答までを実Registryで検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

from . import database_proxy_pipe, runtime_control
from . import test_game_exit_stop as stops
from . import test_game_ready_open as ready
from . import test_runtime_control as runtimes
from . import test_managed_save_evidence as saves
from . import test_game_handle_release as handles
from .registry import RegistryError


class PublicStopReleaseTests(unittest.TestCase):
    def setUp(self):
        self.case = stops.StopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.runtime, self.managed, self.f = self.case.runtime, self.case.managed, self.case.f
        self.session = self.case.base.start()
        self.case.session = self.session
        self.binding = dict(run_id=self.session['run'], operation_id='public-stop',
                            descriptor_sha256=stops.target.wire.sha256(self.session['raw']))
        self.managed.select_game_exit(self.f.token, deadline=time.monotonic() + 30, **self.binding)
        self.close_order = []
        self.release_id = None
        self.returned = False
        self.expire_reads = True

    def query(self, sql, args=()):
        return self.case.source.query(sql, args)

    def retained(self):
        return self.runtime._game_exit(self.session['run'], 'public-stop')[0]

    def effects(self):
        slot = self.retained()
        return (self.case.m.close.call_count, slot['observer_process'].close.call_count,
                len(self.case.peer.adapter.processes), sum(p.effects for p in self.case.peer.pipes))

    def replay(self):
        return self.case.source.stop()

    def pending(self):
        self.assertEqual('COMPLETED', self.query('SELECT status FROM runtime_operations WHERE id=?', ('public-stop',))[0]['status'])
        self.assertEqual('PENDING', self.query('SELECT status FROM runtime_operations WHERE id=?', (self.release_id,))[0]['status'])
        self.assertEqual(1, self.f.state()['pending'])
        self.assertEqual(1, len(self.query('SELECT * FROM reservations WHERE operation=?', (self.release_id,))))
        self.assertNotIn(self.case.instance, self.managed.sessions)

    def no_retry(self):
        before = self.effects(), self.query('SELECT * FROM runtime_operations ORDER BY id'), self.query('SELECT * FROM reservations')
        for operation in ('public-stop', 'another-public-stop'):
            with self.assertRaises(RegistryError):
                self.f.command('stop', dict(self.f.token, timeoutSeconds=10), operation)
        self.assertEqual(before, (self.effects(), self.query('SELECT * FROM runtime_operations ORDER BY id'), self.query('SELECT * FROM reservations')))

    def run_stop(self, mode='ok'):
        entered, proceed = threading.Event(), threading.Event()
        original_stop = self.runtime._stop
        original_release = self.runtime._release_game_handles
        original_transaction = self.f.registry.transaction

        def held(*args):
            entered.set()
            if not proceed.wait(10):
                raise TimeoutError('公共stop予約境界')
            return original_stop(*args)

        def release(token, **values):
            self.release_id = values['operation_id']
            self.assertEqual(self.case.deadline, values['deadline'])
            self.assertEqual('public-stop', values['stop_operation_id'])
            self.assertNotIn(self.case.instance, self.managed.sessions)
            slot = self.retained()
            pipe = database_proxy_pipe.ProxyPipe()
            pipe.close_child_ends()
            slot['observer'].pipe = pipe
            self.addCleanup(pipe.close_after_exit)
            self.fd_numbers = pipe.input, pipe.output
            old_m_close = self.case.m.close

            def close_m():
                self.close_order.append('m')
                if mode == 'm_failure':
                    raise OSError('元M解放失敗')
                old_m_close()
                if mode == 'expired_m':
                    self.f.now = self.f.state()['expires']
                if mode == 'm_deadline':
                    self.case.clock = self.case.deadline + 1

            def close_c():
                self.close_order.append('c')
                if mode == 'c_failure':
                    raise OSError('観測C解放失敗')

            self.case.m.close = Mock(side_effect=close_m)
            slot['observer_process'].close = Mock(side_effect=close_c)
            if mode == 'start_first':
                self.runtime.process_adapter = self.f.adapter
                self.runtime.managed = runtimes.FakeManaged(self.runtime)
                self.assertTrue(self.f.start('start-before-auto-release')['running'])
            if mode in ('expired_before', 'expired_lease', 'expired_generation'):
                self.f.now = self.f.state()['expires']
            if mode == 'expired_lease':
                token['leaseId'] = 'old-lease'
            if mode == 'expired_generation':
                token['expectedGeneration'] = 'old-generation'
            if mode == 'before':
                self.case.clock = self.case.deadline + 1
            if mode == 'race':
                reserved = threading.Event()
                finish = threading.Event()
                close = self.case.m.close.side_effect

                def blocked_close():
                    reserved.set()
                    if not finish.wait(10):
                        raise TimeoutError('解放予約競合境界')
                    close()

                self.case.m.close.side_effect = blocked_close
                with ThreadPoolExecutor(max_workers=1) as competitors:
                    def start():
                        self.assertTrue(reserved.wait(10))
                        before = self.query('SELECT * FROM reservations')
                        try:
                            with self.assertRaises(RegistryError):
                                self.f.start('overlapping-release-start')
                            self.assertEqual(before, self.query('SELECT * FROM reservations'))
                            self.assertEqual(1, len(self.f.adapter.created))
                        finally:
                            finish.set()
                    future = competitors.submit(start)
                    result = original_release(token, **values)
                    future.result(10)
            else:
                result = original_release(token, **values)
            self.returned = True
            if mode == 'lost':
                raise EOFError('解放完了後の応答喪失')
            if mode == 'after':
                self.case.clock = self.case.deadline + 1
            if mode == 'response_binding':
                with self.runtime._transaction(self.case.instance) as db:
                    value = dict(result, runId='other-run')
                    db.execute('UPDATE runtime_operations SET response=? WHERE id=?', (json.dumps(value), self.release_id))
            return result

        @contextmanager
        def transaction(*args, **kwargs):
            with original_transaction(*args, **kwargs) as db:
                if mode == 'commit_deadline':
                    def trace(sql):
                        if sql.startswith("UPDATE runtime_operations SET status='COMPLETED',response=") and 'handlesReleased' in sql:
                            self.case.clock = self.case.deadline + 1
                    db.set_trace_callback(trace)
                yield db
            if mode == 'read_deadline' and kwargs.get('read_only') and self.returned and self.expire_reads:
                self.case.clock = self.case.deadline + 1

        release_session = self.managed.release
        def session_release(*args):
            release_session(*args)
            if mode == 'session_deadline':
                self.case.clock = self.case.deadline + 1
        self.enterContext(patch.object(self.managed, 'release', side_effect=session_release))
        self.enterContext(patch.object(self.runtime, '_stop', side_effect=held))
        self.enterContext(patch.object(self.runtime, '_release_game_handles', side_effect=release))
        self.enterContext(patch.object(self.f.registry, 'transaction', side_effect=transaction))
        if mode == 'expired_entry':
            self.f.now = self.f.state()['expires']
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.case.source.stop)
            try:
                self.assertTrue(entered.wait(10))
                self.case.base.stop_ready.set()
                self.case.base.save_ready.set()
                self.case.source.join(self.session)
                self.assertIsNone(self.session['error'])
                self.case.publish()
                raw = tuple(self.session[k] for k in ('raw', 'stop_request', 'save_request', 'completion_response'))
                self.managed.supply_game_exit(self.f.token, candidate=deepcopy(self.case.candidate),
                                             deadline=self.case.deadline, **self.binding)
            finally:
                proceed.set()
            if mode in ('ok', 'race', 'expired_before', 'expired_m', 'expired_entry'):
                result = future.result(10)
            else:
                error_type = OSError if mode in ('m_failure', 'c_failure') else EOFError if mode == 'lost' else RegistryError
                with self.assertRaises(error_type) as caught:
                    future.result(10)
                if mode in ('before', 'm_deadline', 'after', 'commit_deadline', 'read_deadline', 'session_deadline'):
                    self.assertEqual('STOP_TIMEOUT', caught.exception.code)
                if mode in ('expired_lease', 'expired_generation'):
                    self.assertEqual('STALE_LEASE' if mode == 'expired_lease' else 'STALE_GENERATION', caught.exception.code)
                result = None
        self.assertEqual(raw, tuple(self.session[k] for k in ('raw', 'stop_request', 'save_request', 'completion_response')))
        self.assertEqual('Ready', self.case.data_phase)
        self.assertEqual(1, len(self.case.peer.adapter.processes))
        self.assertEqual(1, sum(p.effects for p in self.case.peer.pipes))
        self.assertIs(self.runtime._transaction.__func__, runtime_control.RuntimeControl._transaction)
        proof = json.loads(self.query('SELECT response FROM runtime_operations WHERE id=?', ('public-stop',))[0]['response'])
        if mode == 'session_deadline':
            self.release_id = proof['gameHandleReleaseOperationId']
        self.assertEqual(self.release_id, proof['gameHandleReleaseOperationId'])
        if mode != 'start_first':
            self.assertEqual(proof, json.loads(self.f.state()['runtime_proof']))
        return result

    def test_selected_public_stop_releases_then_explicit_ready_opens(self):
        result = self.run_stop()
        self.assertEqual(['m', 'c'], self.close_order)
        self.assertTrue(self.retained()['handle_release']['completed'])
        for fd in self.fd_numbers:
            with self.assertRaises(OSError):
                os.fstat(fd)
        pipe = self.retained()['observer'].pipe
        self.assertIsNone(pipe.input)
        self.assertIsNone(pipe.output)
        before = self.effects()
        replayed = self.replay()
        self.assertEqual(dict(result, replayed=True, dryRun=False), replayed)
        self.assertEqual(before, self.effects())
        base = ready.GameReadyTests()
        self.addCleanup(base.doCleanups)
        base.configure(self.case)
        self.assertEqual('OPEN', base.open()['status'])
        self.assertEqual('OfflineIdle', self.case.data_phase)

    def test_expired_before_release_preserves_stop_without_close(self):
        self.run_stop('before')
        self.assertEqual([], self.close_order)
        self.assertNotIn('handle_release', self.retained())
        self.assertEqual([], self.query('SELECT * FROM reservations'))
        self.assertEqual([], self.query("SELECT * FROM runtime_operations WHERE command='game-handle-release'"))
        self.case.clock = 1
        self.no_retry()

    def test_m_failure_preserves_pending_and_rejects_reentry(self):
        self.run_stop('m_failure')
        self.assertEqual(['m'], self.close_order)
        self.assertEqual('UNKNOWN', self.retained()['handle_release']['m'])
        self.pending()
        self.no_retry()

    def test_c_failure_preserves_released_m_and_rejects_reentry(self):
        self.run_stop('c_failure')
        self.assertEqual(['m', 'c'], self.close_order)
        self.assertEqual('RELEASED', self.retained()['handle_release']['m'])
        self.assertEqual('UNKNOWN', self.retained()['handle_release']['c'])
        self.pending()
        self.no_retry()

    def test_m_close_deadline_keeps_c_and_pending(self):
        self.run_stop('m_deadline')
        self.assertEqual(['m'], self.close_order)
        self.pending()
        self.case.clock = 1
        self.no_retry()

    def completed_without_response(self, mode):
        self.run_stop(mode)
        self.assertEqual(['m', 'c'], self.close_order)
        self.assertTrue(self.retained()['handle_release']['completed'])
        self.assertEqual(0, self.f.state()['pending'])
        self.assertEqual([], self.query('SELECT * FROM reservations'))
        self.case.clock = 1
        self.expire_reads = False
        before = self.effects()
        self.assertTrue(self.replay()['replayed'])
        self.assertEqual(before, self.effects())

    def test_release_commit_deadline_keeps_evidence_without_success_response(self):
        self.completed_without_response('commit_deadline')

    def test_release_return_deadline_keeps_evidence_without_success_response(self):
        self.completed_without_response('after')

    def test_final_read_deadline_has_no_success_response(self):
        self.completed_without_response('read_deadline')

    def test_release_response_loss_is_not_reclosed(self):
        self.completed_without_response('lost')

    def test_wrong_release_response_binding_is_rejected(self):
        self.run_stop('response_binding')
        self.no_retry()

    def test_start_during_release_preserves_pending_reservation(self):
        self.run_stop('race')

    def test_replay_final_read_deadline_has_no_success_response(self):
        self.run_stop()
        before = self.effects()
        transaction = self.f.registry.transaction
        @contextmanager
        def expires(*args, **kwargs):
            final_read = [False]
            with transaction(*args, **kwargs) as db:
                if kwargs.get('read_only'):
                    def trace(sql):
                        if sql.startswith('SELECT * FROM runtime_operations WHERE id=') and self.release_id in sql:
                            final_read[0] = True
                    db.set_trace_callback(trace)
                yield db
            if final_read[0]:
                self.case.clock = 20
        with patch.object(self.f.registry, 'transaction', side_effect=expires):
            with self.assertRaises(RegistryError) as caught:
                self.replay()
        self.assertEqual('STOP_TIMEOUT', caught.exception.code)
        self.assertEqual(before, self.effects())
        self.assertTrue(self.retained()['handle_release']['completed'])

    def test_replay_requires_release_progress_and_result(self):
        self.run_stop()
        self.retained()['handle_release'].pop('c')
        self.no_retry()

    def test_session_release_deadline_prevents_dedicated_call(self):
        self.run_stop('session_deadline')
        self.runtime._release_game_handles.assert_not_called()
        self.assertFalse(self.case.m.closed)
        self.retained()['observer_process'].close.assert_not_called()
        self.assertNotIn('handle_release', self.retained())
        self.assertEqual([], self.query('SELECT * FROM reservations'))

    def test_start_before_auto_release_preserves_new_run_without_close(self):
        self.run_stop('start_first')
        self.assertEqual('RUNNING', self.f.state()['state'])
        self.assertNotEqual(self.session['run'], self.f.state()['run_id'])
        self.assertEqual(2, len(self.f.adapter.created))
        self.assertEqual([], self.close_order)
        self.assertNotIn('handle_release', self.retained())
        self.assertEqual([], self.query("SELECT * FROM runtime_operations WHERE command='game-handle-release'"))



    def expired_stop(self, mode):
        before = self.f.state()
        try:
            result = self.run_stop(mode)
        except RegistryError as error:
            self.fail('同stopの期限切れleaseを拒否しました ' + error.code)
        self.assertTrue(result['runtimeStopped'])
        self.assertEqual(['m', 'c'], self.close_order)
        self.assertTrue(self.retained()['handle_release']['completed'])
        self.assertEqual(0, self.f.state()['pending'])
        self.assertEqual([], self.query('SELECT * FROM reservations'))
        for key in ('expires', 'owner', 'lease', 'epoch', 'generation'):
            self.assertEqual(before[key], self.f.state()[key], key)
        self.f.now = before['expires']
        effects = self.effects(), self.query('SELECT * FROM runtime_operations ORDER BY id')
        try:
            replayed = self.replay()
        except RegistryError as error:
            self.fail('確定stopの期限切れ再提示を拒否しました ' + error.code)
        self.assertEqual(dict(result, replayed=True, dryRun=False), replayed)
        self.assertEqual(effects, (self.effects(), self.query('SELECT * FROM runtime_operations ORDER BY id')))
        self.runtime._release_game_handles.assert_called_once()

    def test_expired_lease_replay_keeps_completed_release(self):
        self.expired_stop('ok')

    def test_lease_expired_after_selection_completes_stop(self):
        self.expired_stop('expired_entry')

    def test_lease_expires_before_auto_release_completes_stop(self):
        self.expired_stop('expired_before')

    def test_lease_expires_after_m_close_completes_once(self):
        self.expired_stop('expired_m')

    def test_expired_auto_release_still_rejects_old_lease(self):
        self.run_stop('expired_lease')
        self.assertEqual([], self.close_order)
        self.assertNotIn('handle_release', self.retained())
        self.assertEqual([], self.query('SELECT * FROM reservations'))

    def test_expired_auto_release_still_rejects_old_generation(self):
        self.run_stop('expired_generation')
        self.assertEqual([], self.close_order)
        self.assertNotIn('handle_release', self.retained())
        self.assertEqual([], self.query('SELECT * FROM reservations'))

    def test_expired_lease_still_rejects_next_offline_open(self):
        self.expired_stop('ok')
        base = ready.GameReadyTests()
        self.addCleanup(base.doCleanups)
        base.configure(self.case)
        with self.assertRaises(RegistryError) as caught:
            base.open()
        self.assertEqual('LEASE_EXPIRED', caught.exception.code)
        self.assertEqual((0, 0, 0), base.effects())
        self.assertEqual('Ready', self.case.data_phase)


class ExpiredUnselectedStopTests(unittest.TestCase):
    def expired_stop(self, during):
        case = saves.SaveEvidenceRegistryTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        initial = {}
        start = case.base.start
        stop = case.runtime._stop
        def started():
            session = start()
            initial.update(case.f.state())
            if not during:
                case.f.now = initial['expires']
            return session
        def stopping(*args):
            if during:
                case.f.now = initial['expires']
            return stop(*args)
        with patch.object(case.base, 'start', side_effect=started), patch.object(case.runtime, '_stop', side_effect=stopping), patch.object(case.runtime, '_release_game_handles', wraps=case.runtime._release_game_handles) as release:
            case.public_stop()
            release.assert_not_called()
        self.assertEqual(initial['expires'], case.f.now)
        for key in ('expires', 'owner', 'lease', 'epoch', 'generation'):
            self.assertEqual(initial[key], case.f.state()[key], key)
        result = json.loads(case.f.state()['runtime_proof'])
        self.assertNotIn('gameHandleReleaseOperationId', result)
        self.assertEqual([], case.query("SELECT * FROM runtime_operations WHERE command='game-handle-release'"))

    def test_expired_unselected_stop_and_replay_succeed(self):
        self.expired_stop(False)

    def test_unselected_stop_crosses_expiration_and_replays(self):
        self.expired_stop(True)


class ExpiredManualReleaseTests(unittest.TestCase):
    def test_unbound_expired_release_has_effect_zero(self):
        case = handles.HandleReleaseTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.f.now = case.f.state()['expires']
        before = case.base.snapshot()
        with self.assertRaises(RegistryError) as caught:
            case.release()
        self.assertEqual('LEASE_EXPIRED', caught.exception.code)
        self.assertEqual((0, 0, (0, 0, 0)), case.counts())
        self.assertEqual(before, case.base.snapshot())


def release_binding_case(area, field, value):
    def test(self):
        self.run_stop()
        if area == 'progress':
            self.retained()['handle_release'][field] = value
        else:
            with self.runtime._transaction(self.case.instance) as db:
                if area == 'operation':
                    db.execute('UPDATE runtime_operations SET ' + field + '=? WHERE id=?', (value, self.release_id))
                else:
                    record = db.execute('SELECT response FROM runtime_operations WHERE id=?', (self.release_id,)).fetchone()
                    response = json.loads(record[0])
                    if field == 'stopProofSha256':
                        response['binding'][field] = value
                    else:
                        response[field] = value
                    db.execute('UPDATE runtime_operations SET response=? WHERE id=?', (json.dumps(response), self.release_id))
        self.no_retry()
    return test


for area, field, value in (
    ('progress', 'operationId', 'old-release'),
    ('progress', 'm', 'UNKNOWN'),
    ('progress', 'c', 'UNKNOWN'),
    ('progress', 'fingerprint', '0'*64),
    ('operation', 'instance', 'other-instance'),
    ('operation', 'command', 'stop'),
    ('operation', 'status', 'PENDING'),
    ('response', 'instanceId', 'other-instance'),
    ('response', 'stopOperationId', 'other-stop'),
    ('response', 'handlesReleased', False),
    ('response', 'stopProofSha256', '0'*64),
):
    setattr(PublicStopReleaseTests, 'test_replay_' + area + '_' + field + '_mismatch',
            release_binding_case(area, field, value))
