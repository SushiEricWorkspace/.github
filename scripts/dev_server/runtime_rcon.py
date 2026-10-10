"""識別済み自身のRCONへだけ正常stopを送る。秘密を返却・監査しない。"""
import secrets
import socket
import struct
import time

from .registry import require


def stop(address: str, port: int, password: str, timeout: float, *, verify) -> None:
    deadline = time.monotonic() + timeout
    request_id = secrets.randbelow(2147483646) + 1

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("RCON通信期限を超過しました")
        return seconds

    def packet(client, kind, text):
        body = struct.pack("<ii", request_id, kind) + text.encode() + b"\0\0"
        client.settimeout(remaining())
        client.sendall(struct.pack("<i", len(body)) + body)

    def response(client):
        def exact(size):
            data = b""
            while len(data) < size:
                client.settimeout(remaining())
                block = client.recv(size - len(data))
                require(bool(block), "RCON_FAILED", "RCON応答が途中で終了しました")
                data += block
            return data
        length = struct.unpack("<i", exact(4))[0]
        require(10 <= length <= 65536, "RCON_FAILED", "RCON応答長が不正です")
        body = exact(length)
        return struct.unpack("<ii", body[:8])

    with socket.create_connection((address, port), timeout=remaining()) as client:
        verify()
        packet(client, 3, password)
        auth = response(client)
        if auth[1] == 0:
            auth = response(client)
        require(auth == (request_id, 2), "RCON_FAILED", "RCON認証を確認できません")
        verify()
        packet(client, 2, "stop")
        require(response(client) == (request_id, 0), "RCON_FAILED", "RCON停止応答を確認できません")
        remaining()
