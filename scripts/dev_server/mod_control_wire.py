"""Mod専用controlのbytes契約。OS本人認証・受付・台帳更新は実行しない。"""
import hashlib
import json
import math
from pathlib import PureWindowsPath
import re
import struct

from .contracts import absolute_path

MAX_FRAME = 65536
PURPOSE = "mod-control"
DESCRIPTOR_PROPERTY = "sushieric.managed.controlDescriptor"
DESCRIPTOR_HASH_PROPERTY = "sushieric.managed.controlDescriptorSha256"
RESPONSES = {"HELLO": "GRANT", "BOUND": "ACK", "STOP_BLOCK": "BLOCKED", "SAVE_REQUESTED": "COMPLETION"}
PAYLOAD_FIELDS = {
    "HELLO": {"owner"},
    "GRANT": {"requestBodySha256", "management"},
    "BOUND": {"owner", "management", "statusJson", "statusJsonSha256"},
    "ACK": {"requestBodySha256", "statusJsonSha256", "worldUuid", "runUuid", "stateRevision"},
    "STOP_BLOCK": {"management"},
    "BLOCKED": {"requestBodySha256", "management", "registryRevision"},
    "SAVE_REQUESTED": {"management", "requestJson", "requestSha256"},
    "COMPLETION": {"requestBodySha256", "requestSha256", "completion"},
}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _fields(value, names):
    _require(type(value) is dict and value.keys() == names, "必須項目の欠落または未知項目があります")


def _text(value):
    _require(type(value) is str and bool(value.strip()), "空でない文字列が必要です")
    value.encode("utf-8")


def _hash(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "SHA-256形式が必要です")


def _integer(value, minimum=0):
    _require(type(value) is int and minimum <= value <= 9223372036854775807, "整数の範囲が不正です")


def _identity(value):
    _fields(value, {"hostHash", "bootId", "pid", "startId"})
    _hash(value["hostHash"])
    _text(value["bootId"])
    _integer(value["pid"], 1)
    _text(value["startId"])


def _management(value):
    _fields(value, {"leaseId", "epoch", "generation", "controller", "drainSha256"})
    for name in ("leaseId", "generation"):
        _text(value[name])
    _integer(value["epoch"])
    _identity(value["controller"])
    _hash(value["drainSha256"])


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "JSON項目が重複しています")
            result[key] = value
        return result

    def number(value):
        result = float(value)
        _require(math.isfinite(result), "非有限数は使用できません")
        return result

    def invalid(_):
        raise ValueError("非有限数は使用できません")

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                              parse_float=number, parse_constant=invalid)
        _require(type(document) is dict, "JSON objectが必要です")
        # エスケープされた孤立surrogateもUTF-8原文として使用しない。
        json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except RecursionError:
        raise ValueError("JSONのネストが深すぎます") from None
    return document


def sha256(raw: bytes) -> str:
    """受信した元bytesをhashする。Dataのstate hashをJSON再整形で生成しない。"""
    return hashlib.sha256(raw).hexdigest()


def decode_descriptor(raw: bytes) -> dict:
    """descriptorの構文と固定pathを検証する。ファイル実体・ACL・P本人は別途照合する。"""
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_FRAME - 4, "descriptorのサイズが不正です")
    value = _json(raw)
    _fields(value, {"version", "purpose", "descriptorId", "managementRoot", "instanceId", "leaseId", "epoch",
                    "generationId", "runtimeRunId", "startNonce", "artifactId", "runtimeManifestHash",
                    "worldRoot", "shopDirectory", "controller", "endpoint"})
    _require(type(value["version"]) is int and value["version"] == 1 and value["purpose"] == PURPOSE,
             "descriptorのversion/purposeが不正です")
    for name in ("descriptorId", "instanceId", "leaseId", "generationId", "runtimeRunId", "startNonce", "artifactId"):
        _text(value[name])
        _require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", value[name]) is not None, "固定識別子が不正です")
    _integer(value["epoch"])
    _hash(value["runtimeManifestHash"])
    _identity(value["controller"])
    for name in ("managementRoot", "worldRoot", "shopDirectory"):
        _text(value[name])
    root = absolute_path(value["managementRoot"])
    world = absolute_path(value["worldRoot"])
    shop = absolute_path(value["shopDirectory"])
    generation = root / "instances" / value["instanceId"] / "generations" / value["generationId"]
    _require(world.is_relative_to(generation) and world != generation
             and shop.is_relative_to(generation) and shop.name == "shop_data", "世代のworld/shop pathが一致しません")
    endpoint = value["endpoint"]
    _fields(endpoint, {"transport", "address"})
    _text(endpoint["address"])
    if isinstance(root, PureWindowsPath):
        _require(endpoint["transport"] == "windows-pipe"
                 and endpoint["address"].startswith("\\\\.\\pipe\\") and len(endpoint["address"]) > 9,
                 "Windows pipe endpointが必要です")
    else:
        _require(endpoint["transport"] == "macos-unix"
                 and absolute_path(endpoint["address"]) == root / "manager" / "control" / value["runtimeRunId"] / "mod-control.sock",
                 "固定Unix endpointが必要です")
    return value


