"""Mod専用transportのmock検証。実socket/process/native APIは呼ばない。"""
import copy
import ctypes
import hashlib
import json
from pathlib import PurePosixPath
from types import SimpleNamespace
import unittest
import threading
from unittest.mock import Mock, call, patch

from dev_server import mod_control_transport as transport


def identity(pid=12):
    return {'hostHash': 'a' * 64, 'bootId': 'boot', 'pid': pid, 'startId': '12345'}


def descriptor():
    root = '/private/managed'
    generation = root + '/instances/slot/generations/gen-1'
    value = {'version': 1, 'purpose': 'mod-control', 'descriptorId': 'descriptor-1', 'managementRoot': root,
             'instanceId': 'slot', 'leaseId': 'lease-1', 'epoch': 1, 'generationId': 'gen-1', 'runtimeRunId': 'run-1',
             'startNonce': 'nonce-1', 'artifactId': 'artifact-1', 'runtimeManifestHash': 'b' * 64,
             'worldRoot': generation + '/world', 'shopDirectory': generation + '/world/shop_data',
             'controller': identity(), 'endpoint': {'transport': 'macos-unix',
                                                   'address': root + '/manager/control/run-1/mod-control.sock'}}
    raw = json.dumps(value, separators=(', ', ': ')).encode()
    keys = ['managementRoot', 'instanceId', 'leaseId', 'epoch', 'generationId', 'runtimeRunId', 'controller']
    context = {k: copy.deepcopy(value[k]) for k in keys}
    context['descriptorSha256'] = hashlib.sha256(raw).hexdigest()
    return context, raw


class FakeOperation:
    def __init__(self, native, channel, kind, value):
        self.native, self.channel, self.kind, self.value = native, channel, kind, value
        self.terminal, self.transferred, self.released = False, 0, False
        self.cancelled = 0
        self.pending = native.stall == len(native.operations) + 1

    def start(self):
        self.native.started.append(self.kind)

    def finish(self, deadline, clock):
        if self.pending:
            raise TimeoutError('mock I/O pending')
        self.terminal = True
        if self.kind == 'read':
            count = min(self.value, self.native.chunk, len(self.channel.incoming))
            data = self.channel.incoming[:count]
            self.channel.incoming = self.channel.incoming[count:]
            self.transferred = count
            return data
        if self.kind == 'write':
            self.transferred = min(len(self.value), self.native.chunk)
            self.channel.sent += self.value[:self.transferred]
            return self.transferred
        return None

    def cancel(self):
        self.cancelled += 1
        if self.native.complete_on_cancel:
            self.terminal = True
            self.transferred = 2

    def poll(self):
        if self.native.recover:
            self.terminal = True

    def release(self):
        if not self.terminal:
            raise AssertionError('未終端を解放しました')
        self.released = True


