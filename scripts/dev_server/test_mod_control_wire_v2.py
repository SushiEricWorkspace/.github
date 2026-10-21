"""wire2の合成共有vector。native本人/実Data serializer/受付を成功扱いにしない。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import unittest

from . import mod_control_wire as wire


VECTORS = json.loads(Path(__file__).with_name("mod-control-vectors-v2.json").read_text(encoding="utf-8"))
BOUNDARIES = json.loads(Path(__file__).with_name("mod-control-boundary-vectors-v2.json").read_text(encoding="utf-8"))


def frame(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


def data(record):
    return bytes.fromhex(record["frameHex"])


def context(name):
    original = VECTORS["contexts"][name]
    return deepcopy({k: original[k] for k in ("descriptorJson", "owner", "management", "expectedEvidence")})


class ModControlWireV2Tests(unittest.TestCase):
    def test_shared_normal_frames_and_context(self):
        for session in VECTORS["sessions"]:
            ctx = context(session["contextId"])
            for pair in session["pairs"]:
                with self.subTest(os=session["contextId"], phase=pair["name"]):
                    request, response = data(pair["request"]), data(pair["response"])
                    self.assertEqual("response", wire.correlate_response_v2(request, response)["type"])
                    self.assertTrue(wire.same_request_v2(request, request))
                    if pair["name"] == "HELLO":
                        wire.correlate_grant_v2(request, response, ctx)
                    else:
                        wire.correlate_binding_v2(request, ctx,
                            pre_evidence_json=VECTORS["contexts"][session["contextId"]]["preEvidenceJson"])

    def test_shared_error_outcomes(self):
        outcomes = set()
        for session in VECTORS["sessions"]:
            for pair in session["pairs"]:
                response = wire.correlate_response_v2(data(pair["request"]), data(pair["error"]["input"]))
                self.assertEqual("error", response["type"])
                outcomes.add(response["payload"]["outcome"])
        self.assertEqual({"UNKNOWN", "PENDING", "REJECTED_BEFORE_EFFECT"}, outcomes)

    def test_shared_original_bytes(self):
        def visit(value):
            if type(value) is dict:
                if "frameHex" in value:
                    raw = data(value)
                    self.assertEqual(bytes.fromhex(value["bodyHex"]), raw[4:])
                    self.assertEqual(value["bodyLength"], len(raw) - 4)
                    self.assertEqual(value["bodySha256"], hashlib.sha256(raw[4:]).hexdigest())
                    self.assertEqual(len(raw) - 4, struct.unpack(">I", raw[:4])[0])
                for item in value.values():
                    visit(item)
            elif type(value) is list:
                for item in value:
                    visit(item)
        visit(VECTORS)
        visit(BOUNDARIES)

    def test_shared_rejections(self):
        for case in VECTORS["rejects"]:
            with self.subTest(case=case["name"]), self.assertRaises(ValueError):
                request = case["request"]
                if request is not None:
                    wire.correlate_response_v2(data(request), data(case["input"]))
                else:
                    message = wire.decode_frame_v2(data(case["input"]))
                    ctx = context(case["contextId"])
                    if message["phase"] == "HELLO":
                        # 同じ要求用のGRANTを作り、syntax/hashではなく正本比較へ到達させる。
                        original = next(s for s in VECTORS["sessions"] if s["contextId"] == case["contextId"])
                        reply = json.loads(bytes.fromhex(original["pairs"][0]["response"]["bodyHex"]))
                        reply["payload"].update(requestBodySha256=wire.sha256(data(case["input"])[4:]),
                                                profileEvidenceSha256=message["payload"]["profileEvidenceSha256"])
                        wire.correlate_grant_v2(data(case["input"]), frame(reply), ctx)
                    else:
                        wire.correlate_binding_v2(data(case["input"]), ctx,
                            pre_evidence_json=VECTORS["contexts"][case["contextId"]]["preEvidenceJson"])

    def test_shared_scalar_boundaries(self):
        for session in VECTORS["sessions"]:
            for pair in session["scalarPairs"]:
                with self.subTest(os=session["contextId"], case=pair["name"]):
                    wire.correlate_grant_v2(data(pair["request"]), data(pair["response"]), pair["contextOverride"])

    def test_different_raw_same_structure(self):
        for session in VECTORS["sessions"]:
            pair = session["alternateRawPair"]
            wire.correlate_grant_v2(data(pair["request"]), data(pair["response"]), context(session["contextId"]))
            self.assertFalse(wire.same_request_v2(data(session["pairs"][0]["request"]), data(pair["request"])))
            with self.assertRaises(ValueError):
                wire.correlate_response_v2(data(pair["request"]), data(session["pairs"][0]["response"]))

    def test_complete_frame_limits_and_escape(self):
        for case in BOUNDARIES["cases"]:
            with self.subTest(case=case["name"]):
                if case["expectedStage"] == "ACCEPT_PURE":
                    wire.decode_frame_v2(data(case["input"]))
                elif case["name"] == "bound-inner-escaping":
                    wire.decode_frame_v2(data(case["input"]))
                    with self.assertRaises(ValueError):
                        wire.correlate_binding_v2(data(case["input"]), context("Windows"),
                            pre_evidence_json=VECTORS["contexts"]["Windows"]["preEvidenceJson"])
                else:
                    with self.assertRaises(ValueError):
                        wire.decode_frame_v2(data(case["input"]))

    def test_v1_and_v2_do_not_fallback(self):
        raw = data(VECTORS["sessions"][0]["pairs"][0]["request"])
        with self.assertRaises(ValueError):
            wire.decode_frame(raw)
        legacy = json.loads(Path(__file__).with_name("mod-control-vectors-v1.json").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            wire.decode_frame_v2(bytes.fromhex(legacy["pairs"][0]["requestFrameHex"]))

    def test_context_exact_fields_and_uuid(self):
        pair = VECTORS["sessions"][0]["pairs"][0]
        for name, value in (("extra", True), ("owner", None), ("descriptorJson", b"{}")):
            ctx = context("Windows"); ctx[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                wire.correlate_grant_v2(data(pair["request"]), data(pair["response"]), ctx)
        for name in ("leaseId", "generationId"):
            ctx = context("Windows"); d = json.loads(ctx["descriptorJson"]); d[name] = "a" * 32
            if name == "generationId":
                d["worldRoot"] = d["worldRoot"].replace(d["generationId"], "a" * 32)
            ctx["descriptorJson"] = json.dumps(d)
            with self.subTest(name=name), self.assertRaises(ValueError):
                wire.correlate_grant_v2(data(pair["request"]), data(pair["response"]), ctx)

    def test_context_mismatch_and_not_native_authentication(self):
        pair = VECTORS["sessions"][0]["pairs"][0]
        for key in ("profileSha256", "codeSources", "nativeSources"):
            ctx = context("Windows")
            if key == "profileSha256":
                ctx["expectedEvidence"][key] = "0" * 64
            else:
                ctx["expectedEvidence"][key][0]["origin"] += "/other"
            with self.subTest(key=key), self.assertRaises(ValueError):
                wire.correlate_grant_v2(data(pair["request"]), data(pair["response"]), ctx)
        # 全値を自己申告から構成してもpureはnativeを検査しないことを契約として残す。
        self.assertEqual("GRANT", wire.correlate_grant_v2(data(pair["request"]), data(pair["response"]),
                                                       context("Windows"))["phase"])

    def test_bound_requires_original_pre_and_checkpoint(self):
        pair = VECTORS["sessions"][0]["pairs"][1]
        for raw in (None, VECTORS["contexts"]["Windows"]["boundEvidenceJson"], "[]"):
            with self.subTest(pre=raw is None), self.assertRaises(ValueError):
                wire.correlate_binding_v2(data(pair["request"]), context("Windows"), pre_evidence_json=raw)

    def test_same_structure_pre_can_have_other_whitespace(self):
        pair = VECTORS["sessions"][0]["pairs"][1]
        pre = json.dumps(json.loads(VECTORS["contexts"]["Windows"]["preEvidenceJson"]), indent=2)
        wire.correlate_binding_v2(data(pair["request"]), context("Windows"), pre_evidence_json=pre)

    def test_data_unknown_and_null_are_not_evidence_schema(self):
        pair = VECTORS["sessions"][0]["pairs"][1]
        message = json.loads(bytes.fromhex(pair["request"]["bodyHex"]))
        status = json.loads(message["payload"]["statusJson"])
        status["state"]["dataSerializerExtension"] = None
        raw = json.dumps(status)
        message["payload"].update(statusJson=raw, statusJsonSha256=wire.sha256(raw.encode()))
        # Data全モデルの適否はData serializerが決める。wireは必要相関だけ抽出する。
        wire.correlate_binding_v2(frame(message), context("Windows"),
                                 pre_evidence_json=VECTORS["contexts"]["Windows"]["preEvidenceJson"])

    def test_request_response_nonce_and_inner_echo_tampering(self):
        for index in (0, 1):
            pair = VECTORS["sessions"][0]["pairs"][index]
            for key in ("operationId", "requestNonce", "descriptorSha256"):
                response = json.loads(bytes.fromhex(pair["response"]["bodyHex"]))
                response[key] = "0" * 64 if key == "descriptorSha256" else "other"
                with self.subTest(index=index, key=key), self.assertRaises(ValueError):
                    wire.correlate_response_v2(data(pair["request"]), frame(response))
            response = json.loads(bytes.fromhex(pair["response"]["bodyHex"]))
            response["payload"]["profileEvidenceSha256"] = "0" * 64
            with self.assertRaises(ValueError):
                wire.correlate_response_v2(data(pair["request"]), frame(response))

    def test_frame_prefix_and_outer_unknown(self):
        pair = VECTORS["sessions"][0]["pairs"][0]
        raw = data(pair["request"])
        for bad in (raw[:-1], raw + b" ", b"\0\0\0\0", b"{}"):
            with self.assertRaises(ValueError):
                wire.decode_frame_v2(bad)
        message = json.loads(raw[4:]); message["extra"] = None
        with self.assertRaises(ValueError):
            wire.decode_frame_v2(frame(message))


    def test_completion_limit_uses_original_body_not_numeric_reserialization(self):
        pair = VECTORS["sessions"][0]["pairs"][3]
        request = json.loads(bytes.fromhex(pair["request"]["bodyHex"]))
        requested = json.loads(request["payload"]["requestJson"])
        requested["status"]["state"]["x"] = 100.0
        raw_requested = json.dumps(requested, separators=(",", ":")).replace('"x":100.0', '"x":1e2')
        request["payload"].update(requestJson=raw_requested, requestSha256=wire.sha256(raw_requested.encode()))
        request_frame = frame(request)
        reply = json.loads(bytes.fromhex(pair["response"]["bodyHex"]))
        reply["payload"].update(requestBodySha256=wire.sha256(request_frame[4:]),
                                requestSha256=wire.sha256(raw_requested.encode()))
        reply["payload"]["completion"]["request"] = requested
        identity = reply["payload"]["completion"]["writers"][0]["identity"]
        identity["bootId"] = ""

        def original_body():
            return json.dumps(reply, separators=(",", ":")).replace('"x":100.0', '"x":1e2').encode()

        identity["bootId"] = "A" * (65532 - len(original_body()))
        body = original_body()
        self.assertEqual(65532, len(body))
        self.assertEqual(65534, len(json.dumps(json.loads(body), separators=(",", ":")).encode()))
        response_frame = struct.pack(">I", len(body)) + body
        decoded = wire.decode_frame_v2(response_frame)
        self.assertEqual(100.0, decoded["payload"]["completion"]["request"]["status"]["state"]["x"])
        self.assertEqual("COMPLETION", wire.correlate_response_v2(request_frame, response_frame)["phase"])
        other_body = request_frame[4:] + b" "
        tampered = struct.pack(">I", len(other_body)) + other_body
        with self.assertRaises(ValueError):
            wire.correlate_response_v2(tampered, response_frame)
