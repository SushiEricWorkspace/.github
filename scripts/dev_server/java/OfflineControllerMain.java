package io.github.sushiericworkspace.devserver;

import io.github.sushiericworkspace.sushiericdatamodel.database.run.*;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterAction;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterCodec;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterRequest;
import kotlinx.serialization.json.*;
import java.io.*;
import java.nio.ByteBuffer;
import java.nio.charset.*;
import java.nio.file.Path;
import java.util.*;

/**
 * Pが所有するprivate stdio専用の停止中controller。受付遮断・drain・start排他は起動元Pの責務。
 * EOFや作用後失敗でownerを自動closeしない。CLOSEの返答はCの実終了やRegistry CLOSEDの証明ではない。
 */
public final class OfflineControllerMain {
    static final int FRAME = 65_536;
    private static final Json JSON = Json.Default;
    private final DataInputStream input;
    private final DataOutputStream output;
    private WorldProcessIdentity parent;
    private WorldProcessIdentity self;
    private WorldOfflineWriterSession writer;
    private String session;
    private String nonce;
    private String openId;
    private boolean effect;

    OfflineControllerMain(InputStream input, OutputStream output) {
        this.input = new DataInputStream(input);
        this.output = new DataOutputStream(output);
    }

    public static void main(String[] arguments) throws Exception {
        require(arguments.length == 0);
        new OfflineControllerMain(System.in, System.out).run();
    }

    void run() throws Exception {
        for (;;) {
            JsonObject frame;
            try { frame = read(input); }
            catch (EOFException end) {
                if (effect) throw new IOException("owner終了は未確認です");
                return;
            } catch (Exception invalid) {
                write(packet(effect ? "UNKNOWN" : "REJECTED", null, null));
                if (effect) throw new IOException("controller入力不明。元stateを保全してください");
                return;
            }
            String id = null;
            String hash = null;
            try {
                id = string(frame, "operationId");
                hash = string(frame, "requestHash");
                identifier(id); sha256(hash);
                String command = string(frame, "command");
                JsonObject response;
                if (command.equals("open")) response = open(frame, id, hash);
                else if (command.equals("assign")) response = assign(frame, id, hash);
                else if (command.equals("close")) response = close(frame, id, hash);
                else throw new IllegalArgumentException("未知のcontroller操作です");
                write(response);
                if (command.equals("close")) return;
            } catch (Exception failure) {
                write(packet(effect ? "UNKNOWN" : "REJECTED", id, hash));
                // Dataのcurrent公開後に返答だけ失われた可能性も含め、再取得しない。
                if (effect) throw new IOException("controller結果不明。元stateを保全してください");
                return;
            }
        }
    }

    private JsonObject open(JsonObject frame, String id, String hash) {
        validateOpen(frame);
        require(writer == null && !effect);
        self = identity(object(frame, "owner"));
        parent = identity(object(frame, "controller"));
        require(self.equals(WorldProcessIdentities.INSTANCE.current()));
        require(ProcessHandle.current().parent().orElseThrow().pid() == parent.getPid());
        checkActors();
        WorldManagementBinding management = new WorldManagementBinding(
                string(frame, "leaseId"), number(frame, "epoch"), string(frame, "generation"),
                parent, string(frame, "drainSha256"));
        WorldRunLifecycle lifecycle = new WorldRunLifecycle(
                Path.of(string(frame, "worldRoot")), Path.of(string(frame, "shopDirectory")),
                UUID.fromString(string(frame, "worldUuid")), 5000);
        WorldRunStatus expected = lifecycle.readStatus();
        require(expected.getSha256().equals(string(frame, "expectedStateSha256")) &&
                expected.getState().getStateRevision() == number(frame, "expectedRevision") &&
                expected.getState().getRunUuid().equals(string(frame, "runUuid")) &&
                expected.getState().getGeneration().equals(management.getGeneration()) &&
                expected.getState().getPayload() instanceof WorldRunPhase.Ready);
        session = string(frame, "sessionId");
        nonce = string(frame, "nonce");
        openId = id;
        checkActors();
        effect = true;
        writer = lifecycle.openManagedOfflineWriter(expected, new WorldOfflineOwnerBinding(self, management));
        return status("OPEN", id, hash, writer.getStatus());
    }