class FakeNative:
    transport = 'macos-unix'
    def __init__(self):
        self.pin = Mock()
        self.controller = Mock()
        self.peer = Mock()
        self.listener = Mock()
        self.channel = SimpleNamespace(incoming=b'', sent=b'', close=Mock())
        self.listener.connection.return_value = self.channel
        self.operations, self.started = [], []
        self.chunk, self.stall = 3, -1
        self.complete_on_cancel = self.recover = False
        self.pin_controller, self.pin_process = Mock(return_value=self.controller), Mock(return_value=self.peer)
        self.descriptor = Mock(return_value=self.pin)
        self.listen = Mock(return_value=self.listener)
        self.authenticate = Mock()

    def operation(self, channel, kind, value):
        operation = FakeOperation(self, channel, kind, value)
        self.operations.append(operation)
        return operation


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeNative()
        self.owner = transport.ModControlTransport(['slot'], native=self.native, clock=lambda: 1)
        self.context, self.raw = descriptor()

    def connect(self):
        self.endpoint = self.owner.create(self.context, self.raw)
        self.connection = self.endpoint.accept(identity(13), 10)
        return self.connection

    def tearDown(self):
        self.native.recover = True
        self.owner.close(10)

    def test_context_never_comes_from_descriptor(self):
        for key, value in [('managementRoot', '/outside'), ('runtimeRunId', 'other'), ('leaseId', 'other'),
                           ('generationId', 'other'), ('epoch', 2), ('controller', identity(99)), ('descriptorSha256', 'f' * 64)]:
            with self.subTest(key=key):
                context = copy.deepcopy(self.context)
                context[key] = value
                with self.assertRaises(OSError):
                    self.owner.create(context, self.raw)
        self.native.descriptor.assert_not_called()
        self.native.pin_controller.assert_not_called()

    def test_context_and_expected_m_are_copied(self):
        endpoint = self.owner.create(self.context, self.raw)
        self.context['controller']['pid'] = 99
        self.assertEqual(12, endpoint.context['controller']['pid'])
        expected = identity(13)
        connection = endpoint.accept(expected, 10)
        expected['pid'] = 99
        self.assertEqual(13, connection.expected['pid'])

    def test_wrong_platform_before_descriptor_creation(self):
        self.native.transport = 'windows-pipe'
        with self.assertRaises(transport.TransportError):
            self.owner.create(self.context, self.raw)
        self.native.descriptor.assert_not_called()
        self.native.controller.close.assert_called_once()

    def test_native_p_and_m_failure_before_frame(self):
        self.native.pin_controller.side_effect = PermissionError('P')
        with self.assertRaises(PermissionError):
            self.owner.create(self.context, self.raw)
        self.native.pin_controller.side_effect = None
        endpoint = self.owner.create(self.context, self.raw)
        self.native.authenticate.side_effect = PermissionError('同UID別M')
        with self.assertRaises(PermissionError):
            endpoint.accept(identity(13), 10)
        self.assertEqual(['accept'], self.native.started)
        self.assertIsNone(endpoint.connection)

    def test_descriptor_existing_or_pin_mismatch_does_not_listen(self):
        self.native.descriptor.side_effect = FileExistsError()
        with self.assertRaises(FileExistsError):
            self.owner.create(self.context, self.raw)
        self.native.listen.assert_not_called()
        self.native.descriptor.side_effect = None
        self.native.pin.check.side_effect = PermissionError('原bytes')
        with self.assertRaises(PermissionError):
            self.owner.create(self.context, self.raw)
        self.native.listen.assert_not_called()

    def test_finite_inventory_and_one_run_slot(self):
        endpoint = self.owner.create(self.context, self.raw)
        with self.assertRaises(transport.TransportError):
            self.owner.create(self.context, self.raw)
        expected = identity(13)
        endpoint.accept(expected, 10)
        with self.assertRaises(transport.TransportError):
            endpoint.accept(expected, 10)
        self.assertEqual(1, len(self.native.operations))

    def test_partial_header_and_body_keep_original_bytes(self):
        connection = self.connect()
        body = b'{ "version":2, "value":"original bytes" }'
        frame = len(body).to_bytes(4, 'big') + body
        self.native.channel.incoming = frame
        self.assertEqual(frame, connection.receive_frame(10))
        self.assertEqual(b'', self.native.channel.incoming)
        self.assertTrue(all(op.released for op in self.native.operations))

    def test_body_bound_checked_immediately_after_four_header_bytes(self):
        for size in [0, 65533, 2**32 - 1]:
            with self.subTest(size=size):
                connection = self.connect()
                self.native.channel.incoming = size.to_bytes(4, 'big') + b'x' * 32
                with self.assertRaises(transport.TransportError) as error:
                    connection.receive_frame(10)
                self.assertEqual(4, error.exception.transferred)
                self.assertEqual(b'x' * 32, self.native.channel.incoming)
                connection.close(10)
                self.endpoint.close(10)

    def test_maximum_frame_is_header_inclusive(self):
        connection = self.connect()
        self.native.chunk = 65536
        frame = (65532).to_bytes(4, 'big') + b'x' * 65532
        self.native.channel.incoming = frame
        self.assertEqual(65536, len(connection.receive_frame(10)))
        self.assertEqual(65536, connection.send_frame(frame, 10))
        self.assertEqual(frame, self.native.channel.sent)

    def test_partial_send_and_no_extra_frame(self):
        connection = self.connect()
        frame = b'\0\0\0\x05abcde'
        self.assertEqual(len(frame), connection.send_frame(frame, 10))
        self.assertEqual(frame, self.native.channel.sent)
        before = len(self.native.operations)
        for raw in [b'\0\0\0\0', b'\0\0\0\x01xx', b'\0\0\0\1' + b'x' * 65533]:
            with self.assertRaises(transport.TransportError):
                connection.send_frame(raw, 10)
        self.assertEqual(before, len(self.native.operations))

    def test_expired_deadline_has_no_new_io(self):
        connection = self.connect()
        before = len(self.native.operations)
        with self.assertRaises(transport.TransportError):
            connection.receive_frame(1)
        self.assertEqual(before, len(self.native.operations))

    def test_pending_retains_buffer_connection_and_slot_until_terminal(self):
        connection = self.connect()
        self.native.stall = len(self.native.operations) + 1
        with self.assertRaises(transport.TransportError) as error:
            connection.receive_frame(10)
        operation = connection.pending
        self.assertTrue(error.exception.cancel_pending)
        self.assertIs(operation, self.native.operations[-1])
        self.assertFalse(self.owner.close(10))
        self.assertFalse(operation.released)
        self.native.channel.close.assert_not_called()
        self.native.peer.close.assert_not_called()
        with self.assertRaises(transport.TransportError):
            self.endpoint.accept(identity(13), 10)
        with self.assertRaises(transport.TransportError):
            self.owner.create(self.context, self.raw)
        self.native.recover = True
        self.owner.reap()
        self.assertTrue(operation.released)
        self.assertTrue(self.owner.close(10))
        self.assertEqual({}, self.owner.endpoints)

    def test_cancel_race_reports_completed_bytes_without_retry(self):
        connection = self.connect()
        self.native.stall = len(self.native.operations) + 1
        self.native.complete_on_cancel = True
        before = len(self.native.operations)
        with self.assertRaises(transport.TransportError) as error:
            connection.send_frame(b'\0\0\0\x01x', 10)
        self.assertEqual(2, error.exception.transferred)
        self.assertFalse(error.exception.cancel_pending)
        self.assertEqual(before + 1, len(self.native.operations))
        self.assertIsNone(connection.pending)

    def test_peer_change_after_receive_or_send_closes_connection(self):
        connection = self.connect()
        self.native.channel.incoming = b'\0\0\0\1x'
        self.native.authenticate.side_effect = [None, PermissionError('PID再利用')]
        with self.assertRaises(PermissionError):
            connection.receive_frame(10)
        self.assertTrue(connection.closing)
        with self.assertRaises(transport.TransportError):
            connection.send_frame(b'\0\0\0\1x', 10)

    def test_close_failure_keeps_owner(self):
        self.connect()
        self.native.pin.close.side_effect = OSError('close')
        self.assertFalse(self.owner.close(10))
        self.assertIn('slot', self.owner.endpoints)
        self.native.pin.close.side_effect = None
        self.assertFalse(self.owner.close(10))
        self.assertEqual(1, self.native.pin.close.call_count)

    def test_simultaneous_call_is_rejected_without_wait_queue(self):
        connection = self.connect()
        with connection.lock:
            with self.assertRaises(transport.TransportError):
                connection.send_frame(b'\0\0\0\1x', 10)
            self.assertFalse(connection.close(1))
        self.assertTrue(connection.close(10))


