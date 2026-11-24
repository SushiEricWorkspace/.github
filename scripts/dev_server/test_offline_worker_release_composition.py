"""最初Wのprocess/pipe独立証拠の合成は、元UNKNOWN/予約と他Wを変更しない。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy
import json
import threading
import unittest
from unittest.mock import patch

from . import offline_control
from .registry import RegistryError
from . import test_offline_worker_pipe_recovery as recovery


class WorkerReleaseCompositionTests(unittest.TestCase):
    def setUp(self, history=False):
        self.case=recovery.WorkerPipeRecoveryTests();self.case.setUp(history=history)
        self.addCleanup(self.case.doCleanups)
        self.runtime,self.c,self.f,self.token=self.case.runtime,self.case.c,self.case.f,self.case.token
        self.offline=self.runtime._offline
        self.key=self.case.case.key
        self.case.prepared()

    def prepared(self):
        self.pipe_result=self.case.recover()
        self.fixed=self.offline.release_pipe_results[self.key]

    def observe(self,**changes):
        values=dict(operation_id='observe-worker-released',open_operation_id=self.case.case.opened,
            pipe_operation_id='recover-worker-pipe',deadline=20)
        values.update(changes)
        return self.runtime._offline_observe_worker_released(self.token,**values)

    def status(self):
        with self.runtime.registry.transaction(read_only=True) as db:
            row=db.execute("SELECT status FROM runtime_operations WHERE command='offline-observe-worker-released'").fetchone()
        return row[0] if row else None

    def retained(self):
        self.case.retained()
        self.assertEqual(4,len(self.case.effects))
        self.assertEqual('COMPLETED',self.case.status())

    def test_same_worker_completion_is_separate_from_original_unknown_and_all_workers(self):
        self.prepared()
        with patch.object(self.case.worker.process,'close',side_effect=AssertionError('元process再close')), \
             patch.object(self.case.worker.pipe,'close_after_exit',side_effect=AssertionError('元pipe再close')):
            result=self.observe()
            self.assertEqual(result,self.observe())
        self.assertEqual('OFFLINE_WORKER_RELEASE_CONFIRMED',result['status'])
        self.assertEqual(self.pipe_result['workerOperationId'],result['workerOperationId'])
        self.assertIs(True,result['workerResourcesReleased']);self.assertIs(False,result['terminatesOriginal'])
        self.assertEqual('COMPLETED',self.status());self.retained()

    def test_historical_first_worker_does_not_complete_or_release_latest(self):
        other=WorkerReleaseCompositionTests();other.setUp(history=True);self.addCleanup(other.doCleanups)
        other.prepared();latest=other.case.case.workers[1]
        fds=latest.pipe.input,latest.pipe.output
        with patch.object(latest.process,'close',side_effect=AssertionError('別W作用')), \
             patch.object(latest.pipe,'close_after_exit',side_effect=AssertionError('別pipe作用')):
            result=other.observe()
        self.assertNotEqual(json.loads(latest.request)['operationId'],result['workerOperationId'])
        self.assertEqual(fds,(latest.pipe.input,latest.pipe.output));other.retained()

    def test_missing_same_p_packet_is_not_adopted_from_completed_row(self):
        self.prepared();self.offline.release_pipe_results.clear()
        self.assertRaises(RegistryError,self.observe)
        self.assertIsNone(self.status());self.retained()

    def test_unstarted_pipe_has_no_completion_packet(self):
        self.assertRaises(RegistryError,self.observe)
        self.assertEqual([],self.case.effects);self.assertIsNone(self.status());self.case.retained()

    def test_partial_pipe_cannot_be_composed(self):
        with patch.object(self.case.pipe,'close_after_exit',side_effect=OSError('未完了')):
            self.assertRaises(OSError,self.case.recover)
        self.assertRaises(RegistryError,self.observe)
        self.assertEqual([],self.case.effects);self.assertIsNone(self.status());self.case.retained()

    def test_completed_native_without_pipe_commit_is_refused(self):
        original=self.runtime._transaction
        @contextmanager
        def fail(*args,**kwargs):
            with original(*args,**kwargs) as db:
                yield db
                if not kwargs.get('read_only'):raise OSError('commit前失敗')
        with patch.object(self.runtime,'_transaction',side_effect=fail):self.assertRaises(OSError,self.case.recover)
        self.assertRaises(RegistryError,self.observe)
        self.assertEqual(4,len(self.case.effects));self.assertIsNone(self.status());self.assertIsNone(self.case.status())
        self.case.retained()

    def test_pipe_commit_return_loss_has_no_positive_commit_notification(self):
        original=self.runtime._transaction
        @contextmanager
        def lost(*args,**kwargs):
            with original(*args,**kwargs) as db:yield db
            if not kwargs.get('read_only'):raise OSError('commit退出不明')
        with patch.object(self.runtime,'_transaction',side_effect=lost):self.assertRaises(OSError,self.case.recover)
        self.assertRaises(RegistryError,self.observe)
        self.assertIsNone(self.status());self.retained()

    def test_packet_origin_copy_is_refused(self):
        self.prepared();self.offline.release_pipe_results[self.key]=tuple(list(self.fixed))
        self.assertRaises(RegistryError,self.observe);self.assertIsNone(self.status());self.retained()

    def test_external_pin_group_copy_after_packet_is_refused(self):
        self.prepared();self.case.worker.pipe_pins=tuple(copy(pin) for pin in self.case.pins)
        self.assertRaises(RegistryError,self.observe);self.assertIsNone(self.status())
        self.case.worker.pipe_pins=self.case.pins;self.retained()

    def test_pipe_row_mismatch_is_refused(self):
        self.prepared()
        with self.runtime.registry.transaction() as db:
            db.execute("UPDATE runtime_operations SET response=response||' ' WHERE id='recover-worker-pipe'")
        self.assertRaises(RegistryError,self.observe);self.assertIsNone(self.status());self.retained()

    def test_process_observation_row_mismatch_is_refused(self):
        self.prepared()
        with self.runtime.registry.transaction() as db:
            db.execute("UPDATE runtime_operations SET response=response||' ' WHERE id='observe-close-completion'")
        self.assertRaises(RegistryError,self.observe);self.assertIsNone(self.status());self.retained()

    def test_original_intent_mismatch_is_refused(self):
        self.prepared()
        with self.runtime.registry.transaction() as db:
            db.execute('UPDATE runtime_operations SET response=response||? WHERE id=?',(' ',self.key[1]))
        self.assertRaises(RegistryError,self.observe);self.assertIsNone(self.status())
        self.assertEqual(4,len(self.case.effects))

    def test_wrong_source_operation_is_refused(self):
        self.prepared();self.assertRaises(RegistryError,self.observe,pipe_operation_id='other-pipe')
        self.assertIsNone(self.status());self.retained()

    def test_existing_operation_with_other_body_is_refused(self):
        self.prepared()
        with self.runtime.registry.transaction() as db:
            db.execute('INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)',
                ('observe-worker-released','wrong','other',self.token['instanceId'],'COMPLETED','{}'))
        self.assertRaises(RegistryError,self.observe);self.assertIsNone(self.status());self.retained()

    def test_source_token_change_is_not_rebased(self):
        self.prepared();self.token=dict(self.token,epoch=self.token['epoch']+1)
        self.assertRaises(RegistryError,self.observe);self.assertIsNone(self.status());self.retained()

    def test_original_ids_cannot_be_reused(self):
        self.prepared()
        for value in (self.key[1],self.f.close_operation,'recover-worker-pipe','observe-close-completion'):
            self.assertRaises(RegistryError,self.observe,operation_id=value)
        self.assertIsNone(self.status());self.retained()

    def test_new_deadline_observes_commit_without_reusing_expired_action_deadline(self):
        self.prepared();self.f.clock=21
        self.assertEqual('OFFLINE_WORKER_RELEASE_CONFIRMED',self.observe(deadline=30)['status'])
        self.retained()

    def test_entry_expiry_starts_no_owner_observation(self):
        self.prepared();self.f.clock=20
        with patch.object(self.c,'_verify_close_binding',side_effect=AssertionError('期限後観測')):
            self.assertRaises(RegistryError,self.observe)
        self.assertIsNone(self.status());self.retained()

    def test_resource_entry_expiry_starts_no_owner_observation(self):
        self.prepared();original=self.runtime.registry.resource_use
        @contextmanager
        def late():
            with original():self.f.clock=20;yield
        with patch.object(self.runtime.registry,'resource_use',side_effect=late), \
             patch.object(self.c,'_verify_close_binding',side_effect=AssertionError('期限後観測')):
            self.assertRaises(RegistryError,self.observe)
        self.assertIsNone(self.status());self.retained()

    def test_owner_observation_expiry_starts_no_registry_io(self):
        self.prepared();original=self.c._verify_close_binding;transaction=self.runtime._transaction;calls=[]
        def late(limit):original(limit);self.f.clock=20
        def counted(*args,**kwargs):calls.append(args);return transaction(*args,**kwargs)
        with patch.object(self.c,'_verify_close_binding',side_effect=late), \
             patch.object(self.runtime,'_transaction',side_effect=counted):
            self.assertRaises(RegistryError,self.observe)
        self.assertEqual([],calls);self.assertIsNone(self.status());self.retained()

    def test_other_thread_epoch_commit_after_owner_is_refused(self):
        self.prepared();original=self.c._verify_close_binding
        def changed(limit):original(limit);self.case.case.case.commit(False)
        with patch.object(self.c,'_verify_close_binding',side_effect=changed):self.assertRaises(RegistryError,self.observe)
        self.assertIsNone(self.status());self.retained()

    def test_other_thread_public_renew_retains_fixed_values(self):
        self.prepared();original=self.c._verify_close_binding
        def renewed(limit):original(limit);self.case.case.case.commit(True)
        with patch.object(self.c,'_verify_close_binding',side_effect=renewed):self.observe()
        self.assertEqual('COMPLETED',self.status());self.retained()

    def test_write_inner_mutation_rolls_back_new_completion(self):
        self.prepared()
        def changed(sql):
            if sql.startswith('INSERT INTO runtime_operations'):self.case.worker.nonce='changed'
        with self.case.case.case.trace(changed):self.assertRaises(RegistryError,self.observe)
        self.assertIsNone(self.status());self.retained()

    def test_copy_mutation_prevents_normal_return_after_commit(self):
        self.prepared();original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_WORKER_RELEASE_CONFIRMED':result['workerResourcesReleased']=False
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):self.assertRaises(RegistryError,self.observe)
        self.assertEqual('COMPLETED',self.status());self.retained()

    def test_resource_exit_expiry_preserves_committed_observation(self):
        self.prepared();original=self.runtime.registry.resource_use
        @contextmanager
        def late():
            with original():yield
            self.f.clock=20
        with patch.object(self.runtime.registry,'resource_use',side_effect=late):self.assertRaises(RegistryError,self.observe)
        self.assertEqual('COMPLETED',self.status());self.retained()

    def test_final_read_exit_mutation_prevents_normal_return(self):
        self.prepared();original=self.runtime._transaction;reads=0
        @contextmanager
        def changed(*args,**kwargs):
            nonlocal reads
            with original(*args,**kwargs) as db:yield db
            if kwargs.get('read_only'):
                reads+=1;self.case.worker.nonce='changed'
        with patch.object(self.runtime,'_transaction',side_effect=changed):self.assertRaises(RegistryError,self.observe)
        self.assertEqual(1,reads);self.assertEqual('COMPLETED',self.status());self.retained()

    def test_concurrent_same_id_is_idempotent_without_resource_effect(self):
        self.prepared();entered=threading.Event();resume=threading.Event();original=self.c._verify_close_binding;first=True
        def paused(limit):
            nonlocal first
            original(limit)
            if first:
                first=False;entered.set()
                if not resume.wait(10):raise AssertionError('再開待ち')
        with patch.object(self.c,'_verify_close_binding',side_effect=paused),ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(self.observe);self.assertTrue(entered.wait(10))
            two=pool.submit(self.observe);self.case.case.case.commit(True);resume.set()
            self.assertEqual(one.result(10),two.result(10))
        self.assertEqual('COMPLETED',self.status());self.retained()
