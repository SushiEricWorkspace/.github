"""所有JVMへの単発API仲介。応答とCloseまでinstance lockを保持する。"""
import base64
import hashlib
import os
import socket
import struct
import time

from .local_ipc import MAX_MESSAGE, decode, encode
from .registry import fields, require, ttl


def validate_message(message, timeout):
    ttl(timeout)
    require(type(message) is dict and type(message.get("type")) is str, "INVALID_INPUT", "API要求種別が必要です")
    shape = {"ping": set(), "money_history_save": {"playerUuid"}, "achievement_save": {"playerUuid"},
             "command_execute": {"command"}, "command_complete": {"command", "cursor"}}
    require(message["type"] in shape, "INVALID_INPUT", "単発API仲介の対象外です")
    require(set(message) <= {"type", "nonce"} | shape[message["type"]], "INVALID_INPUT", "未知のAPI要求項目があります")
    require(message.get("nonce") is None or type(message["nonce"]) is str, "INVALID_INPUT", "nonce形式が不正です")
    if message["type"].startswith("command_"):
        require(type(message.get("command")) is str, "INVALID_INPUT", "コマンド文字列が必要です")
    if "playerUuid" in message:
        require(message["playerUuid"] is None or type(message["playerUuid"]) is str, "INVALID_INPUT", "UUID形式が不正です")
    encode(message)


def exchange(endpoint, message, operation_id, timeout, *, verify):
    """応答不明・Close未完は呼出元がwriterを保留し、同じ変更を再実行しない。"""
    deadline = time.monotonic() + timeout
    verify()
    require(endpoint["address"] == "127.0.0.1" and endpoint["protocol"] == "tcp", "ENDPOINT_MISMATCH", "Managementはloopback TCP限定です")
    with socket.create_connection((endpoint["address"], endpoint["port"]), timeout=timeout) as stream:
        def remaining():
            seconds = deadline - time.monotonic()
            require(seconds > 0, "API_TIMEOUT", "API完了確認の期限を超えました")
            stream.settimeout(seconds)

        def read(size):
            value = bytearray()
            while len(value) < size:
                remaining()
                part = stream.recv(size - len(value))
                require(bool(part), "API_DISCONNECTED", "API完了前に切断されました")
                value.extend(part)
            return bytes(value)

        def send(opcode, body):
            remaining()
            mask = os.urandom(4)
            length = len(body)
            require(length < MAX_MESSAGE, "MESSAGE_TOO_LARGE", "API本文がIPC上限を超えます")
            header = bytes([0x80 | opcode, 0x80 | length]) if length < 126 else bytes([0x80 | opcode, 0xfe]) + struct.pack("!H", length)
            stream.sendall(header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(body)))

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        remaining()
        stream.sendall((f"GET /management HTTP/1.1\r\nHost: 127.0.0.1:{endpoint['port']}\r\n"
                        f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            require(len(header) < 8192, "API_PROTOCOL", "API handshakeヘッダーが大きすぎます")
            header.extend(read(1))
        lines = header.decode("ascii").split("\r\n")
        headers = {name.lower(): value.strip() for line in lines[1:] if ":" in line for name, value in [line.split(":", 1)]}
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        require(lines[0].startswith("HTTP/1.1 101 ") and headers.get("sec-websocket-accept") == accept
                and headers.get("upgrade", "").lower() == "websocket" and "upgrade" in headers.get("connection", "").lower(),
                "API_PROTOCOL", "API handshakeが一致しません")
        verify()
        request = dict(message, nonce=message.get("nonce") or operation_id)
        send(1, encode(request))
        response, content, waiting_close = None, bytearray(), False
        while True:
            first, second = read(2)
            opcode, final = first & 15, bool(first & 128)
            require(not first & 0x70 and not second & 128, "API_PROTOCOL", "APIフレームが不正です")
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", read(8))[0]
            require(length < MAX_MESSAGE and len(content) + length < MAX_MESSAGE, "MESSAGE_TOO_LARGE", "API応答がIPC上限を超えます")
            body = read(length)
            if opcode == 8:
                require(waiting_close and response is not None, "API_DISCONNECTED", "API完了応答前にCloseされました")
                return response
            if opcode == 9:
                require(final and length <= 125, "API_PROTOCOL", "API Pingが不正です")
                send(10, body)
                continue
            if opcode == 10:
                continue
            require(not waiting_close and opcode in {0, 1}, "API_PROTOCOL", "APIテキスト応答が必要です")
            content.extend(body)
            if final:
                value = decode(bytes(content))
                content.clear()
                require(value.get("nonce") == request["nonce"], "API_PROTOCOL", "API応答nonceが一致しません")
                response = value
                send(8, struct.pack("!H", 1000))
                waiting_close = True
