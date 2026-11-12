"""C本人closeと元予約の応答記録を実Registry/Eventで検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import offline_control, offline_process, runtime_control, mod_control_startup
from . import test_offline_complete as completed
from . import test_offline_prepare as prepared
from . import test_offline_repeat as repeated
from . import test_offline_process as processes
from .build_inputs import canonical, digest
from .registry import RegistryError


class OfflineOwnerCloseTests(unittest.TestCase):
    def setUp(self):
        self.f = completed.OfflineCompleteTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.complete()
        self.configure(self.f, self.f.a.f.f, self.f.p, self.f.current, self.f.held)

    def configure(self, fixture, prepared_fixture, process_fixture, current, held):
        self.fixture, self.prepared, self.p = fixture, prepared_fixture, process_fixture
        self.runtime, self.token, self.c = fixture.runtime, fixture.token, fixture.c
        self.current, self.held = current, deepcopy(held)
        self.writer = deepcopy(current['writer'])
        self.original = deepcopy(self.saved())
        self.original_request = self.c.request
        self.clock, self.deadline = 0, 10
        self.closes = 0
        self.close_frame = self.reply = None
        self.outer_matched = False
        self.failure = self.response_change = None
        self.db_closed = self.seal_matches = True
        self.exit_on_reply = True
        self.before_effect = self.after_effect = lambda: None
        self.after_receive = lambda: None
        self.close_operation = 'close-owner'
        send, receive = processes.Pipe.send, processes.Pipe.receive

        def sending(pipe, frame, deadline, verify):
            if frame['command'] != 'close':
                return send(pipe, frame, deadline, verify)
            verify()
            pipe.sent.append(deepcopy(frame))
            self.close_frame = deepcopy(frame)
            self.assertEqual({'command','operationId','requestHash','sessionId','nonce',
                              'openOperationId','expectedStateSha256','expectedRevision'}, frame.keys())
            self.assertEqual(self.close_operation, self.saved()['ownerClose']['operationId'])
            if self.failure == 'partial':
                self.c.process.code = 1
                raise TimeoutError('fixture close部分送信')
            self.before_effect()
            verify()
            self.outer_matched = (frame['expectedStateSha256'], frame['expectedRevision']) == (
                self.current['hash'], self.current['revision'])
            opened = json.loads(self.c.request)
            valid = (self.outer_matched and self.current['phase'].startswith('Idle')
                     and all(self.current.get(k) == self.held.get(k) for k in ('hash','revision','run','previous'))
                     and self.current['writer'] == self.writer == self.held['writer']
                     and self.current['world'] == opened['worldUuid']
                     and self.current['generation'] == opened['generation']
                     and self.current['run'] == self.writer['sessionId']
                     and self.writer['owner'] == self.c.data_identity
                     and self.writer['management']['controller'] == self.c.parent_data
                     and all(self.writer['management'][k] == opened[k]
                             for k in ('leaseId','epoch','generation','drainSha256'))
                     and self.db_closed and self.seal_matches)
            base = {key: frame[key] for key in ('operationId','requestHash','sessionId','nonce')}
            if not valid:
                self.c.process.code = 1
                self.reply = dict(base, status='REJECTED')
                return
            self.closes += 1
            previous = self.current['hash']
            self.current.update(phase='Ready', hash='9'*64, revision=self.current['revision']+1, previous=previous)
            self.reply = dict(base, status='OWNER_CLOSED', stateSha256=self.current['hash'],
                              stateRevision=self.current['revision'], runUuid=self.current['run'])
            self.c.process.code = 0 if self.exit_on_reply else None
            if self.failure == 'published':
                self.c.process.code = 1
                self.reply = dict(base, status='UNKNOWN')
            self.after_effect()

        def receiving(pipe, deadline, verify):
            if pipe.sent[-1]['command'] != 'close':
                return receive(pipe, deadline, verify)
            if self.failure == 'lost':
                self.c.process.code = 1
                raise EOFError('fixture Ready公開後の応答喪失')
            verify()
            result = deepcopy(self.reply)
            if self.response_change:
                result[self.response_change[0]] = self.response_change[1]
            self.after_receive()
            return result

        self.enterContext(patch.object(processes.Pipe, 'send', sending))
        self.enterContext(patch.object(processes.Pipe, 'receive', receiving))
        clock = SimpleNamespace(monotonic=lambda: self.clock, sleep=time.sleep)
        for module in (offline_control, offline_process, runtime_control, mod_control_startup):
            self.enterContext(patch.object(module, 'time', clock))

    def saved(self):
        return self.prepared.saved()

    def save(self, value):
        self.prepared.mutate('UPDATE runtime_operations SET response=? WHERE id=?',
                             (json.dumps(value), json.loads(self.c.request)['operationId']))

    def close(self, operation_id=None):
        return self.runtime._offline_close_owner(self.token,
            operation_id=self.close_operation if operation_id is None else operation_id, deadline=self.deadline)

    def prepare_next(self):
        operation='00000000-0000-4000-8000-000000000009'
        request=deepcopy(processes.frame(True)['request'])
        request['requestId']=operation
        return self.runtime._offline_prepare(self.token,operation_id=operation,request=request,deadline=self.deadline)

    def counts(self):
        return self.closes, len(self.p.adapter.processes), [len(pipe.sent) for pipe in self.p.pipes], self.c.pipe.acks

    def pending(self):
        self.prepared.pending()
        self.assertIs(self.c, self.runtime._offline.controllers[self.token['instanceId']])
        self.assertEqual(self.original_request, self.c.request)
        self.assertIs(self.runtime._transaction.__func__, runtime_control.RuntimeControl._transaction)
        self.assertEqual({k:v for k,v in self.saved().items() if k != 'ownerClose'}, self.original)

    def no_retry(self):
        self.clock = 0
        before = self.counts(), deepcopy(self.saved())
        for operation in (self.close_operation, 'another-close'):
            with self.assertRaises(RegistryError):
                self.close(operation)
        with self.assertRaises(RegistryError):
            self.prepare_next()
        self.assertEqual(before, (self.counts(), self.saved()))
        self.pending()

    def test_completed_edit_closes_same_c_from_latest_idle_and_keeps_reservation(self):
        original_edits = json.loads(self.c.assignment_request)['request']
        self.assertTrue(original_edits['action']['products'])
        result = self.close()
        self.assertEqual(('OWNER_CLOSED',6,processes.RUN),
                         (result['status'],result['stateRevision'],result['runUuid']))
        self.assertEqual(('8'*64,5), (self.close_frame['expectedStateSha256'],self.close_frame['expectedRevision']))
        self.assertEqual(canonical(self.close_frame), self.c.close_request)
        self.assertEqual(canonical(result), self.c.close_response)
        self.assertEqual(digest(self.c.close_response), self.saved()['ownerClose']['responseSha256'])
        self.assertEqual(digest(self.c.close_request), self.c.record['ownerCloseRequestSha256'])
        self.assertEqual(1,self.closes)
        self.assertEqual(0,self.c.process.code)
        self.assertEqual('Ready',self.current['phase'])
        self.assertEqual(original_edits,json.loads(self.c.assignment_request)['request'])
        self.assertEqual(3,self.c.pipe.acks)
        self.pending()
        before = self.counts()
        with self.assertRaises(RegistryError):
            self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='start-after-owner-close')
        self.assertEqual(before,self.counts())
        result['stateRevision']=999
        self.assertEqual(6,json.loads(self.c.close_response)['stateRevision'])
        self.no_retry()

    def test_close_after_second_nonempty_edit_uses_latest_revision_and_history(self):
        fixture = repeated.OfflineRepeatTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.finish()
        self.configure(fixture,fixture.f.f.a.f.f,fixture.f.f.p,fixture.current,fixture.current)
        history = deepcopy(self.c.operation_history)
        requests = deepcopy(fixture.requests)
        self.assertEqual(9,fixture.current['revision'])
        result = self.close()
        self.assertEqual((10,processes.RUN),(result['stateRevision'],result['runUuid']))
        self.assertEqual(('7'*64,9),(self.close_frame['expectedStateSha256'],self.close_frame['expectedRevision']))
        self.assertEqual(history,self.c.operation_history)
        self.assertEqual(requests,fixture.requests)
        self.assertTrue(requests[0]['action']['products'])
        self.pending()
        self.no_retry()

    def test_open_without_worker_releases_startup_packet_before_close(self):
        fixture = prepared.OfflinePrepareTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        opened = json.loads(fixture.c.request)
        writer = deepcopy(self.writer)
        writer['owner'] = deepcopy(fixture.c.data_identity)
        writer['management'] = {k:opened[k] for k in ('leaseId','epoch','generation','drainSha256')}
        writer['management']['controller'] = deepcopy(fixture.c.parent_data)
        current = dict(phase='Idle1',hash='b'*64,revision=1,run=processes.RUN,
                       writer=writer,world=opened['worldUuid'],generation=opened['generation'])
        self.configure(fixture,fixture,fixture.p,current,current)
        self.assertFalse(self.c.startup_acknowledged)
        self.assertEqual('OWNER_CLOSED',self.close()['status'])
        self.assertTrue(self.c.startup_acknowledged)
        self.assertEqual(1,self.c.pipe.acks)
        self.assertEqual(1,len(self.p.adapter.processes))
        self.pending()
        self.no_retry()

    def test_live_c_or_remaining_child_keeps_only_owner_response_pending(self):
        self.exit_on_reply=False
        self.c.process.extra=[999]
        self.assertEqual('OWNER_CLOSED',self.close()['status'])
        self.assertIsNone(self.c.process.code)
        self.assertEqual([self.c.process.pid,999],self.c.process.members())
        self.pending()

    def test_no_old_worker_identity_or_exit_observation_is_repeated(self):
        worker = self.runtime._offline.workers[self.token['instanceId']]
        identity = self.p.adapter.identity
        def checked(pid):
            self.assertNotEqual(worker.process.pid,pid)
            return identity(pid)
        with patch.object(self.p.adapter,'identity',side_effect=checked), \
                patch.object(worker.process,'poll',side_effect=AssertionError('旧W再観測')), \
                patch.object(worker.process,'members',side_effect=AssertionError('旧W再観測')):
            self.assertEqual('OWNER_CLOSED',self.close()['status'])
        self.pending()

    def test_expired_deadline_has_effect_zero_and_does_not_consume_close(self):
        before=self.counts(),deepcopy(self.saved())
        self.clock=11
        with self.assertRaises(RegistryError) as caught:self.close()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual(before,(self.counts(),self.saved()))
        self.clock=0
        self.assertEqual('OWNER_CLOSED',self.close()['status'])

    def test_old_open_and_worker_operation_ids_have_effect_zero(self):
        for operation in (json.loads(self.c.request)['operationId'],self.f.operation):
            before=self.counts(),deepcopy(self.saved())
            with self.assertRaises(RegistryError):self.close(operation)
            self.assertEqual(before,(self.counts(),self.saved()))

    def test_missing_complete_ack_has_effect_zero(self):
        self.c.completion_acknowledged=False
        with self.assertRaises(RegistryError):self.close()
        self.assertEqual(0,self.closes)
        self.assertNotIn('ownerClose',self.saved())

    def test_missing_complete_result_has_effect_zero(self):
        saved=self.saved();saved['completion'].pop('responseSha256');self.save(saved)
        before=self.counts()
        with self.assertRaises(RegistryError):self.close()
        self.assertEqual(before,self.counts())
        self.assertNotIn('ownerClose',self.saved())

    def test_intent_write_failure_has_effect_zero(self):
        self.prepared.mutate("CREATE TRIGGER deny_owner_close BEFORE UPDATE OF response ON runtime_operations BEGIN SELECT RAISE(FAIL,'fixture intent'); END")
        before=self.counts()
        with self.assertRaises(sqlite3.Error):self.close()
        self.assertEqual(before,self.counts())
        self.assertNotIn('ownerClose',self.saved())

    def test_result_write_failure_keeps_ready_response_and_intent(self):
        self.after_effect=lambda:self.prepared.mutate("CREATE TRIGGER deny_owner_result BEFORE UPDATE OF response ON runtime_operations BEGIN SELECT RAISE(FAIL,'fixture result'); END")
        with self.assertRaises(sqlite3.Error):self.close()
        self.assertEqual(1,self.closes)
        self.assertEqual('Ready',self.current['phase'])
        self.assertIsNotNone(self.c.close_response)
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()

    def test_partial_send_has_effect_zero_and_nonzero_c(self):
        self.failure='partial'
        with self.assertRaises(TimeoutError):self.close()
        self.assertEqual((0,1),(self.closes,self.c.process.code))
        self.no_retry()

    def test_ready_publication_failure_keeps_one_effect_and_nonzero_c(self):
        self.failure='published'
        with self.assertRaises(RegistryError):self.close()
        self.assertEqual((1,1,'Ready'),(self.closes,self.c.process.code,self.current['phase']))
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()

    def test_response_loss_keeps_one_effect_and_nonzero_c(self):
        self.failure='lost'
        with self.assertRaises(EOFError):self.close()
        self.assertEqual((1,1,'Ready'),(self.closes,self.c.process.code,self.current['phase']))
        self.no_retry()

    def test_deadline_after_response_keeps_intent_without_success_record(self):
        self.after_receive=lambda:setattr(self,'clock',11)
        with self.assertRaises(RegistryError) as caught:self.close()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual(1,self.closes)
        self.assertIsNotNone(self.c.close_response)
        self.assertNotIn('ownerCloseResponseSha256',self.c.record)
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()

    def test_final_registry_comparison_crossing_deadline_has_no_success_record(self):
        original=offline_control.canonical
        def crossing(value):
            result=original(value)
            caller=sys._getframe(1)
            if caller.f_code.co_name=='close_owner' and 'current_idle' in caller.f_locals:
                self.clock=11
            return result
        with patch.object(offline_control,'canonical',crossing):
            with self.assertRaises(RegistryError) as caught:self.close()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.assertEqual(1,self.closes)
        self.no_retry()

    def test_result_commit_deadline_preserves_known_hash_without_return(self):
        transaction=self.runtime.registry.transaction
        @contextmanager
        def traced(*args,**kwargs):
            with transaction(*args,**kwargs) as db:
                def trace(sql):
                    if self.c.close_ready and sql.startswith('UPDATE runtime_operations SET response='):
                        self.clock=11
                db.set_trace_callback(trace)
                yield db
        with patch.object(self.runtime.registry,'transaction',side_effect=traced):
            with self.assertRaises(RegistryError) as caught:self.close()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual(digest(self.c.close_response),self.saved()['ownerClose']['responseSha256'])
        self.assertEqual(1,self.closes)
        self.no_retry()

    def test_local_record_failure_keeps_originals_without_retry(self):
        record=self.c._record
        def failed():
            if self.c.close_response is not None:raise OSError('fixture journal')
            record()
        with patch.object(self.c,'_record',side_effect=failed):
            with self.assertRaises(OSError):self.close()
        self.assertEqual(1,self.closes)
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()

    def test_exit_between_poll_and_identity_uses_original_handle(self):
        identity=self.p.adapter.identity
        self.exit_on_reply=False
        def gone(pid):
            if pid==self.c.process.pid and self.closes:
                self.c.process.code=0
                return None
            return identity(pid)
        with patch.object(self.p.adapter,'identity',side_effect=gone):
            self.assertEqual('OWNER_CLOSED',self.close()['status'])
        self.pending()

    def test_contradictory_identity_is_rejected_even_when_handle_exits_zero(self):
        identity=self.p.adapter.identity
        self.exit_on_reply=False
        def changed(pid):
            value=identity(pid)
            if pid==self.c.process.pid and self.closes:
                self.c.process.code=0
                return dict(value,commandHash='0'*64)
            return value
        with patch.object(self.p.adapter,'identity',side_effect=changed):
            with self.assertRaises(RegistryError):self.close()
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()

    def test_last_os_observation_cannot_replace_creation_snapshot(self):
        poll=self.c.process.poll
        def changed():
            code=poll()
            if 'ownerCloseResponseSha256' in self.c.record:
                self.c.record['creation']['ownerGroup']='other-group'
            return code
        with patch.object(self.c.process,'poll',side_effect=changed):
            with self.assertRaises(RegistryError):self.close()
        self.assertEqual(1,self.closes)
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()

    def test_empty_close_intent_keeps_all_new_admission_blocked(self):
        saved=self.saved()
        saved['ownerClose']={}
        self.save(saved)
        before=self.counts(),deepcopy(self.saved())
        with self.assertRaises(RegistryError):self.prepare_next()
        with self.assertRaises(RegistryError):self.close()
        self.assertEqual(before,(self.counts(),self.saved()))

    def test_missing_identity_with_unknown_handle_exit_preserves_intent(self):
        identity=self.p.adapter.identity
        self.exit_on_reply=False
        def missing(pid):
            if pid==self.c.process.pid and self.closes:return None
            return identity(pid)
        with patch.object(self.p.adapter,'identity',side_effect=missing):
            with self.assertRaises(RegistryError):self.close()
        self.assertIsNone(self.c.process.code)
        self.assertEqual(1,self.closes)
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()

    def test_nonzero_c_exit_with_owner_closed_packet_is_rejected(self):
        self.after_effect=lambda:setattr(self.c.process,'code',1)
        with self.assertRaises(RegistryError):self.close()
        self.assertEqual('OWNER_CLOSED',self.reply['status'])
        self.assertEqual((1,1),(self.closes,self.c.process.code))
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()

    def test_close_intent_blocks_competing_close_prepare_and_start(self):
        entered,proceed=threading.Event(),threading.Event()
        self.addCleanup(proceed.set)
        def hold():
            entered.set()
            self.assertTrue(proceed.wait(10))
        self.before_effect=hold
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.close)
            try:
                self.assertTrue(entered.wait(10))
                before=self.counts(),deepcopy(self.saved())
                with self.assertRaises(RegistryError):self.close('competing-close')
                with self.assertRaises(RegistryError):self.prepare_next()
                with self.assertRaises(RegistryError):
                    self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='competing-start')
                self.assertEqual(before,(self.counts(),self.saved()))
            finally:
                proceed.set()
            self.assertEqual('OWNER_CLOSED',future.result(10)['status'])
        self.assertEqual(1,self.closes)
        self.pending()


def response_case(field,value):
    def test(self):
        self.response_change=(field,value)
        error_type=ValueError if field=='stateSha256' else RegistryError
        with self.assertRaises(error_type):self.close()
        self.assertEqual(1,self.closes)
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()
    return test


for field,value in (('status','UNKNOWN'),('operationId','old-operation'),('requestHash','0'*64),
                    ('sessionId','old-session'),('nonce','old-nonce'),('stateRevision',5),
                    ('stateRevision',True),('runUuid',processes.WORLD),('stateSha256','bad')):
    suffix='_bool' if type(value) is bool else ''
    setattr(OfflineOwnerCloseTests,'test_close_response_'+field+suffix+'_mismatch',response_case(field,value))


def state_case(field):
    def test(self):
        if field=='phase':self.current['phase']='Assigned'
        elif field=='world':self.current['world']='00000000-0000-4000-8000-000000000009'
        elif field=='generation':self.current['generation']='00000000-0000-4000-8000-000000000009'
        elif field in ('hash','revision','run'):
            self.current[field]={'hash':'4'*64,'revision':99,'run':'00000000-0000-4000-8000-000000000009'}[field]
            response=json.loads(self.c.completion_response)
            response[{'hash':'stateSha256','revision':'stateRevision','run':'runUuid'}[field]]=self.current[field]
            self.c.completion_response=canonical(response)
            value=digest(self.c.completion_response)
            for key in ('completionResponseSha256','completionAcknowledgedSha256'):self.c.record[key]=value
            saved=self.saved();saved['completion']['responseSha256']=value;self.save(saved)
            self.original=deepcopy(self.saved())
        elif field=='previous':self.current['previous']='4'*64
        elif field=='writerSessionId':self.current['writer']['sessionId']='00000000-0000-4000-8000-000000000009'
        elif field=='writerGeneration':self.current['writer']['management']['generation']='00000000-0000-4000-8000-000000000009'
        elif field=='owner':self.current['writer']['owner']=dict(self.writer['owner'],startId='other')
        elif field=='controller':self.current['writer']['management']['controller']=dict(self.writer['management']['controller'],startId='other')
        elif field=='db':self.db_closed=False
        elif field=='seal':self.seal_matches=False
        else:self.current['writer']['management'][field]='other' if field!='epoch' else 99
        with self.assertRaises(RegistryError):self.close()
        self.assertTrue(self.outer_matched)
        self.assertEqual((0,1),(self.closes,self.c.process.code))
        self.assertNotIn('responseSha256',self.saved()['ownerClose'])
        self.no_retry()
    return test


for field in ('phase','world','generation','hash','revision','run','previous','writerSessionId','writerGeneration',
              'owner','controller','leaseId','epoch','drainSha256','db','seal'):
    setattr(OfflineOwnerCloseTests,'test_close_state_'+field+'_mismatch_with_matching_outer',state_case(field))


def registry_case(field,value):
    def test(self):
        self.prepared.mutate('UPDATE instances SET '+field+'=? WHERE id=?',(value,self.token['instanceId']))
        before=self.counts()
        with self.assertRaises(RegistryError):self.close()
        self.assertEqual(before,self.counts())
        self.assertNotIn('ownerClose',self.saved())
    return test


for field,value in (('state','RUNNING'),('pending',0),('writers',1),('children',1),('epoch',99),
                    ('generation','00000000-0000-4000-8000-000000000009'),('lease','other-lease'),('generation_bound',0),('expires',0)):
    setattr(OfflineOwnerCloseTests,'test_close_registry_'+field+'_mismatch_before_effect',registry_case(field,value))
