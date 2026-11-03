"""Mod確定blobと同bytesのwire3、予算と旧版分離を検証する。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import sqlite3
import unittest

from . import mod_control_wire as wire
from . import mod_control_operations as operations
from .test_mod_control_transport import FakeNative, descriptor, identity
from .mod_control_transport import ModControlTransport, TransportError, _WinOperation

ROOT = Path(__file__).parent
VECTORS = json.loads((ROOT / 'mod-control-vectors-v3-local.json').read_bytes())


def frame(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    return struct.pack('>I', len(raw)) + raw


class Wire3Tests(unittest.TestCase):
    def test_git_blob_bytes(self):
        for name, size, digest in [
            ('mod-control-vectors-v3-local.json', 73736, 'e515f795e9fc92849506c5389b19daad22cf086b3845bfcb9557d5f22f3c53b9'),
            ('management-stop-vectors.json', 5953, 'c8fa634a4deb076778eb47f9d655dd65e555f44ea3c0e43844a4b21c7d1a2d12'),
            ('management-stop-trace.json', 529, 'fc3d531d25e07ea79ec657de11310ecb8815ee25afbf7716cfbbbb71a708aa3c')]:
            with self.subTest(name=name):
                raw = (ROOT / name).read_bytes()
                self.assertEqual(size, len(raw))
                self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_shared_roundtrips_and_errors_both_os(self):
        for session in VECTORS['sessions']:
            context = {k: v for k, v in VECTORS['contexts'][session['contextId']].items()
                       if k in {'descriptorJson', 'owner', 'management', 'expectedEvidence'}}
            pre = None
            for pair in session['pairs']:
                request = bytes.fromhex(pair['request']['frameHex'])
                response = bytes.fromhex(pair['response']['frameHex'])
                with self.subTest(os=session['contextId'], name=pair['name']):
                    message = wire.decode_frame_v3(request)
                    wire.correlate_response_v3(request, response)
                    if message['phase'] == 'HELLO':
                        pre = message['payload']['profileEvidenceJson']
                        wire.correlate_grant_v3(request, response, context)
                    else:
                        wire.correlate_binding_v3(request, context, pre_evidence_json=pre)
                    for old in (wire.decode_frame, wire.decode_frame_v2):
                        with self.assertRaises(ValueError):
                            old(request)
                    changed = deepcopy(message)
                    changed['requestNonce'] = 'other-nonce'
                    with self.assertRaises(ValueError):
                        wire.correlate_response_v3(frame(changed), response)

    def test_body_exact_limit_and_one_over_and_bad_prefix(self):
        request = bytes.fromhex(VECTORS['sessions'][0]['pairs'][2]['request']['frameHex'])
        message = wire.decode_frame_v3(request)
        short = frame(message)
        raw = short[4:] + b' ' * (wire.MAX_BODY_V3 - len(short) + 4)
        exact = struct.pack('>I', len(raw)) + raw
        self.assertEqual(wire.MAX_BODY_V3 + 4, len(exact))
        wire.decode_frame_v3(exact)
        for bad in (struct.pack('>I', len(raw) + 1) + raw + b' ', b'\0\0\0\0', b'\xff' * 4, exact + b'x'):
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                wire.decode_frame_v3(bad)

    def test_evidence_limit_and_double_escape_outer_budget(self):
        message = wire.decode_frame_v3(bytes.fromhex(VECTORS['sessions'][0]['pairs'][0]['request']['frameHex']))
        text = message['payload']['profileEvidenceJson']
        for extra in (wire.MAX_EVIDENCE_V3 - len(text.encode()), wire.MAX_EVIDENCE_V3 - len(text.encode()) + 1):
            raw = text.encode() + b' ' * extra
            message['payload'].update(profileEvidenceJson=raw.decode(), profileEvidenceSha256=wire.sha256(raw))
            if len(raw) == wire.MAX_EVIDENCE_V3:
                wire.decode_frame_v3(frame(message))
            else:
                with self.assertRaises(ValueError):
                    wire.decode_frame_v3(frame(message))
        # inner JSONの空白はouterでは各byteが6文字へescapeされ得る。
        message['payload']['profileEvidenceJson'] = text + '\t' * (wire.MAX_EVIDENCE_V3 - len(text.encode()))
        message['payload']['profileEvidenceSha256'] = wire.sha256(message['payload']['profileEvidenceJson'].encode())
        raw = json.dumps(message, ensure_ascii=True).replace('\\t', '\\u0009').encode()
        with self.assertRaises(ValueError):
            wire.decode_frame_v3(struct.pack('>I', len(raw)) + raw)

    def test_transport_chunks_and_prefix_budget_are_independent_of_old_wire(self):
        native = FakeNative()
        native.chunk = 9999999
        owner = ModControlTransport(['slot'], native=native, clock=lambda: 1, wire_version=3)
        context, raw = descriptor()
        endpoint = owner.create(context, raw)
        channel = endpoint.accept(identity(13), 10)
        body = b'x' * wire.MAX_BODY_V3
        raw = struct.pack('>I', len(body)) + body
        native.channel.incoming = raw
        self.assertEqual(raw, channel.receive_frame(10))
        self.assertEqual(len(raw), channel.send_frame(raw, 10))
        self.assertEqual(raw, native.channel.sent)
        for operation in native.operations:
            if operation.kind == 'read':
                self.assertLessEqual(operation.value, 65536)
            elif operation.kind == 'write':
                self.assertLessEqual(len(operation.value), 65536)
        native.channel.incoming = struct.pack('>I', wire.MAX_BODY_V3 + 1) + b'x'
        with self.assertRaises(TransportError):
            channel.receive_frame(10)
        self.assertEqual(b'x', native.channel.incoming)
        native.recover = True
        self.assertTrue(owner.close(10))

    def test_large_raw_ledger_readback_and_completed_reply_survive_unknown(self):
        pair = VECTORS['sessions'][0]['pairs'][2]
        request = bytes.fromhex(pair['request']['frameHex'])
        raw = request[4:] + b' ' * (wire.MAX_BODY_V3 - len(request) + 4)
        request = struct.pack('>I', len(raw)) + raw
        response = wire.decode_frame_v3(bytes.fromhex(pair['response']['frameHex']))
        response['payload']['requestBodySha256'] = wire.sha256(raw)
        response = frame(response)
        with sqlite3.connect(':memory:') as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript('CREATE TABLE instances(id TEXT); INSERT INTO instances VALUES (\'slot-a\');'
                'CREATE TABLE runtime_operations(id TEXT, fingerprint TEXT, command TEXT, instance TEXT, status TEXT, response TEXT);'
                'CREATE TABLE operations(id TEXT); CREATE TABLE reference_operations(id TEXT);'
                'CREATE TABLE writer_operations(id TEXT); CREATE TABLE snapshot_operations(id TEXT);')
            connection.execute('BEGIN')
            self.assertIsNone(operations.reserve(connection, 'slot-a', request))
            operations.complete(connection, 'slot-a', request, response)
            connection.commit()
            connection.execute('BEGIN')
            self.assertEqual(response, operations.readback(connection, 'slot-a', request))
            self.assertEqual(response, operations.unknown(connection, 'slot-a', request))
            self.assertEqual(raw.decode(), json.loads(connection.execute('SELECT response FROM runtime_operations').fetchone()[0])['requestBodyUtf8'])

    def test_idle_prefix_holds_one_native_io_without_reissuing(self):
        native = FakeNative()
        owner = ModControlTransport(['slot'], native=native, clock=lambda: 1, wire_version=3)
        context, raw = descriptor()
        channel = owner.create(context, raw).accept(identity(13), 10)
        raw = b'\0\0\0\x02{}'
        native.channel.incoming = raw
        seen = []

        def idle():
            seen.append(len(native.operations))
            native.recover = True

        self.assertEqual(raw, channel.receive_frame(10, idle=idle))
        self.assertEqual([2], seen)
        self.assertTrue(owner.close(10))

    def test_completion_waits_peer_eof_and_rejects_extra_bytes_or_pending_close(self):
        for outcome in ('eof', 'extra', 'pending'):
            with self.subTest(outcome=outcome):
                native = FakeNative()
                owner = ModControlTransport(['slot'], native=native, clock=lambda: 1, wire_version=3)
                context, raw = descriptor()
                channel = owner.create(context, raw).accept(identity(13), 10)
                native.channel.incoming = b'x' if outcome == 'extra' else b''
                if outcome == 'pending':
                    native.stall = len(native.operations) + 1
                if outcome == 'eof':
                    self.assertTrue(channel.wait_peer_close(10))
                else:
                    with self.assertRaises(TransportError):
                        channel.wait_peer_close(10)
                if outcome == 'pending':
                    self.assertIsNotNone(channel.pending)
                native.recover = True
                self.assertTrue(owner.close(10))

    def test_windows_broken_pipe_is_eof_only_for_terminal_read_without_bytes(self):
        operation = object.__new__(_WinOperation)
        operation.terminal, operation.kind, operation.error, operation.transferred = True, 'read', 109, 0
        self.assertEqual(b'', operation.finish(10, lambda: 1))
        for kind, code, transferred in [('write', 109, 0), ('read', 5, 0), ('read', 109, 1)]:
            operation.kind, operation.error, operation.transferred = kind, code, transferred
            with self.subTest(kind=kind, code=code, transferred=transferred), self.assertRaises(OSError):
                operation.finish(10, lambda: 1)


if __name__ == '__main__':
    unittest.main()
