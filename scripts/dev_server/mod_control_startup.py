"""P正本を明示供給するprivate startup。実launch/承認供給/公開commandは未接続。"""
from copy import deepcopy
import json
from pathlib import Path
import threading
import struct

from . import mod_control_operations as operations
from . import mod_control_wire as wire
from .build_inputs import regular_bytes
from .registry import require
from .runtime_bundle import _read_managed_profile, validate_bundle_managed_launch, validate_managed_launch, verify_bundle


def _admission(connection, row):
    gate = connection.execute("SELECT * FROM run_admission WHERE run=?", (row["run_id"],)).fetchone()
    run = connection.execute("SELECT record FROM runs WHERE id=?", (row["run_id"],)).fetchone()
    require(gate is not None and run is not None, "PROOF_REQUIRED", "run受付正本がありません")
    record = json.loads(run[0])
    require(all(gate[k] == row[v] for k, v in (("instance", "id"), ("lease", "lease"),
            ("epoch", "epoch"), ("generation", "generation")))
            and gate["nonce"] == record["nonce"] and type(gate["revision"]) is int
            and gate["start_operation"] == record["startOperationId"],
            "STALE_RUN", "受付正本のlease・世代・nonceが一致しません")
    return dict(gate)


def _require_open(connection, row, *, revision=None):
    gate = _admission(connection, row)
    require(row["state"] == "RUNNING" and not row["pending"] and gate["gate"] == "OPEN",
            "CONTROL_BLOCKED", "このrunの新規受付は閉じています")
    require(revision is None or type(revision) is int and gate["revision"] == revision,
            "STALE_OPERATION", "受付revisionが変更されました")
    return gate["revision"]


def _change(connection, row, revision, state):
    gate = _admission(connection, row)
    require(type(revision) is int and revision == gate["revision"],
            "STALE_OPERATION", "受付revisionが変更されました")
    changed = connection.execute("UPDATE run_admission SET gate=?,revision=revision+1 WHERE run=? AND revision=?",
                                 (state, row["run_id"], revision))
    require(changed.rowcount == 1, "STALE_OPERATION", "受付正本が変更されました")
    return revision + 1


def _touch(connection, row):
    gate = _admission(connection, row)
    return _change(connection, row, gate["revision"], gate["gate"])


def _reply(request, context, phase):
    message = wire.decode_frame_v2(request)
    payload = {"requestBodySha256": wire.sha256(request[4:])}
    if phase == "GRANT":
        require(message["type"] == "request" and message["phase"] == "HELLO", "INVALID_INPUT", "HELLOが必要です")
        payload["management"] = deepcopy(context["management"])
    else:
        require(message["type"] == "request" and message["phase"] == "BOUND", "INVALID_INPUT", "BOUNDが必要です")
        state = wire._status(wire._inner(message["payload"], "statusJson", "statusJsonSha256"), "Running")
        payload["statusJsonSha256"] = message["payload"]["statusJsonSha256"]
        payload.update({key: state[key] for key in ("worldUuid", "runUuid", "stateRevision")})
    payload["profileEvidenceSha256"] = message["payload"]["profileEvidenceSha256"]
    response = dict(message, type="response", phase=phase, payload=payload)
    body = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(body)) + body


