"""検査済みMaven UUIDの削除対象をOS参照で固定する。汎用GCや自動復旧は行わない。"""

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import secrets
import stat

from .runtime_bundle import _require


class _FileInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]


class _WindowsDelete:
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.api.CreateFileW.restype = wintypes.HANDLE
        self.api.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
        self.api.GetFileInformationByHandle.restype = wintypes.BOOL
        self.api.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self.api.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self.api.ReadFile.restype = wintypes.BOOL
        self.api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetFileInformationByHandle.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL

    @staticmethod
    def _native(path: Path) -> str:
        value = str(path)
        return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value

    @staticmethod
    def _normalized(value: str) -> str:
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return os.path.normcase(os.path.normpath(value))

    def check(self, handle, path: Path, directory: bool):
        info = _FileInfo()
        if not self.api.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        _require(not info.attributes & 0x400 and bool(info.attributes & 0x10) == directory,
                 f"maven: handleのリンク/種別が一致しません: {path}")
        name = ctypes.create_unicode_buffer(32768)
        count = self.api.GetFinalPathNameByHandleW(handle, name, len(name), 0)
        if not count:
            raise ctypes.WinError(ctypes.get_last_error())
        _require(count < len(name) and self._normalized(name.value) == self._normalized(str(path)),
                 f"maven: handleが初期固定パスと一致しません: {path}")

    def open(self, path: Path, *, directory: bool, deleting: bool):
        access = 0x80 if directory else 0x80000000
        if deleting:
            access |= 0x10000
        flags = 0x200000 | (0x2000000 if directory else 0)
        share = 3 if directory else 1
        handle = self.api.CreateFileW(self._native(path), access, share, None, 3, flags, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.check(handle, path, directory)
            return handle
        except BaseException:
            self.close(handle)
            raise

    def hash_file(self, handle) -> str:
        digest = hashlib.sha256()
        buffer = ctypes.create_string_buffer(1024 * 1024)
        count = wintypes.DWORD()
        while True:
            if not self.api.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if count.value == 0:
                return digest.hexdigest()
            digest.update(buffer.raw[:count.value])

    def dispose(self, handle, path: Path, *, directory: bool):
        self.check(handle, path, directory)
        delete = ctypes.c_ubyte(1)
        if not self.api.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self, handle):
        if not self.api.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _windows(root: Path, inventory: dict):
    api = _WindowsDelete()
    handles = {}
    parents = [*reversed(root.parents), root]
    directories = [root / name for name in sorted(inventory["directories"], key=lambda value: value.count("/"))]
    try:
        for path in parents + directories:
            handles[path] = api.open(path, directory=True, deleting=path == root or root in path.parents)
        for item in inventory["files"]:
            path = root / item["path"]
            handle = api.open(path, directory=False, deleting=True)
            try:
                _require(api.hash_file(handle) == item["sha256"], f"maven: 削除handleのhashが変わりました: {path}")
                for directory, directory_handle in handles.items():
                    api.check(directory_handle, directory, True)
                api.dispose(handle, path, directory=False)
            finally:
                api.close(handle)
        for path in reversed(directories):
            handle = handles[path]
            api.dispose(handle, path, directory=True)
            api.close(handle)
            del handles[path]
        api.dispose(handles[root], root, directory=True)
        api.close(handles[root])
        del handles[root]
    finally:
        for handle in reversed(list(handles.values())):
            api.close(handle)


def _hash_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while data := os.read(fd, 1024 * 1024):
        digest.update(data)
    return digest.hexdigest()


def _posix(root: Path, inventory: dict):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directories = [root / name for name in sorted(inventory["directories"], key=lambda value: value.count("/"))]
    handles = {}
    quarantine = ".maven-collect-" + secrets.token_hex(16)
    quarantine_fd = None
    try:
        for path in [*reversed(root.parents), root, *directories]:
            handles[path] = os.open(str(path), flags) if path == Path(path.anchor) else os.open(
                path.name, flags, dir_fd=handles[path.parent])
        os.mkdir(quarantine, mode=0o700, dir_fd=handles[root])
        quarantine_fd = os.open(quarantine, flags, dir_fd=handles[root])
        os.fchmod(quarantine_fd, 0o700)
        for index, item in enumerate(inventory["files"]):
            path = root / item["path"]
            parent = handles[path.parent]
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                opened = os.fstat(fd)
                _require(stat.S_ISREG(opened.st_mode) and _hash_fd(fd) == item["sha256"],
                         f"maven: 通常file/hashが一致しません: {path}")
                name = f"{index}-{path.name}"
                os.rename(path.name, name, src_dir_fd=parent, dst_dir_fd=quarantine_fd)
                moved = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
                _require(stat.S_ISREG(moved.st_mode) and (opened.st_dev, opened.st_ino) == (moved.st_dev, moved.st_ino)
                         and _hash_fd(fd) == item["sha256"],
                         f"maven: 差替えfileを{quarantine}/{name}へ保全しました")
                os.unlink(name, dir_fd=quarantine_fd)
            finally:
                os.close(fd)
        os.close(quarantine_fd)
        quarantine_fd = None
        os.rmdir(quarantine, dir_fd=handles[root])
        for path in [*reversed(directories), root]:
            current = os.stat(path.name, dir_fd=handles[path.parent], follow_symlinks=False)
            opened = os.fstat(handles[path])
            _require(stat.S_ISDIR(current.st_mode) and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino),
                     f"maven: 削除dirが差し替わりました: {path}")
            os.rmdir(path.name, dir_fd=handles[path.parent])
    finally:
        if quarantine_fd is not None:
            os.close(quarantine_fd)
        for handle in reversed(list(handles.values())):
            os.close(handle)


def delete_maven(root: Path, inventory: dict):
    """直前検査済みinventoryだけを削除する。失敗時のquarantineはCOLLECTINGとともに保全する。

    期待rootは入力の絶対パスから最初に字句正規化し、差替え後のresolveで追従しない。
    呼出元はbuild lock・owner/state・bundle/記録の照合を保持する。
    """
    fixed = Path(os.path.normpath(str(root.absolute())))
    if os.name == "nt":
        _windows(fixed, inventory)
    else:
        _posix(fixed, inventory)
