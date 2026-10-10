"""Windowsの同一ユーザー限定DACL。既存の管理外ファイルの権限は変更しない。"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import os

if os.name == "nt":
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    def _function(dll, name, arguments, result):
        function = getattr(dll, name)
        function.argtypes, function.restype = arguments, result
        return function

    _function(kernel, "GetCurrentProcess", [], w.HANDLE)
    _function(kernel, "CloseHandle", [w.HANDLE], w.BOOL)
    _function(kernel, "LocalFree", [w.HLOCAL], w.HLOCAL)
    _function(kernel, "GetDriveTypeW", [w.LPCWSTR], w.UINT)
    _function(advapi, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL)
    _function(advapi, "GetTokenInformation", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL)
    _function(advapi, "ConvertSidToStringSidW", [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL)
    _function(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)], w.BOOL)
    _function(advapi, "GetNamedSecurityInfoW", [w.LPCWSTR, ctypes.c_int, w.DWORD, ctypes.POINTER(w.LPVOID), w.LPVOID, ctypes.POINTER(w.LPVOID), w.LPVOID, ctypes.POINTER(w.LPVOID)], w.DWORD)
    _function(advapi, "GetSecurityInfo", [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.POINTER(w.LPVOID), w.LPVOID, ctypes.POINTER(w.LPVOID), w.LPVOID, ctypes.POINTER(w.LPVOID)], w.DWORD)
    _function(advapi, "GetAce", [w.LPVOID, w.DWORD, ctypes.POINTER(w.LPVOID)], w.BOOL)
    _function(advapi, "SetFileSecurityW", [w.LPCWSTR, w.DWORD, w.LPVOID], w.BOOL)


def _check(success):
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())


def _sid_string(sid) -> str:
    text = w.LPWSTR()
    _check(advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)))
    try:
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, w.HLOCAL))


def current_sid() -> str:
    """実効プロセス所有者のSIDを返す。ユーザー名・環境変数から推測しない。"""
    token = w.HANDLE()
    _check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
    try:
        length = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        _check(advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)))
        return _sid_string(ctypes.cast(buffer, ctypes.POINTER(w.LPVOID))[0])
    finally:
        kernel.CloseHandle(token)


@contextmanager
def security_descriptor(*, directory: bool = False):
    """所有者SID一つだけへ全アクセスを許可する保護DACLを生成する。"""
    descriptor = w.LPVOID()
    flags = "OICI" if directory else ""
    sddl = f"O:{current_sid()}D:P(A;{flags};FA;;;{current_sid()})"
    _check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
    try:
        yield descriptor
    finally:
        kernel.LocalFree(descriptor)


def make_private(path, *, directory: bool = False) -> None:
    """新規管理ファイル・ディレクトリだけのDACLを所有者限定へ設定する。"""
    with security_descriptor(directory=directory) as descriptor:
        _check(advapi.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor))
    require_private(path=path)


def require_local_volume(path) -> None:
    """ドライブ名でもネットワークへ割り当てた管理rootは拒否する。"""
    if kernel.GetDriveTypeW(path.anchor) not in (2, 3):
        raise OSError("管理rootはローカルの固定・リムーバブル媒体に限定します")


def require_private(*, path=None, handle=None) -> None:
    """ownerと単一のallow ACEを検査し、継承による他ユーザー許可も拒否する。"""
    owner, acl, descriptor = w.LPVOID(), w.LPVOID(), w.LPVOID()
    if handle is not None:
        result = advapi.GetSecurityInfo(handle, 1, 1 | 4, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
    else:
        result = advapi.GetNamedSecurityInfoW(str(path), 1, 1 | 4, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        if not owner or _sid_string(owner) != current_sid() or not acl:
            raise PermissionError("管理資源の所有者またはDACLが一致しません")
        # ACL: BYTE revision/sbz1, WORD size/aceCount/sbz2
        count = ctypes.c_ushort.from_address(acl.value + 4).value
        if count != 1:
            raise PermissionError("管理資源へ所有者以外の許可が含まれています")
        ace = w.LPVOID()
        _check(advapi.GetAce(acl, 0, ctypes.byref(ace)))
        kind = ctypes.c_ubyte.from_address(ace.value).value
        mask = w.DWORD.from_address(ace.value + 4).value
        if kind != 0 or mask != 0x1F01FF or _sid_string(ace.value + 8) != current_sid():
            raise PermissionError("所有者限定の全アクセス許可ではありません")
    finally:
        kernel.LocalFree(descriptor)
