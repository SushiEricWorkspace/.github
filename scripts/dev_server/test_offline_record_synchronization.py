"""書込み前pinと明示同期の別証拠を、元closeのCASへ結ぶ。"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import offline_control, runtime_control, local_ipc
from . import test_offline_record_recovery as recorded
from .build_inputs import canonical, digest
from .registry import RegistryError


class SynchronizedCloseTests(unittest.TestCase):
    def setUp(self):
        self.case=recorded.RecordedCloseTests()
        self.addCleanup(lambda:self.assertTrue(self.case.doCleanups()))
        self.case.setUp()
        self.runtime,self.offline,self.c,self.f=self.case.runtime,self.case.offline,self.case.c,self.case.f
        self.token,self.key,self.path=self.case.token,self.case.key,self.case.case.path
        self.private=self.enterContext(patch.object(offline_control,'require_private'))

    def fail_write(self):
        with patch.object(runtime_control,'make_private',side_effect=OSError('元権限設定の結果不明')):
            with self.assertRaises(OSError):self.case.case.base.finish()
        self.frame=self.offline.close_synchronizations[self.key]
        self.expected=canonical(self.c.record)
        self.assertEqual(self.expected,self.path.read_bytes())
        self.assertNotIn(self.key,self.offline.close_recoveries)
        self.case.pending()

    def recover(self,**changes):
        args=dict(operation_id='recover-synchronized-close',open_operation_id=self.key[1],
            close_operation_id=self.f.close_operation,termination_sha256=digest(self.c.termination_response),deadline=20)
        args.update(changes)
        return self.runtime._offline_recover_synchronized_close(self.token,**args)

    def pending(self):
        self.case.pending()
        self.assertIs(self.frame,self.offline.close_synchronizations[self.key])
        self.assertFalse(self.frame[1].closed)
        self.assertEqual(self.expected,self.path.read_bytes())

    def test_private_failure_can_terminate_with_distinct_synchronization_evidence(self):
        self.fail_write()
        for actor,process,pipe,_ in self.case.case.base.resources:
            for name in ('poll','members','close'):
                self.enterContext(patch.object(process,name,side_effect=AssertionError('元資源の再作用禁止')))
            self.enterContext(patch.object(pipe,'close_after_exit',side_effect=AssertionError('pipe再解放禁止')))
        self.enterContext(patch.object(self.c,'_record',side_effect=AssertionError('元記録再書込禁止')))
        result=self.recover()
        self.assertEqual('OFFLINE_CLOSE_SYNCHRONIZED',result['status'])
        proof=result['recordSynchronization']
        self.assertFalse(proof['originalWriteCompleted'])
        self.assertTrue(proof['synchronizationCompleted'])
        self.assertEqual(digest(self.expected),proof['recordSha256'])
        self.assertEqual(result['operationId'],proof['operationId'])
        self.assertEqual(self.frame[3],(proof['fileDevice'],proof['fileId']))
        self.assertFalse(self.frame[1].closed)
        self.assertEqual(self.expected,self.path.read_bytes())
        self.case.case.base.completed()
        with self.runtime._transaction(self.key[0],read_only=True) as db:
            row=db.execute('SELECT * FROM runtime_operations WHERE id=?',(result['operationId'],)).fetchone()
            self.assertEqual('offline-recover-synchronized-close',row['command'])
            self.assertEqual(result,offline_control.json.loads(row['response']))

    def test_same_recover_id_does_not_repeat_synchronization(self):
        self.fail_write()
        result=self.recover()
        with patch.object(offline_control.os,'fsync',side_effect=AssertionError('同期再実行禁止')):
            self.assertEqual(result,self.recover())

    def concurrent_recover(self, second_id):
        self.fail_write()
        pin=self.frame[1]
        seek,read,flush,fsync=pin.seek,pin.read,pin.flush,offline_control.os.fsync
        initial=self.c.record_number
        first_read,second_boundary,first_sync,second_done=(threading.Event() for _ in range(4))
        io_lock=threading.Lock()
        actors={};readers=set();counts={'flush':0,'fsync':0}

        def seeking(*args):
            io_lock.acquire()
            try:return seek(*args)
            except BaseException:
                io_lock.release();raise

        def reading(*args):
            try:value=read(*args)
            finally:io_lock.release()
            actor=threading.get_ident()
            if actor not in readers:
                readers.add(actor)
                if actor==actors['first']:
                    first_read.set()
                    self.assertTrue(second_boundary.wait(30),'競合要求が完了しません')
                else:
                    second_boundary.set()
                    self.assertTrue(first_sync.wait(30),'先行同期が進みません')
            return value

        def flushing():
            counts['flush']+=1
            return flush()

        def syncing(fd):
            if fd==pin.fileno():
                counts['fsync']+=1
                if threading.get_ident()==actors['first']:
                    first_sync.set()
                    self.assertTrue(second_done.wait(30),'競合要求が完了しません')
            return fsync(fd)

        def call(first):
            actors['first' if first else 'second']=threading.get_ident()
            try:
                return self.recover(operation_id='recover-synchronized-close' if first else second_id)
            except RegistryError as error:
                return error.code
            finally:
                if not first:
                    second_boundary.set();second_done.set()

        with patch.object(runtime_control,'time',SimpleNamespace(monotonic=lambda:self.f.clock,sleep=time.sleep)), \
             patch.object(pin,'seek',side_effect=seeking),patch.object(pin,'read',side_effect=reading), \
             patch.object(pin,'flush',side_effect=flushing),patch.object(offline_control.os,'fsync',side_effect=syncing):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(call,True)
                try:
                    self.assertTrue(first_read.wait(30),'元pinの実読へ到達しません')
                    second=pool.submit(call,False)
                    other=second.result(40)
                    result=first.result(40)
                finally:
                    second_boundary.set();first_sync.set();second_done.set()
        self.assertEqual({'flush':1,'fsync':1},counts)
        self.assertEqual(initial+1,self.c.record_number)
        self.assertEqual({actors['first']},readers)
        self.assertEqual('RECOVERY_REQUIRED',other)
        self.assertEqual('OFFLINE_CLOSE_SYNCHRONIZED',result['status'])
        self.assertEqual(self.expected,self.path.read_bytes())
        self.assertIs(self.frame,self.offline.close_synchronizations[self.key])
        self.assertFalse(pin.closed)
        self.case.case.base.completed()
        with self.runtime._transaction(self.key[0],read_only=True) as db:
            row=db.execute('SELECT command,status FROM runtime_operations WHERE id=?',
                ('recover-synchronized-close',)).fetchone()
            self.assertEqual(('offline-recover-synchronized-close','COMPLETED'),tuple(row))
            if second_id!='recover-synchronized-close':
                self.assertIsNone(db.execute('SELECT 1 FROM runtime_operations WHERE id=?',(second_id,)).fetchone())

    def test_concurrent_same_id_synchronizes_once(self):
        self.concurrent_recover('recover-synchronized-close')

    def test_concurrent_other_id_synchronizes_once(self):
        self.concurrent_recover('recover-synchronized-other')

    def original_writer_recover(self, fails):
        entered,resume=threading.Event(),threading.Event()
        initial=self.c.record_number
        self.private.side_effect=local_ipc.require_private

        def private(path):
            local_ipc.make_private(path)
            local_ipc.require_private(path)
            entered.set()
            self.assertTrue(resume.wait(30),'元writerを再開できません')
            if fails:raise OSError('元権限設定の結果不明')

        def writer():
            try:return self.case.case.base.finish()
            except (RegistryError,OSError) as error:return error

        original=self.runtime._transaction
        main=threading.get_ident()
        observations=[]
        @contextmanager
        def transaction(*args,**options):
            if threading.get_ident()==main:observations.append(True)
            with original(*args,**options) as db:yield db

        with patch.object(runtime_control,'time',SimpleNamespace(monotonic=lambda:self.f.clock,sleep=time.sleep)), \
             patch.object(runtime_control,'make_private',side_effect=private):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(writer)
                try:
                    self.assertTrue(entered.wait(30),'元writerの権限設定へ到達しません')
                    self.frame=self.offline.close_synchronizations[self.key]
                    self.expected=canonical(self.c.record)
                    pin=self.frame[1]
                    self.assertFalse(future.done())
                    self.assertTrue(self.frame[2].closed)
                    with patch.object(pin,'seek',wraps=pin.seek) as seek, \
                         patch.object(pin,'read',wraps=pin.read) as read, \
                         patch.object(pin,'flush',wraps=pin.flush) as flush, \
                         patch.object(offline_control.os,'fsync',wraps=offline_control.os.fsync) as fsync, \
                         patch.object(self.runtime,'_transaction',side_effect=transaction):
                        try:denied=self.recover()
                        except RegistryError as error:denied=error
                        self.assertIsInstance(denied,RegistryError)
                        self.assertEqual('RECOVERY_REQUIRED',denied.code)
                        seek.assert_not_called();read.assert_not_called();flush.assert_not_called();fsync.assert_not_called()
                        self.assertFalse(observations)
                    self.private.assert_not_called()
                    self.assertEqual(initial,self.c.record_number)
                    self.pending()
                finally:
                    resume.set()
                    result=future.result(40)
        self.assertFalse(self.frame[8].locked())
        if fails:
            self.assertIsInstance(result,OSError)
            self.assertEqual(initial,self.c.record_number)
            self.pending()
            with patch.object(self.frame[1],'flush',wraps=self.frame[1].flush) as flush, \
                 patch.object(offline_control.os,'fsync',wraps=offline_control.os.fsync) as fsync:
                recovered=self.recover()
            flush.assert_called_once();fsync.assert_called_once()
            self.assertEqual('OFFLINE_CLOSE_SYNCHRONIZED',recovered['status'])
            self.assertFalse(self.frame[1].closed)
            self.assertEqual(recovered,self.recover())
        else:
            self.assertIsInstance(result,dict)
            self.assertTrue(self.frame[1].closed)
            with self.assertRaises(RegistryError) as denied:self.recover()
            self.assertEqual('RECOVERY_REQUIRED',denied.exception.code)
        self.assertEqual(initial+1,self.c.record_number)
        self.assertEqual(self.expected,self.path.read_bytes())
        self.assertIs(self.frame,self.offline.close_synchronizations[self.key])
        self.case.case.base.completed()

    def test_active_original_writer_finishes_normally_before_recovery(self):
        self.original_writer_recover(False)

    def test_active_original_writer_fails_before_distinct_synchronization(self):
        self.original_writer_recover(True)

    def test_legacy_recover_cannot_relabel_new_synchronization_as_original_write(self):
        self.fail_write();self.recover()
        with self.assertRaises(RegistryError):self.case.recover()
        with self.assertRaises(RegistryError):self.case.case.recover()

    def test_missing_pin_after_parent_restart_starts_no_sync(self):
        self.fail_write()
        self.runtime._offline=offline_control._OfflineOpen(self.runtime)
        with patch.object(offline_control.os,'fsync',side_effect=AssertionError('pinなし同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.runtime._offline=self.offline
        self.pending()

    def test_partial_prefix_is_preserved_without_sync_or_terminal_dml(self):
        self.fail_write();self.path.write_bytes(self.expected[:3])
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('部分同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual(self.expected[:3],self.path.read_bytes())
        self.case.pending()

    def test_full_different_bytes_are_preserved_without_sync(self):
        self.fail_write();self.path.write_bytes(b'{}')
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('不一致同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual(b'{}',self.path.read_bytes());self.case.pending()

    def test_equal_bytes_different_file_identity_are_not_adopted(self):
        self.fail_write();original=Path.lstat
        def replaced(path,*args,**options):
            value=original(path,*args,**options)
            return SimpleNamespace(st_dev=value.st_dev,st_ino=value.st_ino+1,st_mode=value.st_mode,
                st_nlink=value.st_nlink,st_file_attributes=0) if path==self.path else value
        with self.case.no_terminal_write(),patch.object(Path,'lstat',replaced), \
             patch.object(offline_control.os,'fsync',side_effect=AssertionError('別実体同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_permission_observation_is_required_before_sync(self):
        self.fail_write();self.private.side_effect=PermissionError('権限不一致')
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('権限不明同期禁止')):
            with self.assertRaises(PermissionError):self.recover()
        self.pending()

    def test_original_counter_only_does_not_prove_completion(self):
        self.fail_write();self.c.record_number+=1
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('番号のみ同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_mutable_slot_and_resource_changes_are_not_rebased(self):
        self.fail_write();self.c.process.handle=9993
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('資源変更同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual(9993,self.c.process.handle);self.pending()

    def test_preparation_reference_replacement_is_rejected(self):
        self.fail_write()
        self.offline.close_preparations[self.key]=tuple(list(self.offline.close_preparations[self.key]))
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('epoch変更後の同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_epoch_commit_during_last_permission_observation_prevents_terminal_write(self):
        self.fail_write();committed=False
        def private(path,**options):
            nonlocal committed
            if not committed:
                committed=True;self.case.case.committed()
        self.private.side_effect=private
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('epoch変更後の同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_public_renew_does_not_extend_either_deadline(self):
        self.fail_write();old=self.c.close_binding['deadline'];done=False
        def private(path,**options):
            nonlocal done
            if not done:done=True;self.case.case.committed(True)
        self.private.side_effect=private
        result=self.recover()
        self.assertEqual('OFFLINE_CLOSE_SYNCHRONIZED',result['status'])
        self.assertEqual(old,self.c.close_binding['deadline']);self.case.case.base.completed()

    def test_original_intent_change_prevents_synchronization(self):
        self.fail_write()
        with self.runtime._transaction(self.key[0]) as db:
            db.execute('UPDATE runtime_operations SET response=response || ? WHERE id=?',(' ',self.key[1]))
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('CAS不一致同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_new_sync_failure_is_unknown_and_never_retried(self):
        self.fail_write()
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=OSError('新しい同期結果不明')) as sync:
            with self.assertRaises(OSError):self.recover()
            sync.side_effect=AssertionError('同期再実行禁止')
            with self.assertRaises(RegistryError):self.recover()
        sync.assert_called_once();self.pending()

    def test_deadline_after_flush_starts_no_fsync(self):
        self.fail_write()
        def flushed():self.f.clock=20
        with self.case.no_terminal_write(),patch.object(self.frame[1],'flush',side_effect=flushed), \
             patch.object(offline_control.os,'fsync',side_effect=AssertionError('期限後fsync禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_deadline_after_read_starts_no_flush(self):
        self.fail_write();read=self.frame[1].read
        def delayed(*args):
            result=read(*args);self.f.clock=20;return result
        with self.case.no_terminal_write(),patch.object(self.frame[1],'read',side_effect=delayed), \
             patch.object(self.frame[1],'flush',side_effect=AssertionError('期限後flush禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_resource_entry_deadline_starts_no_file_observation(self):
        self.fail_write();original=self.runtime.registry.resource_use
        @contextmanager
        def expired():
            with original():self.f.clock=20;yield
        with self.case.no_terminal_write(),patch.object(self.runtime.registry,'resource_use',side_effect=expired):
            with self.assertRaises(RegistryError):self.recover()
        self.private.assert_not_called();self.pending()

    def test_resource_exit_deadline_preserves_committed_terminal_and_pin(self):
        self.fail_write();original=self.runtime.registry.resource_use;level=0
        @contextmanager
        def expired():
            nonlocal level
            level+=1
            with original():yield
            level-=1
            if not level:self.f.clock=20
        with patch.object(self.runtime.registry,'resource_use',side_effect=expired):
            with self.assertRaises(RegistryError):self.recover()
        self.assertFalse(self.frame[1].closed);self.case.case.base.completed()

    def test_final_record_read_follows_registry_and_native_observations(self):
        self.fail_write();original=self.runtime._transaction;reads=[]
        @contextmanager
        def tracked(*args,**options):
            with original(*args,**options) as db:yield db
            reads.append(True)
        pin_read=self.frame[1].read
        counts=[]
        def read(*args):counts.append(len(reads));return pin_read(*args)
        with patch.object(self.runtime,'_transaction',side_effect=tracked),patch.object(self.frame[1],'read',side_effect=read):
            self.recover()
        self.assertEqual(len(reads),counts[-1]);self.assertGreater(counts[-1],counts[0])

    def test_final_registry_exit_file_change_prevents_success(self):
        self.fail_write();original=self.runtime._transaction
        @contextmanager
        def changed(*args,**options):
            with original(*args,**options) as db:yield db
            if self.key[0] not in self.offline.controllers:self.path.write_bytes(b'{}')
        with patch.object(self.runtime,'_transaction',side_effect=changed):
            with self.assertRaises(RegistryError):self.recover()
        self.case.case.base.completed();self.assertFalse(self.frame[1].closed)

    def test_original_close_without_positive_completion_is_not_recovered(self):
        original=Path.open
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return recorded._File(file,'close') if path==self.path and args in (('xb',),('x+b',)) else file
        with patch.object(Path,'open',opened):
            with self.assertRaises(OSError):self.case.case.base.finish()
        self.frame=self.offline.close_synchronizations[self.key];self.expected=canonical(self.c.record)
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('元close不明同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_original_fsync_exception_cannot_be_replaced_by_matching_file(self):
        with patch.object(runtime_control.os,'fsync',side_effect=OSError('元fsync不明')):
            with self.assertRaises(OSError):self.case.case.base.finish()
        self.frame=self.offline.close_synchronizations[self.key];self.expected=canonical(self.c.record)
        self.assertEqual(self.expected,self.path.read_bytes())
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('元同期証拠不足')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_other_operation_id_conflict_starts_no_new_sync(self):
        self.fail_write()
        with self.runtime._transaction(self.key[0]) as db:
            db.execute('INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)',
                ('recover-synchronized-close','other','other',self.key[0],'COMPLETED','{}'))
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('ID競合後同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_pin_replacement_cannot_adopt_current_file_as_original(self):
        self.fail_write();original=self.frame
        other=self.path.open('r+b')
        self.addCleanup(other.close)
        values=list(original);values[1]=other
        self.offline.close_synchronizations[self.key]=tuple(values)
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=AssertionError('pin再採用禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.offline.close_synchronizations[self.key]=original
        self.pending()

    def test_file_change_during_registry_observation_prevents_sync(self):
        self.fail_write();transaction=self.runtime._transaction
        @contextmanager
        def changed(*args,**options):
            with transaction(*args,**options) as db:yield db
            self.path.write_bytes(b'{}')
        with self.case.no_terminal_write(),patch.object(self.runtime,'_transaction',side_effect=changed), \
             patch.object(offline_control.os,'fsync',side_effect=AssertionError('原文変更後同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.case.pending();self.assertEqual(b'{}',self.path.read_bytes())

    def test_original_success_does_not_leave_open_pin(self):
        self.case.case.base.finish()
        frame=self.offline.close_synchronizations[self.key]
        self.assertTrue(frame[1].closed)
        with self.assertRaises(RegistryError):self.recover()

    def test_write_inner_pin_frame_change_rolls_back_terminal(self):
        self.fail_write();transaction=self.runtime._transaction
        @contextmanager
        def changed(*args,**options):
            with transaction(*args,**options) as db:
                def trace(sql):
                    if sql.startswith("UPDATE runtime_operations SET status='COMPLETED'"):
                        self.offline.close_synchronizations[self.key]=tuple(list(self.frame))
                db.set_trace_callback(trace)
                yield db
        with patch.object(self.runtime,'_transaction',side_effect=changed):
            with self.assertRaises(RegistryError):self.recover()
        self.case.pending();self.assertFalse(self.frame[1].closed)

    def test_permission_deadline_starts_no_pin_stat(self):
        self.fail_write()
        self.private.side_effect=lambda path,**options:setattr(self.f,'clock',20)
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fstat',side_effect=AssertionError('期限後fstat禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_pin_stat_deadline_starts_no_path_stat(self):
        self.fail_write();fstat=offline_control.os.fstat;lstat=Path.lstat;late=[]
        def delayed(fd):
            value=fstat(fd);self.f.clock=20;return value
        def observed(path,*args,**options):
            if self.f.clock>=20:late.append(True)
            return lstat(path,*args,**options)
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fstat',side_effect=delayed),patch.object(Path,'lstat',observed), \
             patch.object(self.frame[1],'read',side_effect=AssertionError('期限後read禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual([],late);self.pending()

    def test_seek_deadline_starts_no_read(self):
        self.fail_write();seek=self.frame[1].seek
        def delayed(*args):
            value=seek(*args);self.f.clock=20;return value
        with self.case.no_terminal_write(),patch.object(self.frame[1],'seek',side_effect=delayed), \
             patch.object(self.frame[1],'read',side_effect=AssertionError('期限後read禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_new_fsync_deadline_starts_no_following_file_observation(self):
        self.fail_write();sync=offline_control.os.fsync;read=self.frame[1].read;late=[]
        def delayed(fd):sync(fd);self.f.clock=20
        def observed(*args):
            if self.f.clock>=20:late.append(True)
            return read(*args)
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',side_effect=delayed), \
             patch.object(self.frame[1],'read',side_effect=observed):
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual([],late);self.pending()

    def test_mac_full_sync_is_required_and_uses_original_pin(self):
        self.fail_write();full=Mock();fcntl=SimpleNamespace(fcntl=full,F_FULLFSYNC=51)
        with patch.object(offline_control,'sys',SimpleNamespace(platform='darwin')),patch.dict(sys.modules,{'fcntl':fcntl}):
            self.assertEqual('OFFLINE_CLOSE_SYNCHRONIZED',self.recover()['status'])
        full.assert_called_once_with(self.frame[7],51)
        self.case.case.base.completed()

    def test_mac_full_sync_failure_preserves_unknown_without_retry(self):
        self.fail_write();full=Mock(side_effect=OSError('完全同期不明'))
        fcntl=SimpleNamespace(fcntl=full,F_FULLFSYNC=51)
        with self.case.no_terminal_write(),patch.object(offline_control,'sys',SimpleNamespace(platform='darwin')), \
             patch.dict(sys.modules,{'fcntl':fcntl}):
            with self.assertRaises(OSError):self.recover()
            full.side_effect=AssertionError('完全同期再実行禁止')
            with self.assertRaises(RegistryError):self.recover()
        full.assert_called_once();self.pending()

    def test_mac_full_sync_deadline_starts_no_following_read(self):
        self.fail_write();full=Mock(side_effect=lambda *args:setattr(self.f,'clock',20));read=self.frame[1].read;late=[]
        fcntl=SimpleNamespace(fcntl=full,F_FULLFSYNC=51)
        def observed(*args):
            if self.f.clock>=20:late.append(True)
            return read(*args)
        with self.case.no_terminal_write(),patch.object(offline_control,'sys',SimpleNamespace(platform='darwin')), \
             patch.dict(sys.modules,{'fcntl':fcntl}),patch.object(self.frame[1],'read',side_effect=observed):
            with self.assertRaises(RegistryError):self.recover()
        full.assert_called_once();self.assertEqual([],late);self.pending()

    def test_pin_fd_value_is_fixed_before_write(self):
        self.fail_write()
        with self.case.no_terminal_write(),patch.object(self.frame[1],'fileno',return_value=self.frame[7]+1), \
             patch.object(offline_control.os,'fsync',side_effect=AssertionError('別FD同期禁止')), \
             patch.object(self.offline,'_terminal_resources',side_effect=AssertionError('別FD検出前の観測禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_original_private_deadline_keeps_pin_and_completed_write_evidence(self):
        with patch.object(runtime_control,'make_private',side_effect=lambda path:setattr(self.f,'clock',self.f.deadline)):
            with self.assertRaises(RegistryError):self.case.case.base.finish()
        self.frame=self.offline.close_synchronizations[self.key]
        self.assertFalse(self.frame[1].closed)
        self.assertIn(self.key,self.offline.close_recoveries)
        self.case.recover();self.case.case.base.completed()

    def test_reparse_attribute_at_actual_stat_is_not_ignored_after_path_check(self):
        self.fail_write();lstat=Path.lstat;calls=0
        def changed(path,*args,**options):
            nonlocal calls
            value=lstat(path,*args,**options)
            if path==self.path:
                calls+=1
                if calls==2:
                    return SimpleNamespace(st_dev=value.st_dev,st_ino=value.st_ino,st_mode=value.st_mode,
                        st_nlink=value.st_nlink,st_file_attributes=getattr(value,'st_file_attributes',0)|0x400)
            return value
        with self.case.no_terminal_write(),patch.object(Path,'lstat',changed), \
             patch.object(self.frame[1],'flush',side_effect=AssertionError('reparse観測後の同期禁止')):
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual(2,calls);self.pending()

    def test_source_token_binding_precedes_parent_observation(self):
        self.fail_write();original=self.token
        for change in ({'epoch':float(original['epoch'])},{'epoch':original['epoch']+1},{'leaseId':'other-lease'}):
            self.token=dict(original,**change)
            with patch.object(self.offline,'_terminal_resources') as observed:
                with self.assertRaises(RegistryError):self.recover()
                observed.assert_not_called()
        self.token=original;self.pending()

    def test_other_open_alias_cannot_change_original_request(self):
        self.fail_write()
        self.offline.close_synchronizations[(self.key[0],'other-open')]=self.frame
        with patch.object(self.offline,'_terminal_resources') as observed:
            with self.assertRaises(RegistryError):self.recover(open_operation_id='other-open')
            observed.assert_not_called()
        self.pending()

    def test_new_sync_packet_handoff_failure_does_not_repeat_sync(self):
        self.fail_write()
        class Failed(dict):
            def __setitem__(mapping,key,value):raise OSError('新しい同期証拠の引渡し不明')
        self.offline.close_recoveries=Failed()
        sync=offline_control.os.fsync
        with self.case.no_terminal_write(),patch.object(offline_control.os,'fsync',wraps=sync) as observed:
            with self.assertRaises(OSError):self.recover()
            with self.assertRaises(RegistryError):self.recover()
        observed.assert_called_once();self.assertNotIn(self.key,self.offline.close_recoveries);self.pending()

    def test_permissions_are_observed_on_original_pinned_descriptor(self):
        self.fail_write();seen=[]
        def private(path,*,fd=None):
            self.assertEqual(self.path,path)
            self.assertEqual(self.frame[7],fd)
            seen.append(fd)
        self.private.side_effect=private
        self.recover()
        self.assertGreaterEqual(len(seen),3)
        self.case.case.base.completed()


class PrivateDescriptorTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows descriptorの合成検証')
    def test_windows_private_check_uses_original_handle(self):
        import msvcrt
        from . import windows_security
        path=Path('record.json')
        with patch.object(local_ipc,'plain_path'),patch.object(msvcrt,'get_osfhandle',return_value=777) as handle, \
             patch.object(windows_security,'require_private') as private:
            local_ipc.require_private(path,fd=0)
        handle.assert_called_once_with(0);private.assert_called_once_with(handle=777)

    @unittest.skipUnless(os.name=='nt','Windows既定の互換検証')
    def test_default_private_check_still_uses_named_path(self):
        import msvcrt
        from . import windows_security
        path=Path('record.json')
        with patch.object(local_ipc,'plain_path'),patch.object(msvcrt,'get_osfhandle') as handle, \
             patch.object(windows_security,'require_private') as private:
            local_ipc.require_private(path)
        handle.assert_not_called();private.assert_called_once_with(path=path)

    def test_posix_private_check_reads_owner_mode_from_same_descriptor(self):
        path=Path('record.json');info=SimpleNamespace(st_uid=123,st_mode=0o100600)
        system=SimpleNamespace(name='posix',getuid=lambda:123,fstat=Mock(return_value=info))
        with patch.object(local_ipc,'plain_path'),patch.object(local_ipc,'os',system), \
             patch.object(Path,'lstat',side_effect=AssertionError('別実体の権限読取禁止')):
            local_ipc.require_private(path,fd=9)
            system.fstat.assert_called_once_with(9)
            info.st_mode=0o100644
            with self.assertRaises(PermissionError):local_ipc.require_private(path,fd=9)
