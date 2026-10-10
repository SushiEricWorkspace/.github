"""宣言inventoryと貸出の正本を分ける。DB transaction中にOS lockを待たない。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import time
import uuid

from .contracts import absolute_path, validate
from .local_ipc import make_private, plain_path, require_private, supported_os


class RegistryError(RuntimeError):
    """値や秘密を含めず、CLIで分類可能な拒否を返す。"""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise RegistryError(code, message)


def identifier(value, name):
    require(type(value) is str and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", value) is not None,
            "INVALID_INPUT", f"{name}は小文字英数字とハイフンで指定してください")
    return value


def fields(payload, required):
    require(type(payload) is dict and payload.keys() == set(required), "INVALID_INPUT", "必須項目の欠落または未知項目があります")


def ttl(value):
    require(type(value) is int and 0 < value <= 2147483647, "INVALID_INPUT", "ttlSecondsは正の32bit整数で指定してください")
    return value


SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE instances(
 id TEXT PRIMARY KEY, state TEXT NOT NULL, generation TEXT,
 epoch INTEGER NOT NULL DEFAULT 0, lease TEXT, owner TEXT, expires REAL,
 generation_bound INTEGER NOT NULL DEFAULT 0,
 writers INTEGER NOT NULL DEFAULT 0, children INTEGER NOT NULL DEFAULT 0,
 pending INTEGER NOT NULL DEFAULT 0, clean_proof TEXT);
CREATE TABLE ports(port INTEGER PRIMARY KEY, instance TEXT NOT NULL REFERENCES instances(id), service TEXT NOT NULL);
CREATE TABLE operations(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL);
CREATE TABLE reservations(operation TEXT NOT NULL, reference TEXT NOT NULL,
 instance TEXT NOT NULL REFERENCES instances(id), lease TEXT NOT NULL, epoch INTEGER NOT NULL,
 PRIMARY KEY(operation, reference));
CREATE TABLE reference_operations(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, instance TEXT NOT NULL,
 lease TEXT NOT NULL, epoch INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE audit(sequence INTEGER PRIMARY KEY, at REAL NOT NULL, operation TEXT NOT NULL, command TEXT NOT NULL);
"""


