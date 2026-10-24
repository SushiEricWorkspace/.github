"""Mod専用raw transport。native本人と資源を所有し、業務予約・GRANT・再送は扱わない。"""
import copy
from contextlib import contextmanager
import ctypes
import hashlib
import math
import os
from pathlib import Path
import select
import socket
import stat
import struct
import sys
import threading
import time

from .mod_control_wire import MAX_FRAME, _identity, absolute_path, decode_descriptor, descriptor_path


class TransportError(OSError):
    """転送の不明をcallerへ返す。台帳状態や作用前拒否へ自動変換しない。

    transferredは当該frameで完了確認できたbyte数。cancel_pendingなら資源ownerが
    終端まで枠を保持する。COMPLETED原応答の格下げ判断には使用しない。
    """
    def __init__(self, message, *, transferred=0, cancel_pending=False):
        super().__init__(message)
        self.transferred = transferred
        self.cancel_pending = cancel_pending


def _require(condition, message):
    if not condition:
        raise TransportError(message)


def _deadline(value):
    _require(type(value) in (float, int) and math.isfinite(value), "絶対deadlineが不正です")


class ModControlTransport:
    """inventoryの有限instance集合につきrun枠を一つ所有する。

    同instanceの旧run、取消未終端、close失敗が残れば再利用しない。callerはこのownerを
    supervisor寿命で強参照し、close=Falseを正常shutdownとして扱わない。
    native未指定時だけ対象OSのadapterを初期化する。通常CLIには接続していない。
    """
    def __init__(self, instance_ids, *, native=None, clock=time.monotonic):
        self.instances = frozenset(instance_ids)
        _require(bool(self.instances), "inventoryのinstance集合が必要です")
        self.native = native if native is not None else _native()
        self.clock = clock
        self.endpoints = {}
        self.closing = False
        self.lock = threading.Lock()

    def create(self, context: dict, raw_descriptor: bytes):
        """caller固定contextへ原descriptorを照合し、新規file/endpointを所有する。

        contextはroot/run/lease/世代/P本人/期待hashを持つ。本文から期待値を採用しない。
        既存file/socketは拒否し、途中失敗の資源もclose成功までownerへ保持する。
        """
        context = copy.deepcopy(context)
        keys = {"managementRoot", "instanceId", "leaseId", "epoch", "generationId", "runtimeRunId",
                "controller", "descriptorSha256"}
        _require(type(context) is dict and context.keys() == keys, "固定contextのfieldが不正です")
        _require(type(context["epoch"]) is int, "context epochは整数が必要です")
        _identity(context["controller"])
        descriptor = decode_descriptor(raw_descriptor)
        _require(hashlib.sha256(raw_descriptor).hexdigest() == context["descriptorSha256"], "descriptor原hashが一致しません")
        _require(all(descriptor[k] == context[k] for k in keys - {"descriptorSha256"}), "caller固定contextが一致しません")
        instance = context["instanceId"]
        _require(instance in self.instances, "inventoryにないinstanceです")
        path = absolute_path(context["managementRoot"]) / "manager" / "control" / context["runtimeRunId"] / "mod-control.json"
        _require(str(path) == descriptor_path(descriptor), "固定descriptor位置が一致しません")
        with self.lock:
            _require(not self.closing, "transportはshutdown中です")
            _require(instance not in self.endpoints, "旧runまたは未回収資源が残っています")
            endpoint = _Endpoint(self, context, descriptor, raw_descriptor)
            self.endpoints[instance] = endpoint
        try:
            with endpoint.lock:
                _require(not self.closing and not endpoint.closing, "run初期化は閉鎖中です")
                endpoint.controller = self.native.pin_controller(context["controller"])
                endpoint.controller.open(context["controller"])
                _require(not self.closing and not endpoint.closing, "P確認中に閉鎖されました")
                _require(descriptor["endpoint"]["transport"] == self.native.transport, "OSとendpointが一致しません")
                endpoint.pin = self.native.descriptor(path, raw_descriptor)
                endpoint.pin.create(raw_descriptor)
                _require(not self.closing and not endpoint.closing, "descriptor作成中に閉鎖されました")
                endpoint.pin.check(raw_descriptor)
                endpoint.listener = self.native.listen(descriptor["endpoint"], endpoint.pin)
                endpoint.listener.open()
                _require(not self.closing and not endpoint.closing, "run初期化中に閉鎖されました")
            return endpoint
        except BaseException:
            endpoint.closing = True
            endpoint.close(self.clock())
            raise

    def reap(self):
        """保持I/Oの終端だけを非blockingで回収する。新要求や再送を発行しない。"""
        with self.lock:
            endpoints = list(self.endpoints.values())
        for endpoint in endpoints:
            endpoint.reap()

    def close(self, deadline: float) -> bool:
        """受付を閉じ、全資源の終端時だけTrue。期限到達だけで枠を解放しない。"""
        _deadline(deadline)
        with self.lock:
            self.closing = True
            endpoints = list(self.endpoints.values())
        return all([endpoint.close(deadline) for endpoint in endpoints])


