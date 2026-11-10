"""固定bundleの起動/停止。世代構築はGenerationControlで行う。"""
from contextlib import contextmanager, nullcontext
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import time
import threading
import uuid

from .build_inputs import canonical, digest
from .contracts import LAYOUT_ID
from .local_ipc import decode, make_private, plain_path, require_private
from .registry import RegistryError, fields, identifier, require, ttl
from .runtime_bundle import launch_command, verify_bundle, require_data_generation
from .runtime_ports import instance_endpoints, reserve, prebind, require_listeners, socket_keys
from . import runtime_process, runtime_rcon
from .mod_control_startup import _admission, _change, _Startup

TOKEN = {"instanceId", "leaseId", "epoch", "expectedGeneration"}


def write_json(path: Path, value: dict) -> None:
    """新規記録だけを作り、既存証跡や世代を上書きしない。"""
    plain_path(path)
    with path.open("xb") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    make_private(path)


def read_json(path: Path) -> dict:
    plain_path(path)
    require(path.is_file(), "PROOF_REQUIRED", "必要な管理記録がありません")
    return decode(path.read_bytes())


class RuntimeControl:
    """instance lock→短いtransactionで予約・確定し、外部I/Oは両方の外で行う。"""
    def __init__(self, registry, *, process_adapter=runtime_process):
        self.registry = registry
        self.process_adapter = process_adapter
        self.processes = {}
        self.game_processes = {}
        self.game_exits = {}
        self.managed = None
        self._control = None
        self._offline = None
        self._control_lock = threading.Lock()
        self._lock_deadline = threading.local()

    def _offline_open(self, token, *, operation_id, revision, ready, deadline):
        """P内部専用。停止証拠と同instance予約へ束縛してCを一度だけ生成する。"""
        from .offline_control import _OfflineOpen
        with self._control_lock:
            if self._offline is None:
                self._offline = _OfflineOpen(self)
        return self._offline.open(token, operation_id=operation_id, revision=revision, ready=ready, deadline=deadline)

    def _release_game_handles(self, token, *, operation_id, stop_operation_id, deadline):
        """P内部専用。確定stopの元M/観測Cを一度だけ解放し、失敗時は進捗と予約を保持する。

        公共の自己予約stopは確定後に同deadlineで呼ぶ。部分失敗の進捗と予約を保持する。
        """
        from .offline_control import _OfflineOpen
        with self._control_lock:
            if self._offline is None:
                self._offline = _OfflineOpen(self)
        return self._offline.release_game_handles(token, operation_id=operation_id,
                                                 stop_operation_id=stop_operation_id, deadline=deadline)

    def _offline_complete(self, token, *, operation_id, deadline):
        """元W/全child終了の正本証拠からC本人確定を記録する。writer終了とは別。"""
        require(self._offline is not None, "WRITER_PENDING", "保持したoffline取得が必要です")
        return self._offline.complete(token, operation_id=operation_id, deadline=deadline)

    def _offline_observe_worker_exit(self, token, *, operation_id, deadline):
        """保持W/全childの正常終了だけを記録する。候補実体確定・writer終了は後続。"""
        require(self._offline is not None, "WRITER_PENDING", "保持したoffline取得が必要です")
        return self._offline.observe_worker_exit(token, operation_id=operation_id, deadline=deadline)

    def _offline_admit(self, token, *, operation_id, deadline):
        """記録済みAssignedから候補packetまで。編集完了やwriter終了は未確定。"""
        require(self._offline is not None, "WRITER_PENDING", "保持したoffline取得が必要です")
        return self._offline.admit(token, operation_id=operation_id, deadline=deadline)

    def _offline_assign(self, token, *, operation_id, deadline):
        """正本記録済みWを保持C本人へAssigned登録する。候補接続は_offline_admit、完了は後続。"""
        require(self._offline is not None, "WRITER_PENDING", "保持したoffline取得が必要です")
        return self._offline.assign(token, operation_id=operation_id, deadline=deadline)

    def _offline_prepare(self, token, *, operation_id, request, deadline):
        """保持Cの正本予約から単発WをPREPAREDまで進める。編集・終了は未確定。"""
        require(self._offline is not None, "WRITER_PENDING", "保持したoffline取得が必要です")
        return self._offline.prepare(token, operation_id=operation_id, request=request, deadline=deadline)

    def _managed_startup(self, token, *, transport, **inputs):
        """独立P入力のprivate受付。通常start/実承認供給元には未接続。"""
        with self._control_lock:
            if self._control is None:
                self._control = _Startup(self, transport)
            require(self._control.transport is transport, "CONTROL_BUSY", "保持transportを置換しません")
        return self._control.start(token, **inputs)

    def _managed_readback(self, token, *, deadline):
        """既認証sessionの同BOUND原文へ保存ACKだけを返す。起動完成・再OPENは行わない。"""
        require(self._control is not None, "CONTROL_PENDING", "保持した認証sessionがありません")
        return self._control.readback(token, deadline=deadline)

    def _reserve_managed_stop(self, payload, *, operation_id, revision):
        """専用runを短txで閉門/停止予約する。drain・保存・実停止を代行しない。"""
        identifier(operation_id, "operationId")
        fields(payload, TOKEN | {"timeoutSeconds"})
        ttl(payload["timeoutSeconds"])
        fingerprint = digest(canonical({"command": "stop", "payload": payload}))
        with self._transaction(payload["instanceId"]) as connection:
            row = dict(self.registry._instance(connection, payload["instanceId"]))
            self.registry._token(row, payload, allow_expired=True)
            gate = _admission(connection, row)
            require(gate["descriptor_hash"] is not None and gate["gate"] == "OPEN"
                    and row["state"] == "RUNNING" and not row["pending"],
                    "CONTROL_BLOCKED", "同じ専用runのOPEN受付だけを停止予約します")
            for table in ("operations", "runtime_operations", "reference_operations", "writer_operations", "snapshot_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "停止要求は自動再実行しません")
            next_revision = _change(connection, row, revision, "BLOCKED")
            response = {"instanceId": row["id"], "runId": row["run_id"], "pending": True}
            connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                (operation_id, fingerprint, "stop", row["id"], "PENDING", json.dumps(response)))
            connection.execute("UPDATE instances SET state='STOPPING',pending=1,runtime_proof=NULL,clean_proof=NULL WHERE id=?", (row["id"],))
            connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)",
                (operation_id, "artifact:" + row["artifact"], row["id"], row["lease"], row["epoch"]))
        return next_revision

    def _paths(self, instance):
        root = self.registry.root / "instances" / instance
        plain_path(root)
        return root

    def _active(self, row):
        generation = self._active_generation(row)
        for path in generation.rglob("*"):
            plain_path(path)
        return generation

    def _active_generation(self, row):
        """常用照合は世代の正本とpath chainに限定し、無関係な保存treeを列挙しない。"""
        root = self._paths(row["id"])
        require_private(root / "active.json")
        active = read_json(root / "active.json")
        require(active == {"version": 1, "generationId": row["generation"], "artifactId": row["artifact"]},
                "RECOVERY_REQUIRED", "activeとRegistryが一致しません")
        generation = root / "generations" / row["generation"]
        require_private(generation, directory=True)
        require_private(generation / "generation.json")
        record = read_json(generation / "generation.json")
        require(record["version"] == 1 and record["layoutId"] == LAYOUT_ID
                and record["generationId"] == row["generation"] and record["artifactId"] == row["artifact"],
                "RECOVERY_REQUIRED", "世代の完成記録が一致しません")
        self.registry.generation.check_active(row, record)
        staging = root / "staging"
        require(staging.is_dir(), "RECOVERY_REQUIRED", "世代作業領域が通常directoryではありません")
        require_private(staging, directory=True)
        return generation

    def _row(self, payload, *, allow_expired=False):
        with self.registry.transaction(read_only=True) as connection:
            row = dict(self.registry._instance(connection, payload["instanceId"]))
            self.registry._token(row, payload, allow_expired=allow_expired)
            return row

    def _space(self, required_bytes=0):
        used_bytes = 0
        plain_path(self.registry.root)
        roots = [self.registry.root]
        area = self.registry.root.with_name(self.registry.root.name + "-gametest-private")
        try:
            area.lstat()
        except FileNotFoundError:
            pass
        else:
            roots.append(self.registry.gametest._area())
        directories = roots.copy()
        while directories:
            directory = directories.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    info = path.lstat()
                    if os.name != "nt" and stat.S_ISSOCK(info.st_mode) and path.name == "mod-control.sock" and self.managed is not None:
                        if self.managed.owns_socket(path, info):
                            continue
                    if os.name != "nt" and path == self.registry.root / "manager/supervisor.sock":
                        require_private(directory, directory=True)
                        require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                                and stat.S_IMODE(info.st_mode) == 0o600,
                                "IPC_UNVERIFIED", "管理socketの実体・所有権が一致しません")
                        continue
                    if getattr(info, "st_file_attributes", 0) & 0x400 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        raise OSError("管理パスは通常ファイル・ディレクトリに限定します")
                    if stat.S_ISDIR(info.st_mode):
                        directories.append(path)
                    else:
                        used_bytes += info.st_size
        margin = self.registry.inventory["reserveBytes"]
        require(used_bytes + required_bytes + margin <= self.registry.inventory["quotaBytes"]
                and all(shutil.disk_usage(root).free >= required_bytes + margin for root in roots),
                "NO_SPACE", "runtime配備と起動余裕を確保できません。自動削除しません")

    def copy_space(self, size):
        """コピー中の物理空き減少を各書込前に検査する。削除で空きを作らない。"""
        require(shutil.disk_usage(self.registry.root).free >= size + self.registry.inventory["reserveBytes"],
                "NO_SPACE", "コピー中に物理空き容量が減少しました。旧データを保持します")

    @contextmanager
    def _lock_wait(self, deadline):
        previous = getattr(self._lock_deadline, "value", None)
        self._lock_deadline.value = deadline if previous is None else min(previous, deadline)
        try:
            yield
        finally:
            self._lock_deadline.value = previous

    @contextmanager
    def _transaction(self, instance, *, read_only=False):
        from .build_lock import BuildLockError
        from .supervisor import private_lock
        deadline = getattr(self._lock_deadline, "value", None)
        while True:
            lock = private_lock(self.registry.root / "manager/locks" / f"instance-{instance}.lock")
            try:
                lock.__enter__()
                break
            except BuildLockError as error:
                if deadline is None or not isinstance(error.__cause__, OSError) \
                        or error.__cause__.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                require(remaining > 0, "CONTROL_LOCK", "instance lockの待機期限を超過しました")
                time.sleep(min(0.01, remaining))
        try:
            with self.registry.transaction(read_only=read_only) as connection:
                yield connection
        finally:
            lock.__exit__(None, None, None)

    @staticmethod
    def _binding(current, previous, *, run_id=None):
        require(all(current[key] == previous[key] for key in ("id", "lease", "epoch", "generation", "artifact"))
                and current["run_id"] == (previous["run_id"] if run_id is None else run_id),
                "STALE_OPERATION", "予約したlease・世代・runが変更されました")

    def _record(self, run, *, expected=None):
        record = json.dumps(run)
        transaction = (self.registry.transaction() if run.get("kind") == "gametest"
                       else self._transaction(run["instanceId"]))
        with transaction as connection:
            if run.get("kind") != "gametest":
                current = self.registry._instance(connection, run["instanceId"])
                require(current["generation"] == run["generationId"] and current["artifact"] == run["artifactId"]
                        and current["pending"] and (expected is None or current["run_id"] == run["runId"]),
                        "STALE_OPERATION", "runを更新する世代・予約が変更されました")
            if expected is None:
                require(not connection.execute("SELECT 1 FROM runs WHERE id=?", (run["runId"],)).fetchone(),
                        "STALE_OPERATION", "既存runを置換しません")
                connection.execute("INSERT INTO runs VALUES (?,?,?,?)",
                                   (run["runId"], run["instanceId"], run["generationId"], record))
                if run.get("kind") != "gametest":
                    operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (run["startOperationId"],)).fetchone()
                    require(operation is not None and operation["instance"] == current["id"]
                            and operation["command"] == "start" and operation["status"] == "PENDING"
                            and operation["fingerprint"] == run["startRequestHash"],
                            "STALE_OPERATION", "Pの元起動予約が一致しません")
                    connection.execute("INSERT INTO run_admission VALUES (?,?,?,?,?,?,'CLOSED',0,NULL,?)",
                        (run["runId"], current["id"], current["lease"], current["epoch"], current["generation"], run["nonce"], run["startOperationId"]))
            else:
                changed = connection.execute("UPDATE runs SET record=? WHERE id=? AND instance=? AND generation=? AND record=?",
                                             (record, run["runId"], run["instanceId"], run["generationId"], expected))
                require(changed.rowcount == 1, "STALE_OPERATION", "runの読取元が変更されました")
        return record

    def _load_run(self, row, *, with_record=False):
        with self.registry.transaction(read_only=True) as connection:
            value = connection.execute("SELECT record FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            require(value is not None, "PROOF_REQUIRED", "起動記録がありません")
            return (json.loads(value[0]), value[0]) if with_record else json.loads(value[0])

    def execute(self, command, payload, *, operation_id, dry_run=False):
        identifier(operation_id, "operationId")
        require(command in {"start", "stop"}, "INVALID_INPUT", "runtime制御はstart/stopだけです")
        fields(payload, TOKEN | {"timeoutSeconds"})
        identifier(payload["instanceId"], "instanceId")
        ttl(payload["timeoutSeconds"])
        deadline = time.monotonic() + payload["timeoutSeconds"] if command == "stop" else None
        wait = self._lock_wait(deadline) if command == "stop" and self.managed is not None else nullcontext()
        with wait:
            if not dry_run:
                self.refresh()
            fingerprint = digest(canonical({"command": command, "payload": payload}))
            replay = None
            with self._transaction(payload["instanceId"], read_only=True) as connection:
                for table in ("operations", "reference_operations", "writer_operations", "snapshot_operations"):
                    require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                            "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
                cached = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
                if cached:
                    require(cached["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "operationIdに異なる要求を指定できません")
                    row = self.registry._instance(connection, payload["instanceId"])
                    self.registry._token(row, payload, allow_expired=command == "stop")
                    require(cached["status"] == "COMPLETED", "RECOVERY_REQUIRED", "未確定の操作は自動再実行しません")
                    response = json.loads(cached["response"])
                    require(row["run_id"] == response["runId"] and row["state"] == ("RUNNING" if command == "start" else "LEASED_STOPPED"),
                            "STALE_OPERATION", "別runまたは停止後へ古い結果を適用できません")
                    replay = (dict(row), response)
            if replay:
                row, response = replay
                if command == "start":
                    self._active(row)
                    self._running(row)
                with self._transaction(row["id"], read_only=True) as connection:
                    current = self.registry._instance(connection, row["id"])
                    self.registry._token(current, payload, allow_expired=command == "stop")
                    self._binding(current, row)
                    require(current["state"] == row["state"] and not current["pending"], "STALE_OPERATION", "再提示中にrunが変更されました")
                if command == "stop" and not dry_run:
                    self._stop_handle_release(payload, response, deadline, replay=True)
                return dict(response, replayed=True, dryRun=dry_run)
            row = self._row(payload, allow_expired=command == "stop")
            joining_stop = command == "stop" and self.managed is not None and row["state"] == "STOPPING"
            require(row["generation_bound"] and row["generation"] is not None and (not row["pending"] or joining_stop),
                    "INVALID_STATE", "このleaseへ完成generationが結び付いていません")
            generation = self._active(row)
            if command == "start":
                require(row["state"] in ("READY", "LEASED_STOPPED") and not row["writers"] and not row["children"],
                        "NOT_STOPPED", "停止・writer/child不在を確認できません")
                if row["run_id"]:
                    previous = self._load_run(row)
                    require(previous.get("jvmAndChildrenExited") and previous.get("runtimeSaveCompleted"),
                            "PROOF_REQUIRED", "前runの正常停止・保存終了を確認できません")
                    require(self.process_adapter.identity(previous["identity"]["pid"]) is None,
                            "PROCESS_MISMATCH", "前runのPIDが存在します。PID再利用も自動許可しません")
                manifest = verify_bundle(generation / "runtime")
                require_data_generation(manifest, row["generation"])
                require(self.managed is not None, "PROFILE_REQUIRED", "通常起動の独立承認入力がありません")
                self.managed.approved(row, generation)
                self._space()
            else:
                require(row["state"] in ({"RUNNING", "STOPPING"} if self.managed is not None else {"RUNNING"}) and row["run_id"] in self.processes,
                        "NOT_RUNNING", "監督が所有するRUNNING runだけを停止します")
            endpoints = instance_endpoints(next(item for item in self.registry.inventory["instances"] if item["instanceId"] == row["id"]))
            if dry_run:
                if command == "stop":
                    self._running(row)
                return {"dryRun": True, "wouldExecute": command, "instanceId": row["id"]}
            session = self.managed.sessions.get(row["id"]) if self.managed is not None else None
            if joining_stop and session is not None and session.get("direct_stop"):
                return self._join_direct_stop(row, payload, operation_id, fingerprint, session, deadline)
            join_direct = False
            with self._transaction(row["id"]) as connection:
                for table in ("operations", "reference_operations", "writer_operations", "snapshot_operations"):
                    require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                            "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
                current = self.registry._instance(connection, row["id"])
                self.registry._token(current, payload, allow_expired=command == "stop")
                self._binding(current, row)
                joining_current = command == "stop" and self.managed is not None and current["state"] == "STOPPING" and current["pending"]
                joining_current = joining_current or (command == "stop" and session is not None and session.get("direct_stop")
                    and current["run_id"] == session["run"] and current["state"] == "LEASED_STOPPED" and not current["pending"])
                require((current["state"] == row["state"] or joining_current) and (not current["pending"] or joining_current),
                        "BUSY", "枠の状態が変更されました")
                pending_writers = connection.execute("SELECT id FROM writer_sessions WHERE instance=? AND pending!=0", (row["id"],)).fetchall()
                calls = connection.execute("SELECT w.session FROM reservations r LEFT JOIN writer_operations w ON w.id=r.operation WHERE r.instance=? AND r.reference LIKE 'writer-call:%'", (row["id"],)).fetchall()
                drainable = self.managed is not None and command == "stop" and all(
                    value[0] in self.registry.writer.database_proxies for value in [*pending_writers, *calls])
                require(drainable or not pending_writers,
                        "WRITER_PENDING", "writer保存の完了が不明なため停止・再起動を開始できません")
                require(drainable or not calls, "WRITER_PENDING", "writer callが実行中です")
                if command == "stop":
                    gate = _admission(connection, current)
                    parent = connection.execute("SELECT id FROM runtime_operations WHERE instance=? AND command='stop' AND status!='COMPLETED'", (row["id"],)).fetchone()
                    if session is not None and session.get("direct_stop"):
                        parent = connection.execute("SELECT id FROM runtime_operations WHERE id=? AND instance=? AND command='stop'", (session["stop_operation"], row["id"])).fetchone()
                    join_direct = bool(parent and session is not None and session.get("direct_stop")
                                       and parent[0] == session["stop_operation"] and current["run_id"] == session["run"])
                    require(parent is None or join_direct, "CONTROL_PENDING", "元stop要求を保持します")
                    if not join_direct and gate["gate"] != "BLOCKED":
                        _change(connection, current, gate["revision"], "BLOCKED")
                if not join_direct:
                    response = {"instanceId": row["id"], "pending": True}
                    connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                                       (operation_id, fingerprint, command, row["id"], "PENDING", json.dumps(response)))
                    connection.execute("UPDATE instances SET state=?,pending=1,runtime_proof=NULL,clean_proof=NULL WHERE id=?",
                                       ({"start": "STARTING", "stop": "STOPPING"}[command], row["id"]))
                    connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)", (self.registry.clock(), operation_id, command))
                    connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)",
                                       (operation_id, "artifact:" + row["artifact"],
                                         row["id"], row["lease"], row["epoch"]))
            if join_direct:
                return self._join_direct_stop(row, payload, operation_id, fingerprint, session, deadline)
            committed = False
            try:
                if command == "start":
                    result = self._start(row, payload, generation, endpoints, operation_id)
                else:
                    result = self._stop(row, payload, generation, deadline)
                with self._transaction(row["id"]) as connection:
                    current = self.registry._instance(connection, row["id"])
                    self.registry._token(current, payload, allow_expired=command == "stop")
                    operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
                    self._binding(current, row, run_id=result["runId"])
                    require(current["pending"] and current["state"] == {"start": "STARTING", "stop": "STOPPING"}[command]
                            and operation is not None and operation["fingerprint"] == fingerprint
                            and operation["instance"] == row["id"] and operation["command"] == command
                            and operation["status"] == "PENDING", "STALE_OPERATION", "同じ未完了操作ではありません")
                    if command == "start":
                        gate = _admission(connection, current)
                        if self.managed is not None:
                            self.managed.ready(current, connection)
                        else:
                            require(gate["descriptor_hash"] is None and gate["gate"] == "CLOSED",
                                    "CONTROL_BLOCKED", "専用startupの受付を通常proofで開けません")
                        _change(connection, current, gate["revision"], "OPEN")
                        connection.execute("UPDATE instances SET state='RUNNING',pending=0 WHERE id=?", (row["id"],))
                    else:
                        if session is not None and "control_operation" in session:
                            deadline = min(deadline, session["stop_deadline"])
                            evidence, pending = self.managed.save_evidence(connection, row["id"], session, deadline)
                            require(evidence == result["managedSave"] == pending.get("managedSave"),
                                    "STALE_OPERATION", "確定前に元保存原文の記録が変更されました")
                            if "game_exit" in session:
                                require(self.managed.game_exit_evidence(row["id"], session, deadline) == result.get("gameExit"),
                                        "STALE_OPERATION", "確定前にgame退出証拠が変更されました")
                            require(time.monotonic() < deadline, "STOP_TIMEOUT", "stop結果の最終照合後に期限を超過しました")
                        connection.execute("UPDATE instances SET state='LEASED_STOPPED',pending=0,runtime_proof=? WHERE id=?",
                                           (json.dumps(result), row["id"]))
                    connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation_id))
                    connection.execute("DELETE FROM reservations WHERE operation=?", (operation_id,))
                    if command == "stop":
                        connection.execute("DELETE FROM reservations WHERE reference=?", ("stop:" + operation_id,))
                committed = True
                if command == "stop" and self.managed is not None:
                    try:
                        if "managedSave" in result:
                            require(time.monotonic() < deadline, "STOP_TIMEOUT", "stop結果記録後に期限を超過しました")
                    finally:
                        self.managed.release(row["id"], result["runId"])
                if command == "stop" and "managedSave" in result:
                    self._stop_handle_release(payload, result, deadline)
                    require(time.monotonic() < deadline, "STOP_TIMEOUT", "stop返却前に期限を超過しました")
                return result
            except BaseException as error:
                if committed and command == "stop" and "managedSave" in result:
                    raise
                code = error.code if isinstance(error, RegistryError) else "RUNTIME_FAILED"
                with self._transaction(row["id"]) as connection:
                    current = self.registry._instance(connection, row["id"])
                    operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
                    self._binding(current, row, run_id=row.get("reserved_run_id", row["run_id"]))
                    require(current["pending"] and operation is not None and operation["status"] in {"PENDING", "RECOVERY_REQUIRED"}
                            and operation["fingerprint"] == fingerprint and operation["instance"] == row["id"],
                            "STALE_OPERATION", "変更済みの操作を失敗へ上書きしません")
                    connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=?", (row["id"],))
                    gate = connection.execute("SELECT * FROM run_admission WHERE run=?", (current["run_id"],)).fetchone()
                    if gate is not None and gate["gate"] != "BLOCKED":
                        _change(connection, current, gate["revision"], "BLOCKED")
                    failure = {"error": code}
                    pending = json.loads(operation["response"])
                    if command == "stop" and "managedSave" in pending:
                        failure = dict(pending, error=code)
                    connection.execute("UPDATE runtime_operations SET status='RECOVERY_REQUIRED',response=? WHERE id=?",
                                       (json.dumps(failure), operation_id))
                if isinstance(error, RegistryError):
                    raise
                raise RegistryError(code, "runtime操作に失敗しました。保全し、自動kill/再送しません") from None

    def _proof(self, run, generation, *, stopped=False, stopping=False):
        proof = read_json(Path(run["proofPath"]))
        require(proof.get("saveScope", "run") == "run", "PROOF_MISMATCH", "GameTestの終了証跡を永続データの正常保存へ流用できません")
        require(proof.get("schemaVersion") == 1 and proof.get("instanceId") == run["instanceId"]
                and proof.get("generationId") == run["generationId"] and proof.get("startNonce") == run["nonce"],
                "PROOF_MISMATCH", "Mod証跡のrun識別が一致しません")
        require(proof.get("requiresSupervisorProcessExit") is True and proof.get("requiresExternalWriterDrain") is True,
                "PROOF_MISMATCH", "Mod証跡の責務境界が一致しません")
        identity = proof["identity"]
        plain_path(Path(identity["cwd"]))
        plain_path(Path(identity["runtimeManifest"]))
        require(identity["pid"] == run["identity"]["pid"]
                and runtime_process.proof_start_millis(identity["processStartedAt"]) == run["identity"]["startMillis"]
                and Path(identity["cwd"]).resolve() == generation.resolve()
                and identity["artifactId"] == run["artifactId"]
                and identity["runtimeManifestSha256"] == run["manifestHash"]
                and Path(identity["runtimeManifest"]).resolve() == (generation / "runtime/runtime.json").resolve(),
                "PROOF_MISMATCH", "Mod証跡のプロセス・runtime識別が一致しません")
        from .snapshot_data import properties
        world = properties((generation / "server.properties").read_bytes())["level-name"]
        expected_roots = {"world": generation / world, "config": generation / "config",
                          "modConfig": generation / "config/SushiEricServerMod"}
        roots = proof["startup"]["roots"]
        require(roots.keys() == expected_roots.keys(), "ROOT_MISMATCH", "保存root集合が一致しません")
        for key, path in expected_roots.items():
            plain_path(Path(roots[key]))
            require(Path(roots[key]).resolve() == path.resolve(), "ROOT_MISMATCH", "実保存rootが一致しません")
        startup = proof["startup"]
        require(startup["ready"] is True and startup["managementEnabled"] is True
                and startup["managementStarted"] is True and startup["unknownWriters"] == [],
                "START_REJECTED", "Modの起動・保存構成が拒否されました")
        if stopped:
            saved = proof["saves"]
            require(proof["phase"] == "STOPPED" and proof["modSaveCompleted"] is True
                    and proof["vanillaShutdownTailReached"] is True and saved["failures"] == []
                    and saved["pendingWriters"] == 0 and saved["drained"] is True and saved["success"] is True
                    and all(saved["results"].get(key) is True for key in
                            ("management-drain", "money-history", "shop-history", "shop-stocks", "player-data")),
                    "SAVE_FAILED", "Mod/バニラ保存またはwriter drainが完了していません")
        else:
            require(proof["phase"] in ({"STARTED", "STOPPING"} if stopping else {"STARTED"}),
                    "PROOF_MISMATCH", "起動完了phaseが一致しません")
        return proof

    def _start(self, row, payload, generation, endpoints, operation_id):
        nonce = uuid.uuid4().hex
        run_id = "run-" + nonce
        diagnostics = self._paths(row["id"]) / "diagnostics" / run_id
        diagnostics.mkdir(parents=True)
        make_private(diagnostics, directory=True)
        run = {"runId": run_id, "instanceId": row["id"], "generationId": row["generation"],
               "startOperationId": operation_id, "startRequestHash": digest(canonical({"command": "start", "payload": payload})),
               "artifactId": row["artifact"], "nonce": nonce, "identity": None,
               "proofPath": str(diagnostics / "proof.json"), "cwd": str(generation.resolve()),
               "manifestHash": digest((generation / "runtime/runtime.json").read_bytes()), "endpoints": endpoints,
               "stopRequested": False, "jvmAndChildrenExited": False, "runtimeSaveCompleted": False}
        expected = self._record(run)
        with self._transaction(row["id"]) as connection:
            current = self.registry._instance(connection, row["id"])
            self._binding(current, row)
            operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
            require(current["pending"] and current["state"] == "STARTING" and operation is not None
                    and operation["status"] == "PENDING" and operation["instance"] == row["id"]
                    and operation["command"] == "start"
                    and operation["fingerprint"] == digest(canonical({"command": "start", "payload": payload})),
                    "STALE_OPERATION", "起動予約が変更されました")
            reserve(connection, row["id"], run_id, endpoints)
            connection.execute("UPDATE instances SET run_id=? WHERE id=?", (run_id, row["id"]))
        row["reserved_run_id"] = run_id
        deadline = time.monotonic() + payload["timeoutSeconds"]
        session = self.managed.prepare(row, run, generation)
        properties = {"instanceId": row["id"], "generationId": row["generation"], "startNonce": nonce,
                      "proofPath": run["proofPath"], "runtimeManifest": str(generation / "runtime/runtime.json")}
        properties.update({key: session["launch"][key] for key in ("controlDescriptor", "controlDescriptorSha256")})
        require(not any(os.environ.get(key) for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH")),
                "UNREPRODUCIBLE_ENVIRONMENT", "暗黙のJVM環境入力を使用できません")
        arguments = launch_command(generation / "runtime", "server", generation,
                                   jvm_properties=properties, managed_context=session["launch"])
        run["commandHash"] = runtime_process.command_hash(arguments)
        expected = self._record(run, expected=expected)

        def created():
            nonlocal expected
            run["creation"] = {"pid": process.pid, "stage": process.stage, "ownerGroup": process.group}
            write_json(diagnostics / "creation.json", run["creation"])
            expected = self._record(run, expected=expected)
            identity = self.process_adapter.identity(process.pid)
            run["identity"] = identity
            expected = self._record(run, expected=expected)
            write_json(diagnostics / "process.json", {"runId": run_id, "nonce": nonce, "identity": identity})
            require(identity is not None and Path(identity["cwd"]).resolve() == generation.resolve()
                    and Path(identity["executable"]).resolve() == Path(arguments[0]).resolve()
                    and identity["commandHash"] == run["commandHash"], "PROCESS_MISMATCH", "作成したプロセス識別が一致しません")

        # probesを閉じる瞬間のraceは、後段の実PID/socket・nonce照合で検出する。
        with prebind(endpoints):
            pass
        with (diagnostics / "console.log").open("xb") as log:
            make_private(diagnostics / "console.log")
            process = self.process_adapter.OwnedProcess()
            self.processes[run_id] = process
            try:
                process.start(arguments, generation, log, nonce, created, environment=session["observed"]["environment"])
            finally:
                run["creation"] = {"pid": process.pid, "stage": process.stage, "ownerGroup": process.group}
                expected = self._record(run, expected=expected)
            self.managed.handshake({key: payload[key] for key in TOKEN}, run, session, deadline)
            self.managed.listen(row["id"])
            while True:
                require(process.poll() is None, "START_FAILED", "起動途中でJVMが終了しました")
                runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
                if Path(run["proofPath"]).exists():
                    phase = read_json(Path(run["proofPath"]))["phase"]
                    if phase != "INITIALIZING":
                        proof = self._proof(run, generation)
                        require_listeners(endpoints, proof["startup"]["listeners"], self.process_adapter.sockets(process.pid))
                        runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
                        write_json(diagnostics / "startup-proof.json", proof)
                        break
                require(time.monotonic() < deadline, "START_TIMEOUT", "起動期限を超過しました。自動killしません")
                time.sleep(0.1)
        return {"instanceId": row["id"], "generation": row["generation"], "runId": run_id, "running": True, "snapshotClean": False}

    def _rcon_stop(self, run, generation, process, timeout, *, expected):
        def verify():
            runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
            expected = run["endpoints"]["rcon"]
            require(socket_keys([expected]) <= socket_keys(self.process_adapter.sockets(process.pid)),
                    "LISTENER_MISMATCH", "自身のRCON待受を確認できません")
        verify()
        require_private(generation / "server.properties")
        entries = [line.split("=", 1)[1].strip() for line in (generation / "server.properties").read_text(encoding="utf-8").splitlines()
                   if line.startswith("rcon.password=")]
        require(len(entries) == 1 and entries[0], "RCON_FAILED", "RCON秘密を一意に取得できません")
        run["stopRequested"] = True
        expected = self._record(run, expected=expected)
        endpoint = run["endpoints"]["rcon"]
        runtime_rcon.stop(endpoint["address"], endpoint["port"], entries[0], timeout, verify=verify)
        run["stopAcknowledged"] = True
        expected = self._record(run, expected=expected)

        return expected

    def _remember_game_process(self, run, session):
        """認証済Mの元handleと生成snapshotを保持する。退出確定・Readyを表さない。"""
        process = self.processes[run["runId"]]
        fixed = {key: run[key] for key in ("runId", "instanceId", "generationId", "artifactId", "nonce", "identity", "creation")}
        owner = session["context"]["owner"]
        boot = owner["bootId"] if os.name == "nt" else str(uuid.UUID(owner["bootId"]))
        require(process.pid == fixed["creation"]["pid"] == fixed["identity"]["pid"] == owner["pid"]
                and process.group == fixed["creation"]["ownerGroup"] and process.group is not None
                and fixed["creation"]["stage"] == "RUNNING"
                and fixed["identity"]["startId"] == owner["startId"]
                and fixed["identity"]["bootId"] == boot,
                "PROCESS_MISMATCH", "認証済Mと元生成snapshotが一致しません")
        snapshot = canonical({"run": fixed, "owner": owner,
                              "descriptorSha256": digest(session["raw"])})
        with self._control_lock:
            require(run["runId"] not in self.game_processes, "STALE_RUN", "元Mの保持資源を差し替えません")
            self.game_processes[run["runId"]] = (process, snapshot)

    def _game_process(self, run):
        """元runの保持Mと生成snapshotのcopyを返す。台帳/候補/Readyの認証は呼出元で行う。"""
        with self._control_lock:
            held = self.game_processes.get(run["runId"])
            require(held is not None, "PROCESS_UNVERIFIED", "元Mの生成handleを保持していません")
            process, raw = held
            snapshot = json.loads(raw)
            fixed = snapshot["run"]
            require(all(run.get(key) == value for key, value in fixed.items())
                    and process.pid == fixed["creation"]["pid"] == fixed["identity"]["pid"]
                    and process.group == fixed["creation"]["ownerGroup"],
                    "PROCESS_MISMATCH", "元Mの生成handleまたはsnapshotが変更されました")
            return process, snapshot

    def _remember_game_exit(self, instance, session):
        """active slotを外す前に、元run/stopのCと固定原文を終端保持へ移す。"""
        key = (session["run"], session["stop_operation"])
        originals = {"descriptor": session["raw"],
                     "startup_request": self.managed.startup.sessions[instance]["request"],
                     "stop_request": session["stop_request"], "save_request": session["save_request"],
                     "completion_response": session["completion_response"]}
        if "game_exit_input" in session:
            originals["game_exit_input"] = session["game_exit_input"]["raw"]
        binding = canonical({"instanceId": instance, "runId": key[0], "stopOperationId": key[1],
                             "stopFingerprint": session["stop_fingerprint"], "controlOperationId": session["control_operation"]})
        with self._control_lock:
            require(key not in self.game_exits, "STALE_OPERATION", "元game退出の終端資源を差し替えません")
            self.game_exits[key] = (session["game_exit"], originals, binding)

    def _game_exit(self, run_id, stop_operation):
        """Pが保持した元Cのslotと、原文/bindingのcopyを返す。再観測/再attach/解放はしない。"""
        with self._control_lock:
            held = self.game_exits.get((run_id, stop_operation))
            require(held is not None, "PROCESS_UNVERIFIED", "元run/stopの観測Cを保持していません")
            slot, originals, raw = held
            binding = json.loads(raw)
            require(binding["runId"] == run_id and binding["stopOperationId"] == stop_operation,
                    "PROCESS_MISMATCH", "終端資源の元run/stopが一致しません")
            return slot, dict(originals), binding

    def _wait_exit(self, run, process, timeout, *, expected, verify=None):
        with self._control_lock:
            retained = run["runId"] in self.game_processes
            require(retained or not any(held is process for held, _ in self.game_processes.values()),
                    "PROCESS_MISMATCH", "元Mのrun識別が変更されました")
        if retained:
            require(self._game_process(run)[0] is process, "PROCESS_MISMATCH", "別handleで元Mの終了を記録しません")
        deadline = time.monotonic() + timeout
        while True:
            running = process.poll() is None
            members = process.members()
            if verify is not None:
                verify(set(members) | ({process.pid} if running else set()))
            if not running and not members:
                break
            require(time.monotonic() < deadline, "STOP_TIMEOUT", "JVM/child終了期限を超過しました。自動killしません")
            time.sleep(0.1)
        run["exitCode"] = process.poll()
        if retained:
            require(self._game_process(run)[0] is process, "PROCESS_MISMATCH", "終了観測中に元Mの保持資源が変更されました")
        run["jvmAndChildrenExited"] = True
        run["exitedAt"] = self.registry.clock()
        expected = self._record(run, expected=expected)
        transaction = (self.registry.transaction() if run.get("kind") == "gametest"
                       else self._transaction(run["instanceId"]))
        with transaction as connection:
            connection.execute("DELETE FROM endpoint_reservations WHERE run=?", (run["runId"],))
        if not retained:
            process.close()
        self.processes.pop(run["runId"], None)
        return expected

    def _stop_handle_release(self, token, result, deadline, *, replay=False):
        """公共stopの成功応答を同stopの解放正本へ結束する。再提示は結果照合だけを行う。"""
        operation_id = result.get("gameHandleReleaseOperationId")
        if operation_id is None:
            return
        token = {key: token[key] for key in TOKEN}
        require(time.monotonic() < deadline, "STOP_TIMEOUT", "stopのhandle解放前に期限を超過しました")
        if not replay:
            self._release_game_handles(token, operation_id=operation_id,
                                       stop_operation_id=result["stopOperationId"], deadline=deadline)
        with self._transaction(token["instanceId"], read_only=True) as connection:
            row = self.registry._instance(connection, token["instanceId"])
            self.registry._token(row, token, allow_expired=True)
            require(row["run_id"] == result["runId"] and row["state"] == "LEASED_STOPPED" and not row["pending"]
                    and json.loads(row["runtime_proof"]) == result,
                    "RECOVERY_REQUIRED", "元stopとhandle解放の確定結果が必要です")
            slot, _, binding = self._game_exit(result["runId"], result["stopOperationId"])
            progress = slot.get("handle_release", {})
            operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
            require(binding["instanceId"] == row["id"] and progress.get("operationId") == operation_id
                    and progress.get("m") == progress.get("c") == "RELEASED" and progress.get("completed") is True
                    and operation is not None and operation["instance"] == row["id"]
                    and operation["command"] == "game-handle-release" and operation["status"] == "COMPLETED"
                    and operation["fingerprint"] == progress.get("fingerprint"),
                    "RECOVERY_REQUIRED", "元handle解放の結果が未確定です")
            released = json.loads(operation["response"])
            require(released["instanceId"] == row["id"] and released["runId"] == result["runId"]
                    and released["stopOperationId"] == result["stopOperationId"] and released["handlesReleased"] is True
                    and released["binding"]["stopProofSha256"] == digest(canonical(result)),
                    "STALE_OPERATION", "元stopとhandle解放結果が一致しません")
            require(time.monotonic() < deadline, "STOP_TIMEOUT", "handle解放結果の照合後に期限を超過しました")
        require(time.monotonic() < deadline, "STOP_TIMEOUT", "handle解放結果の返却前に期限を超過しました")

    def _stop(self, row, payload, generation, deadline=None):
        run, expected = self._load_run(row, with_record=True)
        process = self.processes[row["run_id"]]
        if self.managed is None or process.poll() is None:
            runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
        if self.managed is not None:
            session = self.managed.sessions[row["id"]]
            if deadline is None:
                deadline = time.monotonic() + payload["timeoutSeconds"]
            deadline = min(deadline, session.get("stop_deadline", deadline))
            if "game_exit_input" in session:
                deadline = min(deadline, session["game_exit_input"]["deadline"])
            require(time.monotonic() < deadline, "STOP_TIMEOUT", "stop待機前に期限を超過しました")
            self.managed.drain(row["id"], session, deadline)
            deadline = min(deadline, session.get("stop_deadline", deadline))
            require(time.monotonic() < deadline, "STOP_TIMEOUT", "stop drain後に期限を超過しました")
            if not session["stop"].is_set():
                expected = self._rcon_stop(run, generation, process, max(0.001, deadline - time.monotonic()), expected=expected)
            completed = session["done"].wait(max(0, deadline - time.monotonic()))
            deadline = min(deadline, session.get("stop_deadline", deadline))
            require(time.monotonic() < deadline, "STOP_TIMEOUT", "stop保存待機中に期限を超過しました")
            require(completed, "CONTROL_UNKNOWN", "MのRequested/COMPLETION/closeが未完了です")
            require(session["error"] is None, "CONTROL_UNKNOWN", "元停止制御が不明です")
            if "game_exit_input" in session:
                self.managed.observe_selected_game_exit(row["id"], session, deadline)
                deadline = min(deadline, session["stop_deadline"])
            if "game_exit" in session:
                self.managed.game_exit_inputs(row["id"], session, session["game_exit"]["candidate"], deadline)
                self.managed.game_exit_evidence(row["id"], session, deadline)
            remaining = max(0.001, deadline - time.monotonic())
        else:
            expected = self._rcon_stop(run, generation, process, payload["timeoutSeconds"], expected=expected)
            remaining = payload["timeoutSeconds"]
        expected = self._wait_exit(run, process, remaining, expected=expected)
        require(run["exitCode"] == 0, "SAVE_FAILED", "JVMが正常終了していません")
        proof = self._proof(run, generation, stopped=True)
        run["runtimeSaveCompleted"] = True
        run["proofHash"] = digest(Path(run["proofPath"]).read_bytes())
        expected = self._record(run, expected=expected)
        result = {"instanceId": row["id"], "generation": row["generation"], "runId": run["runId"],
                  "nonce": run["nonce"], "runtimeStopped": True, "modSaveCompleted": True,
                   "stopRequested": run["stopRequested"], "stopAcknowledged": run.get("stopAcknowledged", False), "exitCode": run["exitCode"],
                  "jvmAndChildrenExited": True, "externalWritersVerified": False, "snapshotClean": False,
                  "proofHash": run["proofHash"]}
        if self.managed is not None and "control_operation" in session:
            result.update(stopOperationId=session["stop_operation"], controlOperationId=session["control_operation"],
                          managedSave=dict(session["save_evidence"]))
            if "game_exit" in session:
                self.managed.game_exit_inputs(row["id"], session, session["game_exit"]["candidate"], deadline)
                result["gameExit"] = self.managed.game_exit_evidence(row["id"], session, deadline)
                if not session["direct_stop"]:
                    result["gameHandleReleaseOperationId"] = "game-release-" + uuid.uuid4().hex
        write_json(Path(run["proofPath"]).parent / "runtime-stop.json", result)
        return result

    def _direct_stop(self, instance, session):
        """外部停止も元runへ予約し、保存proofと実exit後にだけ停止確定する。"""
        deadline = session["stop_deadline"] if "control_operation" in session else time.monotonic() + 30
        operation_id = session.get("stop_operation", "direct-stop-" + uuid.uuid4().hex)
        with self._transaction(instance) as connection:
            row = dict(self.registry._instance(connection, instance))
            if row["run_id"] == session["run"] and row["state"] == "LEASED_STOPPED" and not row["pending"]:
                return
            if "stop_operation" in session and not session["direct_stop"]:
                return
            if connection.execute("SELECT 1 FROM runtime_operations WHERE instance=? AND command='stop' AND status='PENDING' AND id!=?", (instance, operation_id)).fetchone() and not session.get("direct_stop"):
                return
            require(time.monotonic() < deadline, "STOP_TIMEOUT", "元stopの期限を超過しました")
            require(row["run_id"] == session["run"] and row["state"] == "STOPPING" and row["pending"],
                    "STALE_RUN", "直接停止した元runではありません")
            if "stop_operation" not in session:
                connection.execute("INSERT INTO runtime_operations VALUES (?,?, 'stop',?,'PENDING','{}')",
                                   (operation_id, digest(session["raw"]), instance))
        generation = self._active_generation(row)
        # RCON再送を避け、受信済みSTOP_BLOCKと同Requestedだけから保存終端を照合する。
        result = self._stop(row, dict(session["token"], timeoutSeconds=max(0.001, deadline - time.monotonic())), generation, deadline)
        with self._transaction(instance) as connection:
            current = self.registry._instance(connection, instance)
            self._binding(current, row)
            if "control_operation" in session:
                evidence, pending = self.managed.save_evidence(connection, instance, session, deadline)
                require(evidence == result["managedSave"] == pending.get("managedSave"),
                        "STALE_OPERATION", "直接stopの保存原文記録が変更されました")
                if "game_exit" in session:
                    require(self.managed.game_exit_evidence(instance, session, deadline) == result.get("gameExit"),
                            "STALE_OPERATION", "直接stopのgame退出証拠が変更されました")
                require(time.monotonic() < deadline, "STOP_TIMEOUT", "直接stopの最終照合後に期限を超過しました")
            waiters = connection.execute("""SELECT r.*,o.instance AS operation_instance,o.command,o.status,o.response
                FROM reservations r LEFT JOIN runtime_operations o ON o.id=r.operation
                WHERE r.instance=? AND r.reference=? AND r.operation!=?""",
                (instance, "stop:" + operation_id, session.get("control_operation"))).fetchall()
            for waiter in waiters:
                require(waiter["lease"] == row["lease"] and waiter["epoch"] == row["epoch"]
                        and waiter["operation_instance"] == instance and waiter["command"] == "stop"
                        and waiter["status"] == "PENDING" and json.loads(waiter["response"]) == {
                            "runId": session["run"], "stopOperationId": operation_id},
                        "STALE_OPERATION", "合流予約と元stopのbindingが一致しません")
            require(time.monotonic() < deadline, "STOP_TIMEOUT", "合流予約の最終照合後に期限を超過しました")
            connection.execute("UPDATE instances SET state='LEASED_STOPPED',pending=0,runtime_proof=? WHERE id=?", (json.dumps(result), instance))
            for waiter in waiters:
                connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?",
                                   (json.dumps(result), waiter["operation"]))
                connection.execute("DELETE FROM reservations WHERE operation=? AND reference=?",
                                   (waiter["operation"], "stop:" + operation_id))
            connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation_id))
            connection.execute("DELETE FROM reservations WHERE reference=? AND operation=?",
                               ("stop:" + operation_id, session.get("control_operation")))
        try:
            if "managedSave" in result:
                require(time.monotonic() < deadline, "STOP_TIMEOUT", "直接stop結果記録後に期限を超過しました")
        finally:
            self.managed.release(instance, session["run"])
        if "managedSave" in result:
            require(time.monotonic() < deadline, "STOP_TIMEOUT", "直接stop返却前に期限を超過しました")

    def _join_direct_stop(self, row, payload, operation_id, fingerprint, session, deadline=None):
        """元stopの確定で待機予約を終端し、同じ結果だけを期限内に返す。"""
        if deadline is None:
            deadline = time.monotonic() + payload["timeoutSeconds"]
        deadline = min(deadline, session.get("stop_deadline", deadline))
        require(time.monotonic() < deadline, "STOP_TIMEOUT", "合流受付前に期限を超過しました")
        parent = session["stop_operation"]
        pending = {"runId": row["run_id"], "stopOperationId": parent}
        with self._lock_wait(deadline):
            with self._transaction(row["id"]) as connection:
                current = self.registry._instance(connection, row["id"])
                self.registry._token(current, payload, allow_expired=True)
                self._binding(current, row)
                require(current["state"] in {"STOPPING", "LEASED_STOPPED"}, "STALE_RUN", "直接停止の元runではありません")
                for table in ("operations", "runtime_operations", "reference_operations", "writer_operations", "snapshot_operations"):
                    require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                            "OPERATION_CONFLICT", "operationIdが他の操作に使用されています")
                saved = connection.execute("SELECT * FROM runtime_operations WHERE id=? AND instance=? AND command='stop'",
                                           (parent, row["id"])).fetchone()
                require(saved is not None and saved["status"] in {"PENDING", "COMPLETED"},
                        "CONTROL_UNKNOWN", "元停止予約がありません")
                completed = saved["status"] == "COMPLETED"
                if completed:
                    result = json.loads(saved["response"])
                    require(current["state"] == "LEASED_STOPPED" and not current["pending"]
                            and result.get("runId") == session["run"] and result.get("stopOperationId") == parent
                            and current["runtime_proof"] is not None and json.loads(current["runtime_proof"]) == result,
                            "CONTROL_UNKNOWN", "元停止の確定証拠が一致しません")
                else:
                    require(current["state"] == "STOPPING" and current["pending"],
                            "CONTROL_UNKNOWN", "元停止が未確定です")
                require(time.monotonic() < deadline, "STOP_TIMEOUT", "合流予約前に期限を超過しました")
                connection.execute("INSERT INTO runtime_operations VALUES (?,?, 'stop',?,?,?)",
                    (operation_id, fingerprint, row["id"], "COMPLETED" if completed else "PENDING",
                     saved["response"] if completed else json.dumps(pending)))
                if not completed:
                    connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)",
                        (operation_id, "stop:" + parent, row["id"], row["lease"], row["epoch"]))
            while True:
                result = None
                with self._transaction(row["id"], read_only=True) as connection:
                    current = self.registry._instance(connection, row["id"])
                    self.registry._token(current, payload, allow_expired=True)
                    self._binding(current, row)
                    saved = connection.execute("SELECT * FROM runtime_operations WHERE id=? AND instance=? AND command='stop'", (parent, row["id"])).fetchone()
                    waiter = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
                    require(saved is not None and waiter is not None and waiter["instance"] == row["id"]
                            and waiter["command"] == "stop" and waiter["fingerprint"] == fingerprint,
                            "STALE_OPERATION", "元停止または合流要求が変更されました")
                    if saved["status"] == "COMPLETED":
                        require(current["state"] == "LEASED_STOPPED" and not current["pending"], "CONTROL_UNKNOWN", "元停止が未確定です")
                        result = json.loads(saved["response"])
                        require(result.get("runId") == session["run"] and result.get("stopOperationId") == parent
                                and current["runtime_proof"] is not None and json.loads(current["runtime_proof"]) == result and waiter["status"] == "COMPLETED"
                                and waiter["response"] == saved["response"]
                                and not connection.execute("SELECT 1 FROM reservations WHERE operation=?", (operation_id,)).fetchone(),
                                "STALE_OPERATION", "元stopと合流の確定結果が一致しません")
                        require(time.monotonic() < deadline, "STOP_TIMEOUT", "合流結果の最終照合後に期限を超過しました")
                    else:
                        require(saved["status"] == "PENDING" and current["state"] == "STOPPING" and current["pending"]
                                and waiter["status"] == "PENDING" and json.loads(waiter["response"]) == pending,
                                "CONTROL_UNKNOWN", "元停止または合流が不明です")
                require(time.monotonic() < deadline, "STOP_TIMEOUT", "合流結果記録後に期限を超過しました")
                if result is not None:
                    break
                time.sleep(0.01)
        require(time.monotonic() < deadline, "STOP_TIMEOUT", "合流返却前に期限を超過しました")
        return result

    def _running(self, row, *, closing=False, observe_stop=False):
        run = self._load_run(row)
        process = self.processes.get(row["run_id"])
        require(process is not None and process.poll() is None, "PROCESS_MISMATCH", "JVMが監督下で稼働していません")
        runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
        generation = self._active_generation(row)
        stopping = closing and self.managed is not None and row["state"] == "STOPPING"
        if stopping:
            with self._transaction(row["id"], read_only=True) as connection:
                current = self.registry._instance(connection, row["id"])
                self._binding(current, row)
                require(current["state"] == "STOPPING" and current["pending"]
                        and _admission(connection, current)["gate"] == "BLOCKED",
                        "CONTROL_BLOCKED", "元runの停止予約が必要です")
        proof = self._proof(run, generation, stopping=stopping or (observe_stop and self.managed is not None))
        if observe_stop and proof["phase"] == "STOPPING":
            session = self.managed.sessions.get(row["id"])
            startup = self.managed.startup.sessions.get(row["id"])
            require(session is not None and startup is not None and startup.get("acknowledged")
                    and session["context"]["owner"]["pid"] == process.pid
                    and session["context"]["owner"]["startId"] == run["identity"]["startId"],
                    "STALE_RUN", "同じ所有Mの停止制御sessionが必要です")
            self.managed.reserve_stop(row["id"], session)
            session["stop"].set()
            return
        endpoints = {"management": run["endpoints"]["management"]} if stopping else run["endpoints"]
        listeners = proof["startup"]["listeners"]
        actual = self.process_adapter.sockets(process.pid)
        if stopping:
            require(socket_keys(actual) <= socket_keys(list(run["endpoints"].values())),
                    "LISTENER_MISMATCH", "停止中に未知の待受があります")
            listeners = [item for item in listeners if item["service"] == "management"]
            actual = [item for item in actual if socket_keys([item]) == socket_keys(list(endpoints.values()))]
        require_listeners(endpoints, listeners, actual)

    def refresh(self):
        """異常終了をRUNNINGのまま返さない。予約/データは復旧照合まで保留する。"""
        with self.registry.transaction(read_only=True) as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM instances WHERE state='RUNNING'")]
        for row in rows:
            try:
                self._running(row, observe_stop=True)
            except (RegistryError, OSError, ValueError, KeyError):
                with self._transaction(row["id"]) as connection:
                    current = self.registry._instance(connection, row["id"])
                    if any(current[key] != row[key] for key in ("lease", "epoch", "generation", "run_id", "state", "pending")):
                        continue
                    connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=? AND state='RUNNING' AND run_id=?",
                                       (row["id"], row["run_id"]))
