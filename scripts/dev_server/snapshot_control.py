"""正常停止と全writer終了を固定してから、不変のprivate snapshotを公開する。"""
import json
from pathlib import Path

from .build_inputs import canonical, digest, regular_bytes
from .contracts import validate
from .local_ipc import plain_path, require_private
from .registry import RegistryError, fields, identifier, require
from .runtime_bundle import verify_bundle, _files
from .runtime_control import TOKEN
from .snapshot_data import collect, entry, transform
from .storage import mkdir, move, write
from .template_data import shared_data


def verify_snapshot(path: Path) -> dict:
    """完成marker・manifest・全ファイル集合を照合する。登録正本の照合は呼出側で行う。"""
    require_private(path, directory=True)
    require({item.name for item in path.iterdir()} == {"data", "manifest.json", "COMPLETE"},
            "SNAPSHOT_MISMATCH", "snapshotのファイル集合が一致しません")
    document = regular_bytes(path / "manifest.json")
    manifest = json.loads(document)
    validate("manifest", manifest)
    require(regular_bytes(path / "COMPLETE").decode() == digest(document),
            "SNAPSHOT_MISMATCH", "snapshotの完成hashが一致しません")
    files = _files(path / "data")
    require(files == manifest["files"], "SNAPSHOT_MISMATCH", "snapshotの欠損・余分・改変を検出しました")
    for item in files:
        file = path / "data" / item["path"]
        require(file.stat().st_nlink == 1, "UNSUPPORTED_FILE", "snapshotのhardlink共有は許可しません")
        require_private(file)
    return manifest