class _Endpoint:
    def __init__(self, owner, context, descriptor, raw):
        self.owner, self.context, self.descriptor, self.raw = owner, context, descriptor, raw
        self.pin = self.listener = self.connection = self.controller = None
        self.closing = self.close_unknown = False
        self.lock = threading.Lock()

    def accept(self, expected_m_identity: dict, deadline: float):
        """Pが生成・保持したM本人だけを同pipe/socketで認証する。

        expectedはdescriptor/HELLO本文から生成しない。接続枠はrunあたり一つ。
        timeout/認証失敗でも未終端native I/Oはownerが保持し、予約や応答を生成しない。
        """
        _deadline(deadline)
        _require(self.lock.acquire(blocking=False), "run初期化または終了処理が実行中です")
        try:
            _require(not self.owner.closing and not self.closing and self.connection is None, "run接続枠が使用中です")
            expected = copy.deepcopy(expected_m_identity)
            _identity(expected)
            self.pin.check(self.raw)
            connection = _Connection(self, expected)
            connection.lock.acquire()  # 非公開の新objectなので競合しない。公開より先にI/O所有を固定する。
            self.connection = connection
        finally:
            self.lock.release()
        try:
            connection.peer = self.owner.native.pin_process(expected)
            connection.peer.open(expected)
            _require(not self.closing and not connection.closing, "accept中に閉鎖されました")
            connection.channel = self.listener.connection()
            connection._io("accept", None, deadline)
            _require(not self.closing and not connection.closing, "accept中に閉鎖されました")
            self.owner.native.authenticate(connection.channel, connection.peer, expected)
            self.pin.check(self.raw)
            _require(not self.owner.closing and not self.closing and not connection.closing, "認証中に閉鎖されました")
            connection.authenticated = True
            return connection
        except BaseException:
            connection.closing = True
            raise
        finally:
            connection.lock.release()
            if connection.closing:
                connection.close(deadline)

    def reap(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            connection = self.connection
        finally:
            self.lock.release()
        if connection is not None:
            connection.reap()
        if self.closing:
            self.close(self.owner.clock())

    def close(self, deadline):
        _deadline(deadline)
        self.closing = True
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if self.close_unknown:
                return False
            connection = self.connection
        finally:
            self.lock.release()
        # connectionのdetachはendpoint.lockを取得するため、ここで保持しない。
        if connection is not None and not connection.close(deadline):
            return False
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if self.close_unknown or self.connection is not None:
                return False
            try:
                if self.listener is not None:
                    self.listener.close()
                    self.listener = None
                if self.pin is not None:
                    self.pin.close()
                    self.pin = None
                if self.controller is not None:
                    self.controller.close()
                    self.controller = None
            except BaseException:
                self.close_unknown = True
                return False
            with self.owner.lock:
                instance = self.context["instanceId"]
                if self.owner.endpoints.get(instance) is self:
                    self.owner.endpoints.pop(instance)
            return True
        finally:
            self.lock.release()


class _Connection:
    def __init__(self, endpoint, expected):
        self.endpoint, self.expected = endpoint, expected
        self.channel = self.peer = self.pending = None
        self.authenticated = self.closing = self.close_unknown = False
        self.lock = threading.Lock()

    @contextmanager
    def _exclusive(self):
        _require(self.lock.acquire(blocking=False), "同じ接続のI/Oが実行中です")
        try:
            yield
        finally:
            self.lock.release()

    def _io(self, kind, value, deadline, *, transferred=0):
        if self.endpoint.owner.clock() >= deadline:
            raise TransportError("I/O発行前にdeadlineを超えました", transferred=transferred)
        _require(self.pending is None, "前I/Oが未終端です")
        operation = self.endpoint.owner.native.operation(self.channel, kind, value)
        # native発行前からendpoint→connection→operationを強参照する。
        self.pending = operation
        try:
            operation.start()
            result = operation.finish(deadline, self.endpoint.owner.clock)
        except BaseException as error:
            if not operation.terminal:
                operation.cancel()
                operation.poll()
            if operation.terminal:
                try:
                    operation.release()
                except BaseException:
                    self.close_unknown = True
                    raise
                self.pending = None
            raise TransportError("native I/Oが完了していません", transferred=transferred + operation.transferred,
                                 cancel_pending=self.pending is not None) from error
        _require(operation.terminal, "native I/O終端が不明です")
        try:
            operation.release()
        except BaseException:
            self.close_unknown = True
            raise
        self.pending = None
        return result

    def _ready(self):
        _require(self.authenticated and not self.closing and self.pending is None, "接続は使用できません")
        self.endpoint.pin.check(self.endpoint.raw)
        self.endpoint.owner.native.authenticate(self.channel, self.peer, self.expected)

    def receive_frame(self, deadline: float) -> bytes:
        """header4byteを先に検査し、全65536byte以内の元frameを返す。JSON再整形しない。"""
        _deadline(deadline)
        with self._exclusive():
            self._ready()
            result = bytearray()
            try:
                for target in (4, None):
                    if target is None:
                        length = int.from_bytes(result, "big")
                        _require(0 < length <= MAX_FRAME - 4, "body長が上限外です")
                        target = 4 + length
                    while len(result) < target:
                        data = self._io("read", target - len(result), deadline, transferred=len(result))
                        _require(type(data) is bytes and 0 < len(data) <= target - len(result), "frameが途中で切断されました")
                        result.extend(data)
                self._ready()
                return bytes(result)
            except BaseException as error:
                self.closing = True
                if isinstance(error, TransportError):
                    error.transferred = max(error.transferred, len(result))
                raise

    def send_frame(self, raw_frame: bytes, deadline: float):
        """元frameを送る。部分送信/期限超過を未送信へ読み替えず、自動再送しない。"""
        _deadline(deadline)
        _require(type(raw_frame) is bytes and 4 < len(raw_frame) <= MAX_FRAME
                 and int.from_bytes(raw_frame[:4], "big") == len(raw_frame) - 4, "送信frame長が不正です")
        with self._exclusive():
            self._ready()
            sent = 0
            try:
                while sent < len(raw_frame):
                    size = self._io("write", raw_frame[sent:], deadline, transferred=sent)
                    _require(type(size) is int and 0 < size <= len(raw_frame) - sent, "送信byte数が不正です")
                    sent += size
                self._ready()
                return sent
            except BaseException as error:
                self.closing = True
                if isinstance(error, TransportError):
                    error.transferred = max(error.transferred, sent)
                raise

    def reap(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.close_unknown:
                return
            if self.pending is not None:
                self.pending.poll()
                if not self.pending.terminal:
                    return
                try:
                    self.pending.release()
                except BaseException:
                    self.close_unknown = True
                    return
                self.pending = None
        finally:
            self.lock.release()
        if self.closing:
            self.close(self.endpoint.owner.clock())

    def close(self, deadline):
        _deadline(deadline)
        self.closing = True
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if self.close_unknown:
                return False
            if self.pending is not None:
                self.pending.cancel()
                self.pending.poll()
                if not self.pending.terminal:
                    return False
                try:
                    self.pending.release()
                except BaseException:
                    self.close_unknown = True
                    return False
                self.pending = None
            try:
                if self.channel is not None:
                    self.channel.close()
                    self.channel = None
                if self.peer is not None:
                    self.peer.close()
                    self.peer = None
            except BaseException:
                self.close_unknown = True
                return False
            with self.endpoint.lock:
                if self.endpoint.connection is self:
                    self.endpoint.connection = None
            return True
        except OSError:
            return False
        finally:
            self.lock.release()


def _native():
    _require(sys.version_info[:2] == (3, 12), "native adapterはPython3.12専用です")
    if sys.platform == "win32":
        return _WindowsNative()
    if sys.platform == "darwin":
        return _MacNative()
    raise TransportError("対応OSはWindows/macOSです")

class _MacPin:
    def __init__(self, path):
        self.nodes = []
        self.fd = self.writer = None
        self.close_unknown = False
        self.path = Path(path)
        self.parent = None

    def create(self, raw):
        root = self.path.parents[3]
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.nodes.append((fd, None, None, None, False))
        self.nodes[-1] = (fd, None, None, self._id(os.fstat(fd)), False)
        current = Path('/')
        for name in self.path.parent.parts[1:]:
            current /= name
            if current.is_relative_to(root) and current != root:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            self.nodes.append((child, fd, name, None, current.is_relative_to(root)))
            info = os.fstat(child)
            self.nodes[-1] = (child, fd, name, self._id(info), current.is_relative_to(root))
            if current.is_relative_to(root):
                _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "descriptor親がprivateではありません")
            fd = child
        self.parent = fd
        self.writer = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        writer = self.writer
        try:
            view = memoryview(raw)
            while view:
                size = os.write(writer, view)
                _require(size > 0, "descriptor書込が進みません")
                view = view[size:]
            os.fsync(writer)
            identity = self._id(os.fstat(writer))
        finally:
            try:
                os.close(writer)
            except BaseException:
                self.close_unknown = True
                raise
            self.writer = None
        self.fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        _require(self._id(os.fstat(self.fd)) == identity, "descriptorが差し替わりました")
        os.fsync(fd)
        self.check(raw)

    @staticmethod
    def _id(info):
        return info.st_dev, info.st_ino

    def check_parents(self):
        for fd, parent, name, identity, private in self.nodes:
            current = os.fstat(fd)
            _require(stat.S_ISDIR(current.st_mode) and self._id(current) == identity, "descriptor親fdが変わりました")
            if private:
                _require(current.st_uid == os.getuid() and stat.S_IMODE(current.st_mode) == 0o700, "descriptor親の権限が変わりました")
            if parent is not None:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                _require(stat.S_ISDIR(info.st_mode) and self._id(info) == identity, "descriptor親の名前が変わりました")

    def check(self, raw):
        self.check_parents()
        before = os.fstat(self.fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.getuid()
                 and stat.S_IMODE(before.st_mode) == 0o600 and before.st_size == len(raw), "descriptor実体が不正です")
        os.lseek(self.fd, 0, os.SEEK_SET)
        data = bytearray()
        while len(data) <= len(raw):
            part = os.read(self.fd, len(raw) + 1 - len(data))
            if not part:
                break
            data.extend(part)
        after = os.fstat(self.fd)
        named = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
        _require(bytes(data) == raw and (before.st_dev, before.st_ino, before.st_size, before.st_uid, before.st_mode, before.st_nlink)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_uid, after.st_mode, after.st_nlink)
                 and self._id(named) == self._id(after) and stat.S_ISREG(named.st_mode), "descriptor原bytes/identityが変わりました")
        self.check_parents()

    def close(self):
        _require(not self.close_unknown, "descriptor closeは結果不明です")
        try:
            if self.writer is not None:
                os.close(self.writer)
                self.writer = None
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            while self.nodes:
                os.close(self.nodes[-1][0])
                self.nodes.pop()
        except BaseException:
            self.close_unknown = True
            raise


