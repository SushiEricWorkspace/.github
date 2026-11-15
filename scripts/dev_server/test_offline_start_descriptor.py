"""新run/ports正本からdescriptor準備への接続を合成nativeと実Registryで検証する。"""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import mod_control_runtime, offline_control
from . import test_offline_start_record as records
from .build_inputs import canonical, digest
from .mod_control_runtime import ManagedRuntime
from .mod_control_transport import ModControlTransport
from .registry import RegistryError


class StartDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.f = records.StartRecordTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.b, self.runtime, self.offline = self.f.b, self.f.runtime, self.f.offline
        self.native = self.b.base.p.native
        self.pin = SimpleNamespace(handle=11, nodes=[], writer=None, close_unknown=False,
                                   create=Mock(), check=Mock(), close=Mock())
        self.listener = SimpleNamespace(handle=12, active=False, open=Mock(), close=Mock())
        self.controller = SimpleNamespace(handle=13, open=Mock(), close=Mock())
        self.pin.written = None
        self.pin.create.side_effect = lambda raw:setattr(self.pin,'written',raw)
        def check(raw):
            if raw != self.pin.written:raise OSError('元descriptor原文が変更された')
        self.pin.check.side_effect = check
        self.native.pin_controller = Mock(return_value=self.controller)
        self.native.descriptor = Mock(return_value=self.pin)
        self.native.listen = Mock(return_value=self.listener)
        self.transport = ModControlTransport([self.b.token['instanceId']], native=self.native)
        self.managed = ManagedRuntime(self.runtime, {}, transport=self.transport)
        self.managed.approvals = {self.b.token['instanceId']: self.b.approval}
        self.managed.approved = lambda row,generation:(self.b.approval,self.b.profile)
        self.runtime.managed = self.managed
        self.enterContext(patch.object(mod_control_runtime, 'process_identity', return_value={
            'hostHash':'a'*64, 'bootId':'boot', 'pid':101, 'startId':'start'}))
        self.enterContext(patch.object(self.managed, '_world', return_value=Path(self.b.base.o.state['paths']['worldRoot'])))
        self.enterContext(patch.object(mod_control_runtime, '_read_managed_profile', return_value={
            'platform':'Windows','environment':{},'dliText':'fixture\n', 'classpath':['fixture.jar'],
            'remapClasspath':['fixture.jar'],'jvmArgs':['-Dcontrol=${controlDescriptor}','-Dproof=${proofPath}'],
            'mainClass':'FixtureMain','args':[],'argfileEncoding':'utf-8'}))

    def prepare(self, **changes):
        args = dict(operation_id=records.reservations.START,
            read_operation_id=records.reservations.readers.OPERATION,
            open_operation_id=self.b.base.o.base.opened['operationId'], ready=self.b.base.o.ready,
            deadline=self.b.base.f.deadline)
        args.update(changes)
        return self.runtime._offline_prepare_start(self.b.token, **args)

    def retained(self):
        held = self.offline.start_records[self.f.key]
        self.assertTrue(held['recorded'])
        self.assertFalse(held['returned'])
        self.assertEqual({}, self.runtime.processes)
        self.assertEqual('PENDING', self.b.saved()[0]['status'])
        self.assertEqual(1, len(self.b.saved()[1]))
        effects = self.b.base.p.effects()
        with self.assertRaises(RegistryError): self.prepare()
        with self.assertRaises(RegistryError): self.prepare(operation_id=records.reservations.OTHER)
        self.assertEqual(effects, self.b.base.p.effects())
        self.assertIs(held, self.offline.start_records[self.f.key])
        self.assertIsNotNone(self.b.base.p.reader.request)
        self.assertIsNotNone(self.b.base.p.reader.ready_check_response)

    def test_same_nonce_run_ports_and_descriptor_are_bound_without_m(self):
        try:result, (session, verify) = self.prepare()
        except TypeError as error:self.fail(f'実launch展開の補助file原文bytesを保持できません: {error}')
        self.assertIs(session,self.managed.sessions[self.b.token['instanceId']])
        self.assertEqual({'java.args','server-launch.cfg','server-remap-classpath.txt'},
                         session['observed']['auxiliaryFiles'].keys())
        self.assertTrue(all(type(raw) is bytes for raw in session['observed']['auxiliaryFiles'].values()))
        run, gate = self.f.rows()
        value = json.loads(run['record'])
        self.assertEqual('OFFLINE_START_PREPARED', result['status'])
        self.assertEqual((1,'CLOSED',digest(session['raw'])),
                         (gate['revision'],gate['gate'],gate['descriptor_hash']))
        self.assertEqual(value['nonce'],session['descriptor']['startNonce'])
        self.assertEqual(value['runId'],session['run'])
        self.assertEqual(canonical(session['descriptor']),session['raw'])
        self.assertEqual(run['record'],verify())
        self.assertEqual({},self.runtime.processes)
        self.assertEqual((1,1,1,1),self.b.base.p.effects())
        self.native.descriptor.assert_called_once()

    def test_ordinary_record_still_has_closed_zero_and_does_not_prepare(self):
        result, verify = self.f.record()
        self.assertEqual('OFFLINE_START_RECORDED',result['status'])
        self.assertEqual((0,None),(self.f.rows()[1]['revision'],self.f.rows()[1]['descriptor_hash']))
        self.native.descriptor.assert_not_called()
        verify()

    def test_deadline_during_controller_preparation_prevents_descriptor_creation(self):
        self.controller.open.side_effect = lambda *_:setattr(self.b.base.f,'clock',self.b.base.f.deadline)
        with self.assertRaises(RegistryError): self.prepare()
        self.native.descriptor.assert_not_called()
        self.retained()

    def test_registry_commit_during_controller_preparation_prevents_descriptor_creation(self):
        self.controller.open.side_effect = lambda *_:self.b.commit('epoch')
        with self.assertRaises(RegistryError): self.prepare()
        self.native.descriptor.assert_not_called()
        self.retained()

    def test_registry_commit_during_pin_check_prevents_listener_creation(self):
        self.pin.check.side_effect = lambda *_:self.b.commit('intent')
        with self.assertRaises(RegistryError): self.prepare()
        self.native.listen.assert_not_called()
        self.retained()

    def test_normal_renew_during_prepare_keeps_same_deadline(self):
        def renew(*_):
            self.b.base.pool.submit(lambda:self.runtime.registry.execute('renew',dict(self.b.token,ttlSeconds=600),
                operation_id='descriptor-renew')).result(10)
        self.controller.open.side_effect = renew
        result, (_, verify) = self.prepare()
        self.assertEqual('OFFLINE_START_PREPARED',result['status'])
        verify()
        self.assertEqual(self.b.base.f.deadline,self.offline.start_records[self.f.key]['deadline'])

    def final_copy(self, change):
        copy = offline_control.deepcopy
        changed = False
        def mutate(value):
            nonlocal changed
            result = copy(value)
            if isinstance(value,dict) and value.get('status')=='OFFLINE_START_PREPARED' and not changed:
                changed=True
                change(self.managed.sessions[self.b.token['instanceId']])
            return result
        with patch.object(offline_control,'deepcopy',side_effect=mutate):
            with self.assertRaises(RegistryError):self.prepare()
        self.assertTrue(changed)
        self.assertEqual(1,self.f.rows()[1]['revision'])
        self.retained()

    def test_final_copy_descriptor_bytes_change_refuses_return(self):
        self.final_copy(lambda session:session.__setitem__('raw',session['raw']+b' '))

    def test_final_copy_endpoint_handle_change_refuses_return(self):
        self.final_copy(lambda _:setattr(self.listener,'handle',99))

    def test_final_copy_session_replacement_refuses_return(self):
        self.final_copy(lambda session:self.managed.sessions.__setitem__(self.b.token['instanceId'],dict(session)))

    def test_final_copy_launch_object_replacement_refuses_return(self):
        self.final_copy(lambda session:session.__setitem__('launch',dict(session['launch'])))

    def test_final_copy_auxiliary_bytes_change_refuses_return(self):
        def change(session):
            files=session['observed']['auxiliaryFiles']
            files['java.args'] += b' '
        self.final_copy(change)

    def test_final_copy_original_approval_change_refuses_return(self):
        self.final_copy(lambda _:self.b.approval.__setitem__('artifactId','f'*64))

    def test_final_copy_original_helper_release_change_refuses_return(self):
        self.final_copy(lambda _:self.b.base.p.reader.ready_check_release.__setitem__('pipe','UNKNOWN'))

    def test_final_copy_deadline_refuses_return(self):
        self.final_copy(lambda _:setattr(self.b.base.f,'clock',self.b.base.f.deadline))

    def test_helper_return_certificate_cannot_be_refixated_from_session(self):
        prepare = self.managed.prepare
        def replaced(*args,**kwargs):
            packet = prepare(*args,**kwargs)
            self.listener.handle=99
            packet[0]['prepared_binding']=(packet[1][0],lambda:None)
            return packet
        with patch.object(self.managed,'prepare',side_effect=replaced):
            with self.assertRaises(RegistryError):self.prepare()
        self.assertEqual(0,self.f.rows()[1]['revision'])
        self.retained()

    def final_read(self, change=None):
        transaction = self.runtime._transaction
        regular,resources=offline_control.regular_bytes,self.offline._terminal_resources
        reads=0
        after=False
        @contextmanager
        def changed(instance,*,read_only=False):
            nonlocal reads,after
            with transaction(instance,read_only=read_only) as db:
                prepared=read_only and db.execute('SELECT 1 FROM run_admission WHERE revision=1 AND descriptor_hash IS NOT NULL').fetchone()
                yield db
            if prepared:
                reads+=1
                if reads==2:
                    after=True
                    if change is not None:change()
        def file(*args,**kwargs):
            self.assertFalse(after,'退出後は純比較だけを行う')
            return regular(*args,**kwargs)
        def native(*args,**kwargs):
            self.assertFalse(after,'退出後は元資源を再観測しない')
            return resources(*args,**kwargs)
        with patch.object(self.runtime,'_transaction',changed),patch.object(offline_control,'regular_bytes',file),patch.object(self.offline,'_terminal_resources',native):
            if change is not None:
                with self.assertRaises(RegistryError):self.prepare()
            else:
                result,(session,verify)=self.prepare()
                self.assertEqual('OFFLINE_START_PREPARED',result['status'])
                self.assertIs(session,self.managed.sessions[self.b.token['instanceId']])
        self.assertEqual(2,reads)
        if change is not None:self.retained()

    def test_final_read_exit_rechecks_original_helper_and_prepared_resources(self):
        self.final_read(lambda:setattr(self.listener,'handle',99))

    def test_final_read_exit_rechecks_original_helper_completion(self):
        self.final_read(lambda:self.b.base.p.reader.ready_check_release.__setitem__('pipe','UNKNOWN'))

    def test_final_read_exit_rechecks_approval(self):
        self.final_read(lambda:self.b.approval.__setitem__('artifactId','f'*64))

    def test_final_read_exit_success_uses_only_pure_comparison(self):
        self.final_read()

    def test_pin_original_changed_after_helper_return_is_not_used(self):
        prepare = self.managed.prepare
        def changed(*args,**kwargs):
            packet=prepare(*args,**kwargs)
            self.pin.written += b' '
            return packet
        with patch.object(self.managed,'prepare',side_effect=changed):
            with self.assertRaises(OSError):self.prepare()
        self.assertEqual(0,self.f.rows()[1]['revision'])
        self.retained()

    def write_change(self, change):
        transaction = self.runtime._transaction
        changed=False
        @contextmanager
        def writing(instance,*,read_only=False):
            nonlocal changed
            with transaction(instance,read_only=read_only) as db:
                if not read_only and self.offline.start_records.get(self.f.key,{}).get('preparation') is not None and not changed:
                    changed=True
                    change()
                yield db
        with patch.object(self.runtime,'_transaction',writing):
            with self.assertRaises(RegistryError):self.prepare()
        self.assertTrue(changed)
        self.assertEqual((0,None),(self.f.rows()[1]['revision'],self.f.rows()[1]['descriptor_hash']))
        self.retained()

    def test_admission_write_rechecks_original_approval(self):
        self.write_change(lambda:self.b.approval.__setitem__('artifactId','f'*64))

    def test_admission_write_rechecks_prepared_resources(self):
        self.write_change(lambda:setattr(self.listener,'handle',99))

    def test_commit_after_helper_return_refuses_admission_transition(self):
        prepare = self.managed.prepare
        def changed(*args,**kwargs):
            packet=prepare(*args,**kwargs)
            self.b.commit('fingerprint')
            return packet
        with patch.object(self.managed,'prepare',side_effect=changed):
            with self.assertRaises(RegistryError):self.prepare()
        self.assertEqual(0,self.f.rows()[1]['revision'])
        self.retained()

    def after_admission(self, change):
        transaction=self.runtime._transaction
        changed=False
        @contextmanager
        def writing(instance,*,read_only=False):
            nonlocal changed
            with transaction(instance,read_only=read_only) as db:
                prepared=not read_only and self.offline.start_records.get(self.f.key,{}).get('preparation') is not None
                yield db
            if prepared and not changed:
                changed=True
                change()
        return writing

    def test_registry_admission_changed_after_commit_refuses_return(self):
        def change():
            def commit():
                with self.runtime.registry.transaction() as db:
                    db.execute("UPDATE run_admission SET descriptor_hash=? WHERE run=?",('f'*64,self.offline.starts[self.f.key]['run']))
            self.b.base.pool.submit(commit).result(10)
        with patch.object(self.runtime,'_transaction',self.after_admission(change)):
            with self.assertRaises(RegistryError):self.prepare()
        self.retained()

    def test_renew_after_admission_does_not_extend_deadline(self):
        with patch.object(self.runtime,'_transaction',self.after_admission(self.b.renew)):
            result,(_,verify)=self.prepare()
        self.assertEqual('OFFLINE_START_PREPARED',result['status'])
        verify()
        self.assertEqual(self.b.base.f.deadline,self.offline.start_records[self.f.key]['deadline'])

    def test_admission_write_rolls_back_on_fixed_deadline(self):
        self.write_change(lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline))

    def test_world_binding_change_during_prepare_refuses_transition(self):
        prepare=self.managed.prepare
        def changed(*args,**kwargs):
            packet=prepare(*args,**kwargs)
            packet[0]['descriptor']['worldRoot'] += '-other'
            return packet
        with patch.object(self.managed,'prepare',side_effect=changed):
            with self.assertRaises(RegistryError):self.prepare()
        self.assertEqual(0,self.f.rows()[1]['revision'])
        self.retained()

    def changed_prepare_input(self, kind):
        prepare=self.managed.prepare
        def changed(*args,**kwargs):
            approved=self.managed.approved
            first=True
            def supplied(*values):
                nonlocal first
                approval,profile=approved(*values)
                if first:
                    first=False
                    return (dict(approval),profile) if kind=='approval' else (approval,profile+b' ')
                return approval,profile
            with patch.object(self.managed,'approved',side_effect=supplied):
                return prepare(*args,**kwargs)
        with patch.object(self.managed,'prepare',side_effect=changed):
            with self.assertRaises(RegistryError):self.prepare()
        self.assertEqual((0,None),(self.f.rows()[1]['revision'],self.f.rows()[1]['descriptor_hash']))
        self.retained()

    def test_prepared_profile_original_must_match_start_intent(self):
        self.changed_prepare_input('profile')

    def test_prepared_approval_object_must_be_original_start_approval(self):
        self.changed_prepare_input('approval')

    def changed_prepare_product(self, kind):
        create=self.transport.create
        expand=mod_control_runtime._expand_managed_launch
        decode=mod_control_runtime.wire.decode_descriptor
        identity=mod_control_runtime.process_identity.return_value
        original={}
        def owner(*args):
            run=self.offline.start_records[self.f.key]['run']
            field='manifestHash' if kind=='descriptor' else 'proofPath'
            original.update(run=run,field=field,value=run[field])
            if kind=='descriptor':run[field]='f'*64
            return identity
        def decoded(raw):
            if kind=='descriptor' and original:
                original['run'][original['field']]=original['value']
            return decode(raw)
        def created(*args,**kwargs):
            endpoint=create(*args,**kwargs)
            if kind=='launch':original['run'][original['field']]=original['value']+'-other'
            return endpoint
        def expanded(*args):
            value=expand(*args)
            original['run'][original['field']]=original['value']
            return value
        with patch.object(mod_control_runtime,'process_identity',side_effect=owner),patch.object(mod_control_runtime.wire,'decode_descriptor',side_effect=decoded),patch.object(self.transport,'create',side_effect=created),patch.object(mod_control_runtime,'_expand_managed_launch',side_effect=expanded):
            with self.assertRaises(RegistryError):self.prepare()
        self.assertTrue(original)
        self.assertEqual((0,None),(self.f.rows()[1]['revision'],self.f.rows()[1]['descriptor_hash']))
        self.retained()

    def test_prepared_descriptor_must_match_original_manifest(self):
        self.changed_prepare_product('descriptor')

    def test_prepared_launch_must_match_original_proof_path(self):
        self.changed_prepare_product('launch')


if __name__=='__main__':unittest.main()
