"""元終了証拠と実Registry transactionからC本人completeを検証する。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import sqlite3
import struct
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import offline_control, offline_process, runtime_control
from . import test_offline_process as processes
from . import test_offline_worker_exit as exits
from .build_inputs import canonical, digest
from .database_proxy_pipe import ProxyPipe
from .local_ipc import encode
from .registry import RegistryError


class OfflineCompleteTests(unittest.TestCase):
    def setUp(self):
        f = exits.OfflineWorkerExitTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.observe()
        self.f, self.a, self.p, self.c, self.w = f, f.f, f.p, f.c, f.w
        self.runtime, self.token, self.operation = f.runtime, f.token, f.operation
        opened = json.loads(self.c.request)
        writer = dict(sessionId=processes.RUN, owner=deepcopy(self.c.data_identity), management={
            k:opened[k] for k in ('leaseId','epoch','generation','drainSha256')})
        writer['management']['controller'] = deepcopy(self.c.parent_data)
        self.held = dict(phase='Assigned2', writer=deepcopy(writer), worker=deepcopy(self.w.data_identity),
                         operation=self.operation, revision=2, run=processes.RUN)
        self.current = dict(self.a.state, writer=writer, world=processes.WORLD, generation=opened['generation'],
                            previous='f'*64, phase='SavedPendingExit4')
        self.receipt = dict(world=processes.WORLD, run=self.operation, revision=3,
                            worker=deepcopy(self.w.data_identity), seal='5'*64, response=dict(
                                version=1, requestId=self.operation, status='SUCCESS',
                                result=dict(type='io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterResult.Edited',
                                            transactionIds=[7]), code=None, message=None, rejectedBeforeEffect=False))
        self.completes = 0
        self.outer_matched = False
        self.db_closed = self.receipt_matches = self.seal_matches = True
        self.failure = self.response_change = None
        self.before_effect = self.after_effect = lambda:None
        original_send, original_receive = processes.Pipe.send, processes.Pipe.receive

        def send(pipe, frame, deadline, verify):
            if frame['command'] != 'complete': return original_send(pipe, frame, deadline, verify)
            verify()
            pipe.sent.append(deepcopy(frame))
            if self.failure == 'partial': raise TimeoutError('fixture complete部分送信')
            self.before_effect()
            verify()
            self.assertEqual({'command','operationId','requestHash','sessionId','nonce','openOperationId',
                              'expectedStateSha256','expectedRevision','worldUuid','runUuid','receiptSha256','editRequestSha256'}, set(frame))
            self.outer_matched = (frame['expectedStateSha256'], frame['expectedRevision'], frame['worldUuid'], frame['runUuid']) == (
                self.current['hash'], self.current['revision'], self.current['world'], self.current['run'])
            valid = (self.outer_matched and frame['receiptSha256'] == '1'*64
                     and self.current['phase'] == 'SavedPendingExit4' and self.held['phase'] == 'Assigned2'
                     and self.current['writer'] == self.held['writer'] and self.current['worker'] == self.held['worker']
                     and self.current['generation'] == opened['generation']
                     and self.current['world'] == processes.WORLD and self.current['run'] == self.operation
                     and self.current['revision'] == self.held['revision'] + 2
                     and self.receipt['world'] == self.current['world'] and self.receipt['run'] == self.current['run']
                     and self.receipt['revision'] == self.current['revision'] - 1 and self.receipt['worker'] == self.current['worker']
                     and self.db_closed and self.receipt_matches and self.seal_matches)
            if not valid:
                self.c.process.code = 1
                return
            # Dataの固定遷移だけを表し、保存・hash・recover算法は持たない。
            self.completes += 1
            self.current.update(phase='Idle5',hash='8'*64,revision=5,run=writer['sessionId'],previous=frame['expectedStateSha256'],seal=self.receipt['seal'])
            if self.failure == 'published':
                self.c.process.code = 1
                return
            self.held = deepcopy(self.current)
            self.after_effect()

        def receive(pipe, deadline, verify):
            if pipe.sent[-1]['command'] != 'complete': return original_receive(pipe, deadline, verify)
            verify()
            if self.failure == 'lost':
                self.c.process.code = 1
                raise EOFError('fixture Idle公開後のC応答喪失')
            frame = pipe.sent[-1]
            if self.c.process.code is not None:
                return dict(status='UNKNOWN', operationId=self.operation, requestHash=frame['requestHash'])
            response = {k:frame[k] for k in ('operationId','requestHash','sessionId','nonce','receiptSha256')}
            response.update(status='COMPLETED',stateSha256=self.current['hash'],stateRevision=self.current['revision'],
                            runUuid=self.current['run'],candidateStateSha256=frame['expectedStateSha256'],result=deepcopy(self.receipt['response']))
            if self.response_change:
                key, value = self.response_change
                response[key] = value
            return response
        self.enterContext(patch.object(processes.Pipe, 'send', send))
        self.enterContext(patch.object(processes.Pipe, 'receive', receive))

    def complete(self, deadline=None):
        return self.runtime._offline_complete(self.token, operation_id=self.operation,
                                               deadline=time.monotonic()+10 if deadline is None else deadline)

    def saved(self): return self.a.saved()

    def save(self, value):
        self.a.mutate("UPDATE runtime_operations SET response=? WHERE id='offline-prepare-owner'", (json.dumps(value),))

    def counts(self):
        return (self.completes, deepcopy(self.a.counts), len(self.c.pipe.sent), len(self.w.pipe.sent),
                self.c.pipe.acks, self.w.pipe.acks, len(self.p.adapter.processes))

    def preserved(self):
        self.assertEqual(dict.fromkeys(self.a.counts,1), self.a.counts)
        self.assertEqual(self.a.request, self.a.begin_request)
        self.assertEqual(self.a.request, self.a.edit_request)
        self.assertEqual(2,len(self.p.adapter.processes))
        self.assertIs(self.runtime._offline.controllers[self.token['instanceId']], self.c)
        self.assertIs(self.runtime._offline.workers[self.token['instanceId']], self.w)
        self.a.f.f.pending()

    def no_retry(self):
        before = self.counts()
        with self.assertRaises(RegistryError): self.complete()
        self.assertEqual(before, self.counts())
        self.preserved()

    def align_candidate(self):
        candidate = json.loads(self.w.candidate_response)
        candidate.update(stateSha256=self.current['hash'],stateRevision=self.current['revision'],
                         worldUuid=self.current['world'],runUuid=self.current['run'])
        self.w.candidate_response = canonical(candidate)
        self.w.record['candidateResponseSha256'] = digest(self.w.candidate_response)
        exited = json.loads(self.w.exit_response)
        exited['candidateResponseSha256'] = digest(self.w.candidate_response)
        self.w.exit_response = canonical(exited); self.w.record['workerExit'] = deepcopy(exited)
        saved = self.saved()
        saved['admission']['candidateResponseSha256'] = digest(self.w.candidate_response)
        saved['workerExit']['responseSha256'] = digest(self.w.exit_response)
        self.save(saved)

    def test_original_candidate_and_receipt_result_are_recorded_before_pipe_ack(self):
        before = self.saved()
        self.before_effect = lambda:self.assertEqual(dict(operationId=self.operation,workerExitSha256=digest(self.w.exit_response)),self.saved()['completion'])
        original = self.c.acknowledge_completion
        def ack(value, deadline, verify):
            self.assertIn('responseSha256',self.saved()['completion'])
            self.assertEqual(digest(self.c.completion_response), self.saved()['completion']['responseSha256'])
            self.assertEqual(2,self.c.pipe.acks)
            return original(value,deadline,verify)
        with patch.object(self.c,'acknowledge_completion',ack): result = self.complete()
        self.assertEqual(self.receipt['response'],result['result'])
        self.assertEqual((1,'Idle5',5,processes.RUN),(self.completes,self.current['phase'],result['stateRevision'],result['runUuid']))
        candidate = json.loads(self.w.candidate_response)
        frame = json.loads(self.c.completion_request)
        self.assertEqual((candidate['stateSha256'],candidate['receiptSha256']),
                         (frame['expectedStateSha256'],frame['receiptSha256']))
        self.assertEqual(before,{k:v for k,v in self.saved().items() if k!='completion'})
        self.assertEqual((3,1),(self.c.pipe.acks,self.w.pipe.acks))
        self.assertTrue(self.c.completion_acknowledged)
        self.assertIsNone(self.c.process.poll())
        for secret in (self.c.session,self.c.nonce,'tab.apple','purchasePrice','transactionIds'):
            self.assertNotIn(secret,json.dumps(self.saved()))
        result['result']['result']['transactionIds'].append(99)
        self.assertEqual([7],json.loads(self.c.completion_response)['result']['result']['transactionIds'])
        self.no_retry()

    def test_data_rejected_result_is_not_rewritten_to_success(self):
        self.receipt['response'].update(status='REJECTED',result=None,code='fixture-reject',rejectedBeforeEffect=True)
        result = self.complete()
        self.assertEqual(self.receipt['response'],result['result'])
        self.assertEqual(1,self.completes)
        self.preserved()

    def test_old_worker_pid_is_not_resolved_or_observed_again(self):
        identity = self.p.adapter.identity
        def checked(pid):
            if pid == self.w.process.pid: self.fail('終了後W PIDの再解決')
            return identity(pid)
        with patch.object(self.p.adapter,'identity',checked), patch.object(self.w.process,'poll',side_effect=AssertionError('再観測')), patch.object(self.w.process,'members',side_effect=AssertionError('再観測')):
            self.assertEqual('COMPLETED',self.complete()['status'])
        self.preserved()

    def test_missing_exit_evidence_has_zero_effect(self):
        self.w.exit_response = None
        with self.assertRaises(RegistryError): self.complete()
        self.assertEqual(0,self.completes)
        self.assertNotIn('completion',self.saved())
        self.no_retry()

    def test_registry_intent_write_failure_has_zero_ipc_and_effect(self):
        self.a.mutate("CREATE TRIGGER deny_complete BEFORE UPDATE OF response ON runtime_operations WHEN OLD.id='offline-prepare-owner' BEGIN SELECT RAISE(FAIL,'complete intent'); END")
        baseline = self.counts()
        with self.assertRaises(sqlite3.IntegrityError): self.complete()
        self.assertEqual(baseline,self.counts())
        self.assertFalse(self.c.completion_started)

    def test_partial_send_is_not_retried(self):
        self.failure = 'partial'
        with self.assertRaises(TimeoutError): self.complete()
        self.assertEqual(0,self.completes)
        self.no_retry()

    def test_idle_publication_failure_keeps_original_assigned_and_nonzero_exit(self):
        self.failure = 'published'
        with self.assertRaises(RegistryError): self.complete()
        self.assertEqual((1,'Idle5','Assigned2',1),(self.completes,self.current['phase'],self.held['phase'],self.c.process.code))
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.no_retry()

    def test_response_loss_has_one_effect_and_nonzero_exit(self):
        self.failure = 'lost'
        with self.assertRaises(EOFError): self.complete()
        self.assertEqual((1,'Idle5',1),(self.completes,self.current['phase'],self.c.process.code))
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.no_retry()

    def test_registry_result_write_failure_preserves_response_without_ack(self):
        self.a.mutate("CREATE TRIGGER deny_complete_result BEFORE UPDATE OF response ON runtime_operations WHEN json_extract(NEW.response,'$.completion.responseSha256') IS NOT NULL BEGIN SELECT RAISE(FAIL,'complete result'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.complete()
        self.assertEqual((1,2),(self.completes,self.c.pipe.acks))
        self.assertIsNotNone(self.c.completion_response)
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.no_retry()

    def test_local_record_failure_does_not_ack_or_repeat_effect(self):
        with patch.object(self.c,'_record',side_effect=OSError('local complete journal')):
            with self.assertRaises(OSError): self.complete()
        self.assertEqual((1,2),(self.completes,self.c.pipe.acks))
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.no_retry()

    def test_registry_change_after_effect_prevents_result_and_ack(self):
        self.after_effect = lambda:self.a.mutate('UPDATE reservations SET epoch=epoch+1')
        with self.assertRaises(RegistryError): self.complete()
        self.assertEqual((1,2),(self.completes,self.c.pipe.acks))
        self.no_retry()

    def test_ack_failure_preserves_recorded_result_and_does_not_replay(self):
        with patch.object(self.c.pipe,'acknowledged',side_effect=OSError('ack failure')):
            with self.assertRaises(OSError): self.complete()
        self.assertIn('responseSha256',self.saved()['completion'])
        self.assertFalse(self.c.completion_acknowledged)
        self.no_retry()

    def test_deadline_crossed_after_response_does_not_record_or_ack(self):
        clock = SimpleNamespace(value=0); clock.monotonic = lambda:clock.value
        self.after_effect = lambda:setattr(clock,'value',2)
        with patch.object(offline_process,'time',clock),patch.object(offline_control,'time',clock):
            with self.assertRaises(RegistryError) as error: self.complete(deadline=1)
        self.assertEqual('STOP_TIMEOUT',error.exception.code)
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.assertNotIn('completionResponseSha256',self.c.record)
        self.no_retry()

    def test_final_registry_check_crossing_deadline_preserves_intent(self):
        clock = SimpleNamespace(value=0); clock.monotonic = lambda:clock.value
        original = self.runtime._offline._exited
        checks = []
        def checked(*args):
            result = original(*args)
            if self.c.completion_ready:
                checks.append(1)
                if len(checks)==2: clock.value=2
            return result
        with patch.object(self.runtime._offline,'_exited',checked),patch.object(offline_process,'time',clock),patch.object(offline_control,'time',clock):
            with self.assertRaises(RegistryError) as error: self.complete(deadline=1)
        self.assertEqual('STOP_TIMEOUT',error.exception.code)
        self.assertEqual(2,len(checks))
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.assertEqual((1,2),(self.completes,self.c.pipe.acks))
        self.no_retry()

    def test_final_registry_exit_revalidation_prevents_result(self):
        original = self.c.verify
        changed = []
        def checked():
            original()
            if self.c.completion_ready and not changed:
                changed.append(1)
                saved = self.saved(); saved['workerExit']['responseSha256'] = '0'*64; self.save(saved)
        with patch.object(self.c,'verify',checked):
            with self.assertRaises(RegistryError): self.complete()
        self.assertEqual(1,len(changed))
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.assertEqual((1,2),(self.completes,self.c.pipe.acks))
        self.no_retry()

    def test_changed_local_exit_record_is_rejected_before_intent(self):
        self.w.record['workerExit']['ownerGroup'] = 'other'
        with self.assertRaises(RegistryError): self.complete()
        self.assertEqual(0,self.completes)
        self.assertNotIn('completion',self.saved())
        self.no_retry()

    def test_changed_registry_exit_record_is_rejected_before_intent(self):
        saved = self.saved(); saved['workerExit']['responseSha256'] = '0'*64; self.save(saved)
        with self.assertRaises(RegistryError): self.complete()
        self.assertEqual(0,self.completes)
        self.assertNotIn('completion',self.saved())
        self.no_retry()

    def test_expired_deadline_has_zero_complete_effect(self):
        with self.assertRaises(RegistryError): self.complete(deadline=0)
        self.assertEqual(0,self.completes)
        self.no_retry()

    def test_controller_death_before_complete_has_zero_effect(self):
        self.c.process.code = 1
        with self.assertRaises(RegistryError): self.complete()
        self.assertEqual(0,self.completes)
        self.no_retry()

    def test_registry_gate_change_before_complete_has_zero_effect(self):
        self.a.mutate('UPDATE run_admission SET revision=revision+1')
        with self.assertRaises(RegistryError): self.complete()
        self.assertEqual(0,self.completes)
        self.assertNotIn('completion',self.saved())
        self.no_retry()

    def test_competing_complete_and_start_keep_original_transaction_and_reservation(self):
        entered, release = threading.Event(), threading.Event()
        original = self.c.complete
        calls = []
        def held(*args):
            calls.append(1)
            self.assertEqual(1,len(calls),'C completeへ二重到達')
            self.assertNotIn('responseSha256',self.saved()['completion'])
            entered.set(); self.assertTrue(release.wait(10))
            return original(*args)
        with patch.object(self.c,'complete',held),ThreadPoolExecutor(max_workers=3) as pool:
            winner = pool.submit(self.complete)
            try:
                self.assertTrue(entered.wait(10))
                loser = pool.submit(self.complete)
                with self.assertRaises(RegistryError): loser.result(10)
                start = pool.submit(self.runtime.execute,'start',dict(self.token,timeoutSeconds=1),operation_id='start-during-complete')
                with self.assertRaises(RegistryError): start.result(10)
                self.assertEqual(0,self.completes)
                self.preserved()
            finally: release.set()
            self.assertEqual('COMPLETED',winner.result(10)['status'])
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        self.no_retry()

    def test_real_pipe_is_only_cleared_after_registry_result(self):
        pipe = ProxyPipe(); self.addCleanup(pipe.close_after_exit)
        old = self.c.pipe
        original_send = pipe.send
        def send(frame,deadline,verify):
            processes.Pipe.send(old,frame,deadline,verify)
            response = processes.Pipe.receive(old,deadline,verify)
            original_send(frame,deadline,verify)
            body = encode(response)
            os.write(pipe.child_output,struct.pack('!I',len(body))+body)
        original_ack = pipe.acknowledged
        def ack():
            self.assertIn('responseSha256',self.saved()['completion'])
            self.assertIsNotNone(pipe.outgoing)
            self.assertTrue(pipe.incoming)
            original_ack()
        self.c.pipe = pipe
        with patch.object(pipe,'send',send),patch.object(pipe,'acknowledged',ack):
            self.assertEqual('COMPLETED',self.complete()['status'])
        self.assertIsNone(pipe.outgoing)
        self.assertEqual(bytearray(),pipe.incoming)
        self.c.pipe = old
        self.preserved()


def exit_record_case(key):
    def test(self):
        exited = json.loads(self.w.exit_response)
        values = dict(status='UNKNOWN',operationId=processes.RUN,candidateResponseSha256='0'*64,
                      worker=dict(self.w.data_identity,startId='other'),ownerGroup='other',exitCode=False,allChildrenExited=False)
        exited[key] = values[key]
        self.w.exit_response = canonical(exited)
        self.w.record['workerExit'] = deepcopy(exited)
        saved = self.saved(); saved['workerExit']['responseSha256'] = digest(self.w.exit_response); self.save(saved)
        with self.assertRaises(RegistryError): self.complete()
        self.assertEqual(0,self.completes)
        self.assertFalse(self.c.completion_started)
        self.assertNotIn('completion',self.saved())
        self.no_retry()
    return test


for key in ('status','operationId','candidateResponseSha256','worker','ownerGroup','exitCode','allChildrenExited'):
    setattr(OfflineCompleteTests,'test_exit_'+key+'_mismatch_before_ipc',exit_record_case(key))


def response_case(key, value):
    def test(self):
        self.response_change = (key,value)
        with self.assertRaises((RegistryError,ValueError)): self.complete()
        self.assertEqual((1,2),(self.completes,self.c.pipe.acks))
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.no_retry()
    return test


for key,value in dict(status='UNKNOWN',operationId=processes.RUN,requestHash='0'*64,sessionId='old',nonce='old',
                      stateSha256='invalid',stateRevision=True,runUuid=processes.WORLD,candidateStateSha256='0'*64,
                      receiptSha256='0'*64,result=dict(requestId=processes.RUN,status='SUCCESS')).items():
    setattr(OfflineCompleteTests,'test_response_'+key+'_mismatch_keeps_buffer',response_case(key,value))
setattr(OfflineCompleteTests,'test_unknown_data_result_is_not_final',response_case('result',dict(requestId=processes.frame(True)['operationId'],status='UNKNOWN')))


def state_case(key):
    def test(self):
        self.current['hash'] = '6'*64
        if key=='writer': self.current['writer']['sessionId'] = processes.WORLD
        elif key.startswith('owner_'): self.current['writer']['owner'][key[6:]] = 123 if key=='owner_pid' else 'other'
        elif key.startswith('management_controller_'):
            field = key[len('management_controller_'):]
            self.current['writer']['management']['controller'][field] = 123 if field=='pid' else 'other'
        elif key.startswith('management_'):
            field = key[11:]
            self.current['writer']['management'][field] = 2 if field=='epoch' else 'other'
        elif key.startswith('worker_'): self.current['worker'][key[7:]] = 123 if key=='worker_pid' else 'other'
        elif key in ('db_closed','receipt_matches','seal_matches'): setattr(self,key,False)
        elif key.startswith('receipt_'):
            field = key[8:]
            self.receipt[field] = 99 if field=='revision' else dict(self.w.data_identity,startId='other') if field=='worker' else processes.RUN
        elif key=='revision': self.current[key]=6
        elif key=='run': self.current[key]=processes.RUN
        elif key=='world': self.current[key]=processes.RUN
        else: self.current[key]='other'
        self.align_candidate()
        with self.assertRaises(RegistryError): self.complete()
        self.assertTrue(self.outer_matched)
        self.assertEqual((0,1),(self.completes,self.c.process.code))
        self.assertEqual('Assigned2',self.held['phase'])
        self.no_retry()
    return test


for key in ('writer','owner_hostHash','owner_bootId','owner_pid','owner_startId',
            'management_leaseId','management_epoch','management_generation','management_drainSha256',
            'management_controller_hostHash','management_controller_bootId','management_controller_pid','management_controller_startId',
            'worker_hostHash','worker_bootId','worker_pid','worker_startId','generation','world','run','revision',
            'receipt_world','receipt_run','receipt_revision','receipt_worker','db_closed','receipt_matches','seal_matches'):
    setattr(OfflineCompleteTests,'test_state_'+key+'_mismatch_has_zero_complete_effect',state_case(key))
