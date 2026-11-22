"""元closeの正常復帰観測だけを、UNKNOWNを変えず実Registryへ結ぶ。"""
from contextlib import contextmanager
from copy import copy
import importlib.util
import json
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import offline_control, runtime_process
from . import test_offline_release_observation as observation
from .registry import RegistryError


class CloseAdapterTests(unittest.TestCase):
    def test_windows_completion_follows_both_original_closes(self):
        actor=SimpleNamespace(handle=901,job=902)
        calls=[]
        with patch.object(runtime_process,'_winapi',SimpleNamespace(CloseHandle=lambda h:calls.append(h))), \
             patch.object(runtime_process,'kernel',SimpleNamespace(CloseHandle=lambda h:calls.append(h) or True)):
            runtime_process.OwnedProcess.close(actor,verify=lambda:calls.append('verify'),observe=lambda:calls.append('observed'))
        self.assertEqual(['verify',901,'verify',902,'observed'],calls)

    def test_each_native_failure_has_no_completion(self):
        for first in (True,False):
            with self.subTest(first=first):
                observe=Mock()
                with patch.object(runtime_process,'_winapi',SimpleNamespace(CloseHandle=Mock(side_effect=OSError('元handle') if first else None))), \
                     patch.object(runtime_process,'kernel',SimpleNamespace(CloseHandle=Mock(side_effect=OSError('元job')))):
                    with self.assertRaises(OSError):
                        runtime_process.OwnedProcess.close(SimpleNamespace(handle=901,job=902),observe=observe)
                observe.assert_not_called()

    def test_second_verification_failure_starts_no_job_close(self):
        verify=Mock(side_effect=[None,OSError('期限又はbinding')]);observe=Mock()
        with patch.object(runtime_process,'_winapi',SimpleNamespace(CloseHandle=Mock())) as native, \
             patch.object(runtime_process,'kernel',SimpleNamespace(CloseHandle=Mock())) as kernel:
            with self.assertRaises(OSError):
                runtime_process.OwnedProcess.close(SimpleNamespace(handle=901,job=902),verify=verify,observe=observe)
        native.CloseHandle.assert_called_once_with(901);kernel.CloseHandle.assert_not_called();observe.assert_not_called()

    def test_default_close_keeps_native_order_and_inputs(self):
        calls=[]
        with patch.object(runtime_process,'_winapi',SimpleNamespace(CloseHandle=lambda h:calls.append(h))), \
             patch.object(runtime_process,'kernel',SimpleNamespace(CloseHandle=lambda h:calls.append(h) or True)):
            result=runtime_process.OwnedProcess.close(SimpleNamespace(handle=901,job=902))
        self.assertIsNone(result);self.assertEqual([901,902],calls)

    def test_mac_original_wait_alone_controls_completion(self):
        spec=importlib.util.spec_from_file_location('dev_server.mac_close_completion',runtime_process.__file__)
        adapter=importlib.util.module_from_spec(spec)
        with patch('os.name','posix'):spec.loader.exec_module(adapter)
        for failure in (False,True):
            with self.subTest(failure=failure):
                wait=Mock(side_effect=OSError('元wait') if failure else None);observe=Mock();verify=Mock()
                actor=SimpleNamespace(process=SimpleNamespace(wait=wait))
                if failure:
                    with self.assertRaises(OSError):adapter.OwnedProcess.close(actor,verify=verify,observe=observe)
                    observe.assert_not_called()
                else:
                    adapter.OwnedProcess.close(actor,verify=verify,observe=observe);observe.assert_called_once_with()
                verify.assert_called_once_with();wait.assert_called_once_with(timeout=0)


