"""macOS Unix socket／Windows named pipeのJSON限定ローカルIPC。"""
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time

MAX_MESSAGE = 65536


def supported_os() -> None:
    """未検証のOSへ運用を暗黙拡張しない。"""
    if sys.platform not in ("darwin", "win32"):
        raise OSError("初期対象はmacOSとWindowsだけです")
    if sys.version_info[:2] != (3, 12):
        raise OSError("OS adapterはPython 3.12専用です")


def plain_path(path: Path) -> None:
    """親を含むリンク・reparse point・特殊ファイルを拒否する。"""
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & 0x400 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise OSError("管理パスは通常ファイル・ディレクトリに限定します")


def make_private(path: Path, *, directory: bool = False) -> None:
    if os.name == "nt":
        from .windows_security import make_private as protect
        protect(path, directory=directory)
    else:
        path.chmod(0o700 if directory else 0o600)
        require_private(path, directory=directory)


def require_private(path: Path, *, directory: bool = False) -> None:
    plain_path(path)
    if os.name == "nt":
        from .windows_security import require_private as check
        check(path=path)
    else:
        info = path.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
            raise PermissionError("管理資源は所有者限定の権限にしてください")


def endpoint(root: Path) -> str:
    if os.name == "nt":
        from .windows_security import current_sid
        identity = current_sid() + "\0" + str(root).casefold()
        return "\\\\.\\pipe\\sushieric-dev-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    result = str(root / "manager" / "supervisor.sock")
    if len(os.fsencode(result)) >= 104:
        raise OSError("macOSのUnix socket長上限を超えます。短い管理rootを指定してください")
    return result


def encode(document: dict) -> bytes:
    payload = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > MAX_MESSAGE:
        raise ValueError("JSONメッセージが大きすぎます")
    return payload


def decode(payload: bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("JSONの項目が重複しています")
            result[key] = value
        return result
    value = json.loads(payload, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限数です")))
    if type(value) is not dict:
        raise ValueError("JSON objectが必要です")
    return value


if os.name == "nt":
    import _winapi
    from ctypes import wintypes as w
    from multiprocessing.connection import PipeConnection
    from .windows_security import kernel, security_descriptor, require_private as check_handle

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]

    kernel.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(_SecurityAttributes)]
    kernel.CreateNamedPipeW.restype = w.HANDLE

    class WindowsListener:
        """overlapped I/OはPython 3.12のWin32 adapterへ委譲し、DACLは明示する。"""
        def __init__(self, address):
            self.address = address
            self.handle = self._create(first=True)

        def _create(self, *, first=False):
            with security_descriptor() as descriptor:
                attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
                handle = kernel.CreateNamedPipeW(self.address, 3 | 0x40000000 | (0x80000 if first else 0),
                                                4 | 2 | 8, _winapi.PIPE_UNLIMITED_INSTANCES, MAX_MESSAGE, MAX_MESSAGE, 5000, ctypes.byref(attributes))
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                check_handle(handle=handle)
            except BaseException:
                kernel.CloseHandle(handle)
                raise
            return handle

        def accept(self):
            old, self.handle = self.handle, self._create()
            try:
                try:
                    overlapped = _winapi.ConnectNamedPipe(old, overlapped=True)
                except OSError as error:
                    if error.winerror == _winapi.ERROR_NO_DATA:
                        kernel.CloseHandle(old)
                        return None
                    raise
                if overlapped is not None:
                    _winapi.WaitForMultipleObjects([overlapped.event], False, _winapi.INFINITE)
                    _, error = overlapped.GetOverlappedResult(True)
                    if error:
                        raise ctypes.WinError(error)
                return PipeConnection(old)
            except BaseException:
                kernel.CloseHandle(old)
                raise

        def close(self):
            kernel.CloseHandle(self.handle)

    def _connect(address, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle = _winapi.CreateFile(address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, 0,
                                            _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, 0)
                break
            except OSError as error:
                if error.winerror not in (2, 231) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        try:
            check_handle(handle=handle)
            _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
            return PipeConnection(handle)
        except BaseException:
            kernel.CloseHandle(handle)
            raise


class UnixConnection:
    """長さ付きJSON bytesだけを扱い、pickleを復元しない。"""
    def __init__(self, stream):
        self.stream = stream
        self.stream.settimeout(5)

    def send_bytes(self, value):
        self.stream.sendall(len(value).to_bytes(4, "big") + value)

    def recv_bytes(self, maximum=MAX_MESSAGE):
        def read(count):
            result = bytearray()
            while len(result) < count:
                part = self.stream.recv(count - len(result))
                if not part:
                    raise EOFError("接続が終了しました")
                result.extend(part)
            return result
        length = int.from_bytes(read(4), "big")
        if length > maximum:
            raise ValueError("JSONメッセージが大きすぎます")
        return bytes(read(length))

    def poll(self, timeout):
        import select
        return bool(select.select([self.stream], [], [], timeout)[0])

    def close(self):
        self.stream.close()


def _same_uid(stream):
    libc = ctypes.CDLL(None, use_errno=True)
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    libc.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    libc.getpeereid.restype = ctypes.c_int
    if libc.getpeereid(stream.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0 or uid.value != os.getuid():
        raise PermissionError("IPC接続元の所有者が一致しません")


class UnixListener:
    """所有者限定dir/socketとpeer UIDの両方を検査する。"""
    def __init__(self, address):
        self.address = Path(address)
        self.stream = socket.socket(socket.AF_UNIX)
        try:
            if self.address.exists():
                info = self.address.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise PermissionError("IPCパスに未知の実体があります")
                self.address.unlink()  # singleton OS lockを保持する呼び出し元だけが再作成する。
            self.stream.bind(address)
            self.address.chmod(0o600)
            self.stream.listen(8)
        except BaseException:
            self.stream.close()
            raise

    def accept(self):
        stream, _ = self.stream.accept()
        try:
            _same_uid(stream)
            return UnixConnection(stream)
        except BaseException:
            stream.close()
            raise

    def close(self):
        self.stream.close()
        self.address.unlink(missing_ok=True)


@contextmanager
def listener(root: Path):
    supported_os()
    require_private(root / "manager", directory=True)
    server = WindowsListener(endpoint(root)) if os.name == "nt" else UnixListener(endpoint(root))
    try:
        yield server
    finally:
        server.close()


def request(root: Path, document: dict, *, timeout: float = 5) -> dict:
    """既存supervisorへ一要求だけ送る。停止・障害時に自動起動や再送をしない。"""
    supported_os()
    require_private(root / "manager", directory=True)
    if os.name == "nt":
        connection = _connect(endpoint(root), timeout)
    else:
        stream = socket.socket(socket.AF_UNIX)
        stream.settimeout(timeout)
        try:
            stream.connect(endpoint(root))
            _same_uid(stream)
            connection = UnixConnection(stream)
        except BaseException:
            stream.close()
            raise
    try:
        connection.send_bytes(encode(document))
        if not connection.poll(timeout):
            raise TimeoutError("supervisorが応答しません。結果不明の操作を別IDで再送しないでください")
        return decode(connection.recv_bytes(MAX_MESSAGE))
    finally:
        connection.close()
