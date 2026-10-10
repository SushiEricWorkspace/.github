"""ビルド開始時のGit入力を固定し、公開直前の変更を拒否する。"""

import base64
import hashlib
import json
import stat
import subprocess
from pathlib import Path

from .contracts import ContractError, validate_relative_path


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def regular_bytes(path: Path) -> bytes:
    """リンク・特殊ファイルを拒否し、読み取り中の差替えも拒否する。"""
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or
            getattr(before, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ContractError("input: 通常ファイル以外は使用できません")
    data = path.read_bytes()
    after = path.lstat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ContractError("input: 読み取り中に変更されました")
    return data


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def capture_source(root: Path, repo: str) -> dict:
    """tracked全入力・dirty patch・ignoreされていない未追跡入力を記録する。"""
    root = root.absolute()
    commit = git(root, "rev-parse", "HEAD").decode().strip()
    patch = git(root, "diff", "--binary", "HEAD", "--")
    tracked = set(git(root, "ls-files", "-z").decode("utf-8").split("\0")) - {""}
    untracked = set(git(root, "ls-files", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")) - {""}
    entries = []
    for name in sorted(tracked | untracked):
        validate_relative_path(name)
        path = root / name
        # trackedの削除はpatchと入力集合へ明示する。
        if not path.exists() and not path.is_symlink() and name in tracked:
            entries.append({"path": name, "deleted": True})
            continue
        for parent in path.parents:
            if parent == root:
                break
            info = parent.lstat()
            if parent.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ContractError("source: 入力の親ディレクトリがリンクです")
        data = regular_bytes(path)
        entries.append({"path": name, "kind": "regular", "size": len(data), "sha256": digest(data)})
    result = {"repo": repo, "commit": commit, "dirtyPatchSha256": digest(patch),
              "untrackedInputs": [entry for entry in entries if entry["path"] in untracked]}
    return {"root": str(root), "record": result, "inputs": entries,
            "patchBase64": base64.b64encode(patch).decode("ascii")}


def capture_sources(repos: dict[str, Path]) -> dict:
    if not {"Common", "SushiEricServerMod"} <= repos.keys():
        raise ContractError("source: CommonとModの入力指定が必要です")
    return {"sourceLockVersion": 1, "sources": [capture_source(path, repo) for repo, path in sorted(repos.items())]}


def verify_sources(lock: dict) -> None:
    """入力が現在と一致しないlockでは公開しない。再キャプチャして救済しない。"""
    if lock.get("sourceLockVersion") != 1 or not lock.get("sources"):
        raise ContractError("source: 未知または空の入力lockです")
    for source in lock["sources"]:
        if capture_source(Path(source["root"]), source["record"]["repo"]) != source:
            raise ContractError(f"source: {source['record']['repo']}がビルド開始時から変更されています")


def portable_lock(lock: dict) -> dict:
    """artifact側にはworktreeへの可変参照を保存しない。"""
    return {"sourceLockVersion": 1, "sources": [
        {key: value for key, value in source.items() if key != "root"} for source in lock["sources"]
    ]}
