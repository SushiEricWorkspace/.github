"""実TCPでRCON認証/応答失敗を注入し、stop以外の経路へ逃げない。"""
from concurrent.futures import ThreadPoolExecutor
import socket
import struct
import time
import unittest

from .registry import RegistryError
from .runtime_rcon import stop


class RconTests(unittest.TestCase):
    def test_slow_trickle_cannot_extend_whole_rcon_deadline(self):
        seen = []
        with socket.socket() as server, ThreadPoolExecutor(max_workers=1) as workers:
            server.bind(("127.0.0.1", 0))
            server.listen()
            server.settimeout(2)
            def serve():
                with server.accept()[0] as client:
                    client.settimeout(2)
                    header = client.recv(4)
                    body = b""
                    length = struct.unpack("<i", header)[0]
                    while len(body) < length:
                        body += client.recv(length - len(body))
                    request, kind = struct.unpack("<ii", body[:8])
                    seen.append(kind)
                    answer = struct.pack("<iii", 10, request, 2) + b"\0\0"
                    for byte in answer:
                        try:
                            client.sendall(bytes([byte]))
                        except OSError:
                            break
                        time.sleep(0.04)
            pending = workers.submit(serve)
            began = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    stop("127.0.0.1", server.getsockname()[1], "fixture-secret", 0.15, verify=lambda: None)
                self.assertLess(time.monotonic() - began, 0.4)
            finally:
                pending.result(timeout=3)
        self.assertEqual([3], seen)

    def exchange(self, mode):
        seen, checks = [], []
        with socket.socket() as server, ThreadPoolExecutor(max_workers=1) as workers:
            server.bind(("127.0.0.1", 0))
            server.listen()
            server.settimeout(2)

            def serve():
                with server.accept()[0] as client:
                    client.settimeout(2)
                    def exact(size):
                        result = b""
                        while len(result) < size:
                            part = client.recv(size - len(result))
                            if not part:
                                return None
                            result += part
                        return result
                    def receive():
                        header = exact(4)
                        if header is None:
                            return None
                        body = exact(struct.unpack("<i", header)[0])
                        request, kind = struct.unpack("<ii", body[:8])
                        seen.append((kind, body[8:-2].decode()))
                        return request
                    def send(request, kind):
                        body = struct.pack("<ii", request, kind) + b"\0\0"
                        packet = struct.pack("<i", len(body)) + body
                        client.sendall(packet[:3])
                        client.sendall(packet[3:])
                    request = receive()
                    if mode == "oversized":
                        client.sendall(struct.pack("<i", 65537))
                        return
                    send(request, 0)
                    send(-1 if mode == "denied" else request, 2)
                    request = receive()
                    if request is not None:
                        send(request + 1 if mode == "wrong-id" else request, 0)

            pending = workers.submit(serve)
            try:
                stop("127.0.0.1", server.getsockname()[1], "fixture-secret", 2,
                     verify=lambda: checks.append(True))
            finally:
                pending.result(timeout=3)
        return seen, checks

    def test_fragmented_success_checks_ownership_before_secret_and_stop(self):
        seen, checks = self.exchange("success")
        self.assertEqual([(3, "fixture-secret"), (2, "stop")], seen)
        self.assertEqual(2, len(checks))

    def test_authentication_wrong_response_and_oversized_fail(self):
        for mode in ("denied", "wrong-id", "oversized"):
            with self.subTest(mode=mode), self.assertRaises(RegistryError) as error:
                self.exchange(mode)
            self.assertEqual("RCON_FAILED", error.exception.code)
            self.assertNotIn("fixture-secret", str(error.exception))
