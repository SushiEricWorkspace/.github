"""同じstartup枠のacceptと資源遷移を合成native・実Registryで検証する。"""
from contextlib import contextmanager
from copy import deepcopy
import ctypes
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import mod_control_transport, offline_control
from . import test_offline_startup_prepare as preparation
from .test_mod_control_transport import FakeOperation
from .registry import RegistryError

ACCEPT = mod_control_transport._Endpoint.accept


class StartAcceptTests(unittest.TestCase):
    def setUp(self):
        self.f = preparation.StartupPrepareTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.b, self.runtime, self.offline = self.f.b, self.f.runtime, self.f.offline
        self.managed, self.starter = self.f.managed, self.f.starter
        self.native = self.f.f.f.native
        self.listener = self.f.f.f.listener
        self.hooks = {}
        self.managed.transport.clock = lambda: self.b.base.f.clock
        self.connections = []
        self.observation_hook = lambda stage,value:None
        def observed(endpoint, *args, **kwargs):
            observe = kwargs.pop('observe')
            def acquired(stage, value):
                if stage == 'connection': self.connections.append(value)
                observe(stage, value)
                self.observation_hook(stage,value)
            return ACCEPT(endpoint,*args,observe=acquired,**kwargs)
        self.enterContext(patch.object(mod_control_transport._Endpoint, 'accept', new=observed))
        self.peer = SimpleNamespace(handle=None, closed=False, expected=None, native=self.native)
        def opened(expected):
            self.peer.handle = 81
            self.peer.expected = deepcopy(expected)
            self.fire('opened')
        self.peer.open = Mock(side_effect=opened)
        self.peer.close = Mock(side_effect=lambda:setattr(self.peer,'closed',True))
        self.native.pin_process = Mock(return_value=self.peer)
        self.channel = SimpleNamespace(handle=self.listener.handle, listener=self.listener, closed=False)
        self.channel.close = Mock(side_effect=lambda:setattr(self.channel,'closed',True))
        def channel():
            self.listener.active = True
            self.fire('channel')
            return self.channel
        self.listener.connection = Mock(side_effect=channel)
        self.native.operations, self.native.started = [], []
        self.native.stall = -1
        self.native.complete_on_cancel = self.native.recover = False
        def operation(channel, kind, value):
            result = FakeOperation(self.native, channel, kind, value)
            self.native.operations.append(result)
            start, finish, release = result.start, result.finish, result.release
            def issue(*, before_issue=None):
                self.fire('native-prepared')
                if before_issue is not None:
                    before_issue()
                self.fire('start')
                return start()
            def finished(*args):
                value = finish(*args)
                self.fire('finished')
                return value
            def released():
                self.fire('release')
                return release()
            result.start, result.finish, result.release = issue, finished, released
            self.fire('operation')
            return result
        self.native.operation = Mock(side_effect=operation)
        def authenticate(channel, peer, expected):
            self.assertIs(channel, self.channel)
            self.assertIs(peer, self.peer)
            self.assertEqual(expected, self.peer.expected)
            self.fire('authenticate')
        self.native.authenticate = Mock(side_effect=authenticate)

    def fire(self, stage):
        self.hooks.get(stage, lambda: None)()

    def accept(self, **changes):
        records = preparation.creation.descriptors.records
        args = dict(operation_id=records.reservations.START,
            read_operation_id=records.reservations.readers.OPERATION,
            open_operation_id=self.b.base.o.base.opened['operationId'], ready=self.b.base.o.ready,
            deadline=self.b.base.f.deadline)
        args.update(changes)
        return self.runtime._offline_accept_start(self.b.token, **args)

    def candidate(self):
        return self.f.candidate()

    def retained(self):
        self.f.f.retained(1)
        endpoint = self.f.f.f.managed.sessions[self.b.token['instanceId']]['endpoint']
        self.assertIs(endpoint,self.managed.transport.endpoints[self.b.token['instanceId']])
        if self.connections:
            connection = self.connections[0]
            self.assertIs(endpoint.connection,connection)
            if self.native.pin_process.called: self.assertIs(connection.peer,self.peer)
            if self.listener.connection.called: self.assertIs(connection.channel,self.channel)
            self.peer.close.assert_not_called()
            self.channel.close.assert_not_called()
            if connection.handoff_failed:
                connection.reap()
                endpoint.reap()
                self.assertFalse(connection.close(self.b.base.f.deadline))
                self.assertIs(endpoint.connection,connection)
                self.peer.close.assert_not_called()
                self.channel.close.assert_not_called()
        effects = len(self.native.operations), len(self.native.started)
        with self.assertRaises(RegistryError): self.accept()
        self.assertEqual(effects, (len(self.native.operations),len(self.native.started)))

    def final_copy(self, change):
        copy = offline_control.deepcopy
        fired = []
        def mutate(value):
            result = copy(value)
            if isinstance(value,dict) and value.get('status') == 'OFFLINE_START_ACCEPTED':
                fired.append(True)
                change()
            return result
        with patch.object(offline_control,'deepcopy',side_effect=mutate):
            with self.assertRaises(RegistryError): self.accept()
        self.assertEqual([True],fired)
        self.retained()

    def test_same_created_m_endpoint_and_startup_accept_once(self):
        prepare = self.managed.prepare
        with patch.object(self.managed,'prepare',wraps=prepare) as preparing:
            result, (session, process, candidate, check) = self.accept()
        preparing.assert_called_once()
        self.assertEqual('OFFLINE_START_ACCEPTED',result['status'])
        self.assertIs(process,self.f.f.m)
        self.assertIs(session['endpoint'].connection,candidate['connection'])
        self.assertIs(candidate,self.candidate())
        self.assertIs(candidate['connection'].peer,self.peer)
        self.assertIs(candidate['connection'].channel,self.channel)
        self.assertTrue(candidate['connection'].authenticated)
        self.assertIsNone(candidate['connection'].pending)
        self.assertEqual(['accept'],self.native.started)
        self.assertEqual(1,len(self.native.operations))
        self.assertTrue(self.native.operations[0].released)
        self.assertEqual((1,'CLOSED'),tuple(self.f.f.f.f.rows()[1][key] for key in ('revision','gate')))
        self.assertEqual(self.f.f.f.f.rows()[0]['record'],check())
        self.assertEqual('PENDING',self.b.saved()[0]['status'])
        self.assertEqual(1,len(self.b.saved()[1]))
        self.assertEqual(1,self.f.f.generated)
        self.peer.close.assert_not_called()
        self.channel.close.assert_not_called()

    def test_public_renew_during_native_preparation_is_allowed(self):
        self.hooks['operation'] = lambda:self.b.base.pool.submit(self.runtime.registry.execute,'renew',
            dict(self.b.token,ttlSeconds=600),operation_id='accept-renew').result(10)
        _, (_, _, _, check) = self.accept()
        check()
        self.assertEqual(['accept'],self.native.started)
        self.assertEqual(self.b.base.f.deadline,self.f.f.held()['deadline'])

    def test_epoch_commit_during_native_preparation_issues_zero_accept(self):
        self.hooks['operation'] = lambda:self.b.commit('epoch')
        with self.assertRaises((RegistryError,OSError)): self.accept()
        self.assertEqual([],self.native.started)
        self.retained()

    def test_deadline_during_native_preparation_issues_zero_accept(self):
        self.hooks['operation'] = lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline)
        with self.assertRaises((RegistryError,OSError)): self.accept()
        self.assertEqual([],self.native.started)
        self.retained()

    def windows_preparation(self, change):
        class Overlapped(ctypes.Structure):
            _fields_ = [('hEvent',ctypes.c_void_p)]
        kernel = Mock()
        def event(*args):
            change()
            return 82
        kernel.CreateEventW.side_effect = event
        backend = SimpleNamespace(Overlapped=Overlapped, kernel=kernel, close_handle=Mock(),
                                  check=lambda value:self.assertTrue(value))
        self.native.operation.side_effect = lambda channel,kind,value:mod_control_transport._WinOperation(
            backend,channel,kind,value)
        with self.assertRaises((RegistryError,OSError)): self.accept()
        kernel.CreateEventW.assert_called_once()
        kernel.ConnectNamedPipe.assert_not_called()
        backend.close_handle.assert_called_once_with(82)
        self.native.authenticate.assert_not_called()
        self.retained()

    def test_actual_windows_event_preparation_deadline_issues_zero_accept(self):
        self.windows_preparation(lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline))

    def test_actual_windows_event_preparation_commit_issues_zero_accept(self):
        self.windows_preparation(lambda:self.b.commit('epoch'))

    def test_actual_macos_poll_rechecks_before_accept_syscall(self):
        accepted = SimpleNamespace(setblocking=Mock(),fileno=lambda:72)
        stream = SimpleNamespace(accept=Mock(return_value=(accepted,None)))
        self.listener.stream = stream
        def operation(channel,kind,value):
            result = mod_control_transport._MacOperation(channel,kind,value)
            start = result.start
            def begin(*, before_issue):
                def check():
                    self.b.commit('epoch')
                    before_issue()
                start(before_issue=check)
            result.start = begin
            return result
        self.native.operation.side_effect = operation
        stream.fileno = lambda:71
        with self.assertRaises((RegistryError,OSError)): self.accept()
        stream.accept.assert_not_called()
        self.retained()

    def test_descriptor_reread_commit_prevents_peer_open(self):
        check = self.f.f.f.pin.check.side_effect
        def mutate(raw):
            check(raw)
            if self.starter.sessions: self.b.commit('epoch')
        self.f.f.f.pin.check.side_effect = mutate
        with self.assertRaises(RegistryError): self.accept()
        self.native.pin_process.assert_not_called()
        self.retained()

    def test_peer_open_cannot_change_original_listener_handle(self):
        self.hooks['opened'] = lambda:setattr(self.listener,'handle',999)
        with self.assertRaises((RegistryError,OSError)): self.accept()
        self.listener.connection.assert_not_called()
        self.retained()

    def test_registry_commit_during_peer_open_prevents_channel(self):
        self.hooks['opened'] = lambda:self.b.commit('intent')
        with self.assertRaises((RegistryError,OSError)): self.accept()
        self.listener.connection.assert_not_called()
        self.retained()

    def test_channel_must_keep_original_listener_handle(self):
        self.hooks['channel'] = lambda:setattr(self.channel,'handle',999)
        with self.assertRaises((RegistryError,OSError)): self.accept()
        self.assertEqual([],self.native.started)
        self.retained()

    def test_channel_must_keep_original_listener_object(self):
        self.hooks['channel'] = lambda:setattr(self.channel,'listener',object())
        with self.assertRaises((RegistryError,OSError)): self.accept()
        self.assertEqual([],self.native.started)
        self.retained()

    def test_commit_after_accept_completion_prevents_authentication(self):
        self.hooks['finished'] = lambda:self.b.commit('epoch')
        with self.assertRaises((RegistryError,OSError)): self.accept()
        self.native.authenticate.assert_not_called()
        self.assertTrue(self.native.operations[0].released)
        self.assertIsNone(self.connections[0].pending)
        self.assertEqual(81,self.peer.handle)
        self.assertTrue(self.listener.active)
        self.retained()

    def test_deadline_after_accept_completion_prevents_authentication(self):
        self.hooks['finished'] = lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline)
        with self.assertRaises((RegistryError,OSError)): self.accept()
        self.native.authenticate.assert_not_called()
        self.assertTrue(self.native.operations[0].released)
        self.assertIsNone(self.connections[0].pending)
        self.assertEqual(81,self.peer.handle)
        self.assertTrue(self.listener.active)
        self.retained()

    def callback_failure(self, boundary):
        def fail(stage,value):
            if stage == boundary: raise RuntimeError('合成handoff失敗')
        self.observation_hook = fail
        with self.assertRaisesRegex(RuntimeError,'合成handoff失敗'): self.accept()
        self.assertTrue(self.connections[0].handoff_failed)
        self.assertTrue(self.connections[0].closing)
        self.assertEqual(81,self.peer.handle)
        self.assertFalse(self.peer.closed)
        self.assertFalse(self.channel.closed)
        self.native.authenticate.assert_not_called()
        self.retained()

    def test_callback_after_peer_open_preserves_original_connection(self):
        self.callback_failure('opened')
        self.listener.connection.assert_not_called()

    def test_callback_after_channel_acquisition_preserves_original_connection(self):
        self.callback_failure('channel')
        self.assertTrue(self.listener.active)

    def test_failed_pending_accept_is_not_polled_by_later_reap(self):
        self.native.stall = 1
        with self.assertRaises(OSError): self.accept()
        operation = self.native.operations[0]
        operation.poll = Mock()
        self.retained()
        operation.poll.assert_not_called()
        self.assertIs(self.connections[0].pending,operation)
        self.assertFalse(operation.released)

    def test_pending_accept_is_retained_without_second_issue(self):
        self.native.stall = 1
        with self.assertRaises(OSError): self.accept()
        endpoint = self.managed.sessions[self.b.token['instanceId']]['endpoint']
        self.assertIs(endpoint.connection.pending,self.native.operations[0])
        self.assertFalse(self.native.operations[0].released)
        self.retained()

    def test_release_failure_retains_original_io_without_retry(self):
        failed = Mock(side_effect=OSError('合成event解放不明'))
        self.hooks['release'] = failed
        with self.assertRaises(OSError): self.accept()
        endpoint = self.managed.sessions[self.b.token['instanceId']]['endpoint']
        self.assertIs(endpoint.connection.pending,self.native.operations[0])
        self.assertTrue(endpoint.connection.close_unknown)
        failed.assert_called_once()
        self.retained()

    def test_authentication_cannot_change_original_peer_handle(self):
        self.hooks['authenticate'] = lambda:setattr(self.peer,'handle',999)
        with self.assertRaises((RegistryError,OSError)): self.accept()
        self.retained()

    def test_final_copy_connection_replacement_is_rejected(self):
        self.final_copy(lambda:self.candidate().update(connection=object()))

    def test_final_copy_peer_handle_change_is_rejected(self):
        self.final_copy(lambda:setattr(self.peer,'handle',999))

    def test_final_copy_channel_handle_change_is_rejected(self):
        self.final_copy(lambda:setattr(self.channel,'handle',999))

    def test_final_copy_listener_active_change_is_rejected(self):
        self.final_copy(lambda:setattr(self.listener,'active',False))

    def test_final_copy_original_reader_change_is_rejected(self):
        self.final_copy(lambda:self.b.base.p.reader.ready_check_release.clear())

    def test_final_copy_original_descriptor_resource_change_is_rejected(self):
        self.final_copy(lambda:setattr(self.f.f.f.pin,'handle',999))

    def test_final_copy_io_origin_change_is_rejected(self):
        self.final_copy(lambda:setattr(self.native.operations[0],'kind','read'))

    def test_final_copy_io_release_state_change_is_rejected(self):
        self.final_copy(lambda:setattr(self.native.operations[0],'released',False))

    def test_final_copy_clock_replacement_is_rejected(self):
        self.final_copy(lambda:setattr(self.managed.transport,'clock',lambda:0))

    def test_final_copy_handoff_failure_flag_is_rejected(self):
        self.final_copy(lambda:setattr(self.candidate()['connection'],'handoff_failed',True))

    def test_final_copy_deadline_is_rejected(self):
        self.final_copy(lambda:setattr(self.b.base.f,'clock',self.b.base.f.deadline))

    def test_connection_cannot_be_rebased_after_transport_return(self):
        def replace(endpoint,*args,**kwargs):
            result = ACCEPT(endpoint,*args,**kwargs)
            result.peer.handle = 999
            return result
        with patch.object(mod_control_transport._Endpoint,'accept',new=replace):
            with self.assertRaises(RegistryError): self.accept()
        self.retained()

    def test_connection_packet_replacement_before_startup_adoption_is_rejected(self):
        prepare = self.managed.prepare
        def replace(*args, **kwargs):
            packet = prepare(*args, **kwargs)
            def changed(*args, **kwargs):
                result = packet[2](*args, **kwargs)
                return object(), result[1]
            return packet[:2] + (changed,)
        with patch.object(self.managed,'prepare',side_effect=replace):
            with self.assertRaises(RegistryError): self.accept()
        self.retained()

    def test_accepted_final_read_exit_rechecks_connection(self):
        transaction = self.runtime._transaction
        fired = []
        @contextmanager
        def mutate(instance, *, read_only=False):
            frame = inspect.currentframe().f_back
            while frame and frame.f_code.co_name != 'verify_accepted': frame = frame.f_back
            with transaction(instance,read_only=read_only) as connection: yield connection
            if read_only and frame and not fired:
                fired.append(True)
                self.channel.handle = 999
        with patch.object(self.runtime,'_transaction',side_effect=mutate):
            with self.assertRaises(RegistryError): self.accept()
        self.assertEqual([True],fired)
        self.retained()
