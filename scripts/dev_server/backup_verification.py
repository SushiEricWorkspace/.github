"""停止確認済みの試験rootを、新しい保存先へ通常コピーして照合する。"""
import os
from pathlib import Path
import shutil

from .build_inputs import canonical, digest
from .local_ipc import make_private, plain_path
from .registry import require
from .runtime_bundle import _files
from . import storage


def validate_trial_paths(bundle: Path, outputs: list[Path]) -> None:
    """入力bundleと各書込先が重ならないことを、作成前に確認する。"""
    paths = [bundle, *outputs]
    for path in paths:
        require(path.is_absolute(), "INVALID_INPUT", "絶対パスを指定してください")
        plain_path(path)
    resolved = [path.resolve() for path in paths]
    for index, path in enumerate(resolved):
        for other in resolved[index + 1:]:
            require(not path.is_relative_to(other) and not other.is_relative_to(path),
                    "INVALID_INPUT", "入力bundleと書込先、または書込先同士が重なっています")


def copy_verified_tree(source: Path, destination: Path) -> dict:
    """通常ファイル・空directoryを新規コピーし、元とコピーの全体を再照合する。

    呼出側がsupervisor・JVM・writerの停止を確認する。停止の推測や既存保存先の
    上書きはしない。失敗したコピーは証跡として残し、自動削除しない。
    """
    for path in (source, destination):
        require(path.is_absolute(), "INVALID_INPUT", "絶対パスを指定してください")
        plain_path(path)
    require(source.is_dir() and destination.parent.is_dir(), "INVALID_INPUT", "通常directoryが必要です")
    require(not destination.resolve().is_relative_to(source.resolve())
            and not source.resolve().is_relative_to(destination.resolve()),
            "INVALID_INPUT", "コピー元と保存先を包含させないでください")
    require(not destination.exists(), "EXISTING_DATA", "既存の保存先を上書きしません")
    entries = _files(source)
    directories = sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_dir())
    size = sum(entry["size"] for entry in entries)
    require(shutil.disk_usage(destination.parent).free > size + 65536,
            "NO_SPACE", "バックアップ保存先の空き容量が不足しています")
    storage.mkdir(destination)
    for directory in directories:
        storage.mkdir(destination / directory)
    for entry in entries:
        original, copied = source / entry["path"], destination / entry["path"]
        require(original.stat().st_nlink == 1, "UNSUPPORTED_FILE", "共有hardlinkはコピーできません")
        require(shutil.disk_usage(destination).free > entry["size"] + 65536,
                "NO_SPACE", "コピー中に保存先の空き容量が不足しました")
        with original.open("rb") as reader, copied.open("xb") as writer:
            make_private(copied, executable=os.name != "nt" and os.access(original, os.X_OK))
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        storage.flush(copied)
    for directory in reversed(directories):
        storage.sync_directory(destination / directory)
    storage.sync_directory(destination)
    require(_files(source) == entries and _files(destination) == entries,
            "PROOF_MISMATCH", "コピー元と保存先のファイル集合・サイズ・hashが一致しません")
    require(sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_dir()) == directories
            and sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_dir()) == directories,
            "PROOF_MISMATCH", "コピー元と保存先のdirectory集合が一致しません")
    return {"files": len(entries), "directories": len(directories), "logicalBytes": size,
            "treeHash": digest(canonical({"files": entries, "directories": directories}))}
