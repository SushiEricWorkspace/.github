"""私設frameの中断保持と、所有する正常終了childのstdioだけを検証する。"""
import errno
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from .database_proxy_pipe import ProxyPipe
from .local_ipc import MAX_MESSAGE, encode
from . import runtime_process


def read_exact(fd, count):
    value = bytearray()
    while len(value) < count:
        part = os.read(fd, count - len(value))
        if not part:
            raise EOFError('fixture EOF')
        value.extend(part)
    return bytes(value)


class ProxyPipeTests(unittest.TestCase):
    def setUp(self):
        self.pipe = ProxyPipe()
        self.addCleanup(self.pipe.close_after_exit)

    def deadline(self):
        return time.monotonic() + 2

    def test_pipe_setup_failure_closes_only_unpublished_descriptors(self):
        for stage in ('allocation', 'mode'):
            first = os.pipe()
            second = os.pipe() if stage == 'mode' else None
            fds = [*first, *(second or ())]
            values = [first, second] if second else [first, OSError('fixture allocation')]
            with self.subTest(stage=stage), patch('dev_server.database_proxy_pipe.os.pipe', side_effect=values), \
                    patch('dev_server.database_proxy_pipe.os.set_blocking', side_effect=OSError('fixture mode')):
                with self.assertRaises(OSError):
                    ProxyPipe()
            for fd in fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_utf8_round_trip_and_escaped_newline(self):
        value = {'operationId': 'fixture', 'text': '日本語\n次の行'}
        self.pipe.send(value, self.deadline(), lambda: None)
        count = struct.unpack('!I', read_exact(self.pipe.child_input, 4))[0]
        body = read_exact(self.pipe.child_input, count)
        self.assertNotIn(b'\n', body)
        self.assertEqual(value, json.loads(body.decode('utf-8')))
        os.write(self.pipe.child_output, struct.pack('!I', len(body)) + body)
        self.assertEqual(value, self.pipe.receive(self.deadline(), lambda: None))
        self.pipe.acknowledged()
        self.assertEqual((None, 0, bytearray(), None),
                         (self.pipe.outgoing, self.pipe.offset, self.pipe.incoming, self.pipe.expected))

    def test_wire_limit_and_raw_newline_are_rejected_before_write(self):
        for body in (b'x' * (MAX_MESSAGE - 3), b'{\n"x":1}'):
            with self.subTest(length=len(body)), patch('dev_server.database_proxy_pipe.encode', return_value=body), \
                    patch('dev_server.database_proxy_pipe.os.write') as write:
                with self.assertRaises(ValueError):
                    self.pipe.send({}, self.deadline(), lambda: None)
                write.assert_not_called()
        self.assertIsNone(self.pipe.outgoing)

    def test_invalid_peer_frames_are_rejected(self):
        cases = [struct.pack('!I', 0), struct.pack('!I', MAX_MESSAGE - 3)]
        for body in (b'[]', b'{"x":"\xff"}', b'{\n"x":1}', b'{"x":NaN}'):
            cases.append(struct.pack('!I', len(body)) + body)
        for frame in cases:
            with self.subTest(frame=frame[:20]):
                pipe = ProxyPipe()
                try:
                    os.write(pipe.child_output, frame)
                    with self.assertRaises((ValueError, UnicodeDecodeError)):
                        pipe.receive(self.deadline(), lambda: None)
                finally:
                    pipe.close_after_exit()

    def buffered_response(self):
        value = {'status': 'complete'}
        body = encode(value)
        os.write(self.pipe.child_output, struct.pack('!I', len(body)) + body)
        self.assertEqual(value, self.pipe.receive(self.deadline(), lambda: None))
        return value

    def test_buffered_response_revalidates_owner_before_replay(self):
        self.buffered_response()
        def invalid_owner():
            raise ValueError('fixture binding失効')
        with patch('dev_server.database_proxy_pipe.os.read') as read:
            with self.assertRaisesRegex(ValueError, 'binding失効'):
                self.pipe.receive(time.monotonic() - 1, invalid_owner)
            read.assert_not_called()

    def test_buffered_response_after_deadline_needs_no_new_io(self):
        value = self.buffered_response()
        verified = []
        with patch('dev_server.database_proxy_pipe.os.read') as read:
            self.assertEqual(value, self.pipe.receive(time.monotonic() - 1, lambda: verified.append(True)))
            read.assert_not_called()
        self.assertTrue(verified)

    def test_partial_response_timeout_retains_prefix(self):
        body = encode({'status': 'complete'})
        frame = struct.pack('!I', len(body)) + body
        os.write(self.pipe.child_output, frame[:3])
        with self.assertRaises(TimeoutError):
            self.pipe.receive(time.monotonic() + 0.02, lambda: None)
        self.assertEqual(frame[:3], self.pipe.incoming)
        os.write(self.pipe.child_output, frame[3:])
        self.assertEqual({'status': 'complete'}, self.pipe.receive(self.deadline(), lambda: None))

    def test_partial_send_resumes_same_frame_without_duplicate(self):
        value = {'operationId': 'same-id', 'text': 'fixed'}
        original = os.write
        first = True
        def blocked(fd, body):
            nonlocal first
            if first:
                first = False
                return original(fd, body[:5])
            raise BlockingIOError(errno.EAGAIN, 'fixture blocked')
        with patch('dev_server.database_proxy_pipe.os.write', side_effect=blocked):
            with self.assertRaises(TimeoutError):
                self.pipe.send(value, time.monotonic() + 0.02, lambda: None)
        self.assertEqual(5, self.pipe.offset)
        with self.assertRaises(ValueError):
            self.pipe.send(dict(value, text='other'), self.deadline(), lambda: None)
        self.pipe.send(value, self.deadline(), lambda: None)
        count = struct.unpack('!I', read_exact(self.pipe.child_input, 4))[0]
        self.assertEqual(value, json.loads(read_exact(self.pipe.child_input, count)))
        self.assertEqual(len(self.pipe.outgoing), self.pipe.offset)