class _MacPeer:
    def __init__(self, native):
        self.native = native

    def open(self, expected):
        self.native.identity(expected)

    def close(self):
        pass  # macOSはPID handleではなく同socket fdと開始識別を各境界で照合する。


class _MacChannel:
    def __init__(self, listener):
        self.listener = listener
        self.stream = None

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class _MacListener:
    def __init__(self, address, pin):
        self.pin, self.address = pin, address
        self.stream = None
        self.identity = None

    def open(self):
        self.stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.pin.check_parents()
        _require(len(os.fsencode(self.address)) < 104, "Unix endpointが長すぎます")
        self.stream.setblocking(False)
        self.stream.bind(self.address)  # 既存socketをunlinkして再利用しない。
        os.chmod('mod-control.sock', 0o600, dir_fd=self.pin.parent, follow_symlinks=False)
        info = os.stat('mod-control.sock', dir_fd=self.pin.parent, follow_symlinks=False)
        _require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o600, "socket実体/権限が不正です")
        self.identity = self.pin._id(info)
        self.pin.check_parents()
        self.stream.listen(1)

    def connection(self):
        return _MacChannel(self)

    def close(self):
        if self.identity is None:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            return
        self.pin.check_parents()
        info = os.stat('mod-control.sock', dir_fd=self.pin.parent, follow_symlinks=False)
        _require(self.pin._id(info) == self.identity and stat.S_ISSOCK(info.st_mode), "socket名の差替えを保全します")
        self.stream.close()
        os.unlink('mod-control.sock', dir_fd=self.pin.parent)


