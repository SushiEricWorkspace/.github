"""Ready読取のP所有・期限・原文・正常解放をOS生成なしで検証する。"""
from contextlib import nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from . import offline_process as target, runtime_process
from . import test_offline_process as fixture
from .build_inputs import canonical, digest
from .registry import RegistryError


class ReadyPipe(fixture.Pipe):
    def send(self, request, deadline, verify):
        self.current.deadlines.append(deadline)
        verify()
        self.sent.append(deepcopy(request))
        if self.current.failure == 'partial':
            raise TimeoutError('fixture 部分送信')
        self.current.after_send()

    def receive(self, deadline, verify):
        self.current.deadlines.append(deadline)
        verify()
        if self.current.failure == 'lost':
            raise EOFError('fixture 応答喪失')
        request = self.sent[-1]
        response = {k:request[k] for k in ('operationId','requestHash','sessionId','nonce','worldUuid','generation','runUuid')}
        response.update(status='READY_VERIFIED', stateSha256=request['expectedStateSha256'],
                        stateRevision=request['expectedRevision'])
        if self.current.response_change:
            key, value = self.current.response_change
            if key == 'missing': response.pop(value)
            else: response[key] = value
        self.current.reader.process.code = self.current.exit_code
        self.current.after_receive()
        return response


class ReadyProcessTests(unittest.TestCase):
    helper = fixture.OfflineProcessTests.helper

    def setUp(self):
        fixture.OfflineProcessTests.setUp(self)
        self.clock = 0.0
        self.enterContext(patch.object(target.time, 'monotonic', lambda:self.clock))
        self.enterContext(patch.object(target.time, 'sleep', side_effect=AssertionError('実sleep禁止')))
        self.manifest = dict(artifactId='e'*64, files=[])
        for name, raw in (('java', b'fixed-jdk'), ('helper.jar', b'fixed-ready-helper')):
            (self.bundle/name).write_bytes(raw)
            self.manifest['files'].append(dict(path=name, size=len(raw), sha256=digest(raw), kind='regular'))
        (self.bundle/'runtime.json').write_bytes(canonical(self.manifest))
        (self.bundle/'COMPLETE').write_text(self.manifest['artifactId'], encoding='utf-8')
        self.enterContext(patch.object(target, 'verify_bundle', side_effect=lambda _:deepcopy(self.manifest)))
        self.enterContext(patch.object(target, 'offline_ready_command', side_effect=lambda root:[str(root/'java'),'fixed-Ready']))
        ReadyPipe.current = self
        self.enterContext(patch.object(target, 'ProxyPipe', ReadyPipe))
        self.bindings = dict(lease='original', epoch=1, generation='fixed', ready='original', closed='CLOSED',
                             terminal='original', reservation='absent')
        self.original_bindings = deepcopy(self.bindings)
        self.deadlines = []
        self.exit_code = 0
        self.after_receive = self.close_hook = lambda:None
        self.reader = self.helper()
        process = self.reader.process
        self.closes = 0
        self.closed = False
        self.close_failure = False
        original_poll = process.poll
        def poll():
            if self.closed: raise AssertionError('解放後の旧handle poll禁止')
            return original_poll()
        process.poll = poll
        process.members = lambda: ([process.pid] if process.code is None else []) + process.extra
        def close(verify=None):
            self.closes += 1
            if verify: verify()
            self.close_hook()
            if self.close_failure: raise OSError('fixture handle部分解放失敗')
            self.closed = True
            if verify: verify()
        process.close = close
        self.input = {k:v for k,v in fixture.frame().items() if k in {
            'operationId','requestHash','generation','worldRoot','shopDirectory','worldUuid','runUuid',
            'expectedStateSha256','expectedRevision'}}
        original_identity = self.adapter.identity
        def identity(pid):
            if pid == process.pid and process.code == 0:
                raise AssertionError('正常終了後の旧PID再解決禁止')
            return original_identity(pid)
        self.adapter.identity = identity

    def verify(self):
        if not self.valid or self.bindings != self.original_bindings:
            raise RegistryError('STALE_OPERATION', 'fixture 元Ready/CLOSED/終端の変更')

    def read(self):
        return self.reader.verify_ready(self.input, 10.0)

    def effects(self):
        return (sum(getattr(p,'starts',0) for p in self.adapter.processes),len(self.pipes),
                sum(len(p.sent) for p in self.pipes),self.closes)

    def no_retry(self):
        before = self.effects()
        with self.assertRaises(RegistryError): self.read()
        with self.assertRaises(RegistryError): self.reader.start(fixture.frame(), 20.0)
        self.assertEqual(before, self.effects())

    def test_single_read_exits_and_releases_without_rebinding_or_dropping_originals(self):
        original = deepcopy(self.input)
        result = self.read()
        self.assertEqual('READY_READ_RELEASED', result['status'])
        self.assertEqual((1,1,1,1), self.effects())
        self.assertEqual([10.0,10.0], self.deadlines)
        self.assertEqual(original, self.input)
        self.assertEqual(original,json.loads(self.reader.ready_check_input))
        self.assertEqual(10.0,self.reader.ready_check_deadline)
        self.assertEqual(digest(b'fixed-ready-helper'),self.reader.ready_check_artifact['files']['helper.jar'][1])
        self.assertEqual({'handle':'RELEASED','pipe':'RELEASED'}, self.reader.ready_check_release)
        self.assertIs(self.reader.creation_resources[0], self.reader.process)
        self.assertIs(self.reader.creation_resources[1], self.reader.pipe)
        self.assertTrue(all(getattr(self.reader.pipe,n) is None for n in ('child_input','child_output','input','output')))
        self.assertEqual(digest(self.reader.request),result['requestSha256'])
        self.assertEqual(digest(self.reader.ready_check_response),result['responseSha256'])
        result['ready']['stateRevision']=99
        self.assertEqual(0,json.loads(self.reader.ready_check_result)['ready']['stateRevision'])
        journal=''.join(p.read_text(encoding='utf-8') for p in self.reader.diagnostics.glob('creation-*.json'))
        for value in (self.reader.session,self.reader.nonce,self.input['worldRoot']): self.assertNotIn(value,journal)
        self.assertFalse(self.reader.ready)
        self.no_retry()

    def test_invalid_revision_and_unknown_input_reject_before_creation(self):
        for key,value in (('expectedRevision',False),('expectedRevision',0.0),('expectedRevision',2**63),('allowStart',True)):
            with self.subTest(key=key,value=value):
                reader=self.helper()
                with self.assertRaises(RegistryError): reader.verify_ready(dict(self.input,**{key:value}),10.0)
                self.assertEqual(0,reader.process.starts)

    def test_each_origin_binding_rejects_before_creation(self):
        for key in self.bindings:
            with self.subTest(key=key):
                self.bindings=deepcopy(self.original_bindings)
                self.bindings[key]='changed'
                reader=self.helper()
                with self.assertRaises(RegistryError): reader.verify_ready(self.input,10.0)
                self.assertEqual(0,reader.process.starts)
        self.bindings=deepcopy(self.original_bindings)

    def test_entry_deadline_has_no_generation_or_pipe(self):
        self.clock=10.0
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual((0,0,0,0),self.effects())
        self.no_retry()

    def test_renewed_deadline_cannot_retry_entry_failure(self):
        self.clock=10.0
        with self.assertRaises(RegistryError):self.read()
        self.clock=0.0
        with self.assertRaises(RegistryError):self.reader.verify_ready(self.input,20.0)
        self.assertEqual((0,0,0,0),self.effects())

    def test_response_mismatch_retains_original_without_release(self):
        self.response_change=('generation',fixture.RUN)
        with self.assertRaises(RegistryError):self.read()
        self.assertIsNotNone(self.reader.ready_check_response)
        self.assertIsNone(self.reader.ready_check_result)
        self.assertEqual((1,1,1,0),self.effects())
        self.no_retry()

    def test_float_response_revision_is_rejected(self):
        self.response_change=('stateRevision',0.0)
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_each_response_binding_and_missing_field_is_rejected(self):
        for key,value in (('operationId','different'),('requestHash','f'*64),('sessionId','different'),
                          ('nonce','different'),('worldUuid',fixture.RUN),('generation',fixture.RUN),
                          ('runUuid',fixture.RUN),('stateSha256','f'*64),('stateRevision',1),
                          ('status','REJECTED'),('missing','generation')):
            with self.subTest(key=key):
                self.reader=self.helper()
                self.response_change=key,value
                with self.assertRaises(RegistryError):self.read()
                self.assertIsNotNone(self.reader.ready_check_response)
                self.no_retry()

    def test_response_loss_retains_request_and_pipe_without_retry(self):
        self.failure='lost'
        with self.assertRaises(EOFError):self.read()
        self.assertIsNotNone(self.reader.request)
        self.assertIsNone(self.reader.ready_check_response)
        self.assertEqual((1,1,1,0),self.effects())
        self.no_retry()

    def test_response_deadline_preserves_received_raw(self):
        self.after_receive=lambda:setattr(self,'clock',10.0)
        with self.assertRaises(RegistryError):self.read()
        self.assertIsNotNone(self.reader.ready_check_response)
        self.assertEqual(0,self.closes)

    def test_origin_change_after_send_rejects_without_release(self):
        self.after_send=lambda:self.bindings.update(terminal='changed')
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual((1,1,1,0),self.effects())

    def test_helper_nonzero_exit_rejects_without_release(self):
        self.exit_code=1
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_root_exit_does_not_imply_all_children_exited(self):
        self.after_receive=lambda:self.reader.process.extra.append(12345)
        def wait(_):self.clock=10.0
        with patch.object(target.time,'sleep',side_effect=wait):
            with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_live_helper_waits_until_normal_exit(self):
        self.exit_code=None
        with patch.object(target.time,'sleep',side_effect=lambda _:setattr(self.reader.process,'code',0)):
            self.assertEqual('READY_READ_RELEASED',self.read()['status'])

    def test_handle_failure_stays_unknown_and_never_retries(self):
        self.close_failure=True
        with self.assertRaises(OSError):self.read()
        self.assertEqual({'handle':'UNKNOWN','pipe':'NOT_STARTED'},self.reader.ready_check_release)
        self.assertIsNotNone(self.reader.pipe.input)
        self.no_retry()

    def test_handle_close_deadline_prevents_pipe_release(self):
        self.close_hook=lambda:setattr(self,'clock',10.0)
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual({'handle':'UNKNOWN','pipe':'NOT_STARTED'},self.reader.ready_check_release)
        self.assertIsNotNone(self.reader.pipe.input)
        self.no_retry()

    def test_pipe_partial_failure_retains_released_handle_and_unknown_pipe(self):
        def close(verify=None):
            verify();self.reader.pipe.input=None
            raise OSError('fixture pipe部分解放')
        with patch.object(ReadyPipe,'close_after_exit',side_effect=close):
            with self.assertRaises(OSError):self.read()
        self.assertEqual({'handle':'RELEASED','pipe':'UNKNOWN'},self.reader.ready_check_release)
        self.assertIsNotNone(self.reader.pipe.output)
        self.no_retry()

    def test_pipe_deadline_between_fds_keeps_unknown_progress(self):
        def close(verify=None):
            if verify:verify()
            self.reader.pipe.input=None
            self.clock=10.0
            if verify:verify()
            self.reader.pipe.output=None
        with patch.object(ReadyPipe,'close_after_exit',side_effect=close):
            with self.assertRaises(RegistryError):self.read()
        self.assertIsNotNone(self.reader.pipe.output)
        self.assertEqual({'handle':'RELEASED','pipe':'UNKNOWN'},self.reader.ready_check_release)
        self.no_retry()

    def test_unchanged_bytes_at_replaced_inode_are_rejected(self):
        def replace():
            path=self.bundle/'helper.jar'
            saved=path.stat(); replacement=self.bundle/'replacement'
            replacement.write_bytes(path.read_bytes())
            os.utime(replacement,ns=(saved.st_atime_ns,saved.st_mtime_ns))
            replacement.replace(path)
        self.after_send=replace
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_artifact_modified_during_send_is_rejected(self):
        self.after_send=lambda:(self.bundle/'helper.jar').write_bytes(b'changed')
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_artifact_hash_rejects_same_length_and_restored_mtime(self):
        def change():
            path=self.bundle/'helper.jar';info=path.stat()
            path.write_bytes(b'x'*info.st_size)
            os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
        self.after_send=change
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_kept_artifact_snapshot_cannot_be_mutated_in_place(self):
        self.after_send=lambda:self.reader.ready_check_artifact.clear()
        with self.assertRaises(RegistryError):self.read()

    def test_release_progress_cannot_claim_completion_before_close(self):
        self.after_receive=lambda:self.reader.ready_check_release.update(handle='RELEASED')
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_kept_input_cannot_be_replaced(self):
        self.after_receive=lambda:setattr(self.reader,'ready_check_input',b'{}')
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_entry_deadline_cannot_be_extended_by_kept_value(self):
        self.after_receive=lambda:setattr(self.reader,'ready_check_deadline',20.0)
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_replaced_process_after_reply_is_rejected(self):
        self.after_receive=lambda:setattr(self.reader,'process',object())
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_same_process_cannot_rebind_handle_value(self):
        self.after_receive=lambda:setattr(self.reader.process,'handle',12345)
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_same_pipe_cannot_rebind_fd_value(self):
        self.after_receive=lambda:setattr(self.reader.pipe,'input',12345)
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_replaced_pipe_after_reply_is_rejected(self):
        self.after_receive=lambda:setattr(self.reader,'pipe',object())
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_replaced_creation_tuple_after_reply_is_rejected(self):
        self.after_receive=lambda:setattr(self.reader,'creation_resources',tuple(list(self.reader.creation_resources)))
        with self.assertRaises(RegistryError):self.read()
        self.assertEqual(0,self.closes)

    def test_expiry_after_result_copy_refuses_return_with_evidence(self):
        original=target.deepcopy
        seen=[]
        def copy(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='READY_READ_RELEASED':
                seen.append(1)
                if len(seen)==2:self.clock=10.0
            return result
        with patch.object(target,'deepcopy',side_effect=copy):
            with self.assertRaises(RegistryError):self.read()
        self.assertEqual({'handle':'RELEASED','pipe':'RELEASED'},self.reader.ready_check_release)
        self.assertIsNotNone(self.reader.ready_check_result)
        self.no_retry()

    def test_expiry_after_record_copy_does_not_start_final_record(self):
        original=target.deepcopy
        records=[]
        def copy(value):
            result=original(value)
            if isinstance(value,dict) and value.get('status')=='READY_READ_RELEASED':self.clock=10.0
            return result
        record=self.reader._record
        def recording():
            records.append('readyRead' in self.reader.record)
            record()
        with patch.object(target,'deepcopy',side_effect=copy),patch.object(self.reader,'_record',side_effect=recording):
            with self.assertRaises(RegistryError):self.read()
        self.assertNotIn(True,records)
        self.assertIsNone(self.reader.ready_check_result)

    def windows_preparation(self, failure):
        process=runtime_process.OwnedProcess()
        self.reader.process=process
        kernel=Mock(CreateJobObjectW=Mock(return_value=126))
        def prepared(handle, flag):
            if flag:
                if failure=='deadline':self.clock=10.0
                elif failure=='artifact':(self.bundle/'helper.jar').write_bytes(b'changed')
                elif failure=='binding':self.bindings['epoch']=2
                elif failure=='registry':self.registry_change()
                elif failure=='pipe':self.reader.pipe.input=12345
        with patch.object(runtime_process,'security_descriptor',return_value=nullcontext(None)), \
             patch.object(runtime_process,'kernel',kernel), \
             patch.object(runtime_process.ctypes,'get_last_error',return_value=0), \
             patch.object(runtime_process,'open',return_value=nullcontext(Mock(fileno=lambda:11)),create=True), \
             patch.object(runtime_process.msvcrt,'get_osfhandle',side_effect=lambda fd:fd+100), \
             patch.object(runtime_process.os,'set_handle_inheritable',side_effect=prepared), \
             patch.object(runtime_process._winapi,'CreateProcess',side_effect=RegistryError('FIXTURE_CREATION','実生成禁止')) as create:
            with self.assertRaises((RegistryError,ValueError)):self.read()
        create.assert_not_called()
        kernel.AssignProcessToJobObject.assert_not_called()
        kernel.ResumeThread.assert_not_called()
        kernel.CloseHandle.assert_not_called()
        self.assertEqual((126,None,None,'JOB_CREATED'),(process.job,process.handle,process.pid,process.stage))
        self.assertIs(self.reader.creation_resources[0],process)
        self.assertIs(self.reader.creation_resources[1],self.reader.pipe)
        self.assertIsNotNone(self.reader.pipe.input)
        self.no_retry()

    @unittest.skipUnless(os.name=='nt','Windows生成準備を合成')
    def test_actual_owned_process_preparation_deadline_has_process_zero(self):self.windows_preparation('deadline')

    @unittest.skipUnless(os.name=='nt','Windows生成準備を合成')
    def test_actual_owned_process_preparation_artifact_change_has_process_zero(self):self.windows_preparation('artifact')

    @unittest.skipUnless(os.name=='nt','Windows生成準備を合成')
    def test_actual_owned_process_preparation_binding_change_has_process_zero(self):self.windows_preparation('binding')

    @unittest.skipUnless(os.name=='nt','Windows生成準備を合成')
    def test_actual_owned_process_preparation_fd_change_has_process_zero(self):self.windows_preparation('pipe')

    @unittest.skipUnless(os.name=='nt','Windows正常closeを合成')
    def test_actual_owned_close_checks_deadline_between_handles(self):
        process=self.reader.process
        process.handle,process.job=123,126
        process.close=lambda verify:runtime_process.OwnedProcess.close(process,verify=verify)
        kernel=Mock()
        with patch.object(runtime_process._winapi,'CloseHandle',side_effect=lambda _:setattr(self,'clock',10.0)) as close, \
             patch.object(runtime_process,'kernel',kernel):
            with self.assertRaises(RegistryError):self.read()
        close.assert_called_once_with(123)
        kernel.CloseHandle.assert_not_called()
        self.assertEqual({'handle':'UNKNOWN','pipe':'NOT_STARTED'},self.reader.ready_check_release)
        self.no_retry()

    def registry_binding(self):
        from concurrent.futures import ThreadPoolExecutor
        from . import test_runtime_control as runtime_fixture
        base=runtime_fixture.RuntimeControlTests()
        base.setUp();self.addCleanup(base.doCleanups)
        base.prepare()
        token=deepcopy(base.token)
        pool=self.enterContext(ThreadPoolExecutor(max_workers=1))
        def verify():
            with base.registry.transaction(read_only=True) as db:
                row=base.registry._instance(db,token['instanceId'])
                base.registry._token(row,token)
        def change():
            with base.registry.transaction() as db:
                db.execute('UPDATE instances SET epoch=epoch+1 WHERE id=?',(token['instanceId'],))
        self.reader.verify_owner=verify
        self.registry_change=lambda:pool.submit(change).result(timeout=5)
        return base,token

    @unittest.skipUnless(os.name=='nt','Windows生成準備を合成')
    def test_real_registry_transaction_race_rejects_before_creation(self):
        self.registry_binding()
        self.windows_preparation('registry')

    @unittest.skipUnless(os.name=='nt','Windows生成準備を合成')
    def test_registry_commit_during_preparation_inventory_prevents_creation(self):
        base,token=self.registry_binding()
        original=target.regular_bytes
        committed=[]
        def read(path, **kwargs):
            raw=original(path, **kwargs)
            if path.name=='helper.jar' and self.reader.process.stage=='JOB_CREATED' and not committed:
                self.registry_change();committed.append(True)
            return raw
        with patch.object(target,'regular_bytes',side_effect=read):
            self.windows_preparation('inventory')
        self.assertEqual([True],committed)
        with base.registry.transaction(read_only=True) as db:
            self.assertEqual(token['epoch']+1,base.registry._instance(db,token['instanceId'])['epoch'])
        self.assertIsNone(self.reader.ready_check_result)

    def test_registry_commit_during_final_inventory_prevents_return(self):
        import inspect
        base,token=self.registry_binding()
        original_read=target.regular_bytes
        original_copy=target.deepcopy
        copies=[];committed=[]
        def copy(value):
            if isinstance(value,dict) and value.get('status')=='READY_READ_RELEASED':copies.append(True)
            return original_copy(value)
        def read(path, **kwargs):
            raw=original_read(path, **kwargs)
            caller=inspect.currentframe().f_back
            if (path.name=='helper.jar' and len(copies)==2 and not committed
                    and caller.f_back.f_back.f_code.co_name=='verify_ready'):
                self.registry_change();committed.append(True)
            return raw
        with patch.object(target,'regular_bytes',new=read),patch.object(target,'deepcopy',side_effect=copy):
            with self.assertRaises(RegistryError):self.read()
        self.assertEqual([True],committed)
        with base.registry.transaction(read_only=True) as db:
            self.assertEqual(token['epoch']+1,base.registry._instance(db,token['instanceId'])['epoch'])
        self.assertEqual({'handle':'RELEASED','pipe':'RELEASED'},self.reader.ready_check_release)
        self.assertIsNotNone(self.reader.ready_check_input)
        self.assertIsNotNone(self.reader.ready_check_response)
        self.assertIsNotNone(self.reader.ready_check_result)
        self.no_retry()

    def test_real_fixed_bundle_command_and_inventory_are_used(self):
        from . import test_offline_controller_bundle as bundle_fixture
        case=bundle_fixture.OfflineControllerBundleTests()
        case.setUp();self.addCleanup(case.doCleanups)
        case.compile(ready=True)
        from .runtime_bundle import verify_bundle
        from .offline_controller_bundle import offline_ready_command
        self.reader.bundle=case.bundle
        with patch.object(target,'verify_bundle',side_effect=verify_bundle), \
             patch.object(target,'offline_ready_command',side_effect=offline_ready_command):
            result=self.read()
        self.assertEqual(verify_bundle(case.bundle)['artifactId'],result['artifactId'])
        self.assertTrue(self.reader.process.arguments[-1].endswith('$Ready'))


if __name__=='__main__':unittest.main()
