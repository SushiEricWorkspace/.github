"""実write完了通知と元準備packetからの明示close復旧を検証する。"""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import offline_control, offline_process, runtime_control
from . import test_offline_close_checkpoint as checkpoint
from .build_inputs import canonical, digest
from .registry import RegistryError


class _File:
    def __init__(self, file, stage):
        self.file,self.stage=file,stage

    @property
    def closed(self):return self.file.closed

    def __enter__(self):
        self.file.__enter__()
        return self

    def __exit__(self,*args):
        result=self.file.__exit__(*args)
        if self.stage=='close':raise OSError('close不明')
        return result

    def write(self,data):
        if self.stage=='write':
            self.file.write(data[:3])
            raise OSError('部分write')
        return self.file.write(data)

    def flush(self):
        self.file.flush()
        if self.stage=='flush':raise OSError('flush不明')

    def fileno(self):
        return self.file.fileno()


class WriteObservationTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'record.json'
        self.private=self.enterContext(patch.object(runtime_control,'make_private'))

    def test_default_return_and_original_bytes_are_preserved(self):
        self.assertIsNone(runtime_control.write_json(self.path,{'original':'value'}))
        self.assertEqual(canonical({'original':'value'}),self.path.read_bytes())
        self.private.assert_called_once_with(self.path)

    def test_observer_uses_actual_written_bytes_after_close_and_private(self):
        value={'original':'value'}
        original=Path.open
        files=[]
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            if path==self.path:files.append(file)
            return file
        observed=[]
        def private(path):value['original']='later'
        def observe(data):
            self.assertTrue(files[0].closed)
            self.assertEqual(self.path.read_bytes(),data)
            observed.append(data)
        self.private.side_effect=private
        with patch.object(Path,'open',opened):
            self.assertIsNone(runtime_control.write_json(self.path,value,observe=observe))
        self.assertEqual([canonical({'original':'value'})],observed)

    def write_failure(self,stage):
        original=Path.open
        observed=[]
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return _File(file,stage) if path==self.path and args==('xb',) else file
        if stage=='private':self.private.side_effect=OSError('権限設定不明')
        fsync=(patch.object(runtime_control.os,'fsync',side_effect=OSError('fsync不明'))
               if stage=='fsync' else patch.object(runtime_control,'make_private',self.private))
        with patch.object(Path,'open',opened),fsync:
            with self.assertRaises(OSError):runtime_control.write_json(self.path,{'original':'value'},observe=observed.append)
        self.assertEqual([],observed)
        self.assertTrue(self.path.exists())

    def test_partial_write_has_no_observation(self):self.write_failure('write')
    def test_flush_exception_has_no_observation(self):self.write_failure('flush')
    def test_fsync_exception_has_no_observation(self):self.write_failure('fsync')
    def test_close_exception_has_no_observation(self):self.write_failure('close')
    def test_private_exception_has_no_observation(self):self.write_failure('private')

    def test_observer_exception_does_not_retry_write(self):
        with self.assertRaises(OSError):
            runtime_control.write_json(self.path,{'original':'value'},observe=lambda data:(_ for _ in ()).throw(OSError('引渡し不明')))
        self.assertEqual(canonical({'original':'value'}),self.path.read_bytes())
        with self.assertRaises(FileExistsError):runtime_control.write_json(self.path,{'original':'value'})


