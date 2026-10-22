"""固定bundleの起動/停止。世代構築はGenerationControlで行う。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid

from .build_inputs import canonical, digest
from .contracts import LAYOUT_ID
from .local_ipc import decode, make_private, plain_path, require_private
from .registry import RegistryError, fields, identifier, require, ttl
from .runtime_bundle import launch_command, verify_bundle, require_data_generation
from .runtime_ports import instance_endpoints, reserve, prebind, require_listeners, socket_keys
from . import runtime_process, runtime_rcon

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
        directories = [self.registry.root]
        while directories:
            directory = directories.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    info = path.lstat()
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
                and shutil.disk_usage(self.registry.root).free >= required_bytes + margin,
                "NO_SPACE", "runtime配備と起動余裕を確保できません。自動削除しません")

    def copy_space(self, size):
        """コピー中の物理空き減少を各書込前に検査する。削除で空きを作らない。"""
        require(shutil.disk_usage(self.registry.root).free >= size + self.registry.inventory["reserveBytes"],
                "NO_SPACE", "コピー中に物理空き容量が減少しました。旧データを保持します")

    @contextmanager
    def _transaction(self, instance, *, read_only=False):
        from .supervisor import private_lock
        with private_lock(self.registry.root / "manager/locks" / f"instance-{instance}.lock"):
            with self.registry.transaction(read_only=read_only) as connection:
                yield connection

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
            return dict(response, replayed=True, dryRun=dry_run)
        row = self._row(payload, allow_expired=command == "stop")
        require(row["generation_bound"] and row["generation"] is not None and not row["pending"],
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
            self._space()
        else:
            require(row["state"] == "RUNNING" and row["run_id"] in self.processes,
                    "NOT_RUNNING", "監督が所有するRUNNING runだけを停止します")
        endpoints = instance_endpoints(next(item for item in self.registry.inventory["instances"] if item["instanceId"] == row["id"]))
        if dry_run:
            if command == "stop":
                self._running(row)
            return {"dryRun": True, "wouldExecute": command, "instanceId": row["id"]}
        with self._transaction(row["id"]) as connection:
            for table in ("operations", "reference_operations", "writer_operations", "snapshot_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
            current = self.registry._instance(connection, row["id"])
            self.registry._token(current, payload, allow_expired=command == "stop")
            self._binding(current, row)
            require(current["state"] == row["state"] and not current["pending"], "BUSY", "枠の状態が変更されました")
            require(not connection.execute("SELECT 1 FROM writer_sessions WHERE instance=? AND pending!=0", (row["id"],)).fetchone(),
                    "WRITER_PENDING", "writer保存の完了が不明なため停止・再起動を開始できません")
            require(not connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'",
                                           (row["id"],)).fetchone(), "WRITER_PENDING", "writer callが実行中です")
            response = {"instanceId": row["id"], "pending": True}
            connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                               (operation_id, fingerprint, command, row["id"], "PENDING", json.dumps(response)))
            connection.execute("UPDATE instances SET state=?,pending=1,runtime_proof=NULL,clean_proof=NULL WHERE id=?",
                               ({"start": "STARTING", "stop": "STOPPING"}[command], row["id"]))
            connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)", (self.registry.clock(), operation_id, command))
            connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)",
                               (operation_id, "artifact:" + row["artifact"],
                                row["id"], row["lease"], row["epoch"]))
        try:
            if command == "start":
                result = self._start(row, payload, generation, endpoints, operation_id)
            else:
                result = self._stop(row, payload, generation)
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
                    connection.execute("UPDATE instances SET state='RUNNING',pending=0 WHERE id=?", (row["id"],))
                else:
                    connection.execute("UPDATE instances SET state='LEASED_STOPPED',pending=0,runtime_proof=? WHERE id=?",
                                       (json.dumps(result), row["id"]))
                connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?", (json.dumps(result), operation_id))
                connection.execute("DELETE FROM reservations WHERE operation=?", (operation_id,))
            return result
        except BaseException as error:
            code = error.code if isinstance(error, RegistryError) else "RUNTIME_FAILED"
            with self._transaction(row["id"]) as connection:
                current = self.registry._instance(connection, row["id"])
                operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
                self._binding(current, row, run_id=row.get("reserved_run_id", row["run_id"]))
                require(current["pending"] and operation is not None and operation["status"] == "PENDING"
                        and operation["fingerprint"] == fingerprint and operation["instance"] == row["id"],
                        "STALE_OPERATION", "変更済みの操作を失敗へ上書きしません")
                connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=?", (row["id"],))
                connection.execute("UPDATE runtime_operations SET status='RECOVERY_REQUIRED',response=? WHERE id=?",
                                   (json.dumps({"error": code}), operation_id))
            if isinstance(error, RegistryError):
                raise
            raise RegistryError(code, "runtime操作に失敗しました。保全し、自動kill/再送しません") from None

    def _proof(self, run, generation, *, stopped=False):
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
            require(proof["phase"] == "STARTED", "PROOF_MISMATCH", "起動完了phaseが一致しません")
        return proof

    def _start(self, row, payload, generation, endpoints, operation_id):
        nonce = uuid.uuid4().hex
        run_id = "run-" + nonce
        diagnostics = self._paths(row["id"]) / "diagnostics" / run_id
        diagnostics.mkdir(parents=True)
        make_private(diagnostics, directory=True)
        run = {"runId": run_id, "instanceId": row["id"], "generationId": row["generation"],
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
        properties = {"instanceId": row["id"], "generationId": row["generation"], "startNonce": nonce,
                      "proofPath": run["proofPath"], "runtimeManifest": str(generation / "runtime/runtime.json")}
        require(not any(os.environ.get(key) for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH")),
                "UNREPRODUCIBLE_ENVIRONMENT", "暗黙のJVM環境入力を使用できません")
        arguments = launch_command(generation / "runtime", "server", generation,
                                   auxiliary=diagnostics / ".launch", jvm_properties=properties)
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

        process = None
        try:
            # probesを閉じる瞬間のraceは、後段の実PID/socket・nonce照合で検出する。
            with prebind(endpoints):
                pass
            with (diagnostics / "console.log").open("xb") as log:
                make_private(diagnostics / "console.log")
                process = self.process_adapter.OwnedProcess()
                self.processes[run_id] = process
                try:
                    process.start(arguments, generation, log, nonce, created)
                finally:
                    run["creation"] = {"pid": process.pid, "stage": process.stage, "ownerGroup": process.group}
                    expected = self._record(run, expected=expected)
                deadline = time.monotonic() + payload["timeoutSeconds"]
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
        except BaseException:
            if process is not None and process.stage == "RUNNING":
                try:
                    if process.poll() is None:
                        expected = self._rcon_stop(run, generation, process, payload["timeoutSeconds"], expected=expected)
                    expected = self._wait_exit(run, process, payload["timeoutSeconds"], expected=expected)
                except (RegistryError, OSError):
                    pass
            raise

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

    def _wait_exit(self, run, process, timeout, *, expected, verify=None):
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
        run["jvmAndChildrenExited"] = True
        run["exitedAt"] = self.registry.clock()
        expected = self._record(run, expected=expected)
        transaction = (self.registry.transaction() if run.get("kind") == "gametest"
                       else self._transaction(run["instanceId"]))
        with transaction as connection:
            connection.execute("DELETE FROM endpoint_reservations WHERE run=?", (run["runId"],))
        process.close()
        self.processes.pop(run["runId"], None)
        return expected

    def _stop(self, row, payload, generation):
        run, expected = self._load_run(row, with_record=True)
        process = self.processes[row["run_id"]]
        runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
        expected = self._rcon_stop(run, generation, process, payload["timeoutSeconds"], expected=expected)
        expected = self._wait_exit(run, process, payload["timeoutSeconds"], expected=expected)
        require(run["exitCode"] == 0, "SAVE_FAILED", "JVMが正常終了していません")
        proof = self._proof(run, generation, stopped=True)
        run["runtimeSaveCompleted"] = True
        run["proofHash"] = digest(Path(run["proofPath"]).read_bytes())
        expected = self._record(run, expected=expected)
        result = {"instanceId": row["id"], "generation": row["generation"], "runId": run["runId"],
                  "nonce": run["nonce"], "runtimeStopped": True, "modSaveCompleted": True,
                  "stopRequested": run["stopRequested"], "stopAcknowledged": run["stopAcknowledged"], "exitCode": run["exitCode"],
                  "jvmAndChildrenExited": True, "externalWritersVerified": False, "snapshotClean": False,
                  "proofHash": run["proofHash"]}
        write_json(Path(run["proofPath"]).parent / "runtime-stop.json", result)
        return result

    def _running(self, row):
        run = self._load_run(row)
        process = self.processes.get(row["run_id"])
        require(process is not None and process.poll() is None, "PROCESS_MISMATCH", "JVMが監督下で稼働していません")
        runtime_process.require_identity(run["identity"], self.process_adapter.identity(process.pid))
        generation = self._active_generation(row)
        proof = self._proof(run, generation)
        require_listeners(run["endpoints"], proof["startup"]["listeners"], self.process_adapter.sockets(process.pid))

    def refresh(self):
        """異常終了をRUNNINGのまま返さない。予約/データは復旧照合まで保留する。"""
        with self.registry.transaction(read_only=True) as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM instances WHERE state='RUNNING'")]
        for row in rows:
            try:
                self._running(row)
            except (RegistryError, OSError, ValueError, KeyError):
                with self._transaction(row["id"]) as connection:
                    current = self.registry._instance(connection, row["id"])
                    if any(current[key] != row[key] for key in ("lease", "epoch", "generation", "run_id", "state", "pending")):
                        continue
                    connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=? AND state='RUNNING' AND run_id=?",
                                       (row["id"], row["run_id"]))
