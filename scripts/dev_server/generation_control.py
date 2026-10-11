"""全世代を構築してから参照だけを切り替える。旧世代・途中証跡を削除しない。"""
import json
import os
import re
import secrets
from pathlib import Path

from .build_inputs import canonical, digest, regular_bytes
from .contracts import LAYOUT_ID, require_compatible
from .local_ipc import make_private, plain_path, require_private
from .registry import RegistryError, fields, identifier, require, ttl
from .runtime_bundle import verify_bundle, _files
from .runtime_control import TOKEN, read_json
from .snapshot_control import verify_snapshot
from .snapshot_data import entry, properties, collect, transform
from .template_data import shared_data
from . import storage


TABLES = ("operations", "reference_operations", "writer_operations", "snapshot_operations", "runtime_operations")


class GenerationControl:
    """instance/deployment OS lock内で実行する。コピー・OS照合中はDB lockを保持しない。"""
    def __init__(self, registry):
        self.registry = registry

    def _row(self, payload, *, recovery=False):
        with self.registry.transaction(read_only=True) as connection:
            row = dict(self.registry._instance(connection, payload["instanceId"]))
            if recovery:
                require(type(payload["epoch"]) is int and row["epoch"] == payload["epoch"]
                        and row["lease"] == payload["leaseId"],
                        "STALE_LEASE", "復旧するlease/epochが一致しません")
                require(row["generation"] == payload["expectedGeneration"], "STALE_GENERATION", "期待世代が一致しません")
            else:
                self.registry._token(row, payload)
            require(self.registry.writer.summary(connection, row)["externalWritersVerified"]
                    and not row["children"]
                    and not connection.execute("SELECT 1 FROM writer_sessions WHERE instance=? AND pending!=0", (row["id"],)).fetchone()
                    and not connection.execute("SELECT 1 FROM endpoint_reservations WHERE instance=?", (row["id"],)).fetchone(),
                    "PROOF_REQUIRED", "writer・child・待受予約の不在が必要です。結果不明は解除しません")
        return row

    def _stopped(self, row):
        if row["run_id"]:
            run = self.registry.runtime._load_run(row)
            require(run.get("identity") is not None and run.get("creation") is not None,
                    "PROCESS_UNVERIFIED", "生成・開始識別の記録がありません")
            require(self.registry.runtime.process_adapter.identity(run["identity"]["pid"]) is None,
                    "PROCESS_MISMATCH", "記録PIDが生存または再利用されています。停止しません")
            require(not self.registry.runtime.process_adapter.recorded_members(run["creation"], run["nonce"]),
                    "PROCESS_MISMATCH", "所有groupの子プロセスが残っています")
            generation = self.registry.runtime._paths(row["id"]) / "generations" / run["generationId"]
            self.registry.snapshot.stopped(row, generation)

    def _journal(self, operation):
        return self.registry.root / "manager/operations" / ("generation-" + operation)

    def _marker(self, journal, name, value):
        path = journal / (name + ".json")
        if path.exists():
            require(regular_bytes(path) == canonical(value), "JOURNAL_MISMATCH", "journalの内容が一致しません")
        else:
            storage.write(path, canonical(value), resume=True)

    def _source(self, command, payload):
        if command == "prepare":
            return None
        snapshot_id = payload.get("snapshotId")
        if command == "reset":
            template_id = identifier(payload["templateId"], "templateId")
            from .template_control import TemplateControl
            template = TemplateControl(self.registry).registered(template_id)
            snapshot_id = identifier(template["snapshotId"], "snapshotId")
        snapshot_id = identifier(snapshot_id, "snapshotId")
        manifest = self.registry.snapshot._registered(snapshot_id)
        if command == "reset":
            require(template["manifestHash"] == digest(canonical(manifest))
                    and template["artifactId"] == manifest["artifactId"],
                    "TEMPLATE_MISMATCH", "テンプレート参照とsnapshotが一致しません")
        return manifest

    def execute(self, command, payload, *, operation_id, dry_run=False):
        if command == "recover":
            return self.recover(payload, operation_id=operation_id, dry_run=dry_run)
        identifier(operation_id, "operationId")
        extra = {"prepare": {"seed"}, "restore": {"snapshotId"}, "reset": {"templateId"}}[command]
        fields(payload, TOKEN | {"artifactId", "generationId", "acceptEula"} | extra)
        identifier(payload["generationId"], "generationId")
        require(type(payload["artifactId"]) is str and re.fullmatch("[0-9a-f]{64}", payload["artifactId"])
                and payload["acceptEula"] is True, "INVALID_INPUT", "固定artifactとEULA同意が必要です")
        if command == "prepare":
            require(type(payload["seed"]) is int and -(2**63) <= payload["seed"] < 2**63,
                    "INVALID_INPUT", "seedは符号付き64bit整数です")
        fingerprint = digest(canonical({"command": command, "payload": payload}))
        with self.registry.transaction(read_only=True) as connection:
            for table in TABLES[:-1]:
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが他の操作に使われています")
            cached = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
        if cached:
            require(cached["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "同じoperationIdに異なる要求は指定できません")
            require(cached["status"] == "COMPLETED", "RECOVERY_REQUIRED", "途中操作はrecoverで照合してください")
            row = self._row(dict(payload, expectedGeneration=payload["generationId"]))
            require(not row["run_id"], "STALE_OPERATION", "起動後に古い世代切替を再適用しません")
            self.registry.runtime._active_generation(row)
            return dict(json.loads(cached["response"]), replayed=True, dryRun=dry_run)
        row = self._row(payload)
        require(row["state"] in {"LEASED_STOPPED", "READY"} and not row["pending"], "NOT_STOPPED", "停止済み枠が必要です")
        self._stopped(row)
        root = self.registry.runtime._paths(row["id"])
        if row["generation"] is not None:
            self.registry.runtime._active_generation(row)
        else:
            require(not root.exists() or (root.is_dir() and not any(root.iterdir())), "EXISTING_DATA", "初回枠に既存データがあります")
        require(command != "prepare" or row["generation"] is None, "EXISTING_DATA", "prepareは初回の空枠だけに使用します")
        bundle = self.registry.root / "artifacts" / payload["artifactId"]
        artifact = verify_bundle(bundle)
        require(artifact["artifactId"] == payload["artifactId"] and artifact["layoutId"] == LAYOUT_ID,
                "ARTIFACT_MISMATCH", "artifactまたはlayoutが一致しません")
        manifest = self._source(command, payload)
        if manifest:
            require_compatible(manifest, artifact)
        self.registry.runtime._space(sum(item["size"] for item in artifact["files"])
                                    + (bundle / "runtime.json").stat().st_size
                                    + (sum(item["size"] for item in manifest["files"]) if manifest else 0) + 65536)
        journal = self._journal(operation_id)
        staging = root / "staging" / operation_id
        destination = root / "generations" / payload["generationId"]
        for path in (journal, staging, destination):
            plain_path(path)
            require(not path.exists(), "EXISTING_DATA", "世代・作業領域・journalを上書きしません")
        if dry_run:
            return {"dryRun": True, "wouldExecute": command, "instanceId": row["id"]}
        request = {"operationId": operation_id, "fingerprint": fingerprint, "command": command,
                   "payload": payload, "old": row, "sourceHash": digest(canonical(manifest)) if manifest else None}
        with self.registry.transaction() as connection:
            current = self.registry._instance(connection, row["id"])
            self.registry._token(current, payload)
            require(current["state"] == row["state"] and not current["pending"], "BUSY", "枠の状態が変更されました")
            for table in TABLES:
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが使用されました")
            connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                               (operation_id, fingerprint, command, row["id"], "PENDING", json.dumps(request)))
            connection.execute("UPDATE instances SET state='PREPARING',pending=1 WHERE id=?", (row["id"],))
            references = ({"artifact:" + payload["artifactId"], "generation:" + payload["generationId"]}
                    | ({"generation:" + row["generation"]} if row["generation"] else set())
                    | ({"snapshot:" + manifest["snapshotId"]} if manifest else set()))
            for reference in references:
                connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)", (operation_id, reference, row["id"], row["lease"], row["epoch"]))
            connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)", (self.registry.clock(), operation_id, command))
        try:
            storage.mkdir(journal)
            storage.write(journal / "requested.json", canonical(request))
            self._build(request, bundle, manifest, staging)
            return self._commit(request)
        except BaseException as error:
            with self.registry.transaction() as connection:
                connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=?", (row["id"],))
            if isinstance(error, RegistryError):
                raise
            raise RegistryError("GENERATION_FAILED", "世代構築に失敗しました。旧世代・途中データを保全します") from None

    def _build(self, request, bundle, manifest, staging):
        row, payload = request["old"], request["payload"]
        root = self.registry.runtime._paths(row["id"])
        for path in (root, root / "generations", root / "staging", staging):
            if not path.exists():
                storage.mkdir(path)
            require_private(path, directory=True)
        sources = [(bundle / file["path"], "runtime/" + file["path"], file) for file in verify_bundle(bundle)["files"]]
        sources += [(bundle / name, "runtime/" + name, entry(name, regular_bytes(bundle / name))) for name in ("runtime.json", "COMPLETE")]
        if manifest:
            sources += [(self.registry.root / "snapshots" / manifest["snapshotId"] / "data" / file["path"], file["path"], file)
                        for file in manifest["files"]]
        declaration = next(item for item in self.registry.inventory["instances"] if item["instanceId"] == row["id"])
        values = properties(regular_bytes(self.registry.root / "snapshots" / manifest["snapshotId"] / "data/server.properties")) if manifest else {"level-name": "world", "level-seed": payload["seed"]}
        values.update({"server-ip": declaration["minecraftBind"], "server-port": declaration["ports"]["minecraft"],
                       "enable-rcon": "true", "rcon.port": declaration["ports"]["rcon"], "rcon.password": secrets.token_hex(24),
                       "enable-query": "false", "online-mode": "true"})
        for source, name, file in sources:
            data = regular_bytes(source)
            require(entry(file["path"], data) == file, "SOURCE_CHANGED", "コピー元のhashが変更されました")
            if name == "server.properties":
                continue
            if name == "config/SushiEricServerMod/management/config.yml":
                require(data == b"{}\n", "SETTINGS_UNSUPPORTED", "可搬management設定が不正です")
                continue
            self._write_tree(staging, name, data)
            if os.name != "nt" and name.startswith("runtime/") and source.stat().st_mode & 0o100:
                make_private(staging / name, executable=True)
                storage.flush(staging / name)
        self._write_tree(staging, "server.properties", "".join(f"{key}={value}\n" for key, value in sorted(values.items())).encode())
        self._write_tree(staging, "config/SushiEricServerMod/management/config.yml", f"port: {declaration['ports']['management']}\n".encode())
        self._write_tree(staging, "eula.txt", b"eula=true\n")
        verify_bundle(staging / "runtime")
        require(verify_bundle(bundle) == verify_bundle(staging / "runtime"), "SOURCE_CHANGED", "コピー元runtimeが変更されました")
        if manifest:
            require(self._source(request["command"], payload) == manifest, "SOURCE_CHANGED", "snapshotまたはテンプレートが変更されました")
        record = {"version": 1, "generationId": payload["generationId"], "artifactId": payload["artifactId"],
                  "layoutId": LAYOUT_ID, "operationId": request["operationId"], "files": _files(staging)}
        self._write_tree(staging, "generation.json", canonical(record))
        self._marker(self._journal(request["operationId"]), "prepared", {"recordHash": digest(canonical(record))})

    def _write_tree(self, root, name, data):
        self.registry.runtime.copy_space(len(data))
        path = root / name
        missing = []
        parent = path.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for parent in reversed(missing):
            storage.mkdir(parent)
        storage.write(path, data)

    def _built(self, request):
        payload = request["payload"]
        root = self.registry.runtime._paths(payload["instanceId"])
        staging, destination = root / "staging" / request["operationId"], root / "generations" / payload["generationId"]
        require(staging.exists() != destination.exists(), "JOURNAL_MISMATCH", "新世代の実体が一意ではありません")
        path = destination if destination.exists() else staging
        record = read_json(path / "generation.json")
        prepared = read_json(self._journal(request["operationId"]) / "prepared.json")
        require(prepared == {"recordHash": digest(canonical(record))}
                and record["generationId"] == payload["generationId"] and record["artifactId"] == payload["artifactId"]
                and record["operationId"] == request["operationId"] and record["layoutId"] == LAYOUT_ID
                and [file for file in _files(path) if file["path"] != "generation.json"] == record["files"],
                "GENERATION_MISMATCH", "完成世代の全hashが一致しません")
        verify_bundle(path / "runtime")
        for file in _files(path):
            name = file["path"]
            executable = os.name != "nt" and name.startswith("runtime/") and bool(
                (self.registry.root / "artifacts" / payload["artifactId"] / name.removeprefix("runtime/")).stat().st_mode & 0o100)
            require_private(path / name, executable=executable)
            require((path / file["path"]).stat().st_nlink == 1, "UNSUPPORTED_FILE", "世代のhardlink共有は禁止です")
        return path, destination

    def _switch(self, root, operation, value):
        active = root / "active.json"
        if active.exists() and read_json(active) == value:
            return
        candidate = root / ("active-" + operation + ".json")
        if candidate.exists():
            require(regular_bytes(candidate) == canonical(value), "JOURNAL_MISMATCH", "参照候補が一致しません")
        else:
            storage.write(candidate, canonical(value), resume=True)
        storage.move(candidate, active, replace=active.exists())

    def _commit(self, request, *, recovery=False):
        payload, old, operation = request["payload"], request["old"], request["operationId"]
        path, destination = self._built(request)
        root = self.registry.runtime._paths(old["id"])
        expected = {"version": 1, "generationId": payload["generationId"], "artifactId": payload["artifactId"]}
        active = root / "active.json"
        previous = {"version": 1, "generationId": old["generation"], "artifactId": old["artifact"]}
        require((not active.exists() and old["generation"] is None) or (active.exists() and read_json(active) in (previous, expected)),
                "JOURNAL_MISMATCH", "activeは対象の旧世代または新世代ではありません")
        self._stopped(old)
        with self.registry.transaction(read_only=True) as connection:
            current = self.registry._instance(connection, old["id"])
            if not recovery:
                self.registry._token(current, payload)
            require(current["lease"] == old["lease"] and current["epoch"] == old["epoch"],
                    "STALE_LEASE", "世代切替の所有権が一致しません")
        source = self._source(request["command"], payload)
        require((digest(canonical(source)) if source else None) == request["sourceHash"],
                "SOURCE_CHANGED", "復旧時のコピー元参照が変更されています")
        if path != destination:
            storage.move(path, destination)
        self._switch(root, operation, expected)
        journal = self._journal(operation)
        self._marker(journal, "switched", expected)
        result = {"instanceId": old["id"], "generation": payload["generationId"], "artifactId": payload["artifactId"],
                  "prepared": True, "snapshotClean": False, "operationId": operation}
        with self.registry.transaction() as connection:
            row = self.registry._instance(connection, old["id"])
            require(row["lease"] == old["lease"] and row["epoch"] == old["epoch"]
                    and row["generation"] in (old["generation"], payload["generationId"]), "STALE_LEASE", "世代切替の所有権が一致しません")
            connection.execute("UPDATE instances SET generation=?,artifact=?,generation_bound=1,state='READY',pending=0,run_id=NULL,runtime_proof=NULL,clean_proof=NULL WHERE id=?",
                               (payload["generationId"], payload["artifactId"], old["id"]))
            connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation))
            connection.execute("DELETE FROM reservations WHERE operation=?", (operation,))
        self._marker(journal, "committed", result)
        return result

    def check_active(self, row, record):
        """常用照合は完成記録とCOMMITTED journalを比較し、変更可能なworldは再hashしない。"""
        operation = identifier(record["operationId"], "operationId")
        journal = self._journal(operation)
        require(read_json(journal / "prepared.json") == {"recordHash": digest(canonical(record))},
                "JOURNAL_MISMATCH", "世代の完成記録が改変されています")
        result = read_json(journal / "committed.json")
        with self.registry.transaction(read_only=True) as connection:
            cached = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation,)).fetchone()
            require(cached is not None and cached["status"] == "COMPLETED" and result == json.loads(cached["response"])
                    and result["generation"] == row["generation"] and result["instanceId"] == row["id"],
                    "RECOVERY_REQUIRED", "世代のjournal・Registryが一致しません")

    def recover(self, payload, *, operation_id, dry_run=False):
        identifier(operation_id, "operationId")
        fields(payload, TOKEN | {"action", "targetOperationId", "ttlSeconds"})
        ttl(payload["ttlSeconds"])
        require(payload["action"] in {"resume", "rollback"}, "INVALID_INPUT", "resumeまたはrollbackを指定してください")
        if payload["targetOperationId"] is not None:
            identifier(payload["targetOperationId"], "targetOperationId")
        fingerprint = digest(canonical({"command": "recover", "payload": payload}))
        with self.registry.transaction(read_only=True) as connection:
            for table in TABLES[:-1]:
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(), "OPERATION_CONFLICT", "operationIdが使用されています")
            cached = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
            if cached:
                require(cached["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "同じ復旧IDに異なる要求は指定できません")
        response = json.loads(cached["response"]) if cached and cached["status"] == "COMPLETED" else None
        row = self._row(dict(payload, expectedGeneration=response["generation"]) if response else payload, recovery=True)
        self._stopped(row)
        if response:
            require(row["state"] != "RECOVERY_REQUIRED" and row["run_id"] == response["runId"],
                    "STALE_OPERATION", "再起動または別runへ復旧結果を再適用しません")
            if row["generation"]:
                self.registry.runtime._active_generation(row)
            return dict(response, replayed=True, dryRun=dry_run)
        with self.registry.transaction(read_only=True) as connection:
            targets = [dict(value) for value in connection.execute("SELECT * FROM runtime_operations WHERE instance=? AND status!='COMPLETED' AND command!='recover'", (row["id"],))]
            snapshots = [dict(value) for value in connection.execute("SELECT * FROM snapshot_operations WHERE instance=? AND status!='COMPLETED'", (row["id"],))]
            if not targets and not snapshots and payload["targetOperationId"]:
                completed = connection.execute("SELECT * FROM runtime_operations WHERE id=? AND instance=? AND command IN ('prepare','restore','reset')",
                                               (payload["targetOperationId"], row["id"])).fetchone()
                if completed:
                    targets = [dict(completed)]
                else:
                    completed = connection.execute("SELECT * FROM snapshot_operations WHERE id=? AND instance=?", (payload["targetOperationId"], row["id"])).fetchone()
                    if completed:
                        snapshots = [dict(completed)]
        target = payload["targetOperationId"]
        require(row["lease"] is not None or (row["state"] == "RECOVERY_REQUIRED" and target is None and not targets and not snapshots
                and payload["action"] == "resume" and not row["pending"]),
                "STALE_LEASE", "未貸出枠の復旧は保留状態の照合だけです")
        require(len(targets) + len(snapshots) <= 1, "RECOVERY_REQUIRED", "複数の未完了操作は自動調整しません")
        request = None
        if targets:
            operation = targets[0]
            require(operation["id"] == target and operation["command"] in {"prepare", "restore", "reset"},
                    "PROCESS_UNVERIFIED", "起動・停止の結果不明または対象操作が一致しません")
            journal = self._journal(target)
            if (journal / "requested.json").exists():
                request = read_json(journal / "requested.json")
            else:
                require(operation["status"] != "COMPLETED" and payload["action"] == "rollback",
                        "JOURNAL_MISMATCH", "完成journalがないためresumeできません")
                request = json.loads(operation["response"])
            require(request["fingerprint"] == operation["fingerprint"]
                    and digest(canonical({"command": request["command"], "payload": request["payload"]})) == operation["fingerprint"]
                    and request["operationId"] == target and request["old"]["id"] == row["id"]
                    and request["old"]["lease"] == row["lease"] and request["old"]["epoch"] == row["epoch"],
                    "JOURNAL_MISMATCH", "復旧journalの要求・所有権が一致しません")
            if payload["action"] == "resume":
                self._built(request)
            else:
                require(row["generation"] == request["old"]["generation"], "RECOVERY_REQUIRED", "DB確定後の取り消しはrestoreを使用してください")
        elif snapshots:
            require(snapshots[0]["id"] == target, "JOURNAL_MISMATCH", "snapshot操作が一致しません")
            self._snapshot_recovery(row, target, payload["action"], dry_run=True)
        else:
            require(target is None, "STALE_OPERATION", "未完了の対象操作がありません")
            with self.registry.transaction(read_only=True) as connection:
                require(not row["pending"] and not connection.execute("SELECT 1 FROM reservations WHERE instance=?", (row["id"],)).fetchone(),
                        "RECOVERY_REQUIRED", "未照合の操作・参照予約があります")
            if row["generation"]:
                self.registry.runtime._active_generation(row)
            else:
                root = self.registry.runtime._paths(row["id"])
                require(not root.exists() or not any(root.iterdir()), "RECOVERY_REQUIRED", "空枠に未確認データがあります")
        if dry_run:
            return {"dryRun": True, "wouldExecute": "recover-" + payload["action"], "instanceId": row["id"]}
        with self.registry.transaction() as connection:
            if not cached:
                connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)", (operation_id, fingerprint, "recover", row["id"], "PENDING", "{}"))
        if request:
            if payload["action"] == "resume":
                self._commit(request, recovery=True)
            else:
                old = request["old"]
                root = self.registry.runtime._paths(row["id"])
                if old["generation"]:
                    self._switch(root, operation_id, {"version": 1, "generationId": old["generation"], "artifactId": old["artifact"]})
                else:
                    archive = self.registry.root / "quarantine" / (old["id"] + "-" + target)
                    if root.exists():
                        require_private(root, directory=True)
                        _files(root)
                        storage.move(root, archive)
                    elif archive.exists():
                        require_private(archive, directory=True)
                journal = self._journal(target)
                if not journal.exists():
                    storage.mkdir(journal)
                storage.write(journal / "requested.json", canonical(request), resume=True)
                self._marker(journal, "rolled-back", {"operationId": operation_id})
                with self.registry.transaction() as connection:
                    connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps({"rolledBack": True}), target))
                    connection.execute("UPDATE instances SET state=?,pending=0 WHERE id=?", (old["state"], row["id"]))
                    connection.execute("DELETE FROM reservations WHERE operation=?", (target,))
        elif snapshots:
            self._snapshot_recovery(row, target, payload["action"], dry_run=False)
        with self.registry.transaction() as connection:
            current = self.registry._instance(connection, row["id"])
            result = {"instanceId": row["id"], "generation": current["generation"], "runId": current["run_id"], "recovered": True, "action": payload["action"]}
            connection.execute("UPDATE instances SET state=?,pending=0,expires=? WHERE id=?",
                               ("AVAILABLE" if current["lease"] is None else "LEASED_STOPPED" if current["run_id"] or current["generation"] is None else "READY",
                                self.registry.clock() + payload["ttlSeconds"] if current["lease"] else None, row["id"]))
            connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation_id))
        return result

    def _snapshot_recovery(self, row, operation, action, *, dry_run):
        if (self.registry.root / "manager/operations" / ("template-" + operation)).exists():
            from .template_control import TemplateControl
            return TemplateControl(self.registry).recover(row, operation, action, dry_run=dry_run)
        journal = self.registry.root / "manager/operations" / ("snapshot-" + operation)
        if (journal / "requested.json").exists():
            request = read_json(journal / "requested.json")
        else:
            require(action == "rollback", "JOURNAL_MISMATCH", "要求journalがないためresumeできません")
            with self.registry.transaction(read_only=True) as connection:
                pending = connection.execute("SELECT * FROM snapshot_operations WHERE id=?", (operation,)).fetchone()
                request = json.loads(pending["response"])
                require(request.get("action") not in {"validate", "promote"}, "RECOVERY_REQUIRED", "途中の検証・昇格を保全してください")
        require(request["operationId"] == operation and request["instanceId"] == row["id"]
                and request["generation"] == row["generation"] and request["runId"] == row["run_id"]
                and request["leaseId"] == row["lease"] and request["epoch"] == row["epoch"], "JOURNAL_MISMATCH", "snapshot対象が一致しません")
        result = {"rolledBack": True}
        manifest = None
        if action == "resume":
            path = self.registry.root / "snapshots" / request["snapshotId"]
            manifest = verify_snapshot(path)
            require(manifest["cleanStop"]["runId"] == row["run_id"] and manifest["artifactId"] == row["artifact"]
                    and read_json(journal / "prepared.json") == {"manifestHash": digest(canonical(manifest))}, "JOURNAL_MISMATCH", "公開snapshotが一致しません")
            generation = self.registry.runtime._active_generation(row)
            if request.get("action") == "candidate":
                original = self.registry.snapshot._registered(request["sourceSnapshotId"])
                require(digest(canonical(original)) == request["sourceHash"], "SOURCE_CHANGED", "候補の元snapshotが変更されました")
                layout, sources = original["layout"], original["files"]
                generation = self.registry.root / "snapshots" / request["sourceSnapshotId"] / "data"
                normalize = shared_data
            else:
                layout, sources = collect(generation)
                normalize = transform
            require(layout == manifest["layout"] and [entry(file["path"], normalize(file["path"], regular_bytes(generation / file["path"]))) for file in sources] == manifest["files"],
                    "SOURCE_CHANGED", "停止世代とsnapshotが一致しません")
            result = {"snapshotId": manifest["snapshotId"], "artifactId": manifest["artifactId"], "manifestHash": digest(canonical(manifest)), "created": True, "verified": True}
        else:
            with self.registry.transaction(read_only=True) as connection:
                require(not connection.execute("SELECT 1 FROM snapshots WHERE operation=?", (operation,)).fetchone(),
                        "RECOVERY_REQUIRED", "登録済みsnapshotを取り消しません")
        if dry_run:
            return
        if not journal.exists():
            storage.mkdir(journal)
        storage.write(journal / "requested.json", canonical(request), resume=True)
        with self.registry.transaction() as connection:
            if manifest:
                registered = connection.execute("SELECT * FROM snapshots WHERE id=?", (manifest["snapshotId"],)).fetchone()
                if registered:
                    require(registered["artifact"] == manifest["artifactId"] and registered["manifest_hash"] == result["manifestHash"]
                            and registered["operation"] == operation, "JOURNAL_MISMATCH", "登録済みsnapshotが一致しません")
                else:
                    connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)", (manifest["snapshotId"], manifest["artifactId"], result["manifestHash"], operation, self.registry.clock()))
            connection.execute("UPDATE snapshot_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation))
            connection.execute("UPDATE instances SET pending=0,clean_proof=? WHERE id=?", (json.dumps(manifest["cleanStop"]) if manifest else None, row["id"]))
            connection.execute("DELETE FROM reservations WHERE operation=?", (operation,))
        self._marker(journal, "committed" if manifest else "rolled-back", result)