    private JsonObject assign(JsonObject frame, String id, String hash) {
        require(writer != null);
        WorldRunStatus expected = writer.getStatus();
        require(expected.getState().getPayload() instanceof WorldRunPhase.OfflineIdle);
        fields(frame, "command", "operationId", "requestHash", "sessionId", "nonce", "openOperationId",
                "expectedStateSha256", "expectedRevision", "runUuid", "worker", "request");
        uuid(id);
        require(!id.equals(openId) && session.equals(string(frame, "sessionId")) &&
                nonce.equals(string(frame, "nonce")) && openId.equals(string(frame, "openOperationId")) &&
                expected.getSha256().equals(string(frame, "expectedStateSha256")) &&
                expected.getState().getStateRevision() == number(frame, "expectedRevision") &&
                expected.getState().getRunUuid().equals(string(frame, "runUuid")));
        HostAdapterRequest request = HostAdapterCodec.INSTANCE.decodeRequest(
                object(frame, "request").toString().getBytes(StandardCharsets.UTF_8));
        require(request.getRequestId().equals(id) &&
                request.getWorldUuid().equals(expected.getState().getWorldUuid()) &&
                request.getAction() instanceof HostAdapterAction.Edit);
        Long deadline = request.getDeadlineEpochMillis();
        require(deadline == null || System.currentTimeMillis() < deadline);
        WorldProcessIdentity worker = identity(object(frame, "worker"));
        require(worker.getPid() != self.getPid() && worker.getPid() != parent.getPid());
        require(worker.getHostHash().equals(self.getHostHash()) && worker.getBootId().equals(self.getBootId()));
        checkActors();
        WorldProcessIdentities.INSTANCE.requireAlive(worker);
        WorldRunStatus assigned = writer.assignWorker(worker, request, expected);
        WorldRunPhase.OfflineAssigned phase = (WorldRunPhase.OfflineAssigned) assigned.getState().getPayload();
        Map<String, JsonElement> response = new LinkedHashMap<>(status("ASSIGNED", id, hash, assigned));
        response.put("editRequestSha256", JsonElementKt.JsonPrimitive(phase.getRequestSha256()));
        return new JsonObject(response);
    }

    private JsonObject close(JsonObject frame, String id, String hash) {
        require(writer != null);
        WorldRunStatus expected = writer.getStatus();
        validateClose(frame, session, nonce, openId, expected.getSha256(), expected.getState().getStateRevision());
        checkActors();
        writer.close();
        return status("OWNER_CLOSED", id, hash, writer.getStatus());
    }

    private void checkActors() {
        require(self.equals(WorldProcessIdentities.INSTANCE.current()));
        WorldProcessIdentities.INSTANCE.requireAlive(parent);
    }

    static void validateClose(JsonObject frame, String session, String nonce, String openId,
                              String stateHash, long revision) {
        fields(frame, "command", "operationId", "requestHash", "sessionId", "nonce", "openOperationId",
                "expectedStateSha256", "expectedRevision");
        require(string(frame, "command").equals("close") && !string(frame, "operationId").equals(openId));
        identifier(string(frame, "operationId")); sha256(string(frame, "requestHash"));
        require(session.equals(string(frame, "sessionId")) && nonce.equals(string(frame, "nonce")) &&
                openId.equals(string(frame, "openOperationId")) &&
                stateHash.equals(string(frame, "expectedStateSha256")) &&
                revision == number(frame, "expectedRevision"));
    }

    static void validateOpen(JsonObject frame) {
        fields(frame, "command", "operationId", "requestHash", "sessionId", "nonce", "leaseId", "epoch",
                "generation", "controller", "owner", "drainSha256", "worldRoot", "shopDirectory",
                "worldUuid", "runUuid", "expectedStateSha256", "expectedRevision");
        require(string(frame, "command").equals("open"));
        identifier(string(frame, "operationId")); identifier(string(frame, "sessionId"));
        identifier(string(frame, "nonce")); sha256(string(frame, "requestHash"));
        uuid(string(frame, "leaseId")); uuid(string(frame, "generation"));
        uuid(string(frame, "worldUuid")); uuid(string(frame, "runUuid"));
        require(number(frame, "epoch") >= 0 && number(frame, "expectedRevision") >= 0);
        sha256(string(frame, "drainSha256")); sha256(string(frame, "expectedStateSha256"));
        WorldProcessIdentity owner = identity(object(frame, "owner"));
        WorldProcessIdentity controller = identity(object(frame, "controller"));
        require(!owner.equals(controller) && owner.getHostHash().equals(controller.getHostHash()) &&
                owner.getBootId().equals(controller.getBootId()));
        require(Path.of(string(frame, "worldRoot")).isAbsolute() &&
                Path.of(string(frame, "shopDirectory")).isAbsolute());
    }

