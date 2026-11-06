"""通常Mの承認入力、native起動と停止制御のrun所有者。"""
from copy import deepcopy
import ctypes
import json
import os
from pathlib import Path
import stat
import struct
import threading
import time
import uuid

from . import mod_control_operations as operations
from . import mod_control_wire as wire
from . import storage
from .build_inputs import canonical, digest, regular_bytes
from .local_ipc import MAX_MESSAGE, decode, plain_path, require_private
from .mod_control_startup import _Startup, _admission, _change
from .mod_control_transport import ModControlTransport, _WindowsNative
from .registry import RegistryError, fields, require
from .runtime_bundle import _read_managed_profile, _expand_managed_launch, verify_bundle


def approved_input(path, expected_hash, root, inventory):
    """privateな同handle/fdから64KiBまで読み、独立承認hashとinventoryへ結束する。"""
    wire._hash(expected_hash)
    require(path.is_absolute(), "INVALID_INPUT", "承認入力は絶対pathが必要です")
    plain_path(path)
    require_private(path.parent, directory=True)
    if os.name == "nt":
        native = _WindowsNative()
        handles = [native.open_anchor(path.anchor)]
        try:
            for name in path.parent.parts[1:]:
                handle = native.relative(handles[-1], name, directory=True)
                handles.append(handle)
                native.metadata(handle, directory=True)
            native.private(handles[-1])
            handle = native.relative(handles[-1], path.name)
            handles.append(handle)
            native.private(handle)
            before = native.metadata(handle)
            require(0 < before[1] <= MAX_MESSAGE, "INVALID_INPUT", "承認入力の長さが上限外です")
            buffer = ctypes.create_string_buffer(MAX_MESSAGE + 1)
            count = ctypes.c_uint32()
            native.check(native.kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None))
            raw = buffer.raw[:count.value]
            require(len(raw) == before[1] and native.metadata(handle) == before,
                    "INVALID_INPUT", "承認入力の実体・長さが変更されました")
        finally:
            for handle in reversed(handles):
                native.close_handle(handle)
    else:
        handles = [os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)]
        try:
            for name in path.parent.parts[1:]:
                handles.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
            parent = os.fstat(handles[-1])
            require(parent.st_uid == os.getuid() and stat.S_IMODE(parent.st_mode) == 0o700,
                    "INVALID_INPUT", "承認入力の親はprivateではありません")
            handles.append(os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=handles[-1]))
            before = os.fstat(handles[-1])
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.getuid()
                    and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= MAX_MESSAGE,
                    "INVALID_INPUT", "承認入力はprivateな有限の通常fileが必要です")
            raw = os.read(handles[-1], MAX_MESSAGE + 1)
            require(len(raw) == before.st_size and os.fstat(handles[-1]) == before,
                    "INVALID_INPUT", "承認入力が変更されました")
        finally:
            for handle in reversed(handles):
                os.close(handle)
    require(0 < len(raw) <= MAX_MESSAGE and wire.sha256(raw) == expected_hash,
            "INVALID_INPUT", "承認入力の長さ・原hashが一致しません")
    value = decode(raw)
    fields(value, {"managementRoot", "instances"})
    require(value["managementRoot"] == str(root) and type(value["instances"]) is list
            and len(value["instances"]) <= len(inventory["instances"]), "INVALID_INPUT", "承認対象root/件数が一致しません")
    allowed = {item["instanceId"] for item in inventory["instances"]}
    result = {}
    for item in value["instances"]:
        fields(item, {"instanceId", "artifactId", "runtimeManifestHash", "expectedEvidence"})
        require(item["instanceId"] in allowed and item["instanceId"] not in result,
                "INVALID_INPUT", "承認instanceが不正または重複しています")
        wire._hash(item["artifactId"])
        wire._hash(item["runtimeManifestHash"])
        wire._profile_inventory(item["expectedEvidence"])
        result[item["instanceId"]] = deepcopy(item)
    return raw, result


