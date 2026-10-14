"""task専用Mavenのビルド証跡と、成功出力だけの明示回収を管理する。"""

import json
import os
from pathlib import Path
import re

from .build_inputs import canonical, digest, portable_lock, regular_bytes
from .contracts import ContractError, validate_relative_path
from .runtime_bundle import _directory, _files, _require, verify_bundle
from .task_worktrees import TaskError, TaskWorktrees
from .task_maven_delete import delete_maven


def write_receipt(source_lock: Path, bundle: Path) -> dict:
    """freezeが検証済みのbundleを受け取り、lock隣の.receipt.jsonへ保存する。上書きはしない。"""
    manifest_bytes = regular_bytes(bundle / "runtime.json")
    manifest = json.loads(manifest_bytes)
    receipt = {"receiptVersion": 1, "sourceLockSha256": digest(regular_bytes(source_lock)),
               "bundle": str(bundle.resolve()), "artifactId": manifest["artifactId"],
               "manifestSha256": digest(manifest_bytes)}
    path = source_lock.with_suffix(".receipt.json")
    _directory(path.parent)
    with path.open("xb") as output:
        output.write(canonical(receipt))
    return receipt


def maven_inventory(root: Path) -> dict:
    """通常fileと空dirを含む全ディレクトリを記録し、リンク・特殊ファイルを拒否する。"""
    _directory(root)
    _require(root.is_dir(), "maven: UUID出力ディレクトリがありません")
    files = _files(root)
    directories = []
    def fail(error):
        raise error
    for directory, dirs, _ in os.walk(root, followlinks=False, onerror=fail):
        _directory(Path(directory))
        for name in dirs:
            path = Path(directory) / name
            _directory(path)
            relative = path.relative_to(root).as_posix()
            validate_relative_path(relative)
            directories.append(relative)
    return {"files": files, "directories": sorted(directories)}