class NativeBoundaryTests(unittest.TestCase):
    def test_mac_same_socket_pid_uid_and_start_are_all_required(self):
        native = object.__new__(transport._MacNative)
        native.system = Mock()
        native.identity = Mock(return_value=123)
        native.system.getpeereid.side_effect = lambda fd, uid, gid: (setattr(uid._obj, 'value', 123), 0)[1]
        channel = SimpleNamespace(stream=Mock())
        channel.stream.fileno.return_value = 19
        channel.stream.getsockopt.return_value = (13).to_bytes(4, 'little', signed=True)
        with patch.object(transport.os, 'getuid', return_value=123, create=True):
            native.authenticate(channel, None, identity(13))
            channel.stream.getsockopt.return_value = (14).to_bytes(4, 'little', signed=True)
            with self.assertRaises(transport.TransportError):
                native.authenticate(channel, None, identity(13))
        native.system.getpeereid.assert_called_with(19, unittest.mock.ANY, unittest.mock.ANY)
        native.identity.assert_called_once_with(identity(13))

    def test_windows_same_pipe_pid_and_pinned_handle_are_both_checked(self):
        native = object.__new__(transport._WindowsNative)
        native.kernel = Mock()
        native.kernel.GetNamedPipeClientProcessId.side_effect = lambda h, pid: (setattr(pid._obj, 'value', 13), 1)[1]
        channel, peer = SimpleNamespace(handle=99), Mock()
        native.authenticate(channel, peer, identity(13))
        peer.check.assert_called_once_with(identity(13))
        with self.assertRaises(transport.TransportError):
            native.authenticate(channel, peer, identity(14))
        self.assertEqual(1, peer.check.call_count)

    def test_mac_descriptor_rechecks_fd_security_hash_and_named_identity(self):
        pin = object.__new__(transport._MacPin)
        pin.fd, pin.parent, pin.path = 10, 20, PurePosixPath('/root/mod-control.json')
        pin.nodes = [(20, None, None, (1, 20), True)]
        parent = SimpleNamespace(st_dev=1, st_ino=20, st_uid=123, st_mode=0o40700)
        file = SimpleNamespace(st_dev=1, st_ino=10, st_uid=123, st_mode=0o100600, st_nlink=1, st_size=3)
        def info(fd):return parent if fd == 20 else file
        with patch.object(transport.os, 'getuid', return_value=123, create=True), patch.object(transport.os, 'fstat', side_effect=info), \
                patch.object(transport.os, 'stat', return_value=file), patch.object(transport.os, 'lseek'), \
                patch.object(transport.os, 'read', side_effect=[b'ab', b'c', b'']):
            pin.check(b'abc')
        for field, value in [('st_nlink', 2), ('st_mode', 0o100644), ('st_uid', 124), ('st_size', 4)]:
            with self.subTest(field=field):
                wrong = copy.copy(file)
                setattr(wrong, field, value)
                with patch.object(transport.os, 'getuid', return_value=123, create=True), patch.object(transport.os, 'fstat', side_effect=lambda fd: parent if fd == 20 else wrong):
                    with self.assertRaises(transport.TransportError):
                        pin.check(b'abc')

    def test_mac_descriptor_parent_permission_change_rejected(self):
        pin = object.__new__(transport._MacPin)
        pin.nodes = [(20, None, None, (1, 20), True)]
        info = SimpleNamespace(st_dev=1, st_ino=20, st_uid=123, st_mode=0o40755)
        with patch.object(transport.os, 'getuid', return_value=123, create=True), patch.object(transport.os, 'fstat', return_value=info):
            with self.assertRaises(transport.TransportError):
                pin.check_parents()


