"""確定した元C終了からの正常解放・部分失敗を実Registryで検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

from . import database_proxy_pipe, offline_control
from . import test_offline_owner_exit as exited
from . import test_game_owner_close_deadline as game_tests
from .build_inputs import canonical, digest
from .registry import RegistryError


class OfflineOwnerReleaseTests(unittest.TestCase):
    def setUp(self):
        self.base=exited.OfflineOwnerExitTests()
        self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.base.observe()
        self.configure(self.base.f)

    def configure(self, fixture):
        self.f=fixture
        self.runtime,self.token,self.c=fixture.runtime,fixture.token,fixture.c
        self.process,self.pipe=self.c.process,self.c.pipe
        self.before=deepcopy(self.f.saved())
        self.originals=self.c.close_request,self.c.close_response,self.c.owner_exit_response
        self.frames=deepcopy(self.pipe.sent)
        self.fds={'input':self.pipe.input,'output':self.pipe.output}
        self.order=[]
        self.handle_failure=self.fd_failure=None
        self.after=False
        self.before_close=self.after_close=lambda:None
        self.process.close=Mock(side_effect=self.close_handle)
        self.enterContext(patch.object(self.pipe,'close_after_exit',side_effect=lambda **kw:
            database_proxy_pipe.ProxyPipe.close_after_exit(self.pipe,**kw)))
        original=os.close
        def close_fd(fd):
            role=next((k for k,v in self.fds.items() if fd==v),None)
            if role is None:return original(fd)
            if self.fd_failure==role and not self.after:raise OSError('fixture fd解放前の失敗')
            self.order.append(role)
            if self.fd_failure==role:raise OSError('fixture fd解放後の不明')
        self.enterContext(patch.object(database_proxy_pipe.os,'close',side_effect=close_fd))

    def close_handle(self):
        self.before_close()
        if self.handle_failure and not self.after:raise OSError('fixture handle解放前の失敗')
        self.order.append('handle')
        self.after_close()
        if self.handle_failure:raise OSError('fixture handle部分解放失敗')

    def release(self, **changes):
        args=dict(operation_id=self.f.close_operation,deadline=self.f.deadline)
        args.update(changes)
        return self.runtime._offline_release_owner_handles(self.token,**args)

    def pending(self):
        self.f.prepared.pending()
        self.assertEqual(self.before,{k:v for k,v in self.f.saved().items() if k!='ownerRelease'})
        self.assertIs(self.c,self.runtime._offline.controllers[self.token['instanceId']])
        self.assertIs(self.process,self.c.process);self.assertIs(self.pipe,self.c.pipe)
        self.assertEqual(self.originals,(self.c.close_request,self.c.close_response,self.c.owner_exit_response))
        self.assertEqual(self.frames,self.pipe.sent)

    def no_retry(self):
        self.f.clock=0
        before=deepcopy(self.f.saved()),list(self.order),self.process.close.call_count
        for op in (self.f.close_operation,'other-release'):
            with self.assertRaises(RegistryError):self.release(operation_id=op)
        with self.assertRaises(RegistryError):self.f.close()
        with self.assertRaises(RegistryError):self.f.prepare_next()
        with self.assertRaises(RegistryError):
            self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='start-after-release')
        self.assertEqual(before,(self.f.saved(),self.order,self.process.close.call_count))

    def rejected(self, change, code=None):
        change()
        with self.assertRaises(RegistryError) as caught:self.release()
        if code:self.assertEqual(code,caught.exception.code)
        self.assertEqual([],self.order);self.assertEqual(0,self.process.close.call_count)
        self.assertNotIn('ownerRelease',self.f.saved())

    def test_normal_release_records_original_proof_and_keeps_reservation(self):
        result=self.release()
        self.assertEqual('OWNER_HANDLES_RELEASED',result['status'])
        self.assertEqual(['handle','input','output'],self.order)
        self.assertEqual({'handle':'RELEASED','pipe':'RELEASED'},self.c.owner_release)
        self.assertEqual((None,None),(self.pipe.input,self.pipe.output))
        self.assertEqual(digest(self.c.owner_exit_response),result['ownerExitSha256'])
        self.assertEqual(canonical(result),self.c.owner_release_response)
        self.assertEqual(result,self.c.record['ownerRelease'])
        self.assertEqual(digest(canonical(result)),self.f.saved()['ownerRelease']['responseSha256'])
        result['handlesReleased']=False
        self.assertTrue(self.c.record['ownerRelease']['handlesReleased'])
        self.pending();self.no_retry()

    def test_exit_is_not_reobserved_or_pid_reattached_after_release(self):
        identity=self.f.p.adapter.identity
        def checked(pid):
            self.assertNotEqual(self.process.pid,pid)
            return identity(pid)
        with patch.object(self.process,'poll',side_effect=AssertionError('C再観測')), \
                patch.object(self.process,'members',side_effect=AssertionError('C group再観測')), \
                patch.object(self.f.p.adapter,'identity',side_effect=checked):self.release()
        self.pending()

    def test_game_source_keeps_close_deadline_and_original_callback(self):
        game=game_tests.GameOwnerCloseDeadlineTests()
        game.setUp();self.addCleanup(game.doCleanups)
        game.open();game.edit(21)
        case=game.close_fixture(clock=31,deadline=40)
        case.close()
        self.runtime=case.runtime
        self.runtime._offline_observe_owner_exit(case.token,operation_id=case.close_operation,deadline=40)
        self.configure(case)
        case.clock,case.deadline=35,50
        original=self.c.verify_owner
        self.release()
        self.assertIs(original,self.c.verify_owner)
        self.pending()

    def test_close_deadline_cannot_be_extended(self):
        self.f.deadline=20
        self.rejected(lambda:setattr(self.f,'clock',10),'STOP_TIMEOUT')

    def test_shorter_deadline_is_not_extended(self):
        self.f.deadline=5
        self.rejected(lambda:setattr(self.f,'clock',5),'STOP_TIMEOUT')

    def failed(self, role, after=False):
        self.after=after
        if role=='handle':self.handle_failure=True
        else:self.fd_failure=role
        with self.assertRaises(OSError):self.release()
        self.assertNotIn('responseSha256',self.f.saved()['ownerRelease'])
        self.assertEqual('UNKNOWN',self.c.owner_release['handle' if role=='handle' else 'pipe'])
        self.assertEqual('NOT_STARTED' if role=='handle' else 'RELEASED',self.c.owner_release['pipe' if role=='handle' else 'handle'])
        self.pending();self.no_retry()

    def test_handle_failure_before_effect_is_unknown(self):self.failed('handle')
    def test_handle_failure_after_effect_is_unknown(self):self.failed('handle',True)
    def test_input_failure_keeps_both_fds(self):self.failed('input')
    def test_input_failure_after_effect_is_not_reclosed(self):self.failed('input',True)
    def test_output_failure_keeps_input_released(self):
        self.failed('output');self.assertIsNone(self.pipe.input);self.assertEqual(self.fds['output'],self.pipe.output)
    def test_output_failure_after_effect_is_not_reclosed(self):self.failed('output',True)

    def test_handle_crosses_close_deadline_before_pipe(self):
        self.f.deadline=20
        self.after_close=lambda:setattr(self.f,'clock',10)
        with self.assertRaises(RegistryError):self.release()
        self.assertEqual(['handle'],self.order)
        self.assertEqual({'handle':'RELEASED','pipe':'NOT_STARTED'},self.c.owner_release)
        self.pending();self.no_retry()

    def test_binding_change_after_handle_prevents_pipe(self):
        self.after_close=lambda:self.c.record['creation'].update(ownerGroup='changed')
        with self.assertRaises(RegistryError):self.release()
        self.assertEqual(['handle'],self.order)
        self.no_retry()

    def test_each_fd_uses_same_deadline(self):
        original=os.close
        def late(fd):
            result=original(fd)
            if fd==self.fds['input']:self.f.clock=10
            return result
        with patch.object(database_proxy_pipe.os,'close',side_effect=late):
            with self.assertRaises(RegistryError):self.release()
        self.assertEqual(['handle','input'],self.order)
        self.assertIsNone(self.pipe.input);self.assertEqual(self.fds['output'],self.pipe.output)
        self.assertEqual('UNKNOWN',self.c.owner_release['pipe'])
        self.pending();self.no_retry()

    def test_last_fd_crosses_deadline_without_success(self):
        original=os.close
        def late(fd):
            result=original(fd)
            if fd==self.fds['output']:self.f.clock=10
            return result
        with patch.object(database_proxy_pipe.os,'close',side_effect=late):
            with self.assertRaises(RegistryError):self.release()
        self.assertEqual(['handle','input','output'],self.order)
        self.assertEqual('RELEASED',self.c.owner_release['pipe'])
        self.pending();self.no_retry()

    def test_pipe_that_returns_with_open_fd_is_not_released(self):
        with patch.object(self.pipe,'close_after_exit',return_value=None):
            with self.assertRaises(RegistryError):self.release()
        self.assertEqual('UNKNOWN',self.c.owner_release['pipe'])
        self.pending();self.no_retry()

    def serialize(self, intent=False, change=None):
        original=json.dumps
        def changed(value,*args,**kwargs):
            result=original(value,*args,**kwargs)
            if isinstance(value,dict) and 'ownerRelease' in value and ('responseSha256' not in value['ownerRelease'])==intent:
                if change:change()
                else:self.f.clock=10
            return result
        with patch.object(offline_control.json,'dumps',side_effect=changed):
            with self.assertRaises(RegistryError):self.release()
        if not intent:self.no_retry()

    def test_intent_serialization_deadline_has_no_effect(self):
        self.serialize(intent=True)
        self.assertEqual([],self.order);self.assertNotIn('ownerRelease',self.f.saved())
    def test_final_serialization_deadline_keeps_released_and_pending(self):
        self.serialize();self.pending()
    def test_final_serialization_binding_change_is_rejected(self):
        self.serialize(change=lambda:self.c.record.update(commandHash='f'*64))
    def test_final_serialization_result_change_is_rejected(self):
        self.serialize(change=lambda:setattr(self.c,'owner_release_response',b'{}'))

    def test_local_record_failure_keeps_all_released_without_retry(self):
        with patch.object(self.c,'_record',side_effect=OSError('fixture journal失敗')):
            with self.assertRaises(OSError):self.release()
        self.assertIsNotNone(self.c.owner_release_response)
        self.pending();self.no_retry()

    def test_local_record_crosses_deadline(self):
        original=self.c._record
        def late():original();self.f.clock=10
        with patch.object(self.c,'_record',side_effect=late):
            with self.assertRaises(RegistryError):self.release()
        self.assertNotIn('responseSha256',self.f.saved()['ownerRelease'])
        self.pending();self.no_retry()

    def consistent_proof(self, key, value):
        proof=json.loads(self.c.owner_exit_response)
        proof[key]=value
        self.c.owner_exit_response=canonical(proof)
        self.c.record['ownerExit']=deepcopy(proof)
        saved=self.f.saved();saved['ownerExit']['responseSha256']=digest(self.c.owner_exit_response);self.f.save(saved)

    def test_final_update_failure_keeps_local_proof(self):
        original=self.runtime.registry.transaction
        @contextmanager
        def transaction(*args,**kwargs):
            with original(*args,**kwargs) as db:
                if not kwargs.get('read_only'):
                    db.set_authorizer(lambda action,*_:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_UPDATE
                        and self.c.owner_release_response is not None else sqlite3.SQLITE_OK)
                yield db
        with patch.object(self.runtime.registry,'transaction',side_effect=transaction):
            with self.assertRaises(sqlite3.DatabaseError):self.release()
        self.assertNotIn('responseSha256',self.f.saved()['ownerRelease'])
        self.pending();self.no_retry()

    def committed_change(self, change):
        original=self.runtime.registry.transaction
        @contextmanager
        def transaction(*args,**kwargs):
            final=False
            with original(*args,**kwargs) as db:
                yield db
                if not kwargs.get('read_only'):
                    row=db.execute('SELECT response FROM runtime_operations WHERE id=?',(json.loads(self.c.request)['operationId'],)).fetchone()
                    final='responseSha256' in json.loads(row[0]).get('ownerRelease',{})
            if final:change()
        with patch.object(self.runtime.registry,'transaction',side_effect=transaction):
            with self.assertRaises(RegistryError):self.release()
        self.assertEqual(digest(self.c.owner_release_response),self.f.saved()['ownerRelease']['responseSha256'])
        self.pending();self.no_retry()

    def test_commit_deadline_keeps_known_result(self):
        self.committed_change(lambda:setattr(self.f,'clock',10))

    def test_commit_progress_change_does_not_return_success(self):
        self.committed_change(lambda:self.c.owner_release.update(pipe='UNKNOWN'))

    def race(self, mutate=False):
        entered,proceed=threading.Event(),threading.Event()
        self.addCleanup(proceed.set)
        def hold():entered.set();self.assertTrue(proceed.wait(10))
        self.before_close=hold
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.release)
            try:
                self.assertTrue(entered.wait(10))
                if mutate:
                    changed=self.f.saved();changed['ownerRelease']['ownerExitSha256']='f'*64;self.f.save(changed)
                else:
                    self.no_retry()
            finally:proceed.set()
            if mutate:
                with self.assertRaises(RegistryError):future.result(10)
                self.assertEqual(changed,self.f.saved())
                self.assertEqual(['handle'],self.order)
            else:self.assertEqual('OWNER_HANDLES_RELEASED',future.result(10)['status'])

    def test_pending_release_blocks_competing_release_close_prepare_start(self):self.race()
    def test_registry_change_while_handle_is_blocked_prevents_pipe(self):self.race(True)


def mismatch_case(change):
    def test(self):self.rejected(lambda:change(self))
    return test


for name,change in {
    'old_epoch':lambda s:s.token.update(epoch=s.token['epoch']+1),
    'missing_exit_record':lambda s:s.f.save({k:v for k,v in s.f.saved().items() if k!='ownerExit'}),
    'missing_exit_raw':lambda s:setattr(s.c,'owner_exit_response',None),
    'changed_exit_hash':lambda s:s.f.save({**s.f.saved(),'ownerExit':{**s.f.saved()['ownerExit'],'responseSha256':'f'*64}}),
    'changed_exit_raw':lambda s:setattr(s.c,'owner_exit_response',s.c.owner_exit_response+b' '),
    'changed_close_raw':lambda s:setattr(s.c,'close_response',s.c.close_response+b' '),
    'changed_record':lambda s:s.c.record['ownerExit'].update(allChildrenExited=False),
    'changed_group':lambda s:setattr(s.process,'group','changed'),
    'changed_creation':lambda s:s.c.record['creation'].update(ownerGroup='changed'),
    'changed_parent':lambda s:s.f.p.adapter.parent.update(startId='changed'),
    'changed_boot':lambda s:setattr(s.f.p.native,'boot','changed'),
    'changed_complete_ack':lambda s:setattr(s.c,'completion_acknowledged',False),
}.items():setattr(OfflineOwnerReleaseTests,'test_'+name+'_has_effect_zero',mismatch_case(change))

for key,value in (('status','UNKNOWN'),('operationId','old-close'),('closeRequestSha256','f'*64),
                  ('closeResponseSha256','f'*64),('ownerGroup','other-group'),('exitCode',False),
                  ('allChildrenExited',False),('extra',True)):
    setattr(OfflineOwnerReleaseTests,'test_consistent_outer_hash_'+key+'_is_rejected',
            mismatch_case(lambda s,k=key,v=value:s.consistent_proof(k,v)))
