"""最初Wの完成証拠から、次Wの未開始process資源だけを一度解放する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy
import json
import sys
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import offline_control, runtime_process
from . import test_offline_worker_pipe_recovery as recovery
from .registry import RegistryError


class NextWorkerCloseTests(unittest.TestCase):
    def setUp(self):
        self.case=recovery.WorkerPipeRecoveryTests();self.case.setUp(history=True)
        self.addCleanup(self.case.doCleanups)
        self.runtime,self.c,self.f,self.token=self.case.runtime,self.case.c,self.case.f,self.case.token
        self.offline=self.runtime._offline
        self.key=self.case.case.key
        self.worker=self.case.case.workers[1]
        self.worker_id=json.loads(self.worker.request)['operationId']
        self.worker.process.handle,self.worker.process.job=1901,1902
        original=MethodType(runtime_process.OwnedProcess.close,self.worker.process)
        self.mode='normal'
        def close(**options):
            if self.mode=='no-notify':options.pop('observe')
            if self.mode=='prepare-deadline':self.f.clock=20
            if self.mode=='prepare-handle':self.worker.process.handle=9993
            if self.mode=='prepare-epoch':self.case.case.case.commit(False)
            original(**options)
            if self.mode=='lost':raise OSError('元close返却喪失')
        self.close=Mock(side_effect=close)
        self.worker.process.close=self.close
        self.case.prepared();self.case.recover()
        self.first=self.runtime._offline_observe_worker_released(self.token,operation_id='first-worker-released',
            open_operation_id=self.key[1],pipe_operation_id='recover-worker-pipe',deadline=20)
        self.frame=self.offline.release_next_workers[self.key]
        self.intent=self.f.saved()
        self.pipe_values=(self.worker.pipe,self.worker.pipe.input,self.worker.pipe.output,
                          getattr(self.worker,'pipe_pins',None))
        self.effects=[]
        self.hook=lambda h:None
        self.failure=None
        def native(handle):
            self.effects.append(handle)
            self.hook(handle)
            if handle==self.failure:raise OSError('native close不明')
            return True
        self.enterContext(patch.object(runtime_process,'_winapi',SimpleNamespace(CloseHandle=native)))
        self.enterContext(patch.object(runtime_process,'kernel',SimpleNamespace(CloseHandle=native)))

    def recover(self,**changes):
        values=dict(operation_id='close-next-worker',open_operation_id=self.key[1],
            pipe_operation_id='recover-worker-pipe',observation_operation_id='first-worker-released',
            worker_operation_id=self.worker_id,deadline=20)
        values.update(changes)
        return self.runtime._offline_recover_next_worker_close(self.token,**values)

    def status(self):
        with self.runtime.registry.transaction(read_only=True) as db:
            row=db.execute("SELECT status FROM runtime_operations WHERE command='offline-recover-next-worker-close'").fetchone()
        return row[0] if row else None

    def retained(self):
        self.case.retained()
        self.assertEqual(self.intent,self.f.saved())
        self.assertEqual(self.pipe_values,(self.worker.pipe,self.worker.pipe.input,self.worker.pipe.output,
                                          getattr(self.worker,'pipe_pins',None)))
        self.assertEqual({'handle':'NOT_STARTED','pipe':'NOT_STARTED'},self.c.worker_release[self.worker_id])
        self.assertEqual(4,len(self.case.effects))

    def no_retry(self):
        before=list(self.effects)
        for op in ('close-next-worker','another-next-close'):
            with self.assertRaises(RegistryError) as error:self.recover(operation_id=op,deadline=30)
            self.assertEqual('RECOVERY_REQUIRED',error.exception.code)
        self.assertEqual(before,self.effects)

    def rejected(self):
        self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained()

    def test_only_next_original_process_closes_once_and_pipe_is_untouched(self):
        with patch.object(self.worker.process,'close',side_effect=AssertionError('属性からの再採用')), \
             patch.object(self.worker.process,'poll',side_effect=AssertionError('旧PID再観測')), \
             patch.object(self.worker.process,'members',side_effect=AssertionError('旧group再観測')), \
             patch.object(self.worker.pipe,'close_after_exit',side_effect=AssertionError('pipe作用')), \
             patch.object(self.c,'_record',side_effect=AssertionError('局所同期')):
            result=self.recover()
        self.assertEqual('OFFLINE_NEXT_WORKER_CLOSE_COMPLETED',result['status'])
        self.assertEqual(self.worker_id,result['workerOperationId'])
        self.assertIs(True,result['processResourcesClosed'])
        for name in ('pipeReleased','allWorkersReleased','terminatesOriginal'):self.assertIs(False,result[name])
        self.assertEqual([1901,1902],self.effects);self.close.assert_called_once()
        self.assertEqual('COMPLETED',self.status());self.retained();self.no_retry()

    def test_missing_original_frame_is_not_reconstructed(self):
        self.offline.release_next_workers.clear();self.rejected()

    def test_frame_copy_is_not_adopted_as_the_original(self):
        self.offline.release_next_workers[self.key]=tuple(list(self.frame));self.rejected()

    def test_wrong_worker_is_not_selected_from_current_latest(self):
        self.assertRaises(RegistryError,self.recover,worker_operation_id='different-worker')
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained()

    def test_original_release_id_is_not_a_new_recover_id(self):
        self.assertRaises(RegistryError,self.recover,operation_id=self.f.close_operation)
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained()

    def test_current_handle_change_cannot_rebase_the_fixed_value(self):
        self.worker.process.handle=9993;self.rejected()

    def test_current_native_reference_change_is_refused(self):
        self.worker.native=SimpleNamespace();self.rejected()

    def test_pin_group_copy_is_refused(self):
        pins=self.case.pipe.pins
        self.case.pipe._pins=tuple(copy(pin) for pin in pins)
        self.rejected()

    def test_missing_first_worker_completion_starts_no_close(self):
        with self.runtime.registry.transaction() as db:
            db.execute("DELETE FROM runtime_operations WHERE id='first-worker-released'")
        self.rejected()

    def test_first_worker_completion_raw_mismatch_starts_no_close(self):
        with self.runtime.registry.transaction() as db:
            db.execute("UPDATE runtime_operations SET response=response||' ' WHERE id='first-worker-released'")
        self.rejected()

    def test_pipe_completion_mismatch_starts_no_close(self):
        with self.runtime.registry.transaction() as db:
            db.execute("UPDATE runtime_operations SET response=response||' ' WHERE id='recover-worker-pipe'")
        self.rejected()

    def test_existing_id_starts_no_close(self):
        with self.runtime.registry.transaction() as db:
            db.execute('INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)',
                ('close-next-worker','other','other',self.token['instanceId'],'COMPLETED','{}'))
        self.rejected()

    def test_changed_source_token_starts_no_close(self):
        self.token=dict(self.token,epoch=self.token['epoch']+1);self.rejected()

    def test_no_normal_close_notification_cannot_commit(self):
        self.mode='no-notify';self.assertRaises(RegistryError,self.recover)
        self.assertEqual([1901,1902],self.effects);self.assertIsNone(self.status())
        self.retained();self.no_retry()

    def test_close_return_loss_preserves_without_retry(self):
        self.mode='lost';self.assertRaises(OSError,self.recover)
        self.assertEqual([1901,1902],self.effects);self.assertIsNone(self.status())
        self.retained();self.no_retry()

    def test_first_native_failure_starts_no_job_close(self):
        self.failure=1901;self.assertRaises(OSError,self.recover)
        self.assertEqual([1901],self.effects);self.assertIsNone(self.status());self.retained();self.no_retry()

    def test_close_preparation_deadline_starts_no_native_action(self):
        self.mode='prepare-deadline';self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained();self.no_retry()

    def test_close_preparation_handle_change_starts_no_native_action(self):
        self.mode='prepare-handle';self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained();self.no_retry()

    def test_close_preparation_epoch_commit_starts_no_native_action(self):
        self.mode='prepare-epoch';self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained();self.no_retry()

    def test_second_native_failure_does_not_repeat_the_handle(self):
        self.failure=1902;self.assertRaises(OSError,self.recover)
        self.assertEqual([1901,1902],self.effects);self.assertIsNone(self.status());self.retained();self.no_retry()

    def test_entry_expiry_starts_no_registry_io(self):
        self.f.clock=20
        with patch.object(self.runtime,'_transaction',side_effect=AssertionError('期限後read')):
            self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],self.effects);self.retained()

    def test_resource_entry_expiry_starts_no_registry_io(self):
        original=self.runtime.registry.resource_use
        @contextmanager
        def late():
            with original():self.f.clock=20;yield
        with patch.object(self.runtime.registry,'resource_use',side_effect=late), \
             patch.object(self.runtime,'_transaction',side_effect=AssertionError('期限後read')):
            self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],self.effects);self.retained()

    def test_handoff_after_helper_cannot_replace_original_binding(self):
        original=self.offline.observe_worker_released
        def changed(*args,**kwargs):
            result=original(*args,**kwargs);self.worker.nonce='changed';return result
        with patch.object(self.offline,'observe_worker_released',side_effect=changed):self.rejected()

    def test_handoff_response_cannot_be_replaced(self):
        original=self.offline.observe_worker_released
        def changed(*args,**kwargs):
            result,packet=original(*args,**kwargs)
            return result,(packet[0],b'{}',*packet[2:])
        with patch.object(self.offline,'observe_worker_released',side_effect=changed):self.rejected()

    def test_handoff_committed_row_change_starts_no_native_action(self):
        original=self.offline.observe_worker_released
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            with self.runtime.registry.transaction() as db:
                db.execute("UPDATE runtime_operations SET response=response||' ' WHERE id='first-worker-released'")
            return result
        with patch.object(self.offline,'observe_worker_released',side_effect=changed):self.rejected()

    def test_handoff_deadline_starts_no_next_observation(self):
        original=self.offline.observe_worker_released
        def late(*args,**kwargs):
            result=original(*args,**kwargs);self.f.clock=20;return result
        with patch.object(self.offline,'observe_worker_released',side_effect=late):self.rejected()

    def test_owner_observation_expiry_starts_no_registry_io(self):
        original=self.offline.observe_worker_released;verify=self.c._verify_close_binding
        transaction=self.runtime._transaction;handed=False;expired=[]
        def handoff(*args,**kwargs):
            nonlocal handed
            result=original(*args,**kwargs);handed=True;return result
        def late(limit):
            verify(limit)
            if handed:self.f.clock=20
        def checked(*args,**kwargs):
            if handed and self.f.clock>=20:
                expired.append(args);raise AssertionError('期限後Registry read')
            return transaction(*args,**kwargs)
        with patch.object(self.offline,'observe_worker_released',side_effect=handoff), \
             patch.object(self.c,'_verify_close_binding',side_effect=late), \
             patch.object(self.runtime,'_transaction',side_effect=checked):
            self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],expired)
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained()

    def test_first_handle_deadline_starts_no_job_close(self):
        self.hook=lambda h:setattr(self.f,'clock',20)
        self.assertRaises(RegistryError,self.recover)
        self.assertEqual([1901],self.effects);self.assertIsNone(self.status());self.retained();self.no_retry()

    def test_last_native_deadline_has_no_completion_or_write(self):
        def late(h):
            if h==1902:self.f.clock=20
        self.hook=late;self.assertRaises(RegistryError,self.recover)
        self.assertEqual([1901,1902],self.effects);self.assertIsNone(self.status());self.retained();self.no_retry()

    def test_epoch_commit_between_native_actions_starts_no_job_close(self):
        self.hook=lambda h:self.case.case.case.commit(False)
        self.assertRaises(RegistryError,self.recover)
        self.assertEqual([1901],self.effects);self.assertIsNone(self.status());self.retained();self.no_retry()

    def test_normal_renew_between_native_actions_keeps_original_deadline(self):
        self.hook=lambda h:self.case.case.case.commit(True)
        before=self.c.close_binding['deadline'];self.recover()
        self.assertEqual(before,self.c.close_binding['deadline'])
        self.assertEqual([1901,1902],self.effects);self.assertEqual('COMPLETED',self.status());self.retained()

    def test_source_readback_commit_loss_starts_no_target_close(self):
        original=self.runtime._transaction;packets=[]
        @contextmanager
        def lost(*args,**kwargs):
            if not kwargs.get('read_only'):
                frame=sys._getframe()
                while frame.f_code.co_name!='observe_worker_released':frame=frame.f_back
                packets.append(frame.f_locals['handoff'])
            with original(*args,**kwargs) as db:yield db
            if not kwargs.get('read_only'):raise OSError('source commit退出喪失')
        with patch.object(self.runtime,'_transaction',side_effect=lost):self.assertRaises(OSError,self.recover)
        self.assertEqual(1,len(packets));self.assertRaises(RegistryError,packets[0][3],30)
        self.assertEqual([],self.effects);self.assertEqual([],self.frame[4]);self.retained()

    def commit_failure(self,after):
        original=self.runtime._transaction;writes=0
        @contextmanager
        def changed(*args,**kwargs):
            nonlocal writes
            target=not kwargs.get('read_only')
            if target:writes+=1
            with original(*args,**kwargs) as db:
                yield db
                if target and writes==2 and not after:raise OSError('target commit前例外')
            if target and writes==2 and after:raise OSError('target commit退出喪失')
        with patch.object(self.runtime,'_transaction',side_effect=changed):self.assertRaises(OSError,self.recover)
        self.assertEqual([1901,1902],self.effects)
        self.assertEqual('COMPLETED' if after else None,self.status())
        self.assertRaises(RegistryError,self.frame[4][0][3],30)
        self.retained();self.no_retry()

    def test_target_commit_failure_keeps_the_original_resources(self):self.commit_failure(False)

    def test_target_commit_notification_loss_cannot_be_adopted(self):self.commit_failure(True)

    def test_write_inner_original_change_rolls_back(self):
        def change(sql):
            if sql.startswith('INSERT INTO runtime_operations'):self.worker.nonce='changed'
        with self.case.case.case.trace(change):self.assertRaises(RegistryError,self.recover)
        self.assertEqual([1901,1902],self.effects);self.assertIsNone(self.status());self.retained();self.no_retry()

    def test_final_copy_change_rejects_normal_return_after_commit(self):
        original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_NEXT_WORKER_CLOSE_COMPLETED':
                result['pipeReleased']=True
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):self.assertRaises(RegistryError,self.recover)
        self.assertEqual('COMPLETED',self.status());self.retained();self.no_retry()

    def test_original_handle_change_during_copy_is_not_rebased(self):
        original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_NEXT_WORKER_CLOSE_COMPLETED':
                self.worker.process.handle=9993
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):self.assertRaises(RegistryError,self.recover)
        self.assertEqual('COMPLETED',self.status());self.retained();self.no_retry()

    def test_different_id_after_completion_cannot_repeat_native_action(self):
        self.recover();self.assertRaises(RegistryError,self.recover,operation_id='later-next-close')
        self.assertEqual([1901,1902],self.effects);self.retained()

    def test_final_read_exit_original_change_rejects_normal_return(self):
        original=self.runtime._transaction;writes=0
        @contextmanager
        def changed(*args,**kwargs):
            nonlocal writes
            if not kwargs.get('read_only'):writes+=1
            with original(*args,**kwargs) as db:yield db
            if kwargs.get('read_only') and writes==2:self.worker.nonce='changed'
        with patch.object(self.runtime,'_transaction',side_effect=changed):self.assertRaises(RegistryError,self.recover)
        self.assertEqual('COMPLETED',self.status());self.retained();self.no_retry()

    def test_outer_resource_exit_expiry_preserves_commit_and_releases_lock(self):
        original=self.runtime.registry.resource_use;entries=0
        @contextmanager
        def late():
            nonlocal entries
            entries+=1;outer=entries==1
            with original():yield
            if outer:self.f.clock=20
        with patch.object(self.runtime.registry,'resource_use',side_effect=late):self.assertRaises(RegistryError,self.recover)
        self.assertEqual('COMPLETED',self.status());self.assertFalse(self.frame[5].locked())
        self.retained();self.no_retry()

    def concurrent(self,same):
        entered=threading.Event();resume=threading.Event()
        def paused(h):
            if h==1901:
                entered.set()
                if not resume.wait(10):raise AssertionError('再開待ち')
        self.hook=paused
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.recover);self.assertTrue(entered.wait(10))
            second=pool.submit(self.recover,operation_id='close-next-worker' if same else 'other-next-worker')
            try:
                with self.assertRaises(RegistryError) as error:second.result(10)
                self.assertEqual('RECOVERY_REQUIRED',error.exception.code)
                self.case.case.case.commit(True)
            finally:resume.set()
            self.assertEqual('OFFLINE_NEXT_WORKER_CLOSE_COMPLETED',first.result(10)['status'])
        self.assertEqual([1901,1902],self.effects);self.assertEqual('COMPLETED',self.status());self.retained()

    def test_same_id_concurrent_request_does_not_repeat_native_close(self):self.concurrent(True)

    def test_different_id_concurrent_request_does_not_repeat_native_close(self):self.concurrent(False)

    def test_concurrent_request_before_claim_cannot_take_the_same_frame(self):
        entered=threading.Event();resume=threading.Event();original=self.offline.observe_worker_released;first=True
        def paused(*args,**kwargs):
            nonlocal first
            result=original(*args,**kwargs)
            if first:
                first=False;entered.set()
                if not resume.wait(10):raise AssertionError('claim前の再開待ち')
            return result
        with patch.object(self.offline,'observe_worker_released',side_effect=paused),ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(self.recover);self.assertTrue(entered.wait(10))
            two=pool.submit(self.recover,operation_id='concurrent-next-close')
            try:
                with self.assertRaises(RegistryError):two.result(10)
            finally:resume.set()
            self.assertEqual('OFFLINE_NEXT_WORKER_CLOSE_COMPLETED',one.result(10)['status'])
        self.assertEqual([1901,1902],self.effects);self.retained()


class SingleWorkerCloseTests(unittest.TestCase):
    def test_single_worker_has_no_next_worker_to_adopt(self):
        other=recovery.WorkerPipeRecoveryTests();other.setUp();self.addCleanup(other.doCleanups)
        other.prepared();other.recover()
        other.runtime._offline_observe_worker_released(other.token,operation_id='one-worker-released',
            open_operation_id=other.case.opened,pipe_operation_id='recover-worker-pipe',deadline=20)
        self.assertRaises(RegistryError,other.runtime._offline_recover_next_worker_close,other.token,
            operation_id='no-next',open_operation_id=other.case.opened,pipe_operation_id='recover-worker-pipe',
            observation_operation_id='one-worker-released',worker_operation_id='missing',deadline=20)
        other.retained()

