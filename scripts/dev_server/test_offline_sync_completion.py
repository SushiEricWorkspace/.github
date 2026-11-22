"""新同期の正常完了と後続照合の失敗を分け、再同期せず終端する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import local_ipc, offline_control, runtime_control
from . import test_offline_record_fsync_recovery as fsync_case
from .registry import RegistryError


class SyncCompletionTests(unittest.TestCase):
    def setUp(self):
        self.source = fsync_case.FsyncRecoveryTests()
        self.addCleanup(lambda: self.assertTrue(self.source.doCleanups()))
        self.source.setUp()
        self.source.fail_fsync()
        self.case = self.source.case
        self.runtime, self.offline, self.c, self.f = self.case.runtime, self.case.offline, self.case.c, self.case.f
        self.frame, self.path, self.key = self.case.frame, self.case.path, self.case.key
        self.pin, self.expected, self.initial = self.frame[1], self.case.expected, self.c.record_number

    def completed_failure(self):
        sync = offline_control.os.fsync
        completed = []

        def synced(fd):
            sync(fd)
            completed.append(fd)

        def private(path, **options):
            local_ipc.require_private(path, **options)
            if completed:
                raise OSError('新同期正常復帰後の権限観測失敗')

        with self.case.case.no_terminal_write(), patch.object(offline_control.os, 'fsync', side_effect=synced), \
                patch.object(offline_control, 'require_private', side_effect=private):
            with self.assertRaisesRegex(OSError, '新同期正常復帰後'):
                self.case.recover()
        self.assertEqual([self.frame[7]], completed)
        self.pending()

    def pending(self):
        self.case.pending()
        self.assertIs(self.frame, self.offline.close_synchronizations[self.key])
        self.assertIn(self.key, self.offline.close_recoveries)
        self.assertIs(self.frame[0], self.offline.close_recoveries[self.key])
        self.assertEqual(self.initial + 1, self.c.record_number)
        self.assertTrue(self.frame[2].closed)
        self.assertFalse(self.pin.closed)

    def resume(self, **changes):
        with patch.object(self.pin, 'flush', side_effect=AssertionError('新flushの再実行禁止')), \
                patch.object(offline_control.os, 'fsync', side_effect=AssertionError('新fsyncの再実行禁止')), \
                patch.object(self.c, '_record', side_effect=AssertionError('元writeの再実行禁止')):
            return self.case.recover(**changes)

    def test_completed_sync_survives_following_observation_failure(self):
        self.completed_failure()
        for _, process, pipe, _ in self.source.base.resources:
            for name in ('poll', 'members', 'close'):
                self.enterContext(patch.object(process, name, side_effect=AssertionError('元資源の再作用禁止')))
            self.enterContext(patch.object(pipe, 'close_after_exit', side_effect=AssertionError('元pipeの再解放禁止')))
        result = self.resume()
        self.assertEqual('OFFLINE_CLOSE_SYNCHRONIZED', result['status'])
        self.assertFalse(result['recordSynchronization']['originalWriteCompleted'])
        self.assertTrue(result['recordSynchronization']['synchronizationCompleted'])
        self.assertEqual(result, self.resume())
        self.assertEqual(self.initial + 1, self.c.record_number)
        self.source.base.completed()

    def test_other_id_cannot_adopt_completed_sync(self):
        self.completed_failure()
        with self.case.case.no_terminal_write(), self.assertRaises(RegistryError) as denied:
            self.resume(operation_id='different-synchronization')
        self.assertEqual('OPERATION_CONFLICT', denied.exception.code)
        self.pending()
        self.resume()
        self.source.base.completed()

    def test_sync_completion_does_not_overwrite_changed_packet(self):
        sync = offline_control.os.fsync
        replacement = tuple(list(self.frame[0]))

        def changed(fd):
            sync(fd)
            self.offline.close_recoveries[self.key] = replacement

        with self.case.case.no_terminal_write(), patch.object(offline_control.os, 'fsync', side_effect=changed):
            with self.assertRaises(RegistryError):
                self.case.recover()
        self.assertIs(replacement, self.offline.close_recoveries[self.key])
        self.assertEqual(self.initial, self.c.record_number)
        with self.case.case.no_terminal_write(), self.assertRaises(RegistryError):
            self.resume()
        self.case.pending()
        self.assertFalse(self.pin.closed)

    def test_fsync_deadline_keeps_completion_without_next_observation(self):
        sync = offline_control.os.fsync
        late = []

        def synced(fd):
            sync(fd)
            self.f.clock = 20

        def private(path, **options):
            if self.f.clock >= 20:
                late.append(True)
            local_ipc.require_private(path, **options)

        with self.case.case.no_terminal_write(), patch.object(offline_control.os, 'fsync', side_effect=synced), \
                patch.object(offline_control, 'require_private', side_effect=private):
            with self.assertRaises(RegistryError) as expired:
                self.case.recover()
        self.assertEqual('STOP_TIMEOUT', expired.exception.code)
        self.assertEqual([], late)
        self.pending()
        self.resume(deadline=30)
        self.source.base.completed()

    def test_evidence_serialization_deadline_starts_no_flush(self):
        canonical = offline_control.canonical

        def encoded(value):
            raw = canonical(value)
            if isinstance(value, dict) and value.get('synchronizationCompleted') is True:
                self.f.clock = 20
            return raw

        with self.case.case.no_terminal_write(), patch.object(offline_control, 'canonical', side_effect=encoded), \
                patch.object(self.pin, 'flush', side_effect=AssertionError('期限後の新flush禁止')):
            with self.assertRaises(RegistryError):
                self.case.recover()
        self.assertEqual(self.initial, self.c.record_number)
        self.case.pending()

    def test_actual_fsync_then_exception_stays_unknown(self):
        sync = offline_control.os.fsync

        def unknown(fd):
            sync(fd)
            raise OSError('新fsync完了通知なし')

        with self.case.case.no_terminal_write(), patch.object(offline_control.os, 'fsync', side_effect=unknown) as observed:
            with self.assertRaises(OSError):
                self.case.recover()
            with self.assertRaises(RegistryError):
                self.resume()
        observed.assert_called_once_with(self.frame[7])
        self.assertEqual(self.initial, self.c.record_number)
        self.case.pending()

    def test_flush_exception_stays_unknown(self):
        with self.case.case.no_terminal_write(), patch.object(self.pin, 'flush', side_effect=OSError('新flush不明')) as observed:
            with self.assertRaises(OSError):
                self.case.recover()
            with self.assertRaises(RegistryError):
                self.resume()
        observed.assert_called_once()
        self.assertEqual(self.initial, self.c.record_number)
        self.case.pending()

    def test_mac_full_sync_exception_stays_unknown(self):
        full = Mock(side_effect=OSError('完全同期不明'))
        with self.case.case.no_terminal_write(), patch.object(offline_control, 'sys', SimpleNamespace(platform='darwin')), \
                patch.dict(sys.modules, {'fcntl': SimpleNamespace(fcntl=full, F_FULLFSYNC=51)}):
            with self.assertRaises(OSError):
                self.case.recover()
            with self.assertRaises(RegistryError):
                self.resume()
        full.assert_called_once_with(self.frame[7], 51)
        self.assertEqual(self.initial, self.c.record_number)
        self.case.pending()

    def test_mac_full_sync_completion_is_not_repeated(self):
        full = Mock()
        with patch.object(offline_control, 'sys', SimpleNamespace(platform='darwin')), \
                patch.dict(sys.modules, {'fcntl': SimpleNamespace(fcntl=full, F_FULLFSYNC=51)}):
            self.completed_failure()
            full.assert_called_once_with(self.frame[7], 51)
            full.side_effect = AssertionError('完全同期の再実行禁止')
            self.resume()
        self.source.base.completed()

    def test_cached_sync_rechecks_actual_bytes_before_terminal_dml(self):
        self.completed_failure()
        self.path.write_bytes(b'{}')
        with self.case.case.no_terminal_write(), self.assertRaises(RegistryError):
            self.resume()
        self.assertEqual(b'{}', self.path.read_bytes())
        self.case.case.pending()
        self.assertFalse(self.pin.closed)

    def test_cached_sync_keeps_original_handle_binding(self):
        self.completed_failure()
        self.c.process.handle = 9993
        with self.case.case.no_terminal_write(), self.assertRaises(RegistryError):
            self.resume()
        self.assertEqual(9993, self.c.process.handle)
        self.pending()

    def test_cached_sync_keeps_original_native_reference(self):
        self.completed_failure()
        self.c.native = object()
        with self.case.case.no_terminal_write(), self.assertRaises(RegistryError):
            self.resume()
        self.pending()

    def test_cached_sync_keeps_original_frame(self):
        self.completed_failure()
        self.offline.close_synchronizations[self.key] = tuple(list(self.frame))
        with self.case.case.no_terminal_write(), self.assertRaises(RegistryError):
            self.resume()
        self.case.case.pending()
        self.assertFalse(self.pin.closed)

    def test_cached_sync_rejects_other_file_identity(self):
        self.completed_failure()
        lstat = Path.lstat

        def changed(path, *args, **options):
            value = lstat(path, *args, **options)
            if path == self.path:
                return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino + 1, st_mode=value.st_mode,
                                       st_nlink=value.st_nlink, st_file_attributes=0)
            return value

        with self.case.case.no_terminal_write(), patch.object(Path, 'lstat', changed):
            with self.assertRaises(RegistryError):
                self.resume()
        self.pending()

    def test_cached_sync_rejects_changed_permission(self):
        self.completed_failure()
        with self.case.case.no_terminal_write(), patch.object(offline_control, 'require_private', side_effect=PermissionError('権限変更')):
            with self.assertRaises(PermissionError):
                self.resume()
        self.pending()

    def test_epoch_commit_during_cached_read_prevents_terminal_dml(self):
        self.completed_failure()
        committed = []

        def private(path, **options):
            local_ipc.require_private(path, **options)
            if not committed:
                committed.append(True)
                self.case.case.case.committed()

        with self.case.case.no_terminal_write(), patch.object(offline_control, 'require_private', side_effect=private):
            with self.assertRaises(RegistryError):
                self.resume()
        self.assertEqual([True], committed)
        self.pending()

    def test_public_renew_during_cached_read_keeps_both_deadlines(self):
        self.completed_failure()
        original_deadline = self.c.close_binding['deadline']
        committed = []

        def private(path, **options):
            local_ipc.require_private(path, **options)
            if not committed:
                committed.append(True)
                self.case.case.case.committed(True)

        with patch.object(offline_control, 'require_private', side_effect=private):
            self.resume()
        self.assertEqual(original_deadline, self.c.close_binding['deadline'])
        self.assertEqual(self.initial + 1, self.c.record_number)
        self.source.base.completed()

    def test_cached_read_deadline_starts_no_terminal_dml(self):
        self.completed_failure()
        read = self.pin.read

        def delayed(*args):
            data = read(*args)
            self.f.clock = 20
            return data

        with self.case.case.no_terminal_write(), patch.object(self.pin, 'read', side_effect=delayed):
            with self.assertRaises(RegistryError):
                self.resume()
        self.pending()

    def test_cached_final_file_bundle_follows_all_registry_observations(self):
        self.completed_failure()
        transaction, read = self.runtime._transaction, self.pin.read
        observations, reads = [], []

        @contextmanager
        def tracked(*args, **options):
            with transaction(*args, **options) as db:
                yield db
            observations.append(True)

        def observed(*args):
            reads.append(len(observations))
            return read(*args)

        with patch.object(self.runtime, '_transaction', side_effect=tracked), patch.object(self.pin, 'read', side_effect=observed):
            self.resume()
        self.assertEqual(len(observations), reads[-1])
        self.assertGreater(reads[-1], reads[0])
        self.source.base.completed()

    def concurrent_resume(self, second_id):
        self.completed_failure()
        entered, release = threading.Event(), threading.Event()
        read = self.pin.read
        readers = []

        def paused(*args):
            readers.append(threading.get_ident())
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(30), '照合を再開できません')
            return read(*args)

        with patch.object(self.pin, 'read', side_effect=paused):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.resume)
                try:
                    self.assertTrue(entered.wait(30), '保持pinの照合へ到達しません')
                    with self.assertRaises(RegistryError) as busy:
                        self.case.recover(operation_id=second_id)
                    self.assertEqual('RECOVERY_REQUIRED', busy.exception.code)
                    self.assertFalse(future.done())
                finally:
                    release.set()
                result = future.result(40)
        self.assertEqual(1, len(set(readers)))
        self.assertEqual('OFFLINE_CLOSE_SYNCHRONIZED', result['status'])
        self.assertEqual(self.initial + 1, self.c.record_number)
        self.source.base.completed()

    def test_concurrent_same_id_resumes_without_new_sync(self):
        self.concurrent_resume('recover-synchronized-close')

    def test_concurrent_other_id_cannot_enter_cached_pin_read(self):
        self.concurrent_resume('recover-synchronized-other')
