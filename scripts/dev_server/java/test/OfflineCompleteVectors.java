package io.github.sushiericworkspace.devserver;

import io.github.sushiericworkspace.sushiericdatamodel.database.run.*;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.*;
import io.github.sushiericworkspace.sushiericdatamodel.database.SushiEricDatabase;
import kotlinx.serialization.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.*;

/** C本人completeの境界だけを合成し、保存/hash/recover算法は実装しない。 */
public final class OfflineCompleteVectors {
    private static final String OP = "00000000-0000-4000-8000-000000000003";
    private static final String OTHER = "00000000-0000-4000-8000-000000000004";
    private static final String HASH = "2".repeat(64);
    private static int count;
    private static void check(boolean value) { if (!value) throw new AssertionError(); count++; }
    private static JsonElement text(String value) { return JsonElementKt.JsonPrimitive(value); }
    private static JsonObject changed(JsonObject value, String key, JsonElement replacement) {
        Map<String,JsonElement> result = new LinkedHashMap<>(value); result.put(key,replacement); return new JsonObject(result);
    }
    private static JsonObject identity(long pid) {
        return new JsonObject(Map.of("hostHash",text("a".repeat(64)), "bootId",text("fixture-boot"),
                "pid",JsonElementKt.JsonPrimitive(pid), "startId",text("fixture-start")));
    }
    private static JsonObject open() {
        String root = Path.of(".").toAbsolutePath().toString();
        Map<String,JsonElement> frame = new LinkedHashMap<>();
        for (String key:List.of("command","operationId","requestHash","sessionId","nonce","leaseId","generation",
                "drainSha256","worldRoot","shopDirectory","worldUuid","runUuid","expectedStateSha256"))
            frame.put(key,text(switch(key) {
                case "command", "operationId" -> "open";
                case "requestHash", "drainSha256", "expectedStateSha256" -> "a".repeat(64);
                case "leaseId", "generation", "worldUuid", "runUuid" -> WorldRunLifecycle.ID;
                case "worldRoot", "shopDirectory" -> root;
                default -> key.toLowerCase(Locale.ROOT);
            }));
        frame.put("epoch",JsonElementKt.JsonPrimitive(1)); frame.put("expectedRevision",JsonElementKt.JsonPrimitive(0));
        frame.put("controller",identity(ProcessHandle.current().parent().orElseThrow().pid()));
        frame.put("owner",identity(ProcessHandle.current().pid()));
        return new JsonObject(frame);
    }
    private static JsonObject request() {
        return (JsonObject)Json.Default.parseToJsonElement("{\"requestId\":\""+OP+"\",\"worldUuid\":\""+WorldRunLifecycle.ID+
                "\",\"action\":{\"type\":\"io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterAction.Edit\","+
                "\"products\":[{\"type\":\"io.github.sushiericworkspace.sushiericdatamodel.database.host.HostProductChange.Save\","+
                "\"id\":\"tab.apple\",\"expectedHash\":null,\"definition\":{\"itemInternalId\":\"minecraft:stone\",\"purchasePrice\":123},"+
                "\"stock\":{\"type\":\"io.github.sushiericworkspace.sushiericdatamodel.database.host.HostStockEdit.Register\",\"quantity\":7,\"revision\":null}}]}}");
    }
    private static JsonObject assign() {
        return new JsonObject(Map.ofEntries(Map.entry("command",text("assign")), Map.entry("operationId",text(OP)),
                Map.entry("requestHash",text(HASH)), Map.entry("sessionId",text("sessionid")), Map.entry("nonce",text("nonce")),
                Map.entry("openOperationId",text("open")), Map.entry("expectedStateSha256",text("b".repeat(64))),
                Map.entry("expectedRevision",JsonElementKt.JsonPrimitive(1)), Map.entry("runUuid",text(WorldRunLifecycle.RUN)),
                Map.entry("worker",identity(WorldProcessIdentities.WORKER)), Map.entry("request",request())));
    }
    private static JsonObject complete() {
        return new JsonObject(Map.ofEntries(Map.entry("command",text("complete")), Map.entry("operationId",text(OP)),
                Map.entry("requestHash",text(HASH)), Map.entry("sessionId",text("sessionid")), Map.entry("nonce",text("nonce")),
                Map.entry("openOperationId",text("open")), Map.entry("expectedStateSha256",text("c".repeat(64))),
                Map.entry("expectedRevision",JsonElementKt.JsonPrimitive(4)), Map.entry("worldUuid",text(WorldRunLifecycle.ID)),
                Map.entry("runUuid",text(OP)), Map.entry("receiptSha256",text("1".repeat(64))), Map.entry("editRequestSha256",text("e".repeat(64)))));
    }
    private static JsonObject close() {
        return new JsonObject(Map.of("command",text("close"),"operationId",text("close"),"requestHash",text("3".repeat(64)),
                "sessionId",text("sessionid"),"nonce",text("nonce"),"openOperationId",text("open"),
                "expectedStateSha256",text("8".repeat(64)),"expectedRevision",JsonElementKt.JsonPrimitive(5)));
    }
    private static void reset() {
        WorldProcessIdentities.parentAlive = WorldProcessIdentities.controllerAlive = WorldProcessIdentities.selfMatches = true;
        WorldProcessIdentities.workerAlive = true; WorldProcessIdentities.actorPid = ProcessHandle.current().pid();
        WorldProcessIdentities.exited.clear(); WorldProcessIdentities.parentChecks = 0; WorldProcessIdentities.failAfterOpen = false;
        WorldRunLifecycle.opens = WorldRunLifecycle.assigns = WorldRunLifecycle.closes = 0;
        WorldRunLifecycle.completes = WorldRunLifecycle.completeCalls = 0;
        WorldRunLifecycle.begins = WorldRunLifecycle.dbOpens = WorldRunLifecycle.edits = WorldRunLifecycle.dbCloses = WorldRunLifecycle.finishes = 0;
        WorldRunLifecycle.dbOpen = WorldRunLifecycle.failOpen = WorldRunLifecycle.failClose = WorldRunLifecycle.failCloseAfterPublish = false;
        WorldRunLifecycle.failAssignAfterPublish = WorldRunLifecycle.failCompleteAfterPublish = false;
        WorldRunLifecycle.failBeginAfterPublish = WorldRunLifecycle.unknownEdit = WorldRunLifecycle.rejectedEdit = false;
        WorldRunLifecycle.failDbClose = WorldRunLifecycle.failFinishAfterPublish = false;
        WorldRunLifecycle.killParentAfterEdit = WorldRunLifecycle.killControllerAfterEdit = false;
        WorldRunLifecycle.receiptMatches = WorldRunLifecycle.sealMatches = true;
        WorldRunLifecycle.binding = null; WorldRunLifecycle.lastCandidate = null; WorldRunLifecycle.completedResponse = null;
        WorldRunLifecycle.receiptPending = null;
        WorldRunLifecycle.current = WorldRunLifecycle.initialReady(); WorldOfflineWriterSession.lastRequest = null;
        WorldRunLifecycle.expectedRequest = HostAdapterCodec.INSTANCE.decodeRequest(request().toString().getBytes(StandardCharsets.UTF_8));
    }
    private static void publish() {
        check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.OfflineAssigned);
        check(WorldOfflineWriterSession.lastSession.getStatus() == WorldRunLifecycle.current);
        check(WorldRunLifecycle.expectedRequest.equals(WorldOfflineWriterSession.lastRequest));
        try {
            WorldProcessIdentities.actorPid = WorldProcessIdentities.WORKER;
            WorldOfflineOperation operation = new WorldRunLifecycle(Path.of("."),Path.of("."),UUID.fromString(WorldRunLifecycle.ID),5000)
                    .beginOfflineEdit(WorldRunLifecycle.current,WorldRunLifecycle.expectedRequest);
            HostAdapterResponse response;
            try (SushiEricDatabase database = operation.openDatabase(false,5000)) {
                response = database.shopDefinitions(Path.of("fixture-shop")).execute(WorldRunLifecycle.expectedRequest,()->false);
            }
            operation.finish(response);
        } finally { WorldProcessIdentities.actorPid = ProcessHandle.current().pid(); }
        WorldProcessIdentities.workerAlive = false;
        check(WorldRunLifecycle.current.getState().getStateRevision() == 4);
        check(WorldRunLifecycle.current.getState().getRunUuid().equals(OP));
        check(WorldOfflineWriterSession.lastSession.getStatus().getState().getStateRevision() == 2);
        check(WorldOfflineWriterSession.lastSession.getStatus().getState().getRunUuid().equals(WorldRunLifecycle.RUN));
        check(WorldRunLifecycle.expectedRequest.equals(WorldRunLifecycle.lastBeginRequest));
        check(WorldRunLifecycle.expectedRequest.equals(WorldRunLifecycle.lastEditRequest));
    }
    private static byte[] bytes(JsonObject... frames) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream(); DataOutputStream wire = new DataOutputStream(output);
        for (JsonObject frame:frames) { byte[] body=frame.toString().getBytes(StandardCharsets.UTF_8); wire.writeInt(body.length); wire.write(body); }
        return output.toByteArray();
    }
    private static InputStream input(Runnable before, JsonObject... frames) throws IOException {
        int boundary = bytes(open(),assign()).length;
        return new ByteArrayInputStream(bytes(frames)) {
            private boolean published;
            public synchronized int read() {
                if (!published && pos == boundary && pos < count) { publish(); before.run(); published=true; }
                return super.read();
            }
        };
    }
    private static List<JsonObject> packets(ByteArrayOutputStream output) throws Exception {
        DataInputStream wire = new DataInputStream(new ByteArrayInputStream(output.toByteArray())); List<JsonObject> packets = new ArrayList<>();
        while (wire.available()>0) packets.add(OfflineControllerMain.read(wire));
        return packets;
    }
    private static List<JsonObject> run(boolean failed, Runnable before, JsonObject... frames) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream(); boolean thrown=false;
        try { new OfflineControllerMain(input(before,frames),output).run(); } catch (IOException expected) { thrown=true; }
        check(thrown==failed); return packets(output);
    }
    private static List<String> statuses(List<JsonObject> packets) {
        return packets.stream().map(p->((JsonPrimitive)p.get("status")).getContent()).toList();
    }
    private static void effects(int completes) {
        check(WorldRunLifecycle.completes == completes);
        check(WorldRunLifecycle.opens == 1 && WorldRunLifecycle.assigns == 1);
        check(WorldRunLifecycle.begins == 1 && WorldRunLifecycle.dbOpens == 1 && WorldRunLifecycle.edits == 1 &&
                WorldRunLifecycle.dbCloses == 1 && WorldRunLifecycle.finishes == 1);
    }
    private static void rejected(Runnable before, JsonObject frame) throws Exception {
        check(statuses(run(true,before,open(),assign(),frame,frame)).equals(List.of("OPEN","ASSIGNED","UNKNOWN")));
        effects(0); check(WorldRunLifecycle.closes==0);
    }
    private static void replaceState(String world, String generation, String run, long revision, WorldRunPhase payload) {
        WorldRunState old = WorldRunLifecycle.current.getState();
        WorldRunLifecycle.current = new WorldRunStatus("6".repeat(64),new WorldRunState(1,world,generation,run,revision,
                old.getPaths(),old.getOriginSha256(),old.getPreviousStateSha256(),payload));
    }
    private static WorldRunPhase.OfflineSavedPendingExit saved() {
        return (WorldRunPhase.OfflineSavedPendingExit)WorldRunLifecycle.current.getState().getPayload();
    }
    private static WorldProcessIdentity otherIdentity(WorldProcessIdentity value, String field) {
        return new WorldProcessIdentity(field.equals("hostHash")?"0".repeat(64):value.getHostHash(),
                field.equals("bootId")?"other-boot":value.getBootId(),field.equals("pid")?value.getPid()+1:value.getPid(),
                field.equals("startId")?"other-start":value.getStartId());
    }
    private static void receiptCases() throws Exception {
        JsonObject outer=changed(changed(complete(),"expectedStateSha256",text("6".repeat(64))),"receiptSha256",text("2".repeat(64)));
        for (String field:List.of("world","run","revision","worker")) {
            reset(); rejected(()->{
                WorldRunPhase.OfflineSavedPendingExit s=saved();
                replaceState(WorldRunLifecycle.ID,WorldRunLifecycle.ID,OP,4,new WorldRunPhase.OfflineSavedPendingExit(s.getWriter(),s.getWorker(),"2".repeat(64)));
                WorldRunState r=WorldRunLifecycle.receiptPending.getState();
                WorldRunPhase.OfflinePending pending=(WorldRunPhase.OfflinePending)r.getPayload();
                WorldRunPhase payload=field.equals("worker")?new WorldRunPhase.OfflinePending(pending.getWriter(),
                        otherIdentity(pending.getWorker(),"pid"),pending.getOperationId(),pending.getRequestSha256(),pending.getPreviousSealSha256(),null):pending;
                WorldRunLifecycle.receiptPending=new WorldRunStatus("4".repeat(64),new WorldRunState(1,
                        field.equals("world")?OTHER:r.getWorldUuid(),r.getGeneration(),field.equals("run")?OTHER:r.getRunUuid(),
                        field.equals("revision")?r.getStateRevision()+1:r.getStateRevision(),r.getPaths(),r.getOriginSha256(),r.getPreviousStateSha256(),payload));
            },outer); check(WorldRunLifecycle.completeCalls==1);
        }
    }
    private static void stateCases() throws Exception {
        JsonObject outer = changed(complete(),"expectedStateSha256",text("6".repeat(64)));
        for (String field:List.of("hostHash","bootId","pid","startId")) {
            reset(); rejected(()->{
                WorldRunPhase.OfflineSavedPendingExit s=saved(); WorldProcessIdentity w=s.getWorker();
                WorldProcessIdentity other=otherIdentity(w,field);
                WorldProcessIdentities.exited.add(other);
                replaceState(WorldRunLifecycle.ID,WorldRunLifecycle.ID,OP,4,new WorldRunPhase.OfflineSavedPendingExit(s.getWriter(),other,s.getReceiptSha256()));
            },outer); check(WorldRunLifecycle.completeCalls==0);
        }
        for (String field:List.of("session","owner.hostHash","owner.bootId","owner.pid","owner.startId","lease","epoch","generation","controller.hostHash","controller.bootId","controller.pid","controller.startId","drain")) {
            reset(); rejected(()->{
                WorldRunPhase.OfflineSavedPendingExit s=saved(); WorldOfflineWriter w=s.getWriter(); WorldManagementBinding m=w.getManagement();
                WorldManagementBinding other=new WorldManagementBinding(field.equals("lease")?OTHER:m.getLeaseId(),field.equals("epoch")?2:m.getEpoch(),
                        field.equals("generation")?OTHER:m.getGeneration(),field.startsWith("controller.")?otherIdentity(m.getController(),field.substring(11)):m.getController(),
                        field.equals("drain")?"0".repeat(64):m.getDrainSha256());
                WorldOfflineWriter writer=new WorldOfflineWriter(field.equals("session")?OTHER:w.getSessionId(),
                        field.startsWith("owner.")?otherIdentity(w.getOwner(),field.substring(6)):w.getOwner(),other);
                replaceState(WorldRunLifecycle.ID,WorldRunLifecycle.ID,OP,4,new WorldRunPhase.OfflineSavedPendingExit(writer,s.getWorker(),s.getReceiptSha256()));
            },outer); check(WorldRunLifecycle.completeCalls==1);
        }
        reset(); rejected(()->replaceState(WorldRunLifecycle.ID,OTHER,OP,4,saved()),outer);
        reset(); rejected(()->replaceState(WorldRunLifecycle.ID,WorldRunLifecycle.ID,OTHER,4,saved()),
                changed(changed(outer,"operationId",text(OTHER)),"runUuid",text(OTHER)));
        check(WorldRunLifecycle.completeCalls==0);
        reset(); rejected(()->replaceState(OTHER,WorldRunLifecycle.ID,OP,4,saved()),changed(outer,"worldUuid",text(OTHER)));
        reset(); rejected(()->replaceState(WorldRunLifecycle.ID,WorldRunLifecycle.ID,OTHER,4,saved()),changed(outer,"runUuid",text(OTHER)));
        reset(); rejected(()->replaceState(WorldRunLifecycle.ID,WorldRunLifecycle.ID,OP,5,saved()),changed(outer,"expectedRevision",JsonElementKt.JsonPrimitive(5)));
        reset(); rejected(()->{
            WorldRunPhase.OfflineSavedPendingExit s=saved();
            replaceState(WorldRunLifecycle.ID,WorldRunLifecycle.ID,OP,4,new WorldRunPhase.OfflineSavedPendingExit(s.getWriter(),s.getWorker(),"0".repeat(64)));
        },outer);
    }
    private static void lost(boolean printStream) throws Exception {
        reset(); ByteArrayOutputStream captured = new ByteArrayOutputStream();
        OutputStream sink = new OutputStream() {
            private int header, headerBytes, remaining=-1, frames;
            public void write(int value) throws IOException {
                if (frames>=2) throw new IOException("fixture COMPLETED応答喪失");
                captured.write(value);
                if (remaining<0) { header=(header<<8)|(value&255); if (++headerBytes==4) remaining=header; }
                else if (--remaining==0) { frames++; remaining=-1; header=headerBytes=0; }
            }
        };
        boolean thrown=false;
        try { new OfflineControllerMain(input(()->{},open(),assign(),complete(),complete()),
                printStream?new PrintStream(sink,true,StandardCharsets.UTF_8):sink).run(); }
        catch (IOException expected) { thrown=true; }
        check(thrown); effects(1); check(WorldRunLifecycle.closes==0);
        check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.OfflineIdle);
        check(statuses(packets(captured)).equals(List.of("OPEN","ASSIGNED")));
    }
    public static void main(String[] args) throws Exception {
        reset(); List<JsonObject> normal=run(false,()->{},open(),assign(),complete(),close());
        check(statuses(normal).equals(List.of("OPEN","ASSIGNED","COMPLETED","OWNER_CLOSED")));
        JsonObject packet=normal.get(2);
        check(packet.get("operationId").equals(text(OP)) && packet.get("requestHash").equals(text(HASH)));
        check(packet.get("sessionId").equals(text("sessionid")) && packet.get("nonce").equals(text("nonce")));
        check(packet.get("stateSha256").equals(text("8".repeat(64))) && packet.get("stateRevision").equals(JsonElementKt.JsonPrimitive(5)));
        check(packet.get("runUuid").equals(text(WorldRunLifecycle.RUN)));
        check(packet.get("candidateStateSha256").equals(text("c".repeat(64))) && packet.get("receiptSha256").equals(text("1".repeat(64))));
        check(packet.get("result").equals(Json.Default.parseToJsonElement(new String(HostAdapterCodec.INSTANCE.encodeResponse(WorldRunLifecycle.completedResponse),StandardCharsets.UTF_8))));
        check(HostAdapterCodec.INSTANCE.decodeResponse(packet.get("result").toString().getBytes(StandardCharsets.UTF_8)).equals(WorldRunLifecycle.completedResponse));
        check(WorldRunLifecycle.lastCandidate.equals(new WorldOfflineCandidate("c".repeat(64),"1".repeat(64),WorldRunLifecycle.ID,OP,4)));
        effects(1); check(WorldRunLifecycle.closes==1);
        check(((WorldRunPhase.Ready)WorldRunLifecycle.current.getState().getPayload()).getSealSha256().equals(WorldRunLifecycle.RECEIPT_SEAL));
        check(WorldRunLifecycle.current.getState().getPreviousStateSha256().equals("8".repeat(64)));
        reset(); check(statuses(run(true,()->{},open(),assign(),complete(),complete())).equals(List.of("OPEN","ASSIGNED","COMPLETED","UNKNOWN")));
        effects(1); check(WorldRunLifecycle.closes==0);
        reset(); check(statuses(run(false,()->{},complete())).equals(List.of("REJECTED"))); check(WorldRunLifecycle.completes==0);
        for (String field:complete().keySet()) {
            reset(); Map<String,JsonElement> missing=new LinkedHashMap<>(complete()); missing.remove(field); rejected(()->{},new JsonObject(missing));
            check(WorldRunLifecycle.completeCalls==0);
        }
        for (String field:List.of("sessionId","nonce","openOperationId","operationId","requestHash","editRequestSha256","worldUuid","runUuid","expectedStateSha256","receiptSha256","extra")) {
            reset(); rejected(()->{},changed(complete(),field,text(field.equals("operationId")||field.equals("worldUuid")||field.equals("runUuid")?OTHER:"0".repeat(64))));
            if (!field.equals("expectedStateSha256") && !field.equals("receiptSha256")) check(WorldRunLifecycle.completeCalls==0);
        }
        reset(); rejected(()->{},changed(complete(),"expectedRevision",JsonElementKt.JsonPrimitive(3)));
        reset(); rejected(()->{},changed(complete(),"expectedRevision",text("4")));
        stateCases(); receiptCases();
        reset(); rejected(()->WorldProcessIdentities.parentAlive=false,complete()); check(WorldRunLifecycle.completeCalls==0);
        reset(); rejected(()->WorldProcessIdentities.selfMatches=false,complete()); check(WorldRunLifecycle.completeCalls==0);
        reset(); rejected(()->WorldProcessIdentities.workerAlive=true,complete());
        reset(); rejected(()->WorldRunLifecycle.dbOpen=true,complete());
        reset(); rejected(()->WorldRunLifecycle.receiptMatches=false,complete());
        reset(); rejected(()->WorldRunLifecycle.sealMatches=false,complete());
        reset(); WorldRunLifecycle.failCompleteAfterPublish=true;
        check(statuses(run(true,()->{},open(),assign(),complete(),complete())).equals(List.of("OPEN","ASSIGNED","UNKNOWN")));
        effects(1); check(WorldRunLifecycle.closes==0);
        check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.OfflineIdle);
        check(WorldOfflineWriterSession.lastSession.getStatus().getState().getPayload() instanceof WorldRunPhase.OfflineAssigned);
        lost(false); lost(true);
        System.out.println("offline-complete-vectors="+count);
    }
}
