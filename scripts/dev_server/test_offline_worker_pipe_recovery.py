"""元close完了観測と生成時pinから、最初Wの未開始実pipeだけを解放する。"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import copy
import json
import os
import threading
import unittest
from unittest.mock import patch

from . import database_proxy_pipe, offline_control
from . import test_offline_worker_close_completion as completion
from .registry import RegistryError


class WorkerPipeRecoveryTests(unittest.TestCase):
    def setUp(self, history=False):
        self.case=completion.WorkerCloseCompletionTests();self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        if history:
            self.case.base.history();self.case.case.configure()
            for name in ('base','runtime','c','f','token','offline','key','workers','opened'):
                setattr(self.case,name,getattr(self.case.case,name))
        self.base,self.runtime,self.c,self.f,self.token=self.case.base,self.case.runtime,self.case.c,self.case.f,self.case.token
        self.worker=self.case.workers[0]
        self.pipe=database_proxy_pipe.ProxyPipe()
        self.pins=self.pipe.pins
        self.pipe.close_child_ends()
        self.worker.pipe,self.worker.pipe_pins=self.pipe,self.pins
        self.worker.creation_resources=(self.worker.process,self.pipe)
        self.base.originals=[(w.request,w.response,w.exit_response,w.process,w.pipe) for w in self.base.workers]
        self.native_close=os.close
        self.live={value for pin in self.pins if pin.state!='RELEASED' for value in pin.binding[1:]}
        self.effects=[]
        def close(fd):
            self.native_close(fd)
            if fd in self.live:self.effects.append(fd);self.live.remove(fd)
        tracking=patch.object(database_proxy_pipe.os,'close',side_effect=close)
        tracking.start();self.addCleanup(tracking.stop)
        self.addCleanup(self.cleanup_pipe)

    def cleanup_pipe(self):
        for fd in tuple(self.live):
            self.native_close(fd);self.live.remove(fd)

    def prepared(self):
        self.case.failed()
        self.case.observe()
        self.originals=(self.worker.request,self.worker.response,self.worker.exit_response,self.f.saved())

    def recover(self,**changes):
        values=dict(operation_id='recover-worker-pipe',open_operation_id=self.case.opened,
            release_operation_id=self.f.close_operation,observation_operation_id='observe-close-completion',deadline=20)
        values.update(changes)
        return self.runtime._offline_recover_worker_pipe(self.token,**values)

    def retained(self):
        self.case.retained()
        self.assertEqual(self.originals,(self.worker.request,self.worker.response,self.worker.exit_response,self.f.saved()))
        self.assertIs(self.pipe,self.worker.pipe)
        self.assertIs(self.pins,self.worker.pipe_pins)
        self.assertEqual([901,902],self.case.effects)

    def status(self):
        with self.runtime.registry.transaction(read_only=True) as db:
            row=db.execute("SELECT status FROM runtime_operations WHERE command='offline-recover-worker-pipe'").fetchone()
        return row[0] if row else None

    def test_original_pipe_releases_without_process_reclose_or_original_termination(self):
        self.prepared()
        for name in ('close','poll','members'):
            self.enterContext(patch.object(self.worker.process,name,side_effect=AssertionError('旧process再作用')))
        result=self.recover()
        self.assertEqual('OFFLINE_WORKER_PIPE_RELEASED',result['status'])
        self.assertIs(True,result['pipeReleased']);self.assertIs(False,result['terminatesOriginal'])
        self.assertEqual('COMPLETED',self.status())
        self.assertEqual([fd for pin in self.pins[2:] for fd in pin.binding[1:]],self.effects)
        self.assertTrue(all(pin.state=='RELEASED' for pin in self.pins))
        self.retained()
        for operation in ('recover-worker-pipe','other-pipe-recovery'):
            with self.assertRaises(RegistryError):self.recover(operation_id=operation)
        self.assertEqual(4,len(self.effects))

    def test_pin_copy_group_is_refused_before_native_or_intent(self):
        self.prepared();self.pipe._pins=tuple(copy(pin) for pin in self.pins)
        with self.assertRaises(RegistryError):self.recover()
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained()

    def test_historical_first_worker_does_not_release_latest_worker(self):
        fixture=WorkerPipeRecoveryTests();fixture.setUp(history=True);self.addCleanup(fixture.doCleanups)
        self.assertEqual(2,len(fixture.case.workers))
        fixture.prepared()
        latest=fixture.case.workers[1]
        fds=latest.pipe.input,latest.pipe.output
        with patch.object(latest.process,'close',side_effect=AssertionError('別Wのclose')), \
             patch.object(latest.pipe,'close_after_exit',side_effect=AssertionError('別Wのpipe解放')):
            fixture.recover()
        self.assertEqual(fds,(latest.pipe.input,latest.pipe.output));fixture.retained()

    def test_pinless_original_is_not_adopted_from_current_group(self):
        self.worker.pipe_pins=None
        self.prepared()
        with self.assertRaises(RegistryError):self.recover()
        self.assertEqual([],self.effects);self.assertIsNone(self.status())

    def test_missing_committed_observation_prevents_pipe_action(self):
        self.case.failed()
        with self.assertRaises(RegistryError):self.recover()
        self.assertEqual([],self.effects);self.assertIsNone(self.status())

    def rejected(self):
        with self.assertRaises((RegistryError,OSError)):self.recover()
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained()

    def test_other_process_completion_cannot_replace_source(self):
        self.prepared()
        self.case.offline.release_completions[self.case.key]=tuple(list(self.case.offline.release_completions[self.case.key]))
        self.rejected()

    def test_source_packet_copy_is_refused(self):
        self.prepared();self.case.offline.release_failures[self.case.key]=tuple(list(self.case.offline.release_failures[self.case.key]))
        self.rejected()

    def test_pipe_frame_copy_is_refused_before_action(self):
        self.prepared();source=self.case.offline.release_pipes[self.case.key]
        self.case.offline.release_pipes[self.case.key]=tuple(list(source))
        self.rejected()

    def test_creation_pin_reference_copy_is_refused(self):
        self.prepared();self.worker.pipe_pins=tuple(list(self.pins))
        with self.assertRaises(RegistryError):self.recover()
        self.assertEqual([],self.effects);self.assertIsNone(self.status())

    def test_changed_pin_binding_prevents_action(self):
        self.prepared();self.pins[2]._binding=tuple(list(self.pins[2].binding))
        self.rejected()

    def test_same_fd_different_pipe_is_refused(self):
        self.prepared()
        other=database_proxy_pipe.ProxyPipe();self.addCleanup(other.close_after_exit)
        os.dup2(other.input,self.pipe.input)
        self.rejected()

    def test_other_observation_id_is_not_inferred(self):
        self.prepared()
        with self.assertRaises(RegistryError):self.recover(observation_operation_id='other-observation')
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained()

    def test_completed_observation_body_mismatch_prevents_action(self):
        self.prepared()
        with self.runtime.registry.transaction() as db:
            db.execute("UPDATE runtime_operations SET response=response||' ' WHERE id='observe-close-completion'")
        self.rejected()

    def test_existing_operation_id_prevents_action(self):
        self.prepared()
        with self.runtime.registry.transaction() as db:
            db.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",('recover-worker-pipe','wrong','other',self.token['instanceId'],'COMPLETED','{}'))
        self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained()

    def test_epoch_commit_after_owner_observation_starts_no_close(self):
        self.prepared();original=self.c._verify_close_binding
        def observed(deadline):original(deadline);self.case.case.commit(False)
        with patch.object(self.c,'_verify_close_binding',side_effect=observed):self.rejected()

    def test_public_renew_keeps_fixed_deadline_and_source(self):
        self.prepared();original=self.c._verify_close_binding;done=False
        def observed(deadline):
            nonlocal done
            original(deadline)
            if not done:self.case.case.commit(True);done=True
        with patch.object(self.c,'_verify_close_binding',side_effect=observed):self.recover()
        self.assertEqual('COMPLETED',self.status());self.retained()

    def test_expired_entry_starts_no_owner_observation(self):
        self.prepared();self.f.clock=20
        with patch.object(self.c,'_verify_close_binding',side_effect=AssertionError('期限後の観測')):
            self.rejected()

    def test_resource_entry_expiry_starts_no_owner_observation(self):
        self.prepared();original=self.runtime.registry.resource_use
        @contextmanager
        def late():
            with original():self.f.clock=20;yield
        with patch.object(self.runtime.registry,'resource_use',side_effect=late), \
             patch.object(self.c,'_verify_close_binding',side_effect=AssertionError('期限後の観測')):
            self.rejected()

    def test_same_pipe_observation_expiry_starts_no_fd_close(self):
        self.prepared();original=database_proxy_pipe._same_pipe;calls=[]
        def expired(fd,pin):
            calls.append(fd)
            value=original(fd,pin);self.f.clock=20;return value
        with patch.object(database_proxy_pipe,'_same_pipe',side_effect=expired):self.rejected()
        self.assertEqual([self.pins[2].fd],calls)

    def test_owner_observation_expiry_starts_no_transaction(self):
        self.prepared();original=self.c._verify_close_binding;transactions=[]
        def expired(deadline):original(deadline);self.f.clock=20
        def transaction(*args,**kwargs):
            transactions.append(args)
            raise AssertionError('期限後のtransaction開始')
        with patch.object(self.c,'_verify_close_binding',side_effect=expired), \
             patch.object(self.runtime,'_transaction',side_effect=transaction):
            self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],transactions);self.assertEqual([],self.effects);self.retained()

    def test_ownership_observation_failure_consumes_single_attempt(self):
        self.prepared()
        with patch.object(database_proxy_pipe,'_same_pipe',side_effect=OSError('実体観測不明')):
            self.assertRaises(OSError,self.recover)
        with patch.object(database_proxy_pipe,'_same_pipe',side_effect=AssertionError('実体の再観測')):
            self.assertRaises(RegistryError,self.recover)
        self.assertEqual([],self.effects);self.assertIsNone(self.status());self.retained()

    def test_deadline_after_original_fd_keeps_pin_without_retry(self):
        self.prepared();original=os.close;first=self.pins[2].fd
        def close(fd):
            original(fd)
            if fd==first:self.f.clock=20
        with patch.object(database_proxy_pipe.os,'close',side_effect=close):
            with self.assertRaises(RegistryError):self.recover()
        self.assertEqual([first],self.effects)
        self.assertEqual('PIN_PENDING',self.pins[2].state);self.assertIsNone(self.pipe.input)
        self.assertIsNone(self.status());self.retained()
        self.f.clock=0
        self.assertRaises(RegistryError,self.recover)
        self.assertEqual([first],self.effects)

    def close_failure(self,after):
        self.prepared();first=self.pins[2].fd;original=os.close
        def close(fd):
            if fd==first:
                if after:original(fd)
                raise OSError('元端点close不明')
            original(fd)
        with patch.object(database_proxy_pipe.os,'close',side_effect=close):
            self.assertRaises(OSError,self.recover)
        self.assertEqual([first] if after else [],self.effects)
        self.assertEqual('UNKNOWN',self.pins[2].state)
        self.assertIsNone(self.status());self.retained()
        self.assertRaises(RegistryError,self.recover)
        self.assertEqual([first] if after else [],self.effects)

    def test_close_exception_preserves_unknown_without_second_action(self):self.close_failure(False)
    def test_close_return_loss_preserves_unknown_without_second_action(self):self.close_failure(True)

    def test_final_copy_mutation_refuses_normal_return_after_commit(self):
        self.prepared();original=offline_control.deepcopy
        def changed(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_WORKER_PIPE_RELEASED':result['pipeReleased']=False
            return result
        with patch.object(offline_control,'deepcopy',side_effect=changed):self.assertRaises(RegistryError,self.recover)
        self.assertEqual('COMPLETED',self.status());self.retained()

    def test_write_inner_binding_change_rolls_back_completion(self):
        self.prepared()
        def change(sql):
            if sql.startswith('INSERT INTO runtime_operations'):self.worker.nonce='changed'
        with self.case.case.trace(change):self.assertRaises(RegistryError,self.recover)
        self.assertEqual(4,len(self.effects));self.assertIsNone(self.status());self.retained()

    def test_resource_exit_deadline_preserves_completed_resources(self):
        self.prepared();original=self.runtime.registry.resource_use
        @contextmanager
        def late():
            with original():yield
            self.f.clock=20
        with patch.object(self.runtime.registry,'resource_use',side_effect=late):self.assertRaises(RegistryError,self.recover)
        self.assertEqual('COMPLETED',self.status());self.retained()

    def race(self,same):
        self.prepared();entered=threading.Event();resume=threading.Event();original=os.close;first=self.pins[2].fd
        def close(fd):
            if fd==first:
                entered.set()
                if not resume.wait(10):raise AssertionError('競合試験の再開待ち')
            original(fd)
        with patch.object(database_proxy_pipe.os,'close',side_effect=close),ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(self.recover)
            self.assertTrue(entered.wait(10))
            two=pool.submit(self.recover,operation_id='recover-worker-pipe' if same else 'another-pipe-recovery')
            self.case.case.commit(True)
            resume.set()
            self.assertEqual('OFFLINE_WORKER_PIPE_RELEASED',one.result(10)['status'])
            with self.assertRaises(RegistryError):two.result(10)
        self.assertEqual(4,len(self.effects));self.assertEqual('COMPLETED',self.status());self.retained()

    def test_concurrent_same_id_has_one_pipe_release(self):self.race(True)
    def test_concurrent_other_id_has_one_pipe_release(self):self.race(False)