class _MacOperation:
    def __init__(self, channel, kind, value):
        self.channel, self.kind, self.value = channel, kind, value
        self.terminal, self.transferred, self.result = False, 0, None

    def start(self):
        self.poll()

    def poll(self):
        if self.terminal:
            return
        try:
            if self.kind == 'accept':
                stream, _ = self.channel.listener.stream.accept()
                self.channel.stream = stream
                stream.setblocking(False)
                self.result = None
            elif self.kind == 'read':
                self.result = self.channel.stream.recv(self.value)
                self.transferred = len(self.result)
            else:
                self.result = self.channel.stream.send(self.value)
                self.transferred = self.result
            self.terminal = True
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.terminal = True
            raise

    def finish(self, deadline, clock):
        while not self.terminal:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError('socket deadline')
            stream = self.channel.listener.stream if self.kind == 'accept' else self.channel.stream
            if self.kind == 'write':
                select.select([], [stream], [], remaining)
            else:
                select.select([stream], [], [], remaining)
            if clock() >= deadline:
                raise TimeoutError('socket deadline')
            self.poll()
        return self.result

    def cancel(self):
        # 同期nonblocking syscallの呼出は既に終端。未送受信byteの再発行はしない。
        self.terminal = True

    def release(self):
        _require(self.terminal, "socket I/O未終端です")


