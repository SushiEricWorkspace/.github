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
    return _validate_v1_message(_json(frame[4:]))


def _validate_v1_message(value):
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
    return _correlate_response_values(decode_frame(request_frame), decode_frame(response_frame), sha256(request_frame[4:]))


def _correlate_response_values(request, response, request_body_sha256):
    _require(request["type"] == "request" and response["type"] in {"response", "error"}, "要求と応答が必要です")
    for name in ("operationId", "requestNonce", "descriptorSha256"):
        _require(request[name] == response[name], "元要求との相関が一致しません")
    _require(response["phase"] == (request["phase"] if response["type"] == "error" else RESPONSES[request["phase"]]),
             "応答phaseが一致しません")
    payload = response["payload"]
    _require(payload["requestBodySha256"] == request_body_sha256, "元request bodyのhashが一致しません")
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
    return _correlate_binding_value(decode_frame(frame), descriptor_raw, owner=owner, management=management)


def _correlate_binding_value(message, descriptor_raw, *, owner, management):
    descriptor = decode_descriptor(descriptor_raw)
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


_V2_EXTRA = {
    "HELLO": {"profileEvidenceJson", "profileEvidenceSha256"},
    "GRANT": {"profileEvidenceSha256"},
    "BOUND": {"profileEvidenceJson", "profileEvidenceSha256"},
    "ACK": {"profileEvidenceSha256"},
}


def _profile_text(value, limit):
    _require(type(value) is str and "\x00" not in value
             and 0 < len(value.encode("utf-8")) <= limit, "証拠文字列のUTF-8長またはNULが不正です")


def _profile_id(value, limit):
    _require(type(value) is str and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0," + str(limit - 1) + "}", value)
             is not None, "証拠識別子が不正です")


def _canonical_uuid(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value)
             is not None, "canonical UUIDが必要です")


def _profile_inventory(value):
    _fields(value, {"platform", "jdk", "profileSha256", "codeSources", "mods", "nativeSources"})
    _hash(value["profileSha256"])
    platform, jdk = value["platform"], value["jdk"]
    _fields(platform, {"os", "machine", "osBuild"})
    _require(type(platform["os"]) is str and platform["os"] in {"Windows", "Darwin"}, "証拠OSが不正です")
    _profile_text(platform["machine"], 64)
    _profile_text(platform["osBuild"], 128)
    _fields(jdk, {"version", "home", "javaSha256", "releaseSha256"})
    _profile_text(jdk["version"], 128)
    _profile_text(jdk["home"], 4096)
    _hash(jdk["javaSha256"])
    _hash(jdk["releaseSha256"])
    for name in ("codeSources", "mods", "nativeSources"):
        _require(type(value[name]) is list, "証拠集合が必要です")
        ids = []
        for item in value[name]:
            if name == "codeSources":
                _fields(item, {"id", "loaderId", "origin"})
                _profile_id(item["loaderId"], 64)
                _profile_text(item["origin"], 4096)
            elif name == "mods":
                _fields(item, {"id", "version", "sourceIds"})
                _profile_text(item["version"], 128)
                refs = item["sourceIds"]
                _require(type(refs) is list, "sourceIdsが必要です")
                for ref in refs:
                    _profile_id(ref, 64)
                _require(refs == sorted(set(refs)) and set(refs) <= {s["id"] for s in value["codeSources"]},
                         "sourceIdsの参照/順序/重複が不正です")
            else:
                _fields(item, {"id", "origin", "identity"})
                _profile_text(item["origin"], 4096)
                identity = item["identity"]
                _require(type(identity) is dict, "native identityが必要です")
                if identity.get("kind") == "FILE":
                    _fields(identity, {"kind", "volumeId", "fileId", "sha256"})
                    _profile_text(identity["volumeId"], 128)
                    _profile_text(identity["fileId"], 128)
                    _hash(identity["sha256"])
                else:
                    _fields(identity, {"kind", "cacheUuid", "imageUuid", "osBuild"})
                    _require(identity["kind"] == "OS_CACHE_IMAGE" and platform["os"] == "Darwin",
                             "Darwinの承認cache identityが必要です")
                    _canonical_uuid(identity["cacheUuid"])
                    _canonical_uuid(identity["imageUuid"])
                    _profile_text(identity["osBuild"], 128)
                    _require(identity["osBuild"] == platform["osBuild"], "cacheのOS buildが一致しません")
            _profile_id(item["id"], 128 if name == "mods" else 64)
            ids.append(item["id"])
        _require(ids == sorted(set(ids)), "証拠集合の順序/重複が不正です")