class WindowsOverlappedTests(unittest.TestCase):
    def native(self, *, issue=997, completion=996, transferred=0):
        class Overlapped(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                        ('Offset', ctypes.c_uint32), ('OffsetHigh', ctypes.c_uint32), ('hEvent', ctypes.c_void_p)]
        state = SimpleNamespace(error=0, completion=completion, transferred=transferred, addresses=[])
        def issue_call(*args):
            state.error = issue
            state.addresses.append(ctypes.addressof(args[-1]._obj))
            return not issue
        def complete(handle, overlapped, size, wait):
            state.addresses.append(ctypes.addressof(overlapped._obj))
            state.error = state.completion
            size._obj.value = state.transferred
            return not state.completion
        def cancel(handle, overlapped):
            state.addresses.append(ctypes.addressof(overlapped._obj))
            state.error = 1168
            return False
        kernel = SimpleNamespace(CreateEventW=Mock(return_value=101), ReadFile=Mock(side_effect=issue_call),
                                 WriteFile=Mock(side_effect=issue_call), ConnectNamedPipe=Mock(side_effect=issue_call),
                                 GetOverlappedResult=Mock(side_effect=complete), CancelIoEx=Mock(side_effect=cancel),
                                 WaitForSingleObject=Mock(return_value=258))
        native = SimpleNamespace(kernel=kernel, Overlapped=Overlapped, close_handle=Mock(),
                                 check=lambda value: transport._require(value, 'mock API error'))
        return native, state

    def test_cancel_not_found_retains_same_overlapped_event_buffer(self):
        native, state = self.native()
        operation = transport._WinOperation(native, SimpleNamespace(handle=100), 'read', 4)
        with patch.object(ctypes, 'get_last_error', side_effect=lambda: state.error, create=True):
            operation.start()
            buffer, event = operation.buffer, operation.overlapped.hEvent
            self.assertFalse(operation.terminal)
            with self.assertRaises(TimeoutError):
                operation.finish(1, lambda: 1)
            operation.cancel()
            operation.cancel()
            operation.poll()
            self.assertFalse(operation.terminal)
            self.assertIs(buffer, operation.buffer)
            self.assertEqual(event, operation.overlapped.hEvent)
            with self.assertRaises(transport.TransportError):
                operation.release()
            native.close_handle.assert_not_called()
            self.assertEqual(1, native.kernel.CancelIoEx.call_count)
            state.completion = 995
            operation.poll()
            self.assertTrue(operation.terminal)
            with self.assertRaises(OSError):
                operation.finish(10, lambda: 1)
            operation.release()
            self.assertIsNone(operation.buffer)
            native.close_handle.assert_called_once_with(event)
            self.assertEqual({ctypes.addressof(operation.overlapped)}, set(state.addresses))

    def test_cancel_race_normal_completion_preserves_transferred(self):
        native, state = self.native()
        operation = transport._WinOperation(native, SimpleNamespace(handle=100), 'write', b'abc')
        with patch.object(ctypes, 'get_last_error', side_effect=lambda: state.error, create=True):
            operation.start()
            operation.cancel()
            state.completion, state.transferred = 0, 3
            operation.poll()
            self.assertEqual(3, operation.finish(10, lambda: 1))
            self.assertEqual(3, operation.transferred)
            operation.release()
        self.assertEqual(1, native.kernel.WriteFile.call_count)

    def test_connected_race_is_success_without_async_reissue(self):
        native, state = self.native(issue=535)
        operation = transport._WinOperation(native, SimpleNamespace(handle=100), 'accept', None)
        with patch.object(ctypes, 'get_last_error', side_effect=lambda: state.error, create=True):
            operation.start()
            self.assertTrue(operation.terminal)
            self.assertIsNone(operation.finish(10, lambda: 1))
            operation.release()
        native.kernel.GetOverlappedResult.assert_not_called()
        native.kernel.CancelIoEx.assert_not_called()
        self.assertEqual(1, native.kernel.ConnectNamedPipe.call_count)

    def test_event_creation_failure_has_no_issued_io(self):
        native, state = self.native()
        native.kernel.CreateEventW.return_value = None
        operation = transport._WinOperation(native, SimpleNamespace(handle=100), 'read', 4)
        with self.assertRaises(transport.TransportError):
            operation.start()
        self.assertTrue(operation.terminal)
        operation.release()
        native.kernel.ReadFile.assert_not_called()
        native.close_handle.assert_not_called()