class _MacNative:
    transport = 'macos-unix'
    def __init__(self):
        self.system = ctypes.CDLL(None, use_errno=True)
        self.proc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        self.system.gethostuuid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.system.gethostuuid.restype = ctypes.c_int
        self.system.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        self.system.sysctlbyname.restype = ctypes.c_int
        self.system.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        self.system.getpeereid.restype = ctypes.c_int
        self.proc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.proc.proc_pidinfo.restype = ctypes.c_int

    def _host_boot(self):
        host = ctypes.create_string_buffer(16)
        timeout = (ctypes.c_long * 2)(5, 0)
        _require(self.system.gethostuuid(host, timeout) == 0, "host本人を取得できません")
        _require(host.raw not in (b'\0' * 16, b'\xff' * 16), "host本人が未設定です")
        boot = ctypes.create_string_buffer(128)
        size = ctypes.c_size_t(len(boot))
        _require(self.system.sysctlbyname(b'kern.bootsessionuuid', boot, ctypes.byref(size), None, 0) == 0, "boot本人を取得できません")
        _require(0 < len(boot.value) < 128, "boot本人が不正です")
        return hashlib.sha256(host.raw).hexdigest(), boot.value.decode('ascii')

    def identity(self, expected):
        host, boot = self._host_boot()
        info = ctypes.create_string_buffer(136)
        _require(self.proc.proc_pidinfo(expected['pid'], 3, 0, info, len(info)) == len(info), "Mの開始識別を取得できません")
        start = '{}:{}'.format(*struct.unpack_from('QQ', info.raw, 120))
        _require(expected == {'hostHash': host, 'bootId': boot, 'pid': expected['pid'], 'startId': start}
                 and self._host_boot() == (host, boot), "native本人が一致しません")
        return struct.unpack_from('I', info.raw, 20)[0]  # proc_bsdinfo.pbi_uid

    def pin_process(self, expected):
        return _MacPeer(self)

    def pin_controller(self, expected):
        _require(expected['pid'] == os.getpid(), "P本人ではありません")
        return self.pin_process(expected)

    def authenticate(self, channel, peer, expected):
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        fd = channel.stream.fileno()
        _require(self.system.getpeereid(fd, ctypes.byref(uid), ctypes.byref(gid)) == 0, "peer UIDが取得できません")
        pid = struct.unpack('i', channel.stream.getsockopt(0, 2, 4))[0]  # SOL_LOCAL/LOCAL_PEERPID
        _require(pid == expected['pid'] and uid.value == os.getuid() == self.identity(expected), "同socketのpeer本人が一致しません")

    def descriptor(self, path, raw):
        return _MacPin(path)

    def listen(self, endpoint, pin):
        _require(endpoint['transport'] == 'macos-unix', "OSとendpointが一致しません")
        return _MacListener(endpoint['address'], pin)

    def operation(self, channel, kind, value):
        return _MacOperation(channel, kind, value)

class _WinPin:
    def __init__(self, native, path):
        self.native, self.nodes, self.handle, self.writer = native, [], None, None
        self.close_unknown = False
        self.path = absolute_path(str(path))

    def create(self, raw):
        native, path = self.native, self.path
        root = path.parents[3]
        handle = native.open_anchor(path.anchor)
        self.nodes.append((handle, True, False, None))
        self.nodes[-1] = (handle, True, False, native.metadata(handle, directory=True)[0])
        current = type(path)(path.anchor)
        for name in path.parent.parts[1:]:
            current /= name
            private = current.is_relative_to(root)
            handle = native.relative(self.nodes[-1][0], name, directory=True, create=private and current != root)
            self.nodes.append((handle, True, private, None))
            self.nodes[-1] = (handle, True, private, native.metadata(handle, directory=True)[0])
            if private:
                native.private(handle)
        self.writer = native.relative(handle, path.name, create=True, write=True)
        writer = self.writer
        try:
            size = ctypes.c_uint32()
            offset = 0
            while offset < len(raw):
                data = ctypes.create_string_buffer(raw[offset:])
                native.check(native.kernel.WriteFile(writer, data, len(raw) - offset, ctypes.byref(size), None))
                _require(0 < size.value <= len(raw) - offset, "descriptor書込が進みません")
                offset += size.value
            native.check(native.kernel.FlushFileBuffers(writer))
            identity = native.metadata(writer)
        finally:
            try:
                native.close_handle(writer)
            except BaseException:
                self.close_unknown = True
                raise
            self.writer = None
        self.handle = native.relative(handle, path.name)
        _require(native.metadata(self.handle) == identity, "descriptor readonly pinが別実体です")
        self.check(raw)

    def check(self, raw):
        native = self.native
        for handle, directory, private, identity in self.nodes:
            _require(native.metadata(handle, directory=directory)[0] == identity, "descriptor親handleが変わりました")
            if private:
                native.private(handle)
        native.private(self.handle)
        before = native.metadata(self.handle)
        _require(before[1] == len(raw), "descriptorのサイズが変わりました")
        native.check(native.kernel.SetFilePointerEx(self.handle, 0, None, 0))
        data, size = ctypes.create_string_buffer(len(raw) + 1), ctypes.c_uint32()
        native.check(native.kernel.ReadFile(self.handle, data, len(data), ctypes.byref(size), None))
        _require(data.raw[:size.value] == raw and native.metadata(self.handle) == before, "descriptor原bytes/identityが変わりました")
        native.private(self.handle)

    def close(self):
        _require(not self.close_unknown, "descriptor closeは結果不明です")
        try:
            if self.writer is not None:
                self.native.close_handle(self.writer)
                self.writer = None
            if self.handle is not None:
                self.native.close_handle(self.handle)
                self.handle = None
            while self.nodes:
                self.native.close_handle(self.nodes[-1][0])
                self.nodes.pop()
        except BaseException:
            self.close_unknown = True
            raise


