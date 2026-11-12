"""合成vectorによるwire境界。OS peer認証・実台帳・Data admissionは試験しない。"""
from copy import deepcopy
import json
from pathlib import Path
import struct
import unittest

from . import mod_control_wire as wire

VECTORS = json.loads(Path(__file__).with_name("mod-control-vectors-v1.json").read_text(encoding="utf-8"))


def frame(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


class ModControlWireTests(unittest.TestCase):
    def setUp(self):
        self.descriptor_raw = VECTORS["descriptorJson"].encode("utf-8")
        self.owner = deepcopy(VECTORS["owner"])
        self.management = deepcopy(VECTORS["management"])

    def pair(self, index):
        vector = VECTORS["pairs"][index]
        return bytes.fromhex(vector["requestFrameHex"]), bytes.fromhex(vector["responseFrameHex"])

    def test_fixed_normal_vectors(self):
        self.assertEqual(1, VECTORS["vectorFormat"])
        self.assertEqual(VECTORS["descriptorSha256"], wire.sha256(self.descriptor_raw))
        descriptor = wire.decode_descriptor(self.descriptor_raw)
        self.assertEqual("C:\\managed-fixture\\manager\\control\\runtime-a\\mod-control.json",
                         wire.descriptor_path(descriptor))
        for vector in VECTORS["pairs"]:
            with self.subTest(vector=vector["name"]):
                request = bytes.fromhex(vector["requestFrameHex"])
                response = bytes.fromhex(vector["responseFrameHex"])
                self.assertEqual(vector["requestJson"].encode(), request[4:])
                self.assertEqual(vector["responseJson"].encode(), response[4:])
                self.assertEqual(vector["requestBodySha256"], wire.sha256(request[4:]))
                self.assertEqual("response", wire.correlate_response(request, response)["type"])
                self.assertTrue(wire.same_request(request, request))
                if vector["name"] != "hello":
                    wire.correlate_binding(request, self.descriptor_raw, owner=self.owner, management=self.management)

    def test_fixed_rejection_vectors(self):
        for vector in VECTORS["rejectFrames"]:
            with self.subTest(vector=vector["name"]), self.assertRaises(ValueError):
                wire.decode_frame(bytes.fromhex(vector["frameHex"]))
        for vector in VECTORS["rejectCorrelations"]:
            with self.subTest(vector=vector["name"]), self.assertRaises(ValueError):
                wire.correlate_response(bytes.fromhex(vector["requestFrameHex"]), bytes.fromhex(vector["responseFrameHex"]))

    def test_error_outcomes_preserve_original_phase(self):
        for vector in VECTORS["errors"]:
            with self.subTest(vector=vector["name"]):
                response = wire.correlate_response(bytes.fromhex(vector["requestFrameHex"]),
                                                   bytes.fromhex(vector["responseFrameHex"]))
                self.assertEqual(vector["name"], response["payload"]["outcome"])
                self.assertEqual("SAVE_REQUESTED", response["phase"])
                self.assertNotIn("retryable", response["payload"])

    def test_frame_total_limit_and_multiple_frames(self):
        request, _ = self.pair(0)
        # 末尾空白も元bodyの一部。ちょうど64KiBは受理し、1byte超過は拒否。
        body = request[4:] + b" " * (wire.MAX_FRAME - len(request))
        wire.decode_frame(struct.pack(">I", len(body)) + body)
        for invalid in (struct.pack(">I", len(body) + 1) + body + b" ", request + request,
                        request[:3], request[:-1]):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                wire.decode_frame(invalid)

    def test_original_bytes_are_not_canonicalized(self):
        request, response = self.pair(0)
        original = wire.decode_frame(request)
        reformatted_raw = json.dumps(original, ensure_ascii=False, indent=2).encode()
        reformatted = struct.pack(">I", len(reformatted_raw)) + reformatted_raw
        self.assertEqual(original, wire.decode_frame(reformatted))
        self.assertFalse(wire.same_request(request, reformatted))
        with self.assertRaises(ValueError):
            wire.correlate_response(reformatted, response)
        changed = dict(original, requestNonce="other-nonce")
        self.assertFalse(wire.same_request(request, frame(changed)))

    def test_inner_bytes_hash_and_duplicate_fields(self):
        for index, key, hash_key in ((1, "statusJson", "statusJsonSha256"), (3, "requestJson", "requestSha256")):
            request, _ = self.pair(index)
            value = wire.decode_frame(request)
            inner = value["payload"][key]
            # 同じData構造でも原文が違えば旧hashを再利用できない。
            value["payload"][key] = json.dumps(json.loads(inner), indent=2)
            with self.subTest(index=index, kind="hash"), self.assertRaises(ValueError):
                wire.decode_frame(frame(value))
            # hashが合っていてもinnerの重複keyは拒否する。
            duplicate = inner.replace('"sha256":', '"sha256":"' + "9" * 64 + '","sha256":', 1)
            value["payload"][key] = duplicate
            value["payload"][hash_key] = wire.sha256(duplicate.encode())
            with self.subTest(index=index, kind="duplicate"), self.assertRaises(ValueError):
                wire.decode_frame(frame(value))

    def test_wrong_ack_and_incomplete_completion(self):
        request, response = self.pair(1)
        for name, invalid in (("worldUuid", "other-world"), ("runUuid", "other-run"), ("stateRevision", 99),
                              ("statusJsonSha256", "9" * 64)):
            value = wire.decode_frame(response)
            value["payload"][name] = invalid
            with self.subTest(name=name), self.assertRaises(ValueError):
                wire.correlate_response(request, frame(value))
        request, response = self.pair(3)
        for name, invalid in (("acceptance", "OPEN"), ("drain", "PENDING"), ("pendingCount", True),
                              ("pendingCount", 1), ("writers", None)):
            value = wire.decode_frame(response)
            value["payload"]["completion"][name] = invalid
            with self.subTest(name=name), self.assertRaises(ValueError):
                wire.correlate_response(request, frame(value))
        for mode in ("active", "duplicate-child", "missing-children", "huge"):
            value = wire.decode_frame(response)
            writers = value["payload"]["completion"]["writers"]
            if mode == "active":
                writers[0]["state"] = "ACTIVE"
            elif mode == "duplicate-child":
                writers[0]["children"].append(writers[0]["identity"])
            elif mode == "missing-children":
                del writers[0]["children"]
            else:
                writers.extend(deepcopy(writers) * 200)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                wire.correlate_response(request, frame(value))

    def test_bound_stop_requested_use_grant_binding(self):
        for index in (1, 2, 3):
            request, _ = self.pair(index)
            for field, invalid in (("epoch", 4), ("drainSha256", "9" * 64), ("leaseId", "other-lease")):
                management = dict(self.management, **{field: invalid})
                with self.subTest(index=index, field=field), self.assertRaises(ValueError):
                    wire.correlate_binding(request, self.descriptor_raw, owner=self.owner, management=management)
        request, _ = self.pair(1)
        with self.assertRaises(ValueError):
            wire.correlate_binding(request, self.descriptor_raw,
                                   owner=dict(self.owner, startId="other-start"), management=self.management)
        for field, invalid in (("generation", "other-generation"),
                               ("paths", dict(worldRoot="C:/other-world", shopDirectory="C:/other-shop"))):
            value = wire.decode_frame(request)
            inner = json.loads(value["payload"]["statusJson"])
            inner["state"][field] = invalid
            value["payload"]["statusJson"] = json.dumps(inner)
            value["payload"]["statusJsonSha256"] = wire.sha256(value["payload"]["statusJson"].encode())
            with self.subTest(field=field), self.assertRaises(ValueError):
                wire.correlate_binding(frame(value), self.descriptor_raw, owner=self.owner, management=self.management)

    def test_descriptor_strict_fields_paths_types_and_os(self):
        descriptor = wire.decode_descriptor(self.descriptor_raw)
        for field, invalid in (("version", True), ("extra", "unknown"), ("epoch", True),
                               ("runtimeRunId", "../escape"), ("worldRoot", "C:/other/world"),
                               ("shopDirectory", "C:/other/shop_data"),
                               ("controller", dict(descriptor["controller"], pid=False)),
                               ("endpoint", dict(transport="tcp", address="127.0.0.1:1"))):
            value = dict(descriptor, **{field: invalid})
            with self.subTest(field=field), self.assertRaises(ValueError):
                wire.decode_descriptor(json.dumps(value).encode())
        mac = deepcopy(descriptor)
        for key in ("managementRoot", "worldRoot", "shopDirectory"):
            mac[key] = mac[key].replace("C:/managed-fixture", "/managed-fixture")
        mac["endpoint"] = dict(transport="macos-unix",
                               address="/managed-fixture/manager/control/runtime-a/mod-control.sock")
        wire.decode_descriptor(json.dumps(mac).encode())
        self.assertEqual("sushieric.managed.controlDescriptor", wire.DESCRIPTOR_PROPERTY)
        self.assertEqual("sushieric.managed.controlDescriptorSha256", wire.DESCRIPTOR_HASH_PROPERTY)

    def test_unrelated_phases_cannot_be_correlated(self):
        request, _ = self.pair(0)
        _, other_response = self.pair(2)
        with self.assertRaises(ValueError):
            wire.correlate_response(request, other_response)
        _, response = self.pair(0)
        with self.assertRaises(ValueError):
            wire.same_request(request, response)


    def test_grant_binds_descriptor_before_begin_run(self):
        request, response = self.pair(0)
        wire.correlate_grant(request, response, self.descriptor_raw)
        for field, invalid in (("leaseId", "other-lease"), ("epoch", 4), ("generation", "other-generation"),
                               ("controller", dict(self.management["controller"], startId="other-start"))):
            value = wire.decode_frame(response)
            value["payload"]["management"][field] = invalid
            with self.subTest(field=field), self.assertRaises(ValueError):
                wire.correlate_grant(request, frame(value), self.descriptor_raw)
        changed_descriptor = dict(wire.decode_descriptor(self.descriptor_raw), startNonce="other-start")
        with self.assertRaises(ValueError):
            wire.correlate_grant(request, response, json.dumps(changed_descriptor).encode())

    def test_inner_type_errors_and_escaped_surrogate_are_rejected(self):
        request, _ = self.pair(1)
        for field, invalid in (("stateRevision", True), ("payload", None), ("worldUuid", False)):
            value = wire.decode_frame(request)
            inner = json.loads(value["payload"]["statusJson"])
            inner["state"][field] = invalid
            value["payload"]["statusJson"] = json.dumps(inner)
            value["payload"]["statusJsonSha256"] = wire.sha256(value["payload"]["statusJson"].encode())
            with self.subTest(field=field), self.assertRaises(ValueError):
                wire.decode_frame(frame(value))
        raw = b'{"x":"\\ud800"}'
        with self.assertRaises(ValueError):
            wire.decode_frame(struct.pack(">I", len(raw)) + raw)


    def test_requested_structure_compares_types_and_ignores_object_order(self):
        request, response = self.pair(3)
        value = wire.decode_frame(response)
        status = value["payload"]["completion"]["request"]["status"]
        status["state"]["format"] = True
        with self.assertRaises(ValueError):
            wire.correlate_response(request, frame(value))
        value = wire.decode_frame(response)
        inner = value["payload"]["completion"]["request"]["status"]
        inner["state"] = dict(reversed(list(inner["state"].items())))
        wire.correlate_response(request, frame(value))
