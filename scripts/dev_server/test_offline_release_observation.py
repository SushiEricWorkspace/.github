"""最初Wのhandle不明を、元資源とPENDINGを変えず実Registryへ照合記録する。"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import copy
import json
import threading
import unittest
from unittest.mock import patch

from . import offline_control
from . import test_offline_worker_release as worker_tests
from .build_inputs import digest
from .registry import RegistryError


class ReleaseObservationTests(unittest.TestCase):
    def setUp(self):
        self.base=worker_tests.OfflineWorkerReleaseTests()
        self.addCleanup(lambda:self.assertTrue(self.base.doCleanups()))
        self.base.setUp()
        self.configure()

    def configure(self):
        self.runtime,self.c,self.f,self.token=self.base.runtime,self.base.c,self.base.f,self.base.token
        self.offline=self.runtime._offline
        self.opened=json.loads(self.c.request)['operationId']
        self.key=self.token['instanceId'],self.opened
        self.workers=self.base.workers

    def failed(self,after=False):
        self.base.failure=(0,'handle');self.base.after=after
        with self.assertRaises(OSError):self.base.release()
        self.intent=self.f.saved()
        self.assertEqual({'handle':'UNKNOWN','pipe':'NOT_STARTED'},
            self.c.worker_release[json.loads(self.workers[0].request)['operationId']])

    def observe(self,**changes):
        args=dict(operation_id='observe-release-01',open_operation_id=self.opened,
            release_operation_id=self.f.close_operation,deadline=20)
        args.update(changes)
        return self.runtime._offline_observe_worker_release_failure(self.token,**args)

    def count(self):
        with self.runtime.registry.transaction(read_only=True) as db:
            return db.execute("SELECT COUNT(*) FROM runtime_operations WHERE command='offline-observe-worker-release'").fetchone()[0]

    def retained(self):
        self.base.pending()
        self.assertEqual(self.intent,self.f.saved())
        with self.runtime.registry.transaction(read_only=True) as db:
            self.assertEqual('PENDING',db.execute('SELECT status FROM runtime_operations WHERE id=?',(self.opened,)).fetchone()[0])
            self.assertEqual(1,db.execute('SELECT pending FROM instances WHERE id=?',(self.token['instanceId'],)).fetchone()[0])
            self.assertEqual(1,db.execute('SELECT COUNT(*) FROM reservations WHERE operation=?',(self.opened,)).fetchone()[0])

    def reject(self):
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(0,self.count())
        self.retained()

    @contextmanager
    def trace(self,hook):
        original=self.runtime._transaction
        @contextmanager
        def traced(instance,**kwargs):
            with original(instance,**kwargs) as db:
                db.set_trace_callback(hook)
                yield db
        with patch.object(self.runtime,'_transaction',side_effect=traced):yield

    def test_first_handle_unknown_records_observation_without_releasing_or_terminating(self):
        self.failed(after=True)
        before=list(self.base.order)
        for actor in (self.c,*self.workers):
            for name in ('poll','members','close'):
                self.enterContext(patch.object(actor.process,name,side_effect=AssertionError('旧processへの再作用')))
            self.enterContext(patch.object(actor.pipe,'close_after_exit',side_effect=AssertionError('pipe再解放')))
        self.enterContext(patch.object(self.c,'_record',side_effect=AssertionError('局所記録追加')))
        result=self.observe()
        self.assertEqual('OFFLINE_RELEASE_UNKNOWN',result['status'])
        self.assertIs(False,result['terminatesOriginal'])
        self.assertEqual({'handle':'RELEASED','pipe':'RELEASED'},result['source']['owner'])
        self.assertEqual(digest(self.c.owner_release_response),result['source']['ownerReleaseSha256'])
        self.assertEqual('UNKNOWN',result['source']['workers'][0]['handle'])
        self.assertEqual(before,self.base.order)
        self.assertEqual(1,self.count());self.retained()
        self.assertEqual(result,self.observe())
        self.base.no_retry()

    def test_failure_before_close_effect_is_still_unknown(self):
        self.failed()
        self.assertEqual([],self.base.order)
        self.assertEqual('UNKNOWN',self.observe()['source']['workers'][0]['handle'])
        self.retained()

    def test_original_deadline_is_not_extended_by_explicit_observation(self):
        self.failed();original=self.c.close_binding['deadline'];self.f.clock=11
        self.observe();self.assertEqual(original,self.c.close_binding['deadline']);self.retained()

    def test_successful_release_is_not_a_failed_attempt(self):
        self.base.release()
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(0,self.count())

    def test_pipe_unknown_is_not_adopted_as_first_handle_failure(self):
        self.base.failure=(0,'output')
        with self.assertRaises(OSError):self.base.release()
        self.intent=self.f.saved();self.reject()

    def test_other_worker_failure_keeps_unresolved_without_adoption(self):
        self.base.history();self.configure()
        self.base.failure=(1,'handle')
        with self.assertRaises(OSError):self.base.release()
        self.intent=self.f.saved();self.reject()

    def test_equal_historical_actor_cannot_replace_original(self):
        self.base.history();self.configure();self.failed()
        history=self.offline.history[self.token['instanceId']]
        actor,record=history[0];history[0]=(copy(actor),record)
        self.reject()

    def test_parent_restart_cannot_reconstruct_failure(self):
        self.failed();self.runtime._offline=offline_control._OfflineOpen(self.runtime)
        with self.assertRaises(RegistryError) as caught:self.observe()
        self.assertEqual('RECOVERY_REQUIRED',caught.exception.code)
        self.assertEqual(0,self.count());self.retained()

    def test_no_failed_call_cannot_be_forged_by_progress(self):
        original=self.c.verify_owner
        def changed():
            if self.key in self.offline.release_failures:
                self.c.worker_release[json.loads(self.workers[0].request)['operationId']]['handle']='UNKNOWN'
                raise OSError('close前の照合失敗')
            original()
        with patch.object(self.c,'verify_owner',side_effect=changed):
            with self.assertRaises(OSError):self.base.release()
            self.intent=self.f.saved();self.reject()
        self.assertEqual([],self.base.order)

    def test_failure_packet_reference_cannot_be_replaced(self):
        self.failed();packet=self.offline.release_failures[self.key]
        self.offline.release_failures[self.key]=tuple(list(packet));self.reject()

    def test_other_open_alias_cannot_change_request_binding(self):
        self.failed();packet=self.offline.release_failures[self.key]
        self.offline.release_failures[self.token['instanceId'],'other-open-alias']=packet
        with self.assertRaises(RegistryError):self.observe(open_operation_id='other-open-alias')
        self.assertEqual(0,self.count());self.retained()
        self.assertIs(packet,self.offline.release_failures[self.key])

    def test_whole_token_binding_precedes_parent_observation(self):
        self.failed()
        changes=({'epoch':self.token['epoch']+1},{'epoch':float(self.token['epoch'])},
            {'leaseId':'other-lease'},{'expectedGeneration':'other-generation'})
        with patch.object(self.c,'_verify_close_binding') as native:
            for change in changes:
                token=dict(self.token,**change)
                with self.assertRaises(RegistryError):
                    self.runtime._offline_observe_worker_release_failure(token,operation_id='observe-release-01',
                        open_operation_id=self.opened,release_operation_id=self.f.close_operation,deadline=20)
                native.assert_not_called()
        self.assertEqual(0,self.count());self.retained()

    def test_handle_value_change_is_preserved_and_refused(self):
        self.failed();self.workers[0].process.handle=9993;self.reject()
        self.assertEqual(9993,self.workers[0].process.handle)

    def test_close_callback_resource_change_is_not_first_adopted(self):
        self.base.effect=lambda index,role:setattr(self.workers[index].process,'handle',9993)
        self.failed(after=True);self.reject()
        self.assertEqual(9993,self.workers[0].process.handle)

    def test_child_fd_change_is_preserved_and_refused(self):
        self.failed();self.workers[0].pipe.child_input=9993;self.reject()
        self.assertEqual(9993,self.workers[0].pipe.child_input)

    def test_native_reference_change_is_preserved_and_refused(self):
        self.failed();self.workers[0].native=copy(self.workers[0].native);self.reject()

    def test_close_binding_process_cannot_be_replaced(self):
        self.failed();self.c.close_binding['process']=copy(self.c.process);self.reject()

    def test_changed_original_intent_cannot_be_adopted(self):
        self.failed()
        with self.runtime.registry.transaction() as db:
            db.execute("UPDATE runtime_operations SET response=response||' ' WHERE id=?",(self.opened,))
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(0,self.count())

    def commit(self,renew):
        errors=[]
        def run():
            try:
                if renew:self.runtime.registry.execute('renew',dict(self.token,ttlSeconds=1000),operation_id='renew-release-observation')
                else:
                    with self.runtime.registry.transaction() as db:
                        db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?',(self.token['instanceId'],))
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=run);thread.start();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual([],errors)

    def race(self,renew=False):
        self.failed();original=self.c._verify_close_binding
        def observed(deadline):original(deadline);self.commit(renew)
        with patch.object(self.c,'_verify_close_binding',side_effect=observed):
            if renew:
                try:self.observe()
                except RegistryError as error:self.fail('正常renewを拒否しました: '+error.code)
            else:self.reject()
        self.retained()

    def test_epoch_commit_after_parent_observation_refuses_record(self):self.race()
    def test_normal_public_renew_keeps_original_basis(self):self.race(True)

    def test_write_inner_change_rolls_back_observation(self):
        self.failed()
        def change(sql):
            if sql.startswith('INSERT INTO runtime_operations'):
                self.c.worker_release[json.loads(self.workers[0].request)['operationId']]['pipe']='UNKNOWN'
        with self.trace(change):self.reject()

    def test_final_copy_change_refuses_return_after_observation_commit(self):
        self.failed();original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_RELEASE_UNKNOWN':
                result['source']['workers'][0]['handle']='RELEASED'
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):
            with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(1,self.count());self.retained()

    def test_resource_entry_deadline_starts_no_observation_or_sql(self):
        self.failed();original=self.runtime.registry.resource_use
        @contextmanager
        def late(*args,**kwargs):
            with original(*args,**kwargs):self.f.clock=20;yield
        with patch.object(self.runtime.registry,'resource_use',side_effect=late), \
             patch.object(self.c,'_verify_close_binding') as native:
            self.reject();native.assert_not_called()

    def test_resource_exit_deadline_refuses_return_but_preserves_observation(self):
        self.failed();original=self.runtime.registry.resource_use
        @contextmanager
        def late(*args,**kwargs):
            with original(*args,**kwargs):yield
            self.f.clock=20
        with patch.object(self.runtime.registry,'resource_use',side_effect=late):
            with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(1,self.count());self.retained()

    def test_capture_deadline_starts_no_inherited_io_or_close(self):
        original=self.offline._release_snapshot
        def late(*args):result=original(*args);self.f.clock=self.f.deadline;return result
        with patch.object(self.offline,'_release_snapshot',side_effect=late),patch.object(self.c,'verify_owner') as owner:
            with self.assertRaises(RegistryError):self.base.release()
            owner.assert_not_called()
        self.assertEqual([],self.base.order)

    def test_reserved_read_deadline_starts_no_next_row_observation(self):
        self.failed();original=self.offline._reserved;instance=self.runtime.registry._instance
        expired=False;after=[]
        def late(*args):
            nonlocal expired
            result=original(*args);self.f.clock=20;expired=True
            return result
        def observed(*args):
            if expired:after.append(True)
            return instance(*args)
        with patch.object(self.offline,'_reserved',side_effect=late), \
             patch.object(self.runtime.registry,'_instance',side_effect=observed):
            with self.assertRaises(RegistryError):self.observe()
        self.assertEqual([],after);self.assertEqual(0,self.count());self.retained()

    def test_parent_observation_deadline_starts_no_transaction(self):
        self.failed();original=self.c._verify_close_binding
        def late(deadline):original(deadline);self.f.clock=20
        with patch.object(self.c,'_verify_close_binding',side_effect=late),patch.object(self.runtime,'_transaction') as sql:
            with self.assertRaises(RegistryError):self.observe()
            sql.assert_not_called()
        self.assertEqual(0,self.count());self.retained()

    def test_same_observation_concurrently_commits_one_record(self):
        self.failed();original=self.c._verify_close_binding;barrier=threading.Barrier(2)
        def observed(deadline):original(deadline);barrier.wait(5)
        with patch.object(self.c,'_verify_close_binding',side_effect=observed):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first=executor.submit(self.observe);second=executor.submit(self.observe)
                self.assertEqual(first.result(10),second.result(10))
        self.assertEqual(1,self.count());self.retained()

    def test_last_read_exit_resource_change_refuses_return(self):
        self.failed();original=self.runtime._transaction
        @contextmanager
        def changed(instance,**kwargs):
            with original(instance,**kwargs) as db:yield db
            if kwargs.get('read_only'):self.workers[0].pipe.child_input=9993
        with patch.object(self.runtime,'_transaction',side_effect=changed):
            with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(1,self.count());self.retained()

    def test_no_native_or_file_observation_after_last_read(self):
        self.failed();original=self.runtime._transaction;native=self.c._verify_close_binding
        finished=False;after=[]
        @contextmanager
        def observed(instance,**kwargs):
            nonlocal finished
            with original(instance,**kwargs) as db:yield db
            if kwargs.get('read_only'):finished=True
        def called(deadline):
            if finished:after.append(True)
            native(deadline)
        with patch.object(self.runtime,'_transaction',side_effect=observed), \
             patch.object(self.c,'_verify_close_binding',side_effect=called), \
             patch.object(self.c,'_record',side_effect=AssertionError('追加のfile I/O')):
            self.observe()
        self.assertTrue(finished);self.assertEqual([],after);self.retained()

    def test_reservation_binding_must_match(self):
        self.failed()
        with self.runtime.registry.transaction() as db:
            db.execute('UPDATE reservations SET epoch=epoch+1 WHERE operation=?',(self.opened,))
        self.reject()

    def test_parent_identity_is_required(self):
        self.failed()
        with patch.object(self.c.adapter,'identity',return_value=None):self.reject()

    def test_cached_observation_response_cannot_be_replaced(self):
        self.failed();self.observe()
        with self.runtime.registry.transaction() as db:
            db.execute('UPDATE runtime_operations SET response=? WHERE id=?',('{}','observe-release-01'))
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(1,self.count());self.retained()

    def test_observation_starts_no_original_update_or_release_dml(self):
        self.failed();updates=[]
        with self.trace(lambda sql:updates.append(sql) if sql.startswith(('UPDATE','DELETE')) else None):self.observe()
        self.assertEqual([],updates);self.retained()