class WorkerCloseCompletionTests(unittest.TestCase):
    def setUp(self):
        self.case=observation.ReleaseObservationTests();self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.base,self.runtime,self.c,self.f,self.token=self.case.base,self.case.runtime,self.case.c,self.case.f,self.case.token
        self.offline,self.key,self.workers,self.opened=self.case.offline,self.case.key,self.case.workers,self.case.opened
        self.effects=[]

    def failed(self,*,native_failure=None,observe=True,hook=None):
        process=self.workers[0].process
        process.handle,process.job=901,902
        original=MethodType(runtime_process.OwnedProcess.close,process)
        def lost(**options):
            if not observe:options.pop('observe')
            original(**options)
            raise OSError('元close返却の喪失')
        process.close=Mock(side_effect=lost)
        def close(h):
            self.effects.append(h)
            if hook is not None:hook(h)
            if h==native_failure:raise OSError('元native close例外')
            return True
        with patch.object(runtime_process,'_winapi',SimpleNamespace(CloseHandle=close)), \
             patch.object(runtime_process,'kernel',SimpleNamespace(CloseHandle=close)):
            with self.assertRaises((OSError,RegistryError)):self.base.release()
        self.case.intent=self.f.saved()

    def observe(self,**changes):
        args=dict(operation_id='observe-close-completion',open_operation_id=self.opened,
            release_operation_id=self.f.close_operation,deadline=20)
        args.update(changes)
        return self.runtime._offline_observe_worker_close_completion(self.token,**args)

    def count(self):
        with self.runtime.registry.transaction(read_only=True) as db:
            return db.execute("SELECT COUNT(*) FROM runtime_operations WHERE command='offline-observe-worker-close'").fetchone()[0]

    def retained(self):
        self.case.retained()
        self.assertEqual({'handle':'UNKNOWN','pipe':'NOT_STARTED'},
            self.c.worker_release[json.loads(self.workers[0].request)['operationId']])
        self.assertIsNone(self.c.worker_release_response)

    def reject(self):
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(0,self.count());self.retained()

    def test_original_real_close_completion_is_separate_from_unknown_and_pipe(self):
        self.failed();before=list(self.effects)
        for actor in (self.c,*self.workers):
            for name in ('poll','members','close'):
                self.enterContext(patch.object(actor.process,name,side_effect=AssertionError('元資源への再作用')))
            self.enterContext(patch.object(actor.pipe,'close_after_exit',side_effect=AssertionError('pipe解放')))
        self.enterContext(patch.object(self.c,'_record',side_effect=AssertionError('局所write')))
        result=self.observe()
        self.assertEqual('OFFLINE_WORKER_CLOSE_OBSERVED',result['status'])
        self.assertIs(False,result['terminatesOriginal'])
        self.assertIs(True,result['processRelease']['processResourcesClosed'])
        self.assertIs(False,result['processRelease']['pipeReleased'])
        self.assertEqual([901,902],before);self.assertEqual(before,self.effects)
        self.assertEqual(result,self.observe());self.assertEqual(1,self.count());self.retained();self.base.no_retry()

    def test_first_native_exception_is_not_completion(self):
        self.failed(native_failure=901);self.reject();self.assertEqual([901],self.effects)

    def test_job_exception_is_not_completion(self):
        self.failed(native_failure=902);self.reject();self.assertEqual([901,902],self.effects)

    def test_missing_original_observer_cannot_be_reconstructed(self):
        self.failed(observe=False);self.reject()

    def test_held_mismatch_keeps_precedence_over_missing_completion(self):
        self.failed(observe=False);self.workers[0].pipe.child_input=9993
        with self.assertRaises(RegistryError) as caught:self.observe()
        self.assertEqual('PROOF_MISMATCH',caught.exception.code)
        self.assertEqual(0,self.count());self.retained()

    def test_successful_release_is_not_unknown(self):
        self.base.release()
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(0,self.count())

    def test_completion_packet_reference_is_not_readopted(self):
        self.failed();self.offline.release_completions[self.key]=tuple(list(self.offline.release_completions[self.key]));self.reject()

    def test_source_packet_reference_is_not_readopted(self):
        self.failed();self.offline.release_failures[self.key]=tuple(list(self.offline.release_failures[self.key]));self.reject()

    def test_original_resource_change_during_close_prevents_completion(self):
        self.failed(hook=lambda h:setattr(self.workers[0].process,'handle',9993) if h==902 else None)
        self.reject();self.assertEqual(9993,self.workers[0].process.handle)

    def test_resource_change_after_completion_is_preserved(self):
        self.failed();self.workers[0].pipe.child_input=9993;self.reject()

    def test_native_reference_change_after_completion_is_preserved(self):
        self.failed();self.workers[0].native=copy(self.workers[0].native);self.reject()

    def test_parent_restart_has_no_original_completion(self):
        self.failed();self.runtime._offline=offline_control._OfflineOpen(self.runtime)
        with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(0,self.count());self.retained()

    def test_actual_epoch_commit_after_owner_observation_refuses_new_record(self):
        self.failed();original=self.c._verify_close_binding
        def observed(deadline):original(deadline);self.case.commit(False)
        with patch.object(self.c,'_verify_close_binding',side_effect=observed):self.reject()

    def test_public_renew_after_owner_observation_preserves_completion(self):
        self.failed();original=self.c._verify_close_binding
        def observed(deadline):original(deadline);self.case.commit(True)
        with patch.object(self.c,'_verify_close_binding',side_effect=observed):self.observe()
        self.retained()

    def test_write_inner_resource_change_rolls_back_new_record(self):
        self.failed()
        def change(sql):
            if sql.startswith('INSERT INTO runtime_operations'):self.workers[0].pipe.child_input=9993
        with self.case.trace(change):self.reject()

    def test_return_copy_change_is_refused_after_observation_commit(self):
        self.failed();original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_WORKER_CLOSE_OBSERVED':
                result['processRelease']['pipeReleased']=True
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):
            with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(1,self.count());self.retained()

    def test_resource_exit_deadline_preserves_commit_without_normal_return(self):
        self.failed();original=self.runtime.registry.resource_use
        @contextmanager
        def late(*args,**kwargs):
            with original(*args,**kwargs):yield
            self.f.clock=20
        with patch.object(self.runtime.registry,'resource_use',side_effect=late):
            with self.assertRaises(RegistryError):self.observe()
        self.assertEqual(1,self.count());self.retained()

    def test_original_second_close_deadline_starts_no_job_operation(self):
        self.failed(hook=lambda h:setattr(self.f,'clock',self.f.deadline) if h==901 else None)
        self.assertEqual([901],self.effects)
        self.f.clock=0;self.reject()

    def test_observation_does_not_update_or_delete_original_rows(self):
        self.failed();statements=[]
        with self.case.trace(statements.append):self.observe()
        self.assertFalse(any(s.lstrip().upper().startswith(('UPDATE ','DELETE ')) for s in statements))
        self.retained()
