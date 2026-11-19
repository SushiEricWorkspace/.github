"""確定済みcloseの応答喪失だけを、元packetと実Registryから照合する。"""
from contextlib import contextmanager
from copy import deepcopy
import threading
import unittest
from unittest.mock import patch

from . import offline_control, offline_process
from .build_inputs import digest
from .registry import RegistryError
from . import test_offline_finish_close as finished


class OfflineRecoverCloseTests(unittest.TestCase):
    def setUp(self):
        self.base = finished.OfflineFinishCloseTests()
        self.addCleanup(lambda:self.assertTrue(self.base.doCleanups()))
        self.base.setUp()
        self.runtime, self.offline, self.f, self.c = self.base.runtime, self.base.offline, self.base.f, self.base.c
        self.token = self.base.token

    def finish(self):
        result = self.base.finish()
        self.expected = deepcopy(result)
        return result

    def recover(self, **changes):
        args = dict(operation_id='recover-close-01', open_operation_id=self.base.opened['operationId'],
            close_operation_id=self.f.close_operation, termination_sha256=digest(self.c.termination_response), deadline=20)
        args.update(changes)
        return self.runtime._offline_recover_close(self.token, **args)

    def count(self):
        with self.runtime.registry.transaction(read_only=True) as db:
            return db.execute("SELECT count(*) FROM runtime_operations WHERE command='offline-recover-close'").fetchone()[0]

    def retained(self):
        self.assertEqual((('LEASED_STOPPED',0),'COMPLETED',0), self.base.state())
        for actor, process, pipe, resources in self.base.resources:
            self.assertIs(actor.process, process)
            self.assertIs(actor.pipe, pipe)
            self.assertIs(actor.creation_resources, resources)
        self.assertEqual(self.base.snapshot, self.offline._release_snapshot(self.c, self.base.workers, self.base.history))

    def reject(self, **changes):
        with self.assertRaises(RegistryError):self.recover(**changes)

    def test_completed_close_readback_preserves_originals_and_does_not_reclose(self):
        self.finish()
        for actor, process, pipe, _ in self.base.resources:
            for name in ('poll', 'members', 'close'):
                self.enterContext(patch.object(process, name, side_effect=AssertionError('解放済み資源の再作用')))
            self.enterContext(patch.object(pipe, 'close_after_exit', side_effect=AssertionError('pipe再解放')))
        result = self.recover()
        self.assertEqual('OFFLINE_CLOSE_RECOVERED', result['status'])
        self.assertEqual(self.expected, result['original'])
        self.assertEqual(result, self.recover())
        self.assertEqual(1, self.count())
        result['original']['status'] = 'UNKNOWN'
        self.assertEqual(self.expected, self.recover()['original'])
        self.retained()

    def test_commit_then_original_timeout_uses_separate_recovery_deadline(self):
        original = self.runtime._transaction
        @contextmanager
        def expired(instance, **kwargs):
            with original(instance, **kwargs) as db:yield db
            with self.runtime.registry.transaction(read_only=True) as db:
                state = db.execute('SELECT status FROM runtime_operations WHERE id=?',
                                   (self.base.opened['operationId'],)).fetchone()[0]
            if state == 'COMPLETED':self.f.clock = self.f.deadline
        with patch.object(self.runtime, '_transaction', side_effect=expired):
            with self.assertRaises(RegistryError):self.base.finish()
        old_deadline = self.c.close_binding['deadline']
        result = self.recover()
        self.assertEqual('OFFLINE_CLOSE_RECOVERED', result['status'])
        self.assertEqual(old_deadline, self.c.close_binding['deadline'])
        self.retained()

    def test_uncommitted_termination_is_not_recovered(self):
        with patch.object(self.c, '_record', side_effect=OSError('fixture 書込失敗')):
            with self.assertRaises(OSError):self.base.finish()
        self.reject()
        self.assertEqual(0,self.count())
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())

    def test_packet_freeze_deadline_starts_no_terminal_sql(self):
        original=offline_control.deepcopy
        transaction=self.runtime._transaction
        updates=[]
        def late(value):
            result=original(value)
            if isinstance(value,tuple) and len(value)==7 and value[0]==(self.token['instanceId'],self.base.opened['operationId']):
                self.f.clock=self.f.deadline
            return result
        @contextmanager
        def traced(instance, **kwargs):
            with transaction(instance,**kwargs) as db:
                db.set_trace_callback(lambda sql:updates.append(sql)
                    if sql.startswith("UPDATE runtime_operations SET status='COMPLETED'") else None)
                yield db
        with patch.object(offline_control,'deepcopy',side_effect=late),patch.object(self.runtime,'_transaction',side_effect=traced):
            with self.assertRaises(RegistryError):self.base.finish()
        self.assertEqual([],updates)
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())
        self.reject()
        self.assertEqual(0,self.count())

    def test_new_parent_cannot_reconstruct_packet_from_registry(self):
        self.finish()
        self.runtime._offline = offline_control._OfflineOpen(self.runtime)
        self.reject()
        self.assertEqual(0,self.count())
        self.retained()

    def test_expected_termination_hash_and_close_id_are_required(self):
        self.finish()
        self.reject(termination_sha256='0'*64)
        self.reject(close_operation_id='another-close')
        self.reject(operation_id=self.f.close_operation)
        self.assertEqual(0,self.count())
        self.retained()

    def test_pending_release_cannot_be_adopted(self):
        self.finish()
        self.c.owner_release['handle']='UNKNOWN'
        self.reject()
        self.assertEqual('UNKNOWN',self.c.owner_release['handle'])
        self.assertEqual(0,self.count())

    def test_matching_mutable_slot_cannot_rebase_completed_binding(self):
        self.finish()
        slot = self.offline.retired[(self.token['instanceId'],self.base.opened['operationId'])]
        self.c.owner_release['handle'] = 'UNKNOWN'
        slot['snapshot'] = self.offline._release_snapshot(self.c,self.base.workers,self.base.history)
        self.reject()
        self.assertEqual(0,self.count())

    def test_equal_replacement_resource_is_not_adopted(self):
        self.finish()
        slot = self.offline.retired[(self.token['instanceId'],self.base.opened['operationId'])]
        slot['resources'] = list(slot['resources'])
        self.reject()
        self.assertEqual(0,self.count())

    def test_changed_handle_value_is_not_rebased(self):
        self.finish()
        self.c.process.handle=9993
        self.reject()
        self.assertEqual(9993,self.c.process.handle)
        self.assertEqual(0,self.count())

    def test_finish_close_final_exit_uses_same_frozen_packet(self):
        original=self.runtime._transaction
        @contextmanager
        def changed(instance, **kwargs):
            with original(instance,**kwargs) as db:yield db
            if kwargs.get('read_only'):
                with self.runtime.registry.transaction(read_only=True) as db:
                    completed=db.execute('SELECT status FROM runtime_operations WHERE id=?',
                        (self.base.opened['operationId'],)).fetchone()[0]=='COMPLETED'
                if completed:self.c.process.handle=9993
        with patch.object(self.runtime,'_transaction',side_effect=changed):
            with self.assertRaises(RegistryError):self.base.finish()
        self.assertEqual((('LEASED_STOPPED',0),'COMPLETED',0),self.base.state())
        self.reject()
        self.assertEqual(0,self.count())

    def test_changed_private_pipe_fd_is_not_rebased(self):
        self.finish()
        self.c.pipe.child_input=9993
        self.reject()
        self.assertEqual(9993,self.c.pipe.child_input)
        self.assertEqual(0,self.count())

    def test_changed_retained_pipe_buffer_is_not_rebased(self):
        self.finish()
        buffer=getattr(self.c.pipe,'incoming',None)
        if isinstance(buffer,bytearray):buffer.extend(b'changed')
        else:self.c.pipe.incoming=buffer=bytearray(b'changed')
        self.reject()
        self.assertIs(buffer,self.c.pipe.incoming)
        self.assertEqual(0,self.count())

    def race(self, renew=False):
        self.finish()
        original = self.offline._terminal_resources
        failures=[]
        def committed(*args):
            original(*args)
            def change():
                try:
                    if renew:
                        self.runtime.registry.execute('renew',dict(self.token,ttlSeconds=1000),operation_id='renew-close-recovery')
                    else:
                        with self.runtime.registry.transaction() as db:
                            db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?',(self.token['instanceId'],))
                except BaseException as error:failures.append(error)
            thread=threading.Thread(target=change);thread.start();thread.join(5)
            self.assertFalse(thread.is_alive());self.assertEqual([],failures)
        with patch.object(self.offline,'_terminal_resources',side_effect=committed):
            if renew:self.assertEqual('OFFLINE_CLOSE_RECOVERED',self.recover()['status'])
            else:self.reject()
        self.assertEqual(int(renew),self.count())
        self.retained()

    def test_epoch_commit_after_native_observation_refuses_write(self):self.race()
    def test_normal_renew_retains_fixed_binding(self):self.race(True)

    def test_write_transaction_checks_original_memory(self):
        self.finish()
        original = self.runtime._transaction
        @contextmanager
        def changed(instance, **kwargs):
            with original(instance, **kwargs) as db:
                if not kwargs.get('read_only'):
                    def inserted(sql):
                        if sql.startswith('INSERT INTO runtime_operations'):
                            self.c.owner_release['pipe']='UNKNOWN'
                    db.set_trace_callback(inserted)
                yield db
        with patch.object(self.runtime,'_transaction',side_effect=changed):self.reject()
        self.assertEqual(0,self.count())
        self.assertEqual('UNKNOWN',self.c.owner_release['pipe'])

    def test_write_entry_deadline_starts_no_registry_read(self):
        self.finish()
        original=self.runtime._transaction
        reads=[]
        @contextmanager
        def late(instance, **kwargs):
            with original(instance,**kwargs) as db:
                self.f.clock=20
                db.set_trace_callback(lambda sql:reads.append(sql) if sql.startswith('SELECT') else None)
                yield db
        with patch.object(self.runtime,'_transaction',side_effect=late):self.reject()
        self.assertEqual([],reads)
        self.assertEqual(0,self.count())
        self.retained()

    def test_committed_recovery_timeout_preserves_completed_records(self):
        self.finish()
        original=self.runtime._transaction
        @contextmanager
        def late(instance, **kwargs):
            with original(instance, **kwargs) as db:yield db
            if not kwargs.get('read_only'):self.f.clock=20
        with patch.object(self.runtime,'_transaction',side_effect=late):self.reject()
        self.assertEqual(1,self.count())
        self.retained()

    def test_resource_exit_timeout_refuses_return_after_commit(self):
        self.finish()
        original=self.runtime.registry.resource_use
        @contextmanager
        def late(*args, **kwargs):
            with original(*args,**kwargs):yield
            self.f.clock=20
        with patch.object(self.runtime.registry,'resource_use',side_effect=late):self.reject()
        self.assertEqual(1,self.count())
        self.retained()

    def test_expired_recovery_starts_no_native_or_sql(self):
        self.finish();self.f.clock=20
        with patch.object(self.offline,'_terminal_resources') as native:
            self.reject()
            native.assert_not_called()
        self.assertEqual(0,self.count())
        self.retained()

    def test_parent_identity_is_observed_without_resolving_closed_children(self):
        self.finish()
        with patch.object(self.c.adapter,'identity',return_value=None):self.reject()
        self.assertEqual(0,self.count())
        self.retained()

    def test_identity_deadline_starts_no_following_host_observation(self):
        self.finish()
        original=self.c.adapter.identity
        def late(pid):
            value=original(pid);self.f.clock=20;return value
        with patch.object(self.c.adapter,'identity',side_effect=late), \
             patch.object(offline_process,'process_identity',wraps=offline_process.process_identity) as host:
            self.reject()
            host.assert_not_called()
        self.assertEqual(0,self.count())
        self.retained()

    def test_changed_return_copy_is_not_adopted(self):
        self.finish()
        original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSE_RECOVERED':result['status']='UNKNOWN'
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):self.reject()
        self.assertEqual(1,self.count())
        self.retained()

    def test_result_copy_mutation_refuses_return_without_rebasing(self):
        self.finish()
        original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_CLOSE_RECOVERED':
                self.c.record['termination']['status']='UNKNOWN'
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):self.reject()
        self.assertEqual(1,self.count())

    def test_existing_start_is_not_authorized_by_recovery(self):
        self.finish();self.recover()
        self.base.start_blocked()
