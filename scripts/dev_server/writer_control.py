"""協調Managerの世代固定writer。I/Oと停止は同じinstance lockで直列化する。"""
import json
import os
from pathlib import Path
import re
import uuid

from .build_inputs import canonical, digest
from .contracts import absolute_path, validate_relative_path
from .local_ipc import MAX_MESSAGE, encode, make_private, plain_path
from .registry import RegistryError, fields, identifier, require
from .runtime_control import TOKEN
from . import runtime_process

SESSION = TOKEN | {"sessionId", "runId", "nonce"}
EDIT_DIRECTORIES = {"item_data", "ore_data", "shop_data", "recipe_data"}


class WriterControl:
    """登録の期限切れ・プロセス不在を終了証明へ変換しない。"""
    def __init__(self, registry, *, process_adapter=runtime_process):
        self.registry = registry
        self.process_adapter = process_adapter

    def summary(self, connection, row):
        sessions = connection.execute("SELECT status FROM writer_sessions WHERE instance=?", (row["id"],)).fetchall()
        held = sum(item["status"] != "CLOSED" for item in sessions)
        require(held == row["writers"], "RECOVERY_REQUIRED", "writer登録数と枠の記録が一致しません")
        run = connection.execute("SELECT record FROM runs WHERE id=?", (row["run_id"],)).fetchone()
        record = json.loads(run[0]) if run else None
        return {"writers": held, "writerSessions": [{"status": item["status"]} for item in sessions],
                "runId": row["run_id"], "nonce": record["nonce"] if record else None,
                "managementEndpoint": record["endpoints"]["management"] if record else None,
                "externalWritersVerified": held == 0,
                "cleanEligible": row["state"] == "LEASED_STOPPED" and bool(row["runtime_proof"])
                                 and held == 0 and not row["pending"] and not row["children"]}

    def _bound(self, connection, payload, *, closing=False):
        row = dict(self.registry._instance(connection, payload["instanceId"]))
        self.registry._token(row, payload)
        require(row["generation_bound"] and row["generation"] is not None and not row["pending"],
                "INVALID_STATE", "完成世代と未完了操作不在が必要です")
        require(row["state"] in ({"RUNNING", "LEASED_STOPPED"} if closing else {"RUNNING"}),
                "NOT_RUNNING", "writerの書込みは同じRUNNING runだけに許可します")
        recorded = connection.execute("SELECT record FROM runs WHERE id=?", (row["run_id"],)).fetchone()
        require(recorded is not None, "PROOF_REQUIRED", "起動記録がありません")
        run = json.loads(recorded[0])
        require(row["run_id"] == payload["runId"] and run["nonce"] == payload["nonce"],
                "STALE_RUN", "起動識別が一致しません")
        return row

    def _session(self, connection, payload, *, closing=False):
        row = self._bound(connection, payload, closing=closing)
        session = connection.execute("SELECT * FROM writer_sessions WHERE id=?", (payload["sessionId"],)).fetchone()
        require(session is not None and session["instance"] == row["id"]
                and session["lease"] == row["lease"] and session["epoch"] == row["epoch"]
                and session["generation"] == row["generation"] and session["run"] == row["run_id"]
                and session["nonce"] == payload["nonce"], "STALE_WRITER", "writerの接続識別が一致しません")
        return row, session

    def execute(self, command, payload, *, operation_id, dry_run=False):
        identifier(operation_id, "operationId")
        fields(payload, SESSION | ({"pid", "startMillis", "cwd"} if command == "writer-open" else
                                  {"request"} if command == "writer-io" else {"drained", "disconnected"}))
        for name in ("instanceId", "sessionId", "runId", "nonce"):
            identifier(payload[name], name)
        if command == "writer-open":
            require(type(payload["pid"]) is int and payload["pid"] > 0 and type(payload["startMillis"]) is int,
                    "INVALID_INPUT", "nativeプロセス開始識別が必要です")
            absolute_path(payload["cwd"])
        elif command == "writer-close":
            require(payload["drained"] is True and payload["disconnected"] is True,
                    "WRITER_PENDING", "保存・upload完了と接続終了の確認が必要です")
        else:
            try:
                self._validate_request(payload["request"])
            except RegistryError as error:
                error.rejected_before_effect = True
                raise
        fingerprint = digest(canonical({"command": command, "payload": payload}))
        read = command == "writer-io" and payload["request"]["action"] in {"read", "list", "server-read"}
        with self.registry.transaction(read_only=True) as connection:
            for table in ("operations", "runtime_operations", "reference_operations", "snapshot_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
            cached = connection.execute("SELECT * FROM writer_operations WHERE id=?", (operation_id,)).fetchone()
            if cached:
                require(cached["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "同じoperationIdへ別の要求を指定できません")
                row, session = self._session(connection, payload, closing=command == "writer-close")
                require(cached["status"] == "COMPLETED", "WRITER_PENDING", "結果不明のwriter操作を再実行しません")
                require(session["status"] == ("CLOSED" if command == "writer-close" else "OPEN"),
                        "STALE_WRITER", "完了済み接続へ古い操作を再適用できません")
            elif command == "writer-open":
                row = self._bound(connection, payload)
                require(not connection.execute("SELECT 1 FROM writer_sessions WHERE id=?", (payload["sessionId"],)).fetchone(),
                        "STALE_WRITER", "sessionIdは再利用できません")
            else:
                row, session = self._session(connection, payload, closing=command == "writer-close")
                require(session["status"] == "OPEN", "WRITER_PENDING", "writerは終了済みまたは結果不明です")
        # native照合・FS・通信をDB transaction内へ持ち込まない。
        if command == "writer-open" and cached is None:
            identity = self.process_adapter.identity(payload["pid"])
            require(identity is not None and identity["startMillis"] == payload["startMillis"]
                    and identity["cwd"] == str(Path(payload["cwd"]).resolve()),
                    "PROCESS_MISMATCH", "Managerの実開始識別・cwdが一致しません")
            supervisor_identity = self.process_adapter.identity(os.getpid())
            require(supervisor_identity is not None and identity["owner"] == supervisor_identity["owner"],
                    "PROCESS_MISMATCH", "Managerと監督のOS ownerが一致しません")
        else:
            runtime_process.require_identity(json.loads(session["identity"]), self.process_adapter.identity(session["pid"]))
        if cached:
            return dict(json.loads(cached["response"]), replayed=True, dryRun=dry_run)
        if command != "writer-close":
            self.registry.runtime._running(row)
        if dry_run:
            return {"dryRun": True, "wouldExecute": command}
        if read:
            try:
                return self._io(row, payload["request"], operation_id)
            except RegistryError as error:
                error.rejected_before_effect = True
                raise
            except PermissionError:
                raise RegistryError("PERMISSION_DENIED", "読取りを許可されていません", rejected_before_effect=True) from None
        if command == "writer-io":
            self._preflight(row, payload["request"])
        with self.registry.transaction() as connection:
            for table in ("operations", "runtime_operations", "reference_operations", "writer_operations", "snapshot_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが登録中に他の操作で使用されました")
            if command == "writer-open":
                row = self._bound(connection, payload)
                require(not connection.execute("SELECT 1 FROM writer_sessions WHERE id=?", (payload["sessionId"],)).fetchone(),
                        "STALE_WRITER", "sessionIdは再利用できません")
            else:
                row, session = self._session(connection, payload, closing=command == "writer-close")
                require(session["status"] == "OPEN" and not session["pending"], "WRITER_PENDING", "保存操作が未完了です")
            connection.execute("INSERT INTO writer_operations VALUES (?,?,?,?,?,?)",
                               (operation_id, fingerprint, payload["sessionId"], command, "PENDING", "{}"))
            if command == "writer-open":
                connection.execute("INSERT INTO writer_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   (payload["sessionId"], row["id"], row["lease"], row["epoch"], row["generation"],
                                    row["run_id"], payload["nonce"], payload["pid"], json.dumps(identity), "OPEN", 0))
                connection.execute("UPDATE instances SET writers=writers+1,clean_proof=NULL WHERE id=?", (row["id"],))
            elif command == "writer-io":
                connection.execute("UPDATE writer_sessions SET pending=1 WHERE id=?", (payload["sessionId"],))
            else:
                require(not session["pending"], "WRITER_PENDING", "保存操作の完了が不明です")
                connection.execute("UPDATE writer_sessions SET status='CLOSED' WHERE id=?", (payload["sessionId"],))
                connection.execute("UPDATE instances SET writers=writers-1 WHERE id=?", (row["id"],))
            if command != "writer-io":
                response = {"sessionId": payload["sessionId"], "closed": command == "writer-close"}
                connection.execute("UPDATE writer_operations SET status='COMPLETED',response=? WHERE id=?",
                                   (json.dumps(response), operation_id))
                return response
        try:
            response = self._io(row, payload["request"], operation_id)
            encode(response)
            runtime_process.require_identity(json.loads(session["identity"]), self.process_adapter.identity(session["pid"]))
            with self.registry.transaction() as connection:
                self._session(connection, payload)
                connection.execute("UPDATE writer_sessions SET pending=0 WHERE id=?", (payload["sessionId"],))
                recorded = {key: value for key, value in response.items() if key != "apiResponse"}
                connection.execute("UPDATE writer_operations SET status='COMPLETED',response=? WHERE id=?",
                                   (json.dumps(recorded), operation_id))
            return response
        except BaseException:
            # 記録失敗も含めpendingを保全する。応答不明を成功・自動再試行へ変えない。
            with self.registry.transaction() as connection:
                connection.execute("UPDATE writer_sessions SET status='UNKNOWN' WHERE id=?", (payload["sessionId"],))
            raise

    def _preflight(self, row, request):
        """日常的な拒否を保存受付前に返す。受付後のOSエラーは完了不明のままにする。"""
        if request["action"] == "api":
            return
        try:
            base = self.registry.runtime._active_generation(row) / "config/SushiEricServerMod"
            target = base / request["path"]
            plain_path(target)
            action = request["action"]
            if action in {"move", "delete"}:
                require(target.is_file(), "FILE_NOT_FOUND", "操作対象ファイルがありません")
            elif action == "delete-directory":
                require(target.is_dir(), "FILE_NOT_FOUND", "操作対象ディレクトリがありません")
            elif action == "mkdir":
                require(not target.exists() or target.is_dir(), "ALREADY_EXISTS", "同名ファイルが存在します")
            if action == "move":
                destination = base / request["destination"]
                plain_path(destination)
                require(not destination.exists(), "ALREADY_EXISTS", "移動先が存在します")
            if action == "write" or (action == "move" and "text" in request):
                self.registry.runtime._space(len(request["text"].encode("utf-8")))
        except RegistryError as error:
            error.rejected_before_effect = True
            raise
        except PermissionError:
            raise RegistryError("PERMISSION_DENIED", "操作対象を検査できません", rejected_before_effect=True) from None

    def _validate_request(self, request):
        require(type(request) is dict and type(request.get("action")) is str, "INVALID_INPUT", "writer要求が不正です")
        action = request["action"]
        shape = {"read": {"path"}, "server-read": {"path"}, "list": {"path"}, "write": {"path", "text"},
                 "move": {"path", "destination"}, "mkdir": {"path"}, "delete": {"path"},
                 "delete-directory": {"path"}, "api": {"message", "timeoutSeconds"}}
        require(action in shape, "INVALID_INPUT", "未対応のwriter要求です")
        fields(request, {"action"} | shape[action] | ({"text"} if action == "move" and "text" in request else set()))
        if action == "api":
            from .writer_api import validate_message
            validate_message(request["message"], request["timeoutSeconds"])
            return
        require(type(request["path"]) is str, "INVALID_INPUT", "相対パスが必要です")
        if not (action == "list" and request["path"] == ""):
            self._allowed_path(request["path"], server=action == "server-read", write=action not in {"read", "list", "server-read"})
        if action == "move":
            self._allowed_path(request["destination"], write=True)
        if action == "write" or (action == "move" and "text" in request):
            require(type(request["text"]) is str, "INVALID_INPUT", "UTF-8テキストが必要です")
            encode(request)

    def _allowed_path(self, name, *, server=False, write=False):
        validate_relative_path(name)
        # 秘密・JVMが保存するplayer/stock/history・実行rootは編集対象ではない。
        if server:
            require(name in {"config/SushiEricServerMod/manager-compat.json", "usercache.json"}, "PATH_DENIED", "既知の読取り専用serverファイル以外は禁止です")
        else:
            history = re.fullmatch(r"player_data(?:/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:/(?:achievements\.yml|money_history(?:/\d{4}-\d{2}-\d{2}\.yml)?))?)?", name)
            shop_history = re.fullmatch(r"shop_history(?:/\d{4}-\d{2}-\d{2}\.yml)?", name)
            require(name == "config.yml" or name.split("/")[0] in EDIT_DIRECTORIES
                    or (not write and (name == "manager-compat.json" or history or shop_history)),
                    "PATH_DENIED", "Manager編集対象外のパスです")
        require(not re.search(r'[<>"|?*]', name) and all(not part.endswith((" ", "."))
                and not re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part) for part in name.split("/")),
                "PATH_DENIED", "OSで別パスへ解釈される名前は禁止です")

    def _io(self, row, request, operation_id):
        generation = self.registry.runtime._active_generation(row)
        action = request["action"]
        if action == "api":
            from .writer_api import exchange
            run = self.registry.runtime._load_run(row)
            response = exchange(run["endpoints"]["management"], request["message"], operation_id,
                                request["timeoutSeconds"], verify=lambda: self.registry.runtime._running(row))
            return {"completed": True, "apiResponse": response, "responseSha256": digest(canonical(response))}
        base = generation if action == "server-read" else generation / "config/SushiEricServerMod"
        target = base / request["path"]
        plain_path(target)
        if action in {"read", "server-read"}:
            require(target.is_file(), "FILE_NOT_FOUND", "編集ファイルがありません")
            require(target.stat().st_size < MAX_MESSAGE, "MESSAGE_TOO_LARGE", "IPC上限を超えるファイルは読み込めません")
            response = {"text": target.read_text(encoding="utf-8")}
            encode(response)
            return response
        if action == "list":
            if not target.exists():
                return {"entries": []}
            entries = []
            for path in sorted(target.iterdir()):
                plain_path(path)
                if request["path"] or path.name in EDIT_DIRECTORIES or path.name in {"config.yml", "manager-compat.json", "player_data", "shop_history"}:
                    entries.append({"name": path.name, "isDirectory": path.is_dir()})
            response = {"entries": entries}
            encode(response)
            return response
        if action == "write":
            self.registry.runtime._space(len(request["text"].encode("utf-8")))
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(".writer-" + uuid.uuid4().hex)
            with temporary.open("xb") as handle:
                handle.write(request["text"].encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            make_private(temporary)
            plain_path(target)
            os.replace(temporary, target)
        elif action == "move":
            destination = base / request["destination"]
            plain_path(destination)
            require(not destination.exists(), "ALREADY_EXISTS", "移動先が存在します")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if "text" in request:
                with destination.open("xb") as handle:
                    handle.write(request["text"].encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                make_private(destination)
                target.unlink()
            else:
                target.rename(destination)
        elif action == "mkdir":
            target.mkdir(parents=True, exist_ok=True)
        elif action == "delete":
            require(target.is_file(), "FILE_NOT_FOUND", "削除対象ファイルがありません")
            target.unlink()
        else:
            require(target.is_dir(), "FILE_NOT_FOUND", "削除対象ディレクトリがありません")
            paths, directories = [], [target]
            while directories:
                directory = directories.pop()
                for path in directory.iterdir():
                    plain_path(path)
                    paths.append(path)
                    if path.is_dir():
                        directories.append(path)
            for path in sorted(paths, key=lambda entry: len(entry.parts), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            target.rmdir()
        return {"completed": True}
