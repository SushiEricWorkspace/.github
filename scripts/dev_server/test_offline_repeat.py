"""同Cの操作別保持・連続編集と不明時の正本保全を検査する。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from . import offline_control, offline_process, runtime_control
from . import test_offline_next_prepare as previous
from . import test_offline_process as processes
from .build_inputs import canonical, digest
from .registry import RegistryError

THIRD = '00000000-0000-4000-8000-000000000005'


class OfflineRepeatTests(unittest.TestCase):
    def setUp(self):
        f = previous.OfflineNextPrepareTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        if self._testMethodName.startswith('test_assign_state_'):
            f.f.current['hash']='a'*64
            response=json.loads(f.c.completion_response);response['stateSha256']='a'*64
            f.c.completion_response=canonical(response)
            value=digest(f.c.completion_response)
            for field in ('completionResponseSha256','completionAcknowledgedSha256'):f.c.record[field]=value
            saved=f.saved();saved['completion']['responseSha256']=value;f.save(saved)
        f.prepare()
        self.f, self.c, self.runtime, self.token = f, f.c, f.runtime, f.token
        self.original = deepcopy(f.original)
        self.old = f.old
        self.operation = previous.NEXT
        self.current = deepcopy(f.f.current)
        self.current['phase'] = 'Idle'
        self.writer = deepcopy(self.current['writer'])
        self.held = None
        self.effects = dict(assigns=0,begins=0,dbOpens=0,edits=0,dbCloses=0,finishes=0,completes=0)
        self.requests = []
        self.failure = self.change = None
        self.outer_matched = False
        self.before = self.after = lambda command:None
        self.response = self.replay = None
        self.request = deepcopy(f.request)
        self.request['requestId'] = THIRD
        self.request['action']['products'][0]['id'] = 'tab.third'
        self.request['action']['products'][0]['definition']['purchasePrice'] = 789
        send, receive = processes.Pipe.send, processes.Pipe.receive

        def sending(pipe, frame, deadline, verify):
            command = frame['command']
            if command == 'open': return send(pipe,frame,deadline,verify)
            verify()
            pipe.sent.append(deepcopy(frame))
            if self.failure == (command,'partial'): raise TimeoutError('fixture 部分送信')
            self.before(command)
            verify()
            current = self.current
            op = frame['operationId']
            assigned_hash,pending_hash,candidate_hash,idle_hash,edit_hash,receipt_hash = ('2','4','5','7','3','6') if op==previous.NEXT else ('9','b','c','d','e','f')
            base = {k:frame[k] for k in ('operationId','requestHash','sessionId','nonce')}
            w = self.worker()
            valid = True
            if command == 'prepare':
                valid = current['phase']=='Idle' and (current['hash'],current['revision'],current['run']) == (
                    frame['expectedStateSha256'],frame['expectedRevision'],frame['runUuid'])
                self.response = dict(base,status='PREPARED',stateSha256=current['hash'],stateRevision=current['revision'],runUuid=current['run'])
            elif command == 'assign':
                self.outer_matched = (current['hash'],current['revision'],current['run']) == (
                    frame['expectedStateSha256'],frame['expectedRevision'],frame['runUuid'])
                valid = (self.outer_matched and current['phase']=='Idle' and current['writer']==self.writer
                         and current['world']==processes.WORLD and current['generation']==json.loads(self.c.request)['generation']
                         and frame['worker']==w.data_identity and current['run']==self.writer['sessionId'])
                if valid:
                    self.effects['assigns'] += 1
                    self.requests.append(deepcopy(frame['request']))
                    current.update(phase='Assigned',hash=assigned_hash*64,revision=current['revision']+1,
                                   previous=frame['expectedStateSha256'],worker=deepcopy(frame['worker']),
                                   operation=op,editHash=edit_hash*64,resume=None)
                    self.held = deepcopy(current)
                self.response = dict(base,status='ASSIGNED',stateSha256=current['hash'],stateRevision=current['revision'],
                                     runUuid=current['run'],editRequestSha256=edit_hash*64)
            elif command == 'admit':
                self.outer_matched = (current['hash'],current['revision'],current['run']) == (
                    frame['expectedStateSha256'],frame['expectedRevision'],frame['runUuid'])
                expected = dict(previous=json.loads(w.request)['expectedStateSha256'],writer=self.writer,
                                worker=w.data_identity,operation=op,editHash=frame['editRequestSha256'],resume=None)
                valid = self.outer_matched and current['phase']=='Assigned' and all(current[k]==v for k,v in expected.items())
                if valid:
                    self.effects['begins'] += 1
                    current.update(phase='Pending',hash=pending_hash*64,revision=current['revision']+1,run=op,previous=assigned_hash*64)
                    self.requests.append(deepcopy(json.loads(w.request)['request']))
                    if self.failure == ('admit','pending'):
                        w.process.code=1
                        valid=False
                    else:
                        for k in ('dbOpens','edits','dbCloses','finishes'): self.effects[k]+=1
                        self.requests.append(deepcopy(json.loads(w.request)['request']))
                        current.update(phase='SavedPendingExit',hash=candidate_hash*64,revision=current['revision']+1,previous=pending_hash*64)
                        w.process.code=0
                self.response = dict(base,status='CANDIDATE',stateSha256=current['hash'],stateRevision=current['revision'],
                    runUuid=op,worldUuid=current['world'],receiptSha256=receipt_hash*64,editRequestSha256=edit_hash*64)
            elif command == 'complete':
                self.outer_matched = (current['hash'],current['revision'],current['world'],current['run']) == (
                    frame['expectedStateSha256'],frame['expectedRevision'],frame['worldUuid'],frame['runUuid'])
                valid = (self.outer_matched and current['phase']=='SavedPendingExit' and self.held['phase']=='Assigned'
                         and current['writer']==self.held['writer'] and current['worker']==self.held['worker']
                         and current['revision']==self.held['revision']+2 and current['run']==op
                         and frame['receiptSha256']==receipt_hash*64 and frame['editRequestSha256']==self.held['editHash'])
                if valid:
                    self.effects['completes'] += 1
                    current.update(phase='Idle',hash=idle_hash*64,revision=current['revision']+1,run=self.writer['sessionId'],previous=candidate_hash*64)
                result=dict(version=1,requestId=op,status='SUCCESS',result=dict(
                    type='io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterResult.Edited',
                    transactionIds=[8 if op==previous.NEXT else 9]),code=None,message=None,rejectedBeforeEffect=False)
                self.response = dict(base,status='COMPLETED',stateSha256=current['hash'],stateRevision=current['revision'],
                    runUuid=current['run'],receiptSha256=receipt_hash*64,candidateStateSha256=frame['expectedStateSha256'],result=result)
            else: self.fail(command)
            if not valid:
                (w if command in ('prepare','admit') else self.c).process.code=1
                self.response=dict(base,status='REJECTED')
            self.after(command)

        def receiving(pipe, deadline, verify):
            frame = pipe.sent[-1]
            if frame['command']=='open': return receive(pipe,deadline,verify)
            verify()
            if self.failure == (frame['command'],'lost'):
                (self.worker() if frame['command'] in ('prepare','admit') else self.c).process.code=1
                raise EOFError('fixture 作用後応答喪失')
            result=json.loads(self.replay) if self.replay is not None else deepcopy(self.response)
            if self.change: result[self.change[0]]=self.change[1]
            return result
        self.enterContext(patch.object(processes.Pipe,'send',sending))
        self.enterContext(patch.object(processes.Pipe,'receive',receiving))
        self.members()

    def worker(self): return self.runtime._offline.workers[self.token['instanceId']]
    def saved(self): return self.f.saved()
    def save(self,value): return self.f.save(value)
    def members(self):
        w=self.worker()
        w.process.members=lambda:([w.process.pid] if w.process.code is None else [])+w.process.extra
    def call(self,stage,operation=None,deadline=None):
        return getattr(self.runtime,'_offline_'+stage)(self.token,operation_id=self.operation if operation is None else operation,
            deadline=time.monotonic()+10 if deadline is None else deadline)
    def next(self):
        result=self.runtime._offline_prepare(self.token,operation_id=THIRD,request=self.request,deadline=time.monotonic()+10)
        self.operation=THIRD
        self.members()
        return result
    def finish(self):
        try:
            for stage in ('assign','admit','observe_worker_exit','complete'): result=self.call(stage)
        except RegistryError as error: self.fail('照合済み最新Idleの連続操作拒否: '+error.code)
        return result
    def counts(self):
        return deepcopy(self.effects),[len(p.sent) for p in self.f.f.p.pipes],len(self.f.f.p.adapter.processes),self.c.pipe.acks
    def pending(self):
        self.f.f.a.f.f.pending()
        self.assertIs(self.old,self.runtime._offline.history[self.token['instanceId']][0][0])
        self.assertEqual(self.original,self.f.original)
    def no_retry(self,stage):
        before=self.counts()
        with self.assertRaises(RegistryError): self.call(stage)
        self.assertEqual(before,self.counts())
        self.pending()

    def test_same_c_runs_second_and_third_edits_from_latest_idle(self):
        self.assertIs(self.runtime._transaction.__func__,runtime_control.RuntimeControl._transaction)
        cpipe=self.c.pipe
        first=deepcopy(self.c.operation_history)
        second_request=json.loads(self.worker().request)['request']
        original_held=self.c.operation_history[self.f.f.operation]
        self.assertEqual(json.loads(self.old.request)['request'],json.loads(original_held['assignment_request'])['request'])
        self.assertEqual(self.f.f.operation,json.loads(original_held['completion_request'])['operationId'])
        self.before=lambda command:self.assertIn(
            {'assign':'assignment','admit':'admission','complete':'completion'}.get(command,'worker'),self.saved())
        result=self.finish()
        self.assertEqual((9,previous.NEXT,[8]),(result['stateRevision'],result['result']['requestId'],result['result']['result']['transactionIds']))
        self.assertEqual([second_request]*3,self.requests)
        second=self.worker()
        old_exit=second.exit_response
        self.next()
        self.assertEqual(9,json.loads(self.worker().request)['expectedRevision'])
        self.assertIs(second,self.runtime._offline.history[self.token['instanceId']][1][0])
        self.assertEqual(old_exit,second.exit_response)
        self.assertEqual(first[next(iter(first))],self.c.operation_history[next(iter(first))])
        self.assertEqual(digest(self.c.operation_history[previous.NEXT]['completion_response']),self.saved()['history'][1]['completion']['responseSha256'])
        result=self.finish()
        self.assertEqual((13,THIRD,[9]),(result['stateRevision'],result['result']['requestId'],result['result']['result']['transactionIds']))
        self.assertEqual([self.request]*3,self.requests[-3:])
        self.assertEqual(dict.fromkeys(self.effects,2),self.effects)
        self.assertEqual(4,len(self.f.f.p.adapter.processes))
        self.assertEqual(4,len({p.group for p in self.f.f.p.adapter.processes}))
        self.assertIs(cpipe,self.c.pipe)
        self.assertEqual(7,cpipe.acks)
        self.pending()
        for secret in (self.c.session,self.c.nonce,'tab.third','purchasePrice','transactionIds'):
            self.assertNotIn(secret,json.dumps(self.saved()))
        result['result']['result']['transactionIds'].append(100)
        self.assertEqual([9],json.loads(self.c.completion_response)['result']['result']['transactionIds'])

    def test_old_stages_never_send_or_generate(self):
        for stage in ('assign','admit','observe_worker_exit','complete'):
            before=self.counts()
            with self.assertRaises(RegistryError): self.call(stage,self.f.f.operation)
            self.assertEqual(before,self.counts())
        self.assertNotIn('assignment',self.saved())

    def test_old_assigned_response_never_acks_or_retries(self):
        self.change=('operationId',self.f.f.operation)
        with self.assertRaises(RegistryError): self.call('assign')
        self.assertEqual(1,self.effects['assigns'])
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry('assign')

    def test_old_candidate_packet_does_not_become_next_result(self):
        self.call('assign')
        self.replay=self.old.candidate_response
        with self.assertRaises(RegistryError):self.call('admit')
        self.assertNotIn('candidateResponseSha256',self.saved()['admission'])
        self.no_retry('admit')

    def test_old_completed_packet_does_not_ack_next_operation(self):
        for stage in ('assign','admit','observe_worker_exit'):self.call(stage)
        self.replay=self.c.operation_history[self.f.f.operation]['completion_response']
        with self.assertRaises(RegistryError):self.call('complete')
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.assertFalse(self.c.completion_acknowledged)
        self.no_retry('complete')

    def test_assign_intent_write_failure_has_zero_effect(self):
        self.f.f.a.mutate("CREATE TRIGGER deny BEFORE UPDATE OF response ON runtime_operations BEGIN SELECT RAISE(FAIL,'intent'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.call('assign')
        self.assertEqual(dict.fromkeys(self.effects,0),self.effects)
        self.assertFalse(self.c.assignment_started)

    def test_assign_result_write_failure_preserves_effect_without_ack(self):
        self.after=lambda command:self.f.f.a.mutate("CREATE TRIGGER deny BEFORE UPDATE OF response ON runtime_operations BEGIN SELECT RAISE(FAIL,'result'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.call('assign')
        self.assertEqual(1,self.effects['assigns'])
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry('assign')

    def test_assign_ack_failure_preserves_history_and_intent(self):
        with patch.object(self.c.pipe,'acknowledged',side_effect=OSError('fixture ACK')):
            with self.assertRaises(OSError): self.call('assign')
        self.assertIn('responseSha256',self.saved()['assignment'])
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry('assign')

    def test_completion_result_write_failure_keeps_idle_without_ack(self):
        for stage in ('assign','admit','observe_worker_exit'):self.call(stage)
        self.after=lambda command:self.f.f.a.mutate("CREATE TRIGGER deny BEFORE UPDATE OF response ON runtime_operations BEGIN SELECT RAISE(FAIL,'complete result'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.call('complete')
        self.assertEqual(1,self.effects['completes'])
        self.assertFalse(self.c.completion_acknowledged)
        self.no_retry('complete')

    def test_final_assignment_history_change_prevents_result_ack(self):
        self.after=lambda command:self.c.operation_history.clear()
        with self.assertRaises(RegistryError):self.call('assign')
        self.assertEqual(1,self.effects['assigns'])
        self.assertFalse(self.c.assignment_acknowledged)
        self.no_retry('assign')

    def test_completion_final_transaction_deadline_crossing(self):
        for stage in ('assign','admit','observe_worker_exit'):self.call(stage)
        clock=[0]
        exited=self.runtime._offline._exited
        def checked(*args):
            result=exited(*args)
            if self.c.completion_ready:clock[0]=11
            return result
        with patch.object(offline_control.time,'monotonic',lambda:clock[0]),patch.object(self.runtime._offline,'_exited',checked):
            with self.assertRaises(RegistryError) as error:self.call('complete',deadline=10)
        self.assertEqual('STOP_TIMEOUT',error.exception.code)
        self.assertNotIn('responseSha256',self.saved()['completion'])
        self.no_retry('complete')

    def test_concurrent_second_assign_is_effected_once(self):
        entered,release=threading.Event(),threading.Event()
        def before(command):
            entered.set()
            self.assertTrue(release.wait(10))
        self.before=before
        with ThreadPoolExecutor(2) as pool:
            first=pool.submit(self.call,'assign')
            self.assertTrue(entered.wait(10))
            second_entered=threading.Event()
            def second():
                second_entered.set()
                with self.assertRaises(RegistryError):self.call('assign')
            other=pool.submit(second)
            self.assertTrue(second_entered.wait(10))
            release.set()
            self.assertEqual('ASSIGNED',first.result(10)['status']);other.result(10)
        self.assertEqual(1,self.effects['assigns'])
        self.assertEqual(3,len(self.f.f.p.adapter.processes))
        self.pending()

    def test_start_during_second_assignment_preserves_pending(self):
        entered,release=threading.Event(),threading.Event()
        def before(command):
            entered.set()
            self.assertTrue(release.wait(10))
        self.before=before
        with ThreadPoolExecutor(2) as pool:
            assignment=pool.submit(self.call,'assign')
            self.assertTrue(entered.wait(10))
            with self.assertRaises(RegistryError):
                self.runtime.execute('start',dict(self.token,timeoutSeconds=1),operation_id='competing-repeat-start')
            release.set()
            self.assertEqual('ASSIGNED',assignment.result(10)['status'])
        self.assertEqual(3,len(self.f.f.p.adapter.processes))
        self.pending()


def history_case(key):
    def test(self):
        held=self.c.operation_history[next(iter(self.c.operation_history))]
        if key=='missing':self.c.operation_history.clear()
        elif key=='registry':
            saved=self.saved();saved['history'][0]['completion']['responseSha256']='a'*64;self.save(saved)
        elif key=='memory':self.runtime._offline.history[self.token['instanceId']][0][1]['assignment']['responseSha256']='a'*64
        elif key=='response':
            response=json.loads(held['completion_response']);response['stateRevision']=1;held['completion_response']=canonical(response)
        elif key=='record':held['record']['completionAcknowledgedSha256']='a'*64
        else:held[key]=False
        with self.assertRaises(RegistryError):self.call('assign')
        self.assertEqual(dict.fromkeys(self.effects,0),self.effects)
        self.assertNotIn('assignment',self.saved())
    return test

for key in ('missing','registry','memory','response','record','assignment_acknowledged','completion_ready','completion_acknowledged'):
    setattr(OfflineRepeatTests,'test_history_'+key,history_case(key))


def failure_case(stage,failure):
    def test(self):
        if stage in ('admit','complete'):self.call('assign')
        if stage=='complete':
            self.call('admit');self.call('observe_worker_exit')
        before=deepcopy(self.effects)
        self.failure=(stage,failure)
        with self.assertRaises((RegistryError,EOFError,TimeoutError)):self.call(stage)
        peer=self.worker() if stage=='admit' else self.c
        if failure in ('lost','pending'):self.assertEqual(1,peer.process.code)
        if failure=='partial':self.assertEqual(before,self.effects)
        self.no_retry(stage)
    return test

for stage in ('assign','admit','complete'):
    for failure in ('partial','lost'):
        setattr(OfflineRepeatTests,'test_'+stage+'_'+failure,failure_case(stage,failure))
setattr(OfflineRepeatTests,'test_admit_pending',failure_case('admit','pending'))


def state_case(stage,key):
    def test(self):
        if stage=='admit':self.call('assign')
        before=deepcopy(self.effects)
        if stage=='admit':
            self.current['hash']='a'*64
            assigned=json.loads(self.c.assignment_response);assigned['stateSha256']='a'*64
            self.c.assignment_response=canonical(assigned)
            value=digest(self.c.assignment_response)
            for field in ('assignedResponseSha256','assignedAcknowledgedSha256'):self.c.record[field]=value
            saved=self.saved();saved['assignment']['responseSha256']=value;self.save(saved)
        def change(command):
            current=self.current
            if key.startswith('owner_'):current['writer']['owner'][key[6:]]='mismatch'
            elif key.startswith('controller_'):current['writer']['management']['controller'][key[11:]]='mismatch'
            elif key.startswith('management_'):current['writer']['management'][key[11:]]='mismatch'
            elif key=='writer':current['writer']['sessionId']='mismatch'
            elif key=='resume':current['resume']='a'*64
            else:current[key]='mismatch'
        self.before=change
        with self.assertRaises(RegistryError):self.call(stage)
        self.assertTrue(self.outer_matched)
        self.assertEqual(before,self.effects)
        self.assertEqual(1,(self.worker() if stage=='admit' else self.c).process.code)
        self.no_retry(stage)
    return test

for key in ('world','generation','phase','writer',*[p+'_'+k for p in ('owner','controller') for k in ('pid','startId','hostHash','bootId')],
            *['management_'+k for k in ('leaseId','epoch','generation','drainSha256')]):
    setattr(OfflineRepeatTests,'test_assign_state_'+key,state_case('assign',key))
for key in ('previous','writer','resume','worker','operation','editHash'):
    setattr(OfflineRepeatTests,'test_admit_state_'+key,state_case('admit',key))