def descriptor_path(descriptor: dict) -> str:
    """検証済descriptorに対応する固定公開先。実体の読み書きは行わない。"""
    return str(absolute_path(descriptor["managementRoot"]) / "manager" / "control"
               / descriptor["runtimeRunId"] / "mod-control.json")


def _inner(payload, name, hash_name):
    _text(payload[name])
    raw = payload[name].encode("utf-8")
    _hash(payload[hash_name])
    _require(sha256(raw) == payload[hash_name], "Data JSON原bytesのhashが一致しません")
    return _json(raw)


def _status(value, phase):
    # Data全モデルの検証ではなく、controlの相関に必要な部分だけを取り出す。
    _fields(value, {"sha256", "state"})
    _hash(value["sha256"])
    state = value["state"]
    _require(type(state) is dict, "Data stateが必要です")
    for name in ("worldUuid", "runUuid", "generation"):
        _text(state.get(name))
    _integer(state.get("stateRevision"))
    paths = state.get("paths")
    _require(type(paths) is dict, "Data pathsが必要です")
    for name in ("worldRoot", "shopDirectory"):
        _text(paths.get(name))
        absolute_path(paths[name])
    data = state.get("payload")
    _require(type(data) is dict and data.get("type") ==
             "io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldRunPhase." + phase,
             "Data phaseが一致しません")
    _identity(data.get("owner"))
    _management(data.get("management"))
    if phase == "ManagedSaveRequested":
        _text(data.get("challenge"))
    return state


def decode_frame(frame: bytes) -> dict:
    """完成した1frameを厳密検証する。分割受信・timeout・peer認証は呼出元の責務。"""
    _require(type(frame) is bytes and 4 < len(frame) <= MAX_FRAME, "frameのサイズが不正です")
    _require(struct.unpack(">I", frame[:4])[0] == len(frame) - 4, "frame長または末尾dataが不正です")
    value = _json(frame[4:])
    _fields(value, {"version", "purpose", "type", "phase", "operationId", "requestNonce", "descriptorSha256", "payload"})
    _require(type(value["version"]) is int and value["version"] == 1 and value["purpose"] == PURPOSE,
             "wireのversion/purposeが不正です")
    for name in ("operationId", "requestNonce"):
        _text(value[name])
        _require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", value[name]) is not None, "相関識別子が不正です")
    _hash(value["descriptorSha256"])
    kind, phase, payload = value["type"], value["phase"], value["payload"]
    _require(type(kind) is str and kind in {"request", "response", "error"}
             and type(phase) is str and phase in PAYLOAD_FIELDS, "type/phaseが不正です")
    if kind == "error":
        _require(phase in RESPONSES, "errorは元request phaseが必要です")
        _fields(payload, {"requestBodySha256", "code", "outcome"})
        _hash(payload["requestBodySha256"])
        _text(payload["code"])
        _require(type(payload["outcome"]) is str and payload["outcome"] in
                 {"REJECTED_BEFORE_EFFECT", "PENDING", "UNKNOWN"}, "error outcomeが不正です")
        return value
    _require((phase in RESPONSES) == (kind == "request"), "typeとphaseが一致しません")
    _fields(payload, PAYLOAD_FIELDS[phase])
    if kind == "response":
        _hash(payload["requestBodySha256"])
    if "owner" in payload:
        _identity(payload["owner"])
    if "management" in payload:
        _management(payload["management"])
    if phase == "BOUND":
        state = _status(_inner(payload, "statusJson", "statusJsonSha256"), "Running")
        _require(state["payload"]["owner"] == payload["owner"]
                 and state["payload"]["management"] == payload["management"], "BOUNDのowner/bindingが一致しません")
    elif phase == "ACK":
        _hash(payload["statusJsonSha256"])
        for name in ("worldUuid", "runUuid"):
            _text(payload[name])
        _integer(payload["stateRevision"])
    elif phase == "BLOCKED":
        _integer(payload["registryRevision"])
    elif phase == "SAVE_REQUESTED":
        requested = _inner(payload, "requestJson", "requestSha256")
        _fields(requested, {"status"})
        state = _status(requested["status"], "ManagedSaveRequested")
        _require(state["payload"]["management"] == payload["management"], "Requestedのbindingが一致しません")
    elif phase == "COMPLETION":
        _hash(payload["requestSha256"])
        completion = payload["completion"]
        _fields(completion, {"request", "snapshotId", "registryRevision", "acceptance", "drain", "pendingCount", "writers"})
        _fields(completion["request"], {"status"})
        _status(completion["request"]["status"], "ManagedSaveRequested")
        _text(completion["snapshotId"])
        _integer(completion["registryRevision"])
        _require(completion["acceptance"] == "BLOCKED" and completion["drain"] == "COMPLETE"
                 and type(completion["pendingCount"]) is int and completion["pendingCount"] == 0,
                 "完了台帳の遮断/drainが不完全です")
        _require(type(completion["writers"]) is list, "writer全集合が必要です")
        identities = []
        for writer in completion["writers"]:
            _fields(writer, {"identity", "state", "children"})
            _identity(writer["identity"])
            _require(writer["state"] == "CLOSED" and type(writer["children"]) is list, "writer終了が不完全です")
            for identity in [writer["identity"], *writer["children"]]:
                _identity(identity)
                _require(identity not in identities, "writer/child identityが重複しています")
                identities.append(identity)
    return value


