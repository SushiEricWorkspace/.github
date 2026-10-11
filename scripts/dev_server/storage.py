"""同一FSの公開・参照切替を耐久化する。障害時の途中ファイルは削除しない。"""
import ctypes
import os
from pathlib import Path
import sys
import uuid

from .local_ipc import make_private, plain_path, require_private
from .registry import require


def sync_directory(path: Path) -> None:
    plain_path(path)
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def flush(path: Path) -> None:
    """既存の通常ファイルを同期する。リンク・hardlinkは許可しない。"""
    plain_path(path)
    require(path.is_file() and path.stat().st_nlink == 1, "UNSUPPORTED_FILE", "通常の独立ファイルが必要です")
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
        if sys.platform == "darwin":
            import fcntl
            fcntl.fcntl(handle.fileno(), fcntl.F_FULLFSYNC)


def move(source: Path, destination: Path, *, replace=False) -> None:
    """既存destinationの置換はprivateな参照ファイルにだけ許可する。"""
    plain_path(source)
    plain_path(destination)
    require(source.stat().st_dev == destination.parent.stat().st_dev, "DIFFERENT_FS", "同一FSでの公開が必要です")
    if replace:
        require(source.is_file() and destination.is_file() and source.stat().st_nlink == destination.stat().st_nlink == 1,
                "UNSUPPORTED_FILE", "独立した通常参照ファイルだけを置換できます")
        require_private(source)
        require_private(destination)
    else:
        require(not destination.exists(), "EXISTING_DATA", "公開先を上書きしません")
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        kernel.MoveFileExW.restype = wintypes.BOOL
        if not kernel.MoveFileExW(str(source), str(destination), 8 | int(replace)):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        if replace:
            os.replace(source, destination)
        else:
            source.rename(destination)
        sync_directory(destination.parent)
        if source.parent != destination.parent:
            sync_directory(source.parent)


def write(path: Path, data: bytes, *, resume=False) -> None:
    """新規公開。resumeは一致する完成記録だけ再使用し、途中の異なるbytesは残す。"""
    plain_path(path)
    if resume and path.exists():
        require_private(path)
        require(path.is_file() and path.stat().st_nlink == 1 and path.read_bytes() == data,
                "JOURNAL_MISMATCH", "公開済み記録が一致しません")
        return
    temporary = path.with_name(path.name + ".writing")
    plain_path(temporary)
    if resume and temporary.exists():
        require_private(temporary)
        require(temporary.is_file() and temporary.stat().st_nlink == 1, "UNSUPPORTED_FILE", "記録の独立ファイルが必要です")
        if temporary.read_bytes() == data:
            flush(temporary)
            move(temporary, path)
            return
        temporary = path.with_name(path.name + ".retry-" + uuid.uuid4().hex + ".writing")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        make_private(temporary)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        if sys.platform == "darwin":
            import fcntl
            fcntl.fcntl(handle.fileno(), fcntl.F_FULLFSYNC)
    move(temporary, path)


def mkdir(path: Path) -> None:
    plain_path(path)
    path.mkdir()
    make_private(path, directory=True)
    sync_directory(path.parent)
