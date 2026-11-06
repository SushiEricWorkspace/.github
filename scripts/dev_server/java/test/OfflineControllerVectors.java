package io.github.sushiericworkspace.devserver;

import kotlinx.serialization.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.*;

public final class OfflineControllerVectors {
    private static int count;
    private static final Json JSON = Json.Default;
    private static final String HASH = "a".repeat(64);
    private static final String UUID = "00000000-0000-4000-8000-000000000001";

    private static JsonObject open() {
        Map<String, JsonElement> values = new LinkedHashMap<>();
        for (String key : List.of("command", "operationId", "requestHash", "sessionId", "nonce", "leaseId",
                "generation", "drainSha256", "worldRoot", "shopDirectory", "worldUuid", "runUuid", "expectedStateSha256"))
            values.put(key, JsonElementKt.JsonPrimitive(switch (key) {
                case "command" -> "open";
                case "requestHash", "drainSha256", "expectedStateSha256" -> HASH;
                case "leaseId", "generation", "worldUuid", "runUuid" -> UUID;
                case "worldRoot", "shopDirectory" -> Path.of(".").toAbsolutePath().toString();
                default -> key.toLowerCase(Locale.ROOT);
            }));
        values.put("epoch", JsonElementKt.JsonPrimitive(1));
        values.put("expectedRevision", JsonElementKt.JsonPrimitive(0));
        values.put("owner", identity(11));
        values.put("controller", identity(12));
        return new JsonObject(values);
    }
    private static JsonObject identity(long pid) {
        return new JsonObject(Map.of("hostHash", JsonElementKt.JsonPrimitive(HASH),
                "bootId", JsonElementKt.JsonPrimitive("fixture-boot"), "pid", JsonElementKt.JsonPrimitive(pid),
                "startId", JsonElementKt.JsonPrimitive("fixture-start")));
    }
    private static JsonObject changed(JsonObject value, String field, JsonElement replacement) {
        Map<String, JsonElement> result = new LinkedHashMap<>(value);
        result.put(field, replacement);
        return new JsonObject(result);
    }
    private static void rejected(Checked action) throws Exception {
        try { action.run(); }
        catch (IllegalArgumentException | IOException expected) { count++; return; }
        throw new AssertionError("拒否されませんでした");
    }
    private interface Checked { void run() throws Exception; }
    private static byte[] frame(String value) throws Exception {
        byte[] body = value.getBytes(StandardCharsets.UTF_8);
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        DataOutputStream output = new DataOutputStream(bytes);
        output.writeInt(body.length); output.write(body);
        return bytes.toByteArray();
    }
    private static void read(byte[] value) throws Exception {
        OfflineControllerMain.read(new DataInputStream(new ByteArrayInputStream(value)));
    }

    public static void main(String[] arguments) throws Exception {
        JsonObject original = open();
        OfflineControllerMain.validateOpen(original); count++;
        for (String field : original.keySet()) {
            Map<String, JsonElement> missing = new LinkedHashMap<>(original);
            missing.remove(field);
            rejected(() -> OfflineControllerMain.validateOpen(new JsonObject(missing)));
        }
        for (String field : List.of("requestHash", "drainSha256", "expectedStateSha256"))
            rejected(() -> OfflineControllerMain.validateOpen(changed(original, field, JsonElementKt.JsonPrimitive("bad"))));
        for (String field : List.of("leaseId", "generation", "worldUuid", "runUuid"))
            rejected(() -> OfflineControllerMain.validateOpen(changed(original, field, JsonElementKt.JsonPrimitive("old"))));
        for (String field : List.of("epoch", "expectedRevision")) {
            rejected(() -> OfflineControllerMain.validateOpen(changed(original, field, JsonElementKt.JsonPrimitive(-1))));
            rejected(() -> OfflineControllerMain.validateOpen(changed(original, field, JsonElementKt.JsonPrimitive("1"))));
            rejected(() -> OfflineControllerMain.validateOpen(changed(original, field, JSON.parseToJsonElement("1.0"))));
            rejected(() -> OfflineControllerMain.validateOpen(changed(original, field, JSON.parseToJsonElement("9223372036854775808"))));
        }
        rejected(() -> OfflineControllerMain.validateOpen(changed(original, "owner", original.get("controller"))));
        JsonObject otherHost = changed((JsonObject)original.get("owner"), "hostHash", JsonElementKt.JsonPrimitive("b".repeat(64)));
        rejected(() -> OfflineControllerMain.validateOpen(changed(original, "owner", otherHost)));
        JsonObject otherBoot = changed((JsonObject)original.get("owner"), "bootId", JsonElementKt.JsonPrimitive("old-boot"));
        rejected(() -> OfflineControllerMain.validateOpen(changed(original, "owner", otherBoot)));
        rejected(() -> OfflineControllerMain.validateOpen(changed(original, "worldRoot", JsonElementKt.JsonPrimitive("relative"))));
        rejected(() -> OfflineControllerMain.validateOpen(changed(original, "sql", JsonElementKt.JsonPrimitive("delete"))));
        rejected(() -> OfflineControllerMain.validateOpen(changed(original, "command", JsonElementKt.JsonPrimitive("execute"))));
        read(frame(original.toString())); count++;
        for (byte[] bytes : List.of(new byte[]{0}, new byte[]{0, 0, 0}, new byte[]{0, 0, 0, 0},
                new byte[]{0, 1, 0, 0}, new byte[]{-1, -1, -1, -1}, new byte[]{0, 0, 0, 2, 123},
                new byte[]{0, 0, 0, 1, -1}))
            rejected(() -> read(bytes));
        for (String text : List.of("[]", "null", "{}", "{\n}", "{", "\"text\"")) {
            if (!text.equals("{}")) rejected(() -> read(frame(text)));
        }
        JsonObject closeFrame = (JsonObject)JSON.parseToJsonElement(
                "{\"command\":\"close\",\"operationId\":\"close\",\"requestHash\":\"" + HASH +
                "\",\"sessionId\":\"session\",\"nonce\":\"nonce\",\"openOperationId\":\"open\"," +
                "\"expectedStateSha256\":\"" + HASH + "\",\"expectedRevision\":1}");
        OfflineControllerMain.validateClose(closeFrame, "session", "nonce", "open", HASH, 1); count++;
        for (String field : List.of("sessionId", "nonce", "openOperationId", "expectedStateSha256"))
            rejected(() -> OfflineControllerMain.validateClose(changed(closeFrame, field, JsonElementKt.JsonPrimitive("old")),
                    "session", "nonce", "open", HASH, 1));
        rejected(() -> OfflineControllerMain.validateClose(closeFrame, "session", "nonce", "open", HASH, 2));
        rejected(() -> OfflineControllerMain.validateClose(changed(closeFrame, "operationId", JsonElementKt.JsonPrimitive("open")),
                "session", "nonce", "open", HASH, 1));
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        new OfflineControllerMain(new ByteArrayInputStream(new byte[0]), out).run();
        if (out.size() != 0) throw new AssertionError(); count++;
        String close = "{\"command\":\"close\",\"operationId\":\"close\",\"requestHash\":\"" + HASH + "\"}";
        out = new ByteArrayOutputStream();
        new OfflineControllerMain(new ByteArrayInputStream(frame(close)), out).run();
        JsonObject result = OfflineControllerMain.read(new DataInputStream(new ByteArrayInputStream(out.toByteArray())));
        if (!result.get("status").equals(JsonElementKt.JsonPrimitive("REJECTED"))) throw new AssertionError();
        count++;
        System.out.println("offline-vectors=" + count);
    }
}
