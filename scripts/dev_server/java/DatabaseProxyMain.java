package io.github.sushiericworkspace.devserver;

import io.github.sushiericworkspace.sushiericdatamodel.database.host.*;
import io.github.sushiericworkspace.sushiericservermod.management.*;
import io.github.sushiericworkspace.sushiericservermod.management.database.ManagementDatabaseBinding;
import kotlinx.serialization.json.*;
import java.io.*;
import java.net.URI;
import java.net.http.*;
import java.nio.ByteBuffer;
import java.nio.charset.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;

/** 固定stdioから受けたDB専用要求だけを、一つの所有Management接続で処理する。 */
public final class DatabaseProxyMain {
    private static final int FRAME = 65_536;
    private static final int WIRE = 60 * 1024;
    private static final Json JSON = Json.Default;
    private final DataInputStream input = new DataInputStream(System.in);
    private final DataOutputStream output = new DataOutputStream(System.out);
    private Link link;
    private ManagementDatabaseBinding binding;
    private Operation operation;
    private ManagementServerMessage known;
    private boolean unknown;
    private String handle;
    private boolean closing;
    private String closeId;
    private String closeHash;
    private boolean peerCloseSent;

    private record Operation(String id, String hash, ManagementClientMessage message, String wire) {}

    public static void main(String[] arguments) throws Exception {
        if (arguments.length != 0) throw new IllegalArgumentException("固定entrypointに引数はありません");
        new DatabaseProxyMain().run();
    }