def _profile_evidence(raw):
    value = _json(raw)
    _fields(value, {"version", "purpose", "checkpoint", "runtimeManifestHash", "profileSha256",
                    "descriptorSha256", "runtimeRunId", "startNonce", "owner", "platform", "jdk",
                    "codeSources", "mods", "nativeSources"})
    _require(type(value["version"]) is int and value["version"] == 1
             and value["purpose"] == "managed-profile-evidence", "証拠のversion/purposeが不正です")
    _require(type(value["checkpoint"]) is str and value["checkpoint"] in {"PRE_ADMISSION", "BOUND"},
             "証拠checkpointが不正です")
    for name in ("runtimeManifestHash", "descriptorSha256"):
        _hash(value[name])
    for name in ("runtimeRunId", "startNonce"):
        _profile_text(value[name], 128)
    _identity(value["owner"])
    _profile_inventory({k: value[k] for k in ("platform", "jdk", "profileSha256", "codeSources", "mods", "nativeSources")})
    return value


def _v2_projection(value):
    # 検証用の値だけを渡す。再serializeによる別frame長制約を導入しない。
    return dict(value, version=1, payload={k: v for k, v in value["payload"].items()
                                          if k not in _V2_EXTRA.get(value["phase"], set())})


def decode_frame_v2(frame: bytes) -> dict:
    """wire2の構文/証拠を検証する。descriptor1とData抽出を再利用し、native認証や受付はしない。"""
    _require(type(frame) is bytes and 4 < len(frame) <= MAX_FRAME, "frameのサイズが不正です")
    _require(struct.unpack(">I", frame[:4])[0] == len(frame) - 4, "frame長または末尾dataが不正です")
    value = _json(frame[4:])
    _fields(value, {"version", "purpose", "type", "phase", "operationId", "requestNonce", "descriptorSha256", "payload"})
    _require(type(value["version"]) is int and value["version"] == 2, "wire2が必要です")
    phase, kind, payload = value["phase"], value["type"], value["payload"]
    _require(type(phase) is str and phase in PAYLOAD_FIELDS, "phaseが不正です")
    if kind == "error":
        _fields(payload, {"requestBodySha256", "code", "outcome"})
    else:
        _fields(payload, PAYLOAD_FIELDS[phase] | _V2_EXTRA.get(phase, set()))
    _validate_v1_message(_v2_projection(value))
    if kind != "error" and phase in _V2_EXTRA:
        _hash(payload["profileEvidenceSha256"])
        if phase in {"HELLO", "BOUND"}:
            _profile_text(payload["profileEvidenceJson"], MAX_FRAME - 4)
            raw = payload["profileEvidenceJson"].encode("utf-8")
            _require(sha256(raw) == payload["profileEvidenceSha256"], "証拠原bytesのhashが一致しません")
            evidence = _profile_evidence(raw)
            _require(evidence["checkpoint"] == ("PRE_ADMISSION" if phase == "HELLO" else "BOUND")
                     and _same_json(evidence["owner"], payload["owner"]), "証拠のcheckpoint/ownerが一致しません")
    return value