    static JsonObject read(DataInputStream input) throws Exception {
        // header途中のEOFを空入力と区別する。受信済みframeの残りを再要求しない。
        int first = input.read();
        if (first < 0) throw new EOFException();
        byte[] tail = input.readNBytes(3);
        require(tail.length == 3);
        int size = (first << 24) | ((tail[0] & 255) << 16) | ((tail[1] & 255) << 8) | (tail[2] & 255);
        require(size > 0 && size <= FRAME - 4);
        byte[] body = input.readNBytes(size);
        require(body.length == size);
        for (byte value : body) require(value != 10 && value != 13);
        String text = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(body)).toString();
        JsonElement element = JSON.parseToJsonElement(text);
        require(element instanceof JsonObject);
        return (JsonObject) element;
    }

    private void write(JsonObject frame) throws IOException {
        byte[] body = frame.toString().getBytes(StandardCharsets.UTF_8);
        require(body.length + 4 <= FRAME);
        output.writeInt(body.length); output.write(body); output.flush();
    }

    private JsonObject status(String state, String id, String hash, WorldRunStatus value) {
        Map<String, JsonElement> result = new LinkedHashMap<>(packet(state, id, hash));
        result.put("sessionId", JsonElementKt.JsonPrimitive(session));
        result.put("nonce", JsonElementKt.JsonPrimitive(nonce));
        result.put("stateSha256", JsonElementKt.JsonPrimitive(value.getSha256()));
        result.put("stateRevision", JsonElementKt.JsonPrimitive(value.getState().getStateRevision()));
        result.put("runUuid", JsonElementKt.JsonPrimitive(value.getState().getRunUuid()));
        return new JsonObject(result);
    }

    private static JsonObject packet(String status, String id, String hash) {
        Map<String, JsonElement> values = new LinkedHashMap<>();
        values.put("status", JsonElementKt.JsonPrimitive(status));
        if (id != null) values.put("operationId", JsonElementKt.JsonPrimitive(id));
        if (hash != null) values.put("requestHash", JsonElementKt.JsonPrimitive(hash));
        return new JsonObject(values);
    }

    private static WorldProcessIdentity identity(JsonObject value) {
        fields(value, "hostHash", "bootId", "pid", "startId");
        sha256(string(value, "hostHash"));
        return new WorldProcessIdentity(string(value, "hostHash"), string(value, "bootId"),
                number(value, "pid"), string(value, "startId"));
    }
    private static JsonObject object(JsonObject value, String key) { JsonElement item = value.get(key); require(item instanceof JsonObject); return (JsonObject)item; }
    private static String string(JsonObject value, String key) { JsonElement item = value.get(key); require(item instanceof JsonPrimitive && ((JsonPrimitive)item).isString()); return ((JsonPrimitive)item).getContent(); }
    private static long number(JsonObject value, String key) { JsonElement item = value.get(key); require(item instanceof JsonPrimitive && !((JsonPrimitive)item).isString()); String text = ((JsonPrimitive)item).getContent(); require(text.matches("0|[1-9][0-9]*")); return Long.parseLong(text); }
    private static void fields(JsonObject value, String... keys) { require(value.keySet().equals(Set.of(keys))); }
    private static void identifier(String value) { require(value.matches("[a-z0-9][a-z0-9-]{0,127}")); }
    private static void sha256(String value) { require(value.matches("[0-9a-f]{64}")); }
    private static void uuid(String value) { require(UUID.fromString(value).toString().equals(value)); }
    private static void require(boolean value) { if (!value) throw new IllegalArgumentException("限定controller契約に一致しません"); }
}
