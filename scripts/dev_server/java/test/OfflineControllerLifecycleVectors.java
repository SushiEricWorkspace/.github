package io.github.sushiericworkspace.devserver;

import io.github.sushiericworkspace.sushiericdatamodel.database.run.*;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterCodec;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterRequest;
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
    private static JsonObject assign() {
        Map<String,JsonElement> frame = new LinkedHashMap<>(close());
        frame.put("command",JsonElementKt.JsonPrimitive("assign"));
        frame.put("operationId",JsonElementKt.JsonPrimitive(WorldRunLifecycle.ID));
        frame.put("runUuid",JsonElementKt.JsonPrimitive(WorldRunLifecycle.RUN));
        frame.put("worker",identity(WorldProcessIdentities.WORKER));
        frame.put("request",Json.Default.parseToJsonElement("{\"requestId\":\""+WorldRunLifecycle.ID+
                "\",\"worldUuid\":\""+WorldRunLifecycle.ID+"\",\"action\":{\"type\":\""+
                "io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterAction.Edit\",\"products\":[]}}"));
        return new JsonObject(frame);
    }

    private static JsonObject changed(JsonObject value, String field, JsonElement replacement) {
        Map<String,JsonElement> result = new LinkedHashMap<>(value);
        result.put(field,replacement);
        return new JsonObject(result);
    }

    private static void reset() {
        WorldRunLifecycle.opens=0; WorldRunLifecycle.closes=0; WorldRunLifecycle.assigns=0;
        WorldRunLifecycle.failAssignAfterPublish=false;
        WorldOfflineWriterSession.lastRequest=null;
        WorldProcessIdentities.workerAlive=true;
        WorldProcessIdentities.failAfterOpen=false;
        WorldProcessIdentities.parentChecks=0;
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
        assignmentVectors();
        System.out.println("offline-lifecycle-vectors="+count);
    }

    private static void assignmentVectors() throws Exception {
        reset();
        List<JsonObject> assigned=run(true,open(),assign());
        check(statuses(assigned).equals(List.of("OPEN","ASSIGNED")));
        response(assigned.get(1),"ASSIGNED",WorldRunLifecycle.ID,"b".repeat(64),"d".repeat(64),2,WorldRunLifecycle.RUN);
        check(assigned.get(1).get("editRequestSha256").equals(JsonElementKt.JsonPrimitive("e".repeat(64))));
        WorldRunPhase.OfflineAssigned phase=(WorldRunPhase.OfflineAssigned)WorldRunLifecycle.current.getState().getPayload();
        check(phase.getWriter().getSessionId().equals(WorldRunLifecycle.RUN));
        check(phase.getWorker().equals(WorldProcessIdentities.identity(WorldProcessIdentities.WORKER)));
        check(phase.getOperationId().equals(WorldRunLifecycle.ID));
        check(WorldRunLifecycle.assigns==1 && WorldRunLifecycle.closes==0 && WorldRunLifecycle.opens==1);
        reset();
        JsonObject nonEmpty=assign();
        JsonObject requestBody=(JsonObject)nonEmpty.get("request");
        JsonObject product=(JsonObject)Json.Default.parseToJsonElement("{\"type\":\""+
                "io.github.sushiericworkspace.sushiericdatamodel.database.host.HostProductChange.Save\","+
                "\"id\":\"fixture/product\",\"expectedHash\":\""+"a".repeat(64)+"\","+
                "\"definition\":{\"itemInternalId\":\"minecraft:stone\",\"purchasePrice\":123,"+
                "\"salePrice\":45,\"increaseStockOnSale\":true,\"displayOrder\":2}}");
        JsonObject action=changed((JsonObject)requestBody.get("action"),"products",new JsonArray(List.of(product)));
        nonEmpty=changed(nonEmpty,"request",changed(requestBody,"action",action));
        HostAdapterRequest original=HostAdapterCodec.INSTANCE.decodeRequest(
                nonEmpty.get("request").toString().getBytes(StandardCharsets.UTF_8));
        List<JsonObject> forwarded=run(true,open(),nonEmpty);
        check(statuses(forwarded).equals(List.of("OPEN","ASSIGNED")));
        check(original.equals(WorldOfflineWriterSession.lastRequest));
        response(forwarded.get(1),"ASSIGNED",WorldRunLifecycle.ID,"b".repeat(64),"d".repeat(64),2,WorldRunLifecycle.RUN);
        check(WorldRunLifecycle.assigns==1 && WorldRunLifecycle.closes==0 && WorldRunLifecycle.opens==1);
        for (String field:List.of("sessionId","nonce","openOperationId","expectedStateSha256","runUuid")) {
            reset();
            check(statuses(run(true,open(),changed(assign(),field,JsonElementKt.JsonPrimitive("old"))))
                    .equals(List.of("OPEN","UNKNOWN")));
            check(WorldRunLifecycle.assigns==0 && WorldRunLifecycle.closes==0);
        }
        reset();
        check(statuses(run(false,assign())).equals(List.of("REJECTED")));
        check(WorldRunLifecycle.assigns==0 && WorldRunLifecycle.opens==0);
        reset();
        check(statuses(run(true,open(),changed(assign(),"expectedRevision",JsonElementKt.JsonPrimitive(0))))
                .equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.assigns==0);
        for (String field:List.of("hostHash","bootId","startId","pid")) {
            reset();
            JsonObject worker=(JsonObject)assign().get("worker");
            JsonElement other=field.equals("pid")?JsonElementKt.JsonPrimitive(ProcessHandle.current().pid()):
                    JsonElementKt.JsonPrimitive(field.equals("hostHash")?"c".repeat(64):"old");
            check(statuses(run(true,open(),changed(assign(),"worker",changed(worker,field,other))))
                    .equals(List.of("OPEN","UNKNOWN")));
            check(WorldRunLifecycle.assigns==0);
        }
        reset();
        check(statuses(run(true,open(),changed(assign(),"worker",identity(ProcessHandle.current().parent().orElseThrow().pid()))))
                .equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.assigns==0);
        reset(); WorldProcessIdentities.workerAlive=false;
        check(statuses(run(true,open(),assign())).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.assigns==0);
        reset();
        check(statuses(run(true,open(),assign(),assign())).equals(List.of("OPEN","ASSIGNED","UNKNOWN")));
        check(WorldRunLifecycle.assigns==1 && WorldRunLifecycle.closes==0);
        reset();
        JsonObject assignedClose=changed(close(),"expectedStateSha256",JsonElementKt.JsonPrimitive("d".repeat(64)));
        assignedClose=changed(assignedClose,"expectedRevision",JsonElementKt.JsonPrimitive(2));
        check(statuses(run(true,open(),assign(),assignedClose)).equals(List.of("OPEN","ASSIGNED","UNKNOWN")));
        check(WorldRunLifecycle.assigns==1 && WorldRunLifecycle.closes==0);
        for(String field:assign().keySet()) {
            reset();
            Map<String,JsonElement> missing=new LinkedHashMap<>(assign());
            missing.remove(field);
            check(statuses(run(true,open(),new JsonObject(missing))).equals(List.of("OPEN","UNKNOWN")));
            check(WorldRunLifecycle.assigns==0);
        }
        reset();
        check(statuses(run(true,open(),changed(assign(),"sql",JsonElementKt.JsonPrimitive("delete"))))
                .equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.assigns==0);
        for(String field:List.of("requestId","worldUuid","deadlineEpochMillis","action","extra")) {
            reset();
            JsonObject request=(JsonObject)assign().get("request");
            JsonElement other=switch(field) {
                case "deadlineEpochMillis" -> JsonElementKt.JsonPrimitive(1);
                case "action" -> Json.Default.parseToJsonElement("{\"type\":\""+
                        "io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterAction.Products\",\"ids\":[]}");
                default -> JsonElementKt.JsonPrimitive("00000000-0000-4000-8000-000000000003");
            };
            check(statuses(run(true,open(),changed(assign(),"request",changed(request,field,other))))
                    .equals(List.of("OPEN","UNKNOWN")));
            check(WorldRunLifecycle.assigns==0);
        }
        reset(); WorldProcessIdentities.failAfterOpen=true;
        check(statuses(run(true,open(),assign())).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.assigns==0 && WorldRunLifecycle.opens==1);
        reset(); WorldRunLifecycle.failAssignAfterPublish=true;
        check(statuses(run(true,open(),assign(),assign())).equals(List.of("OPEN","UNKNOWN")));
        check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.OfflineAssigned);
        check(WorldRunLifecycle.assigns==1 && WorldRunLifecycle.closes==0 && WorldRunLifecycle.opens==1);
        reset();
        boolean lost=false;
        boolean[] interrupted={false};
        ByteArrayOutputStream onlyOpen=new ByteArrayOutputStream();
        try {
            new OfflineControllerMain(new ByteArrayInputStream(bytes(open(),assign())),new OutputStream() {
                private int header;
                private int headerBytes;
                private int bodyBytes=-1;
                public void write(int value) throws IOException {
                    if(bodyBytes==0) {
                        interrupted[0]=true;
                        throw new IOException("fixture ASSIGNED応答喪失");
                    }
                    onlyOpen.write(value);
                    if(bodyBytes<0) {
                        header=(header<<8)|(value&255);
                        if(++headerBytes==4) bodyBytes=header;
                    } else bodyBytes--;
                }
            }).run();
        } catch(IOException expected) { lost=true; }
        check(lost && interrupted[0] && WorldRunLifecycle.assigns==1 && WorldRunLifecycle.opens==1 && WorldRunLifecycle.closes==0);
        check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.OfflineAssigned);
        DataInputStream response=new DataInputStream(new ByteArrayInputStream(onlyOpen.toByteArray()));
        check(OfflineControllerMain.read(response).get("status").equals(JsonElementKt.JsonPrimitive("OPEN")));
        check(response.available()==0);
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
