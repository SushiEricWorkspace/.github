"""DB proxy専用の非blocking私設stdio frame。timeout後も未完bytesを保持する。"""
import errno
import os
import stat
import struct
import threading
import time
from .local_ipc import MAX_MESSAGE, encode, decode


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    def _same_pipe(fd, pin):
        try:
            compare = ctypes.WinDLL('kernelbase', use_last_error=True).CompareObjectHandles
        except AttributeError as error:
            raise OSError('pipe同一実体の照合APIが利用できません') from error
        compare.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        compare.restype = wintypes.BOOL
        return bool(compare(msvcrt.get_osfhandle(fd), msvcrt.get_osfhandle(pin)))
else:
    import fcntl

    def _same_pipe(fd, pin):
        original, held = os.fstat(fd), os.fstat(pin)
        return (stat.S_ISFIFO(original.st_mode) and stat.S_ISFIFO(held.st_mode)
                and original.st_ino != 0
                and (original.st_dev, original.st_ino) == (held.st_dev, held.st_ino)
                and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
                == fcntl.fcntl(pin, fcntl.F_GETFL) & os.O_ACCMODE)


class _PipePin:
    def __init__(self, owner, name, fd):
        self._owner = owner
        self.name, self.fd = name, fd
        self.pin = None
        self.state = 'OPEN'
        self._binding = None

    @property
    def binding(self):
        return self._binding

    @property
    def owner(self):
        return self._owner

    def verify(self, pipe):
        if self.owner is not pipe:
            raise OSError('生成時の元pipe所有枠と一致しません')
        if self.state == 'RELEASED':
            if (self.binding is None or self.binding[:2] != (self.name, self.fd)
                    or getattr(pipe, self.name) is not None or self.pin is not None):
                raise OSError('解放済みpipeの所有枠が変更されています')
            return
        if (self.state != 'OPEN' or self.binding != (self.name, self.fd, self.pin)
                or getattr(pipe, self.name) != self.fd
                or self.pin is None or not _same_pipe(self.fd, self.pin)):
            raise OSError('元pipeの同一実体を確認できません')

    def close(self, pipe, verify):
        binding = self.binding
        pins, lock = pipe.pins, pipe._lock
        if self.state == 'OPEN' and verify is not None:
            verify()
        self.verify(pipe)
        if self.state == 'RELEASED':
            return
        if verify is not None:
            verify()
        if (self.binding is not binding or binding != (self.name, self.fd, self.pin)
                or self.state != 'OPEN' or getattr(pipe, self.name) != self.fd
                or self.owner is not pipe or pipe.pins is not pins or pipe._lock is not lock):
            raise OSError('元pipeの所有枠が変更されています')
        self.state = 'UNKNOWN'
        os.close(binding[1])
        if self.binding is not binding or binding != (self.name, self.fd, self.pin) or self.state != 'UNKNOWN':
            raise OSError('元pipe解放後の所有bindingが変更されています')
        setattr(pipe, binding[0], None)
        self.state = 'PIN_PENDING'
        if verify is not None:
            verify()
        if (self.binding is not binding or self.state != 'PIN_PENDING' or self.pin != binding[2]
                or getattr(pipe, binding[0]) is not None
                or self.owner is not pipe or pipe.pins is not pins or pipe._lock is not lock):
            raise OSError('元pipe所有pinの解放進捗が変更されています')
        self.state = 'UNKNOWN'
        os.close(binding[2])
        if self.binding is not binding or binding != (self.name, self.fd, self.pin) or self.state != 'UNKNOWN':
            raise OSError('pin解放後の所有bindingが変更されています')
        self.pin = None
        self.state = 'RELEASED'


class ProxyPipe:
    """4byte長を含む64KiB frameを保持し、同要求だけを期限後に続行する。

    呼出側はwriter正本・native本人をverifyで再照合し、対応frameの記録後に
    acknowledgedを呼ぶ。完成bufferの再照合は期限切れでも本人検証後に返すが、
    新たな送受信I/Oは期限内に限る。pipe解放は未生成、又は所有childの実終了確認後に限る。
    生成途中の整理不明は例外.pipeに元objectを渡し、呼出元が保全する。
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._pins = ()
        self.child_input = self.output = self.input = self.child_output = None
        try:
            self.child_input, self.output = os.pipe()
            self.input, self.child_output = os.pipe()
            for name in ('child_input', 'child_output', 'input', 'output'):
                pin = _PipePin(self, name, getattr(self, name))
                self._pins += (pin,)
                pin.pin = os.dup(pin.fd)
                pin._binding = (pin.name, pin.fd, pin.pin)
                if os.get_inheritable(pin.fd) or os.get_inheritable(pin.pin):
                    raise OSError('pipe所有pinは非継承でなければなりません')
                pin.verify(self)
            os.set_blocking(self.input, False)
            os.set_blocking(self.output, False)
        except BaseException as error:
            error.pipe = self
            try:
                for name in ('child_input', 'child_output', 'input', 'output'):
                    value = getattr(self, name)
                    if value is not None:
                        for pin in self.pins:
                            if pin.name == name:
                                pin.state = 'UNKNOWN'
                        os.close(value)
                        setattr(self, name, None)
                for pin in self.pins:
                    if pin.pin is not None:
                        pin.state = 'UNKNOWN'
                        os.close(pin.pin)
                        pin.pin = None
                    pin.state = 'RELEASED'
            except BaseException as cleanup_error:
                cleanup_error.pipe = self
                raise
            raise
        self.outgoing = None
        self.offset = 0
        self.incoming = bytearray()
        self.expected = None

    @property
    def pins(self):
        """生成時に固定した所有pin群。解放後も元識別と進捗を保持する。"""
        return self._pins

    def close_child_ends(self):
        self._close(('child_input', 'child_output'), None)

    def verify_owned(self):
        """生成時のpinで未解放端点を照合する。番号だけの保持枠は受理しない。"""
        lock = self._lock
        if not lock.acquire(blocking=False):
            raise OSError('pipe所有枠の作用が進行中です')
        try:
            if tuple(pin.name for pin in self.pins) != ('child_input', 'child_output', 'input', 'output'):
                raise OSError('生成時のpipe所有pinがありません')
            for pin in self.pins:
                pin.verify(self)
        finally:
            lock.release()

    def _close(self, names, verify):
        lock = self._lock
        if not lock.acquire(blocking=False):
            raise OSError('pipe所有枠の作用が進行中です')
        try:
            pins = self.pins
            if tuple(pin.name for pin in pins) != ('child_input', 'child_output', 'input', 'output'):
                raise OSError('生成時のpipe所有pinがありません')
            bindings = tuple(pin.binding for pin in pins)
            for pin in pins:
                if pin.name in names:
                    pin.close(self, verify)
                if self.pins is not pins:
                    raise OSError('pipe所有pinが変更されています')
            if any(pin.owner is not self or pin.binding is not binding or binding[:2] != (pin.name, pin.fd)
                   or pin.state != 'RELEASED' or pin.pin is not None or getattr(self, pin.name) is not None
                   for pin, binding in zip(pins, bindings) if pin.name in names):
                raise OSError('pipe正常解放の保持bindingが変更されています')
        finally:
            lock.release()

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

    def close_after_exit(self, verify=None):
        self._close(('child_input', 'child_output', 'input', 'output'), verify)