@unittest.skipUnless(sys.platform in ('win32', 'darwin'), '対象OSの所有process生成試験です')
class OwnedProxyChildTests(unittest.TestCase):
    def test_private_stdio_is_bound_before_resume_and_child_exits_normally(self):
        pipe = ProxyPipe()
        process = runtime_process.OwnedProcess()
        observed = {}
        code = """import sys,struct,json
header=sys.stdin.buffer.read(4)
if header:
 size=struct.unpack('!I',header)[0]
 body=sys.stdin.buffer.read(size)
 value=json.loads(body.decode('utf-8'))
 response=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')
 sys.stdout.buffer.write(struct.pack('!I',len(response))+response)
 sys.stdout.buffer.flush()
"""
        def created():
            observed['stage'] = process.stage
            observed['pid'] = process.pid
            try:
                observed['identity'] = runtime_process.identity(process.pid)
                observed['parentInherited'] = [os.get_inheritable(pipe.input), os.get_inheritable(pipe.output)]
            except Exception as error:
                observed['error'] = type(error).__name__
        with tempfile.TemporaryDirectory(prefix='proxy-child-') as directory, \
                (Path(directory) / 'child.log').open('wb') as log:
            try:
                process.start([sys.executable, '-B', '-c', code], Path(directory), log,
                              'proxy-fixture-' + str(os.getpid()), created,
                              input_fd=pipe.child_input, output_fd=pipe.child_output)
                pipe.close_child_ends()
                self.assertEqual('CREATED', observed['stage'])
                self.assertNotIn('error', observed)
                self.assertEqual([False, False], observed['parentInherited'])
                self.assertIsNotNone(observed['identity'])
                self.assertEqual(process.pid, observed['identity']['pid'])
                payload = {'text': 'stdio本人', 'operationId': 'fixture'}
                pipe.send(payload, time.monotonic() + 5, lambda: None)
                self.assertEqual(payload, pipe.receive(time.monotonic() + 5, lambda: None))
            finally:
                if pipe.output is not None:
                    os.close(pipe.output)
                    pipe.output = None
                deadline = time.monotonic() + 5
                while process.pid is not None and time.monotonic() < deadline:
                    if process.poll() is not None and process.members() == []:
                        break
                    time.sleep(0.01)
                if process.pid is not None and process.poll() is not None:
                    self.assertEqual(0, process.poll())
                    self.assertEqual([], process.members())
                    print('proxy-fixture childPid=' + str(process.pid) + ' expectedExit=0 observedExit=0 port=none')
                    process.close()
                    pipe.close_after_exit()
                else:
                    self.fail('所有fixture childの終了不明。killせず保全します。pid=' + str(process.pid))
