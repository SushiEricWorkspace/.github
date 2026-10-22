"""協調Managerの世代固定writer。I/Oは短い予約で停止と競合を検出する。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .build_inputs import canonical, digest
from .contracts import ContractError, absolute_path, validate_relative_path
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
        self.database_proxies = {}
        self.database_lock = threading.RLock()

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
                                  {"request"} if command == "writer-io" else set() if command == "writer-check" else {"drained", "disconnected"}))
        for name in ("instanceId", "sessionId", "runId", "nonce"):
            identifier(payload[name], name)
        if command == "writer-open":
            require(type(payload["pid"]) is int and payload["pid"] > 0 and type(payload["startMillis"]) is int,
                    "INVALID_INPUT", "nativeプロセス開始識別が必要です")
            absolute_path(payload["cwd"])
        elif command == "writer-close":
            require(payload["drained"] is True and payload["disconnected"] is True,
                    "WRITER_PENDING", "保存・upload完了と接続終了の確認が必要です")
        elif command == "writer-io":
            try:
                if type(payload["request"]) is dict and payload["request"].get("action") == "database":
                    payload = deepcopy(payload)
                self._validate_request(payload["request"])
            except ContractError:
                raise RegistryError("INVALID_INPUT", "操作対象のパス形式が不正です", rejected_before_effect=True) from None
            except RegistryError as error:
                error.rejected_before_effect = True
                raise
        fingerprint = digest(canonical({"command": command, "payload": payload}))
        if command == "writer-io" and payload["request"]["action"] == "database":
            return self._database_execute(payload, operation_id, fingerprint, dry_run)
        if command == "writer-close" and self._has_database(payload):
            return self._database_close(payload, operation_id, fingerprint, dry_run)
        read = command == "writer-io" and payload["request"]["action"] in {"read", "list", "server-read"}
        with self.registry.runtime._transaction(payload["instanceId"], read_only=True) as connection:
            for table in ("operations", "runtime_operations", "reference_operations", "snapshot_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが他の操作で使用されています")
            cached = connection.execute("SELECT * FROM writer_operations WHERE id=?", (operation_id,)).fetchone()
            require(not connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'",
                                           (payload["instanceId"],)).fetchone(), "WRITER_PENDING", "writer callが実行中です")
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
            with self.registry.runtime._transaction(payload["instanceId"], read_only=True) as connection:
                self._session(connection, payload, closing=command == "writer-close")
                require(not connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'",
                                               (row["id"],)).fetchone(), "WRITER_PENDING", "writer callが実行中です")
            return dict(json.loads(cached["response"]), replayed=True, dryRun=dry_run)
        if command != "writer-close":
            self.registry.runtime._running(row)
        if command == "writer-check":
            with self.registry.runtime._transaction(payload["instanceId"], read_only=True) as connection:
                _, checked = self._session(connection, payload)
                require(checked["status"] == "OPEN" and not checked["pending"], "WRITER_PENDING", "writerが変更されました")
                require(not connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'",
                                               (row["id"],)).fetchone(), "WRITER_PENDING", "writer callが実行中です")
            return {"sessionId": payload["sessionId"], "open": True}
        if dry_run:
            return {"dryRun": True, "wouldExecute": command}
        if command == "writer-io" and not read:
            self._preflight(row, payload["request"])
        with self.registry.runtime._transaction(payload["instanceId"]) as connection:
            for table in ("operations", "runtime_operations", "reference_operations", "writer_operations", "snapshot_operations"):
                require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (operation_id,)).fetchone(),
                        "OPERATION_CONFLICT", "operationIdが登録中に他の操作で使用されました")
            require(not connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'",
                                           (payload["instanceId"],)).fetchone(), "WRITER_PENDING", "writer callが実行中です")
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
                self._claim(connection, row, payload, operation_id, fingerprint)
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
        claimed = True
        try:
            response = self._io(row, payload["request"], operation_id)
            encode(response)
            runtime_process.require_identity(json.loads(session["identity"]), self.process_adapter.identity(session["pid"]))
            with self.registry.runtime._transaction(payload["instanceId"]) as connection:
                self._reserved(connection, payload, operation_id, fingerprint, command)
                connection.execute("UPDATE writer_sessions SET pending=0 WHERE id=?", (payload["sessionId"],))
                if read:
                    self._delete_claim(connection, row, payload, operation_id, fingerprint)
                    # 読取bodyは保存せず、同IDの再提示も従来どおり新しい読取とする。
                    connection.execute("DELETE FROM writer_operations WHERE id=?", (operation_id,))
                else:
                    recorded = {key: value for key, value in response.items() if key != "apiResponse"}
                    connection.execute("UPDATE writer_operations SET status='COMPLETED',response=? WHERE id=?",
                                       (json.dumps(recorded), operation_id))
            if read:
                claimed = False
            return response
        except BaseException as error:
            rejected = read and isinstance(error, (RegistryError, PermissionError))
            with self.registry.runtime._transaction(payload["instanceId"]) as connection:
                self._reserved(connection, payload, operation_id, fingerprint, command)
                if rejected:
                    self._delete_claim(connection, row, payload, operation_id, fingerprint)
                    connection.execute("DELETE FROM writer_operations WHERE id=?", (operation_id,))
                    connection.execute("UPDATE writer_sessions SET pending=0 WHERE id=?", (payload["sessionId"],))
                else:
                    connection.execute("UPDATE writer_sessions SET status='UNKNOWN' WHERE id=?", (payload["sessionId"],))
            if rejected:
                claimed = False
            if isinstance(error, RegistryError) and rejected:
                error.rejected_before_effect = True
            if isinstance(error, PermissionError) and read:
                raise RegistryError("PERMISSION_DENIED", "読取りを許可されていません", rejected_before_effect=True) from None
            raise
        finally:
            if claimed:
                self._release_call(row, payload, operation_id, fingerprint)

    def _reserved(self, connection, payload, operation_id, fingerprint, command):
        row, session = self._session(connection, payload, closing=command == "writer-close")
        operation = connection.execute("SELECT * FROM writer_operations WHERE id=?", (operation_id,)).fetchone()
        require(session["pending"] and operation is not None and operation["session"] == payload["sessionId"]
                and operation["fingerprint"] == fingerprint and operation["command"] == command
                and operation["status"] == "PENDING", "WRITER_PENDING", "同じ未完了writer操作ではありません")
        return row, session

    def _claim(self, connection, row, payload, operation_id, fingerprint):
        require(not connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'",
                                       (row["id"],)).fetchone(), "WRITER_PENDING", "同じinstanceのwriter callが実行中です")
        operation = connection.execute("SELECT * FROM writer_operations WHERE id=?", (operation_id,)).fetchone()
        require(operation is not None and operation["session"] == payload["sessionId"]
                and operation["fingerprint"] == fingerprint, "OPERATION_CONFLICT", "callの正本要求が一致しません")
        connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)",
                           (operation_id, "writer-call:" + fingerprint, row["id"], row["lease"], row["epoch"]))

    def _claim_existing(self, payload, operation_id, fingerprint):
        with self.registry.runtime._transaction(payload["instanceId"]) as connection:
            row, _ = self._session(connection, payload, closing=True)
            self._claim(connection, row, payload, operation_id, fingerprint)
        return row

    def _delete_claim(self, connection, row, payload, operation_id, fingerprint):
        current = self.registry._instance(connection, row["id"])
        self.registry.runtime._binding(current, row)
        session = connection.execute("SELECT * FROM writer_sessions WHERE id=?", (payload["sessionId"],)).fetchone()
        operation = connection.execute("SELECT * FROM writer_operations WHERE id=?", (operation_id,)).fetchone()
        require(session is not None and all(session[key] == value for key, value in
                (("instance", row["id"]), ("lease", row["lease"]), ("epoch", row["epoch"]),
                 ("generation", row["generation"]), ("run", row["run_id"]), ("nonce", payload["nonce"])))
                and operation is not None and operation["session"] == payload["sessionId"]
                and operation["fingerprint"] == fingerprint, "STALE_OPERATION", "解除するcallのbindingが変更されました")
        deleted = connection.execute("DELETE FROM reservations WHERE operation=? AND reference=? AND instance=? AND lease=? AND epoch=?",
                                     (operation_id, "writer-call:" + fingerprint, row["id"], row["lease"], row["epoch"]))
        require(deleted.rowcount == 1, "STALE_OPERATION", "取得したcallだけを解除できます")

    def _release_call(self, row, payload, operation_id, fingerprint):
        with self.registry.runtime._transaction(row["id"]) as connection:
            self._delete_claim(connection, row, payload, operation_id, fingerprint)

    def _database_path(self, row, session_id):
        return self.registry.runtime._paths(row['id']) / 'diagnostics' / row['run_id'] / ('db-writer-' + session_id)

    def _has_database(self, payload):
        with self.registry.runtime._transaction(payload["instanceId"], read_only=True) as connection:
            row, _ = self._session(connection, payload, closing=True)
        path = self._database_path(row, payload['sessionId'])
        plain_path(path)
        return payload['sessionId'] in self.database_proxies or path.exists()

    def _database_verify(self, binding):
        with self.registry.runtime._transaction(binding["instanceId"], read_only=True) as connection:
            row, session = self._session(connection, binding, closing=True)
        runtime_process.require_identity(json.loads(session['identity']), self.process_adapter.identity(session['pid']))
        self.registry.runtime._running(row)

    def _database_state(self, payload, operation_id, fingerprint, *, closing=False):
        with self.registry.runtime._transaction(payload["instanceId"], read_only=True) as connection:
            row, session = self._session(connection, payload, closing=closing)
            for table in ('operations', 'runtime_operations', 'reference_operations', 'snapshot_operations'):
                require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                        'OPERATION_CONFLICT', 'operationIdが他の操作で使用されています')
            cached = connection.execute('SELECT * FROM writer_operations WHERE id=?', (operation_id,)).fetchone()
            require(not connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'",
                                           (row['id'],)).fetchone(), 'WRITER_PENDING', 'writer callが実行中です')
            if cached:
                require(cached['fingerprint'] == fingerprint and cached['session'] == payload['sessionId'],
                        'OPERATION_CONFLICT', '同じoperationIdへ別の要求を指定できません')
            else:
                require(session['status'] in ({'OPEN', 'UNKNOWN'} if closing else {'OPEN'}) and (closing or not session['pending']),
                        'WRITER_PENDING', '新DB要求を受け付けられません')
        runtime_process.require_identity(json.loads(session['identity']), self.process_adapter.identity(session['pid']))
        if not (closing and cached and cached['status'] == 'COMPLETED'):
            self.registry.runtime._running(row)
        return row, session, cached

    def _database_accept(self, payload, operation_id, fingerprint, command):
        with self.registry.runtime._transaction(payload["instanceId"]) as connection:
            row, session = self._session(connection, payload, closing=command == 'writer-close')
            require(session['status'] in ({'OPEN', 'UNKNOWN'} if command == 'writer-close' else {'OPEN'})
                    and (command == 'writer-close' or not session['pending']),
                    'WRITER_PENDING', '新DB要求を受け付けられません')
            for table in ('operations', 'runtime_operations', 'reference_operations', 'snapshot_operations', 'writer_operations'):
                require(not connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (operation_id,)).fetchone(),
                        'OPERATION_CONFLICT', 'operationIdが受付中に使用されました')
            connection.execute('INSERT INTO writer_operations VALUES (?,?,?,?,?,?)',
                               (operation_id, fingerprint, payload['sessionId'], command, 'PENDING', '{}'))
            self._claim(connection, row, payload, operation_id, fingerprint)
            connection.execute('UPDATE writer_sessions SET pending=1,status=? WHERE id=?',
                               ('CLOSING' if command == 'writer-close' else 'OPEN', payload['sessionId']))
        return row

    def _database_record(self, payload, response, *, phase, exit_proof=None):
        status = response['status']
        closing = exit_proof is not None
        terminal = closing or phase == 'execute' and status in {'RESULT', 'REJECTED'} or phase == 'bootstrap' and status == 'REJECTED'
        metadata = {'sessionId': payload['sessionId'], 'completed': terminal, 'proxyStatus': status,
                    'phase': phase, 'proxyResponseSha256': digest(canonical(response))}
        if 'response' in response:
            metadata['responseSha256'] = digest(canonical(response['response']))
        if status == 'REJECTED':
            metadata.update(rejectedBeforeEffect=True, code='DB_CONTEXT_REJECTED' if phase == 'bootstrap' else 'DB_REQUEST_REJECTED')
        if closing:
            metadata.update(closed=True, **exit_proof)
        with self.registry.runtime._transaction(payload["instanceId"]) as connection:
            row, session = self._session(connection, payload, closing=True)
            operation = connection.execute('SELECT * FROM writer_operations WHERE id=?', (response['operationId'],)).fetchone()
            require(operation is not None and operation['session'] == payload['sessionId']
                    and operation['fingerprint'] == response['requestHash'], 'OPERATION_CONFLICT', '私設応答の正本要求が一致しません')
            require(operation['command'] in {'writer-io', 'writer-close'}, 'OPERATION_CONFLICT', 'DB操作種別が一致しません')
            if operation['status'] == 'COMPLETED':
                previous = json.loads(operation['response'])
                require(previous.get('proxyResponseSha256') == metadata['proxyResponseSha256'],
                        'API_PROTOCOL', '確定済み応答を別の応答へ変更しません')
                return previous
            require(operation['status'] == 'PENDING' and session['pending'], 'WRITER_PENDING', '未完了のDB操作ではありません')
            if operation['command'] == 'writer-close':
                terminal = closing
                metadata['completed'] = closing
            require(not closing or operation['command'] == 'writer-close' and session['status'] == 'CLOSING',
                    'WRITER_PENDING', 'writer終了の正本が一致しません')
            connection.execute('UPDATE writer_operations SET status=?,response=? WHERE id=?',
                               ('COMPLETED' if terminal else 'PENDING', json.dumps(metadata), response['operationId']))
            if closing:
                require(not connection.execute("SELECT 1 FROM writer_operations WHERE session=? AND status!='COMPLETED' AND id!=?",
                                               (payload['sessionId'], response['operationId'])).fetchone(),
                        'WRITER_PENDING', '未完了のwriter操作を残して終了しません')
                connection.execute("UPDATE writer_sessions SET status='CLOSED',pending=0 WHERE id=?", (payload['sessionId'],))
                connection.execute('UPDATE instances SET writers=writers-1 WHERE id=?', (row['id'],))
            elif terminal and session['status'] != 'CLOSING':
                connection.execute("UPDATE writer_sessions SET status='OPEN',pending=0 WHERE id=?", (payload['sessionId'],))
            elif status == 'UNKNOWN' and session['status'] != 'CLOSING':
                connection.execute("UPDATE writer_sessions SET status='UNKNOWN' WHERE id=?", (payload['sessionId'],))
        return metadata

    def _database_unknown(self, payload, operation_id):
        with self.registry.runtime._transaction(payload["instanceId"]) as connection:
            operation = connection.execute('SELECT * FROM writer_operations WHERE id=?', (operation_id,)).fetchone()
            if operation and operation['status'] != 'COMPLETED':
                self._session(connection, payload, closing=True)
                require(operation['session'] == payload['sessionId'], 'OPERATION_CONFLICT', '失敗する正本sessionが一致しません')
                connection.execute("UPDATE writer_sessions SET status=CASE WHEN status='CLOSING' THEN status ELSE 'UNKNOWN' END WHERE id=? AND status!='CLOSED'",
                                   (payload['sessionId'],))

    @staticmethod
    def _database_public(metadata, response=None):
        available = metadata.get('phase') == 'execute' and response is not None and 'response' in response
        value = dict(metadata, responseAvailable=available)
        if available:
            value['apiResponse'] = response['response']
        return value

    def _database_ack(self, payload, proxy):
        require(proxy.request_bytes is not None and proxy.response_bytes is not None,
                'WRITER_PENDING', 'ackする私設応答がありません')
        request = json.loads(proxy.request_bytes)
        with self.registry.runtime._transaction(payload["instanceId"], read_only=True) as connection:
            operation = connection.execute('SELECT * FROM writer_operations WHERE id=?', (request['operationId'],)).fetchone()
            require(operation is not None and operation['session'] == payload['sessionId'],
                    'OPERATION_CONFLICT', 'ackする正本要求がありません')
            metadata = json.loads(operation['response'])
        response_hash = digest(proxy.response_bytes)
        require(metadata.get('proxyResponseSha256') == response_hash,
                'WRITER_PENDING', '正本へ未記録の応答を解放しません')
        proxy.acknowledge(response_hash)

    def _database_interrupt_bootstrap(self, payload, proxy, operation_id, fingerprint):
        proxy.verify()
        require(proxy.started, 'WRITER_PENDING', 'bootstrap本人が未確認です')
        if proxy.request_bytes is None:
            require(proxy.bound and proxy.response_bytes is None,
                    'WRITER_PENDING', 'OPEN ack後の未送信状態が未確認です')
        else:
            request = json.loads(proxy.request_bytes)
            response = json.loads(proxy.response_bytes) if proxy.response_bytes is not None else {}
            require(request.get('command') == 'open' and request.get('operationId') == operation_id
                    and request.get('requestHash') == fingerprint and response.get('status') == 'OPEN'
                    and response.get('operationId') == operation_id and response.get('requestHash') == fingerprint,
                    'WRITER_PENDING', '元DB要求の未送信を確認できません')
        with self.registry.runtime._transaction(payload["instanceId"]) as connection:
            _, session = self._session(connection, payload, closing=True)
            operation = connection.execute('SELECT * FROM writer_operations WHERE id=?', (operation_id,)).fetchone()
            require(operation is not None and operation['session'] == payload['sessionId']
                    and operation['fingerprint'] == fingerprint, 'OPERATION_CONFLICT', 'bootstrap要求が一致しません')
            metadata = json.loads(operation['response'])
            require(metadata.get('phase') == 'bootstrap' and metadata.get('proxyStatus') == 'OPEN',
                    'WRITER_PENDING', '既知OPENの正本がありません')
            if proxy.response_bytes is not None:
                require(metadata.get('proxyResponseSha256') == digest(proxy.response_bytes),
                        'WRITER_PENDING', '既知OPENの保持応答が正本と一致しません')
            metadata.update(completed=True, rejectedBeforeEffect=True, code='DB_BOOTSTRAP_INTERRUPTED')
            connection.execute("UPDATE writer_operations SET status='COMPLETED',response=? WHERE id=?",
                               (json.dumps(metadata), operation_id))
            if session['status'] != 'CLOSING':
                connection.execute("UPDATE writer_sessions SET status='OPEN',pending=0 WHERE id=?", (payload['sessionId'],))
        if proxy.request_bytes is not None:
            self._database_ack(payload, proxy)
        return metadata

    def _database_collect(self, payload, proxy, deadline):
        request = json.loads(proxy.request_bytes)
        phase = 'bootstrap' if request['command'] == 'open' else 'execute'
        response = proxy.receive(deadline)
        if response['status'] == 'PENDING' and phase == 'execute':
            self._database_record(payload, response, phase=phase)
            self._database_ack(payload, proxy)
            resolve = {key: request[key] for key in ('operationId', 'requestHash')}
            response = proxy.begin(dict(resolve, command='resolve', timeoutMillis=30_000), deadline)
        metadata = self._database_record(payload, response, phase=phase)
        if phase == 'bootstrap' and response['status'] == 'OPEN':
            metadata = self._database_interrupt_bootstrap(payload, proxy, request['operationId'], request['requestHash'])
        return metadata, response

    def _database_execute(self, payload, operation_id, fingerprint, dry_run):
        claimed = False
        try:
            from .database_proxy import DatabaseProxy
            from .database_proxy_bundle import proxy_command
            row, session, cached = self._database_state(payload, operation_id, fingerprint)
            require(session['status'] in {'OPEN', 'UNKNOWN', 'CLOSING'} and (cached is not None or session['status'] == 'OPEN'),
                    'STALE_WRITER', 'DB writerは終了済みです')
            proxy = self.database_proxies.get(payload['sessionId'])
            if cached and cached['status'] == 'COMPLETED':
                response = None
                if not dry_run and proxy and proxy.request_bytes and json.loads(proxy.request_bytes)['operationId'] == operation_id:
                    row = self._claim_existing(payload, operation_id, fingerprint)
                    claimed = True
                    response = proxy.receive(time.monotonic())
                    require(digest(canonical(response)) == json.loads(cached['response']).get('proxyResponseSha256'),
                            'API_PROTOCOL', '保持応答のhashが正本と一致しません')
                return dict(self._database_public(json.loads(cached['response']), response), replayed=True, dryRun=dry_run)
            if dry_run:
                return {'dryRun': True, 'wouldExecute': 'writer-io'}
            deadline = time.monotonic() + payload['request']['timeoutSeconds']
            if cached:
                row = self._claim_existing(payload, operation_id, fingerprint)
                claimed = True
                require(proxy is not None, 'WRITER_PENDING', '失われたproxy本人を再生成しません')
                try:
                    if proxy.request_bytes is None:
                        metadata = self._database_interrupt_bootstrap(payload, proxy, operation_id, fingerprint)
                        return self._database_public(metadata)
                    require(json.loads(proxy.request_bytes)['operationId'] == operation_id,
                            'WRITER_PENDING', '別のproxy要求を再送しません')
                    metadata, response = self._database_collect(payload, proxy, deadline)
                    return self._database_public(metadata, response)
                except BaseException:
                    self._database_unknown(payload, operation_id)
                    raise
            bundle = self.registry.runtime._active_generation(row) / 'runtime'
            previous_proxy = proxy
            if proxy is None:
                try:
                    proxy_command(bundle)
                except (ContractError, OSError, ValueError):
                    raise RegistryError('ADAPTER_UNAVAILABLE', '固定DB proxyが同梱されていません', rejected_before_effect=True) from None
                path = self._database_path(row, payload['sessionId'])
                plain_path(path)
                require(not path.exists(), 'WRITER_PENDING', '過去のproxy生成証拠を上書きしません')
            binding = {key: payload[key] for key in SESSION}
            with self.database_lock:
                proxy = self.database_proxies.get(payload['sessionId'])
                created_proxy = False
                if proxy is None:
                    require(previous_proxy is None, 'WRITER_PENDING', '保持proxyが変更されました')
                    require(len(self.database_proxies) < 4, 'DB_PROXY_LIMIT', 'DB proxyの4枠が使用中です')
                    proxy = DatabaseProxy(bundle, path, binding, verify=lambda: self._database_verify(binding),
                                          process_adapter=self.process_adapter)
                    self.database_proxies[payload['sessionId']] = proxy
                    created_proxy = True
                try:
                    require(not proxy.started or proxy.bound, 'WRITER_PENDING', '未確定のbootstrapを再実行しません')
                    row = self._database_accept(payload, operation_id, fingerprint, 'writer-io')
                    claimed = True
                except BaseException:
                    if created_proxy and not proxy.started:
                        self.database_proxies.pop(payload['sessionId'], None)
                    raise
            try:
                if proxy.started and proxy.request_bytes is not None:
                    self._database_ack(payload, proxy)
                common = {'operationId': operation_id, 'requestHash': fingerprint,
                          'timeoutMillis': payload['request']['timeoutSeconds'] * 1000}
                if not proxy.started:
                    proxy.start()
                    run = self.registry.runtime._load_run(row)
                    endpoint = run['endpoints']['management']
                    require(endpoint['address'] == '127.0.0.1' and endpoint['protocol'] == 'tcp',
                            'ENDPOINT_MISMATCH', 'Managementは所有loopbackだけを使用します')
                    response = proxy.begin(dict(common, command='open', port=endpoint['port'], instanceId=row['id'],
                                                generationId=row['generation'], startNonce=run['nonce']), deadline)
                    metadata = self._database_record(payload, response, phase='bootstrap')
                    if response['status'] != 'OPEN':
                        return self._database_public(metadata, response)
                    self._database_ack(payload, proxy)
                response = proxy.begin(dict(common, command='execute', message=payload['request']['message']), deadline)
                metadata = self._database_record(payload, response, phase='execute')
                return self._database_public(metadata, response)
            except BaseException:
                self._database_unknown(payload, operation_id)
                raise
        finally:
            if claimed:
                self._release_call(row, payload, operation_id, fingerprint)

    def _database_close(self, payload, operation_id, fingerprint, dry_run):
        claimed = False
        try:
            row, session, cached = self._database_state(payload, operation_id, fingerprint, closing=True)
            if cached and cached['status'] == 'COMPLETED':
                with self.database_lock:
                    proxy = self.database_proxies.get(payload['sessionId'])
                    if proxy is not None and proxy.exited:
                        self.database_proxies.pop(payload['sessionId'], None)
                return dict(json.loads(cached['response']), replayed=True, dryRun=dry_run)
            if dry_run:
                return {'dryRun': True, 'wouldExecute': 'writer-close'}
            if not cached:
                row = self._database_accept(payload, operation_id, fingerprint, 'writer-close')
            else:
                row = self._claim_existing(payload, operation_id, fingerprint)
            claimed = True
            proxy = self.database_proxies.get(payload['sessionId'])
            require(proxy is not None, 'WRITER_PENDING', '以前のproxy本人とpipeを引き継ぎません')
            deadline = time.monotonic() + 30
            try:
                if proxy.request_bytes is not None:
                    request = json.loads(proxy.request_bytes)
                    if request['operationId'] != operation_id:
                        metadata, response = self._database_collect(payload, proxy, deadline)
                        if not metadata['completed']:
                            return {'sessionId': payload['sessionId'], 'closed': False, 'completed': False,
                                    'proxyStatus': response['status']}
                        if proxy.request_bytes is not None:
                            self._database_ack(payload, proxy)
                    else:
                        response = proxy.receive(deadline)
                        if response['status'] not in {'CLOSED', 'UNBOUND_CLOSED'}:
                            self._database_record(payload, response, phase='close')
                            if response['status'] != 'PENDING':
                                return {'sessionId': payload['sessionId'], 'closed': False, 'completed': False,
                                        'proxyStatus': response['status']}
                            self._database_ack(payload, proxy)
                        else:
                            proof = proxy.finish_exit(deadline)
                            metadata = self._database_record(payload, response, phase='close', exit_proof=proof)
                            with self.database_lock:
                                self.database_proxies.pop(payload['sessionId'], None)
                            return metadata
                with self.registry.runtime._transaction(payload["instanceId"], read_only=True) as connection:
                    pending = connection.execute("SELECT * FROM writer_operations WHERE session=? AND command='writer-io' AND status!='COMPLETED'",
                                                 (payload['sessionId'],)).fetchall()
                require(len(pending) <= 1, 'WRITER_PENDING', '未完了のDB要求が一意ではありません')
                if pending:
                    self._database_interrupt_bootstrap(payload, proxy, pending[0]['id'], pending[0]['fingerprint'])
                response = proxy.begin({'command': 'close', 'operationId': operation_id,
                                        'requestHash': fingerprint, 'timeoutMillis': 30_000}, deadline)
                if response['status'] not in {'CLOSED', 'UNBOUND_CLOSED'}:
                    metadata = self._database_record(payload, response, phase='close')
                    return dict(metadata, closed=False)
                proof = proxy.finish_exit(deadline)
                metadata = self._database_record(payload, response, phase='close', exit_proof=proof)
                with self.database_lock:
                    self.database_proxies.pop(payload['sessionId'], None)
                return metadata
            except BaseException:
                self._database_unknown(payload, operation_id)
                raise
        finally:
            if claimed:
                self._release_call(row, payload, operation_id, fingerprint)

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

    @staticmethod
    def _validate_database_message(message, timeout):
        require(type(timeout) is int and 1 <= timeout <= 30, 'INVALID_INPUT', 'DB待機時間は1〜30秒です')
        shape = {'database_context_request': set(), 'database_history_start': {'binding', 'filter'},
                 'database_history_page': {'handle', 'offset', 'limit'}, 'database_history_end': {'handle'},
                 'database_product_request': {'binding', 'request'}, 'money_history_save': {'playerUuid'}}
        require(type(message) is dict and type(message.get('type')) is str and message['type'] in shape,
                'INVALID_INPUT', 'DB専用の型付き要求だけを受け付けます')
        require(set(message) <= {'type', 'nonce'} | shape[message['type']],
                'INVALID_INPUT', '未知のDB要求項目があります')
        require(message.get('nonce') is None or type(message['nonce']) is str,
                'INVALID_INPUT', 'nonce形式が不正です')
        try:
            size = len(encode(message))
        except (ValueError, UnicodeError):
            raise RegistryError('INVALID_INPUT', 'DB要求は有限値のUTF-8 JSONだけを使用します') from None
        require(size <= 60 * 1024, 'MESSAGE_TOO_LARGE', 'Management要求は60KiBまでです')

    def _validate_request(self, request):
        require(type(request) is dict and type(request.get("action")) is str, "INVALID_INPUT", "writer要求が不正です")
        action = request["action"]
        shape = {"read": {"path"}, "server-read": {"path"}, "list": {"path"}, "write": {"path", "text"},
                 "move": {"path", "destination"}, "mkdir": {"path"}, "delete": {"path"},
                 "delete-directory": {"path"}, "api": {"message", "timeoutSeconds"}, "database": {"message", "timeoutSeconds"}}
        require(action in shape, "INVALID_INPUT", "未対応のwriter要求です")
        fields(request, {"action"} | shape[action] | ({"text"} if action == "move" and "text" in request else set()))
        if action == "database":
            self._validate_database_message(request["message"], request["timeoutSeconds"])
            return
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
