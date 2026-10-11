"""参照と保持期限を照合し、提示済みの通常ファイルだけを明示回収する。"""
import json
import os
from pathlib import Path
import re

from .build_inputs import canonical, digest, regular_bytes
from .local_ipc import plain_path, require_private
from .registry import RegistryError, fields, identifier, require
from .runtime_control import read_json
from .runtime_bundle import _files, verify_bundle
from . import storage


DEFAULT_POLICY = {"snapshotCount": 3, "snapshotSeconds": 14 * 86400,
                  "failureWorldSeconds": 7 * 86400, "resultsSeconds": 30 * 86400}
TABLES = ("operations", "reference_operations", "writer_operations", "snapshot_operations", "runtime_operations")


class GcControl:
    """Registryの排他scope内で実行する。未知データや途中操作は回収しない。"""
    def __init__(self, registry):
        self.registry = registry

    def _meta(self, connection, key, default):
        value = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(value[0]) if value else default

    def policy(self):
        with self.registry.transaction(read_only=True) as connection:
            return self._meta(connection, "gcPolicy", DEFAULT_POLICY)

    def _path(self, reference):
        require(type(reference) is str and ":" in reference, "INVALID_INPUT", "kind:idの参照が必要です")
        kind, name = reference.split(":", 1)
        if kind == "artifact":
            require(re.fullmatch("[0-9a-f]{64}", name), "INVALID_INPUT", "artifact hashが不正です")
            parts = ("artifacts", name)
        elif kind in {"snapshot", "run"}:
            identifier(name, "id")
            parts = ("snapshots" if kind == "snapshot" else "temporary", name)
        elif kind == "generation":
            values = name.split("/")
            require(len(values) == 2, "INVALID_INPUT", "generationはinstance/idで指定してください")
            instance, generation = values
            identifier(instance, "instanceId")
            identifier(generation, "generationId")
            require(instance in {r["instanceId"] for r in self.registry.inventory["instances"]}, "NOT_FOUND", "未登録instanceです")
            parts = ("instances", instance, "generations", generation)
        else:
            raise RegistryError("INVALID_INPUT", "artifact・snapshot・generation・runを指定してください")
        path = self.registry.root.joinpath(*parts)
        plain_path(path)
        return path

    def _roots(self):
        root = self.registry.root
        with self.registry.transaction(read_only=True) as connection:
            instances = [dict(r) for r in connection.execute("SELECT * FROM instances")]
            snapshots = [dict(r) for r in connection.execute("SELECT * FROM snapshots ORDER BY created DESC,id DESC")]
            runs = [json.loads(r[0]) for r in connection.execute("SELECT record FROM runs")]
            reservations = {r[0] for r in connection.execute("SELECT reference FROM reservations")}
            pins = set(self._meta(connection, "gcPins", []))
            retired = set(self._meta(connection, "gcRetiredTemplates", []))
            policy = self._meta(connection, "gcPolicy", DEFAULT_POLICY)
            pending = connection.execute("SELECT 1 FROM runtime_operations WHERE status IN ('PENDING','RECOVERY_REQUIRED') AND command!='gc-apply'").fetchone()
            pending = pending or connection.execute("SELECT 1 FROM snapshot_operations WHERE status!='COMPLETED'").fetchone()
        require(not pending, "BUSY", "未完了・結果不明の操作があるためGC候補を作成できません")
        protected = pins | reservations
        for row in instances:
            if row["artifact"]:
                protected.add("artifact:" + row["artifact"])
            if row["generation"]:
                self.registry.runtime._active_generation(row)
                reference = "generation:" + row["id"] + "/" + row["generation"]
                protected.add(reference)
                record = read_json(self._path(reference) / "generation.json")
                request = read_json(self.registry.generation._journal(record["operationId"]) / "requested.json")
                previous = request["old"]["generation"]
                if previous:
                    protected.add("generation:" + row["id"] + "/" + previous)
        templates = root / "templates"
        promoted = set()
        for path in templates.iterdir():
            plain_path(path)
            require(path.is_file() and path.suffix == ".json", "INVALID_STATE", "未知のtemplateデータがあります")
            if path.stem in retired:
                continue
            from .template_control import TemplateControl
            template = TemplateControl(self.registry).registered(path.stem)
            promoted.add(template["snapshotId"])
            protected.update(("snapshot:" + template["snapshotId"], "artifact:" + template["artifactId"]))
            validation = TemplateControl(self.registry)._completed(template["validationId"])["validation"]
            for instance in (validation["sourceInstanceId"], validation["restoredInstanceId"]):
                for run in runs:
                    if run["instanceId"] == instance and (run["runId"] == validation["runId"] or run["generationId"] == self.registry.snapshot._registered(template["snapshotId"])["generation"]):
                        protected.add("generation:" + instance + "/" + run["generationId"])
                        protected.add("diagnostics:" + instance + "/" + run["runId"])
        unpromoted_count = 0
        for row in snapshots:
            if row["id"] in promoted:
                continue
            unpromoted_count += 1
            if unpromoted_count <= policy["snapshotCount"] or self.registry.clock() <= row["created"] + policy["snapshotSeconds"]:
                protected.add("snapshot:" + row["id"])
        for row in snapshots:
            if "snapshot:" + row["id"] in protected:
                manifest = self.registry.snapshot._registered(row["id"])
                protected.add("artifact:" + manifest["artifactId"])
                clean = manifest["cleanStop"]
                protected.add("generation:" + clean["instanceId"] + "/" + clean["generation"])
                protected.add("diagnostics:" + clean["instanceId"] + "/" + clean["runId"])
        for run in runs:
            if "run:" + run["runId"] in protected or run.get("pinned") or not run.get("jvmAndChildrenExited"):
                protected.add("run:" + run["runId"])
                protected.add("artifact:" + run["artifactId"])
                if run.get("kind") != "gametest":
                    protected.add("generation:" + run["instanceId"] + "/" + run["generationId"])
        # 旧形式のgeneration予約はinstanceを含まないため、同名の全世代を保護する。
        for row in instances:
            generations = root / "instances" / row["id"] / "generations"
            if not generations.exists():
                continue
            plain_path(generations)
            for path in generations.iterdir():
                plain_path(path)
                if "generation:" + path.name in reservations:
                    protected.add("generation:" + row["id"] + "/" + path.name)
                record = read_json(path / "generation.json")
                protected.add("artifact:" + record["artifactId"])
        return protected, snapshots, runs, policy

    def _tree(self, path):
        plain_path(path)
        require_private(path, directory=True)
        files = _files(path)
        directories = ["."]
        for directory, names, _ in os.walk(path, followlinks=False):
            for name in names:
                child = Path(directory) / name
                plain_path(child)
                require_private(child, directory=True)
                directories.append(child.relative_to(path).as_posix())
        for file in files:
            target = path / file["path"]
            require_private(target)
            require(target.stat().st_nlink == 1, "UNSUPPORTED_FILE", "共有hardlinkは回収できません")
        return {"files": files, "directories": sorted(directories)}

    def candidates(self):
        protected, snapshots, runs, policy = self._roots()
        candidates = []
        def add(reference, path):
            if reference in protected or not path.exists():
                return
            tree = self._tree(path)
            candidates.append({"reference": reference, "path": path.relative_to(self.registry.root).as_posix(),
                               "bytes": sum(f["size"] for f in tree["files"]), "tree": tree})
        for row in snapshots:
            if "snapshot:" + row["id"] not in protected:
                self.registry.snapshot._registered(row["id"])
                add("snapshot:" + row["id"], self._path("snapshot:" + row["id"]))
        for declaration in self.registry.inventory["instances"]:
            parent = self.registry.root / "instances" / declaration["instanceId"] / "generations"
            if not parent.exists():
                continue
            plain_path(parent)
            for path in parent.iterdir():
                reference = "generation:" + declaration["instanceId"] + "/" + path.name
                record = read_json(path / "generation.json")
                with self.registry.transaction(read_only=True) as connection:
                    completed = connection.execute("SELECT * FROM runtime_operations WHERE id=? AND command IN ('prepare','restore','reset') AND status='COMPLETED'", (record["operationId"],)).fetchone()
                require(completed is not None and json.loads(completed["response"])["generation"] == path.name,
                        "RECOVERY_REQUIRED", "未確定の世代は回収できません")
                if reference not in protected:
                    for run in runs:
                        if run["instanceId"] == declaration["instanceId"] and run["generationId"] == path.name:
                            self._exited(run)
                add(reference, path)
        for run in runs:
            if run.get("kind") != "gametest":
                path = self.registry.root / "instances" / run["instanceId"] / "diagnostics" / run["runId"]
                reference = "diagnostics:" + run["instanceId"] + "/" + run["runId"]
                if path.exists():
                    with self.registry.transaction(read_only=True) as connection:
                        active = connection.execute("SELECT 1 FROM instances WHERE run_id=?", (run["runId"],)).fetchone()
                    if active or reference in protected or self.registry.clock() <= run.get("exitedAt", self.registry.clock()) + policy["resultsSeconds"]:
                        protected.add("artifact:" + run["artifactId"])
                    elif run.get("jvmAndChildrenExited"):
                        self._exited(run)
                        add(reference, path)
                continue
            if "run:" + run["runId"] in protected or "completedAt" not in run:
                continue
            path = self._path("run:" + run["runId"])
            if not path.exists():
                continue
            self._exited(run)
            result = read_json(path / "diagnostics/result.json")
            require(result["runId"] == run["runId"] and result["artifactId"] == run["artifactId"]
                    and result["jvmAndChildrenExited"] is True
                    and all(key in run and run[key] == value for key, value in result.items()),
                    "PROOF_MISMATCH", "GameTest結果が一致しません")
            if result["testsPassed"] and result["runtimeStopped"]:
                require(digest(regular_bytes(Path(run["proofPath"]))) == result["proofHash"],
                        "PROOF_MISMATCH", "GameTest終了証跡が変わりました")
                from .gametest_report import read_report
                report = read_report(path / "report.xml")
                require(all(result.get(key) == value for key, value in report.items()),
                        "PROOF_MISMATCH", "GameTestレポートが変わりました")
            elapsed = self.registry.clock() - run["completedAt"]
            if elapsed > policy["resultsSeconds"]:
                add("run:" + run["runId"], path)
            else:
                protected.add("artifact:" + run["artifactId"])
                if (result["testsPassed"] and result["runtimeStopped"]) or elapsed > policy["failureWorldSeconds"]:
                    if "world" not in run.get("gcRemoved", []):
                        add("world:" + run["runId"], path / "world")
        for path in (self.registry.root / "artifacts").iterdir():
            plain_path(path)
            if re.fullmatch("[0-9a-f]{64}", path.name) and "artifact:" + path.name not in protected:
                verify_bundle(path)
                add("artifact:" + path.name, path)
        return sorted(candidates, key=lambda x: x["reference"])

    def _exited(self, run):
        adapter = self.registry.runtime.process_adapter
        require(run.get("jvmAndChildrenExited") is True and run.get("identity") is not None
                and run.get("creation") is not None and adapter.identity(run["identity"]["pid"]) is None
                and not adapter.recorded_members(run["creation"], run["nonce"]),
                "PROCESS_MISMATCH", "所有JVM・子プロセスの不在を確認できません")

    def _response(self, result, **extra):
        if "candidates" in result:
            result = dict(result, candidates=[{k: c[k] for k in ("reference", "path", "bytes")} | {"treeHash": digest(canonical(c["tree"]))} for c in result["candidates"]])
        return dict(result, **extra)

    def execute(self, payload, *, operation_id, dry_run=False):
        identifier(operation_id, "operationId")
        require(type(payload) is dict and payload.get("action") in {"plan", "apply", "pin", "policy", "retire-template"},
                "INVALID_INPUT", "GCはplan・apply・pin・policy・retire-templateを指定してください")
        action = payload["action"]
        required = {"plan": set(), "apply": {"planId", "references", "approvedBy"},
                    "pin": {"reference", "pinned"}, "policy": {"policy"}, "retire-template": {"templateId", "approvedBy"}}[action]
        fields(payload, {"action"} | required)
        fingerprint = digest(canonical(payload))
        with self.registry.transaction(read_only=True) as connection:
            for table in TABLES[:-1]:
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(), "OPERATION_CONFLICT", "operationIdが別操作で使用されています")
            previous = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
            if previous:
                require(previous["fingerprint"] == fingerprint and previous["command"] == "gc-" + action, "OPERATION_CONFLICT", "GC要求が一致しません")
                require(previous["status"] == "COMPLETED", "RECOVERY_REQUIRED", "途中GCの記録を保全して診断してください。自動再試行しません")
                if action == "pin":
                    require((payload["reference"] in self._meta(connection, "gcPins", [])) == payload["pinned"],
                            "STALE_OPERATION", "pin設定が後続操作で変更されています")
                elif action == "policy":
                    require(self._meta(connection, "gcPolicy", DEFAULT_POLICY) == payload["policy"],
                            "STALE_OPERATION", "保持設定が後続操作で変更されています")
                return self._response(json.loads(previous["response"]), replayed=True, dryRun=dry_run)
            require(not connection.execute("SELECT 1 FROM runtime_operations WHERE command='gc-apply' AND status!='COMPLETED'").fetchone(), "RECOVERY_REQUIRED", "途中GCが存在します")
        if action == "plan":
            result = {"planId": operation_id, "candidates": self.candidates()}
        elif action == "apply":
            require(payload["approvedBy"] == self.registry.inventory["operator"], "APPROVAL_REQUIRED", "運用者の明示承認が必要です")
            identifier(payload["planId"], "planId")
            references = payload["references"]
            require(type(references) is list and bool(references) and all(type(r) is str for r in references)
                    and len(references) == len(set(references)), "INVALID_INPUT", "提示済み参照を重複なしで指定してください")
            with self.registry.transaction(read_only=True) as connection:
                plan = connection.execute("SELECT * FROM runtime_operations WHERE id=? AND command='gc-plan' AND status='COMPLETED'", (payload["planId"],)).fetchone()
            require(plan is not None, "NOT_FOUND", "保存済みGC計画が必要です")
            old = {c["reference"]: c for c in json.loads(plan["response"])["candidates"]}
            current = {c["reference"]: c for c in self.candidates()}
            require(all(r in old and current.get(r) == old[r] for r in references), "GC_CHANGED", "参照・保持期限・内容が変わりました。候補を再表示してください")
            selected = [old[r] for r in references]
            result = {"removed": references, "bytes": sum(c["bytes"] for c in selected)}
        elif action == "pin":
            path = self._path(payload["reference"])
            require(path.is_dir() and type(payload["pinned"]) is bool, "INVALID_INPUT", "存在する参照とbooleanが必要です")
            result = {"reference": payload["reference"], "pinned": payload["pinned"]}
        elif action == "policy":
            fields(payload["policy"], DEFAULT_POLICY)
            require(all(type(v) is int and v > 0 for v in payload["policy"].values()), "INVALID_INPUT", "保持設定は正の整数です")
            result = {"policy": payload["policy"]}
        else:
            identifier(payload["templateId"], "templateId")
            require(payload["approvedBy"] == self.registry.inventory["operator"], "APPROVAL_REQUIRED", "運用者の明示承認が必要です")
            from .template_control import TemplateControl
            TemplateControl(self.registry).registered(payload["templateId"])
            result = {"retiredTemplate": payload["templateId"]}
        if dry_run:
            return self._response(result, dryRun=True)
        with self.registry.transaction() as connection:
            connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)", (operation_id, fingerprint, "gc-" + action, "gc", "PENDING" if action == "apply" else "COMPLETED", json.dumps(result)))
            connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)", (self.registry.clock(), operation_id, "gc-" + action))
            if action in {"pin", "policy", "retire-template"}:
                key = {"pin": "gcPins", "policy": "gcPolicy", "retire-template": "gcRetiredTemplates"}[action]
                values = self._meta(connection, key, [])
                if action == "pin":
                    values = sorted((set(values) | {payload["reference"]}) if payload["pinned"] else (set(values) - {payload["reference"]}))
                elif action == "policy":
                    values = payload["policy"]
                else:
                    values = sorted(set(values) | {payload["templateId"]})
                connection.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(values)))
        if action == "apply":
            journal = self.registry.root / "manager/operations" / ("gc-" + operation_id)
            try:
                storage.mkdir(journal)
                storage.write(journal / "requested.json", canonical({"payload": payload, "selected": selected}))
                for candidate in selected:
                    self._remove(candidate, operation_id)
                with self.registry.transaction() as connection:
                    for reference in references:
                        kind, name = reference.split(":", 1)
                        if kind == "snapshot":
                            connection.execute("DELETE FROM snapshots WHERE id=?", (name,))
                        if kind in {"world", "run"}:
                            row = connection.execute("SELECT record FROM runs WHERE id=?", (name,)).fetchone()
                            run = json.loads(row[0])
                            run["gcRemoved"] = sorted(set(run.get("gcRemoved", [])) | {kind})
                            connection.execute("UPDATE runs SET record=? WHERE id=?", (json.dumps(run), name))
                    connection.execute("UPDATE runtime_operations SET status='COMPLETED' WHERE id=?", (operation_id,))
                storage.write(journal / "committed.json", canonical(result))
            except BaseException as error:
                with self.registry.transaction() as connection:
                    connection.execute("UPDATE runtime_operations SET status='RECOVERY_REQUIRED' WHERE id=?", (operation_id,))
                raise RegistryError("GC_FAILED", "GCが途中停止しました。残存物・journalを保全し、強制削除・自動再試行しません") from error
        return self._response(result)

    def _remove(self, candidate, operation):
        path = self.registry.root / candidate["path"]
        require(path.is_absolute() and path.is_relative_to(self.registry.root)
                and path != self.registry.root, "INVALID_INPUT", "回収対象の境界が不正です")
        require(self._tree(path) == candidate["tree"], "GC_CHANGED", "回収直前に内容が変わりました")
        destination = self.registry.root / "quarantine" / ("gc-" + operation + "-" + digest(candidate["reference"].encode()))
        storage.move(path, destination)
        require(self._tree(destination) == candidate["tree"], "GC_CHANGED", "隔離後の内容が一致しません")
        for file in candidate["tree"]["files"]:
            target = destination / file["path"]
            plain_path(target)
            require(digest(regular_bytes(target)) == file["sha256"] and target.stat().st_nlink == 1, "GC_CHANGED", "回収ファイルが変わりました")
            target.unlink()
        for name in sorted(candidate["tree"]["directories"], key=lambda n: (n.count("/"), len(n)), reverse=True):
            target = destination if name == "." else destination / name
            plain_path(target)
            target.rmdir()
        storage.sync_directory(destination.parent)
