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
                "\"expectedStateSha256\":\""+"b".repeat(64)+"\",\"expectedRevision\":1}");
    }
    private static void reset() {
        WorldRunLifecycle.opens=0; WorldRunLifecycle.closes=0;
        WorldRunLifecycle.failOpen=false; WorldRunLifecycle.failClose=false; WorldRunLifecycle.failCloseAfterPublish=false;
        WorldRunLifecycle.current=WorldRunLifecycle.initialReady();
        WorldProcessIdentities.parentAlive=true;
        WorldProcessIdentities.selfMatches=true;
    }
    private static List<JsonObject> run(boolean fail, JsonObject... frames) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        DataOutputStream in = new DataOutputStream(bytes);
        for (JsonObject frame : frames) {
            byte[] body = frame.toString().getBytes(StandardCharsets.UTF_8);
            in.writeInt(body.length); in.write(body);
        }
        return runRaw(fail, bytes.toByteArray());
    }
    private static List<JsonObject> runRaw(boolean fail, byte[] bytes) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        boolean thrown=false;
        try { new OfflineControllerMain(new ByteArrayInputStream(bytes),out).run(); }
        catch(IOException expected) { thrown=true; }
        if(thrown!=fail) throw new AssertionError("exit契約不一致");
        List<JsonObject> results=new ArrayList<>();
        DataInputStream response=new DataInputStream(new ByteArrayInputStream(out.toByteArray()));
        while(response.available()>0)
            results.add(OfflineControllerMain.read(response));
        return results;
    }
    private static void check(boolean value) { if(!value) throw new AssertionError(); count++; }
    private static List<String> statuses(List<JsonObject> packets) {
        return packets.stream().map(packet -> ((JsonPrimitive)packet.get("status")).getContent()).toList();
    }
    private static void response(JsonObject packet, String status, String operation, String requestHash,
                                 String stateHash, long revision, String runUuid) {
        check(packet.get("status").equals(JsonElementKt.JsonPrimitive(status)));
        check(packet.get("sessionId").equals(JsonElementKt.JsonPrimitive("sessionid")));
        check(packet.get("nonce").equals(JsonElementKt.JsonPrimitive("nonce")));
        check(packet.get("operationId").equals(JsonElementKt.JsonPrimitive(operation)));
        check(packet.get("requestHash").equals(JsonElementKt.JsonPrimitive(requestHash)));
        check(packet.get("stateSha256").equals(JsonElementKt.JsonPrimitive(stateHash)));
        check(packet.get("stateRevision").equals(JsonElementKt.JsonPrimitive(revision)));
        check(packet.get("runUuid").equals(JsonElementKt.JsonPrimitive(runUuid)));
    }
    public static void main(String[] args) throws Exception {
        reset();
        List<JsonObject> normal=run(false,open(),close());
        check(statuses(normal).equals(List.of("OPEN","OWNER_CLOSED")));
        response(normal.get(0),"OPEN","operationid","a".repeat(64),"b".repeat(64),1,WorldRunLifecycle.RUN);
        response(normal.get(1),"OWNER_CLOSED","close","b".repeat(64),"c".repeat(64),2,WorldRunLifecycle.RUN);
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==1);
        reset();
        check(statuses(run(true,open())).equals(List.of("OPEN")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        reset(); WorldRunLifecycle.failClose=true;
        check(statuses(run(true,open(),close())).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.closes==1);
        reset(); WorldRunLifecycle.failOpen=true;
        check(statuses(run(true,open())).equals(List.of("UNKNOWN")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        reset();
        Map<String,JsonElement> stale=new LinkedHashMap<>(close());
        stale.put("nonce",JsonElementKt.JsonPrimitive("old"));
        check(statuses(run(true,open(),new JsonObject(stale))).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.closes==0);
        reset();
        check(statuses(run(true,open(),open())).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        reset(); WorldProcessIdentities.parentAlive=false;
        check(statuses(run(false,open())).equals(List.of("REJECTED")));
        check(WorldRunLifecycle.opens==0);
        reset(); WorldProcessIdentities.selfMatches=false;
        check(statuses(run(false,open())).equals(List.of("REJECTED")));
        check(WorldRunLifecycle.opens==0);
        reset();
        Map<String,JsonElement> wrongParent=new LinkedHashMap<>(open());
        JsonObject parent=(JsonObject)wrongParent.get("controller");
        Map<String,JsonElement> parentFields=new LinkedHashMap<>(parent);
        parentFields.put("pid",JsonElementKt.JsonPrimitive(ProcessHandle.current().parent().orElseThrow().pid()+1));
        wrongParent.put("controller",new JsonObject(parentFields));
        check(statuses(run(false,new JsonObject(wrongParent))).equals(List.of("REJECTED")));
        check(WorldRunLifecycle.opens==0);
        reset();
        Map<String,JsonElement> staleReady=new LinkedHashMap<>(open());
        staleReady.put("expectedRevision",JsonElementKt.JsonPrimitive(1));
        check(statuses(run(false,new JsonObject(staleReady))).equals(List.of("REJECTED")));
        check(WorldRunLifecycle.opens==0);
        reset();
        check(statuses(runRaw(false,new byte[]{0,0,0,1,35})).equals(List.of("REJECTED")));
        check(WorldRunLifecycle.opens==0);
        reset();
        ByteArrayOutputStream invalid = new ByteArrayOutputStream();
        DataOutputStream stream = new DataOutputStream(invalid);
        byte[] first=open().toString().getBytes(StandardCharsets.UTF_8);
        stream.writeInt(first.length); stream.write(first); stream.writeInt(1); stream.writeByte(35);
        check(statuses(runRaw(true,invalid.toByteArray())).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        reset();
        boolean lost=false;
        try {
            new OfflineControllerMain(new ByteArrayInputStream(invalid.toByteArray()),new OutputStream(){
                public void write(int value) throws IOException { throw new IOException("fixture response loss"); }
            }).run();
        } catch(IOException expected) { lost=true; }
        check(lost && WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);

        reset(); WorldRunLifecycle.failCloseAfterPublish=true;
        List<JsonObject> closeUnknown=run(true,open(),close());
        check(statuses(closeUnknown).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==1);
        check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.Ready);

        reset();
        ByteArrayOutputStream onlyOpen=new ByteArrayOutputStream();
        boolean closeResponseLost=false;
        try {
            new OfflineControllerMain(new ByteArrayInputStream(bytes(open(),close())), new OutputStream() {
                private int header;
                private int headerBytes;
                private int bodyBytes=-1;
                public void write(int value) throws IOException {
                    if(bodyBytes==0) throw new IOException("fixture OWNER_CLOSED応答喪失");
                    onlyOpen.write(value);
                    if(bodyBytes<0) {
                        header=(header<<8)|(value&255);
                        if(++headerBytes==4) bodyBytes=header;
                    } else bodyBytes--;
                }
                public void write(byte[] value,int offset,int length) throws IOException {
                    for(int i=offset;i<offset+length;i++) write(value[i]&255);
                }
            }).run();
        } catch(IOException expected) { closeResponseLost=true; }
        check(closeResponseLost);
        check(WorldRunLifecycle.opens==1);
        check(WorldRunLifecycle.closes==1);
        DataInputStream openResponse=new DataInputStream(new ByteArrayInputStream(onlyOpen.toByteArray()));
        JsonObject openPacket=OfflineControllerMain.read(openResponse);
        response(openPacket,"OPEN","operationid","a".repeat(64),"b".repeat(64),1,WorldRunLifecycle.RUN);
        check(openResponse.available()==0);
        System.out.println("offline-lifecycle-vectors="+count);
    }

    private static byte[] bytes(JsonObject... frames) throws IOException {
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();
        DataOutputStream output=new DataOutputStream(bytes);
        for(JsonObject frame:frames) {
            byte[] body=frame.toString().getBytes(StandardCharsets.UTF_8);
            output.writeInt(body.length); output.write(body);
        }
        return bytes.toByteArray();
    }
}
