"""所有JVMのloopback WebSocket。操作用単発socketと監視専用socketを分離する。"""
import base64
import hashlib
import os
import select
import socket
import struct
import time

from .local_ipc import MAX_MESSAGE, decode, encode
from .registry import require, ttl


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


class WebSocket:
    """期限・frame上限・peer Closeを検査する。中断時のsocket closeは正常終了証明にしない。"""
    def __init__(self, endpoint, timeout, *, verify):
        self.deadline = time.monotonic() + timeout
        verify()
        require(endpoint["address"] == "127.0.0.1" and endpoint["protocol"] == "tcp", "ENDPOINT_MISMATCH", "Managementはloopback TCP限定です")
        self.stream = socket.create_connection((endpoint["address"], endpoint["port"]), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            self._remaining()
            self.stream.sendall((f"GET /management HTTP/1.1\r\nHost: 127.0.0.1:{endpoint['port']}\r\n"
                                 f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                                 "Sec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                require(len(header) < 8192, "API_PROTOCOL", "API handshakeヘッダーが大きすぎます")
                header.extend(self._read(1))
            lines = header.decode("ascii").split("\r\n")
            headers = {name.lower(): value.strip() for line in lines[1:] if ":" in line for name, value in [line.split(":", 1)]}
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            require(lines[0].startswith("HTTP/1.1 101 ") and headers.get("sec-websocket-accept") == accept
                    and headers.get("upgrade", "").lower() == "websocket" and "upgrade" in headers.get("connection", "").lower(),
                    "API_PROTOCOL", "API handshakeが一致しません")
            verify()
        except BaseException:
            self.abort()
            raise

    def begin(self, timeout):
        self.deadline = time.monotonic() + timeout

    def _remaining(self):
        seconds = self.deadline - time.monotonic()
        require(seconds > 0, "API_TIMEOUT", "API完了確認の期限を超えました")
        self.stream.settimeout(seconds)

    def _read(self, size):
        value = bytearray()
        while len(value) < size:
            self._remaining()
            part = self.stream.recv(size - len(value))
            require(bool(part), "API_DISCONNECTED", "API完了前に切断されました")
            value.extend(part)
        return bytes(value)

    def send(self, opcode, body):
        self._remaining()
        mask = os.urandom(4)
        length = len(body)
        require(length < MAX_MESSAGE, "MESSAGE_TOO_LARGE", "API本文がIPC上限を超えます")
        header = bytes([0x80 | opcode, 0x80 | length]) if length < 126 else bytes([0x80 | opcode, 0xfe]) + struct.pack("!H", length)
        self.stream.sendall(header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(body)))

    def receive(self):
        content = bytearray()
        started = False
        while True:
            first, second = self._read(2)
            opcode, final = first & 15, bool(first & 128)
            require(not first & 0x70 and not second & 128, "API_PROTOCOL", "APIフレームが不正です")
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            require(length < MAX_MESSAGE and len(content) + length < MAX_MESSAGE, "MESSAGE_TOO_LARGE", "API応答がIPC上限を超えます")
            body = self._read(length)
            if opcode in {8, 9, 10}:
                require(final and length <= 125 and (opcode != 8 or length != 1), "API_PROTOCOL", "API制御frameが不正です")
                if opcode == 8:
                    return opcode, body
                if opcode == 9:
                    self.send(10, body)
                continue
            require(opcode == (0 if started else 1), "API_PROTOCOL", "APIテキスト応答が必要です")
            content.extend(body)
            if final:
                return 1, bytes(content)
            started = True

    def message(self):
        opcode, body = self.receive()
        require(opcode == 1, "API_DISCONNECTED", "API完了応答前にCloseされました")
        return decode(body)

    def close(self, *, drain=False):
        self.send(8, struct.pack("!H", 1000))
        while True:
            opcode, body = self.receive()
            if opcode == 8:
                self.abort()
                return
            require(drain, "API_PROTOCOL", "Close待機中に未知の応答がありました")
            require(decode(body).get("type") == "monitor_update", "API_PROTOCOL", "Close待機中の監視応答が不正です")

    def abort(self):
        self.stream.close()


def exchange(endpoint, message, operation_id, timeout, *, verify):
    """応答不明・Close未完はwriterを保留する。API successを書き換えない。"""
    stream = WebSocket(endpoint, timeout, verify=verify)
    try:
        request = dict(message, nonce=message.get("nonce") or operation_id)
        stream.send(1, encode(request))
        response = stream.message()
        require(response.get("nonce") == request["nonce"], "API_PROTOCOL", "API応答nonceが一致しません")
        stream.close()
        return response
    finally:
        stream.abort()


class Monitor:
    """subscribe/unsubscribeだけを送る監視socket。全操作で現在の貸出・runを再検査する。"""
    def __init__(self, endpoint, *, verify):
        self.verify = verify
        self.socket = WebSocket(endpoint, 5, verify=verify)

    def _subscription(self, subscribed, operation):
        while True:
            response = self.socket.message()
            if response.get("type") == "monitor_update":
                continue
            require(response.get("type") == "monitor_subscription" and response.get("nonce") == operation
                    and response.get("subscribed") is subscribed, "API_PROTOCOL", "監視購読応答が一致しません")
            return response

    def subscribe(self, interval, operation):
        self.verify()
        self.socket.send(1, encode({"type": "monitor_subscribe", "intervalTicks": interval, "nonce": operation}))
        result = self._subscription(True, operation)
        self.verify()
        return result

    def next(self, timeout):
        self.verify()
        self.socket.begin(timeout)
        if not select.select([self.socket.stream], [], [], timeout)[0]:
            self.verify()
            return {"events": []}
        response = self.socket.message()
        require(response.get("type") == "monitor_update", "API_PROTOCOL", "監視通知が必要です")
        self.verify()
        return {"events": [response]}

    def close(self, operation):
        self.verify()
        self.socket.begin(5)
        self.socket.send(1, encode({"type": "monitor_unsubscribe", "nonce": operation}))
        self._subscription(False, operation)
        self.socket.close(drain=True)
        self.verify()

    def abort(self):
        self.socket.abort()
