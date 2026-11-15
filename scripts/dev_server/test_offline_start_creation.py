"""同じ準備済みdescriptorからのM生成を合成adapterと実Registryで検証する。"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from . import mod_control_runtime, offline_control, runtime_bundle, runtime_control, runtime_process
from . import test_offline_start_descriptor as descriptors
from .build_inputs import ContractError, digest
from .registry import RegistryError


class StartCreationTests(unittest.TestCase):
    def setUp(self):
        self.f = descriptors.StartDescriptorTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.b, self.runtime, self.offline = self.f.b, self.f.runtime, self.f.offline
        self.adapter = self.runtime.process_adapter
        self.preparing = self.after = lambda:None
        self.generated = 0
        self.process = self.adapter.OwnedProcess.return_value
        self.m = type('SyntheticM', (), {})()
        self.m.pid = self.m.group = self.m.handle = self.m.job = self.m.thread = None
        self.m.stage = 'NOT_CREATED'
        self.m.close = Mock(side_effect=AssertionError('元Mを解放しない'))
        self.m.poll = Mock(side_effect=AssertionError('保持binding照合で再pollしない'))
        self.arguments = None

        def start(arguments,cwd,log,nonce,created,**kwargs):
            self.arguments = arguments
            self.environment = kwargs['environment']
            self.m.job, self.m.stage = 501, 'JOB_CREATED'
            self.preparing()
            kwargs['before_create']()
            self.generated += 1
            self.m.pid, self.m.group, self.m.handle, self.m.thread = 98765, nonce, 502, 503
            self.m.stage = 'CREATED'
            self.identity = dict(pid=self.m.pid,startId='original',bootId='boot',cwd=str(cwd.resolve()),
                executable=arguments[0],commandHash=runtime_process.command_hash(arguments))
            created()
            self.m.thread, self.m.stage = None, 'RUNNING'
            self.after()
        self.m.start = start
        self.enterContext(patch.object(self.adapter,'OwnedProcess',side_effect=lambda:
            self.process if not self.f.managed.sessions else self.m))
        identity = self.adapter.identity
        self.enterContext(patch.object(self.adapter,'identity',side_effect=lambda pid:
            deepcopy(self.identity) if pid==98765 else identity(pid)))
        self.command_hook = lambda:None
        def command(bundle,mode,generation,**kwargs):
            self.command_hook()
            observed=self.f.managed.sessions[self.b.token['instanceId']]['observed']
            auxiliary=Path(kwargs['managed_context']['auxiliary'])
            auxiliary.mkdir(parents=True)
            for name,raw in observed['auxiliaryFiles'].items():(auxiliary/name).write_bytes(raw)
            return list(observed['argv'])
        self.command=command
        self.enterContext(patch.object(runtime_control,'launch_command',side_effect=command))
        self.enterContext(patch.object(runtime_control,'prebind',return_value=nullcontext()))

    def create(self, **changes):
        records = descriptors.records
        args = dict(operation_id=records.reservations.START,
            read_operation_id=records.reservations.readers.OPERATION,
            open_operation_id=self.b.base.o.base.opened['operationId'],ready=self.b.base.o.ready,
            deadline=self.b.base.f.deadline)
        args.update(changes)
        return self.runtime._offline_create_start(self.b.token,**args)

    def held(self):
        return self.offline.start_records[self.f.f.key]

    def retained(self, count):
        held = self.held()
        self.assertFalse(held['returned'])
        self.assertEqual(count,self.generated)
        self.assertIs(self.m,held['creation'][1] if 'creation' in held else self.runtime.processes[held['run']['runId']])
        self.assertEqual('PENDING',self.b.saved()[0]['status'])
        self.assertEqual(1,len(self.b.saved()[1]))
        self.assertIsNotNone(self.b.base.p.reader.request)
        self.assertIsNotNone(self.b.base.p.reader.ready_check_response)
        effects=self.b.base.p.effects()
        for changes in ({},{'operation_id':descriptors.records.reservations.OTHER}):
            with self.assertRaises(RegistryError):self.create(**changes)
        self.assertEqual(count,self.generated)
        self.assertEqual(effects,self.b.base.p.effects())
        self.assertIs(held,self.held())
        self.m.close.assert_not_called()
        self.m.poll.assert_not_called()

    def test_same_prepared_session_nonce_ports_and_expected_record(self):
        prepare=self.f.managed.prepare
        with patch.object(self.f.managed,'prepare',wraps=prepare) as preparing:
            result,(session,process,verify)=self.create()
        preparing.assert_called_once()
        run,gate=self.f.f.rows()
        value=json.loads(run['record'])
        self.assertEqual('OFFLINE_START_CREATED',result['status'])
        self.assertEqual((1,'CLOSED',digest(session['raw'])),
            (gate['revision'],gate['gate'],gate['descriptor_hash']))
        self.assertEqual((process.pid,'RUNNING',process.group),
            (value['creation']['pid'],value['creation']['stage'],value['creation']['ownerGroup']))
        self.assertEqual(value['nonce'],session['descriptor']['startNonce'])
        self.assertEqual(digest(run['record'].encode()),result['recordSha256'])
        self.assertEqual(run['record'],verify())
        self.assertIs(session,self.held()['preparation'][0])
        self.assertIs(process,self.m)
        self.assertEqual(1,self.generated)
        self.assertEqual('STARTING',self.b.row()['state'])
        self.assertEqual(1,self.b.row()['pending'])
        self.m.close.assert_not_called()
        self.m.poll.assert_not_called()

    def test_real_launch_writes_the_fixed_auxiliary_bytes(self):
        candidate=self.runtime._active_generation(self.b.row())/'runtime/launch/managed-server-candidate.json'
        read=runtime_bundle.regular_bytes
        def candidate_bytes(path,**kwargs):
            return self.b.profile if path==candidate else read(path,**kwargs)
        create=self.f.pin.create.side_effect
        def pin(raw):
            create(raw)
            (self.runtime.registry.root/'manager/control'/json.loads(raw)['runtimeRunId']).mkdir(parents=True)
        self.f.pin.create.side_effect=pin
        profile=mod_control_runtime._read_managed_profile(self.b.profile)
        def valid(candidate,context,observed):
            self.assertEqual(runtime_bundle._expand_managed_launch(profile,context),observed)
        with patch.object(runtime_control,'launch_command',runtime_bundle.launch_command), \
             patch.object(runtime_bundle,'regular_bytes',side_effect=candidate_bytes), \
             patch.object(runtime_bundle,'verify_bundle',return_value=self.b.base.p.manifest), \
             patch.object(runtime_bundle,'_read_managed_profile',return_value=profile), \
             patch.object(runtime_bundle,'validate_managed_launch',side_effect=valid), \
             patch.object(runtime_bundle,'validate_bundle_managed_launch',side_effect=lambda root,context,observed:valid(profile,context,observed)):
            _,(session,_,verify)=self.create()
        for name,raw in session['observed']['auxiliaryFiles'].items():
            self.assertEqual(raw,(Path(session['launch']['auxiliary'])/name).read_bytes())
        verify()

    def test_generated_argv_must_match_fixed_preparation(self):
        def wrong(*args,**kwargs):return self.command(*args,**kwargs)+['changed']
        with patch.object(runtime_control,'launch_command',side_effect=wrong):
            with self.assertRaises(RegistryError):self.create()
        self.assertEqual(0,self.generated)
        self.assertEqual({},self.runtime.processes)
        self.assertFalse(self.held()['returned'])
        self.assertEqual('PENDING',self.b.saved()[0]['status'])

    def test_deadline_after_adapter_preparation_generates_zero(self):
        self.preparing=lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline)
        with self.assertRaises(RegistryError):self.create()
        self.assertEqual(501,self.m.job)
        self.retained(0)

    def test_deadline_after_last_generation_comparison_generates_zero(self):
        hashing=runtime_process.command_hash
        fired=[]
        def hash_command(arguments):
            result=hashing(arguments)
            if self.m.stage=='JOB_CREATED' and inspect.currentframe().f_back.f_code.co_name=='before_create':
                fired.append(True);self.b.base.f.clock=self.b.base.f.deadline
            return result
        with patch.object(runtime_process,'command_hash',new=hash_command):
            with self.assertRaises(RegistryError):self.create()
        self.assertEqual([True],fired)
        self.retained(0)

    def test_epoch_commit_after_adapter_preparation_generates_zero(self):
        self.preparing=lambda:self.b.commit('epoch')
        with self.assertRaises(RegistryError):self.create()
        self.retained(0)

    def test_descriptor_replaced_after_adapter_preparation_generates_zero(self):
        self.preparing=lambda:setattr(self.f.pin,'written',b'changed')
        with self.assertRaises(OSError):self.create()
        self.retained(0)

    def test_commit_during_descriptor_reread_generates_zero(self):
        check=self.f.pin.check.side_effect
        def mutate(raw):
            check(raw)
            if self.m.stage=='JOB_CREATED':self.b.commit('intent')
        self.f.pin.check.side_effect=mutate
        with self.assertRaises(RegistryError):self.create()
        self.retained(0)

    def test_auxiliary_changed_after_adapter_preparation_generates_zero(self):
        self.preparing=lambda:Path(self.arguments[1][1:]).write_bytes(b'changed')
        with self.assertRaises(RegistryError):self.create()
        self.retained(0)

    def test_auxiliary_grows_after_lstat_is_bounded_before_generation(self):
        lstat=Path.lstat
        fired=[]
        def replace(path,*args,**kwargs):
            result=lstat(path,*args,**kwargs)
            if path.name=='java.args' and self.m.stage=='JOB_CREATED' and not fired:
                fired.append(True);path.write_bytes(b'x'*65533)
            return result
        with patch.object(Path,'lstat',replace):
            with self.assertRaises(ContractError):self.create()
        self.assertEqual([True],fired)
        self.retained(0)

    def test_registry_commit_during_auxiliary_reread_generates_zero(self):
        read=runtime_control.regular_bytes
        fired=[]
        def reading(path,**kwargs):
            result=read(path,**kwargs)
            if path.name=='java.args' and self.m.stage=='JOB_CREATED' and not fired:
                fired.append(True);self.b.commit('intent')
            return result
        with patch.object(runtime_control,'regular_bytes',side_effect=reading):
            with self.assertRaises(RegistryError):self.create()
        self.assertEqual([True],fired)
        self.retained(0)

    def test_environment_changed_after_adapter_preparation_generates_zero(self):
        self.preparing=lambda:self.environment.__setitem__('CHANGED','1')
        with self.assertRaises(RegistryError):self.create()
        self.retained(0)

    def test_normal_renew_after_adapter_preparation_keeps_deadline(self):
        self.preparing=lambda:self.b.base.pool.submit(self.runtime.registry.execute,'renew',
            dict(self.b.token,ttlSeconds=600),operation_id='create-renew').result(10)
        _,(_,_,verify)=self.create()
        verify()
        self.assertEqual(self.b.base.f.deadline,self.held()['deadline'])

    def test_creation_failure_keeps_original_handles_and_reservations(self):
        def fail():raise OSError('合成生成後不明')
        self.after=fail
        with self.assertRaises(OSError):self.create()
        self.assertEqual(502,self.m.handle)
        self.retained(1)

    def test_commit_after_creation_refuses_normal_return(self):
        self.after=lambda:self.b.commit('intent')
        with self.assertRaises(RegistryError):self.create()
        self.retained(1)

    def test_observed_identity_changed_before_completion_is_not_readopted(self):
        self.after=lambda:self.held()['creation_run']['identity'].__setitem__('startId','changed')
        with self.assertRaises(RegistryError):self.create()
        self.retained(1)

    def test_created_group_changed_before_completion_is_not_readopted(self):
        self.after=lambda:setattr(self.m,'group','changed')
        with self.assertRaises(RegistryError):self.create()
        self.retained(1)

    def test_command_and_hash_changed_during_preparation_generates_zero(self):
        def change():
            self.arguments.append('changed')
            self.held()['creation_run']['commandHash']=runtime_process.command_hash(self.arguments)
        self.preparing=change
        with self.assertRaises(RegistryError):self.create()
        self.retained(0)

    def test_helper_return_binding_is_not_readopted_after_yield(self):
        create=self.runtime._create
        @contextmanager
        def intercepted(*args,**kwargs):
            with create(*args,**kwargs) as result:
                self.m.handle=999
                yield result
        with patch.object(self.runtime,'_create',side_effect=intercepted):
            with self.assertRaises(RegistryError):self.create()
        self.retained(1)

    def test_deadline_after_creation_refuses_normal_return(self):
        self.after=lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline)
        with self.assertRaises(RegistryError):self.create()
        self.retained(1)

    def final_copy(self,change):
        copy=offline_control.deepcopy
        fired=[]
        def copying(value):
            result=copy(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_START_CREATED':
                fired.append(True);change()
            return result
        with patch.object(offline_control,'deepcopy',side_effect=copying):
            with self.assertRaises(RegistryError):self.create()
        self.assertEqual([True],fired)
        self.retained(1)

    def test_final_copy_handle_change_refuses_return(self):
        self.final_copy(lambda:setattr(self.m,'handle',999))

    def test_final_copy_process_object_change_refuses_return(self):
        self.final_copy(lambda:self.runtime.processes.__setitem__(self.held()['run']['runId'],Mock()))

    def test_final_copy_identity_object_change_refuses_return(self):
        self.final_copy(lambda:self.held()['creation_run'].__setitem__('identity',dict(self.held()['creation_run']['identity'])))

    def test_final_copy_command_change_refuses_return(self):
        self.final_copy(lambda:self.arguments.append('changed'))

    def test_final_copy_environment_change_refuses_return(self):
        self.final_copy(lambda:self.environment.__setitem__('CHANGED','1'))

    def test_deadline_after_last_return_comparison_refuses_return(self):
        canonical=runtime_control.canonical
        calls=[]
        def encoding(value):
            result=canonical(value)
            held=self.offline.start_records.get(self.f.f.key,{})
            if inspect.currentframe().f_back.f_code.co_name=='check' and held.get('creation') and value is held.get('creation_run'):
                calls.append(True)
                if len(calls)==2:self.b.base.f.clock=self.b.base.f.deadline
            return result
        with patch.object(runtime_control,'canonical',new=encoding):
            with self.assertRaises(RegistryError):self.create()
        self.assertEqual(2,len(calls))
        self.retained(1)

    def test_final_copy_original_helper_change_refuses_return(self):
        self.final_copy(lambda:setattr(self.b.base.p.reader,'ready_check_release',{}))

    def test_final_copy_descriptor_change_refuses_return(self):
        self.final_copy(lambda:self.held()['preparation'][0].__setitem__('raw',b'changed'))

    def read_exit(self,change):
        transaction=self.runtime._transaction
        fired=[]
        @contextmanager
        def exiting(*args,**kwargs):
            with transaction(*args,**kwargs) as db:yield db
            if kwargs.get('read_only') and self.offline.start_records.get(self.f.f.key,{}).get('creation') and not fired:
                fired.append(True);change()
        with patch.object(self.runtime,'_transaction',side_effect=exiting):
            with self.assertRaises(RegistryError):self.create()
        self.assertEqual([True],fired)
        self.retained(1)

    def test_final_read_exit_checks_new_resources(self):
        self.read_exit(lambda:setattr(self.m,'job',999))

    def test_final_read_exit_checks_original_helper(self):
        self.read_exit(lambda:setattr(self.b.base.p.reader,'ready_check_release',{}))

    def test_final_read_exit_checks_original_approval(self):
        self.read_exit(lambda:self.b.approval.__setitem__('artifactId','f'*64))

    def test_run_record_cas_rejects_competing_original(self):
        record=self.runtime._record
        fired=[]
        def writing(run,**kwargs):
            if kwargs.get('expected') is not None and not fired:
                fired.append(True)
                def commit():
                    with self.runtime.registry.transaction() as db:
                        db.execute('UPDATE runs SET record=? WHERE id=?',('{}',run['runId']))
                self.b.base.pool.submit(commit).result(10)
            return record(run,**kwargs)
        with patch.object(self.runtime,'_record',side_effect=writing):
            with self.assertRaises(RegistryError):self.create()
        self.assertEqual(0,self.generated)
        self.assertEqual([True],fired)

    def test_write_pure_comparison_rejects_approval_change(self):
        transaction=self.runtime._transaction
        fired=[]
        @contextmanager
        def entering(*args,**kwargs):
            with transaction(*args,**kwargs) as db:
                if not kwargs.get('read_only') and self.offline.start_records.get(self.f.f.key,{}).get('creation_run') and not fired:
                    fired.append(True);self.b.approval['artifactId']='f'*64
                yield db
        with patch.object(self.runtime,'_transaction',side_effect=entering):
            with self.assertRaises(RegistryError):self.create()
        self.assertEqual([True],fired)
        self.assertNotIn('commandHash',json.loads(self.f.f.rows()[0]['record']))
        self.assertEqual(0,self.generated)

    def test_real_owned_start_preparation_deadline_generates_zero(self):
        self.real_owned_preparation(lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline))

    def test_real_owned_start_preparation_commit_generates_zero(self):
        self.real_owned_preparation(lambda:self.b.commit('epoch'))

    def real_owned_preparation(self,change):
        process=runtime_process.OwnedProcess()
        def job(*_):change();return 601
        with patch.object(self.adapter,'OwnedProcess',side_effect=lambda:self.process if not self.f.managed.sessions else process), \
             patch.object(runtime_process,'security_descriptor',return_value=nullcontext(None)), \
             patch.object(runtime_process.kernel,'CreateJobObjectW',side_effect=job), \
             patch.object(runtime_process._winapi,'CreateProcess',side_effect=AssertionError('実process生成禁止')) as create, \
             patch.object(runtime_process.os,'set_handle_inheritable'), \
             patch.object(runtime_process.msvcrt,'get_osfhandle',return_value=1), \
             patch.object(runtime_process.ctypes,'get_last_error',return_value=5):
            with self.assertRaises(RegistryError):self.create()
        create.assert_not_called()
        self.assertEqual((601,None,'JOB_CREATED'),(process.job,process.pid,process.stage))
        self.assertIs(process,self.runtime.processes[self.held()['run']['runId']])
        self.assertFalse(self.held()['returned'])


if __name__=='__main__':unittest.main()
