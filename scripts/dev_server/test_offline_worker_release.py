"""全Wの生成資源・原本・期限と部分失敗を実Registry transactionで検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy, deepcopy
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

from . import database_proxy_pipe, offline_control
from . import test_offline_owner_release as owner
from . import test_offline_repeat as repeated
from . import test_offline_prepare as prepared
from .build_inputs import canonical, digest
from .registry import RegistryError


class OfflineWorkerReleaseTests(unittest.TestCase):
    def setUp(self):
        self.base=owner.OfflineOwnerReleaseTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.release()
        self.configure()

    def configure(self):
        self.f=self.base.f
        self.runtime,self.token,self.c=self.f.runtime,self.f.token,self.f.c
        self.workers=[w for w,_ in self.runtime._offline.history.get(self.token['instanceId'],[])]
        latest=self.runtime._offline.workers.get(self.token['instanceId'])
        if latest is not None:self.workers.append(latest)
        self.before=deepcopy(self.f.saved())
        self.originals=[(w.request,w.response,w.exit_response,w.process,w.pipe) for w in self.workers]
        self.order=[]
        self.failure=None
        self.after=False
        self.effect=lambda worker,role:None
        self.fds={}
        for index,w in enumerate(self.workers):
            w.process.close=Mock(side_effect=lambda i=index:self.close(i,'handle'))
            for role in ('input','output'):self.fds[getattr(w.pipe,role)]=(index,role)
            self.enterContext(patch.object(w.pipe,'close_after_exit',side_effect=lambda p=w.pipe,**kw:
                database_proxy_pipe.ProxyPipe.close_after_exit(p,**kw)))
        original=os.close
        def close_fd(fd):
            if fd not in self.fds:return original(fd)
            return self.close(*self.fds[fd])
        self.enterContext(patch.object(database_proxy_pipe.os,'close',side_effect=close_fd))

    def close(self,index,role):
        if self.failure==(index,role) and not self.after:raise OSError('fixture 解放前の失敗')
        self.order.append((index,role))
        self.effect(index,role)
        if self.failure==(index,role):raise OSError('fixture 部分解放の不明')

    def release(self,**changes):
        args=dict(operation_id=self.f.close_operation,deadline=self.f.deadline)
        args.update(changes)
        return self.runtime._offline_release_worker_handles(self.token,**args)

    def pending(self):
        self.f.prepared.pending()
        self.assertEqual(self.before,{k:v for k,v in self.f.saved().items() if k!='workerRelease'})
        self.assertEqual(self.originals,[(w.request,w.response,w.exit_response,w.process,w.pipe) for w in self.workers])

    def no_retry(self):
        self.f.clock=0
        before=deepcopy(self.f.saved()),list(self.order)
        for op in (self.f.close_operation,'other-worker-release'):
            with self.assertRaises(RegistryError):self.release(operation_id=op)
        with self.assertRaises(RegistryError):self.f.close()
        with self.assertRaises(RegistryError):self.f.prepare_next()
        with self.assertRaises(RegistryError):
            self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='start-after-w-release')
        self.assertEqual(before,(self.f.saved(),self.order))

    def normal(self):
        result=self.release()
        self.assertEqual('WORKER_HANDLES_RELEASED',result['status'])
        self.assertEqual([(i,r) for i in range(len(self.workers)) for r in ('handle','input','output')],self.order)
        self.assertEqual(digest(self.c.owner_release_response),result['ownerReleaseSha256'])
        self.assertEqual(len(self.workers),len(result['workers']))
        for w,binding in zip(self.workers,result['workers']):
            self.assertEqual(digest(w.exit_response),binding['workerExitSha256'])
            self.assertEqual({'handle':'RELEASED','pipe':'RELEASED'},self.c.worker_release[binding['operationId']])
            self.assertEqual((None,None),(w.pipe.input,w.pipe.output))
        self.assertEqual(canonical(result),self.c.worker_release_response)
        self.assertEqual(result,self.c.record['workerRelease'])
        self.assertEqual(digest(canonical(result)),self.f.saved()['workerRelease']['responseSha256'])
        result['workers'].clear();result['handlesReleased']=False
        self.assertTrue(self.c.record['workerRelease']['handlesReleased'])
        self.assertEqual(len(self.workers),len(self.c.record['workerRelease']['workers']))
        self.pending();self.no_retry()

    def test_normal_release_keeps_original_reservation_and_proofs(self):self.normal()

    def history(self):
        fixture=repeated.OfflineRepeatTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.finish()
        self.base.base.f.configure(fixture,fixture.f.f.a.f.f,fixture.f.f.p,fixture.current,fixture.current)
        self.base.base.start_close();self.base.base.observe()
        self.base.configure(self.base.base.f);self.base.release();self.configure()

    def test_all_historical_and_latest_workers_release_in_order(self):
        self.history();self.assertEqual(2,len(self.workers));self.normal()

    def test_no_worker_records_empty_release_without_os_effect(self):
        fixture=prepared.OfflinePrepareTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        opened=json.loads(fixture.c.request)
        writer=deepcopy(self.f.writer);writer['owner']=deepcopy(fixture.c.data_identity)
        writer['management']={k:opened[k] for k in ('leaseId','epoch','generation','drainSha256')}
        writer['management']['controller']=deepcopy(fixture.c.parent_data)
        current=dict(phase='Idle1',hash='b'*64,revision=1,run=owner.exited.closing.processes.RUN,
            writer=writer,world=opened['worldUuid'],generation=opened['generation'])
        self.base.base.f.configure(fixture,fixture,fixture.p,current,current)
        self.base.base.start_close();self.base.base.observe()
        self.base.configure(self.base.base.f);self.base.release();self.configure()
        self.assertEqual([],self.workers);self.normal()

    def test_no_old_pid_or_group_reobservation(self):
        for w in self.workers:
            self.enterContext(patch.object(w.process,'poll',side_effect=AssertionError('W再観測')))
            self.enterContext(patch.object(w.process,'members',side_effect=AssertionError('W group再観測')))
            self.enterContext(patch.object(w,'verify',side_effect=AssertionError('終了W再照会')))
        self.normal()

    def rejected(self):
        with self.assertRaises(RegistryError):self.release()
        self.assertEqual([],self.order)
        self.assertNotIn('workerRelease',self.f.saved())

    def test_requires_original_owner_release_hash(self):
        saved=self.f.saved();saved['ownerRelease']['responseSha256']='0'*64;self.f.save(saved);self.rejected()

    def test_requires_owner_released_progress(self):
        self.c.owner_release['pipe']='UNKNOWN';self.rejected()

    def test_requires_owner_closed_pipe(self):
        self.c.pipe.output=9911;self.rejected()

    def test_requires_owner_release_original(self):
        self.c.owner_release_response=None;self.rejected()

    def test_original_worker_process_cannot_be_replaced(self):
        w=self.workers[0];w.process=copy(w.process);self.rejected()

    def test_original_worker_pipe_cannot_be_replaced(self):
        w=self.workers[0];w.pipe=copy(w.pipe);self.rejected()

    def test_worker_exit_nonzero_even_when_all_hashes_match(self):
        w=self.workers[0];result=json.loads(w.exit_response);result['exitCode']=1
        w.exit_response=canonical(result);w.record['workerExit']=result
        saved=self.f.saved();saved['workerExit']['responseSha256']=digest(w.exit_response);self.f.save(saved)
        self.rejected()

    def test_history_exit_requires_original_hash(self):
        self.history();w=self.workers[0];w.exit_response+=b' ';self.rejected()

    def test_history_generation_binding_cannot_change(self):
        self.history();w=self.workers[0];frame=json.loads(w.request);frame['epoch']+=1
        w.request=canonical(frame);self.rejected()

    def test_history_completion_ack_is_required(self):
        self.history();next(iter(self.c.operation_history.values()))['completion_acknowledged']=False;self.rejected()

    def test_history_exit_nonzero_with_matching_hashes_is_rejected(self):
        self.history();w=self.workers[0]
        result=json.loads(w.exit_response);result['exitCode']=1
        w.exit_response=canonical(result);w.record['workerExit']=result
        saved=self.f.saved();saved['history'][0]['workerExit']['responseSha256']=digest(w.exit_response)
        saved['history'][0]['completion']['workerExitSha256']=digest(w.exit_response)
        self.runtime._offline.history[self.token['instanceId']][0][1].update(deepcopy(saved['history'][0]))
        self.f.save(saved);self.rejected()

    def test_history_creation_identity_is_required(self):
        self.history();self.workers[0].record['identity']=dict(pid=999);self.rejected()

    def test_history_native_group_cannot_change(self):
        self.history();w=self.workers[0];w.process.group='different'
        result=json.loads(w.exit_response);result['ownerGroup']='different'
        w.exit_response=canonical(result);w.record['workerExit']=result
        saved=self.f.saved();saved['history'][0]['workerExit']['responseSha256']=digest(w.exit_response)
        saved['history'][0]['completion']['workerExitSha256']=digest(w.exit_response)
        self.runtime._offline.history[self.token['instanceId']][0][1].update(deepcopy(saved['history'][0]))
        self.f.save(saved);self.rejected()

    def test_history_original_assignment_body_is_required(self):
        self.history();held=next(iter(self.c.operation_history.values()))
        frame=json.loads(held['assignment_request']);frame['request']['requestId']='different'
        held['assignment_request']=canonical(frame);self.rejected()

    def test_history_request_body_cannot_change_with_assignment(self):
        self.history();w=self.workers[0];held=next(iter(self.c.operation_history.values()))
        frame=json.loads(w.request);frame['request']['action']['products'][0]['id']='different'
        w.request=canonical(frame)
        assigned=json.loads(held['assignment_request']);assigned['request']=deepcopy(frame['request'])
        held['assignment_request']=canonical(assigned);self.rejected()

    def test_history_startup_ack_hash_is_required(self):
        self.history();self.workers[0].record['startupAcknowledgedSha256']='0'*64;self.rejected()

    def test_history_idle_revision_cannot_change_with_assignment(self):
        self.history();w=self.workers[0];held=next(iter(self.c.operation_history.values()))
        for key in ('request','assignment_request'):
            frame=json.loads(w.request if key=='request' else held[key]);frame['expectedRevision']+=1
            if key=='request':w.request=canonical(frame)
            else:held[key]=canonical(frame)
        self.rejected()

    def test_game_source_uses_close_deadline_after_open_expiry(self):
        game=owner.game_tests.GameOwnerCloseDeadlineTests()
        game.setUp();self.addCleanup(game.doCleanups)
        game.open();game.edit(21)
        case=game.close_fixture(clock=31,deadline=40);case.close()
        case.runtime._offline_observe_owner_exit(case.token,operation_id=case.close_operation,deadline=40)
        self.base.configure(case);self.base.release();self.configure()
        case.clock,case.deadline=35,50
        original=self.c.verify_owner
        self.normal();self.assertIs(original,self.c.verify_owner)

    def test_game_source_close_deadline_cannot_be_extended(self):
        game=owner.game_tests.GameOwnerCloseDeadlineTests()
        game.setUp();self.addCleanup(game.doCleanups)
        game.open();game.edit(21)
        case=game.close_fixture(clock=31,deadline=40);case.close()
        case.runtime._offline_observe_owner_exit(case.token,operation_id=case.close_operation,deadline=40)
        self.base.configure(case);self.base.release();self.configure()
        case.clock,case.deadline=40,50;self.rejected()

    def test_deadline_cannot_be_extended(self):
        self.f.deadline=20;self.f.clock=10;self.rejected()

    def test_shorter_call_deadline(self):
        self.f.deadline=5;self.f.clock=5;self.rejected()

    def failed(self,role,after=False):
        self.failure=(0,role);self.after=after
        with self.assertRaises(OSError):self.release()
        state=next(iter(self.c.worker_release.values()))
        self.assertEqual('UNKNOWN',state['handle' if role=='handle' else 'pipe'])
        self.assertNotIn('responseSha256',self.f.saved()['workerRelease'])
        self.pending();self.no_retry()

    def test_handle_failure_before_effect(self):self.failed('handle')
    def test_handle_failure_after_effect(self):self.failed('handle',True)
    def test_input_failure_before_effect(self):self.failed('input')
    def test_input_failure_after_effect(self):self.failed('input',True)
    def test_output_failure_before_effect(self):
        self.failed('output');self.assertIsNone(self.workers[0].pipe.input)
    def test_output_failure_after_effect(self):self.failed('output',True)

    def deadline_after(self,role):
        self.f.deadline=20
        self.effect=lambda i,r:setattr(self.f,'clock',10) if r==role else None
        with self.assertRaises(RegistryError):self.release()
        roles=('handle','input','output');self.assertEqual([(0,r) for r in roles[:roles.index(role)+1]],self.order)
        self.pending();self.no_retry()

    def test_handle_deadline_before_pipe(self):self.deadline_after('handle')
    def test_each_fd_uses_close_deadline(self):self.deadline_after('input')
    def test_last_fd_deadline_prevents_success(self):self.deadline_after('output')

    def test_partial_history_failure_does_not_release_later_worker(self):
        self.history();self.failed('output')
        self.assertEqual(0,self.workers[1].process.close.call_count)

    def test_pipe_returning_with_fd_open_is_not_released(self):
        with patch.object(self.workers[0].pipe,'close_after_exit',return_value=None):
            with self.assertRaises(RegistryError):self.release()
        self.assertEqual('UNKNOWN',next(iter(self.c.worker_release.values()))['pipe'])
        self.no_retry()

    def serialized(self,intent=False,change=None):
        original=json.dumps
        def changed(value,*args,**kwargs):
            result=original(value,*args,**kwargs)
            if isinstance(value,dict) and 'workerRelease' in value and ('responseSha256' not in value['workerRelease'])==intent:
                if change:change()
                else:self.f.clock=10
            return result
        with patch.object(offline_control.json,'dumps',side_effect=changed):
            with self.assertRaises(RegistryError):self.release()
        if intent:self.assertEqual([],self.order)
        else:
            self.assertNotIn('responseSha256',self.f.saved()['workerRelease'])
            self.no_retry()

    def test_intent_json_deadline_has_no_effect(self):self.serialized(True)
    def test_result_json_deadline_has_no_success(self):self.serialized()
    def test_result_json_rechecks_originals(self):
        self.serialized(change=lambda:setattr(self.workers[0],'exit_response',self.workers[0].exit_response+b' '))
    def test_result_json_rechecks_progress(self):
        self.serialized(change=lambda:next(iter(self.c.worker_release.values())).update(handle='UNKNOWN'))
    def test_result_json_rechecks_raw_result(self):
        self.serialized(change=lambda:setattr(self.c,'worker_release_response',b'{}'))

    def test_local_journal_deadline_preserves_intent(self):
        original=self.c._record
        def late():original();self.f.clock=10
        with patch.object(self.c,'_record',side_effect=late):
            with self.assertRaises(RegistryError):self.release()
        self.assertNotIn('responseSha256',self.f.saved()['workerRelease']);self.no_retry()

    def test_local_journal_failure_preserves_intent_without_retry(self):
        with patch.object(self.c,'_record',side_effect=OSError('fixture 記録失敗')):
            with self.assertRaises(OSError):self.release()
        self.assertNotIn('responseSha256',self.f.saved()['workerRelease']);self.no_retry()

    def test_binding_change_after_handle_prevents_pipe(self):
        self.effect=lambda i,r:setattr(self.workers[0].process,'group','changed') if r=='handle' else None
        with self.assertRaises(RegistryError):self.release()
        self.assertEqual([(0,'handle')],self.order);self.no_retry()

    def test_return_copy_deadline_does_not_return_success(self):
        original=offline_control.deepcopy
        def late(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='WORKER_HANDLES_RELEASED' \
                    and 'responseSha256' in self.f.saved().get('workerRelease',{}):self.f.clock=10
            return result
        with patch.object(offline_control,'deepcopy',side_effect=late):
            with self.assertRaises(RegistryError):self.release()
        self.assertIn('responseSha256',self.f.saved()['workerRelease']);self.no_retry()

    def test_commit_deadline_keeps_known_result_without_normal_return(self):
        original=self.runtime._transaction
        @contextmanager
        def late(instance,**kwargs):
            with original(instance,**kwargs) as connection:yield connection
            if not kwargs.get('read_only') and 'responseSha256' in self.f.saved().get('workerRelease',{}):self.f.clock=10
        with patch.object(self.runtime,'_transaction',side_effect=late):
            with self.assertRaises(RegistryError):self.release()
        self.assertIn('responseSha256',self.f.saved()['workerRelease']);self.pending();self.no_retry()

    def test_concurrent_release_is_single_use_with_real_transactions(self):
        entered,leave=threading.Event(),threading.Event()
        def pause(i,r):
            if r=='handle':entered.set();self.assertTrue(leave.wait(30))
        self.effect=pause
        with ThreadPoolExecutor(max_workers=2) as pool:
            future=pool.submit(self.release)
            try:
                self.assertTrue(entered.wait(30))
                with self.assertRaises(RegistryError):self.release()
                with self.assertRaises(RegistryError):self.f.prepare_next()
                with self.assertRaises(RegistryError):
                    self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='racing-start-w')
            finally:leave.set()
            self.assertEqual('WORKER_HANDLES_RELEASED',future.result(30)['status'])
        self.assertEqual([(0,r) for r in ('handle','input','output')],self.order);self.pending()

    def test_registry_binding_change_while_handle_waits_prevents_pipe(self):
        entered,leave=threading.Event(),threading.Event()
        self.effect=lambda i,r:(entered.set(),leave.wait(30)) if r=='handle' else None
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.release)
            try:
                self.assertTrue(entered.wait(30))
                saved=self.f.saved();saved['ownerRelease']['responseSha256']='1'*64;self.f.save(saved)
            finally:leave.set()
            with self.assertRaises(RegistryError):future.result(30)
        self.assertEqual([(0,'handle')],self.order);self.no_retry()


if __name__=='__main__':unittest.main()