def initialize(inventory: dict, *, operation_id: str, dry_run: bool = False) -> dict:
    """明示設定と空の管理rootだけを初期化する。既存runやbackup先へは書き込まない。"""
    supported_os()
    identifier(operation_id, "operationId")
    validate("inventory", inventory)
    flavor = absolute_path(inventory["managementRoot"])
    require(isinstance(flavor, PureWindowsPath) == (os.name == "nt"), "UNSUPPORTED_PATH", "実行OSの絶対パス形式で指定してください")
    root = Path(inventory["managementRoot"])
    if os.name == "nt":
        from .windows_security import require_local_volume
        require_local_volume(root)
    plain_path(root)
    plain_path(Path(inventory["backup"]["destination"]))
    require(root.parent.is_dir(), "INVALID_INPUT", "管理rootの親ディレクトリを明示的に用意してください")
    if (root / "manager" / "INITIALIZED").exists():
        existing = Registry(root)
        require(existing.inventory == inventory, "OPERATION_CONFLICT", "初期化済みrootへ異なるinventoryを指定できません")
        with existing.transaction(read_only=True) as connection:
            recorded = connection.execute("SELECT value FROM meta WHERE key='initOperation'").fetchone()
            require(recorded is not None and recorded[0] == operation_id, "EXISTING_DATA", "別の初期化operationIdで使用されたrootです")
        return {"initialized": True, "replayed": True, "dryRun": dry_run, "instances": [row["instanceId"] for row in inventory["instances"]]}
    require(not root.exists() or (root.is_dir() and not any(root.iterdir())), "EXISTING_DATA", "管理rootが空ではありません。既存データを初期化しません")
    from .local_ipc import endpoint
    endpoint(root)  # Unix socketの長さも、ディレクトリ作成前に検査する。
    if dry_run:
        return {"dryRun": True, "instances": [row["instanceId"] for row in inventory["instances"]]}
    root.mkdir(exist_ok=True)
    make_private(root, directory=True)
    for name in ("manager", "artifacts", "snapshots", "templates", "instances", "temporary", "quarantine"):
        directory = root / name
        directory.mkdir()
        make_private(directory, directory=True)
    for name in ("locks", "operations", "audit"):
        directory = root / "manager" / name
        directory.mkdir()
        make_private(directory, directory=True)
    for name in ["supervisor"] + [f"instance-{row['instanceId']}" for row in inventory["instances"]]:
        lock = root / "manager" / "locks" / f"{name}.lock"
        lock.write_bytes(b"0")
        make_private(lock)
    declaration = root / "manager" / "inventory.json"
    with declaration.open("x", encoding="utf-8") as handle:
        json.dump(inventory, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    make_private(declaration)
    database = root / "manager" / "registry.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(SCHEMA)
        with connection:
            connection.execute("INSERT INTO meta VALUES ('schemaVersion','1')")
            connection.execute("INSERT INTO meta VALUES ('initOperation',?)", (operation_id,))
            digest = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
            connection.execute("INSERT INTO meta VALUES ('inventoryHash',?)", (digest,))
            for instance in inventory["instances"]:
                connection.execute("INSERT INTO instances(id,state) VALUES (?,'AVAILABLE')", (instance["instanceId"],))
                for service, port in instance["ports"].items():
                    connection.execute("INSERT INTO ports VALUES (?,?,?)", (port, instance["instanceId"], service))
    finally:
        connection.close()
    make_private(database)
    marker = root / "manager" / "INITIALIZED"
    marker.write_text("registry-v1\n", encoding="utf-8")
    make_private(marker)
    return {"initialized": True, "instances": [row["instanceId"] for row in inventory["instances"]]}


class Registry:
    """一supervisor所有の貸出DB。期限切れ・異常終了を自動回収しない。"""
    def __init__(self, root: Path, *, clock=time.time):
        self.root = root
        self.clock = clock
        if os.name == "nt":
            from .windows_security import require_local_volume
            require_local_volume(root)
        for directory in (root, root / "manager", root / "manager" / "locks"):
            require_private(directory, directory=True)
        for name in ("inventory.json", "registry.sqlite", "INITIALIZED"):
            require_private(root / "manager" / name)
        require((root / "manager" / "INITIALIZED").read_text(encoding="utf-8") == "registry-v1\n",
                "RECOVERY_REQUIRED", "初期化が完了していません")
        self.inventory = json.loads((root / "manager" / "inventory.json").read_text(encoding="utf-8"))
        validate("inventory", self.inventory)
        require(Path(self.inventory["managementRoot"]) == root, "RECOVERY_REQUIRED", "inventoryの管理rootが一致しません")
        with self.transaction(read_only=True) as connection:
            meta = dict(connection.execute("SELECT key,value FROM meta"))
            digest = hashlib.sha256(json.dumps(self.inventory, sort_keys=True).encode()).hexdigest()
            require(meta.get("schemaVersion") == "1" and meta.get("inventoryHash") == digest,
                    "RECOVERY_REQUIRED", "Registryとinventoryが一致しません")
            actual_ids = {row[0] for row in connection.execute("SELECT id FROM instances")}
            declared_ids = {row["instanceId"] for row in self.inventory["instances"]}
            actual_ports = {tuple(row) for row in connection.execute("SELECT port,instance,service FROM ports")}
            declared_ports = {(port, row["instanceId"], service) for row in self.inventory["instances"] for service, port in row["ports"].items()}
            require(actual_ids == declared_ids and actual_ports == declared_ports,
                    "RECOVERY_REQUIRED", "登録枠またはポートがinventoryと一致しません")

    @contextmanager
    def transaction(self, *, read_only: bool = False):
        """短いDB操作のみ。コピー・IPC・OS lock待機をtransactionへ持ち込まない。"""
        database = self.root / "manager" / "registry.sqlite"
        require_private(database)
        connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            if read_only:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def begin_supervision(self) -> str:
        """前回起動の有無を検査し、再起動時には全枠を復旧待ちにする。"""
        boot = uuid.uuid4().hex
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM meta WHERE key='supervisorBoot'").fetchone():
                connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED'")
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('supervisorBoot',?)", (boot,))
        return boot

    def stop_supervision(self):
        with self.transaction() as connection:
            connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED'")

    def _instance(self, connection, instance_id):
        identifier(instance_id, "instanceId")
        row = connection.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        require(row is not None, "NOT_FOUND", "instanceが登録されていません")
        return row

    def _token(self, row, payload):
        require(type(payload["epoch"]) is int and payload["epoch"] == row["epoch"]
                and type(payload["leaseId"]) is str and payload["leaseId"] == row["lease"]
                and row["lease"] is not None, "STALE_LEASE", "leaseIdまたはepochが古いです")
        require(row["state"] != "RECOVERY_REQUIRED", "RECOVERY_REQUIRED", "復旧照合が必要です")
        require(row["expires"] > self.clock(), "LEASE_EXPIRED", "期限切れは自動回収しません。復旧・引継ぎを確認してください")
        require(payload["expectedGeneration"] == row["generation"], "STALE_GENERATION", "期待世代が一致しません")

    def _safe_release(self, connection, row):
        require(row["state"] == "LEASED_STOPPED", "NOT_STOPPED", "停止状態でないため解放できません")
        require(not any(row[field] for field in ("writers", "children", "pending")), "BUSY", "writer・child・未完了操作が残っています")
        require(not connection.execute("SELECT 1 FROM reservations WHERE instance=?", (row["id"],)).fetchone(),
                "BUSY", "参照予約が残っています")
        # #529/#530/#533が保存証跡と実プロセス照合を提供するまでは、実データ枠を解放しない。
        path = self.root / "instances" / row["id"]
        plain_path(path)
        require(row["generation"] is None and (not path.exists() or (path.is_dir() and not any(path.iterdir()))),
                "PROOF_REQUIRED", "保存・プロセス終了証跡の対応前は実データ枠を解放できません")

    def _cached_valid(self, connection, command, payload, result):
        row = self._instance(connection, payload["instanceId"])
        if command == "acquire":
            self._token(row, {"leaseId": result["leaseId"], "epoch": result["epoch"], "expectedGeneration": result["generation"]})
        elif command == "renew":
            self._token(row, payload)
        else:
            require(row["epoch"] == payload["epoch"] and row["lease"] is None and row["state"] == "AVAILABLE",
                    "STALE_LEASE", "解放済み要求を新しい貸出へ再適用できません")

    def execute(self, command: str, payload: dict, *, operation_id: str, dry_run: bool = False) -> dict:
        """CLI契約を検査し、変更と冪等応答を同じtransactionへ記録する。"""
        mutations = {"acquire", "renew", "release"}
        require(command in mutations | {"list", "status", "doctor"}, "NOT_IMPLEMENTED", "この操作は未実装です")
        identifier(operation_id, "operationId")
        required = {"acquire": {"instanceId", "owner", "ttlSeconds"},
                    "renew": {"instanceId", "leaseId", "epoch", "expectedGeneration", "ttlSeconds"},
                    "release": {"instanceId", "leaseId", "epoch", "expectedGeneration"},
                    "list": set(), "status": {"instanceId"}, "doctor": set()}
        fields(payload, required[command])
        fingerprint = hashlib.sha256(json.dumps({"command": command, "payload": payload}, sort_keys=True).encode()).hexdigest()
        with self.transaction(read_only=command not in mutations or dry_run) as connection:
            if command in {"list", "status", "doctor"}:
                if command == "status":
                    rows = [self._instance(connection, payload["instanceId"])]
                else:
                    rows = connection.execute("SELECT * FROM instances ORDER BY id").fetchall()
                result = [{"instanceId": row["id"], "state": row["state"], "generation": row["generation"],
                           "epoch": row["epoch"], "owner": row["owner"], "leaseExpired": bool(row["lease"] and row["expires"] <= self.clock()),
                           "generationBound": bool(row["generation_bound"])} for row in rows]
                return {"instances": result, "supervisor": "reachable", "ports": "declaration-only-not-bound",
                        "unimplemented": ["prepare", "start", "stop", "snapshot", "restore", "recover", "gc"]}
            cached = connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            require(not connection.execute("SELECT 1 FROM reference_operations WHERE id=?", (operation_id,)).fetchone(),
                    "OPERATION_CONFLICT", "同じoperationIdが参照予約で使用されています")
            if cached:
                require(cached["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "同じoperationIdに異なるpayloadを指定できません")
                result = json.loads(cached["response"])
                self._cached_valid(connection, command, payload, result)
                return dict(result, replayed=True, dryRun=dry_run)
            row = self._instance(connection, payload["instanceId"])
            if command == "acquire":
                identifier(payload["owner"], "owner")
                lifetime = ttl(payload["ttlSeconds"])
                require(row["state"] == "AVAILABLE" and row["lease"] is None, "UNAVAILABLE", "枠は貸出中または復旧待ちです。期限切れでも自動回収しません")
                result = {"instanceId": row["id"], "leaseId": uuid.uuid4().hex, "epoch": row["epoch"] + 1,
                          "generation": row["generation"], "expiresAt": self.clock() + lifetime, "generationBound": False}
                if not dry_run:
                    connection.execute("UPDATE instances SET state='LEASED_STOPPED',lease=?,owner=?,epoch=?,expires=?,generation_bound=0 WHERE id=?",
                                       (result["leaseId"], payload["owner"], result["epoch"], result["expiresAt"], row["id"]))
            else:
                self._token(row, payload)
                if command == "renew":
                    result = {"instanceId": row["id"], "expiresAt": self.clock() + ttl(payload["ttlSeconds"])}
                    if not dry_run:
                        connection.execute("UPDATE instances SET expires=? WHERE id=?", (result["expiresAt"], row["id"]))
                else:
                    self._safe_release(connection, row)
                    result = {"instanceId": row["id"], "released": True, "epoch": row["epoch"]}
                    if not dry_run:
                        connection.execute("UPDATE instances SET state='AVAILABLE',lease=NULL,owner=NULL,expires=NULL,generation_bound=0 WHERE id=?", (row["id"],))
            if dry_run:
                # プレビューleaseは実際のtokenに見える値を返さない。
                return {"dryRun": True, "wouldExecute": command, "instanceId": row["id"]}
            connection.execute("INSERT INTO operations VALUES (?,?,?)", (operation_id, fingerprint, json.dumps(result)))
            connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)", (self.clock(), operation_id, command))
            return result

    def reserve_references(self, payload: dict, operation_id: str, references: list[str]) -> None:
        """後続の長時間操作が参照を保護する入口。取得後にDB外で処理する。"""
        identifier(operation_id, "operationId")
        fields(payload, {"instanceId", "leaseId", "epoch", "expectedGeneration"})
        require(bool(references) and all(type(ref) is str and re.fullmatch(r"[a-z0-9-]+:[a-z0-9-]+", ref) for ref in references),
                "INVALID_INPUT", "参照はkind:idの形式で指定してください")
        with self.transaction() as connection:
            row = self._instance(connection, payload["instanceId"])
            self._token(row, payload)
            expected = set(references)
            fingerprint = hashlib.sha256(json.dumps({"payload": payload, "references": sorted(expected)}, sort_keys=True).encode()).hexdigest()
            require(not connection.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone(),
                    "OPERATION_CONFLICT", "同じoperationIdが別の変更で使用されています")
            recorded = connection.execute("SELECT * FROM reference_operations WHERE id=?", (operation_id,)).fetchone()
            if recorded:
                require(recorded["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "参照予約のoperationIdが別の入力で使用されています")
                if recorded["completed"]:
                    return
            previous = connection.execute("SELECT * FROM reservations WHERE operation=?", (operation_id,)).fetchall()
            if previous:
                require({row["reference"] for row in previous} == expected and all(
                    item["lease"] == payload["leaseId"] and item["epoch"] == payload["epoch"] and item["instance"] == payload["instanceId"] for item in previous),
                    "OPERATION_CONFLICT", "参照予約のoperationIdが別の入力で使用されています")
                return
            if recorded is None:
                connection.execute("INSERT INTO reference_operations(id,fingerprint,instance,lease,epoch) VALUES (?,?,?,?,?)",
                                   (operation_id, fingerprint, row["id"], row["lease"], row["epoch"]))
            for reference in sorted(expected):
                connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)", (operation_id, reference, row["id"], row["lease"], row["epoch"]))

    def finish_reservation(self, payload: dict, operation_id: str) -> None:
        """同じlease/epochだけが、自身の完了した参照予約を解除できる。"""
        identifier(operation_id, "operationId")
        fields(payload, {"instanceId", "leaseId", "epoch", "expectedGeneration"})
        with self.transaction() as connection:
            row = self._instance(connection, payload["instanceId"])
            self._token(row, payload)
            operation = connection.execute("SELECT * FROM reference_operations WHERE id=?", (operation_id,)).fetchone()
            require(operation is not None and operation["instance"] == row["id"] and operation["lease"] == row["lease"] and operation["epoch"] == row["epoch"],
                    "STALE_LEASE", "参照予約の貸出識別が一致しません")
            rows = connection.execute("SELECT * FROM reservations WHERE operation=?", (operation_id,)).fetchall()
            require(all(item["lease"] == row["lease"] and item["epoch"] == row["epoch"] and item["instance"] == row["id"] for item in rows),
                    "STALE_LEASE", "参照予約の貸出識別が一致しません")
            connection.execute("DELETE FROM reservations WHERE operation=?", (operation_id,))
            connection.execute("UPDATE reference_operations SET completed=1 WHERE id=?", (operation_id,))