class ConstructorOwnershipTests(unittest.TestCase):
    def test_create_failure_close_failure_keeps_pin_and_run_slot(self):
        native = FakeNative()
        owner = transport.ModControlTransport(['slot'], native=native, clock=lambda: 1)
        native.pin.create.side_effect = PermissionError('途中検査')
        native.pin.close.side_effect = OSError('close不明')
        context, raw = descriptor()
        with self.assertRaises(PermissionError):
            owner.create(context, raw)
        self.assertIs(native.pin, owner.endpoints['slot'].pin)
        native.controller.close.assert_not_called()
        with self.assertRaises(transport.TransportError):
            owner.create(context, raw)
        native.pin.close.side_effect = None
        self.assertFalse(owner.close(10))
        self.assertEqual(1, native.pin.close.call_count)

    def test_process_open_failure_keeps_owner_until_close(self):
        native = FakeNative()
        owner = transport.ModControlTransport(['slot'], native=native, clock=lambda: 1)
        context, raw = descriptor()
        endpoint = owner.create(context, raw)
        native.peer.open.side_effect = PermissionError('process開始照合')
        native.peer.close.side_effect = OSError('close不明')
        with self.assertRaises(PermissionError):
            endpoint.accept(identity(13), 10)
        self.assertIs(native.peer, endpoint.connection.peer)
        native.listener.connection.assert_not_called()
        self.assertFalse(owner.close(10))
        native.peer.close.side_effect = None
        self.assertFalse(owner.close(10))
        self.assertEqual(1, native.peer.close.call_count)

    def test_win_parent_inspection_failure_retains_open_handle(self):
        native = SimpleNamespace(open_anchor=Mock(return_value=10), metadata=Mock(side_effect=PermissionError('親検査')),
                                 close_handle=Mock())
        pin = transport._WinPin(native, 'C:/managed/manager/control/run-1/mod-control.json')
        with self.assertRaises(PermissionError):
            pin.create(b'raw')
        self.assertEqual(10, pin.nodes[0][0])
        native.close_handle.assert_not_called()
        pin.close()
        native.close_handle.assert_called_once_with(10)

    def test_win_readonly_reopen_must_match_created_file_identity(self):
        data = b'raw'
        handles, directories, closed = [], set(), []
        native = SimpleNamespace(kernel=Mock())
        def relative(parent, name, **options):
            handle = len(handles) + 11
            handles.append(handle)
            if options.get('directory'):
                directories.add(handle)
            return handle
        native.open_anchor = Mock(return_value=10)
        directories.add(10)
        native.relative = Mock(side_effect=relative)
        def metadata(handle, directory=False):
            if directory:return ((1, str(handle).encode()), 0, 16, 1)
            return ((1, b'writer' if handle == handles[-1] and len(handles) == 5 else b'replaced'), len(data), 0, 1)
        native.metadata = Mock(side_effect=metadata)
        native.private = Mock()
        native.check = lambda value: transport._require(value, 'mock API')
        native.close_handle = Mock(side_effect=closed.append)
        native.kernel.WriteFile.side_effect = lambda handle, value, size, written, ov: (setattr(written._obj, 'value', size), 1)[1]
        pin = transport._WinPin(native, 'C:/managed/manager/control/run-1/mod-control.json')
        with self.assertRaises(transport.TransportError):
            pin.create(data)
        pin.close()
        self.assertCountEqual([10, *handles], closed)
        self.assertEqual(len(closed), len(set(closed)))
        native.kernel.ReadFile.assert_not_called()

