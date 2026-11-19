"""局所記録の正常復帰と次の照合I/Oの間で、元close packetを固定する。"""
from contextlib import contextmanager
from copy import copy
import json
import threading
import unittest
from unittest.mock import patch

from . import offline_control
from . import test_offline_finish_close as finished
from .build_inputs import canonical, digest
from .registry import RegistryError


class CloseCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.base=finished.OfflineFinishCloseTests()
        self.addCleanup(lambda:self.assertTrue(self.base.doCleanups()))
        self.base.setUp()
        self.configure()

    def configure(self):
        self.runtime,self.offline,self.f,self.c=self.base.runtime,self.base.offline,self.base.f,self.base.c
        self.token=self.base.token
        self.key=self.token['instanceId'],self.base.opened['operationId']
        self.record_number=self.c.record_number
        self.path=self.c.diagnostics/f'creation-{self.record_number:02d}.json'
        self.published=False

    def record(self, after=lambda:None):
        original=self.c._record
        def recorded():
            original()
            self.published=True
            after()
        return patch.object(self.c,'_record',side_effect=recorded)

    def recover(self, **changes):
        args=dict(operation_id='recover-checkpoint-01',open_operation_id=self.key[1],
            close_operation_id=self.f.close_operation,termination_sha256=digest(self.c.termination_response),deadline=20)
        args.update(changes)
        return self.runtime._offline_recover_pending_close(self.token,**args)

    def pending(self):
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())
        self.assertIn(self.key,self.offline.close_recoveries)
        self.assertIs(self.c,self.offline.controllers[self.token['instanceId']])
        self.assertEqual(self.base.snapshot,self.offline._release_snapshot(self.c,self.base.workers,self.base.history))
        for actor,process,pipe,resources in self.base.resources:
            self.assertIs(process,actor.process);self.assertIs(pipe,actor.pipe);self.assertIs(resources,actor.creation_resources)

    def failure(self):
        with self.assertRaises(RegistryError):self.base.finish()

    def test_record_return_expiry_freezes_packet_before_any_following_io(self):
        transaction=self.runtime._transaction
        after_reads=[]
        @contextmanager
        def observed(instance,**kwargs):
            if self.published:after_reads.append(kwargs)
            with transaction(instance,**kwargs) as db:yield db
        with self.record(lambda:setattr(self.f,'clock',self.f.deadline)), \
             patch.object(self.runtime,'_transaction',side_effect=observed):
            self.failure()
        self.assertEqual([],after_reads)
        self.assertEqual(self.record_number+1,self.c.record_number)
        self.assertEqual(canonical(self.c.record),self.path.read_bytes())
        self.pending()
        for actor,process,pipe,_ in self.base.resources:
            for name in ('poll','members','close'):
                self.enterContext(patch.object(process,name,side_effect=AssertionError('解放済み資源の再作用')))
            self.enterContext(patch.object(pipe,'close_after_exit',side_effect=AssertionError('pipe再解放')))
        with patch.object(self.c,'_record',side_effect=AssertionError('元局所記録の再書込')):
            result=self.recover()
        self.assertEqual('OFFLINE_CLOSE_TERMINATED',result['status'])
        self.assertEqual(self.record_number+1,self.c.record_number)
        self.base.completed()

    def test_post_record_owner_callback_failure_uses_same_frozen_packet(self):
        callback=self.c.verify_owner
        calls=[]
        def failed():
            if self.published:
                calls.append(True)
                raise OSError('局所記録後の承認I/O不明')
            callback()
        self.enterContext(patch.object(self.c,'verify_owner',side_effect=failed))
        self.base.callback=self.c.verify_owner
        with self.record():
            with self.assertRaises(OSError):self.base.finish()
        self.assertEqual([True],calls)
        self.pending()
        packet=self.offline.close_recoveries[self.key]
        self.recover()
        self.assertIs(packet,self.offline.close_recoveries[self.key])
        self.assertEqual([True],calls)
        self.base.completed()

    def test_post_record_read_exit_expiry_preserves_packet(self):
        transaction=self.runtime._transaction
        @contextmanager
        def late(instance,**kwargs):
            with transaction(instance,**kwargs) as db:yield db
            if self.published and kwargs.get('read_only'):self.f.clock=self.f.deadline
        with self.record(),patch.object(self.runtime,'_transaction',side_effect=late):self.failure()
        self.pending();self.recover();self.base.completed()

    def test_post_record_owner_expiry_starts_no_following_registry_read(self):
        callback=self.c.verify_owner
        transaction=self.runtime._transaction
        expired=False
        reads=[]
        def late():
            nonlocal expired
            callback()
            if self.published:
                self.f.clock=self.f.deadline
                expired=True
        @contextmanager
        def observed(instance,**kwargs):
            if expired:reads.append(kwargs)
            with transaction(instance,**kwargs) as db:yield db
        self.enterContext(patch.object(self.c,'verify_owner',side_effect=late))
        self.base.callback=self.c.verify_owner
        with self.record(),patch.object(self.runtime,'_transaction',side_effect=observed):self.failure()
        self.assertTrue(expired);self.assertEqual([],reads)
        self.pending();self.recover();self.base.completed()

    def test_result_encoding_expiry_starts_no_owner_observation(self):
        original=offline_control.canonical
        callback=self.c.verify_owner
        expired=False
        calls=[]
        def late(value):
            nonlocal expired
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_SESSION_COMPLETED':
                self.f.clock=self.f.deadline
                expired=True
            return result
        def observed():
            if expired:calls.append(True)
            callback()
        with patch.object(offline_control,'canonical',side_effect=late), \
             patch.object(self.c,'verify_owner',side_effect=observed):self.failure()
        self.assertTrue(expired);self.assertEqual([],calls)
        self.assertNotIn(self.key,self.offline.close_recoveries)
        self.assertFalse(self.path.exists())
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())

    def test_record_write_exception_has_no_completed_packet(self):
        with patch.object(self.c,'_record',side_effect=OSError('局所write不明')):
            with self.assertRaises(OSError):self.base.finish()
        self.assertNotIn(self.key,self.offline.close_recoveries)
        with self.assertRaises(RegistryError):self.recover()
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())

    def test_record_exception_after_file_publication_still_requires_recovery_evidence(self):
        original=self.c._record
        def uncertain():
            original()
            raise OSError('正常復帰が確認できない')
        with patch.object(self.c,'_record',side_effect=uncertain):
            with self.assertRaises(OSError):self.base.finish()
        self.assertEqual(canonical(self.c.record),self.path.read_bytes())
        self.assertNotIn(self.key,self.offline.close_recoveries)
        with self.assertRaises(RegistryError):self.recover()
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())

    def before_packet(self, change, *, in_place=False, rebase=True):
        transaction=self.runtime._transaction
        updates=[]
        retained=[]
        originals=(self.c.close_request,self.c.close_response,self.c.owner_release_response,
            self.c.worker_release_response,self.c.writer_closed_response)
        def changed():
            slot=self.offline.retired[self.key]
            retained.append((slot,slot['snapshot'],self.path.read_bytes(),self.c.termination_response))
            change()
            if not rebase:return
            snapshot=self.offline._release_snapshot(self.c,self.base.workers,self.base.history)
            if in_place:
                slot['snapshot'][0][:]=snapshot[0]
                slot['snapshot'][1][:]=snapshot[1]
            else:slot['snapshot']=snapshot
        @contextmanager
        def traced(instance,**kwargs):
            with transaction(instance,**kwargs) as db:
                db.set_trace_callback(lambda sql:updates.append(sql)
                    if (sql.startswith('UPDATE runtime_operations SET status=')
                        or sql.startswith('DELETE FROM reservations')
                        or sql.startswith('UPDATE instances SET pending=0')
                        or sql.startswith('INSERT INTO runtime_operations')) else None)
                yield db
        with self.record(changed),patch.object(self.runtime,'_transaction',side_effect=traced):
            with self.assertRaises(RegistryError) as error:self.base.finish()
            self.assertEqual('PROOF_MISMATCH',error.exception.code)
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual([],updates)
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())
        if rebase:self.assertNotIn(self.key,self.offline.close_recoveries)
        slot,snapshot,raw,response=retained[0]
        self.assertIs(slot,self.offline.retired[self.key])
        if not rebase or in_place:self.assertIs(snapshot,slot['snapshot'])
        else:self.assertIsNot(snapshot,slot['snapshot'])
        self.assertEqual(raw,self.path.read_bytes())
        self.assertIs(response,self.c.termination_response)
        self.assertEqual(originals,(self.c.close_request,self.c.close_response,self.c.owner_release_response,
            self.c.worker_release_response,self.c.writer_closed_response))
        self.assertIs(self.c,self.offline.controllers[self.token['instanceId']])
        for actor,process,pipe,resources in self.base.resources:
            self.assertIs(process,actor.process);self.assertIs(pipe,actor.pipe);self.assertIs(resources,actor.creation_resources)

    def test_pre_packet_unknown_release_and_snapshot_are_not_adopted(self):
        self.before_packet(lambda:self.c.owner_release.__setitem__('handle','UNKNOWN'))
        self.assertEqual('UNKNOWN',self.c.owner_release['handle'])

    def test_pre_packet_changed_record_and_snapshot_are_not_adopted(self):
        self.before_packet(lambda:self.c.record['ownerRelease'].__setitem__('handlesReleased',False))
        self.assertFalse(self.c.record['ownerRelease']['handlesReleased'])

    def test_pre_packet_snapshot_contents_are_not_rebased(self):
        self.before_packet(lambda:self.c.owner_release.__setitem__('handle','UNKNOWN'),in_place=True)
        self.assertEqual('UNKNOWN',self.c.owner_release['handle'])

    def test_record_handle_change_is_not_adopted(self):
        value=9993
        self.before_packet(lambda:setattr(self.c.process,'handle',value),rebase=False)
        self.assertIs(value,self.c.process.handle)

    def test_record_child_fd_change_is_not_adopted(self):
        value=9993
        self.before_packet(lambda:setattr(self.c.pipe,'child_input',value),rebase=False)
        self.assertIs(value,self.c.pipe.child_input)

    def test_record_job_change_is_not_adopted(self):
        value=9993
        self.before_packet(lambda:setattr(self.c.process,'job',value),rebase=False)
        self.assertIs(value,self.c.process.job)

    def test_record_thread_change_is_not_adopted(self):
        value=9993
        self.before_packet(lambda:setattr(self.c.process,'thread',value),rebase=False)
        self.assertIs(value,self.c.process.thread)

    def test_record_native_change_is_not_adopted(self):
        value=copy(self.c.native)
        self.assertIsNot(value,self.c.native)
        self.before_packet(lambda:setattr(self.c,'native',value),rebase=False)
        self.assertIs(value,self.c.native)

    def test_record_adapter_change_is_not_adopted(self):
        value=copy(self.c.adapter)
        self.assertIsNot(value,self.c.adapter)
        self.before_packet(lambda:setattr(self.c,'adapter',value),rebase=False)
        self.assertIs(value,self.c.adapter)

    def test_record_popen_change_is_not_adopted(self):
        value=object()
        self.before_packet(lambda:setattr(self.c.process,'process',value),rebase=False)
        self.assertIs(value,self.c.process.process)

    def test_record_incoming_change_is_not_adopted(self):
        value=bytearray(b'changed')
        self.before_packet(lambda:setattr(self.c.pipe,'incoming',value),rebase=False)
        self.assertIs(value,self.c.pipe.incoming)

    def test_record_worker_handle_change_is_not_adopted(self):
        value=9993
        self.before_packet(lambda:setattr(self.base.workers[0].process,'handle',value),rebase=False)
        self.assertIs(value,self.base.workers[0].process.handle)

    def test_record_close_binding_change_is_not_adopted(self):
        self.before_packet(lambda:self.c.close_binding.__setitem__('extraObservation','changed'),rebase=False)
        self.assertEqual('changed',self.c.close_binding['extraObservation'])

    def test_post_record_memory_deadline_starts_no_following_observation(self):
        original=self.offline._terminal_memory
        callback=self.c.verify_owner
        after=[]
        def late(slot):
            result=original(slot)
            if self.published:self.f.clock=self.f.deadline
            return result
        def observed():
            if self.published:after.append(True)
            callback()
        self.enterContext(patch.object(self.c,'verify_owner',side_effect=observed))
        self.base.callback=self.c.verify_owner
        with self.record(),patch.object(self.offline,'_terminal_memory',side_effect=late):self.failure()
        self.assertEqual([],after)
        self.pending();self.recover();self.base.completed()

    def committed(self, renew=False):
        failures=[]
        def change():
            try:
                if renew:
                    self.runtime.registry.execute('renew',dict(self.token,ttlSeconds=1000),operation_id='renew-checkpoint')
                else:
                    with self.runtime.registry.transaction() as db:
                        db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?',(self.token['instanceId'],))
            except BaseException as error:failures.append(error)
        thread=threading.Thread(target=change);thread.start();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual([],failures)

    def test_registry_epoch_after_record_is_not_adopted(self):
        with self.record(self.committed):self.failure()
        self.pending()
        with self.assertRaises(RegistryError):self.recover()
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())

    def test_normal_public_renew_after_record_preserves_finish_behavior(self):
        original_deadline=self.c.close_binding['deadline']
        with self.record(lambda:self.committed(True)):
            self.assertEqual('OFFLINE_SESSION_COMPLETED',self.base.finish()['status'])
        self.assertEqual(original_deadline,self.c.close_binding['deadline'])
        self.base.completed()

    def after_packet(self, change):
        callback=self.c.verify_owner
        changed=False
        updates=[]
        transaction=self.runtime._transaction
        def observed():
            nonlocal changed
            if self.published and not changed:
                self.assertIn(self.key,self.offline.close_recoveries)
                changed=True
                change()
            callback()
        @contextmanager
        def traced(instance,**kwargs):
            with transaction(instance,**kwargs) as db:
                db.set_trace_callback(lambda sql:updates.append(sql)
                    if sql.startswith("UPDATE runtime_operations SET status='COMPLETED'") else None)
                yield db
        self.enterContext(patch.object(self.c,'verify_owner',side_effect=observed))
        self.base.callback=self.c.verify_owner
        with self.record(),patch.object(self.runtime,'_transaction',side_effect=traced):self.failure()
        self.assertTrue(changed);self.assertEqual([],updates)
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.base.state())
        packet=self.offline.close_recoveries[self.key]
        with self.assertRaises(RegistryError):self.recover()
        self.assertIs(packet,self.offline.close_recoveries[self.key])
        self.assertIs(self.c,self.offline.controllers[self.token['instanceId']])
        return packet

    def test_post_packet_private_fd_change_is_not_rebased(self):
        self.after_packet(lambda:setattr(self.c.pipe,'child_input',9993))
        self.assertEqual(9993,self.c.pipe.child_input)

    def test_post_packet_record_number_change_is_not_rebased(self):
        self.after_packet(lambda:setattr(self.c,'record_number',self.c.record_number+1))
        self.assertEqual(self.record_number+2,self.c.record_number)

    def test_post_packet_registry_response_change_is_not_overwritten(self):
        failures=[]
        def changed():
            def commit():
                try:
                    with self.runtime.registry.transaction() as db:
                        raw=db.execute('SELECT response FROM runtime_operations WHERE id=?',(self.key[1],)).fetchone()[0]
                        value=json.loads(raw);value['otherObservation']='retained'
                        db.execute('UPDATE runtime_operations SET response=? WHERE id=?',(json.dumps(value),self.key[1]))
                except BaseException as error:failures.append(error)
            thread=threading.Thread(target=commit);thread.start();thread.join(5)
            self.assertFalse(thread.is_alive());self.assertEqual([],failures)
        self.after_packet(changed)
        self.assertEqual('retained',self.f.saved()['otherObservation'])

    def test_post_packet_raw_change_keeps_original_packet(self):
        expected=[]
        def changed():
            expected.append(self.c.termination_response)
            self.c.termination_response+=b' '
        packet=self.after_packet(changed)
        self.assertEqual(expected[0],packet[0])
        self.assertEqual(expected[0]+b' ',self.c.termination_response)

    def test_post_packet_foreign_worker_slot_is_preserved(self):
        foreign=object()
        self.after_packet(lambda:self.offline.workers.__setitem__(self.token['instanceId'],foreign))
        self.assertIs(foreign,self.offline.workers[self.token['instanceId']])

    def test_post_packet_old_start_stays_blocked_before_and_after_recover(self):
        with self.record(lambda:setattr(self.f,'clock',self.f.deadline)):self.failure()
        self.pending()
        with patch.object(self.runtime,'refresh'), \
             patch.object(self.runtime,'_active',side_effect=AssertionError('元予約中のstart準備')):
            with self.assertRaises(RegistryError) as error:
                self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='pending-checkpoint-start')
        self.assertEqual('INVALID_STATE',error.exception.code)
        self.recover();self.base.completed();self.base.start_blocked()

    def test_without_worker_record_expiry_can_be_explicitly_recovered(self):
        self.base.base.test_open_without_worker_records_closed();self.base.configure();self.configure()
        self.assertEqual([],self.base.workers)
        with self.record(lambda:setattr(self.f,'clock',self.f.deadline)):self.failure()
        self.pending();self.recover();self.base.completed()

    def test_all_worker_history_record_expiry_can_be_explicitly_recovered(self):
        self.base.base.base.history();self.base.base.base.release();self.base.base.configure()
        self.base.base.record();self.base.configure();self.configure()
        self.assertEqual(2,len(self.base.workers))
        with self.record(lambda:setattr(self.f,'clock',self.f.deadline)):self.failure()
        self.pending();self.recover();self.base.completed()

    def test_new_parent_cannot_adopt_post_record_packet(self):
        with self.record(lambda:setattr(self.f,'clock',self.f.deadline)):self.failure()
        offline=self.runtime._offline
        self.runtime._offline=offline_control._OfflineOpen(self.runtime)
        with self.assertRaises(RegistryError):self.recover()
        self.assertIs(offline,self.offline)
        self.pending()

    def test_known_completed_readback_refuses_pending_checkpoint(self):
        with self.record(lambda:setattr(self.f,'clock',self.f.deadline)):self.failure()
        args=dict(operation_id='known-checkpoint',open_operation_id=self.key[1],close_operation_id=self.f.close_operation,
            termination_sha256=digest(self.c.termination_response),deadline=20)
        with self.assertRaises(RegistryError):self.runtime._offline_recover_close(self.token,**args)
        self.pending()
        self.recover()
        self.assertEqual('OFFLINE_CLOSE_RECOVERED',self.runtime._offline_recover_close(self.token,**args)['status'])
        self.base.completed()

    def test_unexpected_terminal_response_change_rolls_back(self):
        original=offline_control.json.dumps
        count=0
        def changed(value,*args,**kwargs):
            nonlocal count
            result=original(value,*args,**kwargs)
            if isinstance(value,dict) and value.get('termination',{}).get('state')=='COMPLETED':
                count+=1
                if count==2:return result+' '
            return result
        with patch.object(offline_control.json,'dumps',side_effect=changed):self.failure()
        self.assertEqual(2,count)
        self.pending();self.recover();self.base.completed()
