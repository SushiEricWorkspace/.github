import io.github.sushiericworkspace.sushiericdatamodel.database.host.*;
import io.github.sushiericworkspace.sushiericservermod.management.*;
import io.github.sushiericworkspace.sushiericservermod.management.database.ManagementDatabaseBinding;
import kotlinx.serialization.json.*;
import java.lang.reflect.*;
import java.net.http.HttpClient;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.CompletableFuture;

/** 固定Mod DTOとproxy本体の元接続/nonce/種別/分類を共有fixtureで検証する。 */
public final class DatabaseProxyVectors {
    private static final Json JSON = Json.Default;
    private static final Class<?> PROXY;
    static {
        try { PROXY = Class.forName("io.github.sushiericworkspace.devserver.DatabaseProxyMain"); }
        catch (Exception error) { throw new ExceptionInInitializerError(error); }
    }

    private static Object construct(Class<?> type, Class<?>[] types, Object... arguments) throws Exception {
        Constructor<?> constructor = type.getDeclaredConstructor(types);
        constructor.setAccessible(true);
        return constructor.newInstance(arguments);
    }

    private static void set(Object target, String name, Object value) throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        field.set(target, value);
    }

    private static Object get(Object target, String name) throws Exception {
        Field field = target.getClass().getDeclaredField(name);
        field.setAccessible(true);
        return field.get(target);
    }

    private static Object call(Object target, String name, Class<?>[] types, Object... args) throws Exception {
        Method method = target.getClass().getDeclaredMethod(name, types);
        method.setAccessible(true);
        return method.invoke(target, args);
    }

    private static String text(JsonObject value, String key) { return ((JsonPrimitive)value.get(key)).getContent(); }

    public static void main(String[] args) throws Exception {
        JsonArray cases = (JsonArray)((JsonObject)JSON.parseToJsonElement(Files.readString(Path.of(args[0])))).get("cases");
        int count = 0;
        for (JsonElement element : cases) {
            JsonObject vector = (JsonObject)element;
            JsonObject response = (JsonObject)vector.get("response");
            String id = text(vector, "expectedNonce");
            String hash = "a".repeat(64);
            String expected = text(vector, "expected");
            String handle = "22222222-2222-4222-8222-222222222222";
            ManagementClientMessage message = switch (text(response, "type")) {
                case "database_context" -> new ManagementClientMessage.DatabaseContextRequest(id);
                case "database_history" -> new ManagementClientMessage.DatabaseHistoryEnd(handle, id);
                case "money_history_save_result" -> new ManagementClientMessage.MoneyHistorySave(null, id);
                case "database_product_result" -> new ManagementClientMessage.DatabaseProductRequest(
                    new ManagementDatabaseBinding("33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444", "slot", "generation", null),
                    new HostAdapterRequest(1, text((JsonObject)response.get("response"), "requestId"),
                        "33333333-3333-4333-8333-333333333333", new HostAdapterAction.Products(List.of()), null), id);
                default -> throw new AssertionError("未知vector");
            };
            Object proxy = construct(PROXY, new Class<?>[]{});
            Class<?> linkType = Class.forName(PROXY.getName() + "$Link");
            Object link = construct(linkType, new Class<?>[]{});
            Class<?> operationType = Class.forName(PROXY.getName() + "$Operation");
            Object operation = construct(operationType, new Class<?>[]{String.class, String.class, ManagementClientMessage.class, String.class}, id, hash, message, "held-original-request");
            set(proxy, "link", link);
            set(proxy, "operation", operation);
            set(proxy, "handle", handle);
            set(link, "pending", CompletableFuture.completedFuture(response.toString()));
            try {
                String actual;
                try {
                    JsonObject result = (JsonObject)call(proxy, "finish", new Class<?>[]{long.class}, System.nanoTime() + 1_000_000_000L);
                    actual = text(result, "status");
                    if (!"INVALID".equals(expected) && !expected.equals(actual)) throw new AssertionError(text(vector, "name") + ": " + actual);
                    if ("INVALID".equals(expected)) throw new AssertionError("不正応答を受理しました");
                    JsonObject resolve = (JsonObject)JSON.parseToJsonElement("{\"command\":\"resolve\",\"operationId\":\"" + id + "\",\"requestHash\":\"" + hash + "\",\"timeoutMillis\":1000}");
                    JsonObject saved = (JsonObject)call(proxy, "resolve", new Class<?>[]{JsonObject.class, String.class, String.class, long.class}, resolve, id, hash, System.nanoTime() + 1_000_000_000L);
                    if (!actual.equals(text(saved, "status")) || get(proxy, "operation") != operation) throw new AssertionError("原要求の分類が変わりました");
                    if (count == 0) {
                        Object wrong = construct(operationType, new Class<?>[]{String.class, String.class, ManagementClientMessage.class, String.class},
                            id, hash, new ManagementClientMessage.MoneyHistorySave(null, id), "different-kind");
                        set(proxy, "operation", wrong);
                        set(proxy, "known", null);
                        set(link, "pending", CompletableFuture.completedFuture(response.toString()));
                        try {
                            call(proxy, "finish", new Class<?>[]{long.class}, System.nanoTime() + 1_000_000_000L);
                            throw new AssertionError("元nonceが同じでも別種別の応答を受理しました");
                        } catch (InvocationTargetException invalidKind) {
                            if (get(proxy, "known") != null || get(proxy, "operation") != wrong) throw new AssertionError("別種別を確定記録しました");
                        }
                    }
                } catch (InvocationTargetException rejected) {
                    actual = "INVALID".equals(expected) ? "INVALID" : "UNKNOWN";
                }
                if (!expected.equals(actual)) throw new AssertionError(text(vector, "name") + ": " + actual);
                count++;
            } finally {
                ((HttpClient)get(link, "client")).close();
            }
        }
        System.out.println("shared-vectors=" + count);
    }
}
