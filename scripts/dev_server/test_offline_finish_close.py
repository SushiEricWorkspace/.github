"""offline終端の正本・退避保持・旧start遮断を実Registryで確認する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import Mock, patch

from . import offline_control, runtime_control
from . import test_offline_writer_closed as closed
from .build_inputs import canonical, digest
from .registry import RegistryError


class OfflineFinishCloseTests(unittest.TestCase):
    def setUp(self):
        self.base=closed.OfflineWriterClosedTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.record();self.configure()

    def configure(self):
        self.f,self.c,self.runtime,self.token=self.base.f,self.base.c,self.base.runtime,self.base.token
        self.workers=self.base.workers
        self.before=deepcopy(self.f.saved())
        self.opened=json.loads(self.c.request)
        self.offline=self.runtime._offline
        self.callback=self.c.verify_owner
        self.history=self.offline.history.get(self.token['instanceId'],[])
        self.resources=[(a,a.process,a.pipe,a.creation_resources) for a in (self.c,*self.workers)]
        self.snapshot=self.offline._release_snapshot(self.c,self.workers,self.history)

    def finish(self,**changes):
        args=dict(operation_id=self.f.close_operation,deadline=self.f.deadline);args.update(changes)
        return self.runtime._offline_finish_close(self.token,**args)

    def state(self):
        with self.runtime._transaction(self.token['instanceId'],read_only=True) as db:
            row=db.execute('SELECT state,pending FROM instances WHERE id=?',(self.token['instanceId'],)).fetchone()
            operation=db.execute('SELECT status FROM runtime_operations WHERE id=?',(self.opened['operationId'],)).fetchone()
            refs=db.execute('SELECT count(*) FROM reservations WHERE operation=?',(self.opened['operationId'],)).fetchone()[0]
        return tuple(row),operation[0],refs

    def completed(self):
        self.assertEqual((('LEASED_STOPPED',0),'COMPLETED',0),self.state())
        self.assertEqual(self.before,{k:v for k,v in self.f.saved().items() if k!='termination'})
        slot=self.offline.retired[(self.token['instanceId'],self.opened['operationId'])]
        self.assertIs(self.c,slot['controller']);self.assertIs(self.callback,self.c.verify_owner)
        if self.history:self.assertIs(self.history,slot['history'])
        else:self.assertEqual([],slot['history'])
        self.assertTrue(all(a is b for a,b in zip(self.workers,slot['workers'])))
        for actor,process,pipe,resources in self.resources:
            self.assertIs(process,actor.process);self.assertIs(pipe,actor.pipe);self.assertIs(resources,actor.creation_resources)
        self.assertEqual(self.snapshot,self.offline._release_snapshot(self.c,self.workers,self.history))
        for mapping in (self.offline.controllers,self.offline.workers,self.offline.history):
            self.assertNotIn(self.token['instanceId'],mapping)
        self.assertEqual('COMPLETED',self.f.saved()['termination']['state'])
        self.assertEqual(digest(self.c.termination_response),self.f.saved()['termination']['responseSha256'])

    def rejected(self):
        with self.assertRaises(RegistryError):self.finish()
        self.assertEqual('PENDING',self.state()[1])
        self.assertNotIn('termination',self.f.saved())

    def test_terminates_original_open_and_reservation_retaining_objects_and_raw(self):
        try:result=self.finish()
        except RegistryError as error:self.fail('正常終端が拒否されました: '+error.code)
        self.assertEqual('OFFLINE_SESSION_COMPLETED',result['status'])
        self.assertEqual(digest(self.c.writer_closed_response),result['writerClosedSha256'])
        self.assertEqual(canonical(result),self.c.termination_response)
        self.assertEqual(result,self.c.record['termination']);self.completed()
        result['status']='UNKNOWN';self.assertEqual('OFFLINE_SESSION_COMPLETED',self.c.record['termination']['status'])

    def test_history_is_retained_with_all_original_workers(self):
        self.base.base.history();self.base.base.release();self.base.configure();self.base.record();self.configure()
        self.assertEqual(2,len(self.workers));self.finish();self.completed()

    def test_without_worker_can_terminate(self):
        self.base.test_open_without_worker_records_closed();self.configure()
        self.assertEqual([],self.workers);self.finish();self.completed()

    def test_game_source_uses_close_deadline_and_keeps_callback(self):
        case=self.base.game();case.clock,case.deadline=35,50
        self.base.record();self.configure();self.finish();self.completed()

    def test_close_deadline_cannot_be_extended(self):
        self.f.clock,self.f.deadline=10,20;self.rejected()

    def test_shorter_caller_deadline_is_used(self):
        self.f.clock,self.f.deadline=5,5;self.rejected()

    def test_closed_hash_required(self):
        saved=self.f.saved();saved['writerClosed']['responseSha256']='0'*64;self.f.save(saved);self.rejected()

    def test_closed_state_required(self):
        saved=self.f.saved();saved['writerClosed']['state']='UNKNOWN';self.f.save(saved);self.rejected()

    def test_closed_raw_required(self):self.c.writer_closed_response=None;self.rejected()

    def test_closed_local_record_required(self):
        self.c.record['writerClosed']['writerStatus']='UNKNOWN';self.rejected()

    def test_owner_release_hash_required(self):
        saved=self.f.saved();saved['ownerRelease']['responseSha256']='0'*64;self.f.save(saved);self.rejected()

    def test_worker_release_hash_required(self):
        saved=self.f.saved();saved['workerRelease']['responseSha256']='0'*64;self.f.save(saved);self.rejected()

    def test_worker_progress_required(self):
        next(iter(self.c.worker_release.values()))['pipe']='UNKNOWN';self.rejected()

    def test_parent_pipe_must_be_closed(self):self.workers[0].pipe.output=9993;self.rejected()

    def test_original_exit_raw_required(self):self.workers[0].exit_response+=b' ';self.rejected()

    def test_original_creation_binding_required(self):
        self.c.record['creation']['ownerGroup']='changed';self.rejected()

    def test_released_processes_are_not_polled_or_closed_again(self):
        for actor in (self.c,*self.workers):
            for name in ('poll','members','close'):
                self.enterContext(patch.object(actor.process,name,side_effect=AssertionError('終了済み資源の再作用')))
        self.finish();self.completed()

    def serialization(self,intent=False,change=None):
        original=json.dumps
        transaction=self.runtime._transaction
        final_updates=[]
        @contextmanager
        def traced(instance,**kwargs):
            with transaction(instance,**kwargs) as db:
                db.set_trace_callback(lambda sql:final_updates.append(sql)
                    if sql.startswith("UPDATE runtime_operations SET status='COMPLETED'") else None)
                yield db
        def changed(value,*args,**kwargs):
            result=original(value,*args,**kwargs)
            if isinstance(value,dict) and 'termination' in value and ('state' not in value['termination'])==intent:
                if change:change()
                else:self.f.clock=10
            return result
        with patch.object(offline_control.json,'dumps',side_effect=changed),patch.object(self.runtime,'_transaction',side_effect=traced):
            with self.assertRaises(RegistryError):self.finish()
        self.assertEqual([],final_updates,'直列化後の不一致・期限超過で終端SQLを発行してはいけません')
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.state())
        self.assertNotIn('state',self.f.saved().get('termination',{}))
        if intent:self.assertNotIn('termination',self.f.saved())

    def test_intent_json_deadline_rolls_back(self):self.serialization(True)
    def test_final_json_deadline_rolls_back(self):self.serialization()
    def test_final_json_rechecks_release_progress(self):
        self.serialization(change=lambda:next(iter(self.c.worker_release.values())).update(handle='UNKNOWN'))
    def test_final_json_rechecks_creation(self):
        self.serialization(change=lambda:self.c.record['creation'].update(ownerGroup='changed'))
    def test_final_json_rechecks_raw(self):
        self.serialization(change=lambda:setattr(self.c,'termination_response',b'{}'))
    def test_final_json_rechecks_local_record(self):
        self.serialization(change=lambda:self.c.record['termination'].update(status='UNKNOWN'))

    def test_journal_failure_retains_intent_and_originals(self):
        with patch.object(self.c,'_record',side_effect=OSError('fixture 記録失敗')):
            with self.assertRaises(OSError):self.finish()
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.state())
        self.assertIs(self.c,self.offline.retired[(self.token['instanceId'],self.opened['operationId'])]['controller'])
        with self.assertRaises(RegistryError):self.finish()

    def test_journal_deadline_does_not_terminate(self):
        original=self.c._record
        def late():original();self.f.clock=10
        with patch.object(self.c,'_record',side_effect=late):
            with self.assertRaises(RegistryError):self.finish()
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.state())

    def journal_not_started(self):
        with patch.object(self.c,'_record',wraps=self.c._record) as record:
            with self.assertRaises(RegistryError) as error:self.finish()
        self.assertEqual('STOP_TIMEOUT',error.exception.code)
        record.assert_not_called()
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.state())
        self.assertNotIn('state',self.f.saved()['termination'])
        self.assertEqual(self.before,{k:v for k,v in self.f.saved().items() if k!='termination'})
        slot=self.offline.retired[(self.token['instanceId'],self.opened['operationId'])]
        self.assertIs(self.c,slot['controller']);self.assertIs(self.callback,self.c.verify_owner)
        self.assertEqual(canonical(slot['result']),self.c.termination_response)
        self.assertEqual(self.snapshot,self.offline._release_snapshot(self.c,self.workers,self.history))

    def test_second_snapshot_deadline_does_not_start_journal(self):
        original=self.offline._release_snapshot
        calls=0
        def late(*args):
            nonlocal calls
            result=original(*args);calls+=1
            if calls==2:self.f.clock=self.f.deadline
            return result
        with patch.object(self.offline,'_release_snapshot',side_effect=late):self.journal_not_started()

    def test_read_transaction_exit_deadline_does_not_start_journal(self):
        original=self.runtime._transaction
        with patch.object(self.offline,'_release_snapshot',wraps=self.offline._release_snapshot) as snapshot:
            @contextmanager
            def late(instance,**kwargs):
                with original(instance,**kwargs) as db:yield db
                if kwargs.get('read_only') and snapshot.call_count==2:self.f.clock=self.f.deadline
            with patch.object(self.runtime,'_transaction',side_effect=late):self.journal_not_started()

    def test_result_copy_deadline_does_not_start_journal(self):
        original=offline_control.deepcopy
        def late(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_SESSION_COMPLETED':self.f.clock=self.f.deadline
            return result
        with patch.object(offline_control,'deepcopy',side_effect=late):self.journal_not_started()

    def test_final_sql_rollback_keeps_retired_objects_and_pending_reservation(self):
        original=self.runtime._transaction
        @contextmanager
        def failed(instance,**kwargs):
            with original(instance,**kwargs) as db:
                yield db
                if not kwargs.get('read_only') and db.execute('SELECT status FROM runtime_operations WHERE id=?',
                        (self.opened['operationId'],)).fetchone()[0]=='COMPLETED':
                    raise OSError('fixture commit前失敗')
        with patch.object(self.runtime,'_transaction',side_effect=failed):
            with self.assertRaises(OSError):self.finish()
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.state())
        self.assertIs(self.c,self.offline.retired[(self.token['instanceId'],self.opened['operationId'])]['controller'])
        self.assertNotIn(self.token['instanceId'],self.offline.controllers)
        with self.assertRaises(RegistryError):self.finish()

    def test_post_sql_deadline_rolls_back_and_retains_evidence(self):
        original=self.offline._terminal
        def late(*args):self.f.clock=10;return original(*args)
        with patch.object(self.offline,'_terminal',side_effect=late):
            with self.assertRaises(RegistryError):self.finish()
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.state())
        self.assertNotIn('state',self.f.saved()['termination'])
        self.assertIs(self.c,self.offline.retired[(self.token['instanceId'],self.opened['operationId'])]['controller'])

    def test_commit_deadline_preserves_terminal_without_normal_return(self):
        original=self.runtime._transaction
        @contextmanager
        def late(instance,**kwargs):
            with original(instance,**kwargs) as db:yield db
            if not kwargs.get('read_only') and self.state()[1]=='COMPLETED':self.f.clock=10
        with patch.object(self.runtime,'_transaction',side_effect=late):
            with self.assertRaises(RegistryError):self.finish()
        self.completed()

    def test_return_copy_deadline_preserves_terminal_without_return(self):
        original=offline_control.deepcopy
        def late(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_SESSION_COMPLETED' and self.state()[1]=='COMPLETED':
                self.f.clock=10
            return result
        with patch.object(offline_control,'deepcopy',side_effect=late):
            with self.assertRaises(RegistryError):self.finish()
        self.completed()

    def after_commit(self,change):
        original=self.runtime._transaction
        done=False
        @contextmanager
        def changed(instance,**kwargs):
            nonlocal done
            with original(instance,**kwargs) as db:yield db
            if not done and not kwargs.get('read_only') and self.state()[1]=='COMPLETED':
                done=True;change()
        with patch.object(self.runtime,'_transaction',side_effect=changed):
            with self.assertRaises(RegistryError):self.finish()
        self.assertEqual('COMPLETED',self.state()[1])

    def test_after_commit_rechecks_original_object(self):
        self.after_commit(lambda:setattr(self.workers[0],'creation_resources',tuple(list(self.workers[0].creation_resources))))

    def test_after_commit_rechecks_original_raw(self):
        self.after_commit(lambda:setattr(self.workers[0],'exit_response',self.workers[0].exit_response+b' '))

    def test_after_commit_rechecks_released_pipe(self):
        self.after_commit(lambda:setattr(self.c.pipe,'input',9994))

    def test_after_commit_rechecks_worker_controller_binding(self):
        self.after_commit(lambda:setattr(self.workers[0],'controller',None))
        self.assertIsNone(self.workers[0].controller);self.completed()

    def test_after_commit_rechecks_history_worker_controller_binding(self):
        self.base.base.history();self.base.base.release();self.base.configure();self.base.record();self.configure()
        self.assertEqual(2,len(self.workers))
        self.after_commit(lambda:setattr(self.workers[0],'controller',None))
        self.assertIsNone(self.workers[0].controller);self.assertIs(self.c,self.workers[1].controller)
        self.completed()

    def test_after_commit_rechecks_registry_proof(self):
        def changed():
            saved=self.f.saved();saved['workerRelease']['responseSha256']='1'*64;self.f.save(saved)
        self.after_commit(changed)

    def test_original_pending_callback_is_never_used_after_commit(self):
        original=self.c.verify_owner
        def verify():
            self.assertEqual('PENDING',self.state()[1]);original()
        self.c.verify_owner=verify;self.callback=verify
        self.finish();self.completed()

    def start_blocked(self,runtime=None,operation='start-after-offline'):
        runtime=runtime or self.runtime
        with patch.object(runtime,'refresh'),patch.object(runtime,'_active',side_effect=AssertionError('旧承認でstart準備')):
            with self.assertRaises(RegistryError) as error:
                runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id=operation)
        self.assertEqual('CONTROL_BLOCKED',error.exception.code)

    def test_terminal_persistent_proof_blocks_old_start_before_preflight(self):
        self.finish();self.start_blocked();self.completed()

    def test_new_runtime_without_offline_memory_still_blocks_start(self):
        self.finish()
        fresh=runtime_control.RuntimeControl(self.runtime.registry,process_adapter=self.runtime.process_adapter)
        self.assertIsNone(fresh._offline);self.start_blocked(fresh);self.completed()

    def test_dry_run_also_requires_independent_ready_gate(self):
        self.finish()
        with self.assertRaises(RegistryError) as error:
            self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='dry-after-offline',dry_run=True)
        self.assertEqual('CONTROL_BLOCKED',error.exception.code)

    def test_intent_and_terminal_compete_with_start_in_actual_registry(self):
        entered,leave=threading.Event(),threading.Event();original=self.c._record
        def paused():entered.set();self.assertTrue(leave.wait(30));original()
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        with patch.object(self.c,'_record',side_effect=paused),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.finish)
            try:
                self.assertTrue(entered.wait(30))
                with self.assertRaises(RegistryError):self.finish()
                with self.assertRaises(RegistryError):
                    self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='start-during-end')
            finally:leave.set()
            self.assertEqual('OFFLINE_SESSION_COMPLETED',future.result(30)['status'])
        self.start_blocked();self.completed()

    def test_registry_change_during_journal_prevents_terminal(self):
        entered,leave=threading.Event(),threading.Event();original=self.c._record
        def paused():entered.set();self.assertTrue(leave.wait(30));original()
        with patch.object(self.c,'_record',side_effect=paused),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.finish)
            try:
                self.assertTrue(entered.wait(30))
                saved=self.f.saved();saved['writerClosed']['state']='UNKNOWN';self.f.save(saved)
            finally:leave.set()
            with self.assertRaises(RegistryError):future.result(30)
        self.assertEqual('PENDING',self.state()[1]);self.assertNotIn('state',self.f.saved()['termination'])

    def test_inflight_old_approval_is_rechecked_in_final_start_transaction(self):
        entered,leave=threading.Event(),threading.Event()
        original=runtime_control.RuntimeControl._offline_open
        pool=ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown,wait=True)
        self.addCleanup(leave.set)
        futures=[]
        def opening(runtime,token,**kwargs):
            runtime.managed.approved=Mock()
            runtime.managed.sessions={}
            self.enterContext(patch.object(runtime,'_space',side_effect=lambda:
                (entered.set(),self.assertTrue(leave.wait(30)))))
            generated=self.enterContext(patch.object(runtime,'_start',side_effect=AssertionError('旧承認でOS生成')))
            def start():
                try:runtime.execute('start',dict(token,timeoutSeconds=10),operation_id='inflight-old-start')
                except RegistryError as error:return error.code
                return 'ALLOWED'
            futures.append((pool.submit(start),generated))
            self.assertTrue(entered.wait(30),'旧run承認からのpreflightに到達していません')
            runtime.managed.approved.assert_called_once()
            self.assertIs(runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
            return original(runtime,token,**kwargs)
        fixture=closed.OfflineWriterClosedTests()
        self.addCleanup(fixture.doCleanups)
        try:
            with patch.object(runtime_control.RuntimeControl,'_offline_open',opening):fixture.setUp()
            self.base=fixture;self.base.record();self.configure()
            self.finish();self.completed()
        finally:leave.set()
        future,generated=futures[0]
        self.assertEqual('CONTROL_BLOCKED',future.result(30));generated.assert_not_called()
        self.completed()

    def test_no_retry_after_terminal_or_prepare_with_old_c(self):
        self.finish()
        for operation in (self.f.close_operation,'other-terminal'):
            with self.assertRaises(RegistryError):self.finish(operation_id=operation)
        with self.assertRaises(RegistryError):self.f.prepare_next()
        self.completed()


if __name__=='__main__':unittest.main()
