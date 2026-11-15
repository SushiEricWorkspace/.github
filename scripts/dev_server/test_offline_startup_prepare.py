"""固定M生成証拠からCLOSED/1のstartup受入を実Registryと合成nativeで検証する。"""
from contextlib import contextmanager
from copy import deepcopy
import inspect
import json
import unittest
from unittest.mock import Mock, patch

from . import mod_control_runtime, mod_control_startup, offline_control, runtime_bundle
from . import test_offline_start_creation as creation
from .build_inputs import canonical, digest
from .registry import RegistryError
from .test_mod_control_wire_v2 import VECTORS


class StartupPrepareTests(unittest.TestCase):
    def setUp(self):
        self.f = creation.StartCreationTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.b, self.runtime, self.offline = self.f.b, self.f.runtime, self.f.offline
        self.managed = self.f.f.managed
        self.starter = self.managed.startup
        evidence = deepcopy(VECTORS['contexts']['Windows']['expectedEvidence'])
        evidence['profileSha256'] = digest(self.b.profile)
        self.b.approval['expectedEvidence'] = evidence
        self.validation = lambda: None
        self.owner_hook = lambda: None
        def owner(native, adapter, pid):
            if pid == 98765:
                self.owner_hook()
                return dict(hostHash='a'*64, bootId='boot', pid=pid, startId='original')
            return dict(hostHash='a'*64, bootId='boot', pid=101, startId='start')
        self.enterContext(patch.object(mod_control_runtime, 'process_identity', side_effect=owner))
        profile = mod_control_runtime._read_managed_profile(self.b.profile)
        self.enterContext(patch.object(mod_control_startup, '_read_managed_profile', return_value=profile))
        manifest = deepcopy(self.b.base.p.manifest)
        manifest['files'].append(dict(path='launch/managed-server-candidate.json',
                                     sha256=digest(self.b.profile), size=len(self.b.profile)))
        def valid(value, launch, observed):
            self.assertEqual(runtime_bundle._expand_managed_launch(profile, launch), observed)
            self.validation()
        self.enterContext(patch.object(mod_control_startup, 'validate_managed_launch', side_effect=valid))
        self.enterContext(patch.object(mod_control_startup, 'validate_bundle_managed_launch',
                                     side_effect=valid))
        self.enterContext(patch.object(mod_control_startup, 'verify_bundle', return_value=manifest))
        self.accept = self.enterContext(patch('dev_server.mod_control_transport._Endpoint.accept',
                                             side_effect=AssertionError('接続交換は後続スライス')))

    def prepare(self, **changes):
        records = creation.descriptors.records
        args = dict(operation_id=records.reservations.START,
            read_operation_id=records.reservations.readers.OPERATION,
            open_operation_id=self.b.base.o.base.opened['operationId'], ready=self.b.base.o.ready,
            deadline=self.b.base.f.deadline)
        args.update(changes)
        return self.runtime._offline_prepare_handshake(self.b.token, **args)

    def retained(self):
        self.f.retained(1)
        self.accept.assert_not_called()

    def final_copy(self, change):
        copy = offline_control.deepcopy
        fired = []
        def mutate(value):
            result = copy(value)
            if isinstance(value, dict) and value.get('status') == 'OFFLINE_STARTUP_PREPARED':
                fired.append(True)
                change()
            return result
        with patch.object(offline_control, 'deepcopy', side_effect=mutate):
            with self.assertRaises(RegistryError): self.prepare()
        self.assertEqual([True], fired)
        self.retained()

    def candidate(self):
        return self.starter.sessions[self.b.token['instanceId']]

    def test_same_created_m_closed_one_and_original_reservations(self):
        prepare = self.managed.prepare
        with patch.object(self.managed, 'prepare', wraps=prepare) as preparing:
            result, (session, process, candidate, check) = self.prepare()
        preparing.assert_called_once()
        self.assertEqual('OFFLINE_STARTUP_PREPARED', result['status'])
        self.assertIs(session, self.f.held()['creation'][0])
        self.assertIs(process, self.f.m)
        self.assertIs(candidate, self.candidate())
        self.assertIs(candidate['endpoint'], session['endpoint'])
        self.assertEqual((1, 'CLOSED', digest(session['raw'])),
            tuple(self.f.f.f.rows()[1][key] for key in ('revision','gate','descriptor_hash')))
        self.assertEqual((98765, 'original'),
            (candidate['context']['owner']['pid'], candidate['context']['owner']['startId']))
        self.assertEqual(digest(canonical(candidate['context'])), result['contextSha256'])
        self.assertEqual(self.b.saved()[0]['fingerprint'], candidate['origin'][1])
        self.assertEqual(self.f.f.f.rows()[0]['record'], check())
        self.assertFalse(candidate['active'])
        self.assertIsNone(candidate['connection'])
        self.assertIsNone(candidate['request'])
        self.assertEqual('PENDING', self.b.saved()[0]['status'])
        self.assertEqual(1, len(self.b.saved()[1]))
        self.assertEqual(1, self.f.generated)
        self.accept.assert_not_called()

    def test_normal_renew_during_profile_validation_and_after_return(self):
        calls = []
        def renew():
            operation = 'startup-renew-' + str(len(calls))
            calls.append(operation)
            self.b.base.pool.submit(self.runtime.registry.execute, 'renew',
                dict(self.b.token, ttlSeconds=600), operation_id=operation).result(10)
        self.validation = renew
        _, (_, _, _, check) = self.prepare()
        renew()
        check()
        self.assertEqual(3, len(calls))
        self.assertEqual(self.b.base.f.deadline, self.f.held()['deadline'])

    def test_registry_epoch_commit_during_profile_validation(self):
        self.validation = lambda: self.b.commit('epoch')
        with self.assertRaises(RegistryError): self.prepare()
        self.retained()

    def test_deadline_during_profile_validation(self):
        self.validation = lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline)
        with self.assertRaises(RegistryError): self.prepare()
        self.retained()

    def test_deadline_during_native_owner_observation(self):
        self.owner_hook = lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline)
        with self.assertRaises(RegistryError): self.prepare()
        self.retained()

    def test_native_owner_must_match_generated_identity(self):
        owner = mod_control_runtime.process_identity.side_effect
        def wrong(native, adapter, pid):
            result = owner(native, adapter, pid)
            if pid == 98765: result['pid'] += 1
            return result
        with patch.object(mod_control_runtime, 'process_identity', side_effect=wrong):
            with self.assertRaises(RegistryError): self.prepare()
        self.retained()

    def test_approval_changed_inside_startup_write(self):
        transaction = self.runtime._transaction
        @contextmanager
        def mutate(instance, *, read_only=False):
            with transaction(instance, read_only=read_only) as connection:
                if not read_only and self.starter.sessions:
                    self.b.approval['expectedEvidence']['profileSha256'] = '0'*64
                yield connection
        with patch.object(self.runtime, '_transaction', side_effect=mutate):
            with self.assertRaises(RegistryError): self.prepare()
        self.retained()

    def test_context_changed_after_return_copy(self):
        self.final_copy(lambda: self.candidate()['context']['owner'].update(pid=1))

    def test_launch_changed_after_return_copy(self):
        self.final_copy(lambda: self.candidate()['launch_context'].update(startNonce='changed'))

    def test_observed_changed_after_return_copy(self):
        self.final_copy(lambda: self.candidate()['observed']['auxiliaryFiles'].clear())

    def test_startup_resource_changed_after_return_copy(self):
        self.final_copy(lambda: self.candidate().update(endpoint=object()))

    def test_original_m_resource_changed_after_return_copy(self):
        self.final_copy(lambda: setattr(self.f.m, 'handle', 999))

    def test_original_helper_changed_after_return_copy(self):
        self.final_copy(lambda: self.b.base.p.reader.ready_check_release.clear())

    def test_checkpoint_changed_after_return_copy(self):
        self.final_copy(lambda: self.candidate().update(checkpoint=None))

    def test_deadline_after_return_copy(self):
        self.final_copy(lambda: setattr(self.b.base.f, 'clock', self.b.base.f.deadline))

    def test_startup_binding_cannot_be_rebased_after_helper_return(self):
        admit = self.starter.admit
        def replace(*args, **kwargs):
            packet = admit(*args, **kwargs)
            packet[0]['observed']['argv'].append('changed')
            return packet
        with patch.object(self.starter, 'admit', side_effect=replace):
            with self.assertRaises(RegistryError): self.prepare()
        self.retained()

    def test_completed_startup_memory_checked_after_final_read_exit(self):
        transaction = self.runtime._transaction
        fired = []
        @contextmanager
        def mutate(instance, *, read_only=False):
            frame = inspect.currentframe().f_back
            # contextmanagerの呼出元を辿り、完成済みstartupの最終readだけを対象にする。
            while frame and frame.f_code.co_name != 'check': frame = frame.f_back
            with transaction(instance, read_only=read_only) as connection:
                yield connection
            if read_only and frame and frame.f_code.co_filename == mod_control_startup.__file__ and not fired:
                fired.append(True)
                self.candidate()['hello'] = b'changed'
        with patch.object(self.runtime, '_transaction', side_effect=mutate):
            with self.assertRaises(RegistryError): self.prepare()
        self.assertEqual([True], fired)
        self.retained()

    def test_callback_keeps_initial_fixed_binding(self):
        _, (_, _, candidate, check) = self.prepare()
        candidate['observed']['argv'].append('changed')
        with self.assertRaises(RegistryError): check()
        self.accept.assert_not_called()
