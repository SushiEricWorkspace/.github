package io.github.sushiericworkspace.devserver;

import io.github.sushiericworkspace.sushiericdatamodel.database.run.*;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.*;
import kotlinx.serialization.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.*;

/** 実DB/nativeを使わず、Wの単発作用と候補公開境界を検査する。 */
public final class OfflineWorkerVectors {
    private static final String OP="00000000-0000-4000-8000-000000000003";
    private static final String HASH="2".repeat(64);
    private static int count;

    private static void check(boolean value) { if(!value) throw new AssertionError(); count++; }
    private static JsonElement text(String value) { return JsonElementKt.JsonPrimitive(value); }
    private static JsonObject identity(long pid) {
        WorldProcessIdentity value=WorldProcessIdentities.identity(pid);
        return new JsonObject(Map.of("hostHash",text(value.getHostHash()),"bootId",text(value.getBootId()),
                "pid",JsonElementKt.JsonPrimitive(pid),"startId",text(value.getStartId())));
    }
    private static JsonObject changed(JsonObject value,String key,JsonElement replacement) {
        Map<String,JsonElement> result=new LinkedHashMap<>(value); result.put(key,replacement); return new JsonObject(result);
    }
    private static JsonObject prepare() {
        String id=WorldRunLifecycle.ID;
        String root=Path.of(".").toAbsolutePath().toString();
        Map<String,JsonElement> v=new LinkedHashMap<>();
        for(String key:List.of("command","operationId","requestHash","sessionId","nonce","leaseId","generation",
                "drainSha256","worldRoot","shopDirectory","worldUuid","runUuid","expectedStateSha256"))
            v.put(key,text(switch(key) {
                case "command" -> "prepare";
                case "operationId" -> OP;
                case "requestHash" -> HASH;
                case "leaseId","generation","worldUuid" -> id;
                case "runUuid" -> WorldRunLifecycle.RUN;
                case "drainSha256" -> "a".repeat(64);
                case "expectedStateSha256" -> "b".repeat(64);
                case "worldRoot" -> root;
                case "shopDirectory" -> Path.of(root).resolve("shop_data").toString();
                default -> key.toLowerCase(Locale.ROOT);
            }));
        v.put("epoch",JsonElementKt.JsonPrimitive(1));
        v.put("expectedRevision",JsonElementKt.JsonPrimitive(1));
        v.put("controller",identity(ProcessHandle.current().parent().orElseThrow().pid()));
        v.put("owner",identity(WorldProcessIdentities.CONTROLLER));
        v.put("worker",identity(ProcessHandle.current().pid()));
        v.put("request",Json.Default.parseToJsonElement("{\"requestId\":\""+OP+"\",\"worldUuid\":\""+id+
                "\",\"action\":{\"type\":\"io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterAction.Edit\","+
                "\"products\":[{\"type\":\"io.github.sushiericworkspace.sushiericdatamodel.database.host.HostProductChange.Save\","+
                "\"id\":\"tab.apple\",\"expectedHash\":null,\"definition\":{\"itemInternalId\":\"minecraft:stone\",\"purchasePrice\":123},"+
                "\"stock\":{\"type\":\"io.github.sushiericworkspace.sushiericdatamodel.database.host.HostStockEdit.Register\",\"quantity\":7,\"revision\":null}}]}}"));
        return new JsonObject(v);
    }
    private static JsonObject admit() {
        return new JsonObject(Map.of("command",text("admit"),"operationId",text(OP),"requestHash",text(HASH),
                "sessionId",text("sessionid"),"nonce",text("nonce"),"preparedStateSha256",text("b".repeat(64)),
                "expectedStateSha256",text("d".repeat(64)),"expectedRevision",JsonElementKt.JsonPrimitive(2),
                "runUuid",text(WorldRunLifecycle.RUN),"editRequestSha256",text("e".repeat(64))));
    }
    private static void reset() {
        WorldProcessIdentities.parentAlive=true; WorldProcessIdentities.controllerAlive=true;
        WorldProcessIdentities.selfMatches=true; WorldProcessIdentities.workerAlive=true;
        WorldProcessIdentities.failAfterOpen=false; WorldProcessIdentities.parentChecks=0;
        WorldRunLifecycle.begins=WorldRunLifecycle.dbOpens=WorldRunLifecycle.edits=WorldRunLifecycle.dbCloses=WorldRunLifecycle.finishes=0;
        WorldRunLifecycle.opens=WorldRunLifecycle.closes=0;
        WorldRunLifecycle.dbOpen=WorldRunLifecycle.failBeginAfterPublish=WorldRunLifecycle.unknownEdit=false;
        WorldRunLifecycle.rejectedEdit=WorldRunLifecycle.failDbClose=WorldRunLifecycle.failFinishAfterPublish=false;
        WorldRunLifecycle.killParentAfterEdit=WorldRunLifecycle.killControllerAfterEdit=false;
        WorldRunLifecycle.lastBeginRequest=WorldRunLifecycle.lastEditRequest=null; WorldRunLifecycle.observedShop=null;
        WorldProcessIdentity parent=WorldProcessIdentities.identity(ProcessHandle.current().parent().orElseThrow().pid());
        WorldRunLifecycle.binding=new WorldOfflineOwnerBinding(WorldProcessIdentities.identity(WorldProcessIdentities.CONTROLLER),
                new WorldManagementBinding(WorldRunLifecycle.ID,1,WorldRunLifecycle.ID,parent,"a".repeat(64)));
        WorldRunLifecycle.current=WorldRunLifecycle.idle(1);
        WorldRunLifecycle.expectedRequest=HostAdapterCodec.INSTANCE.decodeRequest(prepare().get("request").toString().getBytes(StandardCharsets.UTF_8));
    }
    private static byte[] bytes(JsonObject...frames) throws IOException {
        ByteArrayOutputStream bytes=new ByteArrayOutputStream(); DataOutputStream output=new DataOutputStream(bytes);
        for(JsonObject frame:frames) {
            byte[] body=frame.toString().getBytes(StandardCharsets.UTF_8); output.writeInt(body.length); output.write(body);
        }
        return bytes.toByteArray();
    }
    private static InputStream input(Runnable afterPrepared,JsonObject...frames) throws IOException {
        byte[] bytes=bytes(frames);
        int first=bytes(frames[0]).length;
        return new ByteArrayInputStream(bytes) {
            private boolean published;
            public synchronized int read() {
                if(!published && pos==first && pos<count) {
                    noEffects();
                    WorldRunLifecycle.current=WorldRunLifecycle.assigned(
                            WorldProcessIdentities.identity(ProcessHandle.current().pid()),OP,"e".repeat(64));
                    published=true;
                    afterPrepared.run();
                }
                if(frames.length>=2 && pos==count) throw new AssertionError("最終ACKを読んではいけません");
                return super.read();
            }
        };
    }
    private static List<JsonObject> packets(ByteArrayOutputStream output) throws Exception {
        List<JsonObject> result=new ArrayList<>();
        DataInputStream in=new DataInputStream(new ByteArrayInputStream(output.toByteArray()));
        while(in.available()>0) result.add(OfflineControllerMain.read(in));
        return result;
    }
    private static List<JsonObject> run(boolean failed,Runnable middle,JsonObject...frames) throws Exception {
        ByteArrayOutputStream out=new ByteArrayOutputStream(); boolean thrown=false;
        try { new OfflineControllerMain.Worker(input(middle,frames),out).run(); }
        catch(IOException expected) { thrown=true; }
        check(thrown==failed);
        return packets(out);
    }
    private static List<String> statuses(List<JsonObject> packets) {
        return packets.stream().map(p->((JsonPrimitive)p.get("status")).getContent()).toList();
    }
    private static void noEffects() {
        check(WorldRunLifecycle.begins==0 && WorldRunLifecycle.dbOpens==0 && WorldRunLifecycle.edits==0 &&
                WorldRunLifecycle.dbCloses==0 && WorldRunLifecycle.finishes==0);
    }
    private static void effects(int begins,int opens,int edits,int closes,int finishes) {
        check(WorldRunLifecycle.begins==begins && WorldRunLifecycle.dbOpens==opens && WorldRunLifecycle.edits==edits &&
                WorldRunLifecycle.dbCloses==closes && WorldRunLifecycle.finishes==finishes);
        check(WorldRunLifecycle.opens==0 && WorldRunLifecycle.closes==0);
    }
    private static void common(JsonObject packet,String status,String hash,long revision,String run) {
        for(var entry:Map.of("status",status,"operationId",OP,"requestHash",HASH,"sessionId","sessionid","nonce","nonce",
                "stateSha256",hash,"runUuid",run).entrySet()) check(packet.get(entry.getKey()).equals(text(entry.getValue())));
        check(packet.get("stateRevision").equals(JsonElementKt.JsonPrimitive(revision)));
    }
    public static void main(String[] args) throws Exception {
        reset();
        List<JsonObject> normal=run(false,()->{},prepare(),admit());
        check(statuses(normal).equals(List.of("PREPARED","CANDIDATE")));
        common(normal.get(0),"PREPARED","b".repeat(64),1,WorldRunLifecycle.RUN);
        common(normal.get(1),"CANDIDATE","c".repeat(64),4,OP);
        check(normal.get(1).get("receiptSha256").equals(text("1".repeat(64))));
        check(normal.get(1).get("editRequestSha256").equals(text("e".repeat(64))));
        check(normal.get(1).get("worldUuid").equals(text(WorldRunLifecycle.ID)));
        check(WorldRunLifecycle.expectedRequest.equals(WorldRunLifecycle.lastBeginRequest));
        check(WorldRunLifecycle.expectedRequest.equals(WorldRunLifecycle.lastEditRequest));
        check(((HostAdapterAction.Edit)WorldRunLifecycle.lastEditRequest.getAction()).getProducts().size()==1);
        check(WorldRunLifecycle.observedShop.equals(Path.of(((JsonPrimitive)prepare().get("shopDirectory")).getContent())));
        WorldRunPhase.OfflineSavedPendingExit saved=(WorldRunPhase.OfflineSavedPendingExit)WorldRunLifecycle.current.getState().getPayload();
        check(saved.getWriter().getSessionId().equals(WorldRunLifecycle.RUN));
        check(saved.getWorker().equals(WorldProcessIdentities.INSTANCE.current()));
        effects(1,1,1,1,1);
        reset();
        check(statuses(run(true,()->{},prepare())).equals(List.of("PREPARED","REJECTED"))); noEffects();
        for(String field:List.of("expectedStateSha256","runUuid","leaseId","generation","worldRoot","requestHash")) {
            reset();
            check(statuses(run(true,()->{},changed(prepare(),field,text("old")),admit())).equals(List.of("REJECTED")));
            noEffects();
        }
        for(String actor:List.of("controller","owner","worker")) {
            for(String field:List.of("hostHash","bootId","startId","pid")) {
                reset();
                JsonObject frame=prepare();
                JsonObject person=(JsonObject)frame.get(actor);
                JsonElement value=field.equals("pid")?JsonElementKt.JsonPrimitive(Long.parseLong(((JsonPrimitive)person.get("pid")).getContent())+1):
                        text(field.equals("hostHash")?"b".repeat(64):"old");
                check(statuses(run(true,()->{},changed(frame,actor,changed(person,field,value)),admit()))
                        .equals(List.of("REJECTED"))); noEffects();
            }
        }
        reset();
        check(statuses(run(true,()->{},changed(prepare(),"expectedRevision",JsonElementKt.JsonPrimitive(0)),admit()))
                .equals(List.of("REJECTED"))); noEffects();
        reset();
        check(statuses(run(true,()->{},changed(prepare(),"sql",text("delete")),admit())).equals(List.of("REJECTED"))); noEffects();
        for(String field:List.of("requestId","worldUuid","action")) {
            reset();
            JsonObject request=(JsonObject)prepare().get("request");
            JsonElement value=field.equals("action")?Json.Default.parseToJsonElement("{\"type\":\""+
                    "io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterAction.Products\",\"ids\":[]}"):text(WorldRunLifecycle.RUN);
            check(statuses(run(true,()->{},changed(prepare(),"request",changed(request,field,value)),admit())).equals(List.of("REJECTED")));
            noEffects();
        }
        for(String field:List.of("operationId","requestHash","sessionId","nonce","preparedStateSha256","expectedStateSha256","runUuid","editRequestSha256")) {
            reset();
            check(statuses(run(true,()->{},prepare(),changed(admit(),field,text("old")))).equals(List.of("PREPARED","REJECTED")));
            noEffects();
        }
        reset();
        check(statuses(run(true,()->{},prepare(),changed(admit(),"expectedRevision",JsonElementKt.JsonPrimitive(1))))
                .equals(List.of("PREPARED","REJECTED"))); noEffects();
        for(String mismatch:List.of("previous-state","writer","resumePendingSha256")) {
            reset();
            JsonObject admission=changed(changed(admit(),"expectedStateSha256",text("9".repeat(64))),
                    "expectedRevision",JsonElementKt.JsonPrimitive(7));
            Runnable publish=()->{
                WorldRunState state=WorldRunLifecycle.current.getState();
                WorldRunPhase.OfflineAssigned phase=(WorldRunPhase.OfflineAssigned)state.getPayload();
                WorldOfflineWriter writer=phase.getWriter();
                if(mismatch.equals("writer")) writer=new WorldOfflineWriter(WorldRunLifecycle.ID,
                        writer.getOwner(),writer.getManagement());
                WorldRunPhase.OfflineAssigned replacement=new WorldRunPhase.OfflineAssigned(writer,phase.getWorker(),
                        phase.getOperationId(),phase.getRequestSha256(),phase.getPreviousSealSha256(),
                        mismatch.equals("resumePendingSha256")?"8".repeat(64):null);
                WorldRunLifecycle.current=new WorldRunStatus("9".repeat(64),new WorldRunState(1,
                        state.getWorldUuid(),state.getGeneration(),state.getRunUuid(),7,state.getPaths(),state.getOriginSha256(),
                        mismatch.equals("previous-state")?"8".repeat(64):state.getPreviousStateSha256(),replacement));
                check(admission.get("expectedStateSha256").equals(text(WorldRunLifecycle.current.getSha256())));
                check(admission.get("expectedRevision").equals(JsonElementKt.JsonPrimitive(
                        WorldRunLifecycle.current.getState().getStateRevision())));
                check(admission.get("runUuid").equals(text(WorldRunLifecycle.current.getState().getRunUuid())));
            };
            check(statuses(run(true,publish,prepare(),admission)).equals(List.of("PREPARED","REJECTED")));
            noEffects();
        }
        for(String actor:List.of("P","C")) {
            reset();
            Runnable death=()->{if(actor.equals("P")) WorldProcessIdentities.parentAlive=false; else WorldProcessIdentities.controllerAlive=false;};
            check(statuses(run(true,death,prepare(),admit())).equals(List.of("PREPARED","REJECTED"))); noEffects();
        }
        reset();
        check(statuses(run(true,()->{WorldRunLifecycle.current=WorldRunLifecycle.idle(1);},prepare(),admit()))
                .equals(List.of("PREPARED","REJECTED"))); noEffects();
        reset();
        check(statuses(run(true,()->{},prepare(),changed(admit(),"request",prepare().get("request"))))
                .equals(List.of("PREPARED","REJECTED"))); noEffects();
        for(String actor:List.of("P","C")) {
            reset();
            WorldRunLifecycle.killParentAfterEdit=actor.equals("P");
            WorldRunLifecycle.killControllerAfterEdit=actor.equals("C");
            check(statuses(run(true,()->{},prepare(),admit())).equals(List.of("PREPARED","UNKNOWN")));
            effects(1,1,1,1,0);
            check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.OfflinePending);
        }
        reset(); WorldRunLifecycle.failBeginAfterPublish=true;
        check(statuses(run(true,()->{},prepare(),admit(),admit())).equals(List.of("PREPARED","UNKNOWN")));
        effects(1,0,0,0,0);
        check(WorldRunLifecycle.current.getState().getRunUuid().equals(OP));
        check(WorldRunLifecycle.current.getState().getStateRevision()==3);
        for(String failure:List.of("edit","close","finish")) {
            reset();
            WorldRunLifecycle.unknownEdit=failure.equals("edit"); WorldRunLifecycle.failDbClose=failure.equals("close");
            WorldRunLifecycle.failFinishAfterPublish=failure.equals("finish");
            check(statuses(run(true,()->{},prepare(),admit(),admit())).equals(List.of("PREPARED","UNKNOWN")));
            effects(1,1,1,1,failure.equals("finish")?1:0);
            check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.OfflineSavedPendingExit == failure.equals("finish"));
        }
        reset(); WorldRunLifecycle.rejectedEdit=true;
        check(statuses(run(false,()->{},prepare(),admit())).equals(List.of("PREPARED","CANDIDATE"))); effects(1,1,1,1,1);
        reset();
        ByteArrayOutputStream onlyPrepared=new ByteArrayOutputStream(); boolean[] injected={false}; boolean lost=false;
        try {
            new OfflineControllerMain.Worker(input(()->{},prepare(),admit()),new PrintStream(new OutputStream() {
                int header, headerBytes, bodyBytes=-1;
                public void write(int value) throws IOException {
                    if(bodyBytes==0) { injected[0]=true; throw new IOException("fixture 候補応答喪失"); }
                    onlyPrepared.write(value);
                    if(bodyBytes<0) { header=(header<<8)|(value&255); if(++headerBytes==4) bodyBytes=header; }
                    else bodyBytes--;
                }
            },true,StandardCharsets.UTF_8)).run();
        } catch(IOException expected) { lost=true; }
        check(lost && injected[0]); effects(1,1,1,1,1);
        check(statuses(packets(onlyPrepared)).equals(List.of("PREPARED")));
        check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.OfflineSavedPendingExit);
        System.out.println("offline-worker-vectors="+count);
    }
}
