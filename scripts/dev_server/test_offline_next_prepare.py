"""記録済みIdleから次W待機と旧操作保全を実Registryで検査する。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import offline_control, offline_process, runtime_control
from . import test_offline_complete as completed
from . import test_offline_process as processes
from .build_inputs import canonical, digest
from .registry import RegistryError


NEXT = '00000000-0000-4000-8000-000000000004'


class OfflineNextPrepareTests(unittest.TestCase):
    def setUp(self):
        f = completed.OfflineCompleteTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.complete()
        self.f, self.runtime, self.token, self.c, self.old = f, f.runtime, f.token, f.c, f.w
        self.original = self.saved()
        self.original_request = self.old.request
        self.writer = deepcopy(f.current['writer'])
        self.request = processes.frame(True)['request']
        self.request['requestId'] = NEXT
        self.request['action']['products'][0].update(id='tab.second')
        self.request['action']['products'][0]['definition']['purchasePrice'] = 456
        self.received = None
        self.outer_matched = False
        self.phase = 'WAITING'
        self.expected_phase = 'Idle5'
        self.failure = self.response_change = None
        self.before_prepare = self.after_prepare = lambda:None
        send, receive = processes.Pipe.send, processes.Pipe.receive

        def sending(pipe, frame, deadline, verify):
            if frame['command'] != 'prepare' or frame['operationId'] != NEXT: return send(pipe,frame,deadline,verify)
            verify()
            pipe.sent.append(deepcopy(frame))
            if self.failure == 'partial': raise TimeoutError('fixture 次W部分送信')
            self.before_prepare()
            verify()
            self.received = deepcopy(frame['request'])
            current = f.current
            self.outer_matched = (frame['expectedStateSha256'],frame['expectedRevision'],frame['runUuid']) == (
                current['hash'],current['revision'],current['run'])
            valid = (self.outer_matched and current['phase']=='Idle5' and current['world']==frame['worldUuid']
                     and current['generation']==frame['generation'] and current['writer']==self.writer
                     and current['run']==self.writer['sessionId'])
            if not valid:
                self.worker().process.code = 1
                return
            self.phase = 'PREPARED'
            self.after_prepare()

        def receiving(pipe, deadline, verify):
            if pipe.sent[-1]['command'] != 'prepare' or pipe.sent[-1]['operationId'] != NEXT: return receive(pipe,deadline,verify)
            verify()
            if self.failure == 'lost':
                self.worker().process.code = 1
                raise EOFError('fixture 次W PREPARED応答喪失')
            frame = pipe.sent[-1]
            response = {k:frame[k] for k in ('operationId','requestHash','sessionId','nonce')}
            response.update(status='PREPARED',stateSha256=f.current['hash'],stateRevision=f.current['revision'],runUuid=f.current['run'])
            if self.failure == 'old': return json.loads(self.old.response)
            if self.response_change:
                key,value = self.response_change; response[key] = value
            return response
        self.enterContext(patch.object(processes.Pipe,'send',sending))
        self.enterContext(patch.object(processes.Pipe,'receive',receiving))

    def saved(self): return self.f.saved()
    def save(self,value): return self.f.save(value)
    def worker(self): return self.runtime._offline.workers[self.token['instanceId']]
    def prepare(self,deadline=None,operation_id=NEXT,request=None):
        return self.runtime._offline_prepare(self.token,operation_id=operation_id,request=self.request if request is None else request,
                                              deadline=time.monotonic()+10 if deadline is None else deadline)

    def preserved(self):
        self.assertEqual(1,self.f.completes)
        self.assertEqual(dict.fromkeys(self.f.a.counts,1),self.f.a.counts)
        self.assertEqual(self.original_request,self.old.request)
        self.assertIs(self.runtime._offline.controllers[self.token['instanceId']],self.c)
        self.assertEqual(self.expected_phase,self.f.current['phase'])
        self.f.a.f.f.pending()

    def no_retry(self):
        before = (len(self.f.p.adapter.processes),[len(pipe.sent) for pipe in self.f.p.pipes],self.f.completes,deepcopy(self.f.a.counts))
        with self.assertRaises(RegistryError): self.prepare()
        self.assertEqual(before,(len(self.f.p.adapter.processes),[len(pipe.sent) for pipe in self.f.p.pipes],self.f.completes,self.f.a.counts))
        self.preserved()

    def test_recorded_idle_reaches_next_prepared_with_history_and_same_c(self):
        self.before_prepare = lambda:self.assertEqual(NEXT,self.saved()['worker']['operationId'])
        c_before = deepcopy(self.c.pipe.sent)
        try: result = self.prepare()
        except RegistryError as error: self.fail('記録済み最新Idleの次W待機拒否: '+error.code)
        w = self.worker()
        self.assertIsNot(w,self.old)
        self.assertIs(w.controller,self.c)
        self.assertEqual((NEXT,'8'*64,5,processes.RUN),(result['operationId'],result['stateSha256'],result['stateRevision'],result['runUuid']))
        self.assertEqual(c_before,self.c.pipe.sent)
        self.assertEqual(self.request,self.received)
        self.assertEqual(self.request,json.loads(w.request)['request'])
        self.assertEqual((self.c.session,self.c.nonce),(w.session,w.nonce))
        self.assertEqual(3,len(self.f.p.adapter.processes))
        self.assertEqual(3,len({p.group for p in self.f.p.adapter.processes}))
        self.assertEqual({k:self.original[k] for k in ('worker','assignment','admission','workerExit','completion')},self.saved()['history'][0])
        self.assertIs(self.old,self.runtime._offline.history[self.token['instanceId']][0][0])
        self.assertEqual(digest(w.response),self.saved()['worker']['startupResponseSha256'])
        self.assertTrue(w.startup_acknowledged)
        self.assertEqual(1,w.pipe.acks)
        self.assertNotIn('assignment',self.saved())
        for secret in (self.c.session,self.c.nonce,'tab.second','purchasePrice','transactionIds'):
            self.assertNotIn(secret,json.dumps(self.saved()))
        self.no_retry()

    def test_old_w_pid_and_exit_are_not_reobserved(self):
        identity = self.f.p.adapter.identity
        def checked(pid):
            if pid == self.old.process.pid: self.fail('旧Wの再解決')
            return identity(pid)
        with patch.object(self.f.p.adapter,'identity',checked),patch.object(self.old.process,'poll',side_effect=AssertionError('旧W再観測')),patch.object(self.old.process,'members',side_effect=AssertionError('旧W再観測')):
            self.assertEqual('PREPARED',self.prepare()['status'])
        self.preserved()

    def test_old_operation_id_is_not_reused(self):
        request = deepcopy(self.request);request['requestId']=self.f.operation
        with patch.object(self.f.p.adapter,'OwnedProcess',side_effect=AssertionError('重複IDで生成')):
            with self.assertRaises(RegistryError): self.prepare(operation_id=self.f.operation,request=request)
        self.assertEqual(self.original,self.saved())
        self.assertEqual(2,len(self.f.p.adapter.processes))

    def test_late_old_stages_do_not_send_frames(self):
        self.prepare()
        before = [len(pipe.sent) for pipe in self.f.p.pipes]
        for name,op in (('_offline_assign',self.f.operation),('_offline_admit',self.f.operation),
                        ('_offline_observe_worker_exit',self.f.operation),('_offline_complete',self.f.operation)):
            with self.subTest(stage=name,operation=op):
                with self.assertRaises(RegistryError): getattr(self.runtime,name)(self.token,operation_id=op,deadline=time.monotonic()+10)
        self.assertEqual(before,[len(pipe.sent) for pipe in self.f.p.pipes])
        self.assertNotIn('assignment',self.saved())
        self.preserved()

    def test_old_prepared_response_is_not_accepted_for_new_w(self):
        self.failure='old'
        with self.assertRaises(RegistryError): self.prepare()
        self.assertNotIn('startupResponseSha256',self.saved()['worker'])
        self.no_retry()

    def test_completion_without_ack_has_no_archive_or_generation(self):
        self.c.completion_acknowledged=False
        with self.assertRaises(RegistryError): self.prepare()
        self.assertEqual(self.original,self.saved())
        self.assertEqual(2,len(self.f.p.adapter.processes))
        self.no_retry()

    def test_intent_transaction_failure_preserves_original_proofs(self):
        self.f.a.mutate("CREATE TRIGGER deny_next BEFORE UPDATE OF response ON runtime_operations WHEN json_extract(NEW.response,'$.history') IS NOT NULL BEGIN SELECT RAISE(FAIL,'next intent'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.prepare()
        self.assertEqual(self.original,self.saved())
        self.assertEqual({},self.runtime._offline.history)
        self.assertIs(self.worker(),self.old)

    def test_partial_send_preserves_new_intent_and_old_w(self):
        self.failure='partial'
        with self.assertRaises(TimeoutError): self.prepare()
        self.assertEqual('WAITING',self.phase)
        self.no_retry()

    def test_response_loss_has_nonzero_exit_and_no_db_effect(self):
        self.failure='lost'
        with self.assertRaises(EOFError): self.prepare()
        self.assertEqual(('PREPARED',1),(self.phase,self.worker().process.code))
        self.assertNotIn('startupResponseSha256',self.saved()['worker'])
        self.no_retry()

    def test_response_write_failure_keeps_prepared_without_ack(self):
        self.f.a.mutate("CREATE TRIGGER deny_next_result BEFORE UPDATE OF response ON runtime_operations WHEN json_extract(NEW.response,'$.history') IS NOT NULL AND json_extract(NEW.response,'$.worker.startupResponseSha256') IS NOT NULL BEGIN SELECT RAISE(FAIL,'next result'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.prepare()
        self.assertFalse(self.worker().startup_acknowledged)
        self.assertNotIn('startupResponseSha256',self.saved()['worker'])
        self.no_retry()

    def test_history_change_during_start_prevents_prepared_record(self):
        def change():
            saved=self.saved();saved['history'][0]['completion']['responseSha256']='0'*64;self.save(saved)
        self.after_prepare=change
        with self.assertRaises(RegistryError): self.prepare()
        self.assertFalse(self.worker().startup_acknowledged)
        self.assertNotIn('startupResponseSha256',self.saved()['worker'])
        self.no_retry()

    def test_memory_history_change_during_start_is_rejected(self):
        self.after_prepare=lambda:self.runtime._offline.history[self.token['instanceId']][0][1]['completion'].update(responseSha256='0'*64)
        with self.assertRaises(RegistryError): self.prepare()
        self.assertNotIn('startupResponseSha256',self.saved()['worker'])
        self.no_retry()

    def test_new_w_creation_failure_keeps_history_and_original_handle(self):
        with patch.object(processes.Process,'start',side_effect=OSError('次W生成途中')):
            with self.assertRaises(OSError):self.prepare()
        self.assertIs(self.old,self.runtime._offline.history[self.token['instanceId']][0][0])
        self.assertIsNot(self.worker(),self.old)
        self.assertFalse(self.worker().ready)
        self.assertNotIn('startupResponseSha256',self.saved()['worker'])
        self.no_retry()

    def test_new_ack_failure_keeps_recorded_response_without_replay(self):
        with patch.object(processes.Pipe,'acknowledged',side_effect=OSError('次W ack failure')):
            with self.assertRaises(OSError):self.prepare()
        self.assertIn('startupResponseSha256',self.saved()['worker'])
        self.assertFalse(self.worker().startup_acknowledged)
        self.no_retry()

    def test_final_transaction_rechecks_history_before_result(self):
        original=self.runtime._offline._opened
        checks=[]
        def checked(connection,*args):
            result=original(connection,*args)
            if self.worker() is not self.old and self.worker().ready:
                checks.append(1)
                if len(checks)==2:
                    result[2]['history'][0]['completion']['responseSha256']='0'*64
                    connection.execute("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'",(json.dumps(result[2]),))
            return result
        with patch.object(self.runtime._offline,'_opened',checked):
            with self.assertRaises(RegistryError):self.prepare()
        self.assertEqual(2,len(checks))
        self.assertNotIn('startupResponseSha256',self.saved()['worker'])
        self.assertFalse(self.worker().startup_acknowledged)
        self.no_retry()

    def test_competing_next_prepare_and_start_only_generate_one_new_w(self):
        entered,release=threading.Event(),threading.Event()
        def hold():
            entered.set();self.assertTrue(release.wait(10))
        self.before_prepare=hold
        with ThreadPoolExecutor(max_workers=3) as pool:
            winner=pool.submit(self.prepare)
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(RegistryError):pool.submit(self.prepare).result(10)
                with self.assertRaises(RegistryError):pool.submit(self.runtime.execute,'start',dict(self.token,timeoutSeconds=1),operation_id='start-next-w').result(10)
                self.assertEqual(3,len(self.f.p.adapter.processes))
                self.preserved()
            finally:release.set()
            self.assertEqual('PREPARED',winner.result(10)['status'])
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        self.no_retry()

    def test_expired_deadline_does_not_archive_or_generate(self):
        with self.assertRaises(RegistryError):self.prepare(deadline=0)
        self.assertEqual(self.original,self.saved())
        self.assertEqual(2,len(self.f.p.adapter.processes))

    def test_final_registry_deadline_preserves_history_and_intent(self):
        clock=SimpleNamespace(value=0);clock.monotonic=lambda:clock.value
        original=self.runtime._offline._opened
        checks=[]
        def checked(*args):
            result=original(*args)
            if self.worker() is not self.old and self.worker().ready:
                checks.append(1)
                if len(checks)==2:clock.value=2
            return result
        with patch.object(self.runtime._offline,'_opened',checked),patch.object(offline_control,'time',clock),patch.object(offline_process,'time',clock):
            with self.assertRaises(RegistryError) as error:self.prepare(deadline=1)
        self.assertEqual('STOP_TIMEOUT',error.exception.code)
        self.assertEqual(2,len(checks))
        self.assertNotIn('startupResponseSha256',self.saved()['worker'])
        self.no_retry()


def completion_case(key):
    def test(self):
        if key=='ready':self.c.completion_ready=False
        elif key=='response':self.c.completion_response=canonical(dict(json.loads(self.c.completion_response),stateSha256='0'*64))
        elif key=='local':self.c.record['completionAcknowledgedSha256']='0'*64
        elif key=='exit':self.old.record['workerExit']['allChildrenExited']=False
        else:
            saved=self.saved();saved['completion'][key]='0'*64;self.save(saved)
        with self.assertRaises(RegistryError):self.prepare()
        self.assertNotIn('history',self.saved())
        self.assertEqual(2,len(self.f.p.adapter.processes))
        self.no_retry()
    return test


for key in ('ready','response','local','exit','responseSha256','workerExitSha256','operationId'):
    setattr(OfflineNextPrepareTests,'test_completion_'+key+'_mismatch_blocks_generation',completion_case(key))


def response_case(key,value):
    def test(self):
        self.response_change=(key,value)
        with self.assertRaises((RegistryError,ValueError)):self.prepare()
        self.assertNotIn('startupResponseSha256',self.saved()['worker'])
        self.no_retry()
    return test


for key,value in dict(operationId=processes.WORLD,requestHash='0'*64,sessionId='old',nonce='old',stateSha256='0'*64,stateRevision=1,runUuid=processes.WORLD).items():
    setattr(OfflineNextPrepareTests,'test_new_response_'+key+'_mismatch_is_not_acked',response_case(key,value))


def state_case(key):
    def test(self):
        current=self.f.current
        if key=='session':current['writer']['sessionId']=processes.WORLD
        elif key.startswith('owner_'):current['writer']['owner'][key[6:]]=123 if key=='owner_pid' else 'other'
        elif key.startswith('controller_'):current['writer']['management']['controller'][key[11:]]=123 if key=='controller_pid' else 'other'
        elif key.startswith('management_'):current['writer']['management'][key[11:]]=99 if key=='management_epoch' else 'other'
        else:current[key]='other'
        self.expected_phase=current['phase']
        current['hash']='6'*64
        response=dict(json.loads(self.c.completion_response),stateSha256=current['hash'])
        self.c.completion_response=canonical(response)
        value=digest(self.c.completion_response)
        self.c.record.update(completionResponseSha256=value,completionAcknowledgedSha256=value)
        saved=self.saved();saved['completion']['responseSha256']=value;self.save(saved)
        # 合成stateのhashへ外側期待値も合わせ、内側bindingの拒否を確認する。
        with self.assertRaises((RegistryError,ValueError)):self.prepare()
        self.assertTrue(self.outer_matched)
        self.assertEqual(1,self.worker().process.code)
        self.assertFalse(self.worker().startup_acknowledged)
        self.no_retry()
    return test


for key in ('session','owner_hostHash','owner_bootId','owner_pid','owner_startId','controller_hostHash','controller_bootId','controller_pid','controller_startId',
            'management_leaseId','management_epoch','management_generation','management_drainSha256','generation','world','phase'):
    setattr(OfflineNextPrepareTests,'test_state_'+key+'_mismatch_rejects_before_db',state_case(key))