    private void run() throws Exception {
        try {
            for (;;) {
                JsonObject frame;
                try { frame = read(); }
                catch (EOFException end) { return; }
                catch (Exception invalid) { write(packet("REJECTED", null, null, "INVALID_FRAME")); return; }
                String id = null;
                String hash = null;
                try {
                    id = string(frame, "operationId");
                    hash = string(frame, "requestHash");
                    require(id.matches("[a-z0-9][a-z0-9-]{0,127}") && hash.matches("[0-9a-f]{64}"));
                    String command = string(frame, "command");
                    long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeout(frame));
                    JsonObject result;
                    switch (command) {
                        case "open" -> result = open(frame, id, hash, deadline);
                        case "execute" -> result = execute(frame, id, hash, deadline);
                        case "resolve" -> result = resolve(frame, id, hash, deadline);
                        case "close" -> result = close(frame, id, hash, deadline);
                        default -> throw new IllegalArgumentException("未知の専用操作です");
                    }
                    write(result);
                    if (Set.of("CLOSED", "UNBOUND_CLOSED").contains(string(result, "status"))) return;
                } catch (TimeoutException timeout) {
                    write(packet("PENDING", id, hash, "TIMEOUT"));
                } catch (IllegalArgumentException rejected) {
                    if (operation != null && known == null || unknown) {
                        unknown = true;
                        write(packet("UNKNOWN", id, hash, "CONNECTION_OR_RESULT_UNKNOWN"));
                    } else write(packet("REJECTED", id, hash, "INVALID_REQUEST"));
                } catch (Exception failure) {
                    unknown = true;
                    write(packet("UNKNOWN", id, hash, "CONNECTION_OR_RESULT_UNKNOWN"));
                }
            }
        } finally {
            if (link != null) link.shutdown();
        }
    }

    private JsonObject open(JsonObject frame, String id, String hash, long deadline) throws Exception {
        fields(frame, "command", "operationId", "requestHash", "timeoutMillis", "port", "instanceId", "generationId", "startNonce");
        require(link == null && !closing);
        int port = integer(frame, "port");
        require(port > 0 && port <= 65_535);
        String instance = string(frame, "instanceId");
        String generation = string(frame, "generationId");
        String startNonce = string(frame, "startNonce");
        require(!instance.isEmpty() && !generation.isEmpty() && !startNonce.isEmpty());
        link = new Link();
        link.connect(port, deadline);
        Operation initial = make(id, hash, new ManagementClientMessage.DatabaseContextRequest(id));
        operation = initial;
        link.send(initial.wire(), deadline);
        ManagementServerMessage response = response(initial, deadline);
        require(response instanceof ManagementServerMessage.DatabaseContext);
        ManagementServerMessage.DatabaseContext context = (ManagementServerMessage.DatabaseContext) response;
        known = response;
        operation = null;
        require(context.getSuccess() && context.getBinding() != null);
        ManagementDatabaseBinding value = context.getBinding();
        uuid(value.getWorldUuid()); uuid(value.getRunId());
        require(instance.equals(value.getInstanceId()) && generation.equals(value.getGenerationId()) && value.getStartNonce() == null);
        binding = new ManagementDatabaseBinding(value.getWorldUuid(), value.getRunId(), instance, generation, startNonce);
        known = response;
        operation = null;
        return result("OPEN", id, hash, response);
    }

    private JsonObject execute(JsonObject frame, String id, String hash, long deadline) throws Exception {
        fields(frame, "command", "operationId", "requestHash", "timeoutMillis", "message");
        require(binding != null && !closing);
        ManagementClientMessage message = JSON.decodeFromString(ManagementClientMessage.Companion.serializer(), frame.get("message").toString());
        message = checked(message, id);
        Operation next = make(id, hash, message);
        if (operation != null) {
            require(operation.id().equals(id) && operation.hash().equals(hash) && operation.wire().equals(next.wire()));
            if (known != null) return result(unknown ? "UNKNOWN" : "RESULT", id, hash, known);
            return finish(deadline);
        }
        require(!unknown);
        operation = next;
        known = null;
        link.send(next.wire(), deadline);
        return finish(deadline);
    }

    private JsonObject resolve(JsonObject frame, String id, String hash, long deadline) throws Exception {
        fields(frame, "command", "operationId", "requestHash", "timeoutMillis");
        require(operation != null && operation.id().equals(id) && operation.hash().equals(hash));
        if (known == null) return finish(deadline);
        if (!unknown) return result("RESULT", id, hash, known);
        return result("UNKNOWN", id, hash, known);
    }

    private JsonObject finish(long deadline) throws Exception {
        ManagementServerMessage value = response(operation, deadline);
        unknown = false;
        if (value instanceof ManagementServerMessage.DatabaseContext context) {
            require(context.getSuccess() && context.getBinding() != null && samePublicBinding(context.getBinding()));
        } else if (value instanceof ManagementServerMessage.DatabaseHistory history) {
            if (operation.message() instanceof ManagementClientMessage.DatabaseHistoryStart) {
                if (history.getSuccess()) { require(history.getHandle() != null); uuid(history.getHandle()); handle = history.getHandle(); }
                else require(history.getHandle() == null);
            }
            if (operation.message() instanceof ManagementClientMessage.DatabaseHistoryPage && history.getSuccess()) require(Objects.equals(handle, history.getHandle()));
            if (operation.message() instanceof ManagementClientMessage.DatabaseHistoryEnd) {
                if (history.getSuccess() && Objects.equals(history.getHandle(), handle)) handle = null;
                else unknown = true;
            }
        } else if (value instanceof ManagementServerMessage.DatabaseProductResult product) {
            HostAdapterResponse body = product.getResponse();
            require(operation.message() instanceof ManagementClientMessage.DatabaseProductRequest);
            require(operation.id().equals(body.getRequestId()));
            HostAdapterCodec.INSTANCE.decodeResponse(HostAdapterCodec.INSTANCE.encodeResponse(body));
            if (body.getStatus() == HostAdapterStatus.SUCCESS) {
                HostAdapterAction action = ((ManagementClientMessage.DatabaseProductRequest)operation.message()).getRequest().getAction();
                require(action instanceof HostAdapterAction.Edit && body.getResult() instanceof HostAdapterResult.Edited ||
                        action instanceof HostAdapterAction.Products && body.getResult() instanceof HostAdapterResult.Products ||
                        action instanceof HostAdapterAction.DirectoryManifests && body.getResult() instanceof HostAdapterResult.DirectoryManifests);
            }
            unknown = body.getStatus() == HostAdapterStatus.UNKNOWN ||
                    body.getStatus() == HostAdapterStatus.REJECTED && !body.getRejectedBeforeEffect();
        } else if (value instanceof ManagementServerMessage.MoneyHistorySaveResult money) {
            unknown = !money.getSuccess() || money.getPendingCount() != 0;
        }
        known = value;
        return result(unknown ? "UNKNOWN" : "RESULT", operation.id(), operation.hash(), value);
    }

    private JsonObject close(JsonObject frame, String id, String hash, long deadline) throws Exception {
        fields(frame, "command", "operationId", "requestHash", "timeoutMillis");
        require(link != null);
        if (closing) require(id.equals(closeId) && hash.equals(closeHash));
        else { closing = true; closeId = id; closeHash = hash; }
        if (unknown && operation != null) return packet("UNKNOWN", id, hash, "UNRESOLVED_REQUEST");
        if (operation != null && known == null && !(operation.message() instanceof ManagementClientMessage.DatabaseHistoryEnd && operation.id().equals(id)))
            return packet("PENDING", id, hash, "UNRESOLVED_REQUEST");
        if (handle != null) {
            if (operation == null || !(operation.message() instanceof ManagementClientMessage.DatabaseHistoryEnd) ||
                    !operation.id().equals(id)) {
                operation = make(id, hash, new ManagementClientMessage.DatabaseHistoryEnd(handle, id));
                known = null;
                link.send(operation.wire(), deadline);
            }
            JsonObject ended = finish(deadline);
            require(string(ended, "status").equals("RESULT") && handle == null);
        }
        if (!peerCloseSent) { link.beginClose(deadline); peerCloseSent = true; }
        link.closed(deadline);
        JsonObject result = packet(binding == null ? "UNBOUND_CLOSED" : "CLOSED", id, hash, null);
        Map<String, JsonElement> values = new LinkedHashMap<>(result);
        values.put("historyEnded", JsonElementKt.JsonPrimitive(true));
        values.put("pending", JsonElementKt.JsonPrimitive(0));
        values.put("peerClosed", JsonElementKt.JsonPrimitive(!link.connectionFailed));
        if (binding != null) {
            values.put("worldUuid", JsonElementKt.JsonPrimitive(binding.getWorldUuid()));
            values.put("databaseRunId", JsonElementKt.JsonPrimitive(binding.getRunId()));
        }
        return new JsonObject(values);
    }

    private ManagementClientMessage checked(ManagementClientMessage message, String id) {
        require(link != null);
        if (operation != null && known != null && !unknown && !operation.id().equals(id)) { operation = null; known = null; }
        return switch (message) {
            case ManagementClientMessage.DatabaseContextRequest value -> { nonce(value.getNonce(), id); yield new ManagementClientMessage.DatabaseContextRequest(id); }
            case ManagementClientMessage.DatabaseHistoryStart value -> {
                nonce(value.getNonce(), id); require(handle == null && samePublicBinding(value.getBinding()));
                yield new ManagementClientMessage.DatabaseHistoryStart(binding, value.getFilter(), id);
            }
            case ManagementClientMessage.DatabaseHistoryPage value -> {
                nonce(value.getNonce(), id); require(Objects.equals(handle, value.getHandle()) && handle != null && value.getOffset() >= 0 && value.getLimit() >= 1 && value.getLimit() <= 1000);
                yield new ManagementClientMessage.DatabaseHistoryPage(handle, value.getOffset(), value.getLimit(), id);
            }
            case ManagementClientMessage.DatabaseHistoryEnd value -> {
                nonce(value.getNonce(), id); require(Objects.equals(handle, value.getHandle()) && handle != null);
                yield new ManagementClientMessage.DatabaseHistoryEnd(handle, id);
            }
            case ManagementClientMessage.DatabaseProductRequest value -> {
                nonce(value.getNonce(), id); require(samePublicBinding(value.getBinding()));
                HostAdapterRequest request = value.getRequest(); uuid(id);
                require(id.equals(request.getRequestId()) && binding.getWorldUuid().equals(request.getWorldUuid()));
                require(request.getAction() instanceof HostAdapterAction.Products || request.getAction() instanceof HostAdapterAction.DirectoryManifests || request.getAction() instanceof HostAdapterAction.Edit);
                byte[] bytes = HostAdapterCodec.INSTANCE.encodeRequest(request); require(bytes.length <= 56 * 1024);
                HostAdapterCodec.INSTANCE.decodeRequest(bytes);
                yield new ManagementClientMessage.DatabaseProductRequest(binding, request, id);
            }
            case ManagementClientMessage.MoneyHistorySave value -> { nonce(value.getNonce(), id); if (value.getPlayerUuid() != null) uuid(value.getPlayerUuid()); yield new ManagementClientMessage.MoneyHistorySave(value.getPlayerUuid(), id); }
            default -> throw new IllegalArgumentException("DB専用種別ではありません");
        };
    }

    private boolean samePublicBinding(ManagementDatabaseBinding value) {
        return binding.getWorldUuid().equals(value.getWorldUuid()) && binding.getRunId().equals(value.getRunId()) &&
                Objects.equals(binding.getInstanceId(), value.getInstanceId()) && Objects.equals(binding.getGenerationId(), value.getGenerationId()) &&
                (value.getStartNonce() == null || binding.getStartNonce().equals(value.getStartNonce()));
    }

    private Operation make(String id, String hash, ManagementClientMessage message) {
        String wire = JSON.encodeToString(ManagementClientMessage.Companion.serializer(), message);
        require(wire.getBytes(StandardCharsets.UTF_8).length <= WIRE);
        return new Operation(id, hash, message, wire);
    }

    private ManagementServerMessage response(Operation request, long deadline) throws Exception {
        ManagementServerMessage value = JSON.decodeFromString(ManagementServerMessage.Companion.serializer(), link.receive(deadline));
        String nonce = switch (value) {
            case ManagementServerMessage.DatabaseContext item -> item.getNonce();
            case ManagementServerMessage.DatabaseHistory item -> item.getNonce();
            case ManagementServerMessage.DatabaseProductResult item -> item.getNonce();
            case ManagementServerMessage.MoneyHistorySaveResult item -> item.getNonce();
            default -> throw new IOException("未知の応答種別です");
        };
        require(request.id().equals(nonce));
        require(request.message() instanceof ManagementClientMessage.DatabaseContextRequest && value instanceof ManagementServerMessage.DatabaseContext ||
                (request.message() instanceof ManagementClientMessage.DatabaseHistoryStart || request.message() instanceof ManagementClientMessage.DatabaseHistoryPage || request.message() instanceof ManagementClientMessage.DatabaseHistoryEnd) && value instanceof ManagementServerMessage.DatabaseHistory ||
                request.message() instanceof ManagementClientMessage.DatabaseProductRequest && value instanceof ManagementServerMessage.DatabaseProductResult ||
                request.message() instanceof ManagementClientMessage.MoneyHistorySave && value instanceof ManagementServerMessage.MoneyHistorySaveResult);
        return value;
    }

    private JsonObject result(String status, String id, String hash, ManagementServerMessage response) {
        Map<String, JsonElement> value = new LinkedHashMap<>(packet(status, id, hash, null));
        value.put("response", JSON.parseToJsonElement(ManagementMessageCodec.INSTANCE.encode(response)));
        return new JsonObject(value);
    }

    private static JsonObject packet(String status, String id, String hash, String code) {
        Map<String, JsonElement> values = new LinkedHashMap<>();
        values.put("status", JsonElementKt.JsonPrimitive(status));
        if (id != null) values.put("operationId", JsonElementKt.JsonPrimitive(id));
        if (hash != null) values.put("requestHash", JsonElementKt.JsonPrimitive(hash));
        if (code != null) values.put("code", JsonElementKt.JsonPrimitive(code));
        return new JsonObject(values);
    }

    private JsonObject read() throws Exception {
        int size = input.readInt(); require(size > 0 && size <= FRAME - 4);
        byte[] body = input.readNBytes(size); require(body.length == size);
        for (byte value : body) require(value != 10 && value != 13);
        String text = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(body)).toString();
        JsonElement element = JSON.parseToJsonElement(text); require(element instanceof JsonObject);
        return (JsonObject) element;
    }

    private void write(JsonObject frame) throws IOException {
        byte[] bytes = frame.toString().getBytes(StandardCharsets.UTF_8);
        if (bytes.length + 4 > FRAME) throw new IOException("応答frame上限です");
        output.writeInt(bytes.length); output.write(bytes); output.flush();
    }

    private static String string(JsonObject value, String key) { JsonElement item = value.get(key); require(item instanceof JsonPrimitive && ((JsonPrimitive)item).isString()); return ((JsonPrimitive)item).getContent(); }
    private static int integer(JsonObject value, String key) { JsonElement item = value.get(key); require(item instanceof JsonPrimitive && !((JsonPrimitive)item).isString()); return Integer.parseInt(((JsonPrimitive)item).getContent()); }
    private static int timeout(JsonObject value) { int result = integer(value, "timeoutMillis"); require(result >= 1 && result <= 30_000); return result; }
    private static void fields(JsonObject value, String... keys) { require(value.keySet().equals(Set.of(keys))); }
    private static void nonce(String nonce, String id) { require(nonce == null || nonce.equals(id)); }
    private static void uuid(String value) { require(UUID.fromString(value).toString().equals(value)); }
    private static void require(boolean value) { if (!value) throw new IllegalArgumentException("専用契約に一致しません"); }
    private static long remaining(long deadline) throws TimeoutException { long value = deadline - System.nanoTime(); if (value <= 0) throw new TimeoutException(); return value; }

    private static final class Link implements WebSocket.Listener {
        private final HttpClient client;
        private WebSocket socket;
        private CompletableFuture<WebSocket> connecting;
        private boolean connectionFailed;
        private final CompletableFuture<Integer> peerClosed = new CompletableFuture<>();
        private CompletableFuture<String> pending;
        private final StringBuilder incoming = new StringBuilder();
        private int incomingBytes;
        private Throwable failure;
        private CompletableFuture<WebSocket> closingWrite;

        Link() {
            client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build();
        }
        void connect(int port, long deadline) throws Exception {
            require(connecting == null);
            connecting = client.newWebSocketBuilder().connectTimeout(Duration.ofNanos(remaining(deadline)))
                    .buildAsync(URI.create("ws://127.0.0.1:" + port + "/management"), this);
            socket = connecting.get(remaining(deadline), TimeUnit.NANOSECONDS);
        }
        synchronized void send(String text, long deadline) throws Exception {
            if (failure != null || peerClosed.isDone()) throw new IOException("接続が終了しています");
            require(pending == null || pending.isDone());
            pending = new CompletableFuture<>();
            long timeout = remaining(deadline);
            socket.sendText(text, true).get(timeout, TimeUnit.NANOSECONDS);
        }
        String receive(long deadline) throws Exception {
            CompletableFuture<String> value;
            synchronized (this) { if (failure != null) throw new IOException("応答の接続が不明です"); value = pending; }
            require(value != null);
            return value.get(remaining(deadline), TimeUnit.NANOSECONDS);
        }
        void beginClose(long deadline) throws Exception {
            if (socket == null) {
                if (connecting == null) { connectionFailed = true; return; }
                try { socket = connecting.get(remaining(deadline), TimeUnit.NANOSECONDS); }
                catch (ExecutionException rejected) { connectionFailed = true; return; }
            }
            if (failure != null) throw new IOException("接続の終端が不明です");
            if (closingWrite == null) closingWrite = socket.sendClose(WebSocket.NORMAL_CLOSURE, "");
        }
        void closed(long deadline) throws Exception {
            if (!connectionFailed) {
                closingWrite.get(remaining(deadline), TimeUnit.NANOSECONDS);
                require(peerClosed.get(remaining(deadline), TimeUnit.NANOSECONDS) == WebSocket.NORMAL_CLOSURE);
                synchronized(this) { if (failure != null) throw new IOException("接続の終端が不明です"); }
            }
            client.shutdown();
            if (!client.awaitTermination(Duration.ofNanos(remaining(deadline)))) throw new TimeoutException();
        }
        void shutdown() { client.shutdown(); }
        @Override public void onOpen(WebSocket value) { value.request(1); }
        @Override public CompletionStage<?> onText(WebSocket value, CharSequence data, boolean last) {
            synchronized(this) {
                incomingBytes += data.toString().getBytes(StandardCharsets.UTF_8).length;
                if (incomingBytes > WIRE || pending == null) fail(new IOException("応答上限又は未要求応答です"));
                else {
                    incoming.append(data);
                    if (last) {
                        if (!pending.complete(incoming.toString())) fail(new IOException("応答が重複しています"));
                        incoming.setLength(0); incomingBytes = 0;
                    }
                }
            }
            value.request(1); return CompletableFuture.completedFuture(null);
        }
        @Override public CompletionStage<?> onBinary(WebSocket value, ByteBuffer data, boolean last) { synchronized(this) { fail(new IOException("binary応答です")); } return CompletableFuture.completedFuture(null); }
        @Override public CompletionStage<?> onClose(WebSocket value, int code, String reason) {
            synchronized(this) {
                if (closingWrite == null) closingWrite = value.sendClose(code, "");
                if (pending != null && !pending.isDone()) pending.completeExceptionally(new EOFException());
            }
            peerClosed.complete(code);
            return CompletableFuture.completedFuture(null);
        }
        @Override public void onError(WebSocket value, Throwable error) { synchronized(this) { fail(error); } }
        private void fail(Throwable error) { failure = error; if (pending != null) pending.completeExceptionally(error); }
    }
}
