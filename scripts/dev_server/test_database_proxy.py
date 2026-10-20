"""proxy生成/保持/終了の直接境界。fake nativeと自己所有portless正常childを区別する。"""
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from .build_inputs import canonical, digest
from .database_proxy import DatabaseProxy
from .registry import RegistryError
from . import runtime_process


def frame(command='execute', operation='request-id'):
    return {'command': command, 'operationId': operation, 'requestHash': digest(operation.encode()), 'timeoutMillis': 1000}


class FakePipe:
    def __init__(self):
        self.child_input = 101
        self.child_output = 102
        self.sent = []
        self.reply = None
        self.partial = False
        self.closed = False
        self.acks = 0

    def close_child_ends(self):
        self.child_input = self.child_output = None

    def send(self, value, deadline, verify):
        verify()
        self.sent.append(value)
        if self.partial:
            raise TimeoutError('fixture partial send')

    def receive(self, deadline, verify):
        verify()
        if self.reply is None:
            raise TimeoutError('fixture pending reply')
        return self.reply

    def acknowledged(self):
        self.acks += 1
        self.reply = None

    def close_after_exit(self):
        self.closed = True


class FakeProcess:
    def __init__(self):
        self.pid = None
        self.stage = 'NOT_CREATED'
        self.group = 'fake-group'
        self.code = None
        self.children = []
        self.closed = False
        self.starts = 0
        self.identity = None

    def start(self, arguments, cwd, log, name, created, **stdio):
        self.starts += 1
        self.pid = 123456
        self.stage = 'CREATED'
        self.identity = {'owner': 'fixture-owner', 'pid': self.pid, 'startMillis': 123,
                         'cwd': str(cwd.resolve()), 'executable': str(Path(arguments[0]).resolve()),
                         'commandHash': runtime_process.command_hash(arguments)}
        created()
        self.stage = 'RUNNING'

    def poll(self):
        return self.code

    def members(self):
        return self.children

    def close(self):
        self.closed = True


class DatabaseProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.valid = True
        self.process = FakeProcess()
        self.adapter = types.SimpleNamespace(OwnedProcess=lambda: self.process, identity=self.identity)
        self.command = [str(self.bundle / 'jdk/bin/java.exe'), 'fixed-main']
        self.proxy = DatabaseProxy(self.bundle, self.root / 'diagnostics', {'nonceHash': 'binding-only'},
                                   verify=self.verify, process_adapter=self.adapter)

    def identity(self, pid):
        return {'owner': 'fixture-owner'} if pid == os.getpid() else self.process.identity

    def verify(self):
        if not self.valid:
            raise RegistryError('STALE_WRITER', 'fixture owner expired')

    def start(self):
        with patch('dev_server.database_proxy.proxy_command', return_value=self.command), \
                patch('dev_server.database_proxy.ProxyPipe', FakePipe):
            self.proxy.start()

    def response(self, status, request=None, **extra):
        request = request or frame()
        self.proxy.pipe.reply = {key: request[key] for key in ('operationId', 'requestHash')} | {'status': status} | extra
        return self.proxy.pipe.reply

    def test_native_identity_is_recorded_before_resume_and_no_body_is_journalled(self):
        self.start()
        records = [json.loads(path.read_bytes()) for path in sorted((self.root / 'diagnostics').glob('creation-*.json'))]
        self.assertIsNone(records[0]['creation'])
        self.assertEqual('CREATED', records[1]['creation']['stage'])
        self.assertIsNone(records[1]['identity'])
        self.assertEqual(self.process.identity, records[2]['identity'])
        self.assertEqual('RUNNING', records[3]['creation']['stage'])
        self.assertFalse(any('status' in record for record in records))

    def test_creation_failure_is_retained_and_never_replaced(self):
        with patch('dev_server.database_proxy.proxy_command', return_value=self.command), \
                patch('dev_server.database_proxy.ProxyPipe', FakePipe), \
                patch.object(self.proxy, '_record', side_effect=[None, OSError('fixture journal failure')]):
            with self.assertRaises(OSError):
                self.proxy.start()
        self.assertIs(self.process, self.proxy.process)
        self.assertEqual('CREATED', self.process.stage)
        with self.assertRaises(RegistryError):
            self.proxy.start()
        self.assertEqual(1, self.process.starts)
        self.assertFalse(self.process.closed)

    def test_buffer_survives_record_failure_and_owner_expiry_still_rejects(self):
        self.start()
        response = self.response('RESULT', response={'private': 'fixture-body'})
        actual = self.proxy.begin(frame(), time.monotonic() + 1)
        actual['response']['private'] = 'caller-mutated'
        self.assertEqual('fixture-body', self.proxy.receive(time.monotonic() - 1)['response']['private'])
        self.assertEqual(1, len(self.proxy.pipe.sent))
        self.valid = False
        with self.assertRaises(RegistryError):
            self.proxy.receive(time.monotonic() + 1)
        self.valid = True
        self.assertEqual(response, self.proxy.receive(time.monotonic() + 1))
        with self.assertRaises(RegistryError):
            self.proxy.acknowledge('0' * 64)
        self.assertEqual(0, self.proxy.pipe.acks)
        self.proxy.acknowledge(digest(canonical(response)))
        self.assertEqual(1, self.proxy.pipe.acks)
        self.assertIsNone(self.proxy.request_bytes)

    def test_partial_send_is_not_resumed_by_receive_or_second_begin(self):
        self.start()
        self.proxy.pipe.partial = True
        with self.assertRaises(TimeoutError):
            self.proxy.begin(frame(), time.monotonic() + 1)
        with self.assertRaises(TimeoutError):
            self.proxy.receive(time.monotonic() + 1)
        with self.assertRaises(RegistryError):
            self.proxy.begin(frame(), time.monotonic() + 1)
        self.assertEqual(1, len(self.proxy.pipe.sent))
        self.assertFalse(self.proxy.sent_complete)

    def test_unknown_cannot_be_acknowledged_or_promoted_by_process_exit(self):
        self.start()
        response = self.response('UNKNOWN')
        self.proxy.begin(frame(), time.monotonic() + 1)
        self.process.code = 0
        with self.assertRaises(RegistryError):
            self.proxy.acknowledge(digest(canonical(response)))
        with self.assertRaises(RegistryError):
            self.proxy.finish_exit(time.monotonic() + 1)
        self.assertIsNotNone(self.proxy.response_bytes)
        self.assertFalse(self.process.closed)

    def test_pending_allows_only_original_resolve_and_close_blocks_new_request(self):
        self.start()
        response = self.response('PENDING')
        self.proxy.begin(frame(), time.monotonic() + 1)
        self.proxy.acknowledge(digest(canonical(response)))
        with self.assertRaises(RegistryError):
            self.proxy.begin(frame('execute', 'new-id'), time.monotonic() + 1)
        with self.assertRaises(RegistryError):
            self.proxy.begin(frame('resolve', 'new-id'), time.monotonic() + 1)
        self.response('RESULT')
        result = self.proxy.begin(frame('resolve'), time.monotonic() + 1)
        self.proxy.acknowledge(digest(canonical(result)))
        close = frame('close', 'close-id')
        self.response('PENDING', close)
        result = self.proxy.begin(close, time.monotonic() + 1)
        self.proxy.acknowledge(digest(canonical(result)))
        with self.assertRaises(RegistryError):
            self.proxy.begin(frame('execute', 'new-id'), time.monotonic() + 1)
        self.assertEqual(3, len(self.proxy.pipe.sent))

    def test_close_needs_terminal_frame_exit_zero_and_no_children(self):
        self.start()
        self.proxy.bound = True
        close = frame('close')
        self.response('CLOSED', close, historyEnded=True, pending=0, peerClosed=True)
        self.proxy.begin(close, time.monotonic() + 1)
        self.process.code = 0
        self.process.children = [987654]
        with self.assertRaises(TimeoutError):
            self.proxy.finish_exit(time.monotonic() - 1)
        self.assertFalse(self.process.closed)
        self.process.children = []
        self.assertTrue(self.proxy.finish_exit(time.monotonic() + 1)['proxyExited'])
        self.assertTrue(self.process.closed)
        self.assertTrue(self.proxy.pipe.closed)

    def test_implicit_jvm_environment_is_rejected_before_child_creation(self):
        with patch.dict(os.environ, {'JDK_JAVA_OPTIONS': '-Dfixture=true'}):
            with self.assertRaises(RegistryError):
                self.proxy.start()
        self.assertEqual(0, self.process.starts)

    def test_owned_exit_zero_during_identity_error_allows_readback_without_closure(self):
        self.start()
        def exiting(pid):
            self.process.code = 0
            raise OSError(299, 'fixture identity read raced with owned exit')
        self.adapter.identity = exiting
        self.proxy.verify()
        self.assertFalse(self.proxy.exited)
        self.assertFalse(self.process.closed)
        with self.assertRaises(RegistryError):
            self.proxy.finish_exit(time.monotonic() + 1)
        self.assertFalse(self.proxy.pipe.closed)

    def test_owned_exit_nonzero_during_identity_error_is_not_accepted(self):
        self.start()
        def exiting(pid):
            self.process.code = 17
            raise OSError(299, 'fixture nonzero owned exit')
        self.adapter.identity = exiting
        with self.assertRaises(OSError):
            self.proxy.verify()
        self.assertFalse(self.proxy.exited)
        self.assertFalse(self.process.closed)

    def test_identity_error_while_owned_process_stays_running_is_preserved(self):
        self.start()
        self.adapter.identity = lambda pid: (_ for _ in ()).throw(OSError(299, 'fixture still alive'))
        with self.assertRaises(OSError):
            self.proxy.verify()
        self.assertIsNone(self.process.code)
        self.assertFalse(self.proxy.exited)

    def test_identity_mismatch_is_not_accepted_even_when_owned_process_exits_zero(self):
        self.start()
        for key in ('pid', 'startMillis', 'commandHash'):
            with self.subTest(key=key):
                self.process.code = None
                def exiting(pid):
                    self.process.code = 0
                    value = dict(self.process.identity)
                    value[key] = value[key] + 1 if type(value[key]) is int else 'different'
                    return value
                self.adapter.identity = exiting
                with self.assertRaises(RegistryError):
                    self.proxy.verify()
                self.assertFalse(self.proxy.exited)

    def test_missing_identity_with_owned_exit_zero_is_readback_only(self):
        self.start()
        def exiting(pid):
            self.process.code = 0
            return None
        self.adapter.identity = exiting
        self.proxy.verify()
        self.assertFalse(self.proxy.exited)
        with self.assertRaises(RegistryError):
            self.proxy.finish_exit(time.monotonic() + 1)
        self.assertFalse(self.process.closed)

    def test_missing_identity_while_owned_process_runs_is_rejected(self):
        self.start()
        self.adapter.identity = lambda pid: None
        with self.assertRaises(RegistryError):
            self.proxy.verify()
        self.assertIsNone(self.process.code)

    def test_owner_revocation_is_rejected_even_after_owned_exit_zero(self):
        self.start()
        self.process.code = 0
        self.valid = False
        with self.assertRaises(RegistryError) as raised:
            self.proxy.verify()
        self.assertEqual('STALE_WRITER', raised.exception.code)
        self.assertFalse(self.proxy.exited)

    def prepare_read_wait(self, after):
        self.start()
        self.clock = 0.0
        self.after = after
        request = frame()
        self.proxy.request_bytes = canonical(request)
        self.proxy.sent_complete = True
        self.proxy.pipe.sent.append(request)
        self.expected = self.response('RESULT', request)
        self.adapter.identity = lambda pid: (_ for _ in ()).throw(PermissionError(5, 'fixture exit in progress'))

    def advance_read_wait(self, seconds):
        self.clock += seconds
        self.after()

    def read_waiting(self):
        return patch.multiple('dev_server.database_proxy.time', monotonic=lambda: self.clock, sleep=self.advance_read_wait)

    def test_read_wait_exit_zero_without_send_or_closure(self):
        self.prepare_read_wait(lambda: setattr(self.process, 'code', 0))
        with self.read_waiting():
            self.assertEqual(self.expected, self.proxy.receive(1))
        self.assertEqual(1, len(self.proxy.pipe.sent))
        self.assertFalse(self.proxy.exited)
        self.assertFalse(self.process.closed)
        with self.assertRaises(RegistryError):
            self.proxy.finish_exit(1)

    def test_read_wait_valid_identity_rechecks_before_response(self):
        self.prepare_read_wait(lambda: setattr(self.adapter, 'identity', lambda pid: self.process.identity))
        with self.read_waiting():
            self.assertEqual(self.expected, self.proxy.receive(1))
        self.assertIsNone(self.process.code)
        self.assertEqual(1, len(self.proxy.pipe.sent))

    def test_read_wait_owner_revoked_rejects(self):
        self.prepare_read_wait(lambda: setattr(self, 'valid', False))
        with self.read_waiting(), self.assertRaises(RegistryError) as raised:
            self.proxy.receive(1)
        self.assertEqual('STALE_WRITER', raised.exception.code)
        self.assertFalse(self.proxy.exited)

    def test_read_wait_identity_mismatch_rejects(self):
        self.prepare_read_wait(lambda: setattr(self.adapter, 'identity', lambda pid: dict(self.process.identity, startMillis=999)))
        with self.read_waiting(), self.assertRaises(RegistryError):
            self.proxy.receive(1)
        self.assertEqual(1, len(self.proxy.pipe.sent))
        self.assertFalse(self.proxy.exited)

    def test_read_wait_nonzero_rejects(self):
        self.prepare_read_wait(lambda: setattr(self.process, 'code', 17))
        with self.read_waiting(), self.assertRaises(RegistryError) as raised:
            self.proxy.receive(1)
        self.assertEqual('WRITER_PENDING', raised.exception.code)
        self.assertEqual(17, self.process.code)
        self.assertFalse(self.proxy.exited)
        self.assertFalse(self.process.closed)

    def test_read_wait_expired_rejects_without_send(self):
        self.prepare_read_wait(lambda: self.fail('期限後に待ちません'))
        with self.read_waiting(), self.assertRaises(OSError):
            self.proxy.receive(0)
        self.assertEqual(1, len(self.proxy.pipe.sent))
        self.assertFalse(self.proxy.exited)

    def test_read_wait_partial_frame_does_not_wait_or_send(self):
        self.prepare_read_wait(lambda: self.fail('partial後に待ちません'))
        self.proxy.sent_complete = False
        with self.read_waiting(), self.assertRaises(OSError):
            self.proxy.receive(1)
        self.assertEqual(1, len(self.proxy.pipe.sent))

    def test_new_begin_never_uses_wait_as_send_permission(self):
        self.prepare_read_wait(lambda: self.fail('新送信前に待ちません'))
        self.proxy.request_bytes = None
        self.proxy.sent_complete = False
        self.proxy.pipe.sent = []
        with self.read_waiting(), self.assertRaises(OSError):
            self.proxy.begin(frame(), 1)
        self.assertEqual([], self.proxy.pipe.sent)

    def test_owned_portless_child_has_private_stdio_normal_exit_and_no_body_log(self):
        script = self.root / 'fixture-child.py'
        script.write_text('''import json,struct,sys
while True:
 h=sys.stdin.buffer.read(4)
 if not h:break
 n=struct.unpack('!I',h)[0];request=json.loads(sys.stdin.buffer.read(n));command=request['command']
 response={k:request[k] for k in ('operationId','requestHash')}
 response['status']='OPEN' if command=='open' else 'CLOSED'
 if command=='close':response.update(historyEnded=True,pending=0,peerClosed=True)
 body=json.dumps(response,separators=(',',':')).encode();sys.stdout.buffer.write(struct.pack('!I',len(body))+body);sys.stdout.buffer.flush()
 if command=='close':break
''', encoding='utf-8')
        proxy = DatabaseProxy(self.bundle, self.root / 'owned', {'nonceHash': 'fixture-binding'}, verify=lambda: None)
        identity = runtime_process.identity(os.getpid())
        self.assertIsNotNone(identity)
        with patch('dev_server.database_proxy.proxy_command', return_value=[identity['executable'], '-B', str(script)]):
            proxy.start()
        opening = frame('open', 'open-id') | {'privateBody': 'do-not-journal-typed-body'}
        result = proxy.begin(opening, time.monotonic() + 5)
        proxy.acknowledge(digest(canonical(result)))
        result = proxy.begin(frame('close', 'close-id'), time.monotonic() + 5)
        proof = proxy.finish_exit(time.monotonic() + 5)
        self.assertTrue(proof['proxyChildrenExited'])
        self.assertTrue(proxy.exited)
        for path in (self.root / 'owned').iterdir():
            self.assertNotIn(b'do-not-journal-typed-body', path.read_bytes())
        print(json.dumps({'fixture': 'DatabaseProxy private stdio only; no TCP/server/DB',
                          'pid': proxy.process.pid, 'exit': 0, 'allChildrenExited': True}))