class _WinPeer:
    def __init__(self, native):
        self.native, self.handle = native, None

    def open(self, expected):
        self.handle = self.native.kernel.OpenProcess(0x101000, False, expected['pid'])
        self.native.check(self.handle)
        self.check(expected)

    def check(self, expected):
        native = self.native
        _require(native.kernel.WaitForSingleObject(self.handle, 0) == 258, "保持processは稼働していません")
        values = [ctypes.c_uint64() for _ in range(4)]
        native.check(native.kernel.GetProcessTimes(self.handle, *(ctypes.byref(v) for v in values)))
        pid = native.kernel.GetProcessId(self.handle)
        host, boot = native.host_boot()
        _require(expected == {'hostHash': host, 'bootId': boot, 'pid': pid, 'startId': str(values[0].value)}
                 and native.security.process_sid(self.handle) == native.security.current_sid(), "保持process本人が一致しません")

    def close(self):
        if self.handle is not None:
            self.native.close_handle(self.handle)
            self.handle = None


class _WinChannel:
    def __init__(self, listener):
        self.listener, self.handle = listener, listener.handle
        self.closed = False

    def close(self):
        if not self.closed:
            success = self.listener.native.kernel.DisconnectNamedPipe(self.handle)
            _require(success or ctypes.get_last_error() == 233, "pipeの切断が完了していません")
            self.closed = True
            self.listener.active = False


class _WinListener:
    def __init__(self, native, address):
        self.native, self.active = native, False
        self.address, self.handle = address, None

    def open(self):
        native = self.native
        with native.security.security_descriptor() as descriptor:
            attributes = native.SecurityAttributes(ctypes.sizeof(native.SecurityAttributes), descriptor, False)
            self.handle = native.kernel.CreateNamedPipeW(self.address, 3 | 0x40000000 | 0x80000, 8, 1,
                                                         MAX_FRAME, MAX_FRAME, 0, ctypes.byref(attributes))
        if self.handle in (None, ctypes.c_void_p(-1).value):
            self.handle = None
            raise TransportError("run pipeを新規所有できません")
        native.private(self.handle)

    def connection(self):
        _require(not self.active, "pipe接続枠が使用中です")
        self.active = True
        return _WinChannel(self)

    def close(self):
        if self.handle is not None:
            _require(not self.active, "pipe接続が未終端です")
            self.native.close_handle(self.handle)
            self.handle = None


class _WinOperation:
    def __init__(self, native, channel, kind, value):
        self.native, self.channel, self.kind, self.value = native, channel, kind, value
        self.overlapped = native.Overlapped()
        self.buffer = None
        self.terminal, self.transferred = True, 0
        self.cancel_requested = False
        self.error = None
        self.cancel_error = None
        self.release_unknown = False

    def start(self):
        kernel = self.native.kernel
        self.overlapped.hEvent = kernel.CreateEventW(None, True, False, None)
        self.native.check(self.overlapped.hEvent)
        if self.kind == 'accept':
            self.terminal = False
            success = kernel.ConnectNamedPipe(self.channel.handle, ctypes.byref(self.overlapped))
        else:
            self.buffer = ctypes.create_string_buffer(self.value if self.kind == 'write' else self.value)
            size = len(self.value) if self.kind == 'write' else self.value
            transferred = ctypes.c_uint32()
            self.terminal = False
            method = kernel.WriteFile if self.kind == 'write' else kernel.ReadFile
            success = method(self.channel.handle, self.buffer, size, ctypes.byref(transferred), ctypes.byref(self.overlapped))
        error = 0 if success else ctypes.get_last_error()
        if self.kind == 'accept' and error == 535:
            self.terminal = True  # ERROR_PIPE_CONNECTEDは同期接続成功。
        elif not success and error != 997:
            self.error, self.terminal = error, True
        else:
            self.poll()

    def poll(self):
        if self.terminal:
            return
        transferred = ctypes.c_uint32()
        success = self.native.kernel.GetOverlappedResult(self.channel.handle, ctypes.byref(self.overlapped), ctypes.byref(transferred), False)
        error = 0 if success else ctypes.get_last_error()
        if error == 996:
            return
        self.transferred = transferred.value
        self.error = error or None
        self.terminal = True

    def finish(self, deadline, clock):
        while not self.terminal:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError('pipe deadline')
            status = self.native.kernel.WaitForSingleObject(self.overlapped.hEvent, min(0xFFFFFFFE, max(1, math.ceil(remaining * 1000))))
            if status not in (0, 258):
                raise TransportError('pipe待機が不明です')
            self.poll()
        if self.error is not None:
            raise OSError(self.error, 'pipe I/Oが失敗しました')
        if self.kind == 'read':
            _require(self.transferred <= self.value, "native readサイズが不正です")
            return self.buffer.raw[:self.transferred]
        return self.transferred if self.kind == 'write' else None

    def cancel(self):
        if not self.terminal and not self.cancel_requested:
            self.cancel_requested = True
            if not self.native.kernel.CancelIoEx(self.channel.handle, ctypes.byref(self.overlapped)):
                self.cancel_error = ctypes.get_last_error()  # NOT_FOUNDもI/O終端の証明ではない。

    def release(self):
        _require(self.terminal and not self.release_unknown, "OVERLAPPEDが未終端またはclose不明です")
        if self.overlapped.hEvent:
            try:
                self.native.close_handle(self.overlapped.hEvent)
            except BaseException:
                self.release_unknown = True
                raise
            self.overlapped.hEvent = None
        self.buffer = None


