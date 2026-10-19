"""DB proxy専用の非blocking私設stdio frame。timeout後も未完bytesを保持する。"""
import errno
import os
import struct
import time
from .local_ipc import MAX_MESSAGE, encode, decode


class ProxyPipe:
    """4byte長を含む64KiB frameを保持し、同要求だけを期限後に続行する。

    呼出側はwriter正本・native本人をverifyで再照合し、対応frameの記録後に
    acknowledgedを呼ぶ。完成bufferの再照合は期限切れでも本人検証後に返すが、
    新たな送受信I/Oは期限内に限る。pipe解放は未生成、又は所有childの実終了確認後に限る。
    """
    def __init__(self):
        self.child_input = self.output = self.input = self.child_output = None
        try:
            self.child_input, self.output = os.pipe()
            self.input, self.child_output = os.pipe()
            os.set_blocking(self.input, False)
            os.set_blocking(self.output, False)
        except BaseException:
            self.close_after_exit()
            raise
        self.outgoing = None
        self.offset = 0
        self.incoming = bytearray()
        self.expected = None

    def close_child_ends(self):
        for name in ('child_input', 'child_output'):
            value = getattr(self, name)
            if value is not None:
                os.close(value)
                setattr(self, name, None)

    @staticmethod
    def would_block(error):
        return error.errno in (errno.EAGAIN, errno.EWOULDBLOCK) or getattr(error, 'winerror', None) == 232

    @staticmethod
    def wait(deadline, verify):
        verify()
        if time.monotonic() >= deadline:
            raise TimeoutError('DB proxyの待機期限です')
        time.sleep(0.01)

    def send(self, message, deadline, verify):
        body = encode(message)
        if len(body) + 4 > MAX_MESSAGE:
            raise ValueError('私設frameが64KiBを超えています')
        if b'\n' in body or b'\r' in body:
            raise ValueError('私設frameは単一行UTF-8 JSONです')
        frame = struct.pack('!I', len(body)) + body
        if self.outgoing is None:
            self.outgoing = frame
        elif self.outgoing != frame:
            raise ValueError('未完送信に別の要求を混ぜられません')
        while self.offset < len(frame):
            verify()
            if time.monotonic() >= deadline:
                raise TimeoutError('DB proxyの待機期限です')
            try:
                count = os.write(self.output, frame[self.offset:])
            except OSError as error:
                if not self.would_block(error):
                    raise
                count = 0
            self.offset += count
            if not count:
                self.wait(deadline, verify)

    def receive(self, deadline, verify):
        while True:
            verify()
            if self.expected is None and len(self.incoming) >= 4:
                self.expected = struct.unpack('!I', self.incoming[:4])[0]
                if not 0 < self.expected <= MAX_MESSAGE - 4:
                    raise ValueError('私設frameの長さが不正です')
            if self.expected is not None and len(self.incoming) >= self.expected + 4:
                if len(self.incoming) != self.expected + 4:
                    raise ValueError('未要求のframeが続いています')
                body = bytes(self.incoming[4:])
                if b'\n' in body or b'\r' in body:
                    raise ValueError('私設frameに生の改行があります')
                return decode(body.decode('utf-8', errors='strict'))
            if time.monotonic() >= deadline:
                raise TimeoutError('DB proxyの待機期限です')
            try:
                chunk = os.read(self.input, MAX_MESSAGE - len(self.incoming))
            except OSError as error:
                if not self.would_block(error):
                    raise
                self.wait(deadline, verify)
                continue
            if not chunk:
                raise EOFError('DB proxy pipeが終了しました')
            self.incoming.extend(chunk)

    def acknowledged(self):
        # 呼出側が対応frameを照合・記録した後だけ次へ進む。writerの完了とは別。
        self.outgoing = None
        self.offset = 0
        self.incoming.clear()
        self.expected = None

    def close_after_exit(self):
        self.close_child_ends()
        for name in ('input', 'output'):
            value = getattr(self, name)
            if value is not None:
                os.close(value)
                setattr(self, name, None)
