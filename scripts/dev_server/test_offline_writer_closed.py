"""解放正本からのCLOSED記録と予約保持を実Registryで検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch

from . import offline_control
from . import test_offline_worker_release as released
from .build_inputs import canonical, digest
from .registry import RegistryError


class OfflineWriterClosedTests(unittest.TestCase):
    def setUp(self):
        self.base=released.OfflineWorkerReleaseTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.release();self.configure()

    def configure(self):
        self.f,self.c,self.runtime,self.token=self.base.f,self.base.c,self.base.runtime,self.base.token
        self.before=deepcopy(self.f.saved())
        self.workers=list(self.base.workers)
        self.originals=self.c.close_request,self.c.close_response,self.c.owner_exit_response,self.c.owner_release_response,self.c.worker_release_response
        self.counts=[self.c.process.close.call_count,*[w.process.close.call_count for w in self.workers]]
        self.order=list(self.base.order)
        self.frames=deepcopy(self.c.pipe.sent)

    def record(self,**changes):
        args=dict(operation_id=self.f.close_operation,deadline=self.f.deadline);args.update(changes)
        return self.runtime._offline_record_writer_closed(self.token,**args)

    def pending(self):
        self.f.prepared.pending()
        self.assertEqual(self.before,{k:v for k,v in self.f.saved().items() if k!='writerClosed'})
        self.assertEqual(self.originals,(self.c.close_request,self.c.close_response,self.c.owner_exit_response,
            self.c.owner_release_response,self.c.worker_release_response))
        self.assertEqual(self.counts,[self.c.process.close.call_count,*[w.process.close.call_count for w in self.workers]])
        self.assertEqual(self.order,self.base.order);self.assertEqual(self.frames,self.c.pipe.sent)
        self.assertIs(self.c,self.runtime._offline.controllers[self.token['instanceId']])

    def no_retry(self):
        self.f.clock=0
        saved=deepcopy(self.f.saved())
        for operation in (self.f.close_operation,'other-writer-close'):
            with self.assertRaises(RegistryError):self.record(operation_id=operation)
        with self.assertRaises(RegistryError):self.f.close()
        with self.assertRaises(RegistryError):self.f.prepare_next()
        with self.assertRaises(RegistryError):
            self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='start-after-writer-closed')
        self.assertEqual(saved,self.f.saved())

    def rejected(self):
        with self.assertRaises(RegistryError):self.record()
        self.assertNotIn('writerClosed',self.f.saved())

    def normal(self):
        result=self.record()
        self.assertEqual(('OFFLINE_WRITER_CLOSED','CLOSED'),(result['status'],result['writerStatus']))
        self.assertEqual(digest(self.c.owner_release_response),result['ownerReleaseSha256'])
        self.assertEqual(digest(self.c.worker_release_response),result['workerReleaseSha256'])
        self.assertEqual(canonical(result),self.c.writer_closed_response)
        self.assertEqual(result,self.c.record['writerClosed'])
        saved=self.f.saved()['writerClosed']
        self.assertEqual('CLOSED',saved['state'])
        self.assertEqual(digest(canonical(result)),saved['responseSha256'])
        result['writerStatus']='UNKNOWN';self.assertEqual('CLOSED',self.c.record['writerClosed']['writerStatus'])
        self.pending();self.no_retry()

    def test_closed_records_release_proofs_and_keeps_original_pending_reservation(self):self.normal()

    def test_history_workers_are_included(self):
        self.base.history();self.base.release();self.configure()
        self.assertEqual(2,len(self.workers));self.normal()

    def test_open_without_worker_records_closed(self):
        fixture=released.prepared.OfflinePrepareTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        opened=json.loads(fixture.c.request)
        writer=deepcopy(self.f.writer);writer['owner']=deepcopy(fixture.c.data_identity)
        writer['management']={k:opened[k] for k in ('leaseId','epoch','generation','drainSha256')}
        writer['management']['controller']=deepcopy(fixture.c.parent_data)
        current=dict(phase='Idle1',hash='b'*64,revision=1,run=released.owner.exited.closing.processes.RUN,
            writer=writer,world=opened['worldUuid'],generation=opened['generation'])
        exited=self.base.base.base
        exited.f.configure(fixture,fixture,fixture.p,current,current)
        exited.start_close();exited.observe()
        self.base.base.configure(exited.f);self.base.base.release()
        self.base.configure();self.base.release();self.configure()
        self.assertEqual([],self.workers);self.normal()

    def game(self):
        game=released.owner.game_tests.GameOwnerCloseDeadlineTests()
        game.setUp();self.addCleanup(game.doCleanups)
        game.open();game.edit(21)
        case=game.close_fixture(clock=31,deadline=40);case.close()
        case.runtime._offline_observe_owner_exit(case.token,operation_id=case.close_operation,deadline=40)
        self.base.base.configure(case);self.base.base.release()
        self.base.configure();self.base.release();self.configure()
        return case

    def test_game_source_does_not_use_open_deadline_as_lifetime(self):
        case=self.game();case.clock,case.deadline=35,50
        callback=self.c.verify_owner;self.normal();self.assertIs(callback,self.c.verify_owner)

    def test_game_source_cannot_extend_close_deadline(self):
        case=self.game();case.clock,case.deadline=40,50;self.rejected()

    def test_close_deadline_cannot_be_extended(self):
        self.f.clock,self.f.deadline=10,20;self.rejected()

    def test_shorter_caller_deadline_is_used(self):
        self.f.clock,self.f.deadline=5,5;self.rejected()

    def test_owner_release_hash_required(self):
        saved=self.f.saved();saved['ownerRelease']['responseSha256']='0'*64;self.f.save(saved);self.rejected()

    def test_worker_release_hash_required(self):
        saved=self.f.saved();saved['workerRelease']['responseSha256']='0'*64;self.f.save(saved);self.rejected()

    def test_worker_release_raw_required(self):
        self.c.worker_release_response=None;self.rejected()

    def test_worker_release_local_record_required(self):
        self.c.record['workerRelease']['handlesReleased']=False;self.rejected()

    def test_worker_handle_released_progress_required(self):
        next(iter(self.c.worker_release.values()))['handle']='UNKNOWN';self.rejected()

    def test_worker_pipe_released_progress_required(self):
        next(iter(self.c.worker_release.values()))['pipe']='UNKNOWN';self.rejected()

    def test_worker_parent_pipe_must_be_closed(self):
        self.workers[0].pipe.input=9991;self.rejected()

    def test_owner_progress_must_be_released(self):
        self.c.owner_release['handle']='UNKNOWN';self.rejected()

    def test_owner_pipe_must_be_closed(self):
        self.c.pipe.output=9992;self.rejected()

    def test_history_progress_must_be_released(self):
        self.base.history();self.base.release();self.configure()
        operation=json.loads(self.workers[0].request)['operationId']
        self.c.worker_release[operation]['pipe']='UNKNOWN';self.rejected()

    def test_exit_proof_must_be_original(self):
        self.workers[0].exit_response+=b' ';self.rejected()

    def test_no_pid_group_reobservation_or_native_close(self):
        for process in [self.c.process,*[w.process for w in self.workers]]:
            for name in ('poll','members','close'):
                self.enterContext(patch.object(process,name,side_effect=AssertionError('終了済み資源の再作用')))
        result=self.record();self.assertEqual('CLOSED',result['writerStatus'])

    def serialization(self,intent=False,change=None):
        original=json.dumps
        def changed(value,*args,**kwargs):
            result=original(value,*args,**kwargs)
            if isinstance(value,dict) and 'writerClosed' in value and ('responseSha256' not in value['writerClosed'])==intent:
                if change:change()
                else:self.f.clock=10
            return result
        with patch.object(offline_control.json,'dumps',side_effect=changed):
            with self.assertRaises(RegistryError):self.record()
        if intent:self.assertNotIn('writerClosed',self.f.saved())
        else:
            self.assertNotIn('state',self.f.saved()['writerClosed'])
            self.assertNotIn('responseSha256',self.f.saved()['writerClosed']);self.no_retry()

    def test_intent_json_deadline_rolls_back(self):self.serialization(True)
    def test_final_json_deadline_does_not_commit_closed(self):self.serialization()
    def test_final_json_checks_released_progress(self):
        self.serialization(change=lambda:next(iter(self.c.worker_release.values())).update(pipe='UNKNOWN'))
    def test_final_json_checks_parent_pipe_closed(self):
        self.serialization(change=lambda:setattr(self.workers[0].pipe,'output',9993))
    def test_final_json_checks_result_raw(self):
        self.serialization(change=lambda:setattr(self.c,'writer_closed_response',b'{}'))
    def test_final_json_checks_result_record(self):
        self.serialization(change=lambda:self.c.record['writerClosed'].update(writerStatus='UNKNOWN'))
    def test_final_json_checks_exit_binding(self):
        self.serialization(change=lambda:self.c.record['creation'].update(ownerGroup='changed'))

    def test_local_journal_deadline_keeps_intent(self):
        original=self.c._record
        def late():original();self.f.clock=10
        with patch.object(self.c,'_record',side_effect=late):
            with self.assertRaises(RegistryError):self.record()
        self.assertNotIn('state',self.f.saved()['writerClosed']);self.pending();self.no_retry()

    def test_local_journal_failure_keeps_intent(self):
        with patch.object(self.c,'_record',side_effect=OSError('fixture 記録失敗')):
            with self.assertRaises(OSError):self.record()
        self.assertNotIn('state',self.f.saved()['writerClosed']);self.pending();self.no_retry()

    def test_commit_deadline_keeps_known_closed_without_return(self):
        original=self.runtime._transaction
        @contextmanager
        def late(instance,**kwargs):
            with original(instance,**kwargs) as connection:yield connection
            if not kwargs.get('read_only') and 'state' in self.f.saved().get('writerClosed',{}):self.f.clock=10
        with patch.object(self.runtime,'_transaction',side_effect=late):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual('CLOSED',self.f.saved()['writerClosed']['state']);self.pending();self.no_retry()

    def test_return_copy_deadline_keeps_known_closed_without_return(self):
        original=offline_control.deepcopy
        def late(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_WRITER_CLOSED' \
                    and 'state' in self.f.saved().get('writerClosed',{}):self.f.clock=10
            return result
        with patch.object(offline_control,'deepcopy',side_effect=late):
            with self.assertRaises(RegistryError):self.record()
        self.assertEqual('CLOSED',self.f.saved()['writerClosed']['state']);self.no_retry()

    def test_closed_intent_blocks_duplicate_prepare_and_start_in_real_registry(self):
        entered,leave=threading.Event(),threading.Event()
        original=self.c._record
        def paused():
            entered.set();self.assertTrue(leave.wait(30));original()
        with patch.object(self.c,'_record',side_effect=paused),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.record)
            try:
                self.assertTrue(entered.wait(30))
                with self.assertRaises(RegistryError):self.record()
                with self.assertRaises(RegistryError):self.f.prepare_next()
                with self.assertRaises(RegistryError):
                    self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='racing-start-closed')
            finally:leave.set()
            self.assertEqual('CLOSED',future.result(30)['writerStatus'])
        self.pending();self.no_retry()

    def test_registry_change_during_journal_prevents_closed_commit(self):
        entered,leave=threading.Event(),threading.Event()
        original=self.c._record
        def paused():
            entered.set();self.assertTrue(leave.wait(30));original()
        with patch.object(self.c,'_record',side_effect=paused),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.record)
            try:
                self.assertTrue(entered.wait(30))
                saved=self.f.saved();saved['workerRelease']['responseSha256']='1'*64;self.f.save(saved)
            finally:leave.set()
            with self.assertRaises(RegistryError):future.result(30)
        self.assertNotIn('state',self.f.saved()['writerClosed']);self.no_retry()


if __name__=='__main__':unittest.main()