class _WindowsNative:
    transport = 'windows-pipe'
    def __init__(self):
        from . import windows_security
        self.security = windows_security
        self.kernel = windows_security.kernel
        self.nt = ctypes.WinDLL('ntdll', use_last_error=True)
        handle, pointer, u32 = ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32
        class SecurityAttributes(ctypes.Structure):
            _fields_ = [('nLength', u32), ('lpSecurityDescriptor', pointer), ('bInheritHandle', ctypes.c_int)]
        class Overlapped(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t), ('Offset', u32), ('OffsetHigh', u32), ('hEvent', handle)]
        class UnicodeString(ctypes.Structure):
            _fields_ = [('Length', ctypes.c_ushort), ('MaximumLength', ctypes.c_ushort), ('Buffer', pointer)]
        class ObjectAttributes(ctypes.Structure):
            _fields_ = [('Length', u32), ('RootDirectory', handle), ('ObjectName', ctypes.POINTER(UnicodeString)),
                        ('Attributes', u32), ('SecurityDescriptor', pointer), ('SecurityQualityOfService', pointer)]
        class IoStatus(ctypes.Structure):
            _fields_ = [('Status', pointer), ('Information', ctypes.c_size_t)]
        self.SecurityAttributes, self.Overlapped = SecurityAttributes, Overlapped
        self.UnicodeString, self.ObjectAttributes, self.IoStatus = UnicodeString, ObjectAttributes, IoStatus
        declarations = {
            'CreateFileW': ([ctypes.c_wchar_p, u32, u32, pointer, u32, u32, handle], handle),
            'CreateNamedPipeW': ([ctypes.c_wchar_p, u32, u32, u32, u32, u32, u32, ctypes.POINTER(SecurityAttributes)], handle),
            'ConnectNamedPipe': ([handle, ctypes.POINTER(Overlapped)], ctypes.c_int),
            'DisconnectNamedPipe': ([handle], ctypes.c_int),
            'GetNamedPipeClientProcessId': ([handle, ctypes.POINTER(u32)], ctypes.c_int),
            'CreateEventW': ([pointer, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p], handle),
            'ReadFile': ([handle, pointer, u32, ctypes.POINTER(u32), pointer], ctypes.c_int),
            'WriteFile': ([handle, pointer, u32, ctypes.POINTER(u32), pointer], ctypes.c_int),
            'GetOverlappedResult': ([handle, ctypes.POINTER(Overlapped), ctypes.POINTER(u32), ctypes.c_int], ctypes.c_int),
            'CancelIoEx': ([handle, ctypes.POINTER(Overlapped)], ctypes.c_int),
            'FlushFileBuffers': ([handle], ctypes.c_int),
            'SetFilePointerEx': ([handle, ctypes.c_int64, pointer, u32], ctypes.c_int),
            'GetFileType': ([handle], u32),
            'GetFileInformationByHandleEx': ([handle, ctypes.c_int, pointer, u32], ctypes.c_int),
            'OpenProcess': ([u32, ctypes.c_int, u32], handle),
            'GetProcessId': ([handle], u32),
            'GetProcessTimes': ([handle, pointer, pointer, pointer, pointer], ctypes.c_int),
            'WaitForSingleObject': ([handle, u32], u32),
            'GetSystemFirmwareTable': ([u32, u32, pointer, u32], u32),
        }
        for name, (arguments, result) in declarations.items():
            method = getattr(self.kernel, name)
            method.argtypes, method.restype = arguments, result
        self.nt.NtCreateFile.argtypes = [ctypes.POINTER(handle), u32, ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatus),
                                        pointer, u32, u32, u32, u32, pointer, u32]
        self.nt.NtCreateFile.restype = ctypes.c_int32
        self.nt.RtlNtStatusToDosError.argtypes, self.nt.RtlNtStatusToDosError.restype = [u32], u32
        self.nt.NtQuerySystemInformation.argtypes = [u32, pointer, u32, pointer]
        self.nt.NtQuerySystemInformation.restype = ctypes.c_int32
        windows_security.advapi.GetSecurityDescriptorControl.argtypes = [pointer, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(u32)]
        windows_security.advapi.GetSecurityDescriptorControl.restype = ctypes.c_int

    @staticmethod
    def check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())

    def close_handle(self, handle):
        self.check(self.kernel.CloseHandle(handle))

    def open_anchor(self, anchor):
        handle = self.kernel.CreateFileW(anchor, 0x80000000 | 0x20000 | 0x100000, 1, None, 3, 0x2200000, None)
        _require(handle not in (None, ctypes.c_void_p(-1).value), "固定driveを開けません")
        return handle

    def relative(self, parent, name, *, directory=False, create=False, write=False):
        text = ctypes.create_unicode_buffer(name)
        length = len(name.encode('utf-16-le'))
        _require(0 < length <= 65532 and '/' not in name and '\\' not in name and name not in ('.', '..'), "相対componentが不正です")
        string = self.UnicodeString(length, length + 2, ctypes.cast(text, ctypes.c_void_p))
        attributes = self.ObjectAttributes(ctypes.sizeof(self.ObjectAttributes), parent, ctypes.pointer(string), 0x40, None, None)
        result, status = ctypes.c_void_p(), self.IoStatus()
        options = 0x200000 | 0x20 | (1 if directory else 0x40)
        access = 0x80000000 | 0x20000 | 0x100000 | (0x40000000 if write else 0)
        def opened():
            return self.nt.NtCreateFile(ctypes.byref(result), access, ctypes.byref(attributes), ctypes.byref(status), None, 0,
                                        1, (3 if directory else 2) if create else 1, options, None, 0)
        if create:
            with self.security.security_descriptor(directory=directory) as descriptor:
                attributes.SecurityDescriptor = descriptor
                code = opened()
        else:
            code = opened()
        if code < 0:
            raise ctypes.WinError(self.nt.RtlNtStatusToDosError(code & 0xFFFFFFFF))
        return result.value

    def metadata(self, handle, *, directory=False):
        class Standard(ctypes.Structure):
            _fields_ = [('allocation', ctypes.c_int64), ('size', ctypes.c_int64), ('links', ctypes.c_uint32),
                        ('deleted', ctypes.c_ubyte), ('directory', ctypes.c_ubyte)]
        class FileId(ctypes.Structure):
            _fields_ = [('volume', ctypes.c_uint64), ('file', ctypes.c_ubyte * 16)]
        standard, identity = Standard(), FileId()
        attributes = (ctypes.c_uint32 * 2)()
        self.check(self.kernel.GetFileInformationByHandleEx(handle, 1, ctypes.byref(standard), ctypes.sizeof(standard)))
        self.check(self.kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)))
        self.check(self.kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)))
        _require(self.kernel.GetFileType(handle) == 1 and bool(standard.directory) == directory and not standard.deleted
                 and not attributes[0] & 0x400 and (directory or standard.links == 1), "通常file/directoryのhandleではありません")
        return (identity.volume, bytes(identity.file)), standard.size, attributes[0], standard.links

    def private(self, handle):
        self.security.require_private(handle=handle)
        descriptor = ctypes.c_void_p()
        result = self.security.advapi.GetSecurityInfo(handle, 1, 4, None, None, None, None, ctypes.byref(descriptor))
        _require(result == 0, "DACL controlを取得できません")
        try:
            control, revision = ctypes.c_ushort(), ctypes.c_uint32()
            self.check(self.security.advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
            _require(control.value & 0x1000, "保護DACLが必要です")
        finally:
            self.kernel.LocalFree(descriptor)

    def host_boot(self):
        size = self.kernel.GetSystemFirmwareTable(0x52534D42, 0, None, 0)
        _require(8 <= size <= 2 * 1024 * 1024, "SMBIOSのサイズが不正です")
        buffer = ctypes.create_string_buffer(size)
        _require(self.kernel.GetSystemFirmwareTable(0x52534D42, 0, buffer, size) == size, "SMBIOSを取得できません")
        length = struct.unpack_from('<I', buffer.raw, 4)[0]
        _require(length <= size - 8, "SMBIOSの長さが不正です")
        offset, host = 8, None
        while offset + 4 <= length + 8:
            kind, formatted = buffer.raw[offset:offset + 2]
            _require(formatted >= 4 and offset + formatted <= length + 8, "SMBIOS entryが不正です")
            if kind == 1:
                _require(formatted >= 24, "SMBIOS UUIDがありません")
                host = buffer.raw[offset + 8:offset + 24]
                break
            offset += formatted
            while offset + 1 < length + 8 and buffer.raw[offset:offset + 2] != b'\0\0':
                offset += 1
            _require(offset + 1 < length + 8, "SMBIOS終端がありません")
            offset += 2
        _require(host is not None and host not in (b'\0' * 16, b'\xff' * 16), "host本人が未設定です")
        boot = ctypes.create_string_buffer(64)
        _require(self.nt.NtQuerySystemInformation(90, boot, len(boot), None) == 0, "boot本人を取得できません")
        return hashlib.sha256(host).hexdigest(), boot.raw[:16].hex()

    def pin_process(self, expected):
        return _WinPeer(self)

    def pin_controller(self, expected):
        _require(expected['pid'] == os.getpid(), "P本人ではありません")
        return self.pin_process(expected)

    def authenticate(self, channel, peer, expected):
        pid = ctypes.c_uint32()
        self.check(self.kernel.GetNamedPipeClientProcessId(channel.handle, ctypes.byref(pid)))
        _require(pid.value == expected['pid'], "同pipeのpeer PIDが一致しません")
        peer.check(expected)

    def descriptor(self, path, raw):
        return _WinPin(self, path)

    def listen(self, endpoint, pin):
        _require(endpoint['transport'] == 'windows-pipe', "OSとendpointが一致しません")
        return _WinListener(self, endpoint['address'])

    def operation(self, channel, kind, value):
        return _WinOperation(self, channel, kind, value)