class SnapshotControl:
    """コピーをDB transaction外・instance/deployment lock内で実行する。"""
    def __init__(self, registry):
        self.registry = registry

    def _clean(self, payload, *, copying=False):
        with self.registry.transaction(read_only=True) as connection:
            row = dict(self.registry._instance(connection, payload["instanceId"]))
            self.registry._token(row, payload)
            summary = self.registry.writer.summary(connection, row)
            require(row["state"] == ("SNAPSHOTTING" if copying else "LEASED_STOPPED")
                    and row["generation_bound"] and row["runtime_proof"]
                    and not any(row[key] for key in ("writers", "children"))
                    and bool(row["pending"]) == copying and summary["externalWritersVerified"],
                    "PROOF_REQUIRED", "正常停止・writer終了・未完了操作不在が必要です")
            require(not connection.execute("SELECT 1 FROM writer_sessions WHERE instance=? AND pending!=0", (row["id"],)).fetchone()
                    and not connection.execute("SELECT 1 FROM endpoint_reservations WHERE instance=?", (row["id"],)).fetchone(),
                    "PROOF_REQUIRED", "writer操作または待受予約が残っています")
        generation = self.registry.runtime._active_generation(row)
        run, saved = self.stopped(row, generation)
        return row, generation, run, saved

    def stopped(self, row, generation):
        """停止済みrunの正本と保存証跡を照合する。lease・状態・writerは呼出側で検査する。"""
        runtime = self.registry.runtime
        run = runtime._load_run(row)
        proof = json.loads(row["runtime_proof"])
        expected_proof = runtime._paths(row["id"]) / "diagnostics" / row["run_id"] / "proof.json"
        plain_path(expected_proof)
        require(run.get("stopRequested") is True and run.get("stopAcknowledged") is True
                and run.get("jvmAndChildrenExited") is True and run.get("runtimeSaveCompleted") is True
                and run.get("exitCode") == 0 and run["runId"] == proof.get("runId")
                and run["nonce"] == proof.get("nonce") and run["generationId"] == row["generation"]
                and run["artifactId"] == row["artifact"] and run["instanceId"] == row["id"]
                and Path(run["proofPath"]) == expected_proof
                and proof.get("proofHash") == run.get("proofHash")
                and digest(regular_bytes(Path(run["proofPath"]))) == run.get("proofHash"),
                "PROOF_MISMATCH", "正常停止証跡の識別・hashが一致しません")
        require(runtime.process_adapter.identity(run["identity"]["pid"]) is None,
                "PROCESS_MISMATCH", "記録PIDが存在します。再利用を推測しません")
        saved = runtime._proof(run, generation, stopped=True)
        return run, saved

    def _registered(self, snapshot_id):
        with self.registry.transaction(read_only=True) as connection:
            row = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            require(row is not None, "NOT_FOUND", "完成snapshotが登録されていません")
        manifest = verify_snapshot(self.registry.root / "snapshots" / snapshot_id)
        require(manifest["snapshotId"] == snapshot_id and manifest["artifactId"] == row["artifact"]
                and digest(canonical(manifest)) == row["manifest_hash"],
                "SNAPSHOT_MISMATCH", "snapshotとRegistryが一致しません")
        return manifest

    def execute(self, payload, *, operation_id, dry_run=False):
        identifier(operation_id, "operationId")
        if type(payload) is dict and payload.get("action") in {"validate", "promote"}:
            from .template_control import TemplateControl
            return TemplateControl(self.registry).execute(payload, operation_id=operation_id, dry_run=dry_run)
        require(type(payload) is dict and payload.get("action") in {"create", "candidate", "verify"},
                "INVALID_INPUT", "snapshotはcreate・candidate・verify・validate・promoteを指定してください")
        candidate = payload["action"] == "candidate"
        create = payload["action"] != "verify"
        fields(payload, {"action", "snapshotId"} | (TOKEN if create else set()) | ({"sourceSnapshotId"} if candidate else set()))
        snapshot_id = identifier(payload["snapshotId"], "snapshotId")
        if not create:
            manifest = self._registered(snapshot_id)
            return {"snapshotId": snapshot_id, "artifactId": manifest["artifactId"], "verified": True, "dryRun": dry_run}
        fingerprint = digest(canonical(payload))
        with self.registry.transaction(read_only=True) as connection:
            for table in ("operations", "runtime_operations", "writer_operations", "reference_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
            cached = connection.execute("SELECT * FROM snapshot_operations WHERE id=?", (operation_id,)).fetchone()
            if cached:
                require(cached["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "operationIdに異なる要求を指定できません")
                require(cached["status"] == "COMPLETED", "RECOVERY_REQUIRED", "snapshotの未完了journalを保全してください")
        row, generation, run, saved = self._clean(payload)
        source_manifest = None
        source_root = generation
        if candidate:
            source_id = identifier(payload["sourceSnapshotId"], "sourceSnapshotId")
            source_manifest = self._registered(source_id)
            require(source_manifest["visibility"] == "private"
                    and source_manifest["cleanStop"]["instanceId"] == row["id"]
                    and source_manifest["cleanStop"]["runId"] == row["run_id"]
                    and source_manifest["generation"] == row["generation"]
                    and source_manifest["artifactId"] == row["artifact"],
                    "PROOF_MISMATCH", "同じ枠・世代・正常停止runのprivate snapshotが必要です")
            source_root = self.registry.root / "snapshots" / source_id / "data"
        if cached:
            manifest = self._registered(snapshot_id)
            require(manifest["cleanStop"]["runId"] == row["run_id"], "STALE_OPERATION", "別runへ古い結果を適用できません")
            return dict(json.loads(cached["response"]), replayed=True, dryRun=dry_run)
        destination = self.registry.root / "snapshots" / snapshot_id
        staging = self.registry.root / "snapshots" / (".staging-" + operation_id)
        journal = self.registry.root / "manager/operations" / ("snapshot-" + operation_id)
        for path in (destination, staging, journal):
            plain_path(path)
            require(not path.exists(), "EXISTING_DATA", "snapshot・作業領域・journalを上書きしません")
        layout, sources = (source_manifest["layout"], source_manifest["files"]) if candidate else collect(generation)
        artifact = verify_bundle(generation / "runtime")
        require(artifact["artifactId"] == row["artifact"]
                and digest(regular_bytes(generation / "runtime/runtime.json")) == run["manifestHash"],
                "ARTIFACT_MISMATCH", "停止したruntimeが変更されています")
        require(verify_bundle(self.registry.root / "artifacts" / row["artifact"]) == artifact,
                "ARTIFACT_MISMATCH", "保存するartifact参照先が一致しません")
        self.registry.runtime._space(sum(item["size"] for item in sources) + 65536)
        if dry_run:
            return {"dryRun": True, "wouldExecute": "snapshot-create", "snapshotId": snapshot_id, "files": len(sources)}
        requested = {"operationId": operation_id, "fingerprint": fingerprint, "instanceId": row["id"],
                     "generation": row["generation"], "runId": row["run_id"], "snapshotId": snapshot_id,
                     "leaseId": row["lease"], "epoch": row["epoch"]}
        if candidate:
            requested.update(action="candidate", sourceSnapshotId=source_id, sourceHash=digest(canonical(source_manifest)))
        with self.registry.transaction() as connection:
            current = self.registry._instance(connection, row["id"])
            self.registry._token(current, payload)
            require(current["state"] == "LEASED_STOPPED" and not current["pending"] and not current["writers"],
                    "BUSY", "枠の状態が変更されました")
            for table in ("operations", "runtime_operations", "writer_operations", "reference_operations", "snapshot_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが登録中に使用されました")
            connection.execute("INSERT INTO snapshot_operations VALUES (?,?,?,?,?)", (operation_id, fingerprint, row["id"], "PENDING", json.dumps(requested)))
            connection.execute("UPDATE instances SET state='SNAPSHOTTING',pending=1,clean_proof=NULL WHERE id=?", (row["id"],))
            references = {"artifact:" + row["artifact"], "generation:" + row["generation"], "snapshot:" + snapshot_id}
            if candidate:
                references.add("snapshot:" + source_id)
            for reference in sorted(references):
                connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)", (operation_id, reference, row["id"], row["lease"], row["epoch"]))
            connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)", (self.registry.clock(), operation_id, "snapshot-create"))
        try:
            mkdir(journal)
            write(journal / "requested.json", canonical(requested))
            mkdir(staging)
            mkdir(staging / "data")
            copied = []
            for item in sources:
                self._clean(payload, copying=True)
                data = regular_bytes(source_root / item["path"])
                require(entry(item["path"], data) == item, "SOURCE_CHANGED", "コピー中に元データが変更されました")
                data = shared_data(item["path"], data) if candidate else transform(item["path"], data)
                self.registry.runtime.copy_space(len(data))
                target = staging / "data" / item["path"]
                missing = []
                parent = target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for parent in reversed(missing):
                    mkdir(parent)
                write(target, data)
                copied.append(entry(item["path"], data))
            if candidate:
                require(self._registered(source_id) == source_manifest, "SOURCE_CHANGED", "元snapshotが変更されました")
                current_layout, current_sources = source_manifest["layout"], source_manifest["files"]
            else:
                current_layout, current_sources = collect(generation)
            require((current_layout, current_sources) == (layout, sources), "SOURCE_CHANGED", "元データの集合がコピー中に変更されました")
            self._clean(payload, copying=True)
            require(verify_bundle(generation / "runtime") == artifact
                    and verify_bundle(self.registry.root / "artifacts" / row["artifact"]) == artifact,
                    "ARTIFACT_MISMATCH", "コピー中にruntimeが変更されました")
            clean = {"instanceId": row["id"], "generation": row["generation"], "runId": run["runId"], "nonce": run["nonce"],
                     "stopRequested": True, "jvmAndChildrenExited": True, "externalWritersExited": True,
                     "backgroundWritersDrained": True, "saveResults": [
                         {"writer": key, "success": value, "pendingCount": 0} for key, value in sorted(saved["saves"]["results"].items())]}
            manifest = {"schemaVersion": 1, "snapshotId": snapshot_id, "generation": row["generation"], "artifactId": row["artifact"],
                        "layout": layout, "transformVersion": 1, "visibility": "shared-template" if candidate else "private", "cleanStop": clean, "files": copied}
            validate("manifest", manifest)
            manifest_hash = digest(canonical(manifest))
            write(staging / "manifest.json", canonical(manifest))
            write(journal / "prepared.json", canonical({"manifestHash": manifest_hash}))
            write(staging / "COMPLETE", manifest_hash.encode())
            verify_snapshot(staging)
            self._clean(payload, copying=True)
            move(staging, destination)
            write(journal / "published.json", canonical({"manifestHash": manifest_hash}))
            self._clean(payload, copying=True)
            result = {"snapshotId": snapshot_id, "artifactId": row["artifact"], "manifestHash": manifest_hash, "created": True, "verified": True}
            with self.registry.transaction() as connection:
                current = self.registry._instance(connection, row["id"])
                self.registry._token(current, payload)
                require(current["state"] == "SNAPSHOTTING" and current["pending"] == 1, "BUSY", "snapshotの所有状態が変更されました")
                connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)", (snapshot_id, row["artifact"], manifest_hash, operation_id, self.registry.clock()))
                connection.execute("UPDATE snapshot_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation_id))
                connection.execute("UPDATE instances SET state='LEASED_STOPPED',pending=0,clean_proof=? WHERE id=?", (json.dumps(clean), row["id"]))
                connection.execute("DELETE FROM reservations WHERE operation=?", (operation_id,))
            write(journal / "committed.json", canonical(result))
            return result
        except BaseException as error:
            with self.registry.transaction() as connection:
                connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=?", (row["id"],))
                connection.execute("UPDATE snapshot_operations SET status='RECOVERY_REQUIRED' WHERE id=? AND status!='COMPLETED'", (operation_id,))
            if isinstance(error, RegistryError):
                raise
            raise RegistryError("SNAPSHOT_FAILED", "snapshot作成に失敗しました。元世代・journal・作業領域を保全します") from None
