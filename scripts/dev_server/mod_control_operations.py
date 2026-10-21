"""Mod wire2専用要求の原bytesを既runtime_operationsへ保持する。native受付には未接続。"""
import json
import struct

from .local_ipc import decode
from .mod_control_wire import correlate_response_v2, decode_frame_v2, sha256
from .registry import fields, identifier, require

COMMAND = "mod-control/"


def _frame(body):
    raw = body.encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


def _entry(connection, instance_id, request_frame):
    require(connection.in_transaction, "INVALID_STATE", "呼出元の短いtransactionが必要です")
    identifier(instance_id, "instanceId")
    request = decode_frame_v2(request_frame)
    require(request["type"] == "request", "INVALID_INPUT", "Mod control requestが必要です")
    row = connection.execute("SELECT * FROM runtime_operations WHERE id=?", (request["operationId"],)).fetchone()
    if row is None:
        return request, None, None
    require(row["instance"] == instance_id and row["command"] == COMMAND + request["phase"]
            and row["fingerprint"] == sha256(request_frame[4:]),
            "OPERATION_CONFLICT", "元control要求の識別が一致しません")
    record = decode(row["response"].encode("utf-8"))
    fields(record, {"requestBodyUtf8", "responseBodyUtf8"})
    require(type(record["requestBodyUtf8"]) is str and type(record["responseBodyUtf8"]) is str,
            "RECOVERY_REQUIRED", "controlの原bytes記録が不正です")
    require(record["requestBodyUtf8"].encode("utf-8") == request_frame[4:],
            "OPERATION_CONFLICT", "同じoperationIdへ異なる原bytesを指定できません")
    response = _frame(record["responseBodyUtf8"])
    decoded = correlate_response_v2(request_frame, response)
    if row["status"] == "COMPLETED":
        require(decoded["type"] == "response" or decoded["payload"]["outcome"] == "REJECTED_BEFORE_EFFECT",
                "RECOVERY_REQUIRED", "完了したcontrol応答が不正です")
    else:
        outcome = {"PENDING": "PENDING", "RECOVERY_REQUIRED": "UNKNOWN"}.get(row["status"])
        require(outcome is not None and decoded["type"] == "error" and decoded["payload"]["outcome"] == outcome,
                "RECOVERY_REQUIRED", "controlの状態と保存応答が一致しません")
    return request, row, response


def readback(connection, instance_id: str, request_frame: bytes) -> bytes | None:
    """同instance・同原要求だけの保存応答を返す。未登録はNone。作用・再作用を実行しない。

    呼出元はpeer/descriptor本人認証を別途行い、Registry.transaction内で呼ぶ。
    PENDING/RECOVERY_REQUIREDも保存済みerror応答を返し、native再実行を許可しない。
    """
    return _entry(connection, instance_id, request_frame)[2]


def reserve(connection, instance_id: str, request_frame: bytes) -> bytes | None:
    """元要求をPENDINGへ予約する。既存ならreadbackのみ、新規ならNoneを返す。

    instance/run/lease/世代の排他照合は実受付側の責務。この予約だけではadmitしない。
    transactionを開始・commitせず、native作用を呼ぶcallbackも受け取らない。
    """
    request, row, response = _entry(connection, instance_id, request_frame)
    if row is not None:
        return response
    for table in ("operations", "reference_operations", "writer_operations", "snapshot_operations"):
        require(not connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (request["operationId"],)).fetchone(),
                "OPERATION_CONFLICT", "operationIdが他の操作に使用されています")
    require(connection.execute("SELECT 1 FROM instances WHERE id=?", (instance_id,)).fetchone() is not None,
            "NOT_FOUND", "instanceが登録されていません")
    pending = dict(request, type="error", payload={
        "requestBodySha256": sha256(request_frame[4:]), "code": "CONTROL_PENDING", "outcome": "PENDING"})
    body = json.dumps(pending, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    correlate_response_v2(request_frame, _frame(body))
    record = {"requestBodyUtf8": request_frame[4:].decode("utf-8"), "responseBodyUtf8": body}
    connection.execute("INSERT INTO runtime_operations VALUES (?,?,?,?,?,?)",
                       (request["operationId"], sha256(request_frame[4:]), COMMAND + request["phase"], instance_id,
                        "PENDING", json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))))
    return None


def complete(connection, instance_id: str, request_frame: bytes, response_frame: bytes) -> bytes:
    """PENDINGへ相関済み終端応答を保存する。完了済みは同bytesだけ許可し、不明状態へ再作用しない。"""
    request, row, saved = _entry(connection, instance_id, request_frame)
    require(row is not None, "INVALID_STATE", "元control予約がありません")
    result = correlate_response_v2(request_frame, response_frame)
    require(result["type"] == "response" or result["payload"]["outcome"] == "REJECTED_BEFORE_EFFECT",
            "INVALID_INPUT", "完了には成功または作用前拒否の終端応答が必要です")
    if row["status"] == "COMPLETED":
        require(saved == response_frame, "OPERATION_CONFLICT", "保存済み応答原bytesを変更できません")
        return saved
    require(row["status"] == "PENDING", "RECOVERY_REQUIRED", "結果不明のcontrolを再実行・完了扱いにしません")
    record = {"requestBodyUtf8": request_frame[4:].decode("utf-8"), "responseBodyUtf8": response_frame[4:].decode("utf-8")}
    connection.execute("UPDATE runtime_operations SET status='COMPLETED',response=? WHERE id=?",
                       (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")), request["operationId"]))
    return response_frame


def unknown(connection, instance_id: str, request_frame: bytes) -> bytes:
    """未完了予約を既runtimeのRECOVERY_REQUIREDへ保全し、wire UNKNOWNを返す。確定済みは維持する。"""
    request, row, saved = _entry(connection, instance_id, request_frame)
    require(row is not None, "INVALID_STATE", "元control予約がありません")
    if row["status"] in {"COMPLETED", "RECOVERY_REQUIRED"}:
        return saved
    value = dict(request, type="error", payload={
        "requestBodySha256": sha256(request_frame[4:]), "code": "CONTROL_UNKNOWN", "outcome": "UNKNOWN"})
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    response = _frame(body)
    correlate_response_v2(request_frame, response)
    record = {"requestBodyUtf8": request_frame[4:].decode("utf-8"), "responseBodyUtf8": body}
    connection.execute("UPDATE runtime_operations SET status='RECOVERY_REQUIRED',response=? WHERE id=?",
                       (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")), request["operationId"]))
    return response
