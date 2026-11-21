"""元buffered closeの正常復帰を、明示flush不明の別の肯定証拠へ結ぶ。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import offline_control, runtime_control
from . import test_offline_record_fsync_recovery as fsync_case
from .build_inputs import canonical
from .registry import RegistryError


class BufferedCloseTests(unittest.TestCase):
    def test_original_close_drains_buffer_before_notification(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path=Path(directory.name)/'record.json'
        original=Path.open
        stages,exits,before=[],[],[]
        value={'original':'保持した原文'}
        class File(fsync_case._File):
            def flush(file):
                before.append(os.fstat(file.fileno()).st_size)
                raise OSError('元flush不明')
            def __exit__(file,*args):
                result=super().__exit__(*args)
                exits.append(args[0])
                return result
        def opened(target,*args,**options):
            file=original(target,*args,**options)
            return File(file,None) if target==path and args==('x+b',) else file
        def retained(file,stage):
            stages.append(stage)
            if stage=='closed':
                self.assertTrue(file.closed)
                self.assertEqual(canonical(value),path.read_bytes())
        with patch.object(Path,'open',opened), \
             patch.object(runtime_control.os,'fsync',side_effect=AssertionError('元fsyncは未開始')), \
             patch.object(runtime_control,'make_private',side_effect=AssertionError('元権限設定は未開始')):
            with self.assertRaisesRegex(OSError,'元flush不明'):
                runtime_control.write_json(path,value,retain=retained)
        self.assertEqual([0],before)
        self.assertEqual(['opened','written','closed'],stages)
        self.assertEqual([OSError],exits)


class FlushRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case=fsync_case.FsyncRecoveryTests()
        self.addCleanup(lambda:self.assertTrue(self.case.doCleanups()))
        self.case.setUp()
        self.legacy=self.case.case
        self.runtime,self.offline,self.c,self.f=self.case.runtime,self.case.offline,self.case.c,self.case.f
        self.path,self.key,self.base=self.case.path,self.case.key,self.case.base
        self.initial=self.c.record_number
        self.case.fail_fsync=self.fail_flush
        self.legacy.fail_write=self.fail_flush
        self.exits=[]

    def fail_flush(self):
        original=Path.open
        class File(fsync_case._File):
            def flush(file):raise OSError('元flush不明')
            def __exit__(file,*args):
                result=super().__exit__(*args)
                self.exits.append(args[0])
                return result
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return File(file,None) if path==self.path and args==('x+b',) else file
        with patch.object(Path,'open',opened), \
             patch.object(runtime_control.os,'fsync',side_effect=AssertionError('元fsyncは未開始')) as fsync:
            with self.assertRaisesRegex(OSError,'元flush不明'):self.base.finish()
        fsync.assert_not_called()
        self.case.capture()
        self.frame,self.expected=self.case.frame,self.case.expected
        self.assertEqual(self.expected,self.path.read_bytes())
        self.assertTrue(self.frame[2].closed)
        self.assertFalse(self.frame[1].closed)
        self.assertNotIn(self.key,self.offline.close_recoveries)
        self.legacy.pending()

    def test_original_close_flush_proof_uses_distinct_sync_without_rewrite_or_reclose(self):
        self.case.test_fsync_unknown_uses_distinct_sync_without_rewrite_or_reclose()
        self.assertEqual([OSError],self.exits)

    def refused(self):
        self.case.capture()
        self.frame,self.expected=self.case.frame,self.case.expected
        self.assertTrue(self.frame[2].closed)
        self.assertEqual(self.expected,self.path.read_bytes())
        with self.legacy.case.no_terminal_write(), \
             patch.object(offline_control.os,'fsync',side_effect=AssertionError('肯定close証拠なしの同期禁止')):
            with self.assertRaises(RegistryError):self.legacy.recover(deadline=30)
        self.assertEqual(self.initial,self.c.record_number)
        self.legacy.pending()

    def test_full_write_exception_and_normal_close_are_not_adopted(self):
        original=Path.open
        class File(fsync_case._File):
            def write(file,data):
                file.file.write(data)
                raise OSError('元write不明')
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return File(file,None) if path==self.path and args==('x+b',) else file
        with patch.object(Path,'open',opened):
            with self.assertRaisesRegex(OSError,'元write不明'):self.base.finish()
        self.refused()

    def test_already_closed_stream_is_not_a_new_positive_close(self):
        original=Path.open
        class File(fsync_case._File):
            def flush(file):
                file.file.close()
                raise OSError('元flush不明')
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return File(file,None) if path==self.path and args==('x+b',) else file
        with patch.object(Path,'open',opened):
            with self.assertRaisesRegex(OSError,'元flush不明'):self.base.finish()
        self.refused()

    def test_close_exception_after_flush_failure_keeps_full_bytes_unknown(self):
        original=Path.open
        class File(fsync_case._File):
            def flush(file):raise OSError('元flush不明')
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return File(file,'close') if path==self.path and args==('x+b',) else file
        with patch.object(Path,'open',opened):
            with self.assertRaisesRegex(OSError,'close不明'):self.base.finish()
        self.refused()

    def test_missing_close_notification_keeps_full_bytes_unknown(self):
        record=self.c._record
        def unobserved(*,observe=None,retain=None):
            def retained(handle,stage):
                if stage!='closed':retain(handle,stage)
            return record(observe=observe,retain=retained)
        with patch.object(self.c,'_record',side_effect=unobserved):self.fail_flush()
        self.refused()

    def test_deadline_after_native_close_does_not_start_recovery_sync(self):
        original=Path.open
        class File(fsync_case._File):
            def flush(file):raise OSError('元flush不明')
            def __exit__(file,*args):
                result=super().__exit__(*args)
                self.f.clock=20
                return result
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return File(file,None) if path==self.path and args==('x+b',) else file
        with patch.object(Path,'open',opened):
            with self.assertRaises(RegistryError) as expired:self.base.finish()
        self.assertEqual('STOP_TIMEOUT',expired.exception.code)
        self.refused()

    def test_original_writer_owns_close_notification_after_flush_failure(self):
        original=self.c._record
        entered,resume=threading.Event(),threading.Event()
        def record(*,observe=None,retain=None):
            def retained(handle,stage):
                retain(handle,stage)
                if stage=='closed':
                    entered.set()
                    self.assertTrue(resume.wait(30),'元writerを再開できません')
            return original(observe=observe,retain=retained)
        def writer():
            try:return self.fail_flush()
            except (RegistryError,OSError) as error:return error
        with patch.object(self.c,'_record',side_effect=record), \
             patch.object(runtime_control,'time',SimpleNamespace(monotonic=lambda:self.f.clock,sleep=time.sleep)):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(writer)
                try:
                    self.assertTrue(entered.wait(30),'元close正常復帰の通知へ到達しません')
                    self.case.capture()
                    with patch.object(self.case.frame[1],'read',wraps=self.case.frame[1].read) as read:
                        with self.assertRaises(RegistryError) as busy:self.legacy.recover()
                    self.assertEqual('RECOVERY_REQUIRED',busy.exception.code)
                    read.assert_not_called()
                    self.assertFalse(future.done())
                    self.assertEqual(self.initial,self.c.record_number)
                    self.legacy.pending()
                finally:
                    resume.set()
                    result=future.result(40)
        self.assertIsNone(result)
        self.legacy.recover()
        self.assertEqual(self.initial+1,self.c.record_number)
        self.base.completed()

    def test_concurrent_same_id_synchronizes_once(self):
        self.legacy.concurrent_recover('recover-synchronized-close')

    def test_concurrent_other_id_synchronizes_once(self):
        self.legacy.concurrent_recover('recover-synchronized-other')

    def test_epoch_commit_during_observation_prevents_terminal_write(self):
        self.legacy.test_epoch_commit_during_last_permission_observation_prevents_terminal_write()

    def test_public_renew_keeps_both_deadlines(self):
        self.legacy.test_public_renew_does_not_extend_either_deadline()

    def test_wrong_bytes_are_preserved_without_sync(self):
        self.legacy.test_full_different_bytes_are_preserved_without_sync()

    def test_partial_bytes_are_preserved_without_sync(self):
        self.legacy.test_partial_prefix_is_preserved_without_sync_or_terminal_dml()

    def test_new_sync_unknown_is_not_retried(self):
        self.legacy.test_new_sync_failure_is_unknown_and_never_retried()

    def test_read_deadline_starts_no_new_flush(self):
        self.legacy.test_deadline_after_read_starts_no_flush()

    def test_permission_failure_starts_no_sync(self):
        self.legacy.test_permission_observation_is_required_before_sync()
