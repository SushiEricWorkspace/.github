package io.github.sushiericworkspace.devserver;

import io.github.sushiericworkspace.sushiericdatamodel.database.run.*;
import kotlinx.serialization.json.*;
import java.io.*;
import java.nio.file.Path;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** Data/本人APIをfakeへ置き換え、実root・DB・native本人照合を行わない。 */
public final class OfflineControllerLifecycleVectors {
    private static int count;
    private static JsonObject open() {
        String id = WorldRunLifecycle.ID;
        String root = Path.of(".").toAbsolutePath().toString();
        Map<String, JsonElement> v = new LinkedHashMap<>();
        for (String key : List.of("command","operationId","requestHash","sessionId","nonce","leaseId",
                "generation","drainSha256","worldRoot","shopDirectory","worldUuid","runUuid","expectedStateSha256"))
            v.put(key, JsonElementKt.JsonPrimitive(switch(key) {
                case "command" -> "open";
                case "requestHash","drainSha256","expectedStateSha256" -> "a".repeat(64);
                case "leaseId","generation","worldUuid","runUuid" -> id;
                case "worldRoot","shopDirectory" -> root;
                default -> key.toLowerCase(Locale.ROOT);
            }));
        v.put("epoch", JsonElementKt.JsonPrimitive(1));
        v.put("expectedRevision", JsonElementKt.JsonPrimitive(0));
        v.put("owner", identity(ProcessHandle.current().pid()));
        v.put("controller", identity(ProcessHandle.current().parent().orElseThrow().pid()));
        return new JsonObject(v);
    }
    private static JsonObject identity(long pid) {
        return new JsonObject(Map.of("hostHash",JsonElementKt.JsonPrimitive("a".repeat(64)),
                "bootId",JsonElementKt.JsonPrimitive("fixture-boot"),"startId",JsonElementKt.JsonPrimitive("fixture-start"),
                "pid",JsonElementKt.JsonPrimitive(pid)));
    }
    private static JsonObject close() {
        return (JsonObject)Json.Default.parseToJsonElement(
                "{\"command\":\"close\",\"operationId\":\"close\",\"requestHash\":\""+"b".repeat(64)+
                "\",\"sessionId\":\"sessionid\",\"nonce\":\"nonce\",\"openOperationId\":\"operationid\","+
                "\"expectedStateSha256\":\""+"a".repeat(64)+"\",\"expectedRevision\":1}");
    }
    private static void reset() {
        WorldRunLifecycle.opens=0; WorldRunLifecycle.closes=0;
        WorldRunLifecycle.failOpen=false; WorldRunLifecycle.failClose=false;
        WorldProcessIdentities.parentAlive=true;
    }
    private static List<String> run(boolean fail, JsonObject... frames) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        DataOutputStream in = new DataOutputStream(bytes);
        for (JsonObject frame : frames) {
            byte[] body = frame.toString().getBytes(StandardCharsets.UTF_8);
            in.writeInt(body.length); in.write(body);
        }
        return runRaw(fail, bytes.toByteArray());
    }
    private static List<String> runRaw(boolean fail, byte[] bytes) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        boolean thrown=false;
        try { new OfflineControllerMain(new ByteArrayInputStream(bytes),out).run(); }
        catch(IOException expected) { thrown=true; }
        if(thrown!=fail) throw new AssertionError("exit契約不一致");
        List<String> results=new ArrayList<>();
        DataInputStream response=new DataInputStream(new ByteArrayInputStream(out.toByteArray()));
        while(response.available()>0)
            results.add(((JsonPrimitive)OfflineControllerMain.read(response).get("status")).getContent());
        return results;
    }
    private static void check(boolean value) { if(!value) throw new AssertionError(); count++; }
    public static void main(String[] args) throws Exception {
        reset();
        check(run(false,open(),close()).equals(List.of("OPEN","OWNER_CLOSED")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==1);
        reset();
        check(run(true,open()).equals(List.of("OPEN")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        reset(); WorldRunLifecycle.failClose=true;
        check(run(true,open(),close()).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.closes==1);
        reset(); WorldRunLifecycle.failOpen=true;
        check(run(true,open()).equals(List.of("UNKNOWN")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        reset();
        Map<String,JsonElement> stale=new LinkedHashMap<>(close());
        stale.put("nonce",JsonElementKt.JsonPrimitive("old"));
        check(run(true,open(),new JsonObject(stale)).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.closes==0);
        reset();
        check(run(true,open(),open()).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        reset(); WorldProcessIdentities.parentAlive=false;
        check(run(false,open()).equals(List.of("REJECTED")));
        check(WorldRunLifecycle.opens==0);
        reset();
        check(runRaw(false,new byte[]{0,0,0,1,35}).equals(List.of("REJECTED")));
        check(WorldRunLifecycle.opens==0);
        reset();
        ByteArrayOutputStream invalid = new ByteArrayOutputStream();
        DataOutputStream stream = new DataOutputStream(invalid);
        byte[] first=open().toString().getBytes(StandardCharsets.UTF_8);
        stream.writeInt(first.length); stream.write(first); stream.writeInt(1); stream.writeByte(35);
        check(runRaw(true,invalid.toByteArray()).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        reset();
        boolean lost=false;
        try {
            new OfflineControllerMain(new ByteArrayInputStream(invalid.toByteArray()),new OutputStream(){
                public void write(int value) throws IOException { throw new IOException("fixture response loss"); }
            }).run();
        } catch(IOException expected) { lost=true; }
        check(lost && WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        System.out.println("offline-lifecycle-vectors="+count);
    }
}
