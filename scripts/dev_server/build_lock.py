"""タスク単位のビルドと担当変更を、同じOS lockで排他する。"""

from contextlib import contextmanager
import os
from pathlib import Path
import stat


class BuildLockError(RuntimeError):
    """ビルド中、またはlockパスが通常ファイルでない場合の拒否。"""


@contextmanager
def build_lock(path: Path):
    """同じtaskだけを直列化する。競合は待たず拒否し、他taskのDB操作を止めない。"""
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BuildLockError("build: lockの親が通常ディレクトリではありません")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BuildLockError("build: lockが通常ファイルではありません")
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise BuildLockError("build: このtaskはビルドまたは担当変更の操作中です") from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
