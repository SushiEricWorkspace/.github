"""元flush/closeを肯定できるfsync不明だけを、明示同期へ結ぶ。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import local_ipc, offline_control, runtime_control
from . import test_offline_record_synchronization as synchronized
from .build_inputs import canonical
from .registry import RegistryError
from .test_offline_record_recovery import _File


class FsyncRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case=synchronized.SynchronizedCloseTests()
        self.addCleanup(lambda:self.assertTrue(self.case.doCleanups()))
        self.case.setUp()
        self.runtime,self.offline,self.c,self.f=self.case.runtime,self.case.offline,self.case.c,self.case.f
        self.path,self.key=self.case.path,self.case.key
        self.base=self.case.case.case.base
        self.initial=self.c.record_number
        self.case.private.side_effect=local_ipc.require_private
        local_ipc.make_private(self.path.parent,directory=True)
        self.case.fail_write=self.fail_fsync

    def capture(self):
        self.case.frame=self.offline.close_synchronizations[self.key]
        self.case.expected=canonical(self.c.record)
        self.frame,self.expected=self.case.frame,self.case.expected

    def fail_fsync(self):
        with patch.object(runtime_control.os,'fsync',side_effect=OSError('元fsync不明')):
            with self.assertRaisesRegex(OSError,'元fsync不明'):self.base.finish()
        self.capture()
        self.assertEqual(self.expected,self.path.read_bytes())
        self.assertTrue(self.frame[2].closed)
        self.assertFalse(self.frame[1].closed)
        self.assertNotIn(self.key,self.offline.close_recoveries)
        self.case.pending()

    def test_fsync_unknown_uses_distinct_sync_without_rewrite_or_reclose(self):
        self.fail_fsync()
        for _,process,pipe,_ in self.base.resources:
            for name in ('poll','members','close'):
                self.enterContext(patch.object(process,name,side_effect=AssertionError('元資源の再作用禁止')))
            self.enterContext(patch.object(pipe,'close_after_exit',side_effect=AssertionError('pipe再解放禁止')))
        with patch.object(self.c,'_record',side_effect=AssertionError('元記録再書込禁止')), \
             patch.object(runtime_control,'make_private',side_effect=AssertionError('recoverの権限変更禁止')), \
             patch.object(offline_control.os,'fsync',wraps=offline_control.os.fsync) as fsync:
            result=self.case.recover()
            self.assertEqual(result,self.case.recover())
        fsync.assert_called_once_with(self.frame[7])
        self.assertEqual('OFFLINE_CLOSE_SYNCHRONIZED',result['status'])
        self.assertFalse(result['recordSynchronization']['originalWriteCompleted'])
        self.assertTrue(result['recordSynchronization']['synchronizationCompleted'])
        self.assertEqual(self.initial+1,self.c.record_number)
        self.assertEqual(self.expected,self.path.read_bytes())
        self.assertFalse(self.frame[1].closed)
        self.base.completed()
        with self.assertRaises(RegistryError):self.case.case.recover()
        with self.assertRaises(RegistryError):self.case.case.case.recover()

    def test_flush_unknown_with_full_bytes_does_not_start_sync(self):
        original=Path.open
        record=self.c._record
        def unobserved(*,observe=None,retain=None):
            def retained(handle,stage):
                if stage!='closed':retain(handle,stage)
            return record(observe=observe,retain=retained)
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return _File(file,'flush') if path==self.path and args==('x+b',) else file
        with patch.object(Path,'open',opened),patch.object(self.c,'_record',side_effect=unobserved):
            with self.assertRaisesRegex(OSError,'flush不明'):self.base.finish()
        self.capture()
        self.assertEqual(self.expected,self.path.read_bytes())
        with self.case.case.no_terminal_write(), \
             patch.object(offline_control.os,'fsync',side_effect=AssertionError('flush不明後の同期禁止')):
            with self.assertRaises(RegistryError):self.case.recover()
        self.case.pending()

    def test_close_unknown_after_fsync_failure_does_not_start_sync(self):
        original=Path.open
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return _File(file,'close') if path==self.path and args==('x+b',) else file
        with patch.object(Path,'open',opened), \
             patch.object(runtime_control.os,'fsync',side_effect=OSError('元fsync不明')):
            with self.assertRaisesRegex(OSError,'close不明'):self.base.finish()
        self.capture()
        with self.case.case.no_terminal_write(), \
             patch.object(offline_control.os,'fsync',side_effect=AssertionError('close不明後の同期禁止')):
            with self.assertRaises(RegistryError):self.case.recover()
        self.case.pending()

    def test_successful_original_exit_is_called_only_once_after_fsync_failure(self):
        original=Path.open
        exits=[]
        class File(_File):
            def __exit__(self,*args):
                exits.append(args[0])
                return super().__exit__(*args)
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return File(file,None) if path==self.path and args==('x+b',) else file
        with patch.object(Path,'open',opened):self.fail_fsync()
        self.assertEqual([OSError],exits)
        self.case.recover()
        self.assertEqual([OSError],exits)
        self.base.completed()

    def test_close_notification_deadline_does_not_adopt_closed_property(self):
        original=Path.open
        class File(_File):
            def __exit__(file,*args):
                result=super().__exit__(*args)
                self.f.clock=20
                return result
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return File(file,None) if path==self.path and args==('x+b',) else file
        with patch.object(Path,'open',opened), \
             patch.object(runtime_control.os,'fsync',side_effect=OSError('元fsync不明')):
            with self.assertRaises(RegistryError) as expired:self.base.finish()
        self.assertEqual('STOP_TIMEOUT',expired.exception.code)
        self.capture()
        self.assertTrue(self.frame[2].closed)
        with self.case.case.no_terminal_write(), \
             patch.object(offline_control.os,'fsync',side_effect=AssertionError('close未観測後の同期禁止')):
            with self.assertRaises(RegistryError):self.case.recover(deadline=30)
        self.case.pending()

    def test_original_writer_owns_close_notification_after_fsync_failure(self):
        original=self.c._record
        real_fsync=runtime_control.os.fsync
        writer_id=[]
        entered,resume=threading.Event(),threading.Event()
        def record(*,observe=None,retain=None):
            def retained(handle,stage):
                retain(handle,stage)
                if stage=='closed':
                    entered.set()
                    self.assertTrue(resume.wait(30),'元writerを再開できません')
            return original(observe=observe,retain=retained)
        def writer():
            writer_id.append(threading.get_ident())
            try:return self.base.finish()
            except (RegistryError,OSError) as error:return error
        def fsync(fd):
            if threading.get_ident() in writer_id:raise OSError('元fsync不明')
            return real_fsync(fd)
        with patch.object(self.c,'_record',side_effect=record), \
             patch.object(runtime_control,'time',SimpleNamespace(monotonic=lambda:self.f.clock,sleep=time.sleep)), \
             patch.object(runtime_control.os,'fsync',side_effect=fsync) as observed:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(writer)
                try:
                    self.assertTrue(entered.wait(30),'肯定close通知へ到達しません')
                    self.capture()
                    with patch.object(self.frame[1],'read',wraps=self.frame[1].read) as read:
                        with self.assertRaises(RegistryError) as busy:self.case.recover()
                    self.assertEqual('RECOVERY_REQUIRED',busy.exception.code)
                    read.assert_not_called()
                    observed.assert_called_once()
                    self.assertFalse(future.done())
                    self.assertEqual(self.initial,self.c.record_number)
                    self.case.pending()
                finally:
                    resume.set()
                    result=future.result(40)
        self.assertIsInstance(result,OSError)
        self.assertEqual('元fsync不明',str(result))
        self.case.recover()
        self.assertEqual(self.initial+1,self.c.record_number)
        self.base.completed()

    def test_wrong_bytes_are_preserved_without_sync(self):
        self.case.test_full_different_bytes_are_preserved_without_sync()

    def test_partial_bytes_are_preserved_without_sync(self):
        self.case.test_partial_prefix_is_preserved_without_sync_or_terminal_dml()

    def test_epoch_commit_during_observation_prevents_terminal_write(self):
        self.case.test_epoch_commit_during_last_permission_observation_prevents_terminal_write()

    def test_public_renew_keeps_fixed_deadlines(self):
        self.case.test_public_renew_does_not_extend_either_deadline()

    def test_concurrent_same_id_synchronizes_once(self):
        self.case.concurrent_recover('recover-synchronized-close')

    def test_concurrent_other_id_synchronizes_once(self):
        self.case.concurrent_recover('recover-synchronized-other')

    def test_new_sync_unknown_is_not_retried(self):
        self.case.test_new_sync_failure_is_unknown_and_never_retried()

    def test_read_deadline_starts_no_new_flush(self):
        self.case.test_deadline_after_read_starts_no_flush()

    def test_permission_failure_starts_no_sync(self):
        self.case.test_permission_observation_is_required_before_sync()