class _Startup:
    """inventoryの各instanceに同期1枠。呼出元は共有4workerへ待機処理を載せない。

    transportと受信原文を寿命中保持する。未知終端のsessionを新runへ再利用しない。
    実supervisorでの制御進行・承認入力の供給は後続で接続する。
    """
    def __init__(self, runtime, transport):
        self.runtime = runtime
        self.registry = runtime.registry
        self.transport = transport
        self.instances = {item["instanceId"] for item in self.registry.inventory["instances"]}
        self.sessions = {}
        self.lock = threading.Lock()

    def _row(self, connection, token, context, revision):
        row = dict(self.registry._instance(connection, token["instanceId"]))
        self.registry._token(row, token)
        descriptor, raw = wire._profile_context(context)
        record = connection.execute("SELECT record FROM runs WHERE id=?", (row["run_id"],)).fetchone()
        require(record is not None, "PROOF_REQUIRED", "Pのrun正本がありません")
        run = json.loads(record[0])
        require(row["state"] == "STARTING" and row["pending"]
                and descriptor["runtimeRunId"] == row["run_id"] and descriptor["startNonce"] == run["nonce"]
                and descriptor["artifactId"] == row["artifact"]
                and descriptor["runtimeManifestHash"] == run["manifestHash"]
                and descriptor["instanceId"] == row["id"] and descriptor["managementRoot"] == str(self.registry.root)
                and descriptor["leaseId"] == row["lease"] and descriptor["epoch"] == row["epoch"]
                and descriptor["generationId"] == row["generation"],
                "STALE_RUN", "P正本とstartup contextが一致しません")
        require(not row["writers"] and not row["children"]
                and not connection.execute("SELECT 1 FROM writer_sessions WHERE instance=? AND status!='CLOSED'", (row["id"],)).fetchone()
                and not connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'", (row["id"],)).fetchone(),
                "WRITER_PENDING", "GRANT前のwriter drainが未完了です")
        gate = _admission(connection, row)
        operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (gate["start_operation"],)).fetchone()
        require(operation is not None and operation["command"] == "start" and operation["status"] == "PENDING"
                and operation["instance"] == row["id"] and operation["fingerprint"] == run["startRequestHash"],
                "STALE_OPERATION", "元起動予約の所有・状態・指紋が一致しません")
        require(connection.execute("SELECT 1 FROM reservations WHERE operation=? AND reference=? AND instance=? AND lease=? AND epoch=?",
                    (operation["id"], "artifact:" + row["artifact"], row["id"], row["lease"], row["epoch"])).fetchone(),
                "STALE_OPERATION", "元artifact予約のbindingが一致しません")
        gate["start_fingerprint"] = operation["fingerprint"]
        require(type(revision) is int and gate["revision"] == revision and gate["gate"] == "CLOSED",
                "CONTROL_BLOCKED", "元の閉門revisionが必要です")
        return row, descriptor, raw, gate

    def start(self, token, *, profile_raw, launch_context, observed, context, revision, deadline):
        """Pが独立保持するexact入力で2往復する。承認やMの実起動を代行しない。

        ACK原文をcommit後に送信し、同run/revisionでOPENとRUNNINGを同時確定する。
        送信/保存不明時は門を開けず、完成済み原応答をUNKNOWNへ格下げしない。
        """
        token, launch_context, observed, context = deepcopy((token, launch_context, observed, context))
        require(token["instanceId"] in self.instances, "NOT_FOUND", "inventory外のinstanceです")
        with self.lock:
            require(token["instanceId"] not in self.sessions, "CONTROL_BUSY", "同instanceのstartup資源を保持中です")
            session = {"token": token, "context": context, "profile_raw": profile_raw, "launch_context": launch_context,
                       "observed": observed, "request": None, "hello": None, "origin": None,
                       "checkpoint": None, "active": True, "endpoint": None, "connection": None}
            self.sessions[token["instanceId"]] = session
        try:
            require(type(profile_raw) is bytes and wire.sha256(profile_raw) == context["expectedEvidence"]["profileSha256"],
                    "PROFILE_MISMATCH", "明示profile原bytesがP期待値と一致しません")
            validate_managed_launch(_read_managed_profile(profile_raw), launch_context, observed)
            bundle = Path(launch_context["bundle"])
            manifest = verify_bundle(bundle)
            require(any(item["path"] == "launch/managed-server-candidate.json" and item["sha256"] == wire.sha256(profile_raw)
                        and item["size"] == len(profile_raw) for item in manifest["files"]),
                    "PROFILE_MISMATCH", "bundleのcandidate原bytesと明示profileが一致しません")
            manifest_hash = wire.sha256(regular_bytes(bundle / "runtime.json"))
            validate_bundle_managed_launch(bundle, launch_context, observed)
            with self.runtime._transaction(token["instanceId"]) as connection:
                row, descriptor, raw, gate = self._row(connection, token, context, revision)
                require(gate["descriptor_hash"] is None, "CONTROL_BUSY", "一度結束したstartupを再実行しません")
                generation = self.registry.root / "instances" / row["id"] / "generations" / row["generation"]
                require(bundle == generation / "runtime" and launch_context["run"] == str(generation)
                        and manifest["artifactId"] == row["artifact"] and manifest_hash == descriptor["runtimeManifestHash"],
                        "PROFILE_MISMATCH", "固定generationの検証済みbundleが必要です")
                require(launch_context["instanceId"] == row["id"] and launch_context["generationId"] == row["generation"]
                        and launch_context["startNonce"] == descriptor["startNonce"]
                        and launch_context["controlDescriptor"] == wire.descriptor_path(descriptor)
                        and launch_context["controlDescriptorSha256"] == wire.sha256(raw),
                        "PROFILE_MISMATCH", "独立launch入力とdescriptorが一致しません")
                transport_context = {"managementRoot": str(self.registry.root), "instanceId": row["id"],
                    "leaseId": row["lease"], "epoch": row["epoch"], "generationId": row["generation"],
                    "runtimeRunId": row["run_id"], "controller": deepcopy(context["management"]["controller"]),
                    "descriptorSha256": launch_context["controlDescriptorSha256"]}
                session["origin"] = (gate["start_operation"], gate["start_fingerprint"])
                session["checkpoint"] = ({key: row[key] for key in
                    ("id", "lease", "epoch", "generation", "artifact", "run_id")}, gate["revision"], wire.sha256(raw), gate["nonce"])
                revision = _change(connection, row, revision, "CLOSED")
                connection.execute("UPDATE run_admission SET descriptor_hash=? WHERE run=?", (wire.sha256(raw), row["run_id"]))
            session["endpoint"] = self.transport.create(transport_context, raw)
            channel = session["endpoint"].accept(context["owner"], deadline)
            session["connection"] = channel
            hello = channel.receive_frame(deadline)
            session["request"] = session["hello"] = hello
            grant_frame = _reply(hello, context, "GRANT")
            wire.correlate_grant_v2(hello, grant_frame, context)
            self._save(token, context, revision, hello, grant_frame)
            require(channel.send_frame(grant_frame, deadline) == len(grant_frame),
                    "CONTROL_UNKNOWN", "GRANT全bytesの送信を確認できません")
            request = channel.receive_frame(deadline)
            session["request"] = request
            message = wire.correlate_binding_v2(request, context,
                pre_evidence_json=wire.decode_frame_v2(hello)["payload"]["profileEvidenceJson"])
            require(message["type"] == "request" and message["phase"] == "BOUND", "INVALID_INPUT", "BOUNDが必要です")
            ack_frame = _reply(request, context, "ACK")
            ack = wire.correlate_response_v2(request, ack_frame)
            require(ack["type"] == "response" and ack["phase"] == "ACK", "INVALID_INPUT", "成功ACKの原文が必要です")
            self._save(token, context, revision, request, ack_frame)
            require(channel.send_frame(ack_frame, deadline) == len(ack_frame),
                    "CONTROL_UNKNOWN", "ACK全bytesの送信を確認できません")
            with self.runtime._transaction(token["instanceId"]) as connection:
                row, _, _, gate = self._row(connection, token, context, revision)
                _change(connection, row, revision, "OPEN")
                connection.execute("UPDATE instances SET state='RUNNING',pending=0 WHERE id=?", (row["id"],))
                result = {"instanceId": row["id"], "generation": row["generation"], "runId": row["run_id"],
                          "running": True, "snapshotClean": False}
                changed = connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=? AND instance=? AND command='start' AND status='PENDING' AND fingerprint=?",
                    (json.dumps(result), gate["start_operation"], row["id"], gate["start_fingerprint"]))
                require(changed.rowcount == 1, "STALE_OPERATION", "元起動予約だけを完成できます")
                deleted = connection.execute("DELETE FROM reservations WHERE operation=? AND reference=? AND instance=? AND lease=? AND epoch=?",
                    (gate["start_operation"], "artifact:" + row["artifact"], row["id"], row["lease"], row["epoch"]))
                require(deleted.rowcount == 1, "STALE_OPERATION", "元artifact予約だけを解放できます")
            return ack_frame
        except BaseException:
            if session["checkpoint"] is not None:
                # wireの解析/保存失敗で、照合済みP起動の保全を妨げない。
                for preserve in (self._unknown_start, self._unknown_wire):
                    try:
                        preserve(session)
                    except BaseException:
                        pass
            raise
        finally:
            with self.lock:
                session["active"] = False

    def _checkpoint(self, connection, session):
        expected, revision, descriptor_hash, nonce = session["checkpoint"]
        row = self.registry._instance(connection, expected["id"])
        self.runtime._binding(row, expected)
        gate = _admission(connection, row)
        require(gate["nonce"] == nonce and (gate["descriptor_hash"] == descriptor_hash
                or gate["descriptor_hash"] is None and gate["revision"] == revision),
                "STALE_RUN", "別descriptorの受付を変更しません")
        origin, fingerprint = session["origin"]
        operation = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (origin,)).fetchone()
        require(gate["start_operation"] == origin and operation is not None
                and operation["instance"] == row["id"] and operation["command"] == "start"
                and operation["fingerprint"] == fingerprint,
                "STALE_OPERATION", "別の起動予約へ不明遷移しません")
        return row, gate, operation

    def _unknown_start(self, session):
        with self.runtime._transaction(session["token"]["instanceId"]) as connection:
            row, gate, operation = self._checkpoint(connection, session)
            if operation["status"] == "COMPLETED":
                return
            require(row["state"] == "STARTING" and row["pending"] and gate["gate"] == "CLOSED",
                    "STALE_OPERATION", "元の起動中予約だけを保全します")
            require(connection.execute("SELECT 1 FROM reservations WHERE operation=? AND reference=? AND instance=? AND lease=? AND epoch=?",
                        (operation["id"], "artifact:" + row["artifact"], row["id"], row["lease"], row["epoch"])).fetchone(),
                    "STALE_OPERATION", "元artifact予約が変更されました")
            changed = connection.execute("UPDATE runtime_operations SET status='RECOVERY_REQUIRED' WHERE id=? AND instance=? AND command='start' AND status='PENDING' AND fingerprint=?",
                (operation["id"], row["id"], operation["fingerprint"]))
            require(changed.rowcount == 1, "STALE_OPERATION", "同所有PENDINGだけを不明へ遷移します")
            _change(connection, row, gate["revision"], "BLOCKED")
            connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=?", (row["id"],))

    def _unknown_wire(self, session):
        if session["request"] is None:
            return
        with self.runtime._transaction(session["token"]["instanceId"]) as connection:
            row, _, _ = self._checkpoint(connection, session)
            request = session["request"]
            if operations.readback(connection, row["id"], request) is not None:
                operations.unknown(connection, row["id"], request)

    def _save(self, token, context, revision, request, response):
        with self.runtime._transaction(token["instanceId"]) as connection:
            row, _, _, _ = self._row(connection, token, context, revision)
            require(operations.reserve(connection, row["id"], request) is None,
                    "CONTROL_PENDING", "保存済み要求へ作用・応答送信を再実行しません")
            operations.complete(connection, row["id"], request, response)

    def readback(self, token, *, deadline):
        with self.lock:
            session = self.sessions.get(token["instanceId"])
            require(session is not None and session["connection"] is not None
                    and type(token["epoch"]) is int and token == session["token"],
                    "STALE_RUN", "保持した同じP sessionのtokenが必要です")
            require(not session["active"], "CONTROL_BUSY", "startup/読戻し中のframeを先取りしません")
            session["active"] = True
        try:
            channel = session["connection"]
            request = channel.receive_frame(deadline)
            context = session["context"]
            message = wire.correlate_binding_v2(request, context,
                pre_evidence_json=wire.decode_frame_v2(session["hello"])["payload"]["profileEvidenceJson"])
            require(message["type"] == "request" and message["phase"] == "BOUND", "INVALID_INPUT", "元BOUNDだけを読み戻します")
            with self.runtime._transaction(token["instanceId"], read_only=True) as connection:
                row = self.registry._instance(connection, token["instanceId"])
                gate = _admission(connection, row)
                descriptor = wire.decode_descriptor(context["descriptorJson"].encode())
                require(row["run_id"] == descriptor["runtimeRunId"] and row["lease"] == token["leaseId"]
                        and row["epoch"] == token["epoch"] and row["generation"] == token["expectedGeneration"]
                        and gate["nonce"] == descriptor["startNonce"]
                        and gate["descriptor_hash"] == wire.sha256(context["descriptorJson"].encode()),
                        "STALE_RUN", "保存ACKを別runへ返しません")
                saved = operations.readback(connection, row["id"], request)
                require(saved is not None and wire.decode_frame_v2(saved)["type"] == "response"
                        and wire.decode_frame_v2(saved)["phase"] == "ACK", "CONTROL_PENDING", "完成ACK原文がありません")
            require(channel.send_frame(saved, deadline) == len(saved), "CONTROL_UNKNOWN", "保存ACK全bytesの送達が不明です")
            return saved
        finally:
            with self.lock:
                session["active"] = False