class RecordedCloseTests(unittest.TestCase):
    def setUp(self):
        self.case=checkpoint.CloseCheckpointTests()
        self.addCleanup(lambda:self.assertTrue(self.case.doCleanups()))
        self.case.setUp()
        self.runtime,self.offline,self.c,self.f=self.case.runtime,self.case.offline,self.case.c,self.case.f
        self.token,self.key=self.case.token,self.case.key
        self.records=[]
        def write(path,value,*,observe=None,retain=None):
            self.records.append(path)
            runtime_control.write_json(path,value,observe=observe,retain=retain)
        self.enterContext(patch.object(offline_process,'write_json',side_effect=write))
        self.enterContext(patch.object(runtime_control,'make_private'))
        def close_pins():
            for frame in self.offline.close_synchronizations.values():
                if frame[1] is not None and not frame[1].closed:
                    frame[1].close()
        self.addCleanup(close_pins)

    def recover(self,**changes):
        args=dict(operation_id='recover-recorded-close',open_operation_id=self.key[1],close_operation_id=self.f.close_operation,
            termination_sha256=digest(self.c.termination_response),deadline=20)
        args.update(changes)
        return self.runtime._offline_recover_recorded_close(self.token,**args)

    def pending(self):
        self.assertEqual((('LEASED_STOPPED',1),'PENDING',1),self.case.base.state())
        self.assertIs(self.c,self.offline.controllers[self.token['instanceId']])
        for actor,process,pipe,resources in self.case.base.resources:
            self.assertIs(process,actor.process);self.assertIs(pipe,actor.pipe);self.assertIs(resources,actor.creation_resources)

    @contextmanager
    def no_terminal_write(self):
        original=self.runtime._transaction
        updates=[]
        @contextmanager
        def traced(instance,**options):
            with original(instance,**options) as db:
                db.set_trace_callback(lambda sql:updates.append(sql) if sql.startswith((
                    "UPDATE runtime_operations SET status='COMPLETED'",'DELETE FROM reservations',
                    'UPDATE instances SET pending=0','INSERT INTO runtime_operations')) else None)
                yield db
        with patch.object(self.runtime,'_transaction',side_effect=traced):yield
        self.assertEqual([],updates)

    def missing_handoff(self):
        class FailOnce(dict):
            failed=False
            def __setitem__(mapping,key,value):
                if not mapping.failed:
                    mapping.failed=True
                    raise OSError('packet引渡し失敗')
                return super().__setitem__(key,value)
        self.offline.close_recoveries=FailOnce()
        with self.assertRaises(OSError):self.case.base.finish()
        self.assertNotIn(self.key,self.offline.close_recoveries)
        self.assertIn(self.key,self.offline.close_preparations)
        self.assertEqual(canonical(self.c.record),self.case.path.read_bytes())
        self.pending()
        return self.offline.close_preparations[self.key]

    def test_actual_write_completed_but_packet_handoff_failure_can_be_recovered(self):
        preparation=self.missing_handoff()
        self.enterContext(patch.object(self.c,'_record',side_effect=AssertionError('記録再実行禁止')))
        result=self.recover()
        self.assertEqual('OFFLINE_CLOSE_TERMINATED',result['status'])
        self.assertIs(preparation[0],self.offline.close_recoveries[self.key])
        self.assertEqual(1,len(self.records))
        self.case.base.completed()
        self.assertEqual(result,self.recover())

    def test_record_outer_exception_preserves_completed_write_without_normal_return(self):
        original=self.c._record
        def failed(**options):original(**options);raise OSError('外側復帰不明')
        with patch.object(self.c,'_record',side_effect=failed):
            with self.assertRaises(OSError):self.case.base.finish()
        self.pending()
        self.recover();self.case.base.completed()
        self.assertEqual(1,len(self.records))

    def test_fsync_error_and_matching_file_or_counter_do_not_prove_completion(self):
        with patch.object(runtime_control.os,'fsync',side_effect=OSError('fsync不明')):
            with self.assertRaises(OSError):self.case.base.finish()
        self.assertEqual(canonical(self.c.record),self.case.path.read_bytes())
        self.c.record_number+=1
        with self.assertRaises(RegistryError) as caught:self.recover()
        self.assertEqual('RECOVERY_REQUIRED',caught.exception.code)
        self.assertNotIn(self.key,self.offline.close_recoveries)
        self.pending()
        self.assertEqual(1,len(self.records))

    def test_no_write_completion_notification_is_not_recovered(self):
        with patch.object(self.c,'_record',side_effect=OSError('write前拒否')):
            with self.assertRaises(OSError):self.case.base.finish()
        with self.assertRaises(RegistryError):self.recover()
        self.pending()
        self.assertEqual([],self.records)

    def test_changed_preparation_is_not_rebased(self):
        preparation=self.missing_handoff()
        self.offline.close_preparations[self.key]=tuple(list(preparation))
        with self.assertRaises(RegistryError):self.recover()
        self.assertNotIn(self.key,self.offline.close_recoveries)
        self.pending()

    def test_unknown_handle_does_not_use_post_effect_values(self):
        self.missing_handoff()
        self.c.process.handle=9993
        with self.assertRaises(RegistryError):self.recover()
        self.assertEqual(9993,self.c.process.handle)
        self.pending()

    def test_new_parent_has_no_preparation_to_adopt(self):
        self.missing_handoff()
        self.runtime._offline=offline_control._OfflineOpen(self.runtime)
        with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_expired_recovery_starts_no_publication(self):
        self.missing_handoff()
        self.f.clock=20
        with self.assertRaises(RegistryError):self.recover()
        self.assertNotIn(self.key,self.offline.close_recoveries)
        self.pending()

    def test_partial_write_is_preserved_without_terminal_dml(self):
        original=Path.open
        def opened(path,*args,**options):
            file=original(path,*args,**options)
            return _File(file,'write') if path==self.case.path and args in (('xb',),('x+b',)) else file
        with self.no_terminal_write(),patch.object(Path,'open',opened):
            with self.assertRaises(OSError):self.case.base.finish()
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual(canonical(self.c.record)[:3],self.case.path.read_bytes())
        self.pending()

    def test_actual_written_bytes_cannot_be_replaced_by_current_record(self):
        original=runtime_control.canonical
        def changed(value):
            raw=original(value)
            return b'{}' if value is self.c.record else raw
        with self.no_terminal_write(),patch.object(runtime_control,'canonical',side_effect=changed):
            with self.assertRaises(RegistryError):self.case.base.finish()
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual(b'{}',self.case.path.read_bytes())
        self.pending()

    def test_epoch_commit_between_preparation_and_terminal_write_refuses_recovery(self):
        self.missing_handoff()
        original=self.offline._terminal_resources
        def observed(*args):
            original(*args)
            self.case.committed()
        with self.no_terminal_write(),patch.object(self.offline,'_terminal_resources',side_effect=observed):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()

    def test_normal_public_renew_keeps_preparation_and_deadline(self):
        preparation=self.missing_handoff()
        original=self.offline._terminal_resources
        def observed(*args):
            original(*args)
            self.case.committed(True)
        with patch.object(self.offline,'_terminal_resources',side_effect=observed):self.recover()
        self.assertIs(preparation,self.offline.close_preparations[self.key])
        self.case.base.completed()

    def test_resource_entry_deadline_starts_no_packet_publication_or_terminal_write(self):
        self.missing_handoff()
        original=self.runtime.registry.resource_use
        @contextmanager
        def expired(*args,**options):
            with original(*args,**options):
                self.f.clock=20
                yield
        with self.no_terminal_write(),patch.object(self.runtime.registry,'resource_use',side_effect=expired):
            with self.assertRaises(RegistryError):self.recover()
        self.assertNotIn(self.key,self.offline.close_recoveries)
        self.pending()

    def test_resource_exit_deadline_starts_no_terminal_write(self):
        self.missing_handoff()
        original=self.runtime.registry.resource_use
        @contextmanager
        def expired(*args,**options):
            with original(*args,**options):yield
            self.f.clock=20
        with self.no_terminal_write(),patch.object(self.runtime.registry,'resource_use',side_effect=expired):
            with self.assertRaises(RegistryError):self.recover()
        self.pending()
