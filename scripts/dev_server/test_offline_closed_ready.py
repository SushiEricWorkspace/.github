"""独立Readyと元CLOSED終端の一致証拠を実Registryで確認する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from . import offline_control, runtime_control
from . import test_offline_finish_close as finish
from .build_inputs import digest
from .registry import RegistryError


class ClosedReadyTests(unittest.TestCase):
    def setUp(self):
        self.base=finish.OfflineFinishCloseTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.finish()
        self.f,self.c,self.runtime,self.token=self.base.f,self.base.c,self.base.runtime,self.base.token
        self.opened=self.base.opened
        response=json.loads(self.c.close_response)
        self.ready={key:response[key] for key in ('runUuid','stateSha256','stateRevision')}
        self.ready['worldUuid']=self.opened['worldUuid']
        self.before=deepcopy(self.f.saved())

    def check(self,**changes):
        args=dict(open_operation_id=self.opened['operationId'],ready=self.ready,deadline=self.f.deadline)
        args.update(changes)
        return self.runtime._offline_check_closed_ready(self.token,**args)

    def rejected(self,**changes):
        with self.assertRaises(RegistryError):self.check(**changes)
        self.assertEqual(self.before,self.f.saved())
        self.base.completed()

    def test_explicit_ready_matches_closed_terminal_without_mutation(self):
        result=self.check()
        self.assertEqual('OFFLINE_CLOSED_READY_VERIFIED',result['status'])
        self.assertEqual(self.ready,result['ready'])
        self.assertEqual(self.token['instanceId'],result['instanceId'])
        self.assertEqual(self.token['expectedGeneration'],result['generationId'])
        self.assertEqual(self.opened['operationId'],result['openOperationId'])
        self.assertEqual(digest(self.c.termination_response),result['terminationSha256'])
        self.assertEqual(digest(self.c.writer_closed_response),result['writerClosedSha256'])
        result['ready']['stateRevision']+=1
        self.assertEqual(self.ready,self.check()['ready'])
        self.assertEqual(self.before,self.f.saved());self.base.completed()

    def test_different_ready_hash_is_rejected(self):
        self.rejected(ready=dict(self.ready,stateSha256='0'*64))

    def test_caller_ready_is_frozen_before_result_copy(self):
        expected=deepcopy(self.ready)
        original=offline_control.deepcopy
        def changed(value):
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSED_READY_VERIFIED':
                self.ready['stateSha256']='0'*64
            return original(value)
        with patch.object(offline_control,'deepcopy',side_effect=changed):
            self.assertEqual(expected,self.check()['ready'])
        self.assertEqual(self.before,self.f.saved())

    def input_copy_race(self,change):
        entered,leave=threading.Event(),threading.Event()
        original=offline_control.deepcopy
        def paused(value):
            if isinstance(value,tuple) and len(value)==2 and value[0] is self.token and value[1] is self.ready:
                entered.set();self.assertTrue(leave.wait(30))
            return original(value)
        with patch.object(offline_control,'deepcopy',side_effect=paused),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.check)
            try:
                self.assertTrue(entered.wait(30));change()
            finally:leave.set()
            with self.assertRaises(RegistryError) as error:future.result(30)
        self.assertEqual('INVALID_INPUT',error.exception.code)
        self.assertEqual(self.before,self.f.saved());self.base.completed();self.base.start_blocked()

    def test_ready_cleared_before_initial_copy_is_rejected(self):
        self.input_copy_race(self.ready.clear)

    def test_float_revision_before_initial_copy_is_rejected(self):
        self.input_copy_race(lambda:self.ready.update(stateRevision=float(self.ready['stateRevision'])))

    def test_invalid_terminal_is_rejected_before_result_copy(self):
        saved=self.f.saved();saved['writerClosed']['state']='UNKNOWN';self.f.save(saved)
        copied=[]
        original=offline_control.deepcopy
        def observed(value):
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSED_READY_VERIFIED':copied.append(value)
            return original(value)
        with patch.object(offline_control,'deepcopy',side_effect=observed):
            with self.assertRaises(RegistryError):self.check()
        self.assertEqual([],copied)
        self.assertEqual(saved,self.f.saved())

    def test_stale_token_is_rejected(self):
        original=self.token
        for key,value in (('leaseId','other-lease'),('epoch',original['epoch']+1),
                          ('expectedGeneration','12345678-1234-4234-8234-123456789abc')):
            with self.subTest(key=key):
                self.token=dict(original,**{key:value})
                self.rejected()
        self.token=original

    def test_different_ready_revision_is_rejected(self):
        self.rejected(ready=dict(self.ready,stateRevision=self.ready['stateRevision']+1))

    def test_different_world_is_rejected(self):
        self.rejected(ready=dict(self.ready,worldUuid='12345678-1234-4234-8234-123456789abc'))

    def test_different_data_run_is_rejected(self):
        self.rejected(ready=dict(self.ready,runUuid='12345678-1234-4234-8234-123456789abc'))

    def test_missing_ready_is_not_inferred(self):self.rejected(ready={})

    def test_extra_ready_field_is_rejected(self):self.rejected(ready=dict(self.ready,approved=True))

    def test_boolean_revision_is_rejected(self):self.rejected(ready=dict(self.ready,stateRevision=True))

    def test_out_of_range_revision_is_rejected(self):
        for value in (-1,9223372036854775808):
            with self.subTest(value=value):self.rejected(ready=dict(self.ready,stateRevision=value))

    def test_wrong_open_operation_is_rejected(self):self.rejected(open_operation_id='other-offline-open')

    def test_failed_terminal_update_keeps_incomplete_slot_and_is_rejected(self):
        case=finish.OfflineFinishCloseTests()
        case.setUp();self.addCleanup(case.doCleanups)
        transaction=case.runtime._transaction
        self.assertIs(transaction.__func__,runtime_control.RuntimeControl._transaction)
        denied=[]
        def authorize(action,table,*_):
            if action==sqlite3.SQLITE_UPDATE and table=='runtime_operations':
                denied.append(table);return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        @contextmanager
        def blocked(instance,**kwargs):
            with transaction(instance,**kwargs) as db:
                db.set_authorizer(authorize)
                try:yield db
                finally:db.set_authorizer(None)
        with patch.object(case.runtime,'_transaction',side_effect=blocked):
            with self.assertRaises(sqlite3.DatabaseError):case.finish()
        self.assertEqual(['runtime_operations'],denied)
        key=(case.token['instanceId'],case.opened['operationId'])
        slot=case.offline.retired[key]
        self.assertNotIn('encoded',slot);self.assertNotIn('result',slot)
        response=json.loads(case.c.close_response)
        ready={name:response[name] for name in ('runUuid','stateSha256','stateRevision')}
        ready['worldUuid']=case.opened['worldUuid']
        try:
            case.runtime._offline_check_closed_ready(case.token,open_operation_id=case.opened['operationId'],
                                                      ready=ready,deadline=case.f.deadline)
        except RegistryError as error:self.assertEqual('PROOF_REQUIRED',error.code)
        except Exception as error:self.fail('未完成slotの拒否で未検査キーを参照しました: '+type(error).__name__)
        else:self.fail('未完成slotから正常証拠を返しました')
        self.assertIs(slot,case.offline.retired[key]);self.assertIs(case.c,case.offline.controllers[key[0]])
        self.assertEqual(case.before,case.f.saved())
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),case.state())
        self.assertEqual(case.snapshot,case.offline._release_snapshot(case.c,case.workers,case.history))

    def test_retired_object_required_without_reconstruction(self):
        fresh=runtime_control.RuntimeControl(self.runtime.registry,process_adapter=self.runtime.process_adapter)
        with self.assertRaises(RegistryError):
            fresh._offline_check_closed_ready(self.token,open_operation_id=self.opened['operationId'],
                                               ready=self.ready,deadline=self.f.deadline)
        self.assertIsNone(fresh._offline);self.base.completed()

    def test_new_call_deadline_does_not_extend_old_close(self):
        self.f.clock=11
        self.assertLess(self.c.close_binding['deadline'],self.f.clock)
        self.assertEqual(self.ready,self.check(deadline=20)['ready'])
        self.assertEqual(10,self.c.close_binding['deadline']);self.base.completed()

    def test_expired_deadline_is_rejected(self):self.f.clock=self.f.deadline;self.rejected()

    def test_worker_controller_binding_is_rechecked(self):
        self.base.workers[0].controller=None
        with self.assertRaises(RegistryError):self.check()
        self.assertEqual(self.before,self.f.saved())

    def test_no_reobserve_reclose_or_pending_callback(self):
        for actor in (self.c,*self.base.workers):
            for name in ('poll','members','close'):
                self.enterContext(patch.object(actor.process,name,side_effect=AssertionError('終了資源への再作用')))
        self.enterContext(patch.object(self.c,'verify_owner',side_effect=AssertionError('旧PENDING callback')))
        self.enterContext(patch.object(self.c,'_record',side_effect=AssertionError('局所記録の変更')))
        self.check();self.assertEqual(self.before,self.f.saved())

    def test_old_start_remains_blocked_after_evidence(self):
        self.check();self.base.start_blocked();self.base.completed()

    def test_second_transaction_rejects_concurrent_registry_change(self):
        entered,leave=threading.Event(),threading.Event()
        original=offline_control.deepcopy
        def paused(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSED_READY_VERIFIED':
                entered.set();self.assertTrue(leave.wait(30))
            return result
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        with patch.object(offline_control,'deepcopy',side_effect=paused),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.check)
            try:
                self.assertTrue(entered.wait(30))
                saved=self.f.saved();saved['writerClosed']['state']='UNKNOWN';self.f.save(saved)
            finally:leave.set()
            with self.assertRaises(RegistryError):future.result(30)
        self.assertEqual('UNKNOWN',self.f.saved()['writerClosed']['state'])
        self.assertEqual((('LEASED_STOPPED',0),'COMPLETED',0),self.base.state())

    def test_final_transaction_rechecks_worker_binding(self):
        original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSED_READY_VERIFIED':
                self.base.workers[0].controller=None
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):
            with self.assertRaises(RegistryError):self.check()
        self.assertEqual(self.before,self.f.saved())

    def test_copy_expiry_is_rejected(self):
        original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSED_READY_VERIFIED':self.f.clock=self.f.deadline
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):self.rejected()

    def test_transaction_exit_expiry_is_rejected(self):
        original=self.runtime._transaction
        reads=[]
        @contextmanager
        def changed(instance,**kwargs):
            with original(instance,**kwargs) as connection:yield connection
            if kwargs.get('read_only'):
                reads.append(True)
                if len(reads)==2:self.f.clock=self.f.deadline
        with patch.object(self.runtime,'_transaction',side_effect=changed):self.rejected()


if __name__=='__main__':unittest.main()
