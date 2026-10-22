"""固定bundleのGameTestを監督する。永続枠やRCONを流用しない。"""

import json
import os
from pathlib import Path
import re
import time
import traceback
import uuid

from .build_inputs import canonical, digest
from .contracts import ContractError
from .gametest_report import read_report
from .local_ipc import make_private, plain_path
from .registry import RegistryError, fields, identifier, require, ttl
from .runtime_bundle import launch_command, verify_bundle
from .runtime_control import read_json, write_json
from . import runtime_process


class GameTestControl:
    """予約と結果はRegistry、重い検査とJVM待機はDB transaction外で行う。"""
    def __init__(self, registry):
        self.registry = registry

    def execute(self, payload, *, operation_id, dry_run=False):
        if type(payload) is dict and payload.get("action") == "pin":
            return self._pin(payload, operation_id, dry_run)
        fields(payload, {"runId", "owner", "artifactId", "timeoutSeconds", "acceptEula"})
        identifier(operation_id, "operationId")
        identifier(payload["runId"], "runId")
        identifier(payload["owner"], "owner")
        ttl(payload["timeoutSeconds"])
        require(type(payload["artifactId"]) is str and re.fullmatch(r"[0-9a-f]{64}", payload["artifactId"]),
                "INVALID_INPUT", "artifactIdはSHA-256で指定してください")
        require(payload["acceptEula"] is True, "EULA_REQUIRED", "GameTestのEULAへの明示同意が必要です")
        require(not any(os.environ.get(key) for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH")),
                "UNREPRODUCIBLE_ENVIRONMENT", "暗黙のJVM環境入力を使用できません")
        fingerprint = digest(canonical({"command": "gametest", "payload": payload}))
        with self.registry.transaction(read_only=True) as connection:
            replay = self._check(connection, payload, operation_id, fingerprint)
        if replay is not None:
            return self._result(replay, replayed=True, dryRun=dry_run)
        bundle = self.registry.root / "artifacts" / payload["artifactId"]
        verify_bundle(bundle)
        self.registry.runtime._space()
        if dry_run:
            return {"dryRun": True, "wouldExecute": "gametest", "runId": payload["runId"]}
        with self.registry.transaction() as connection:
            replay = self._check(connection, payload, operation_id, fingerprint)
            if replay is not None:
                return self._result(replay, replayed=True)
            connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                               (operation_id, fingerprint, "gametest", payload["runId"], "PENDING", json.dumps(payload)))
            connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)",
                               (self.registry.clock(), operation_id, "gametest"))
        try:
            result = self._run(payload, bundle)
            with self.registry.transaction() as connection:
                connection.execute("UPDATE runtime_operations SET status=?,response=? WHERE id=?",
                                   ("COMPLETED" if result["testsPassed"] and result["runtimeStopped"] else "FAILED",
                                    json.dumps(result), operation_id))
        except BaseException as error:
            code = error.code if isinstance(error, RegistryError) else "GAMETEST_FAILED"
            failure = {"runId": payload["runId"], "artifactId": payload["artifactId"], "error": code,
                       "errorType": type(error).__name__, "nativeError": getattr(error, "winerror", None) or getattr(error, "errno", None),
                       "frames": [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                                  for frame in traceback.extract_tb(error.__traceback__)]}
            with self.registry.transaction() as connection:
                connection.execute("UPDATE runtime_operations SET status='RECOVERY_REQUIRED',response=? WHERE id=?",
                                   (json.dumps(failure), operation_id))
            raise RegistryError(code, "GameTestの証跡・予約を保全しました。自動kill・再実行しません") from error
        return self._result(result)

    def _check(self, connection, payload, operation_id, fingerprint):
        for table in ("operations", "reference_operations", "writer_operations", "snapshot_operations"):
            require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                    "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
        previous = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
        if previous:
            require(previous["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "operationIdの要求が一致しません")
            require(previous["status"] in ("COMPLETED", "FAILED"), "RECOVERY_REQUIRED", "未確定の実行を自動再実行しません")
            return json.loads(previous["response"])
        require(not connection.execute("SELECT 1 FROM runtime_operations WHERE command='gametest' AND instance=?",
                                       (payload["runId"],)).fetchone(), "EXISTING_DATA", "GameTestの実行IDは使用済みです")
        require(not connection.execute("SELECT 1 FROM runs WHERE id=?", (payload["runId"],)).fetchone(),
                "EXISTING_DATA", "実行IDが別の起動記録で使用されています")
        run = self.registry.root / "temporary" / payload["runId"]
        plain_path(run)
        require(not run.exists(), "EXISTING_DATA", "GameTestの既存データを上書きしません")
        count = connection.execute("SELECT count(*) FROM runtime_operations WHERE command='gametest' AND status IN ('PENDING','RECOVERY_REQUIRED')").fetchone()[0]
        require(count < self.registry.inventory["gameTestConcurrency"], "BUSY", "GameTestの同時実行枠が使用中です")
        return None

    def _pin(self, payload, operation_id, dry_run):
        fields(payload, {"action", "runId", "owner", "pinned"})
        identifier(operation_id, "operationId")
        identifier(payload["runId"], "runId")
        identifier(payload["owner"], "owner")
        require(type(payload["pinned"]) is bool, "INVALID_INPUT", "pinnedはbooleanで指定してください")
        fingerprint = digest(canonical({"command": "gametest-pin", "payload": payload}))
        with self.registry.transaction(read_only=dry_run) as connection:
            row = connection.execute("SELECT record FROM runs WHERE id=?", (payload["runId"],)).fetchone()
            require(row is not None, "NOT_FOUND", "GameTestの実行記録がありません")
            run = json.loads(row[0])
            require(run.get("kind") == "gametest" and run["owner"] == payload["owner"],
                    "OWNER_MISMATCH", "このownerのGameTestではありません")
            require("completedAt" in run, "BUSY", "未確定の実行はすでに保護されておりpinを変更できません")
            for table in ("operations", "reference_operations", "writer_operations", "snapshot_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
            previous = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (operation_id,)).fetchone()
            if previous:
                require(previous["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "pin要求が一致しません")
                require(run["pinned"] == payload["pinned"], "STALE_OPERATION", "pin変更後へ古い結果を適用できません")
                return dict(json.loads(previous["response"]), replayed=True, dryRun=dry_run)
            result = {"runId": run["runId"], "pinned": payload["pinned"]}
            if dry_run:
                return dict(result, dryRun=True)
            run["pinned"] = payload["pinned"]
            connection.execute("UPDATE runs SET record=? WHERE id=?", (json.dumps(run), run["runId"]))
            connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                               (operation_id, fingerprint, "gametest-pin", run["runId"], "COMPLETED", json.dumps(result)))
            connection.execute("INSERT INTO audit(at,operation,command) VALUES (?,?,?)",
                               (self.registry.clock(), operation_id, "gametest-pin"))
            return result

    @staticmethod
    def _result(result, **extra):
        require(result["testsPassed"] and result["runtimeStopped"], "GAMETEST_FAILED",
                "GameTestまたは終了時保存に失敗しました。run-idの結果・ログを確認してください")
        return dict(result, **extra)

    def _proof(self, run, bundle):
        proof = read_json(Path(run["proofPath"]))
        identity = proof["identity"]
        require(proof["schemaVersion"] == 1 and proof["instanceId"] == run["instanceId"]
                and proof["generationId"] == run["generationId"] and proof["startNonce"] == run["nonce"],
                "PROOF_MISMATCH", "GameTestの起動識別が一致しません")
        require(identity["pid"] == run["identity"]["pid"]
                and runtime_process.proof_start_millis(identity["processStartedAt"]) == run["identity"]["startMillis"]
                and identity["artifactId"] == run["artifactId"]
                and identity["runtimeManifestSha256"] == run["manifestHash"]
                and Path(identity["runtimeManifest"]).resolve() == (bundle / "runtime.json").resolve()
                and Path(identity["cwd"]).resolve() == Path(run["cwd"]).resolve(),
                "PROOF_MISMATCH", "GameTestのプロセス・runtimeが一致しません")
        startup = proof["startup"]
        require(startup["ready"] is True and startup["managementEnabled"] is False
                and startup["managementStarted"] is False and startup["listeners"] == []
                and startup["unknownWriters"] == [], "START_REJECTED", "GameTestの起動・待受構成が一致しません")
        cwd = Path(run["cwd"])
        expected = {"world": cwd / "world", "config": cwd / "config", "modConfig": cwd / "config/SushiEricServerMod"}
        require(startup["roots"].keys() == expected.keys(), "ROOT_MISMATCH", "GameTestの保存root集合が一致しません")
        for key, path in expected.items():
            plain_path(Path(startup["roots"][key]))
            require(Path(startup["roots"][key]).resolve() == path.resolve(), "ROOT_MISMATCH", "GameTestの保存rootが一致しません")
        require(type(startup["seed"]) is int, "PROOF_MISMATCH", "実効seedの記録がありません")
        return proof

    def _run(self, payload, bundle):
        runtime = self.registry.runtime
        adapter = runtime.process_adapter
        cwd = self.registry.root / "temporary" / payload["runId"]
        cwd.mkdir()
        make_private(cwd, directory=True)
        diagnostics = cwd / "diagnostics"
        diagnostics.mkdir()
        make_private(diagnostics, directory=True)
        run = {"kind": "gametest", "runId": payload["runId"], "instanceId": payload["runId"],
               "generationId": payload["runId"], "owner": payload["owner"], "artifactId": payload["artifactId"],
               "templateId": None, "nonce": uuid.uuid4().hex, "cwd": str(cwd), "identity": None,
               "proofPath": str(diagnostics / "proof.json"), "manifestHash": digest((bundle / "runtime.json").read_bytes()),
               "endpoints": {}, "stopRequested": False, "jvmAndChildrenExited": False, "runtimeSaveCompleted": False,
               "createdAt": self.registry.clock(), "pinned": False}
        expected = runtime._record(run)
        write_json(diagnostics / "request.json", payload)
        (cwd / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        # 初回properties欠損の警告を保存失敗証跡に混ぜない。
        (cwd / "server.properties").write_text("enable-rcon=false\nenable-query=false\n", encoding="utf-8")
        arguments = launch_command(bundle, "gameTest", cwd, jvm_properties={
            "instanceId": run["instanceId"], "generationId": run["generationId"], "startNonce": run["nonce"],
            "proofPath": run["proofPath"], "runtimeManifest": str(bundle / "runtime.json")})
        run["commandHash"] = runtime_process.command_hash(arguments)
        expected = runtime._record(run, expected=expected)
        process = adapter.OwnedProcess()
        runtime.processes[run["runId"]] = process

        def created():
            nonlocal expected
            run["creation"] = {"pid": process.pid, "stage": process.stage, "ownerGroup": process.group}
            write_json(diagnostics / "creation.json", run["creation"])
            expected = runtime._record(run, expected=expected)
            run["identity"] = adapter.identity(process.pid)
            expected = runtime._record(run, expected=expected)
            require(run["identity"] is not None and Path(run["identity"]["cwd"]).resolve() == cwd.resolve()
                    and Path(run["identity"]["executable"]).resolve() == Path(arguments[0]).resolve()
                    and run["identity"]["commandHash"] == run["commandHash"],
                    "PROCESS_MISMATCH", "GameTestの生成プロセスが一致しません")
            write_json(diagnostics / "process.json", run["identity"])

        def check_listeners(pids):
            for pid in pids:
                require(adapter.sockets(pid) == [], "LISTENER_MISMATCH", "GameTestのJVMまたは所有childが未予約の待受を開始しました")

        with (diagnostics / "console.log").open("xb") as log:
            try:
                process.start(arguments, cwd, log, run["nonce"], created)
            finally:
                run["creation"] = {"pid": process.pid, "stage": process.stage, "ownerGroup": process.group}
                expected = runtime._record(run, expected=expected)
            deadline = time.monotonic() + payload["timeoutSeconds"]
            while process.poll() is None:
                try:
                    actual = adapter.identity(process.pid)
                except OSError as error:
                    if process.poll() is not None:
                        break
                    if self._proof(run, bundle)["phase"] not in ("STOPPING", "STOPPED"):
                        raise
                    run["identityReadFailure"] = {"errorType": type(error).__name__,
                                                  "nativeError": getattr(error, "winerror", None) or getattr(error, "errno", None)}
                    check_listeners(set(process.members()) | {process.pid})
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
                    continue
                if actual is None and process.poll() is not None:
                    break
                runtime_process.require_identity(run["identity"], actual)
                check_listeners(set(process.members()) | {process.pid})
                require(time.monotonic() < deadline, "GAMETEST_TIMEOUT", "GameTestの期限を超過しました。自動killしません")
                time.sleep(0.1)
            expected = runtime._wait_exit(run, process, max(1, deadline - time.monotonic()), expected=expected, verify=check_listeners)
        result = {"runId": run["runId"], "artifactId": run["artifactId"], "templateId": None,
                  "exitCode": run["exitCode"], "jvmAndChildrenExited": True, "testsPassed": False,
                  "runtimeStopped": False, "snapshotClean": False}
        try:
            result.update(read_report(cwd / "report.xml"))
            result["testsPassed"] = result["testsPassed"] and run["exitCode"] == 0
        except ContractError:
            result["reportError"] = "MISSING_OR_INVALID_REPORT"
        try:
            proof = self._proof(run, bundle)
            saved = proof["saves"]
            result["seed"] = proof["startup"]["seed"]
            result["runtimeStopped"] = (run["exitCode"] == 0 and proof["phase"] == "STOPPED"
                and proof["saveScope"] == "gametest-shutdown" and proof["gameTestShutdownSaveCompleted"] is True
                and proof["vanillaShutdownTailReached"] is True and saved["failures"] == []
                and saved["pendingWriters"] == 0 and saved["drained"] is True and saved["success"] is True
                and proof["requiresSupervisorProcessExit"] is True and proof["requiresExternalWriterDrain"] is True)
            result["proofHash"] = digest(Path(run["proofPath"]).read_bytes())
        except (RegistryError, OSError, ValueError, KeyError):
            result["proofError"] = "MISSING_OR_INVALID_SHUTDOWN_PROOF"
        result["worldRetentionUntil"] = self.registry.clock() + (0 if result["testsPassed"] and result["runtimeStopped"] else 7 * 86400)
        result["resultsRetentionUntil"] = self.registry.clock() + 30 * 86400
        run.update(result, completedAt=self.registry.clock())
        expected = runtime._record(run, expected=expected)
        write_json(diagnostics / "result.json", result)
        return result
