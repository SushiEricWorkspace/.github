"""独立枠の実API検証と運用者の明示承認を、不変テンプレート参照へ結び付ける。"""
import json

from .build_inputs import canonical, digest, regular_bytes
from .contracts import require_compatible, validate
from .fixtures import FIXTURES
from .local_ipc import plain_path, require_private
from .registry import RegistryError, fields, identifier, require
from .runtime_bundle import verify_bundle
from .runtime_control import TOKEN, read_json
from .snapshot_data import entry, transform
from .template_data import require_shared
from . import storage


TABLES = ("operations", "runtime_operations", "writer_operations", "reference_operations", "snapshot_operations")


class TemplateControl:
    """snapshotとテンプレートを上書きしない。失敗時はjournal・予約・途中ファイルを保全する。"""
    def __init__(self, registry):
        self.registry = registry

    def _shared(self, snapshot_id):
        manifest = self.registry.snapshot._registered(snapshot_id)
        root = self.registry.root / "snapshots" / snapshot_id / "data"
        require_shared(manifest, lambda name: regular_bytes(root / name))
        require_compatible(manifest, verify_bundle(self.registry.root / "artifacts" / manifest["artifactId"]))
        return manifest, root

    def _report(self, instance, run_id, fixture, action):
        with self.registry.transaction(read_only=True) as connection:
            row = connection.execute("SELECT record FROM runs WHERE id=? AND instance=?", (run_id, instance)).fetchone()
            require(row is not None, "PROOF_REQUIRED", "fixtureのrun記録がありません")
            run = json.loads(row[0])
        path = self.registry.runtime._paths(instance) / "diagnostics" / run_id / f"fixture-{fixture}-{action}.json"
        require_private(path)
        document = read_json(path)
        fields(document, {"schemaVersion", "fixture", "seed", "instanceId", "generationId", "nonce", "oreRevivalSecond",
                          "action", "inventoryCount", "money", "internalId", "files"})
        spec = next(value for value in FIXTURES if value["name"] == fixture)
        expected = spec["expected"]
        require(document["schemaVersion"] == 1 and document["fixture"] == fixture and document["action"] == action
                and document["instanceId"] == instance and document["generationId"] == run["generationId"]
                and document["nonce"] == run["nonce"] and document["seed"] == spec["seed"]
                and document["inventoryCount"] == expected["inventoryCount"] and document["money"] == expected["money"]
                and document["internalId"] == spec["itemDefinition"]["internalId"]
                and document["oreRevivalSecond"] == expected["oreRevivalSecond"],
                "PROOF_MISMATCH", "fixtureの実API結果・識別・期待値が一致しません")
        names = {expected[key] for key in ("vanillaPlayerFile", "modProfileFile", "itemDefinitionFile", "unknownRegularFile")}
        names.update(expected["dimensionMarkers"])
        require(type(document["files"]) is list and len(document["files"]) == len(names)
                and {file["path"] for file in document["files"]} == names,
                "PROOF_MISMATCH", "fixtureの検証ファイル集合が一致しません")
        return run, path, document

    def _validation(self, payload, row, manifest, root):
        fixture = payload["fixture"]
        require(fixture in {value["name"] for value in FIXTURES}, "INVALID_INPUT", "固定fixture名を指定してください")
        clean = manifest["cleanStop"]
        require(clean["instanceId"] != row["id"] and row["artifact"] == manifest["artifactId"],
                "VALIDATION_REQUIRED", "同じ固定artifactを別のinstanceへ復元して検証してください")
        generation = self.registry.runtime._active_generation(row)
        record = read_json(generation / "generation.json")
        request = read_json(self.registry.generation._journal(record["operationId"]) / "requested.json")
        with self.registry.transaction(read_only=True) as connection:
            operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (record["operationId"],)).fetchone()
            require(operation is not None and operation["status"] == "COMPLETED" and operation["command"] == "restore",
                    "VALIDATION_REQUIRED", "完成したsnapshot復元の記録が必要です")
            require(operation["fingerprint"] == request["fingerprint"] == digest(canonical({"command": request["command"], "payload": request["payload"]})),
                    "PROOF_MISMATCH", "復元journalと登録要求が一致しません")
        require(request["command"] == "restore" and request["payload"]["snapshotId"] == payload["snapshotId"]
                and request["payload"]["generationId"] == row["generation"]
                and request["sourceHash"] == digest(canonical(manifest)),
                "PROOF_MISMATCH", "実際に復元した候補snapshot・世代・hashが一致しません")
        source_run, _, created = self._report(clean["instanceId"], clean["runId"], fixture, "create")
        source_generation = self.registry.runtime._paths(clean["instanceId"]) / "generations" / clean["generation"]
        source_record = read_json(source_generation / "generation.json")
        prepared = read_json(self.registry.generation._journal(source_record["operationId"]) / "requested.json")
        spec = next(value for value in FIXTURES if value["name"] == fixture)
        require(prepared["command"] == "prepare" and prepared["sourceHash"] is None
                and prepared["payload"]["seed"] == spec["seed"]
                and prepared["payload"]["generationId"] == clean["generation"],
                "PROOF_MISMATCH", "固定seedから新規生成したfixtureが必要です。既存worldを自動取り込みしません")
        with self.registry.transaction(read_only=True) as connection:
            source_operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (source_record["operationId"],)).fetchone()
            require(source_operation is not None and source_operation["status"] == "COMPLETED"
                    and source_operation["command"] == "prepare"
                    and source_operation["fingerprint"] == prepared["fingerprint"]
                    == digest(canonical({"command": "prepare", "payload": prepared["payload"]})),
                    "PROOF_MISMATCH", "新規生成journalと登録要求が一致しません")
        run, report_path, observed = self._report(row["id"], row["run_id"], fixture, "verify")
        require(source_run["artifactId"] == run["artifactId"] == manifest["artifactId"]
                and created["files"] == observed["files"], "PROOF_MISMATCH", "生成・復元試験のartifactまたはデータが一致しません")
        for file in created["files"]:
            require(file == entry(file["path"], regular_bytes(root / file["path"]))
                    == entry(file["path"], regular_bytes(generation / file["path"])),
                    "SOURCE_CHANGED", "fixtureのデータがsnapshot・独立枠・API証跡間で一致しません")
        # worldの保存値は実起動後に変わるため、復元直後の記録で全コピーを照合する。
        initial = {file["path"]: file for file in record["files"]}
        for file in manifest["files"]:
            if file["path"] in {"server.properties", "config/SushiEricServerMod/management/config.yml"}:
                data = transform(file["path"], regular_bytes(generation / file["path"]))
                require(entry(file["path"], data) == file, "SETTINGS_UNSUPPORTED", "復元後の可搬設定が一致しません")
            else:
                require(initial.get(file["path"]) == file, "PROOF_MISMATCH", "復元直後の全データhashが一致しません")
        stopped_run, saved = self.registry.snapshot.stopped(row, generation)
        proof = dict(manifest["cleanStop"], instanceId=row["id"], generation=row["generation"], runId=run["runId"], nonce=run["nonce"],
                     saveResults=[{"writer": key, "success": value, "pendingCount": 0} for key, value in sorted(saved["saves"]["results"].items())])
        spec = next(value for value in FIXTURES if value["name"] == fixture)
        document = {"schemaVersion": 1, "validationId": payload["validationId"], "snapshotId": payload["snapshotId"],
                    "artifactId": manifest["artifactId"], "fixture": fixture, "sourceInstanceId": clean["instanceId"],
                    "restoredInstanceId": row["id"], "runId": stopped_run["runId"], "nonce": stopped_run["nonce"],
                    "cleanStop": proof, "passedChecks": spec["expected"]["checks"],
                    "reportFiles": [entry(report_path.name, regular_bytes(report_path))]}
        validate("validation", document)
        return document

    def _completed(self, validation_id):
        path = self.registry.root / "manager/audit" / ("validation-" + validation_id + ".json")
        require_private(path)
        document = read_json(path)
        fields(document, {"operationId", "manifestHash", "validation"})
        validate("validation", document["validation"])
        require(document["validation"]["validationId"] == validation_id, "PROOF_MISMATCH", "validationIdが一致しません")
        with self.registry.transaction(read_only=True) as connection:
            row = connection.execute("SELECT * FROM snapshot_operations WHERE id=?", (document["operationId"],)).fetchone()
            require(row is not None and row["status"] == "COMPLETED"
                    and json.loads(row["response"]).get("validationHash") == digest(canonical(document)),
                    "VALIDATION_REQUIRED", "完成した検証の登録・hashがありません")
        return document

    def registered(self, template_id):
        with self.registry.transaction(read_only=True) as connection:
            retired = connection.execute("SELECT value FROM meta WHERE key='gcRetiredTemplates'").fetchone()
            require(not retired or template_id not in json.loads(retired[0]), "TEMPLATE_RETIRED", "このテンプレートは明示的に廃止されています")
        path = self.registry.root / "templates" / (template_id + ".json")
        require(path.exists(), "NOT_FOUND", "登録テンプレートがありません")
        require_private(path)
        template = read_json(path)
        fields(template, {"schemaVersion", "templateId", "snapshotId", "manifestHash", "artifactId", "validationId", "operationId"})
        require(template["schemaVersion"] == 1 and template["templateId"] == template_id, "TEMPLATE_MISMATCH", "テンプレート識別が一致しません")
        with self.registry.transaction(read_only=True) as connection:
            row = connection.execute("SELECT * FROM snapshot_operations WHERE id=?", (template["operationId"],)).fetchone()
            require(row is not None and row["status"] == "COMPLETED"
                    and json.loads(row["response"]).get("templateHash") == digest(canonical(template)),
                    "TEMPLATE_MISMATCH", "承認済み昇格の登録・hashがありません")
        proof = self._completed(identifier(template["validationId"], "validationId"))
        manifest, _ = self._shared(identifier(template["snapshotId"], "snapshotId"))
        require(template["artifactId"] == manifest["artifactId"] and template["manifestHash"] == proof["manifestHash"] == digest(canonical(manifest))
                and proof["validation"]["snapshotId"] == template["snapshotId"], "TEMPLATE_MISMATCH", "検証・テンプレート・snapshotが一致しません")
        return template

    def recover(self, row, operation_id, action, *, dry_run):
        require(action == "resume", "RECOVERY_REQUIRED", "検証・昇格の公開物は取り消し・削除しません。照合付きresumeを使用してください")
        journal = self.registry.root / "manager/operations" / ("template-" + operation_id)
        require_private(journal, directory=True)
        request = read_json(journal / "requested.json")
        fields(request, {"operationId", "fingerprint", "payload", "owner", "manifestHash"})
        payload = request["payload"]
        action = payload["action"]
        extra = {"fixture", "validationId"} if action == "validate" else {"templateId", "validationId", "approvedBy", "taskId", "purpose"}
        fields(payload, TOKEN | {"action", "snapshotId"} | extra)
        require(action in {"validate", "promote"} and request["operationId"] == operation_id
                and request["owner"] == row["owner"]
                and all(payload[key] == value for key, value in {"instanceId": row["id"], "leaseId": row["lease"],
                    "epoch": row["epoch"], "expectedGeneration": row["generation"]}.items()),
                "JOURNAL_MISMATCH", "元の操作・owner・lease・世代が一致しません")
        manifest, root = self._shared(identifier(payload["snapshotId"], "snapshotId"))
        manifest_hash = digest(canonical(manifest))
        require(request["manifestHash"] == manifest_hash, "SOURCE_CHANGED", "元の候補hashが一致しません")
        with self.registry.transaction(read_only=True) as connection:
            operation = connection.execute("SELECT * FROM snapshot_operations WHERE id=? AND instance=?", (operation_id, row["id"])).fetchone()
            require(operation is not None and operation["fingerprint"] == request["fingerprint"] == digest(canonical(payload)),
                    "JOURNAL_MISMATCH", "登録要求とjournalが一致しません")
        identifier(payload["validationId"], "validationId")
        validation = self._validation(dict(payload, fixture=payload["templateId"]) if action == "promote" else payload, row, manifest, root)
        if action == "validate":
            document = {"operationId": operation_id, "manifestHash": manifest_hash, "validation": validation}
            destination = self.registry.root / "manager/audit" / ("validation-" + payload["validationId"] + ".json")
            result = {"validationId": payload["validationId"], "validationHash": digest(canonical(document)), "validated": True, "runId": row["run_id"]}
        else:
            identifier(payload["taskId"], "taskId")
            require(payload["approvedBy"] == self.registry.inventory["operator"] and type(payload["purpose"]) is str
                    and bool(payload["purpose"].strip()) and payload["templateId"] in {value["name"] for value in FIXTURES},
                    "APPROVAL_REQUIRED", "元の運用者承認・用途・固定版が必要です")
            previous = self._completed(payload["validationId"])
            require(previous["manifestHash"] == manifest_hash and previous["validation"] == validation,
                    "SOURCE_CHANGED", "元の完成した検証と再照合結果が一致しません")
            document = {"schemaVersion": 1, "templateId": payload["templateId"], "snapshotId": payload["snapshotId"],
                        "manifestHash": manifest_hash, "artifactId": manifest["artifactId"], "validationId": payload["validationId"], "operationId": operation_id}
            destination = self.registry.root / "templates" / (payload["templateId"] + ".json")
            result = {"templateId": payload["templateId"], "templateHash": digest(canonical(document)), "promoted": True, "runId": row["run_id"]}
        require_private(destination)
        require(regular_bytes(destination) == canonical(document), "JOURNAL_MISMATCH", "公開済み証跡が再照合結果と一致しません")
        if operation["status"] == "COMPLETED":
            require(json.loads(operation["response"]) == result, "JOURNAL_MISMATCH", "確定済み結果が一致しません")
        if dry_run:
            return
        # 外側のrecoverがinstance lock・writer不在・正常停止を照合している。
        self.registry.generation._stopped(row)
        require(self._shared(payload["snapshotId"])[0] == manifest, "SOURCE_CHANGED", "復旧中に候補が変更されました")
        self.registry.generation._marker(journal, "committed", result)
        with self.registry.transaction() as connection:
            current = self.registry._instance(connection, row["id"])
            require(all(current[key] == row[key] for key in ("lease", "epoch", "generation", "run_id", "owner")),
                    "JOURNAL_MISMATCH", "復旧中に所有情報が変更されました")
            connection.execute("UPDATE snapshot_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation_id))
            connection.execute("DELETE FROM reservations WHERE operation=?", (operation_id,))

    def execute(self, payload, *, operation_id, dry_run=False):
        action = payload["action"]
        extra = {"fixture", "validationId"} if action == "validate" else {"templateId", "validationId", "approvedBy", "taskId", "purpose"}
        fields(payload, TOKEN | {"action", "snapshotId"} | extra)
        for name in ("snapshotId", "validationId"):
            identifier(payload[name], name)
        if action == "promote":
            identifier(payload["templateId"], "templateId")
            identifier(payload["taskId"], "taskId")
            require(payload["approvedBy"] == self.registry.inventory["operator"]
                    and type(payload["purpose"]) is str and bool(payload["purpose"].strip()),
                    "APPROVAL_REQUIRED", "inventoryの運用者の明示承認と用途が必要です")
            require(payload["templateId"] in {value["name"] for value in FIXTURES}, "INVALID_INPUT", "初期テンプレートは固定の名前・版だけを指定してください")
        fingerprint = digest(canonical(payload))
        with self.registry.transaction(read_only=True) as connection:
            for table in TABLES[:-1]:
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(), "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
            cached = connection.execute("SELECT * FROM snapshot_operations WHERE id=?", (operation_id,)).fetchone()
        row, _, run, _ = self.registry.snapshot._clean(payload)
        manifest, root = self._shared(payload["snapshotId"])
        manifest_hash = digest(canonical(manifest))
        if cached:
            require(cached["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "同じoperationIdに異なる要求は指定できません")
            require(cached["status"] == "COMPLETED", "RECOVERY_REQUIRED", "途中の検証・昇格は証跡を保全して診断してください")
            result = json.loads(cached["response"])
            require(result["runId"] == run["runId"], "STALE_OPERATION", "別runへ古い結果を適用しません")
            if action == "validate":
                self._completed(payload["validationId"])
            else:
                self.registered(payload["templateId"])
            return dict(result, replayed=True, dryRun=dry_run)
        if action == "validate":
            validation = self._validation(payload, row, manifest, root)
            document = {"operationId": operation_id, "manifestHash": manifest_hash, "validation": validation}
            destination = self.registry.root / "manager/audit" / ("validation-" + payload["validationId"] + ".json")
            result = {"validationId": payload["validationId"], "validationHash": digest(canonical(document)), "validated": True, "runId": run["runId"]}
        else:
            validation = self._completed(payload["validationId"])
            require(validation["manifestHash"] == manifest_hash and validation["validation"]["snapshotId"] == payload["snapshotId"]
                    and validation["validation"]["fixture"] == payload["templateId"]
                    and validation["validation"]["runId"] == row["run_id"], "VALIDATION_REQUIRED", "この候補と独立試験runに一致する検証が必要です")
            # 古い検証ファイルだけでは昇格せず、停止済み世代と実API結果を再照合する。
            current = self._validation(dict(payload, fixture=payload["templateId"]), row, manifest, root)
            require(current == validation["validation"], "SOURCE_CHANGED", "検証後に試験結果が変更されました")
            document = {"schemaVersion": 1, "templateId": payload["templateId"], "snapshotId": payload["snapshotId"],
                        "manifestHash": manifest_hash, "artifactId": manifest["artifactId"], "validationId": payload["validationId"], "operationId": operation_id}
            destination = self.registry.root / "templates" / (payload["templateId"] + ".json")
            result = {"templateId": payload["templateId"], "templateHash": digest(canonical(document)), "promoted": True, "runId": run["runId"]}
        journal = self.registry.root / "manager/operations" / ("template-" + operation_id)
        for path in (destination, journal):
            plain_path(path)
            require(not path.exists(), "EXISTING_DATA", "検証・テンプレートの名前と版を上書きしません")
        if dry_run:
            return {"dryRun": True, "wouldExecute": "snapshot-" + action, "manifestHash": manifest_hash}
        with self.registry.transaction() as connection:
            self.registry._token(self.registry._instance(connection, row["id"]), payload)
            for table in TABLES:
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(), "OPERATION_CONFLICT", "operationIdが使用されました")
            connection.execute("INSERT INTO snapshot_operations VALUES (?,?,?,?,?)", (operation_id, fingerprint, row["id"], "PENDING", json.dumps(payload)))
            connection.execute("UPDATE instances SET state='SNAPSHOTTING',pending=1 WHERE id=?", (row["id"],))
            for reference in ("artifact:" + manifest["artifactId"], "snapshot:" + payload["snapshotId"], "validation:" + payload["validationId"]):
                connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)", (operation_id, reference, row["id"], row["lease"], row["epoch"]))
            connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)", (self.registry.clock(), operation_id, "snapshot-" + action))
        try:
            storage.mkdir(journal)
            storage.write(journal / "requested.json", canonical({"operationId": operation_id, "fingerprint": fingerprint, "payload": payload, "owner": row["owner"], "manifestHash": manifest_hash}))
            self.registry.snapshot._clean(payload, copying=True)
            storage.write(destination, canonical(document))
            require(self._shared(payload["snapshotId"])[0] == manifest, "SOURCE_CHANGED", "公開中に候補が変更されました")
            self.registry.snapshot._clean(payload, copying=True)
            with self.registry.transaction() as connection:
                self.registry._token(self.registry._instance(connection, row["id"]), payload)
                connection.execute("UPDATE snapshot_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation_id))
                connection.execute("UPDATE instances SET state='LEASED_STOPPED',pending=0 WHERE id=?", (row["id"],))
                connection.execute("DELETE FROM reservations WHERE operation=?", (operation_id,))
            storage.write(journal / "committed.json", canonical(result))
            return result
        except BaseException as error:
            with self.registry.transaction() as connection:
                connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=?", (row["id"],))
                connection.execute("UPDATE snapshot_operations SET status='RECOVERY_REQUIRED' WHERE id=? AND status!='COMPLETED'", (operation_id,))
            if isinstance(error, RegistryError):
                raise
            raise RegistryError("TEMPLATE_FAILED", "検証・昇格に失敗しました。候補・検証・journal・予約を保全します") from None
