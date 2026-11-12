"""実Registry/元transactionで初回Assignedと失敗境界を検証する。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import sqlite3
import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch

from . import offline_process, runtime_control
from . import test_offline_prepare as preparation
from . import test_offline_process as processes
from .build_inputs import canonical, digest
from .database_proxy_pipe import ProxyPipe
from .local_ipc import encode
from .registry import RegistryError


class OfflineAssignTests(unittest.TestCase):
    def setUp(self):
        f = preparation.OfflinePrepareTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.prepare()
        self.f, self.p = f, f.p
        self.runtime, self.registry, self.token = f.runtime, f.registry, f.token
        self.c = f.c
        self.w = self.runtime._offline.workers[self.token['instanceId']]
        self.assigns = 0
        self.phase = 'Idle1'
        self.failure = self.response_change = None
        self.before_assign = self.after_assign = lambda:None
        original_send, original_receive = processes.Pipe.send, processes.Pipe.receive

        def send(pipe, request, deadline, verify):
            if request['command'] != 'assign': return original_send(pipe, request, deadline, verify)
            verify()
            pipe.sent.append(deepcopy(request))
            if self.failure == 'partial': raise TimeoutError('fixture 部分送信')
            self.before_assign()
            verify()
            self.assertEqual(self.w.data_identity, request['worker'])
            self.assertEqual((self.c.session,self.c.nonce),(request['sessionId'],request['nonce']))
            self.assertEqual(self.f.operation, request['operationId'])
            self.assertEqual(json.loads(self.c.request)['operationId'], request['openOperationId'])
            self.assertEqual(json.loads(self.w.request)['requestHash'], request['requestHash'])
            self.assertEqual('Idle1', self.phase)
            self.assertEqual(('b'*64,1,processes.RUN),
                (request['expectedStateSha256'],request['expectedRevision'],request['runUuid']))
            self.assertEqual(self.f.request, request['request'])
            self.assigns += 1
            self.phase = 'Assigned2'
            self.after_assign()

        def receive(pipe, deadline, verify):
            request = pipe.sent[-1]
            if request['command'] != 'assign': return original_receive(pipe, deadline, verify)
            verify()
            if self.failure == 'lost':
                self.c.process.code = 1
                raise EOFError('fixture Assigned公開後のpeer非正常終了/応答喪失')
            response = {k:request[k] for k in ('operationId','requestHash','sessionId','nonce')}
            response.update(status='ASSIGNED',stateSha256='c'*64,stateRevision=2,
                            runUuid=processes.RUN,editRequestSha256='d'*64)
            if self.response_change:
                key,value = self.response_change
                response[key] = value
            return response
        self.enterContext(patch.object(processes.Pipe,'send',send))
        self.enterContext(patch.object(processes.Pipe,'receive',receive))

    def assign(self):
        return self.runtime._offline_assign(self.token,operation_id=self.f.operation,deadline=time.monotonic()+10)

    def saved(self): return self.f.saved()
    def mutate(self, sql, args=()): return self.f.mutate(sql,args)

    def no_retry(self):
        before = (self.assigns,len(self.c.pipe.sent),len(self.w.pipe.sent),len(self.p.adapter.processes))
        with self.assertRaises(RegistryError): self.assign()
        self.assertEqual(before,(self.assigns,len(self.c.pipe.sent),len(self.w.pipe.sent),len(self.p.adapter.processes)))
        self.f.pending()

    def test_registered_prepared_reaches_assigned_and_only_recorded_response_is_acked(self):
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        saved_before = self.saved()
        self.before_assign = lambda:self.assertEqual({'operationId':self.f.operation},self.saved()['assignment'])
        original = self.c.acknowledge_assignment
        def ack(value,worker,verify,*,deadline):
            self.assertEqual(digest(self.c.assignment_response),self.saved()['assignment']['responseSha256'])
            return original(value,worker,verify,deadline=deadline)
        with patch.object(self.c,'acknowledge_assignment',ack): result = self.assign()
        self.assertEqual(('ASSIGNED',2,processes.RUN),(result['status'],result['stateRevision'],result['runUuid']))
        self.assertEqual((1,'Assigned2'),(self.assigns,self.phase))
        self.assertEqual(self.f.request,self.c.pipe.sent[-1]['request'])
        self.assertEqual(self.w.data_identity,self.c.pipe.sent[-1]['worker'])
        self.assertEqual([2,1],[self.c.pipe.acks,self.w.pipe.acks])
        self.assertEqual(1,len(self.w.pipe.sent))
        self.assertEqual(2,len(self.p.adapter.processes))
        self.assertEqual(saved_before['worker'],self.saved()['worker'])
        self.w.verify()
        self.c.verify()
        self.assertTrue(self.c.assignment_acknowledged)
        self.assertEqual(saved_before['startupResponseSha256'],digest(self.c.response))
        for secret in (self.c.session,self.c.nonce,'tab.apple','purchasePrice'):
            self.assertNotIn(secret,json.dumps(self.saved()))
        self.no_retry()

    def test_each_prepared_record_mismatch_rejects_before_intent_and_effect(self):
        baseline = self.saved()
        cases = [('operationId',processes.WORLD),('requestSha256','e'*64),('startupResponseSha256','e'*64)]
        for key,value in cases:
            with self.subTest(key=key):
                changed=deepcopy(baseline); changed['worker'][key]=value
                self.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'",(json.dumps(changed),))
                with self.assertRaises(RegistryError): self.assign()
                self.assertEqual(0,self.assigns)
                self.assertFalse(self.c.assignment_started)
                self.assertNotIn('assignment',self.saved())
        self.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'",(json.dumps(baseline),))

    def test_each_prepared_state_and_management_mismatch_rejects_before_intent(self):
        original=self.w.request
        frame=json.loads(original)
        cases=[('leaseId',processes.WORLD),('epoch',99),('generation',processes.RUN),('drainSha256','e'*64),
               ('worldRoot','other'),('shopDirectory','other'),('worldUuid',processes.RUN),
               ('expectedStateSha256','e'*64),('expectedRevision',99),('runUuid',processes.WORLD)]
        for key,value in cases:
            with self.subTest(key=key):
                self.assertNotEqual(frame[key],value)
                self.w.request=canonical(dict(frame,**{key:value}))
                try:
                    with self.assertRaises(RegistryError): self.assign()
                    self.assertEqual(0,self.assigns)
                    self.assertNotIn('assignment',self.saved())
                    self.assertFalse(self.c.assignment_started)
                finally: self.w.request=original

    def test_registry_binding_change_prevents_assignment_effect(self):
        self.mutate('UPDATE run_admission SET revision=revision+1')
        with self.assertRaises(RegistryError): self.assign()
        self.assertEqual(0,self.assigns)
        self.assertNotIn('assignment',self.saved())

    def test_unacknowledged_prepared_never_reaches_assignment(self):
        self.w.startup_acknowledged=False
        with self.assertRaises(RegistryError): self.assign()
        self.assertEqual(0,self.assigns)
        self.assertNotIn('assignment',self.saved())

    def test_native_worker_death_rejects_before_send(self):
        self.w.process.code=1
        with self.assertRaises(RegistryError): self.assign()
        self.assertEqual(0,self.assigns)
        self.assertEqual(1,len(self.c.pipe.sent))
        self.no_retry()

    def test_native_controller_identity_mismatch_rejects_before_send(self):
        self.c.process.identity['startId']='reused'
        with self.assertRaises(RegistryError): self.assign()
        self.assertEqual(0,self.assigns)
        self.assertEqual(1,len(self.c.pipe.sent))
        self.no_retry()

    def test_intent_record_failure_prevents_assignment_send(self):
        self.mutate("CREATE TRIGGER deny_assignment BEFORE UPDATE OF response ON runtime_operations WHEN OLD.id='offline-prepare-owner' BEGIN SELECT RAISE(FAIL,'assignment intent'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.assign()
        self.assertEqual(0,self.assigns)
        self.assertFalse(self.c.assignment_started)
        self.f.pending()

    def test_partial_send_never_retries_or_acks(self):
        self.failure='partial'
        with self.assertRaises(TimeoutError): self.assign()
        self.assertEqual(0,self.assigns)
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry()

    def test_post_publication_loss_has_nonzero_peer_exit_and_no_additional_effect(self):
        self.failure='lost'
        with self.assertRaises(EOFError): self.assign()
        self.assertEqual((1,'Assigned2',1),(self.assigns,self.phase,self.c.process.poll()))
        self.assertNotIn('responseSha256',self.saved()['assignment'])
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry()

    def test_post_publication_binding_change_has_no_ack_or_additional_effect(self):
        self.after_assign=lambda:self.mutate('UPDATE run_admission SET revision=revision+1')
        with self.assertRaises(RegistryError): self.assign()
        self.assertEqual(1,self.assigns)
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry()

    def test_response_record_failure_preserves_assigned_and_pipe(self):
        self.after_assign=lambda:self.mutate("CREATE TRIGGER deny_result BEFORE UPDATE OF response ON runtime_operations WHEN OLD.id='offline-prepare-owner' BEGIN SELECT RAISE(FAIL,'assignment result'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.assign()
        self.assertEqual(1,self.assigns)
        self.assertTrue(self.c.assignment_ready)
        self.assertFalse(self.c.assignment_acknowledged)
        self.assertNotIn('responseSha256',self.saved()['assignment'])
        self.no_retry()

    def test_local_response_journal_failure_keeps_unknown_and_unacked(self):
        original=self.c._record
        def record():
            if self.c.assignment_response is not None: raise OSError('local assigned journal')
            original()
        with patch.object(self.c,'_record',record):
            with self.assertRaises(OSError): self.assign()
        self.assertEqual(1,self.assigns)
        self.assertFalse(self.c.assignment_ready)
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry()

    def test_ack_journal_failure_keeps_recorded_assigned_and_unacked_pipe(self):
        original=self.c._record
        def record():
            if 'assignedAcknowledgedSha256' in self.c.record: raise OSError('local ack journal')
            original()
        with patch.object(self.c,'_record',record):
            with self.assertRaises(OSError): self.assign()
        self.assertEqual(1,self.assigns)
        self.assertIn('responseSha256',self.saved()['assignment'])
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry()

    def test_concurrent_assignment_reserves_once_in_original_transaction(self):
        entered,release=threading.Event(),threading.Event()
        original=self.c.assign
        calls=[]
        def held(*args):
            calls.append(1)
            self.assertEqual(1,len(calls),'C assignへ二重到達しました')
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args)
        with patch.object(self.c,'assign',held),ThreadPoolExecutor(max_workers=2) as pool:
            winner=pool.submit(self.assign)
            try:
                self.assertTrue(entered.wait(10))
                loser=pool.submit(self.assign)
                with self.assertRaises(RegistryError): loser.result(10)
                self.assertEqual(0,self.assigns)
                self.assertEqual({'operationId':self.f.operation},self.saved()['assignment'])
                self.f.pending()
            finally: release.set()
            self.assertEqual('ASSIGNED',winner.result(10)['status'])
        self.assertEqual(1,self.assigns)
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        self.no_retry()


def response_mismatch(key,value):
    def test(self):
        self.response_change=(key,value)
        with self.assertRaises((RegistryError,ValueError)): self.assign()
        self.assertEqual(1,self.assigns)
        self.assertFalse(self.c.assignment_ready)
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry()
    return test

for key,value in [('operationId',processes.WORLD),('requestHash','e'*64),('sessionId','wrong'),('nonce','wrong'),
                  ('status','OPEN'),('runUuid',processes.WORLD),('stateRevision',1),('stateSha256','invalid'),
                  ('editRequestSha256','invalid'),('extra','unexpected')]:
    setattr(OfflineAssignTests,'test_response_'+key+'_mismatch',response_mismatch(key,value))


class AssignmentAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.pipe=ProxyPipe()
        self.addCleanup(self.pipe.close_after_exit)
        response=dict(status='ASSIGNED',stateRevision=2)
        self.pipe.send(dict(command='assign'),time.monotonic()+1,lambda:None)
        body=encode(response)
        os.write(self.pipe.child_output,struct.pack('!I',len(body))+body)
        self.assertEqual(response,self.pipe.receive(time.monotonic()+1,lambda:None))
        self.c=offline_process.OfflineProcess.__new__(offline_process.OfflineProcess)
        self.c.assignment_ready=True
        self.c.assignment_acknowledged=False
        self.c.assignment_response=canonical(response)
        self.c.record={}
        self.c.pipe=self.pipe
        self.c._record=Mock()
        self.worker=Mock()

    def test_real_buffer_advances_after_verified_record_and_never_twice(self):
        verify=Mock()
        self.c.acknowledge_assignment(digest(self.c.assignment_response),self.worker,verify,deadline=time.monotonic()+1)
        verify.assert_called_once()
        self.c._record.assert_called_once()
        self.assertIsNone(self.pipe.outgoing)
        self.assertFalse(self.pipe.incoming)
        self.pipe.send(dict(command='next'),time.monotonic()+1,lambda:None)
        with self.assertRaises(RegistryError): self.c.acknowledge_assignment(digest(self.c.assignment_response),self.worker,verify,deadline=time.monotonic()+1)
        self.assertIsNotNone(self.pipe.outgoing)

    def test_wrong_hash_and_record_failure_retain_real_buffer(self):
        before=(self.pipe.outgoing,bytes(self.pipe.incoming))
        with self.assertRaises(RegistryError): self.c.acknowledge_assignment('e'*64,self.worker,lambda:None,deadline=time.monotonic()+1)
        self.c._record.side_effect=OSError('journal')
        with self.assertRaises(OSError): self.c.acknowledge_assignment(digest(self.c.assignment_response),self.worker,lambda:None,deadline=time.monotonic()+1)
        self.assertFalse(self.c.assignment_acknowledged)
        self.assertEqual(before,(self.pipe.outgoing,bytes(self.pipe.incoming)))