def process_identity(native, adapter, pid):
    """Pがnative観測したPID/startをDataの本人形式へ変換する。M本文を使用しない。"""
    identity = adapter.identity(pid)
    require(identity is not None, "PROCESS_MISMATCH", "所有process本人が存在しません")
    if native.transport == "windows-pipe":
        host, boot = native.host_boot()
        start = identity["startId"]
    else:
        host, boot = native._host_boot()
        info = ctypes.create_string_buffer(136)
        require(native.proc.proc_pidinfo(pid, 3, 0, info, len(info)) == len(info),
                "PROCESS_MISMATCH", "所有process開始識別を取得できません")
        start = "{}:{}".format(*struct.unpack_from("QQ", info.raw, 120))
    value = {"hostHash": host, "bootId": boot, "pid": pid, "startId": start}
    wire._identity(value)
    return value


def _closed_writers(connection, instance, run):
    """同runの全writerについて、耐久closeと実proxy/child退出証拠を列挙する。"""
    values = connection.execute("SELECT * FROM writer_sessions WHERE instance=? AND run=?", (instance, run)).fetchall()
    writers = []
    for value in values:
        require(value["status"] == "CLOSED" and not value["pending"], "WRITER_PENDING", "未終了writerが残っています")
        require(not connection.execute("SELECT 1 FROM writer_operations WHERE session=? AND status!='COMPLETED'", (value["id"],)).fetchone(),
                "WRITER_PENDING", "元writer要求が未完了です")
        closes = connection.execute("SELECT response FROM writer_operations WHERE session=? AND command='writer-close' AND status='COMPLETED'", (value["id"],)).fetchall()
        require(len(closes) == 1, "PROOF_REQUIRED", "writer終了の耐久記録が一意ではありません")
        closed = json.loads(closes[0][0])
        require(closed.get("proxyExited") is True and closed.get("proxyChildrenExited") is True
                and type(closed.get("proxyIdentity")) is dict, "PROOF_REQUIRED", "proxy/childの実exit証拠がありません")
        wire._identity(closed["proxyIdentity"])
        writers.append({"identity": closed["proxyIdentity"], "state": "CLOSED", "children": []})
    return writers


