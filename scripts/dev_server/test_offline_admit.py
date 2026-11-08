"""実Registryと元transactionでadmit・候補packetの境界を検証する。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import struct
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from . import runtime_control
from . import test_offline_assign as assignments
from . import test_offline_process as processes
from .build_inputs import canonical, digest
from .registry import RegistryError
from .database_proxy_pipe import ProxyPipe
from .local_ipc import encode


class OfflineAdmitTests(unittest.TestCase):
    def setUp(self):
        f=assignments.OfflineAssignTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.assign()
        self.f,self.p,self.c,self.w=f,f.p,f.c,f.w
        self.runtime,self.registry,self.token=f.runtime,f.registry,f.token
        self.operation=f.f.operation
        self.request=deepcopy(self.w.pipe.sent[0]['request'])
        self.counts=dict(begins=0,dbOpens=0,edits=0,dbCloses=0,finishes=0)
        self.state=dict(hash='c'*64,revision=2,run=processes.RUN,previous='b'*64,
                        writer=deepcopy(self.c.data_identity),worker=deepcopy(self.w.data_identity),
                        operation=self.operation,editHash='d'*64,resume=None)
        self.phase='Assigned2'
        self.before_effect=self.after_candidate=lambda:None
        self.failure=self.response_change=None
        self.exit_after_candidate=False
        self.outer_matched=False
        self.rejected=None
        self.begin_request=self.edit_request=None
        original_send,original_receive=processes.Pipe.send,processes.Pipe.receive

        def send(pipe,frame,deadline,verify):
            if frame['command']!='admit': return original_send(pipe,frame,deadline,verify)
            verify()
            pipe.sent.append(deepcopy(frame))
            if self.failure=='partial': raise TimeoutError('fixture admit部分送信')
            self.before_effect()
            verify()
            expected={'command','operationId','requestHash','sessionId','nonce','preparedStateSha256',
                      'expectedStateSha256','expectedRevision','runUuid','editRequestSha256'}
            self.assertEqual(expected,set(frame))
            self.assertEqual((self.operation,json.loads(self.w.request)['requestHash'],self.c.session,self.c.nonce),
                             tuple(frame[k] for k in ('operationId','requestHash','sessionId','nonce')))
            self.assertEqual(('b'*64,self.state['hash'],self.state['revision'],self.state['run']),
                             tuple(frame[k] for k in ('preparedStateSha256','expectedStateSha256','expectedRevision','runUuid')))
            self.outer_matched=True
            # 保存/hash/recoverは行わず、実Dataと同じ作用前bindingと固定遷移だけを表す。
            expected_state=dict(previous='b'*64,writer=self.c.data_identity,worker=self.w.data_identity,
                                operation=self.operation,editHash=frame['editRequestSha256'],resume=None)
            for key,value in expected_state.items():
                if self.state[key]!=value:
                    self.rejected=key
                    self.w.process.code=1
                    return
            self.assertEqual('Assigned2',self.phase)
            self.counts['begins']+=1
            self.phase='Pending3'; self.state.update(hash='f'*64,revision=3,run=self.operation)
            self.begin_request=deepcopy(self.request)
            if self.failure=='pending':
                self.w.process.code=1
                return
            self.counts['dbOpens']+=1
            self.counts['edits']+=1
            self.edit_request=deepcopy(self.request)
            self.counts['dbCloses']+=1
            self.counts['finishes']+=1
            self.phase='SavedPendingExit4'; self.state.update(hash='e'*64,revision=4)
            self.after_candidate()
            if self.exit_after_candidate: self.w.process.code=0

        def receive(pipe,deadline,verify):
            if pipe.sent[-1]['command']!='admit': return original_receive(pipe,deadline,verify)
            verify()
            if self.failure=='lost':
                self.w.process.code=1
                raise EOFError('fixture 候補公開後の応答喪失')
            frame=pipe.sent[-1]
            if self.phase!='SavedPendingExit4':
                return dict(status='UNKNOWN' if self.counts['begins'] else 'REJECTED',
                            operationId=frame['operationId'],requestHash=frame['requestHash'])
            response={k:frame[k] for k in ('operationId','requestHash','sessionId','nonce','editRequestSha256')}
            response.update(status='CANDIDATE',stateSha256='e'*64,receiptSha256='1'*64,
                            worldUuid=processes.WORLD,runUuid=self.operation,stateRevision=4)
            if self.response_change:
                key,value=self.response_change
                response[key]=value
            return response
        self.enterContext(patch.object(processes.Pipe,'send',send))
        self.enterContext(patch.object(processes.Pipe,'receive',receive))

    def admit(self):
        return self.runtime._offline_admit(self.token,operation_id=self.operation,deadline=time.monotonic()+10)

    def saved(self): return self.f.saved()
    def mutate(self,sql,args=()): return self.f.mutate(sql,args)

    def no_retry(self):
        before=(deepcopy(self.counts),len(self.c.pipe.sent),len(self.w.pipe.sent),len(self.p.adapter.processes))
        with self.assertRaises(RegistryError): self.admit()
        self.assertEqual(before,(self.counts,len(self.c.pipe.sent),len(self.w.pipe.sent),len(self.p.adapter.processes)))
        self.f.f.pending()

    def test_candidate_keeps_reservation_and_original_request_without_final_ack(self):
        original=self.saved()
        self.before_effect=lambda:self.assertEqual({'operationId':self.operation},self.saved()['admission'])
        result=self.admit()
        self.assertEqual(('CANDIDATE',self.operation,4),(result['status'],result['runUuid'],result['stateRevision']))
        self.assertEqual(dict.fromkeys(self.counts,1),self.counts)
        self.assertEqual(self.request,self.begin_request)
        self.assertEqual(self.request,self.edit_request)
        self.assertEqual(1,len(self.edit_request['action']['products']))
        self.assertNotIn('request',self.w.pipe.sent[-1])
        self.assertEqual([2,2],[len(self.c.pipe.sent),len(self.w.pipe.sent)])
        self.assertEqual([2,1],[self.c.pipe.acks,self.w.pipe.acks])
        self.assertEqual(2,len(self.p.adapter.processes))
        self.assertEqual(original['worker'],self.saved()['worker'])
        self.assertEqual(original['assignment'],self.saved()['assignment'])
        self.assertEqual(digest(self.w.candidate_response),self.saved()['admission']['candidateResponseSha256'])
        self.assertEqual('PREPARED',json.loads(self.w.response)['status'])
        self.assertTrue(self.w.candidate_ready)
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        for secret in (self.c.session,self.c.nonce,'tab.apple','purchasePrice'):
            self.assertNotIn(secret,json.dumps(self.saved()))
        self.no_retry()

    def test_normal_exit_and_pid_reuse_after_send_do_not_reattach_old_worker(self):
        self.exit_after_candidate=True
        self.w.process.extra=[100099]
        identity=self.p.adapter.identity
        def checked(pid):
            if pid==self.w.process.pid and self.w.process.code is not None:
                self.fail('exit後の旧W PIDを再解決しました')
            return identity(pid)
        with patch.object(self.p.adapter,'identity',checked):
            try: result=self.admit()
            except RegistryError as error: self.fail('正常exit後の候補受信を拒否しました: '+error.code)
        self.assertEqual('CANDIDATE',result['status'])
        self.assertEqual(0,self.w.process.poll())
        self.assertEqual(2,len(self.w.process.members()))
        self.f.f.pending()
        self.no_retry()

    def test_nonzero_exit_after_candidate_rejected(self):
        self.after_candidate=lambda:setattr(self.w.process,'code',1)
        with self.assertRaises(RegistryError): self.admit()
        self.assertEqual(dict.fromkeys(self.counts,1),self.counts)
        self.assertEqual('SavedPendingExit4',self.phase)
        self.assertFalse(self.w.candidate_ready)
        self.assertNotIn('candidateResponseSha256',self.saved()['admission'])
        self.no_retry()

    def test_real_pipe_retains_candidate_after_normal_exit(self):
        old_pipe=self.w.pipe
        pipe=ProxyPipe()
        self.addCleanup(pipe.close_after_exit)
        original=pipe.send
        def send(frame,deadline,verify):
            processes.Pipe.send(old_pipe,frame,deadline,verify)
            original(frame,deadline,verify)
            response=processes.Pipe.receive(old_pipe,deadline,verify)
            body=encode(response)
            os.write(pipe.child_output,struct.pack('!I',len(body))+body)
            self.w.process.code=0
        self.w.pipe=pipe
        with patch.object(pipe,'send',send):
            try: result=self.admit()
            except RegistryError as error: self.fail('正常exitとbuffer受信の競合: '+error.code)
        self.assertEqual('CANDIDATE',result['status'])
        self.assertEqual(dict.fromkeys(self.counts,1),self.counts)
        self.assertIsNotNone(pipe.outgoing)
        self.assertTrue(pipe.incoming)
        self.assertEqual(digest(self.w.candidate_response),self.saved()['admission']['candidateResponseSha256'])
        self.f.f.pending()

    def test_each_assignment_record_mismatch_rejects_before_admit(self):
        baseline=self.saved()
        for key,value in [('operationId',processes.WORLD),('responseSha256','9'*64)]:
            with self.subTest(key=key):
                changed=deepcopy(baseline); changed['assignment'][key]=value
                self.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'",(json.dumps(changed),))
                with self.assertRaises(RegistryError): self.admit()
                self.assertEqual(dict.fromkeys(self.counts,0),self.counts)
                self.assertFalse(self.w.admission_started)
                self.assertNotIn('admission',self.saved())
        self.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'",(json.dumps(baseline),))

    def test_unacknowledged_assignment_rejects_before_intent(self):
        self.c.assignment_acknowledged=False
        with self.assertRaises(RegistryError): self.admit()
        self.assertEqual(dict.fromkeys(self.counts,0),self.counts)
        self.assertNotIn('admission',self.saved())

    def test_registry_change_before_admit_has_zero_effect(self):
        self.mutate('UPDATE run_admission SET revision=revision+1')
        with self.assertRaises(RegistryError): self.admit()
        self.assertEqual(dict.fromkeys(self.counts,0),self.counts)

    def test_worker_exit_before_send_is_rejected_without_effect(self):
        self.w.process.code=0
        with self.assertRaises(RegistryError): self.admit()
        self.assertEqual(dict.fromkeys(self.counts,0),self.counts)
        self.assertEqual(1,len(self.w.pipe.sent))
        self.no_retry()

    def test_intent_record_failure_has_no_admit_or_effect(self):
        self.mutate("CREATE TRIGGER deny_admit BEFORE UPDATE OF response ON runtime_operations WHEN OLD.id='offline-prepare-owner' BEGIN SELECT RAISE(FAIL,'admit intent'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.admit()
        self.assertEqual(dict.fromkeys(self.counts,0),self.counts)
        self.assertFalse(self.w.admission_started)

    def test_partial_send_is_not_retried(self):
        self.failure='partial'
        with self.assertRaises(TimeoutError): self.admit()
        self.assertEqual(dict.fromkeys(self.counts,0),self.counts)
        self.no_retry()

    def test_pending_publication_failure_has_no_additional_effect_and_nonzero_exit(self):
        self.failure='pending'
        with self.assertRaises(RegistryError): self.admit()
        self.assertEqual(dict(begins=1,dbOpens=0,edits=0,dbCloses=0,finishes=0),self.counts)
        self.assertEqual(('Pending3',self.operation,3,1),(self.phase,self.state['run'],self.state['revision'],self.w.process.poll()))
        self.no_retry()

    def test_candidate_response_loss_preserves_saved_state_and_nonzero_exit(self):
        self.failure='lost'
        with self.assertRaises(EOFError): self.admit()
        self.assertEqual(dict.fromkeys(self.counts,1),self.counts)
        self.assertEqual(('SavedPendingExit4',self.operation,4,1),(self.phase,self.state['run'],self.state['revision'],self.w.process.poll()))
        self.assertNotIn('candidateResponseSha256',self.saved()['admission'])
        self.no_retry()

    def test_controller_death_after_publication_prevents_candidate_acceptance(self):
        self.after_candidate=lambda:setattr(self.c.process,'code',1)
        with self.assertRaises(RegistryError): self.admit()
        self.assertEqual(dict.fromkeys(self.counts,1),self.counts)
        self.assertFalse(self.w.candidate_ready)
        self.no_retry()

    def test_candidate_record_failure_preserves_buffers_and_no_ack(self):
        self.after_candidate=lambda:self.mutate("CREATE TRIGGER deny_candidate BEFORE UPDATE OF response ON runtime_operations WHEN OLD.id='offline-prepare-owner' BEGIN SELECT RAISE(FAIL,'candidate record'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.admit()
        self.assertTrue(self.w.candidate_ready)
        self.assertEqual(dict.fromkeys(self.counts,1),self.counts)
        self.assertNotIn('candidateResponseSha256',self.saved()['admission'])
        self.assertEqual(1,self.w.pipe.acks)
        self.no_retry()

    def test_local_candidate_record_failure_is_not_promoted(self):
        original=self.w._record
        def record():
            if self.w.candidate_response is not None: raise OSError('candidate journal')
            original()
        with patch.object(self.w,'_record',record):
            with self.assertRaises(OSError): self.admit()
        self.assertFalse(self.w.candidate_ready)
        self.assertEqual(dict.fromkeys(self.counts,1),self.counts)
        self.no_retry()

    def test_admit_competitors_and_start_keep_original_transaction_and_reservation(self):
        entered,release=threading.Event(),threading.Event()
        original=self.w.admit
        calls=[]
        def held(*args):
            calls.append(1)
            self.assertEqual(1,len(calls),'W admitへ二重到達しました')
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args)
        with patch.object(self.w,'admit',held),ThreadPoolExecutor(max_workers=3) as pool:
            winner=pool.submit(self.admit)
            try:
                self.assertTrue(entered.wait(10))
                loser=pool.submit(self.admit)
                with self.assertRaises(RegistryError): loser.result(10)
                start=pool.submit(self.runtime.execute,'start',dict(self.token,timeoutSeconds=1),operation_id='start-after-assigned')
                with self.assertRaises(RegistryError): start.result(10)
                self.assertEqual(dict.fromkeys(self.counts,0),self.counts)
                self.assertEqual(2,len(self.p.adapter.processes))
                self.f.f.pending()
            finally: release.set()
            self.assertEqual('CANDIDATE',winner.result(10)['status'])
        self.assertEqual(dict.fromkeys(self.counts,1),self.counts)
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        self.no_retry()


def state_rejection(key,value):
    def test(self):
        # 外側hash/revision/runは変更した実state側fixtureへ一致させ、内側bindingだけを崩す。
        self.state.update(hash='9'*64,**{key:value})
        assigned=json.loads(self.c.assignment_response); assigned['stateSha256']='9'*64
        self.c.assignment_response=canonical(assigned)
        saved=self.saved(); saved['assignment']['responseSha256']=digest(self.c.assignment_response)
        self.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'",(json.dumps(saved),))
        with self.assertRaises(RegistryError): self.admit()
        self.assertTrue(self.outer_matched)
        self.assertEqual(key,self.rejected)
        self.assertEqual(dict.fromkeys(self.counts,0),self.counts)
        self.assertEqual(1,self.w.process.poll())
        self.no_retry()
    return test

for key,value in [('previous','0'*64),('writer',{}),('worker',{}),('operation',processes.WORLD),('editHash','0'*64),('resume','0'*64)]:
    setattr(OfflineAdmitTests,'test_state_'+key+'_rejection',state_rejection(key,value))


def response_rejection(key,value):
    def test(self):
        self.response_change=(key,value)
        with self.assertRaises((RegistryError,ValueError)): self.admit()
        self.assertEqual(dict.fromkeys(self.counts,1),self.counts)
        self.assertFalse(self.w.candidate_ready)
        self.assertNotIn('candidateResponseSha256',self.saved()['admission'])
        self.no_retry()
    return test

for key,value in [('operationId',processes.WORLD),('requestHash','0'*64),('sessionId','wrong'),('nonce','wrong'),
                  ('worldUuid',processes.RUN),('runUuid',processes.RUN),('stateRevision',3),('editRequestSha256','0'*64),
                  ('stateSha256','invalid'),('receiptSha256','invalid'),('status','SUCCESS'),('extra','unexpected')]:
    setattr(OfflineAdmitTests,'test_candidate_'+key+'_rejection',response_rejection(key,value))