class SecurityBoundaryTests(unittest.TestCase):
    def test_context_bool_epoch_does_not_match_integer_descriptor(self):
        native = FakeNative()
        owner = transport.ModControlTransport(['slot'], native=native)
        context, raw = descriptor()
        context['epoch'] = True
        with self.assertRaises(transport.TransportError):
            owner.create(context, raw)
        native.pin_controller.assert_not_called()

    def test_windows_metadata_rejects_reparse_multilink_and_deleted(self):
        native = object.__new__(transport._WindowsNative)
        state = SimpleNamespace(attributes=0, links=1, deleted=0, directory=0)
        def information(handle, kind, buffer, size):
            if kind == 1:
                value = buffer._obj
                value.size, value.links, value.deleted, value.directory = 3, state.links, state.deleted, state.directory
            elif kind == 18:
                buffer._obj.volume = 1
            elif kind == 9:
                buffer[0] = state.attributes
            return 1
        native.kernel = SimpleNamespace(GetFileInformationByHandleEx=Mock(side_effect=information), GetFileType=Mock(return_value=1))
        self.assertEqual(3, native.metadata(10)[1])
        for field, value in [('attributes', 0x400), ('links', 2), ('deleted', 1), ('directory', 1)]:
            with self.subTest(field=field):
                before = getattr(state, field)
                setattr(state, field, value)
                with self.assertRaises(transport.TransportError):
                    native.metadata(10)
                setattr(state, field, before)

    def test_win_parent_identity_ignores_mutable_metadata_but_checks_safety(self):
        raw = b'raw'
        native = object.__new__(transport._WindowsNative)
        state = SimpleNamespace(volume=1, file=10, size=0, attributes=16, links=1, directory=1, deleted=0,
                                leaf_links=1, leaf_raw=raw, leaf_attributes=0, change_leaf=False)
        def information(handle, kind, output, size):
            parent = handle == 10
            if kind == 1:
                value = output._obj
                value.size = state.size if parent else len(raw)
                value.links = state.links if parent else state.leaf_links
                value.directory = state.directory if parent else 0
                value.deleted = state.deleted if parent else 0
            elif kind == 18:
                output._obj.volume = state.volume if parent else 1
                output._obj.file[0] = state.file if parent else 20
            elif kind == 9:
                output[0] = state.attributes if parent else state.leaf_attributes
            return 1
        def read(handle, output, size, written, overlapped):
            ctypes.memmove(output, state.leaf_raw, len(raw))
            written._obj.value = len(raw)
            if state.change_leaf:
                state.leaf_attributes = 32
            return 1
        native.kernel = SimpleNamespace(GetFileInformationByHandleEx=Mock(side_effect=information),
                                        GetFileType=Mock(return_value=1), SetFilePointerEx=Mock(return_value=1),
                                        ReadFile=Mock(side_effect=read))
        native.private = Mock()
        native.open_anchor = Mock(return_value=10)
        native.relative = Mock(side_effect=lambda parent, name, **options: 10 if options.get('directory') else 20)
        native.close_handle = Mock()
        native.kernel.WriteFile = Mock(side_effect=lambda handle, data, size, written, ov: (setattr(written._obj, 'value', size), 1)[1])
        native.kernel.FlushFileBuffers = Mock(return_value=1)
        pin = transport._WinPin(native, 'C:/managed/manager/control/run-1/mod-control.json')
        pin.create(raw)
        for field, value in [('attributes', 48), ('size', 4096), ('links', 2)]:
            with self.subTest(mutable=field):
                before = getattr(state, field)
                setattr(state, field, value)
                native.private.reset_mock()
                pin.check(raw)
                self.assertEqual([call(10)] * (len(pin.nodes) - 1) + [call(20), call(20)], native.private.call_args_list)
                setattr(state, field, before)
        for field, value in [('volume', 2), ('file', 11), ('attributes', 0x410), ('directory', 0), ('deleted', 1)]:
            with self.subTest(reject=field):
                before = getattr(state, field)
                setattr(state, field, value)
                native.kernel.ReadFile.reset_mock()
                with self.assertRaises(transport.TransportError):
                    pin.check(raw)
                native.kernel.ReadFile.assert_not_called()
                setattr(state, field, before)
        for field, value in [('leaf_links', 2), ('leaf_raw', b'bad'), ('change_leaf', True)]:
            with self.subTest(leaf_reject=field):
                before = getattr(state, field)
                setattr(state, field, value)
                with self.assertRaises(transport.TransportError):
                    pin.check(raw)
                setattr(state, field, before)
                state.leaf_attributes = 0
        native.private.side_effect = PermissionError('親DACL')
        with self.assertRaises(PermissionError):
            pin.check(raw)

    def test_windows_dacl_requires_protected_control_and_releases_descriptor(self):
        native = object.__new__(transport._WindowsNative)
        state = SimpleNamespace(control=0x1000)
        def descriptor(handle, kind, flags, owner, group, dacl, sacl, output):
            output._obj.value = 100
            return 0
        def control(descriptor, value, revision):
            value._obj.value = state.control
            return 1
        native.security = SimpleNamespace(require_private=Mock(), advapi=SimpleNamespace(
            GetSecurityInfo=Mock(side_effect=descriptor), GetSecurityDescriptorControl=Mock(side_effect=control)))
        native.kernel = SimpleNamespace(LocalFree=Mock())
        native.private(10)
        state.control = 0
        with self.assertRaises(transport.TransportError):
            native.private(10)
        self.assertEqual(2, native.kernel.LocalFree.call_count)
        self.assertEqual(2, native.security.require_private.call_count)

    def test_mac_parent_name_swap_is_rejected_without_reading_leaf(self):
        pin = object.__new__(transport._MacPin)
        pin.nodes = [(20, 19, 'run', (1, 20), True)]
        pinned = SimpleNamespace(st_dev=1, st_ino=20, st_uid=123, st_mode=0o40700)
        swapped = SimpleNamespace(st_dev=1, st_ino=21, st_uid=123, st_mode=0o40700)
        with patch.object(transport.os, 'getuid', return_value=123, create=True), \
                patch.object(transport.os, 'fstat', return_value=pinned), patch.object(transport.os, 'stat', return_value=swapped), \
                patch.object(transport.os, 'read') as read:
            with self.assertRaises(transport.TransportError):
                pin.check_parents()
        read.assert_not_called()

class LifecycleInterleavingTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeNative()
        self.owner = transport.ModControlTransport(['slot'], native=self.native, clock=lambda: 1)
        self.context, self.raw = descriptor()

    def run_blocked(self, action, hook, crossed):
        entered, resume = threading.Event(), threading.Event()
        values, errors = [], []
        def barrier(*args):
            entered.set()
            if not resume.wait(4):
                raise AssertionError('mock barrierが解除されません')
        hook.side_effect = barrier
        def worker():
            try:values.append(action())
            except BaseException as error:errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(4))
            crossed()
        finally:
            resume.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], values)
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], transport.TransportError)
        return errors

    def test_create_publication_does_not_allow_close_or_reap_to_orphan_resources(self):
        def crossed():
            self.assertIn('slot', self.owner.endpoints)
            self.assertFalse(self.owner.close(1))
            self.owner.reap()
            self.native.pin.close.assert_not_called()
            self.native.controller.close.assert_not_called()
            with self.assertRaises(transport.TransportError):
                self.owner.create(self.context, self.raw)
        self.run_blocked(lambda: self.owner.create(self.context, self.raw), self.native.pin.create, crossed)
        self.native.listen.assert_not_called()
        self.native.pin.close.assert_called_once()
        self.native.controller.close.assert_called_once()
        self.assertTrue(self.owner.close(10))
        self.assertEqual({}, self.owner.endpoints)

    def test_shutdown_during_controller_open_does_not_create_descriptor(self):
        self.run_blocked(lambda: self.owner.create(self.context, self.raw), self.native.controller.open,
                         lambda: self.assertFalse(self.owner.close(1)))
        self.native.descriptor.assert_not_called()
        self.native.controller.close.assert_called_once()

    def test_accept_process_initialization_is_owned_before_publication(self):
        endpoint = self.owner.create(self.context, self.raw)
        def crossed():
            self.assertIsNotNone(endpoint.connection)
            self.assertFalse(endpoint.close(1))
            self.owner.reap()
            self.native.peer.close.assert_not_called()
            with self.assertRaises(transport.TransportError):
                endpoint.accept(identity(13), 10)
        self.run_blocked(lambda: endpoint.accept(identity(13), 10), self.native.peer.open, crossed)
        self.native.listener.connection.assert_not_called()
        self.native.peer.close.assert_called_once()
        self.assertTrue(self.owner.close(10))

    def test_accept_io_is_not_cancelled_released_or_closed_by_parallel_reap(self):
        endpoint = self.owner.create(self.context, self.raw)
        entered, resume = threading.Event(), threading.Event()
        original = self.native.operation
        observed = []
        def operation(channel, kind, value):
            op = original(channel, kind, value)
            finish = op.finish
            def blocked(deadline, clock):
                entered.set()
                if not resume.wait(4):raise AssertionError('I/O barrierが解除されません')
                return finish(deadline, clock)
            op.finish = blocked
            observed.append(op)
            return op
        self.native.operation = operation
        values, errors = [], []
        def worker():
            try:values.append(endpoint.accept(identity(13), 10))
            except BaseException as error:errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(4))
            self.assertIs(observed[0], endpoint.connection.pending)
            self.assertFalse(endpoint.close(1))
            self.owner.reap()
            self.assertEqual(0, observed[0].cancelled)
            self.assertFalse(observed[0].released)
            self.native.channel.close.assert_not_called()
            self.native.peer.close.assert_not_called()
        finally:
            resume.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], values)
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], transport.TransportError)
        self.assertTrue(observed[0].released)
        self.assertTrue(self.owner.close(10))

    def test_old_endpoint_reclose_cannot_pop_new_run(self):
        old = self.owner.create(self.context, self.raw)
        self.assertTrue(old.close(10))
        new = self.owner.create(self.context, self.raw)
        self.assertTrue(old.close(10))
        self.assertIs(new, self.owner.endpoints['slot'])
        self.assertTrue(self.owner.close(10))

    def test_old_connection_reclose_cannot_detach_new_connection(self):
        endpoint = self.owner.create(self.context, self.raw)
        old = endpoint.accept(identity(13), 10)
        self.assertTrue(old.close(10))
        new = endpoint.accept(identity(13), 10)
        self.assertTrue(old.close(10))
        self.assertIs(new, endpoint.connection)
        with self.assertRaises(transport.TransportError):
            endpoint.accept(identity(13), 10)
        self.assertTrue(self.owner.close(10))


