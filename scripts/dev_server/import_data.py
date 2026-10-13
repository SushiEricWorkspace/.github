"""人間用runを変更せず、停止確認済みのデータを管理世代へ持ち込む。"""
from contextlib import contextmanager
import os
from pathlib import Path
import platform
import stat

from .build_inputs import regular_bytes
from .contracts import absolute_path, validate_relative_path
from .local_ipc import plain_path
from .registry import require, RegistryError
from .snapshot_data import collect, entry, properties, transform
from .template_data import shared_data


IMPORTED_TEMPLATES = {"initial-world-windows-v1": "Windows", "initial-world-macos-v1": "Darwin"}
IMPORT_CHECKS = ["IMPORT_SOURCE", "IMPORT_RESTORE", "IMPORT_SAVE"]


@contextmanager
def source_guard(source: str):
    """Minecraftのsession.lockを非ブロッキングで保持する。元ファイルは生成・変更しない。

    正常保存済み・外部writer終了の運用者確認は別途必要で、lockだけを正常停止証跡にしない。
    """
    absolute_path(source, "sourceDirectory")
    path = Path(source)
    require(path.is_absolute(), "INVALID_INPUT", "sourceDirectoryは対象OSの絶対パスが必要です")
    plain_path(path)
    world = properties(regular_bytes(path / "server.properties")).get("level-name", "")
    validate_relative_path(world, "level-name")
    lock = path / world / "session.lock"
    plain_path(lock)
    require(lock.is_file() and lock.stat().st_nlink == 1, "PROOF_REQUIRED", "既存worldのsession.lockが必要です")
    with lock.open("r+b") as handle:
        original = os.fstat(handle.fileno())
        require(stat.S_ISREG(original.st_mode) and original.st_size > 0, "UNSUPPORTED_FILE", "session.lockが不正です")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RegistryError("SOURCE_BUSY", "持込元worldは使用中です。正常停止してから取り込んでください") from None
        try:
            require(os.stat(lock).st_ino == original.st_ino, "SOURCE_CHANGED", "session.lockが置き換わりました")
            yield
            require(os.stat(lock).st_ino == original.st_ino, "SOURCE_CHANGED", "session.lockが置き換わりました")
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.lockf(handle, fcntl.LOCK_UN)


def inspect_source(source: str) -> dict:
    """lock保持中のrunを収集し、元hashと可搬データhashを返す。秘密の値は出力しない。"""
    path = Path(source)
    require(platform.system() in IMPORTED_TEMPLATES.values(), "INVALID_INPUT", "Windows/macOSのrunだけを持ち込めます")
    layout, files = collect(path, importing=True)
    portable = []
    for file in files:
        data = regular_bytes(path / file["path"])
        require(entry(file["path"], data) == file, "SOURCE_CHANGED", "持込元データが収集中に変更されました")
        portable.append(entry(file["path"], shared_data(file["path"], transform(file["path"], data))))
    require(collect(path, importing=True) == (layout, files), "SOURCE_CHANGED", "持込元データが収集中に変更されました")
    return {"schemaVersion": 1, "sourceDirectory": path.as_posix(), "platform": platform.system(),
            "layout": layout, "sourceFiles": files, "files": portable}
