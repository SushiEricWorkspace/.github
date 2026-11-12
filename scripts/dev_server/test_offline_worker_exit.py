"""実Registryと元transactionで候補に束縛したW終了観測を検証する。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import struct
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import offline_control, offline_process, runtime_control
from . import test_offline_admit as admissions
from .build_inputs import canonical, digest
from .database_proxy_pipe import ProxyPipe
from .registry import RegistryError
from .local_ipc import encode


class OfflineWorkerExitTests(unittest.TestCase):
    def setUp(self):
        f = admissions.OfflineAdmitTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.admit()
        self.f, self.w, self.c, self.p = f, f.w, f.c, f.p
        self.runtime, self.token, self.operation = f.runtime, f.token, f.operation
        self.w.process.code = 0
        # 生存中の元Wと、終了後も残るchildを独立に表す。
        self.w.process.members = lambda: ([self.w.process.pid] if self.w.process.code is None else []) + self.w.process.extra
        self.baseline = self.effects()

    def effects(self):
        return (deepcopy(self.f.counts), len(self.c.pipe.sent), len(self.w.pipe.sent),
                self.c.pipe.acks, self.w.pipe.acks, len(self.p.adapter.processes))

    def observe(self, deadline=None):
        return self.runtime._offline_observe_worker_exit(self.token, operation_id=self.operation,
                                                        deadline=time.monotonic()+10 if deadline is None else deadline)

    def saved(self): return self.f.saved()

    def save(self, value):
        self.f.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'", (json.dumps(value),))

    def preserved(self):
        self.assertEqual(self.baseline, self.effects())
        self.assertEqual(dict.fromkeys(self.f.counts, 1), self.f.counts)
        self.assertEqual(self.f.request, self.f.begin_request)
        self.assertEqual(self.f.request, self.f.edit_request)
        self.assertEqual(('SavedPendingExit4', 4, self.operation),
                         (self.f.phase, self.f.state['revision'], self.f.state['run']))
        self.f.f.f.pending()

    def no_retry(self):
        with self.assertRaises(RegistryError): self.observe()
        with self.assertRaises(RegistryError): self.f.admit()
        self.preserved()

    def test_normal_exit_records_original_worker_and_candidate_without_completing(self):
        before = self.saved()
        result = self.observe()
        self.assertEqual('WORKER_EXITED', result['status'])
        self.assertEqual(self.operation, result['operationId'])
        self.assertEqual(digest(self.w.candidate_response), result['candidateResponseSha256'])
        self.assertEqual(self.w.data_identity, result['worker'])
        self.assertEqual(self.w.process.group, result['ownerGroup'])
        self.assertEqual((0, True), (result['exitCode'], result['allChildrenExited']))
        self.assertEqual(result, json.loads(self.w.exit_response))
        self.assertEqual(result, self.w.record['workerExit'])
        self.assertEqual(dict(operationId=self.operation, responseSha256=digest(canonical(result))),
                         self.saved()['workerExit'])
        self.assertEqual(before, {k:v for k,v in self.saved().items() if k != 'workerExit'})
        self.assertIsNone(self.c.process.poll())
        self.assertIs(self.runtime._offline.workers[self.token['instanceId']], self.w)
        result['worker']['startId'] = 'changed-copy'
        self.assertNotEqual(result, json.loads(self.w.exit_response))
        self.no_retry()

    def test_exited_worker_pid_is_never_resolved_again(self):
        identity = self.p.adapter.identity
        def checked(pid):
            if pid == self.w.process.pid: self.fail('終了後の旧W PIDを再解決しました')
            return identity(pid)
        with patch.object(self.p.adapter, 'identity', checked):
            self.assertEqual('WORKER_EXITED', self.observe()['status'])
        self.preserved()

    def test_real_candidate_pipe_buffers_are_preserved_after_observation(self):
        pipe = ProxyPipe()
        self.addCleanup(pipe.close_after_exit)
        self.w.pipe = pipe
        pipe.send(json.loads(self.w.admission_request), time.monotonic()+1, self.c.verify)
        body = encode(json.loads(self.w.candidate_response))
        os.write(pipe.child_output, struct.pack('!I', len(body))+body)
        pipe.receive(time.monotonic()+1, self.c.verify)
        before = (pipe.outgoing, bytes(pipe.incoming), pipe.input, pipe.output)
        self.assertEqual('WORKER_EXITED', self.observe()['status'])
        self.assertEqual(before, (pipe.outgoing, bytes(pipe.incoming), pipe.input, pipe.output))
        self.assertEqual(len(pipe.outgoing), pipe.offset)
        self.f.f.f.pending()

    def test_running_worker_and_child_are_waited_for_in_order(self):
        self.w.process.code = None
        self.w.process.extra = [100099]
        steps = []
        def advance(_):
            self.assertNotIn('workerExit', self.w.record)
            self.assertNotIn('responseSha256', self.saved()['workerExit'])
            steps.append(1)
            if len(steps) == 1: self.w.process.code = 0
            elif len(steps) == 2: self.w.process.extra.clear()
            else: self.fail('終了条件を満たしても待機しました')
        clock = SimpleNamespace(monotonic=lambda:0, sleep=advance)
        with patch.object(offline_process, 'time', clock), patch.object(offline_control, 'time', clock):
            self.assertEqual('WORKER_EXITED', self.observe(deadline=1)['status'])
        self.assertEqual(2, len(steps))
        self.preserved()

    def test_members_crossing_deadline_does_not_record_success(self):
        clock = SimpleNamespace(value=0, sleep=Mock(side_effect=AssertionError('実sleep不要')))
        clock.monotonic = lambda:clock.value
        def members():
            self.assertEqual(0, clock.value)
            clock.value = 2
            return []
        with patch.object(self.w.process, 'members', members), patch.object(offline_process, 'time', clock), patch.object(offline_control, 'time', clock):
            with self.assertRaises(RegistryError) as error: self.observe(deadline=1)
        self.assertEqual('STOP_TIMEOUT', error.exception.code)
        self.assertIsNone(self.w.exit_response)
        self.assertNotIn('workerExit', self.w.record)
        self.assertEqual({'operationId':self.operation}, self.saved()['workerExit'])
        self.no_retry()

    def test_poll_crossing_deadline_does_not_record_success(self):
        clock = SimpleNamespace(value=0, sleep=Mock(side_effect=AssertionError('実sleep不要')))
        clock.monotonic = lambda:clock.value
        def poll():
            clock.value = 2
            return 0
        with patch.object(self.w.process, 'poll', poll), patch.object(offline_process, 'time', clock), patch.object(offline_control, 'time', clock):
            with self.assertRaises(RegistryError) as error: self.observe(deadline=1)
        self.assertEqual('STOP_TIMEOUT', error.exception.code)
        self.assertIsNone(self.w.exit_response)
        self.assertNotIn('workerExit', self.w.record)
        self.assertEqual({'operationId':self.operation}, self.saved()['workerExit'])
        self.no_retry()

    def test_last_checked_crossing_deadline_does_not_record_success(self):
        clock = SimpleNamespace(value=0, observed=False, sleep=Mock(side_effect=AssertionError('実sleep不要')))
        clock.monotonic = lambda:clock.value
        original = self.c.verify
        def members():
            clock.observed = True
            return []
        def checked():
            original()
            if clock.observed: clock.value = 2
        with patch.object(self.w.process, 'members', members), patch.object(self.c, 'verify', checked), patch.object(offline_process, 'time', clock), patch.object(offline_control, 'time', clock):
            with self.assertRaises(RegistryError) as error: self.observe(deadline=1)
        self.assertEqual('STOP_TIMEOUT', error.exception.code)
        self.assertIsNone(self.w.exit_response)
        self.assertNotIn('workerExit', self.w.record)
        self.assertEqual({'operationId':self.operation}, self.saved()['workerExit'])
        self.no_retry()

    def test_final_registry_check_crossing_deadline_preserves_intent(self):
        clock = SimpleNamespace(value=0, checks=0, sleep=Mock(side_effect=AssertionError('実sleep不要')))
        clock.monotonic = lambda:clock.value
        original = self.runtime._offline._candidate
        def candidate(*args):
            result = original(*args)
            if self.w.exit_response is not None:
                clock.checks += 1
                if clock.checks == 2: clock.value = 2
            return result
        with patch.object(self.runtime._offline, '_candidate', candidate), patch.object(offline_process, 'time', clock), patch.object(offline_control, 'time', clock):
            with self.assertRaises(RegistryError) as error: self.observe(deadline=1)
        self.assertEqual('STOP_TIMEOUT', error.exception.code)
        self.assertEqual(2, clock.checks)
        self.assertIsNotNone(self.w.exit_response)
        self.assertIn('workerExit', self.w.record)
        self.assertEqual({'operationId':self.operation}, self.saved()['workerExit'])
        self.no_retry()

    def timeout(self):
        clock = SimpleNamespace(value=0)
        clock.monotonic = lambda:clock.value
        clock.sleep = Mock(side_effect=lambda _:setattr(clock, 'value', 2))
        with patch.object(offline_process, 'time', clock), patch.object(offline_control, 'time', clock):
            with self.assertRaises(RegistryError) as error: self.observe(deadline=1)
        self.assertEqual('STOP_TIMEOUT', error.exception.code)
        self.assertNotIn('workerExit', self.w.record)
        self.assertNotIn('responseSha256', self.saved()['workerExit'])
        self.no_retry()

    def test_live_worker_is_not_exit_even_with_empty_members(self):
        self.w.process.code = None
        self.w.process.members = lambda: []
        self.timeout()

    def test_child_surviving_zero_exit_blocks_result(self):
        self.w.process.extra = [100099]
        self.timeout()

    def test_unknown_members_does_not_prove_empty_group(self):
        self.w.process.members = lambda: None
        self.timeout()

    def test_expired_deadline_does_not_record_even_exited_worker(self):
        with patch.object(self.w.process, 'poll', side_effect=AssertionError('期限後のOS観測')):
            with self.assertRaises(RegistryError) as error: self.observe(deadline=0)
        self.assertEqual('STOP_TIMEOUT', error.exception.code)
        self.no_retry()

    def test_each_nonzero_or_invalid_exit_is_rejected(self):
        for code in (1, -1, False, '0'):
            with self.subTest(code=code):
                self.w.exit_started = False
                saved = self.saved(); saved.pop('workerExit', None); self.save(saved)
                self.w.process.code = code
                clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 2]), sleep=Mock())
                with patch.object(offline_process, 'time', clock):
                    with self.assertRaises(RegistryError) as error: self.observe(deadline=1)
                self.assertEqual('WRITER_PENDING', error.exception.code)
                self.assertNotIn('workerExit', self.w.record)
                self.assertNotIn('responseSha256', self.saved()['workerExit'])
                self.no_retry()

    def test_members_query_failure_keeps_unknown(self):
        with patch.object(self.w.process, 'members', side_effect=OSError('group取得失敗')):
            with self.assertRaises(OSError): self.observe()
        self.assertNotIn('workerExit', self.w.record)
        self.no_retry()

    def test_candidate_not_ready_rejects_before_observation_intent(self):
        self.w.candidate_ready = False
        with self.assertRaises(RegistryError): self.observe()
        self.assertNotIn('workerExit', self.saved())
        self.assertFalse(self.w.exit_started)
        self.preserved()

    def test_each_candidate_record_mismatch_rejects_before_observation(self):
        before = self.saved()
        for key, value in [('operationId', self.f.state['writer']['hostHash']), ('candidateResponseSha256', '0'*64)]:
            with self.subTest(key=key):
                saved = deepcopy(before); saved['admission'][key] = value; self.save(saved)
                with self.assertRaises(RegistryError): self.observe()
                self.assertFalse(self.w.exit_started)
                self.assertNotIn('workerExit', self.saved())
                self.preserved()
        self.save(before)

    def test_local_candidate_hash_mismatch_rejects_before_observation(self):
        self.w.record['candidateResponseSha256'] = '0'*64
        with self.assertRaises(RegistryError): self.observe()
        self.assertFalse(self.w.exit_started)
        self.assertNotIn('workerExit', self.saved())
        self.preserved()

    def test_changed_candidate_bytes_reject_before_observation(self):
        response = json.loads(self.w.candidate_response); response['receiptSha256'] = '0'*64
        self.w.candidate_response = canonical(response)
        with self.assertRaises(RegistryError): self.observe()
        self.assertNotIn('workerExit', self.saved())
        self.preserved()

    def test_wrong_operation_rejects_without_observation(self):
        with self.assertRaises(RegistryError):
            self.runtime._offline_observe_worker_exit(self.token, operation_id=self.f.request['worldUuid'], deadline=time.monotonic()+10)
        self.assertFalse(self.w.exit_started)
        self.assertNotIn('workerExit', self.saved())
        self.preserved()

    def test_registry_change_during_wait_prevents_result(self):
        self.w.process.code = None
        def advance(_):
            self.w.process.code = 0
            self.f.mutate('UPDATE run_admission SET revision=revision+1')
        with patch.object(offline_process, 'time', SimpleNamespace(monotonic=lambda:0, sleep=advance)):
            with self.assertRaises(RegistryError): self.observe(deadline=1)
        self.assertNotIn('workerExit', self.w.record)
        self.no_retry()

    def test_observation_intent_change_during_wait_prevents_result(self):
        self.w.process.code = None
        def advance(_):
            self.w.process.code = 0
            saved = self.saved(); saved['workerExit']['operationId'] = self.f.request['worldUuid']; self.save(saved)
        with patch.object(offline_process, 'time', SimpleNamespace(monotonic=lambda:0, sleep=advance)):
            with self.assertRaises(RegistryError): self.observe(deadline=1)
        self.assertNotIn('workerExit', self.w.record)
        self.no_retry()

    def test_controller_death_during_wait_prevents_result(self):
        self.w.process.code = None
        def advance(_):
            self.w.process.code = 0
            self.c.process.code = 1
        with patch.object(offline_process, 'time', SimpleNamespace(monotonic=lambda:0, sleep=advance)):
            with self.assertRaises(RegistryError): self.observe(deadline=1)
        self.assertNotIn('workerExit', self.w.record)
        self.no_retry()

    def test_parent_boot_change_during_wait_prevents_result(self):
        self.w.process.code = None
        def advance(_):
            self.w.process.code = 0
            self.p.native.boot = 'new-boot'
        with patch.object(offline_process, 'time', SimpleNamespace(monotonic=lambda:0, sleep=advance)):
            with self.assertRaises(RegistryError): self.observe(deadline=1)
        self.assertNotIn('workerExit', self.w.record)
        self.no_retry()

    def test_observation_intent_write_failure_does_not_observe(self):
        self.f.mutate("CREATE TRIGGER deny_exit BEFORE UPDATE OF response ON runtime_operations WHEN OLD.id='offline-prepare-owner' BEGIN SELECT RAISE(FAIL,'exit intent'); END")
        with patch.object(self.w.process, 'poll', wraps=self.w.process.poll) as poll:
            with self.assertRaises(sqlite3.IntegrityError): self.observe()
            poll.assert_not_called()
        self.assertFalse(self.w.exit_started)
        self.assertNotIn('workerExit', self.saved())
        self.preserved()

    def test_local_exit_record_failure_preserves_intent_and_no_retry(self):
        with patch.object(self.w, '_record', side_effect=OSError('local exit journal')):
            with self.assertRaises(OSError): self.observe()
        self.assertIsNotNone(self.w.exit_response)
        self.assertNotIn('responseSha256', self.saved()['workerExit'])
        self.no_retry()

    def test_registry_exit_result_write_failure_preserves_local_evidence(self):
        self.f.mutate("CREATE TRIGGER deny_exit_result BEFORE UPDATE OF response ON runtime_operations WHEN json_extract(NEW.response,'$.workerExit.responseSha256') IS NOT NULL BEGIN SELECT RAISE(FAIL,'exit result'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.observe()
        self.assertIsNotNone(self.w.exit_response)
        self.assertNotIn('responseSha256', self.saved()['workerExit'])
        self.no_retry()

    def test_observation_response_loss_has_no_additional_action(self):
        original = self.w.observe_exit
        def lost(*args):
            original(*args)
            raise EOFError('終了観測後の返答喪失')
        with patch.object(self.w, 'observe_exit', lost):
            with self.assertRaises(EOFError): self.observe()
        self.assertIsNotNone(self.w.exit_response)
        self.assertNotIn('responseSha256', self.saved()['workerExit'])
        self.no_retry()

    def test_registry_changed_after_local_record_rejects_result(self):
        original = self.w._record
        def record():
            original()
            self.f.mutate('UPDATE reservations SET epoch=epoch+1')
        with patch.object(self.w, '_record', record):
            with self.assertRaises(RegistryError): self.observe()
        self.assertNotIn('responseSha256', self.saved()['workerExit'])
        self.no_retry()

    def test_competitors_and_start_preserve_original_transaction(self):
        entered, release = threading.Event(), threading.Event()
        original = self.w.observe_exit
        calls = []
        def held(*args):
            calls.append(1)
            self.assertEqual(1, len(calls), 'W終了観測へ二重到達しました')
            self.assertEqual({'operationId':self.operation}, self.saved()['workerExit'])
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args)
        with patch.object(self.w, 'observe_exit', held), ThreadPoolExecutor(max_workers=3) as pool:
            winner = pool.submit(self.observe)
            try:
                self.assertTrue(entered.wait(10))
                loser = pool.submit(self.observe)
                with self.assertRaises(RegistryError): loser.result(10)
                start = pool.submit(self.runtime.execute, 'start', dict(self.token, timeoutSeconds=1), operation_id='start-after-candidate')
                with self.assertRaises(RegistryError): start.result(10)
                self.preserved()
            finally: release.set()
            self.assertEqual('WORKER_EXITED', winner.result(10)['status'])
        self.assertIs(self.runtime._transaction.__func__, runtime_control.RuntimeControl._transaction)
        self.no_retry()

    def test_gc_and_stopping_prevent_intent(self):
        for name in ('gc_active', 'stopping'):
            with self.subTest(name=name):
                setattr(self.f.registry, name, True)
                try:
                    with self.assertRaises(RegistryError): self.observe()
                finally: setattr(self.f.registry, name, False)
                self.assertNotIn('workerExit', self.saved())
                self.preserved()


def identity_rejection(key):
    def test(self):
        if key == 'pid': self.w.process.pid += 10
        elif key == 'group': self.w.process.group = 'different-group'
        elif key == 'ownerGroup': self.w.record['creation']['ownerGroup'] = 'different-group'
        elif key == 'controllerGroup':
            self.w.process.group = self.c.process.group
            self.w.record['creation']['ownerGroup'] = self.c.process.group
        elif key == 'creation': self.w.record['creation']['pid'] += 10
        else: self.w.record[key] = {}
        with self.assertRaises(RegistryError): self.observe()
        self.assertNotIn('workerExit', self.w.record)
        self.no_retry()
    return test


for key in ('pid', 'group', 'ownerGroup', 'controllerGroup', 'identity', 'controlIdentity', 'creation'):
    setattr(OfflineWorkerExitTests, 'test_original_'+key+'_mismatch_rejects', identity_rejection(key))