class ManagedRuntime:
    """inventory各枠に受信threadを一つ保持し、managed-stopとは別に進行する。"""
    def __init__(self, runtime, approvals, *, transport=None):
        self.runtime = runtime
        self.registry = runtime.registry
        self.approvals = deepcopy(approvals)
        self.transport = transport or ModControlTransport(
            [item["instanceId"] for item in self.registry.inventory["instances"]], wire_version=3)
        self.startup = _Startup(runtime, self.transport, version=3)
        self.sessions = {}
        self.lock = threading.Lock()

    def approved(self, row, generation):
        item = self.approvals.get(row["id"])
        require(item is not None, "PROFILE_REQUIRED", "通常起動の独立承認入力がありません")
        manifest = verify_bundle(generation / "runtime")
        raw = regular_bytes(generation / "runtime/launch/managed-server-candidate.json")
        require(item["artifactId"] == row["artifact"] == manifest["artifactId"]
                and item["runtimeManifestHash"] == wire.sha256(regular_bytes(generation / "runtime/runtime.json"))
                and item["expectedEvidence"]["profileSha256"] == wire.sha256(raw),
                "PROFILE_MISMATCH", "固定bundleと独立承認が一致しません")
        _read_managed_profile(raw)
        return item, raw

    def owns_socket(self, path, info):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            endpoint = session.get("endpoint")
            if endpoint is not None and endpoint.listener is not None and path == Path(endpoint.descriptor["endpoint"]["address"]):
                require_private(path.parent, directory=True)
                listener = endpoint.listener
                require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
                        and listener.identity == (info.st_dev, info.st_ino), "IPC_UNVERIFIED", "所有control socket実体が変わりました")
                return True
        return False

    def prepare(self, row, run, generation):
        approval, profile_raw = self.approved(row, generation)
        shop = generation / "config/SushiEricServerMod/shop_data"
        plain_path(shop)
        if not shop.exists():
            storage.mkdir(shop)
        require(shop.is_dir(), "ROOT_MISMATCH", "shop保存先はディレクトリが必要です")
        with self.lock:
            require(row["id"] not in self.sessions, "CONTROL_BUSY", "旧runの制御資源が残っています")
            session = {"run": run["runId"], "stop": threading.Event(), "done": threading.Event(),
                       "error": None, "close_ids": {}, "drain_lock": threading.Lock()}
            self.sessions[row["id"]] = session
        controller = process_identity(self.transport.native, self.runtime.process_adapter, os.getpid())
        world = self._world(generation)
        leaf = self.registry.root / "manager/control" / run["runId"]
        address = ("\\\\.\\pipe\\sushieric-mod-" + uuid.uuid4().hex if os.name == "nt" else str(leaf / "mod-control.sock"))
        descriptor = {"version": 1, "purpose": wire.PURPOSE, "descriptorId": uuid.uuid4().hex,
            "managementRoot": str(self.registry.root), "instanceId": row["id"], "leaseId": row["lease"],
            "epoch": row["epoch"], "generationId": row["generation"], "runtimeRunId": run["runId"],
            "startNonce": run["nonce"], "artifactId": row["artifact"], "runtimeManifestHash": run["manifestHash"],
            "worldRoot": str(world), "shopDirectory": str(shop), "controller": controller,
            "endpoint": {"transport": self.transport.native.transport, "address": address}}
        raw = canonical(descriptor)
        wire.decode_descriptor(raw)
        context = {key: descriptor[key] for key in ("managementRoot", "instanceId", "leaseId", "epoch", "generationId", "runtimeRunId", "controller")}
        context["descriptorSha256"] = wire.sha256(raw)
        session["endpoint"] = self.transport.create(context, raw)
        launch = {"bundle": str(generation / "runtime"), "run": str(generation), "auxiliary": str(leaf / "auxiliary"),
                  "privateTmp": str(leaf / "private-tmp"), "controlDescriptor": wire.descriptor_path(descriptor),
                  "controlDescriptorSha256": wire.sha256(raw), "runtimeManifest": str(generation / "runtime/runtime.json"),
                  "proofPath": run["proofPath"], "instanceId": row["id"], "generationId": row["generation"], "startNonce": run["nonce"]}
        session.update(descriptor=descriptor, raw=raw, approval=approval, profile_raw=profile_raw, launch=launch,
                       observed=_expand_managed_launch(_read_managed_profile(profile_raw), launch))
        return session

    @staticmethod
    def _world(generation):
        from .snapshot_data import properties
        return generation / properties((generation / "server.properties").read_bytes())["level-name"]

    def handshake(self, token, run, session, deadline):
        descriptor = session["descriptor"]
        owner = process_identity(self.transport.native, self.runtime.process_adapter, run["identity"]["pid"])
        require(owner["startId"] == run["identity"]["startId"],
                "PROCESS_MISMATCH", "生成時に保持したMの開始識別と一致しません")
        management = {"leaseId": descriptor["leaseId"], "epoch": descriptor["epoch"],
                      "generation": descriptor["generationId"], "controller": descriptor["controller"],
                      "drainSha256": wire.sha256(canonical({"run": run["runId"], "writers": []}))}
        context = {"descriptorJson": session["raw"].decode("utf-8"), "owner": owner,
                   "management": management, "expectedEvidence": session["approval"]["expectedEvidence"]}
        with self.runtime._transaction(token["instanceId"], read_only=True) as connection:
            revision = _admission(connection, self.registry._instance(connection, token["instanceId"]))["revision"]
        self.startup.start(token, profile_raw=session["profile_raw"], launch_context=session["launch"],
            observed=session["observed"], context=context, revision=revision, deadline=deadline,
            endpoint=session["endpoint"], defer_ready=True)
        session.update(context=context, token=deepcopy(token))

    def ready(self, row, connection):
        session = self.startup.sessions[row["id"]]
        require(session.get("acknowledged"), "CONTROL_PENDING", "ACK送信が未確認です")
        gate = _admission(connection, row)
        require(gate["gate"] == "CLOSED" and gate["revision"] == session["revision"]
                and gate["descriptor_hash"] == wire.sha256(session["context"]["descriptorJson"].encode()),
                "STALE_OPERATION", "readyまでに受付予約が変更されました")

    def listen(self, instance):
        session = self.sessions[instance]
        thread = threading.Thread(target=self._serve, args=(instance, session), name="mod-control-" + instance, daemon=True)
        session["thread"] = thread
        thread.start()

    def _serve(self, instance, session):
        channel = self.startup.sessions[instance]["connection"]
        request = None
        try:
            # 受信slotはrunに固定し、共有workerや停止drainのlockを保持しない。
            def idle():
                process = self.runtime.processes.get(session["run"])
                require(process is not None and process.poll() is None,
                        "CONTROL_UNKNOWN", "停止制御前にMが終了しました")
            while True:
                request = channel.receive_frame(time.monotonic() + 30, idle=idle)
                message = wire.correlate_binding_v3(request, session["context"],
                    pre_evidence_json=wire.decode_frame_v3(self.startup.sessions[instance]["hello"])["payload"]["profileEvidenceJson"])
                if message["phase"] != "BOUND":
                    break
                require(request == self.startup.sessions[instance]["request"], "STALE_RUN", "元BOUND原文だけを読み戻します")
                with self.runtime._transaction(instance, read_only=True) as connection:
                    row = self.registry._instance(connection, instance)
                    gate = _admission(connection, row)
                    require(row["run_id"] == session["run"] and gate["descriptor_hash"] == wire.sha256(session["raw"]),
                            "STALE_RUN", "別runへACKを提示しません")
                    saved = operations.readback(connection, instance, request)
                    require(saved is not None and wire.decode_frame_v3(saved)["phase"] == "ACK",
                            "CONTROL_PENDING", "元ACK原文がありません")
                require(channel.send_frame(saved, time.monotonic() + 30) == len(saved), "CONTROL_UNKNOWN", "ACK読戻し送信が不明です")
            require(message["phase"] == "STOP_BLOCK", "INVALID_INPUT", "停止要求が必要です")
            session["stop"].set()
            deadline = time.monotonic() + 30
            with self.runtime._lock_wait(deadline):
                self.reserve_stop(instance, session)
                with self.runtime._transaction(instance) as connection:
                    require(operations.reserve(connection, instance, request) is None,
                            "CONTROL_PENDING", "停止要求の再作用を許可しません")
                    row = self.registry._instance(connection, instance)
                    parent = connection.execute("SELECT id FROM runtime_operations WHERE instance=? AND command='stop' AND status='PENDING'", (instance,)).fetchone()
                    session["direct_stop"] = parent is None
                    session["stop_operation"] = parent[0] if parent else "direct-stop-" + uuid.uuid4().hex
                    session["control_operation"] = message["operationId"]
                    pending = {"runId": session["run"], "controlOperationId": message["operationId"]}
                    if parent is None:
                        connection.execute("INSERT INTO runtime_operations VALUES (?,?, 'stop',?,'PENDING',?)",
                            (session["stop_operation"], digest(session["raw"]), instance, json.dumps(pending)))
                    else:
                        connection.execute("UPDATE runtime_operations SET response=? WHERE id=?", (json.dumps(pending), parent[0]))
                    connection.execute("INSERT INTO reservations VALUES (?,?,?,?,?)",
                        (message["operationId"], "stop:" + session["stop_operation"], instance, row["lease"], row["epoch"]))
                self.drain(instance, session, deadline)
                with self.runtime._transaction(instance) as connection:
                    row, gate, writers = self.snapshot(connection, instance, session)
                    reply = self.reply(request, "BLOCKED", {"management": session["context"]["management"],
                                                              "registryRevision": gate["revision"]})
                    operations.complete(connection, instance, request, reply)
                require(channel.send_frame(reply, deadline) == len(reply), "CONTROL_UNKNOWN", "BLOCKED送信が不明です")
            request = channel.receive_frame(time.monotonic() + 30)
            deadline = time.monotonic() + 30
            with self.runtime._lock_wait(deadline):
                message = wire.correlate_binding_v3(request, session["context"])
                require(message["phase"] == "SAVE_REQUESTED", "INVALID_INPUT", "Requestedが必要です")
                requested = wire._inner(message["payload"], "requestJson", "requestSha256")
                bound = wire._status(wire._inner(wire.decode_frame_v3(self.startup.sessions[instance]["request"])["payload"],
                                               "statusJson", "statusJsonSha256"), "Running")
                state = wire._status(requested["status"], "ManagedSaveRequested")
                require(all(wire._same_json(state[key], bound[key]) for key in ("worldUuid", "runUuid", "generation", "paths"))
                        and state["stateRevision"] > bound["stateRevision"],
                        "STALE_RUN", "元Runningから進んだRequestedではありません")
                with self.runtime._transaction(instance) as connection:
                    row, gate, writers = self.snapshot(connection, instance, session)
                    require(operations.reserve(connection, instance, request) is None,
                            "CONTROL_PENDING", "Requestedの再作用を許可しません")
                    completion = {"request": requested, "snapshotId": uuid.uuid4().hex, "registryRevision": gate["revision"],
                                  "acceptance": "BLOCKED", "drain": "COMPLETE", "pendingCount": 0, "writers": writers}
                    reply = self.reply(request, "COMPLETION", {"requestSha256": message["payload"]["requestSha256"], "completion": completion})
                    operations.complete(connection, instance, request, reply)
                require(channel.send_frame(reply, deadline) == len(reply), "CONTROL_UNKNOWN", "COMPLETION送信が不明です")
                require(channel.wait_peer_close(deadline) is True, "CONTROL_UNKNOWN", "Mのcontrol closeが未確認です")
                require(session["endpoint"].close(deadline), "CONTROL_UNKNOWN", "control closeが未確認です")
                session["done"].set()
                self.runtime._direct_stop(instance, session)
        except BaseException as error:
            session["error"] = error
            try:
                with self.runtime._lock_wait(time.monotonic() + 30), self.runtime._transaction(instance) as connection:
                    row = self.registry._instance(connection, instance)
                    require(row["run_id"] == session["run"], "STALE_RUN", "別runを不明へ変更しません")
                    if request is not None and operations.readback(connection, instance, request) is not None:
                        operations.unknown(connection, instance, request)
                    gate = _admission(connection, row)
                    if gate["gate"] != "BLOCKED":
                        _change(connection, row, gate["revision"], "BLOCKED")
                    connection.execute("UPDATE instances SET state='RECOVERY_REQUIRED' WHERE id=?", (instance,))
            except BaseException as preserve_error:
                session["preservationError"] = preserve_error

    def release(self, instance, run_id):
        with self.lock:
            session = self.sessions[instance]
            require(session["run"] == run_id and session["done"].is_set() and session["error"] is None,
                    "CONTROL_UNKNOWN", "未終端control slotを解放しません")
            self.sessions.pop(instance)
            self.startup.sessions.pop(instance)

    @staticmethod
    def reply(request, phase, payload):
        message = wire.decode_frame_v3(request)
        body = canonical(dict(message, type="response", phase=phase,
                              payload={"requestBodySha256": wire.sha256(request[4:]), **payload}))
        reply = struct.pack(">I", len(body)) + body
        wire.correlate_response_v3(request, reply)
        return reply

    def reserve_stop(self, instance, session):
        with self.runtime._lock_wait(time.monotonic() + 30), self.runtime._transaction(instance) as connection:
            row = self.registry._instance(connection, instance)
            gate = _admission(connection, row)
            require(row["run_id"] == session["run"] and gate["descriptor_hash"] == wire.sha256(session["raw"]),
                    "STALE_RUN", "別runへ停止要求を適用しません")
            if row["state"] == "STOPPING":
                require(row["pending"] and gate["gate"] == "BLOCKED", "CONTROL_BLOCKED", "停止予約が一致しません")
                return
            require(row["state"] == "RUNNING" and not row["pending"] and gate["gate"] == "OPEN",
                    "CONTROL_BLOCKED", "稼働中の元runだけを停止します")
            _change(connection, row, gate["revision"], "BLOCKED")
            connection.execute("UPDATE instances SET state='STOPPING',pending=1 WHERE id=?", (instance,))

    def drain(self, instance, session, deadline):
        require(session["drain_lock"].acquire(timeout=max(0, deadline - time.monotonic())),
                "WRITER_PENDING", "同runのdrainが未完了です")
        try:
            with self.runtime._lock_wait(deadline):
                self._drain(instance, session, deadline)
        finally:
            session["drain_lock"].release()

    def _drain(self, instance, session, deadline):
        while True:
            with self.runtime._transaction(instance, read_only=True) as connection:
                row = self.registry._instance(connection, instance)
                require(row["run_id"] == session["run"], "STALE_RUN", "drain中にrunが変更されました")
                claims = connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'", (instance,)).fetchone()
                sessions = [dict(value) for value in connection.execute("SELECT * FROM writer_sessions WHERE instance=? AND run=? AND status!='CLOSED'", (instance, session["run"]))]
            require(time.monotonic() < deadline, "WRITER_PENDING", "writer drain期限を超過しました")
            if claims:
                time.sleep(0.01)
                continue
            if not sessions:
                return
            for writer in sessions:
                payload = dict(session["token"], sessionId=writer["id"], runId=session["run"],
                               nonce=writer["nonce"], drained=True, disconnected=True)
                require(writer["id"] in self.registry.writer.database_proxies,
                        "WRITER_PENDING", "外部writerのdrain/終了証拠がありません")
                operation = session["close_ids"].setdefault(writer["id"], uuid.uuid4().hex)
                fingerprint = digest(canonical({"command": "writer-close", "payload": payload}))
                try:
                    result = self.registry.writer._database_close(payload, operation, fingerprint, False, deadline=deadline)
                except RegistryError as error:
                    with self.runtime._transaction(instance, read_only=True) as connection:
                        accepted = connection.execute("SELECT 1 FROM writer_operations WHERE id=?", (operation,)).fetchone()
                        competing = connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'", (instance,)).fetchone()
                    if error.code == "WRITER_PENDING" and accepted is None and competing:
                        break
                    raise
                require(result.get("closed") is True, "WRITER_PENDING", "proxy/child終了が未確認です")

    def snapshot(self, connection, instance, session):
        row = self.registry._instance(connection, instance)
        gate = _admission(connection, row)
        require(row["run_id"] == session["run"] and row["state"] == "STOPPING" and row["pending"]
                and gate["gate"] == "BLOCKED" and not row["writers"] and not row["children"],
                "WRITER_PENDING", "同runの閉門・全writer/child終了が未確認です")
        require(not connection.execute("SELECT 1 FROM reservations WHERE instance=? AND reference LIKE 'writer-call:%'", (instance,)).fetchone(),
                "WRITER_PENDING", "writer callが残っています")
        writers = _closed_writers(connection, instance, session["run"])
        return row, gate, writers