class TaskMaven:
    """記録はtask外へ残す。回収はREMOVED/current ownerの選択UUIDだけを対象にする。

    start/succeed/failは呼出元でACTIVE・登録ownerを照合し、taskのbuild lockを保持する。
    dry_run/collectは内部でcurrent ownerを照合してbuild lockを取得する。
    """

    def __init__(self, workspace: Path):
        _directory(workspace.absolute())
        self.registry = TaskWorktrees(workspace.resolve())
        self.records = self.registry.root / ".build-records"
        _directory(self.records)

    def paths(self, task_id: str, build_id: str) -> tuple[Path, Path]:
        root = self.registry.target(task_id, "SushiEricServerMod").parent
        _require(bool(re.fullmatch(r"[0-9a-f]{32}", build_id)), "maven: 完全なUUID hexを指定してください")
        maven = root / "maven" / build_id
        record = self.records / task_id / f"{build_id}.json"
        _directory(maven)
        _directory(record.parent)
        return maven, record

    def _write(self, record: dict, previous: dict | None = None):
        _, path = self.paths(record["taskId"], record["buildId"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if previous is None:
            with path.open("xb") as output:
                output.write(canonical(record))
            return
        _require(regular_bytes(path) == canonical(previous), "maven: ビルド記録が変わりました")
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("xb") as output:
            output.write(canonical(record))
            output.flush()
            os.fsync(output.fileno())
        _require(regular_bytes(path) == canonical(previous), "maven: ビルド記録が変わりました")
        temporary.replace(path)

    def start(self, task_id: str, build_id: str, owner: str, source_lock: Path) -> dict:
        """Maven作成前にSTARTEDを排他的保存し、UUIDの再使用を拒否する。"""
        maven, _ = self.paths(task_id, build_id)
        _require(not maven.exists(), "maven: UUID出力は既に存在します")
        record = {"recordVersion": 1, "taskId": task_id, "buildId": build_id,
                  "owner": owner, "state": "STARTED", "maven": str(maven),
                  "sourceLock": str(source_lock.absolute()), "bundle": None, "artifactId": None,
                  "manifestSha256": None, "publications": [], "inventory": None}
        self._write(record)
        return record

    def succeed(self, record: dict, source_lock: dict) -> dict:
        """receipt・完成bundle・発行コピーを照合して成功を記録する。入力再照合は呼出元で行う。"""
        lock = Path(record["sourceLock"])
        receipt = json.loads(regular_bytes(lock.with_suffix(".receipt.json")))
        _require(receipt["receiptVersion"] == 1
                 and receipt["sourceLockSha256"] == digest(canonical(source_lock))
                 and digest(regular_bytes(lock)) == receipt["sourceLockSha256"],
                 "maven: receiptとビルド入力lockが一致しません")
        maven, _ = self.paths(record["taskId"], record["buildId"])
        publications = []
        for item in source_lock["publications"]:
            path = Path(item["path"]).absolute()
            _directory(path.parent)
            path = path.resolve()
            _require(maven in path.parents, "maven: 発行ファイルがUUIDの外です")
            publications.append({"coordinate": item["coordinate"], "path": path.relative_to(maven).as_posix(),
                                 "sha256": item["sha256"]})
        completed = dict(record, state="SUCCEEDED", bundle=receipt["bundle"], artifactId=receipt["artifactId"],
                         manifestSha256=receipt["manifestSha256"], publications=publications,
                         inventory=maven_inventory(maven))
        self._verify_output(completed, maven)
        _require(json.loads(regular_bytes(Path(receipt["bundle"]) / "inputs/source-lock.json"))
                 == portable_lock(source_lock), "maven: bundleとビルド入力lockが一致しません")
        self._write(completed, record)
        return completed

    def fail(self, record: dict, error: BaseException):
        """失敗を記録する。未確定のbundleを成功へ推定せず、出力は保全する。"""
        failed = dict(record, state="FAILED", error=str(error))
        receipt_path = Path(record["sourceLock"]).with_suffix(".receipt.json")
        if receipt_path.exists():
            try:
                receipt = json.loads(regular_bytes(receipt_path))
                failed.update({key: receipt[key] for key in ("bundle", "artifactId", "manifestSha256")})
            except (OSError, ValueError, KeyError, TypeError) as receipt_error:
                failed["receiptError"] = str(receipt_error)
        self._write(failed, record)

    def _verify_output(self, record: dict, maven: Path):
        bundle = Path(record["bundle"])
        _require(bundle.is_absolute(), "maven: bundleは絶対パスが必要です")
        _directory(bundle)
        bundle = bundle.resolve()
        maven = maven.resolve()
        _require(bundle != maven and maven not in bundle.parents
                 and bundle not in maven.parents, "maven: bundleと回収対象の包含は許可しません")
        manifest = verify_bundle(bundle)
        _require(bundle.name == record["artifactId"] == manifest["artifactId"]
                 and digest(regular_bytes(bundle / "runtime.json")) == record["manifestSha256"],
                 "maven: bundleのID/manifest hashが一致しません")
        inventory = maven_inventory(maven)
        _require(inventory == record["inventory"], "maven: 未知ファイル/dirまたは出力hashの変更があります")
        expected = []
        for item in record["publications"]:
            validate_relative_path(item["path"])
            path = maven / item["path"]
            _require(path.suffix in (".jar", ".pom") and digest(regular_bytes(path)) == item["sha256"],
                     "maven: 発行POM/JARが一致しません")
            expected.append((item["coordinate"], path.name, item["sha256"]))
        observed = []
        for item in json.loads(regular_bytes(bundle / "inputs/publications.json")):
            validate_relative_path(item["file"])
            path = bundle / item["file"]
            _require(digest(regular_bytes(path)) == item["sha256"], "maven: bundleの発行コピーが一致しません")
            observed.append((item["coordinate"], path.name, item["sha256"]))
        _require(bool(expected) and sorted(expected) == sorted(observed),
                 "maven: 発行記録とbundleのコピーが一致しません")

    def _candidate(self, task_id: str, build_id: str, snapshot) -> tuple[dict, str]:
        _require(snapshot[0]["state"] == "REMOVED", "maven: REMOVED以外のtaskは保持します")
        maven, path = self.paths(task_id, build_id)
        record_bytes = regular_bytes(path)
        record = json.loads(record_bytes)
        _require(record["recordVersion"] == 1 and record["taskId"] == task_id
                 and record["buildId"] == build_id and record["maven"] == str(maven),
                 "maven: UUID/taskの記録が一致しません")
        _require(record["state"] == "SUCCEEDED", f"maven: {record['state']}の出力は保持します")
        self._verify_output(record, maven)
        fingerprint = digest(canonical({"task": snapshot, "record": record, "recordHash": digest(record_bytes)}))
        return record, fingerprint

    def dry_run(self, task_id: str, owner: str) -> list[dict]:
        """UUIDごとの候補hashまたは保持理由を返す。回収や過去記録の推定は行わない。"""
        _require((self.registry.root / "registry.sqlite").is_file(), "maven: task registryがありません")
        with self.registry.build_guard(task_id):
            snapshot = self.registry._read_snapshot(task_id, owner,
                ("ACTIVE", "CREATING", "REMOVING", "RECOVERY_REQUIRED", "REMOVED"))
            root = self.registry.target(task_id, "SushiEricServerMod").parent / "maven"
            records = self.records / task_id
            _directory(root)
            _directory(records)
            names = {p.name for p in root.iterdir()} if root.exists() else set()
            if records.exists():
                names |= {p.name.removesuffix(".json") for p in records.iterdir()}
            result = []
            for build_id in sorted(names):
                row = {"taskId": task_id, "owner": owner, "buildId": build_id, "eligible": False}
                try:
                    _, fingerprint = self._candidate(task_id, build_id, snapshot)
                    row.update(eligible=True, candidateHash=fingerprint)
                except (ContractError, TaskError, OSError, ValueError, KeyError, TypeError) as error:
                    row["reason"] = str(error)
                result.append(row)
            with self.registry.database() as db:
                db.execute("BEGIN")
                self.registry._recheck(db, task_id, snapshot)
            return result

    def collect(self, task_id: str, owner: str, build_id: str, candidate_hash: str) -> dict:
        """dry-runのhashと全証跡を再照合し、選択したUUIDの通常file/空dirだけを削除する。"""
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", candidate_hash)), "maven: 候補hashを指定してください")
        _require((self.registry.root / "registry.sqlite").is_file(), "maven: task registryがありません")
        with self.registry.build_guard(task_id):
            snapshot = self.registry._read_snapshot(task_id, owner, ("REMOVED",))
            record, observed = self._candidate(task_id, build_id, snapshot)
            _require(observed == candidate_hash, "maven: dry-run後に候補hashが変わりました")
            with self.registry.database() as db:
                db.execute("BEGIN")
                self.registry._recheck(db, task_id, snapshot)
            collecting = dict(record, state="COLLECTING")
            self._write(collecting, record)
            maven, _ = self.paths(task_id, build_id)
            self._delete(maven, record, snapshot)
            collected = dict(collecting, state="COLLECTED")
            self._write(collected, collecting)
            return {"taskId": task_id, "buildId": build_id, "state": "COLLECTED"}

    def _delete(self, maven: Path, record: dict, snapshot):
        with self.registry.database() as db:
            db.execute("BEGIN")
            self.registry._recheck(db, record["taskId"], snapshot)
        _, path = self.paths(record["taskId"], record["buildId"])
        _require(regular_bytes(path) == canonical(dict(record, state="COLLECTING")),
                 "maven: 削除直前にビルド記録が変わりました")
        # 記録更新後の差替えも拒否し、再帰削除で未知ファイルを巻き込まない。
        self._verify_output(record, maven)
        delete_maven(maven, record["inventory"])