class LateReadyDeadlineTests(unittest.TestCase):
    def test_mac_late_ready_never_issues_new_accept_read_or_write(self):
        for kind, method, value, success in [('accept', 'accept', None, (Mock(), None)),
                                            ('read', 'recv', 4, b'body'), ('write', 'send', b'body', 4)]:
            with self.subTest(kind=kind):
                stream = Mock()
                getattr(stream, method).side_effect = [BlockingIOError(), success]
                channel = SimpleNamespace(stream=stream, listener=SimpleNamespace(stream=stream))
                operation = transport._MacOperation(channel, kind, value)
                operation.start()
                now = [1]
                def late(*args):
                    now[0] = 10
                    return ([stream], [stream], [])
                with patch.object(transport.select, 'select', side_effect=late):
                    with self.assertRaises(TimeoutError):
                        operation.finish(10, lambda: now[0])
                self.assertEqual(1, getattr(stream, method).call_count)
                self.assertFalse(operation.terminal)
                operation.cancel()
                self.assertTrue(operation.terminal)


class CloseUnknownTests(unittest.TestCase):
    def test_consumed_channel_close_reused_value_is_never_closed_again(self):
        native = FakeNative()
        owner = transport.ModControlTransport(['slot'], native=native, clock=lambda: 1)
        context, raw = descriptor()
        endpoint = owner.create(context, raw)
        connection = endpoint.accept(identity(13), 10)
        live = {77: 'original-channel'}
        def consumed():
            self.assertEqual('original-channel', live.pop(77))
            live[77] = 'unrelated-channel'
            raise OSError('close後の結果不明')
        native.channel.close.side_effect = consumed
        self.assertFalse(connection.close(10))
        self.assertTrue(connection.close_unknown)
        owner.reap()
        self.assertFalse(owner.close(10))
        self.assertFalse(connection.close(10))
        self.assertEqual(1, native.channel.close.call_count)
        self.assertEqual('unrelated-channel', live[77])
        self.assertIs(connection, endpoint.connection)

    def test_consumed_terminal_event_close_reused_handle_is_never_released_again(self):
        win, state = WindowsOverlappedTests().native(issue=0, completion=0, transferred=5)
        native = FakeNative()
        native.channel.handle = 77
        owner = transport.ModControlTransport(['slot'], native=native, clock=lambda: 1)
        context, raw = descriptor()
        endpoint = owner.create(context, raw)
        connection = endpoint.accept(identity(13), 10)
        operations = []
        def operation(channel, kind, value):
            result = transport._WinOperation(win, channel, kind, value)
            operations.append(result)
            return result
        native.operation = operation
        live = {101: 'original-event'}
        def consumed(handle):
            self.assertEqual('original-event', live.pop(handle))
            live[handle] = 'unrelated-event'
            raise OSError('CloseHandle後の不明')
        win.close_handle.side_effect = consumed
        with patch.object(ctypes, 'get_last_error', side_effect=lambda: state.error, create=True):
            with self.assertRaises(OSError):
                connection.send_frame(b'\0\0\0\1x', 10)
        self.assertTrue(connection.close_unknown)
        self.assertTrue(operations[0].release_unknown)
        owner.reap()
        self.assertFalse(owner.close(10))
        with self.assertRaises(transport.TransportError):
            operations[0].release()
        self.assertEqual(1, win.close_handle.call_count)
        self.assertEqual('unrelated-event', live[101])
        self.assertIs(operations[0], connection.pending)
