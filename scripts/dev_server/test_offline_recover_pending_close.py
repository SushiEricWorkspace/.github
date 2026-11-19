"""元終端writeが未commitでも、同じPの固定意図と全解放証拠から終端できる。"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

from . import offline_control
from . import test_offline_finish_close as finished
from .build_inputs import digest
from .registry import RegistryError


class OfflineRecoverPendingCloseTests(unittest.TestCase):
    def setUp(self):
        self.base = finished.OfflineFinishCloseTests()
        self.addCleanup(lambda:self.assertTrue(self.base.doCleanups()))
        self.base.setUp()
        self.runtime, self.offline, self.f, self.c = self.base.runtime, self.base.offline, self.base.f, self.base.c
        self.token = self.base.token

    def pending(self, detached=False):
        if detached:
            with patch.object(self.offline, '_terminal', side_effect=OSError('終端write rollback')):
                with self.assertRaises(OSError):self.base.finish()
        else:
            original = self.c._record
            def late(**options):
                original(**options)
                self.f.clock = self.f.deadline
            with patch.object(self.c, '_record', side_effect=late):
                with self.assertRaises(RegistryError):self.base.finish()
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1), self.base.state())
        self.assertIn('termination', self.f.saved())
        self.assertNotIn('state', self.f.saved()['termination'])
        self.intent = json.dumps(self.f.saved())
        self.packet = self.offline.close_recoveries[(self.token['instanceId'],self.base.opened['operationId'])]
        self.assertEqual(self.intent, self.packet[2]())

    def recover(self, **changes):
        args = dict(operation_id='recover-pending-close-01', open_operation_id=self.base.opened['operationId'],
            close_operation_id=self.f.close_operation, termination_sha256=digest(self.c.termination_response), deadline=20)
        args.update(changes)
        return self.runtime._offline_recover_pending_close(self.token, **args)

    def count(self):
        with self.runtime.registry.transaction(read_only=True) as db:
            return db.execute("SELECT count(*) FROM runtime_operations WHERE command='offline-recover-pending-close'").fetchone()[0]

    def retained(self):
        for actor, process, pipe, resources in self.base.resources:
            self.assertIs(actor.process, process)
            self.assertIs(actor.pipe, pipe)
            self.assertIs(actor.creation_resources, resources)
        self.assertIs(self.packet, self.offline.close_recoveries[(self.token['instanceId'],self.base.opened['operationId'])])
        self.assertEqual(self.base.snapshot, self.offline._release_snapshot(self.c,self.base.workers,self.base.history))

    def reject(self, **changes):
        with self.assertRaises(RegistryError):self.recover(**changes)

    def uncommitted(self):
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())
        self.assertEqual(0,self.count())

    def test_original_intent_completes_atomically_without_reclose(self):
        self.pending()
        for actor, process, pipe, _ in self.base.resources:
            for name in ('poll','members','close'):
                self.enterContext(patch.object(process,name,side_effect=AssertionError('解放済み資源の再作用')))
            self.enterContext(patch.object(pipe,'close_after_exit',side_effect=AssertionError('pipe再解放')))
        result = self.recover()
        self.assertEqual('OFFLINE_CLOSE_TERMINATED',result['status'])
        self.assertEqual(json.loads(self.packet[0]),result['original'])
        self.base.completed();self.retained()
        self.assertEqual(result,self.recover())
        self.assertEqual(1,self.count())
        result['original']['status']='UNKNOWN'
        self.assertEqual('OFFLINE_SESSION_COMPLETED',self.recover()['original']['status'])
        self.base.start_blocked()

    def test_rolled_back_terminal_with_detached_maps_can_complete(self):
        self.pending(detached=True)
        self.assertNotIn(self.token['instanceId'],self.offline.controllers)
        self.recover();self.base.completed();self.retained()

    def test_all_worker_history_is_retained(self):
        self.base.base.base.history();self.base.base.base.release();self.base.base.configure()
        self.base.base.record();self.base.configure()
        self.runtime, self.offline, self.f, self.c = self.base.runtime, self.base.offline, self.base.f, self.base.c
        self.token = self.base.token
        self.assertEqual(2,len(self.base.workers))
        self.pending();self.recover();self.base.completed();self.retained()

    def test_without_worker_can_complete(self):
        self.base.base.test_open_without_worker_records_closed();self.base.configure()
        self.runtime, self.offline, self.f, self.c = self.base.runtime, self.base.offline, self.base.f, self.base.c
        self.token = self.base.token
        self.assertEqual([],self.base.workers)
        self.pending();self.recover();self.base.completed();self.retained()

    def test_known_completed_readback_does_not_release_pending(self):
        self.pending()
        args=dict(operation_id='known-readback',open_operation_id=self.base.opened['operationId'],
            close_operation_id=self.f.close_operation,termination_sha256=digest(self.c.termination_response),deadline=20)
        with self.assertRaises(RegistryError):self.runtime._offline_recover_close(self.token,**args)
        self.uncommitted()
        self.recover()
        self.assertEqual('OFFLINE_CLOSE_RECOVERED',self.runtime._offline_recover_close(self.token,**args)['status'])

    def test_already_completed_original_requires_known_readback(self):
        self.base.finish();self.reject()
        self.assertEqual(0,self.count());self.base.completed()

    def test_missing_packet_cannot_be_reconstructed(self):
        with patch.object(self.c,'_record',side_effect=OSError('元局所記録の失敗')):
            with self.assertRaises(OSError):self.base.finish()
        self.reject();self.uncommitted()

    def test_parent_restart_cannot_adopt_originals(self):
        self.pending()
        self.runtime._offline=offline_control._OfflineOpen(self.runtime)
        self.reject();self.uncommitted();self.retained()

    def test_parent_identity_is_required(self):
        self.pending()
        with patch.object(self.c.adapter,'identity',return_value=None):self.reject()
        self.uncommitted();self.retained()

    def test_close_hash_and_id_are_required(self):
        self.pending()
        self.reject(termination_sha256='0'*64)
        self.reject(close_operation_id='different-close')
        self.reject(operation_id=self.f.close_operation)
        self.uncommitted();self.retained()

    def test_published_intent_cannot_be_replaced_by_equivalent_json(self):
        self.pending()
        with self.runtime.registry.transaction() as db:
            db.execute('UPDATE runtime_operations SET response=? WHERE id=?',
                (self.intent+' ',self.base.opened['operationId']))
        self.reject();self.uncommitted();self.retained()

    def test_reservation_binding_must_match(self):
        self.pending()
        with self.runtime.registry.transaction() as db:
            db.execute('UPDATE reservations SET epoch=epoch+1 WHERE operation=?',(self.base.opened['operationId'],))
        self.reject();self.uncommitted();self.retained()

    def test_fixed_row_run_must_match(self):
        self.pending()
        with self.runtime.registry.transaction() as db:
            db.execute('UPDATE instances SET run_id=? WHERE id=?',('changed-run',self.token['instanceId']))
        self.reject();self.uncommitted();self.retained()

    def test_other_pending_operation_is_preserved(self):
        self.pending()
        with self.runtime.registry.transaction() as db:
            db.execute('INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)',
                ('another-pending','f','start',self.token['instanceId'],'PENDING','{}'))
        self.reject();self.uncommitted();self.retained()

    def test_operation_namespace_conflict_is_preserved(self):
        self.pending()
        with self.runtime.registry.transaction() as db:
            db.execute('INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)',
                ('recover-pending-close-01','different','offline-recover-pending-close',self.token['instanceId'],'COMPLETED','{}'))
        self.reject()
        self.assertEqual('PENDING',self.base.state()[1]);self.assertEqual(1,self.count());self.retained()

    def test_cached_result_cannot_be_replaced(self):
        self.pending();self.recover()
        with self.runtime.registry.transaction() as db:
            db.execute('UPDATE runtime_operations SET response=? WHERE id=?',('{}','recover-pending-close-01'))
        self.reject();self.assertEqual(1,self.count());self.base.completed();self.retained()

    def test_unknown_release_cannot_be_rebased(self):
        self.pending()
        self.c.owner_release['handle']='UNKNOWN'
        self.packet[1]['snapshot']=self.offline._release_snapshot(self.c,self.base.workers,self.base.history)
        self.reject();self.uncommitted()
        self.assertEqual('UNKNOWN',self.c.owner_release['handle'])

    def test_resource_reference_change_is_rejected(self):
        self.pending();self.c.creation_resources=list(self.c.creation_resources)
        self.reject();self.uncommitted()

    def test_foreign_active_slot_is_preserved(self):
        self.pending()
        other=object();self.offline.controllers[self.token['instanceId']]=other
        self.reject();self.uncommitted();self.retained()
        self.assertIs(other,self.offline.controllers[self.token['instanceId']])

    def test_latest_worker_cannot_be_rebased(self):
        self.pending()
        self.packet[1]['latest']=object()
        self.offline.workers[self.token['instanceId']]=self.packet[1]['latest']
        self.reject();self.uncommitted()

    def test_latest_reference_is_fixed_before_packet_copy(self):
        original=offline_control.deepcopy
        other=object()
        key=self.token['instanceId'],self.base.opened['operationId']
        def changed(value):
            result=original(value)
            if isinstance(value,tuple) and len(value)==7 and value[0]==key:
                self.offline.retired[key]['latest']=other
                self.offline.workers[self.token['instanceId']]=other
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):
            with self.assertRaises(RegistryError):self.base.finish()
        self.packet=self.offline.close_recoveries[key]
        self.reject();self.uncommitted();self.retained()
        self.assertIs(other,self.offline.workers[self.token['instanceId']])

    def race(self, renew=False):
        self.pending()
        original=self.offline._terminal_resources
        failures=[]
        def observed(*args):
            original(*args)
            def change():
                try:
                    if renew:
                        self.runtime.registry.execute('renew',dict(self.token,ttlSeconds=1000),operation_id='renew-pending-close')
                    else:
                        with self.runtime.registry.transaction() as db:
                            db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?',(self.token['instanceId'],))
                except BaseException as error:failures.append(error)
            thread=threading.Thread(target=change);thread.start();thread.join(5)
            self.assertFalse(thread.is_alive());self.assertEqual([],failures)
        with patch.object(self.offline,'_terminal_resources',side_effect=observed):
            if renew:self.recover()
            else:self.reject()
        if renew:self.base.completed()
        else:self.uncommitted()
        self.retained()

    def test_epoch_commit_after_native_refuses_terminal_write(self):self.race()
    def test_public_renew_does_not_extend_recovery_deadline(self):self.race(True)

    def test_concurrent_same_request_commits_one_recovery(self):
        self.pending();original=self.offline._terminal_resources
        barrier=threading.Barrier(2)
        def observed(*args):original(*args);barrier.wait(5)
        with patch.object(self.offline,'_terminal_resources',side_effect=observed):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first=executor.submit(self.recover)
                second=executor.submit(self.recover)
                self.assertEqual(first.result(10),second.result(10))
        self.assertEqual(1,self.count());self.base.completed();self.retained()

    def trace(self, hook):
        transaction=self.runtime._transaction
        @contextmanager
        def traced(instance,**kwargs):
            with transaction(instance,**kwargs) as db:
                db.set_trace_callback(hook)
                yield db
        return patch.object(self.runtime,'_transaction',side_effect=traced)

    def test_write_inner_resource_change_rolls_back_all_records(self):
        self.pending()
        def change(sql):
            if sql.startswith('INSERT INTO runtime_operations'):self.c.owner_release['pipe']='UNKNOWN'
        with self.trace(change):self.reject()
        self.uncommitted();self.assertEqual('UNKNOWN',self.c.owner_release['pipe'])

    def test_write_inner_foreign_slot_is_not_removed(self):
        self.pending();other=object()
        def change(sql):
            if sql.startswith('INSERT INTO runtime_operations'):self.offline.controllers[self.token['instanceId']]=other
        with self.trace(change):self.reject()
        self.uncommitted();self.retained()
        self.assertIs(other,self.offline.controllers[self.token['instanceId']])

    def test_post_write_ledger_mismatch_rolls_back(self):
        self.pending()
        original=self.offline._terminal_state
        def changed(db,*args):
            db.execute('UPDATE run_admission SET revision=revision+1 WHERE run=?',(self.packet[1]['row']['run_id'],))
            original(db,*args)
        with patch.object(self.offline,'_terminal_state',side_effect=changed):self.reject()
        self.uncommitted();self.retained()

    def test_expired_entry_starts_no_native_or_sql(self):
        self.pending();self.f.clock=20
        with patch.object(self.offline,'_terminal_resources') as native:
            self.reject();native.assert_not_called()
        self.uncommitted();self.retained()

    def test_write_entry_deadline_starts_no_sql(self):
        self.pending();transaction=self.runtime._transaction;reads=[]
        @contextmanager
        def late(instance,**kwargs):
            with transaction(instance,**kwargs) as db:
                self.f.clock=20
                db.set_trace_callback(lambda sql:reads.append(sql) if sql.startswith(('SELECT','UPDATE','DELETE','INSERT')) else None)
                yield db
        with patch.object(self.runtime,'_transaction',side_effect=late):self.reject()
        self.assertEqual([],reads);self.uncommitted();self.retained()

    def test_intent_read_deadline_starts_no_terminal_update(self):
        self.pending();updates=[]
        def late(sql):
            if sql.startswith('SELECT response FROM runtime_operations'):self.f.clock=20
            if sql.startswith('UPDATE'):updates.append(sql)
        with self.trace(late):self.reject()
        self.assertEqual([],updates);self.uncommitted();self.retained()

    def test_native_deadline_starts_no_write(self):
        self.pending();original=self.offline._terminal_resources
        def late(*args):original(*args);self.f.clock=20
        with patch.object(self.offline,'_terminal_resources',side_effect=late):self.reject()
        self.uncommitted();self.retained()

    def test_cached_read_deadline_starts_no_terminal_observation(self):
        self.pending();self.recover()
        def late(sql):
            if sql.startswith('SELECT * FROM runtime_operations'):self.f.clock=20
        with self.trace(late),patch.object(self.offline,'_terminal_state') as observe:
            self.reject();observe.assert_not_called()
        self.assertEqual(1,self.count());self.base.completed();self.retained()

    def test_committed_timeout_preserves_terminal_and_recover_readback(self):
        self.pending();transaction=self.runtime._transaction
        @contextmanager
        def late(instance,**kwargs):
            with transaction(instance,**kwargs) as db:yield db
            if not kwargs.get('read_only'):self.f.clock=20
        with patch.object(self.runtime,'_transaction',side_effect=late):self.reject()
        self.assertEqual(1,self.count());self.base.completed();self.retained()
        self.f.clock=19;self.recover();self.assertEqual(1,self.count())

    def test_resource_exit_deadline_refuses_return_after_commit(self):
        self.pending();resource_use=self.runtime.registry.resource_use
        @contextmanager
        def late(*args,**kwargs):
            with resource_use(*args,**kwargs):yield
            self.f.clock=20
        with patch.object(self.runtime.registry,'resource_use',side_effect=late):self.reject()
        self.assertEqual(1,self.count());self.base.completed();self.retained()

    def test_result_copy_change_is_rejected_after_commit(self):
        self.pending();original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSE_TERMINATED':result['status']='UNKNOWN'
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):self.reject()
        self.assertEqual(1,self.count());self.base.completed();self.retained()

    def test_result_copy_deadline_refuses_return_after_commit(self):
        self.pending();original=offline_control.deepcopy
        def late(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSE_TERMINATED':self.f.clock=20
            return result
        with patch.object(offline_control,'deepcopy',side_effect=late):self.reject()
        self.assertEqual(1,self.count());self.base.completed();self.retained()

    def test_final_copy_resource_change_is_not_rebased(self):
        self.pending();original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSE_TERMINATED':self.c.process.handle=9993
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):self.reject()
        self.assertEqual(1,self.count());self.assertEqual(9993,self.c.process.handle)
