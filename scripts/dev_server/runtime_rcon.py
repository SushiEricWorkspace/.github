"""識別済み自身のRCONへだけ送信する。秘密を返却・監査しない。"""
import secrets
import socket
import struct
import time

from .registry import require


def stop(address: str, port: int, password: str, timeout: float, *, verify) -> None:
    command(address, port, password, timeout, "stop", verify=verify)


def command(address: str, port: int, password: str, timeout: float, text: str, *, verify) -> str:
    """秘密送信前とコマンド送信前に所有確認し、期限内の単一RCON応答を返す。"""
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
        require(body[-2:] == b"\0\0", "RCON_FAILED", "RCON応答末尾が不正です")
        return struct.unpack("<ii", body[:8]), body[8:-2].decode("utf-8")

    with socket.create_connection((address, port), timeout=remaining()) as client:
        verify()
        packet(client, 3, password)
        auth, _ = response(client)
        if auth[1] == 0:
            auth, _ = response(client)
        require(auth == (request_id, 2), "RCON_FAILED", "RCON認証を確認できません")
        verify()
        packet(client, 2, text)
        reply, result = response(client)
        require(reply == (request_id, 0), "RCON_FAILED", "RCON応答を確認できません")
        remaining()
        return result