def correlate_response_v2(request_frame: bytes, response_frame: bytes) -> dict:
    """wire2の元body/inner原bytes相関。異版errorを含むfallbackや作用の再実行を許可しない。"""
    request, response = decode_frame_v2(request_frame), decode_frame_v2(response_frame)
    _correlate_response_values(_v2_projection(request), _v2_projection(response), sha256(request_frame[4:]))
    if response["type"] != "error" and request["phase"] in {"HELLO", "BOUND"}:
        _require(response["payload"]["profileEvidenceSha256"] == request["payload"]["profileEvidenceSha256"],
                 "元証拠のhashが一致しません")
    return response


def _profile_context(context):
    _fields(context, {"descriptorJson", "owner", "management", "expectedEvidence"})
    _require(type(context["descriptorJson"]) is str, "descriptor原UTF-8文字列が必要です")
    raw = context["descriptorJson"].encode("utf-8")
    descriptor = decode_descriptor(raw)
    _identity(context["owner"])
    _descriptor_binding(descriptor, context["management"])
    _canonical_uuid(descriptor["leaseId"])
    _canonical_uuid(descriptor["generationId"])
    _profile_inventory(context["expectedEvidence"])
    _require(all(context["owner"][k] == descriptor["controller"][k] for k in ("hostHash", "bootId")),
             "MとPのhost/bootが一致しません")
    return descriptor, raw


def _evidence_context(evidence, context, descriptor, descriptor_raw):
    _require(evidence["descriptorSha256"] == sha256(descriptor_raw)
             and _same_json(evidence["owner"], context["owner"])
             and all(evidence[k] == descriptor[k] for k in ("runtimeManifestHash", "runtimeRunId", "startNonce"))
             and all(_same_json(evidence[k], v) for k, v in context["expectedEvidence"].items()),
             "P正本と証拠が一致しません")


def correlate_grant_v2(hello_frame: bytes, grant_frame: bytes, context: dict) -> dict:
    """callerが固定したP正本とPRE/GRANTを照合する。contextの由来を認証するAPIではない。"""
    descriptor, raw = _profile_context(context)
    response = correlate_response_v2(hello_frame, grant_frame)
    _require(response["type"] == "response" and response["phase"] == "GRANT"
             and response["descriptorSha256"] == sha256(raw)
             and _same_json(response["payload"]["management"], context["management"]), "同じP正本へのGRANTが必要です")
    hello = decode_frame_v2(hello_frame)
    _evidence_context(_profile_evidence(hello["payload"]["profileEvidenceJson"].encode("utf-8")), context, descriptor, raw)
    return response


def correlate_binding_v2(frame: bytes, context: dict, *, pre_evidence_json: str | None = None) -> dict:
    """固定GRANT/Data相関とBOUND不変証拠を照合する。PRE原文は元HELLOからcallerが保持して渡す。"""
    descriptor, raw = _profile_context(context)
    message = decode_frame_v2(frame)
    _correlate_binding_value(_v2_projection(message), raw, owner=context["owner"], management=context["management"])
    if message["phase"] == "BOUND":
        _require(type(pre_evidence_json) is str, "元HELLOのPRE証拠が必要です")
        _profile_text(pre_evidence_json, MAX_FRAME - 4)
        pre = _profile_evidence(pre_evidence_json.encode("utf-8"))
        bound = _profile_evidence(message["payload"]["profileEvidenceJson"].encode("utf-8"))
        _require(pre["checkpoint"] == "PRE_ADMISSION", "元PRE checkpointが必要です")
        _evidence_context(pre, context, descriptor, raw)
        _evidence_context(bound, context, descriptor, raw)
        _require(_same_json(dict(pre, checkpoint="BOUND"), bound), "PREから不変証拠が変わりました")
    return message


def same_request_v2(original_frame: bytes, readback_frame: bytes) -> bool:
    """wire2の同要求原bytesだけを判定する。永続化/安全な作用再実行は保証しない。"""
    _require(decode_frame_v2(original_frame)["type"] == "request"
             and decode_frame_v2(readback_frame)["type"] == "request", "requestが必要です")
    return original_frame == readback_frame