def _same_json(left, right):
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if type(left) is not type(right):
            return False
        if type(left) is dict:
            if left.keys() != right.keys():
                return False
            pending.extend((left[key], right[key]) for key in left)
        elif type(left) is list:
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif left != right:
            return False
    return True


def correlate_response(request_frame: bytes, response_frame: bytes) -> dict:
    """元request bodyのhashと応答を照合する。成功でも認証・最新性・耐久化を証明しない。"""
    request = decode_frame(request_frame)
    response = decode_frame(response_frame)
    _require(request["type"] == "request" and response["type"] in {"response", "error"}, "要求と応答が必要です")
    for name in ("operationId", "requestNonce", "descriptorSha256"):
        _require(request[name] == response[name], "元要求との相関が一致しません")
    _require(response["phase"] == (request["phase"] if response["type"] == "error" else RESPONSES[request["phase"]]),
             "応答phaseが一致しません")
    payload = response["payload"]
    _require(payload["requestBodySha256"] == sha256(request_frame[4:]), "元request bodyのhashが一致しません")
    if response["type"] == "error":
        return response
    original = request["payload"]
    if request["phase"] == "BOUND":
        state = _status(_inner(original, "statusJson", "statusJsonSha256"), "Running")
        _require(payload["statusJsonSha256"] == original["statusJsonSha256"]
                 and all(payload[name] == state[name] for name in ("worldUuid", "runUuid", "stateRevision")),
                 "ACKの元Runningが一致しません")
    elif request["phase"] == "STOP_BLOCK":
        _require(payload["management"] == original["management"], "遮断bindingが一致しません")
    elif request["phase"] == "SAVE_REQUESTED":
        requested = _inner(original, "requestJson", "requestSha256")
        _require(payload["requestSha256"] == original["requestSha256"]
                 and _same_json(payload["completion"]["request"], requested), "完了台帳の元Requestedが一致しません")
    return response


def _descriptor_binding(descriptor, management):
    _management(management)
    _require(management["controller"] == descriptor["controller"]
             and management["leaseId"] == descriptor["leaseId"] and management["epoch"] == descriptor["epoch"]
             and management["generation"] == descriptor["generationId"], "descriptorとGRANTが一致しません")


def correlate_grant(hello_frame: bytes, grant_frame: bytes, descriptor_raw: bytes) -> dict:
    """beginRun前にGRANTとdescriptorの固定P/lease/epoch/世代を照合する。native認証は別途必要。"""
    descriptor = decode_descriptor(descriptor_raw)
    response = correlate_response(hello_frame, grant_frame)
    _require(response["type"] == "response" and response["phase"] == "GRANT"
             and response["descriptorSha256"] == sha256(descriptor_raw), "同じdescriptorへのGRANTが必要です")
    _descriptor_binding(descriptor, response["payload"]["management"])
    return response


def correlate_binding(frame: bytes, descriptor_raw: bytes, *, owner: dict, management: dict) -> dict:
    """認証境界が固定したdescriptor/M/GRANTと後続requestを照合する。入力を認証済みに変換しない。"""
    descriptor = decode_descriptor(descriptor_raw)
    message = decode_frame(frame)
    _require(message["type"] == "request" and message["phase"] in {"BOUND", "STOP_BLOCK", "SAVE_REQUESTED"},
             "GRANT後のrequestが必要です")
    _identity(owner)
    _management(management)
    _require(message["descriptorSha256"] == sha256(descriptor_raw), "descriptorの原bytesが一致しません")
    _descriptor_binding(descriptor, management)
    payload = message["payload"]
    _require(payload["management"] == management, "GRANTのbindingが一致しません")
    if message["phase"] in {"BOUND", "SAVE_REQUESTED"}:
        state = (_status(_inner(payload, "statusJson", "statusJsonSha256"), "Running")
                 if message["phase"] == "BOUND" else
                 _status(_inner(payload, "requestJson", "requestSha256")["status"], "ManagedSaveRequested"))
        _require(state["payload"]["owner"] == owner and state["generation"] == descriptor["generationId"]
                 and state["paths"]["worldRoot"] == descriptor["worldRoot"]
                 and state["paths"]["shopDirectory"] == descriptor["shopDirectory"], "Dataのowner/世代/pathが一致しません")
    return message


def same_request(original_frame: bytes, readback_frame: bytes) -> bool:
    """同ID/nonceを含む元request bytesの完全一致だけを判定する。再実行の許可ではない。"""
    _require(decode_frame(original_frame)["type"] == "request"
             and decode_frame(readback_frame)["type"] == "request", "requestが必要です")
    return original_frame == readback_frame
