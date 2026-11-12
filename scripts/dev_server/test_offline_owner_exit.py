"""close時の元C bindingと終了証拠を実Registry/Eventで検証する。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from . import offline_control, offline_process
from . import test_offline_owner_close as closing
from . import test_offline_prepare as prepared
from . import test_offline_repeat as repeated
from . import test_game_owner_close_deadline as game_closing
from .build_inputs import canonical, digest
from .registry import RegistryError


class OfflineOwnerExitTests(unittest.TestCase):
    def setUp(self):
        self.f = closing.OfflineOwnerCloseTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.start_close()

    def start_close(self):
        self.f.close()
        self.c, self.runtime, self.token = self.f.c, self.f.runtime, self.f.token
        self.original = deepcopy(self.f.saved())
        self.process, self.pipe = self.c.process, self.c.pipe
        self.frames = deepcopy(self.pipe.sent)
        self.fds = self.pipe.input, self.pipe.output
        self.enterContext(patch.object(self.process, 'members', side_effect=lambda:
            ([self.process.pid] if self.process.code is None else []) + self.process.extra))
        self.enterContext(patch.object(offline_process.time, 'sleep', side_effect=lambda _:setattr(self.f,'clock',self.f.clock+1)))

    def observe(self, **changes):
        values = dict(operation_id=self.f.close_operation, deadline=self.f.deadline)
        values.update(changes)
        return self.runtime._offline_observe_owner_exit(self.token, **values)

    def preserved(self):
        self.f.prepared.pending()
        self.assertIs(self.c, self.runtime._offline.controllers[self.token['instanceId']])
        self.assertIs(self.process, self.c.process)
        self.assertIs(self.pipe, self.c.pipe)
        self.assertEqual(self.frames, self.pipe.sent)
        self.assertEqual(self.fds, (self.pipe.input, self.pipe.output))
        self.assertEqual(1, self.f.closes)
        self.assertEqual(self.original, {k:v for k,v in self.f.saved().items() if k != 'ownerExit'})

    def no_retry(self):
        self.f.clock = 0
        saved = deepcopy(self.f.saved())
        with patch.object(self.process, 'poll', side_effect=AssertionError('再観測')):
            for operation in (self.f.close_operation, 'another-exit'):
                with self.assertRaises(RegistryError):
                    self.observe(operation_id=operation)
        with self.assertRaises(RegistryError):self.f.close()
        with self.assertRaises(RegistryError):self.f.prepare_next()
        with self.assertRaises(RegistryError):
            self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='start-after-owner-exit')
        self.assertEqual(saved,self.f.saved())

    def rejected(self, change, code=None):
        change()
        with self.assertRaises(RegistryError) as caught:self.observe()
        if code:self.assertEqual(code,caught.exception.code)
        self.assertNotIn('responseSha256',self.f.saved().get('ownerExit',{}))
        if 'ownerExit' in self.f.saved():self.no_retry()

    def test_exit_after_nonempty_edit_records_proof_without_closed_or_release(self):
        result = self.observe()
        self.assertEqual('OWNER_EXIT_OBSERVED',result['status'])
        self.assertEqual(self.f.close_operation,result['operationId'])
        self.assertEqual(self.c.data_identity,result['owner'])
        self.assertEqual(self.process.group,result['ownerGroup'])
        self.assertEqual((0,True),(result['exitCode'],result['allChildrenExited']))
        self.assertEqual(digest(self.c.close_request),result['closeRequestSha256'])
        self.assertEqual(digest(self.c.close_response),result['closeResponseSha256'])
        self.assertEqual(canonical(result),self.c.owner_exit_response)
        self.assertEqual(result,self.c.record['ownerExit'])
        self.assertEqual(digest(canonical(result)),self.f.saved()['ownerExit']['responseSha256'])
        result['owner']['startId']='changed'
        self.assertNotEqual(result,self.c.record['ownerExit'])
        self.preserved();self.no_retry()

    def test_exit_after_second_edit_keeps_history_and_latest_close(self):
        fixture = repeated.OfflineRepeatTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.finish()
        self.f.configure(fixture,fixture.f.f.a.f.f,fixture.f.f.p,fixture.current,fixture.current)
        self.start_close()
        history = deepcopy(self.c.operation_history)
        self.assertEqual(10,json.loads(self.c.close_response)['stateRevision'])
        self.assertEqual('OWNER_EXIT_OBSERVED',self.observe()['status'])
        self.assertEqual(history,self.c.operation_history)
        self.preserved()

    def test_exit_after_open_without_worker(self):
        fixture = prepared.OfflinePrepareTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        opened = json.loads(fixture.c.request)
        writer = deepcopy(self.f.writer)
        writer['owner'] = deepcopy(fixture.c.data_identity)
        writer['management'] = {k:opened[k] for k in ('leaseId','epoch','generation','drainSha256')}
        writer['management']['controller'] = deepcopy(fixture.c.parent_data)
        current = dict(phase='Idle1',hash='b'*64,revision=1,run=closing.processes.RUN,
                       writer=writer,world=opened['worldUuid'],generation=opened['generation'])
        self.f.configure(fixture,fixture,fixture.p,current,current)
        self.start_close()
        self.assertEqual('OWNER_EXIT_OBSERVED',self.observe()['status'])
        self.assertEqual(1,len(fixture.p.adapter.processes))
        self.preserved()

    def test_game_owner_exit_keeps_close_deadline_after_open_expires(self):
        game = game_closing.GameOwnerCloseDeadlineTests()
        game.setUp();self.addCleanup(game.doCleanups)
        game.open();game.edit(21)
        self.f = game.close_fixture(clock=31,deadline=40)
        self.start_close()
        self.f.clock,self.f.deadline = 35,50
        game.game.case.clock = 35
        original=self.c.verify_owner
        self.assertEqual('OWNER_EXIT_OBSERVED',self.observe()['status'])
        self.assertIs(original,self.c.verify_owner)
        self.preserved()

    def test_exit_cannot_extend_original_close_deadline(self):
        self.f.clock,self.f.deadline=10,20
        before=deepcopy(self.f.saved())
        with patch.object(self.process,'poll',side_effect=AssertionError('close期限後の観測')):
            self.rejected(lambda:None,'STOP_TIMEOUT')
        self.assertEqual(before,self.f.saved())

    def test_shorter_observation_deadline_is_not_extended_to_close_deadline(self):
        self.f.clock,self.f.deadline=5,5
        with patch.object(self.process,'poll',side_effect=AssertionError('観測期限後の観測')):
            self.rejected(lambda:None,'STOP_TIMEOUT')

    def test_close_deadline_reaches_wait_and_final_transaction(self):
        self.f.deadline=20
        def members():self.f.clock=10;return []
        with patch.object(self.process,'members',side_effect=members):self.rejected(lambda:None,'STOP_TIMEOUT')
        self.assertIsNone(self.c.owner_exit_response)

    def test_process_exit_cannot_extend_close_deadline(self):
        self.f.clock=10
        with patch.object(self.process,'poll',side_effect=AssertionError('close期限後の観測')):
            with self.assertRaises(RegistryError) as caught:
                self.c.observe_owner_exit(deadline=20,verify=lambda:None)
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)

    def test_close_deadline_reaches_final_serialization(self):
        self.f.deadline=20
        self.serialized_boundary()

    def test_exited_pid_is_not_resolved_again(self):
        identity = self.f.p.adapter.identity
        def checked(pid):
            self.assertNotEqual(self.process.pid,pid)
            return identity(pid)
        with patch.object(self.f.p.adapter,'identity',side_effect=checked):self.observe()
        self.preserved()

    def test_live_c_waits_for_handle_and_children(self):
        self.process.code=None
        self.process.extra=[999]
        steps=[]
        def progress(_):
            steps.append(list(self.process.extra))
            if self.process.code is None:self.process.code=0
            else:self.process.extra=[]
        with patch.object(offline_process.time,'sleep',side_effect=progress):self.observe()
        self.assertEqual([[999],[999]],steps)
        self.preserved()

    def test_live_identity_disappears_only_with_positive_zero_handle(self):
        self.process.code=None
        identity=self.f.p.adapter.identity
        def gone(pid):
            if pid==self.process.pid:
                self.process.code=0
                return None
            return identity(pid)
        with patch.object(self.f.p.adapter,'identity',side_effect=gone):self.observe()
        self.preserved()

    def test_unknown_handle_with_missing_identity_is_rejected(self):
        self.process.code=None
        identity=self.f.p.adapter.identity
        with patch.object(self.f.p.adapter,'identity',side_effect=lambda pid:None if pid==self.process.pid else identity(pid)):
            self.rejected(lambda:None,'WRITER_PENDING')

    def test_changed_live_identity_is_not_replaced_by_zero_exit(self):
        self.process.code=None
        identity=self.f.p.adapter.identity
        def changed(pid):
            result=identity(pid)
            if pid==self.process.pid:
                self.process.code=0
                result['commandHash']='f'*64
            return result
        with patch.object(self.f.p.adapter,'identity',side_effect=changed):
            self.rejected(lambda:None,'PROCESS_MISMATCH')

    def test_nonzero_exit_preserves_intent(self):self.rejected(lambda:setattr(self.process,'code',1),'WRITER_PENDING')
    def test_boolean_exit_is_not_zero(self):self.rejected(lambda:setattr(self.process,'code',False),'WRITER_PENDING')
    def test_live_c_times_out_without_kill(self):self.rejected(lambda:setattr(self.process,'code',None),'STOP_TIMEOUT')
    def test_remaining_child_times_out(self):self.rejected(lambda:setattr(self.process,'extra',[999]),'STOP_TIMEOUT')
    def test_unknown_members_is_not_empty(self):
        with patch.object(self.process,'members',return_value=None):self.rejected(lambda:None,'PROCESS_MISMATCH')

    def test_expired_deadline_does_not_observe_or_reserve(self):
        before=deepcopy(self.f.saved())
        with patch.object(self.process,'poll',side_effect=AssertionError('期限後の観測')):
            self.rejected(lambda:setattr(self.f,'clock',10),'STOP_TIMEOUT')
        self.assertEqual(before,self.f.saved())

    def test_poll_crosses_deadline(self):
        def poll():self.f.clock=10;return 0
        with patch.object(self.process,'poll',side_effect=poll), \
                patch.object(self.process,'members',side_effect=AssertionError('期限後のgroup照会')):
            self.rejected(lambda:None,'STOP_TIMEOUT')

    def test_members_crosses_deadline(self):
        def members():self.f.clock=10;return []
        with patch.object(self.process,'members',side_effect=members):self.rejected(lambda:None,'STOP_TIMEOUT')
        self.assertIsNone(self.c.owner_exit_response)

    def test_members_changes_creation_after_last_os_observation(self):
        def members():self.c.record['creation']['ownerGroup']='changed';return []
        with patch.object(self.process,'members',side_effect=members):self.rejected(lambda:None,'PROCESS_MISMATCH')
        self.assertIsNone(self.c.owner_exit_response)

    def test_parent_identity_crosses_deadline(self):
        identity=self.f.p.adapter.identity
        def late(pid):self.f.clock=10;return identity(pid)
        with patch.object(self.f.p.adapter,'identity',side_effect=late):self.rejected(lambda:None,'STOP_TIMEOUT')

    def test_local_serialization_deadline_preserves_result_bytes(self):
        original=offline_process.canonical
        def late(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='OWNER_EXIT_OBSERVED':self.f.clock=10
            return result
        with patch.object(offline_process,'canonical',side_effect=late):self.rejected(lambda:None,'STOP_TIMEOUT')
        self.assertIsNotNone(self.c.owner_exit_response)

    def test_local_record_failure_preserves_result_and_intent(self):
        original=self.c._record
        def failed():
            if 'ownerExit' in self.c.record:raise OSError('fixture終了記録失敗')
            original()
        with patch.object(self.c,'_record',side_effect=failed):
            with self.assertRaises(OSError):self.observe()
        self.assertIsNotNone(self.c.owner_exit_response)
        self.no_retry();self.preserved()

    def test_local_record_crosses_deadline(self):
        original=self.c._record
        def late():original();self.f.clock=10
        with patch.object(self.c,'_record',side_effect=late):self.rejected(lambda:None,'STOP_TIMEOUT')

    def serialized_boundary(self, intent=False, change=None):
        original=json.dumps
        def encoded(value,*args,**kwargs):
            result=original(value,*args,**kwargs)
            if isinstance(value,dict) and 'ownerExit' in value and ('responseSha256' not in value['ownerExit']) == intent:
                if change:change()
                else:self.f.clock=10
            return result
        with patch.object(offline_control.json,'dumps',side_effect=encoded):self.rejected(lambda:None)

    def test_intent_serialization_deadline_has_no_os_observation(self):
        with patch.object(self.process,'poll',side_effect=AssertionError('意図確定前の観測')):
            self.serialized_boundary(intent=True)
        self.assertNotIn('ownerExit',self.f.saved())

    def test_final_serialization_deadline_keeps_pending_proof(self):self.serialized_boundary()
    def test_final_serialization_binding_change_is_rejected(self):
        self.serialized_boundary(change=lambda:self.c.record.update(commandHash='f'*64))
    def test_final_serialization_result_change_is_rejected(self):
        self.serialized_boundary(change=lambda:setattr(self.c,'owner_exit_response',b'{}'))

    def test_registry_update_failure_keeps_local_proof_and_reservation(self):
        original=self.runtime.registry.transaction
        @contextmanager
        def transaction(*args,**kwargs):
            with original(*args,**kwargs) as db:
                if not kwargs.get('read_only'):
                    db.set_authorizer(lambda action,*_:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_UPDATE
                        and self.c.owner_exit_response is not None else sqlite3.SQLITE_OK)
                yield db
        with patch.object(self.runtime.registry,'transaction',side_effect=transaction):
            with self.assertRaises(sqlite3.DatabaseError):self.observe()
        self.assertIsNotNone(self.c.owner_exit_response)
        self.assertNotIn('responseSha256',self.f.saved()['ownerExit'])
        self.no_retry();self.preserved()

    def test_commit_deadline_keeps_known_proof_without_success_return(self):
        original=self.runtime.registry.transaction
        @contextmanager
        def transaction(*args,**kwargs):
            final=False
            with original(*args,**kwargs) as db:
                yield db
                if not kwargs.get('read_only'):
                    saved=json.loads(db.execute('SELECT response FROM runtime_operations WHERE id=?',
                        (json.loads(self.c.request)['operationId'],)).fetchone()[0])
                    final='responseSha256' in saved.get('ownerExit',{})
            if final:self.f.clock=10
        with patch.object(self.runtime.registry,'transaction',side_effect=transaction):
            with self.assertRaises(RegistryError) as caught:self.observe()
        self.assertEqual('STOP_TIMEOUT',caught.exception.code)
        self.assertEqual(digest(self.c.owner_exit_response),self.f.saved()['ownerExit']['responseSha256'])
        self.no_retry();self.preserved()

    def test_waiting_observation_blocks_competitors_with_real_transactions(self):
        self.process.code=None
        entered,proceed=threading.Event(),threading.Event()
        self.addCleanup(proceed.set)
        def wait(_):
            entered.set();self.assertTrue(proceed.wait(10));self.process.code=0
        with patch.object(offline_process.time,'sleep',side_effect=wait), ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.observe)
            try:
                self.assertTrue(entered.wait(10))
                before=deepcopy(self.f.saved())
                for operation in (self.f.close_operation,'competing-exit'):
                    with self.assertRaises(RegistryError):self.observe(operation_id=operation)
                with self.assertRaises(RegistryError):self.f.close('competing-close')
                with self.assertRaises(RegistryError):self.f.prepare_next()
                with self.assertRaises(RegistryError):
                    self.runtime.execute('start',dict(self.token,timeoutSeconds=10),operation_id='competing-start')
                self.assertEqual(before,self.f.saved())
            finally:proceed.set()
            self.assertEqual('OWNER_EXIT_OBSERVED',future.result(10)['status'])
        self.preserved()

    def test_changed_registry_intent_during_wait_is_not_overwritten(self):
        self.process.code=None
        entered,proceed=threading.Event(),threading.Event()
        self.addCleanup(proceed.set)
        def wait(_):
            entered.set();self.assertTrue(proceed.wait(10));self.process.code=0
        with patch.object(offline_process.time,'sleep',side_effect=wait), ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.observe)
            try:
                self.assertTrue(entered.wait(10))
                changed=self.f.saved()
                changed['ownerExit']['closeResponseSha256']='f'*64
                self.f.save(changed)
            finally:proceed.set()
            with self.assertRaises(RegistryError):future.result(10)
        self.assertEqual(changed,self.f.saved())
        self.assertIsNone(self.c.owner_exit_response)
        self.no_retry()


def mismatch_case(change):
    def test(self):self.rejected(lambda:change(self))
    return test


for name,change in {
    'old_token':lambda s:s.token.update(epoch=s.token['epoch']+1),
    'close_not_ready':lambda s:setattr(s.c,'close_ready',False),
    'missing_close_hash':lambda s:s.f.save({**s.f.saved(),'ownerClose':{'operationId':s.f.close_operation}}),
    'missing_close_response_hash':lambda s:s.f.save({**s.f.saved(),'ownerClose':{k:v for k,v in s.f.saved()['ownerClose'].items() if k!='responseSha256'}}),
    'missing_complete_ack':lambda s:setattr(s.c,'completion_acknowledged',False),
    'changed_history':lambda s:s.f.save({**s.f.saved(),'history':[{}]}),
    'changed_close_request':lambda s:setattr(s.c,'close_request',s.c.close_request+b' '),
    'changed_close_response':lambda s:setattr(s.c,'close_response',s.c.close_response+b' '),
    'changed_parent':lambda s:s.f.p.adapter.parent.update(startId='changed'),
    'changed_boot':lambda s:setattr(s.f.p.native,'boot','changed'),
    'changed_identity':lambda s:s.c.identity.update(startId='changed'),
    'changed_data_identity':lambda s:s.c.data_identity.update(hostHash='f'*64),
    'changed_creation':lambda s:s.c.record['creation'].update(ownerGroup='changed'),
    'changed_group':lambda s:setattr(s.process,'group','changed'),
    'changed_command_hash':lambda s:s.c.record.update(commandHash='f'*64),
    'replaced_process':lambda s:setattr(s.c,'process',deepcopy(s.process)),
    'replaced_pipe':lambda s:setattr(s.c,'pipe',deepcopy(s.pipe)),
}.items():
    setattr(OfflineOwnerExitTests,'test_'+name+'_rejects_without_completion',mismatch_case(change))
