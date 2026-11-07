package io.github.sushiericworkspace.devserver;

import io.github.sushiericworkspace.sushiericdatamodel.database.run.*;
import kotlinx.serialization.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.*;

/** Dataの保存/hash/recover算法を持たず、候補bindingと単発委譲を検証する。 */
public final class OfflineGameExitVectors {
    private static final String ID = WorldRunLifecycle.ID;
    private static final String RUN = WorldRunLifecycle.RUN;
    private static final String OTHER = "00000000-0000-4000-8000-000000000003";
    private static final String CANDIDATE = "6".repeat(64);
    private static int count;
    private static void check(boolean value) { if (!value) throw new AssertionError(); count++; }
    private static JsonElement text(String value) { return JsonElementKt.JsonPrimitive(value); }
    private static JsonElement number(long value) { return JsonElementKt.JsonPrimitive(value); }
    private static JsonObject changed(JsonObject value, String key, JsonElement replacement) {
        Map<String,JsonElement> values=new LinkedHashMap<>(value); values.put(key,replacement); return new JsonObject(values);
    }
    private static WorldProcessIdentity identity(long pid) { return WorldProcessIdentities.identity(pid); }
    private static JsonObject jsonIdentity(long pid) { return jsonIdentity(identity(pid)); }
    private static JsonObject jsonIdentity(WorldProcessIdentity value) {
        return new JsonObject(Map.of("hostHash",text(value.getHostHash()),"bootId",text(value.getBootId()),
                "pid",number(value.getPid()),"startId",text(value.getStartId())));
    }
    private static WorldManagementBinding management() {
        return new WorldManagementBinding(ID,1,ID,identity(ProcessHandle.current().parent().orElseThrow().pid()),"a".repeat(64));
    }
    private static JsonObject frame() {
        Map<String,JsonElement> value=new LinkedHashMap<>();
        for (String key:List.of("command","operationId","requestHash","sessionId","nonce","leaseId","generation",
                "drainSha256","worldRoot","shopDirectory","worldUuid","runUuid","expectedStateSha256","saveProofSha256","requestedStateSha256"))
            value.put(key,text(switch(key) {
                case "command" -> "observe-game-exit";
                case "operationId" -> "game-exit";
                case "requestHash", "drainSha256" -> "a".repeat(64);
                case "expectedStateSha256" -> CANDIDATE;
                case "saveProofSha256" -> "7".repeat(64);
                case "requestedStateSha256" -> "4".repeat(64);
                case "leaseId", "generation", "worldUuid" -> ID;
                case "runUuid" -> RUN;
                case "worldRoot", "shopDirectory" -> Path.of(key).toAbsolutePath().toString();
                default -> key.toLowerCase(Locale.ROOT);
            }));
        value.put("epoch",number(1)); value.put("expectedRevision",number(3));
        value.put("controller",jsonIdentity(ProcessHandle.current().parent().orElseThrow().pid()));
        value.put("owner",jsonIdentity(ProcessHandle.current().pid())); value.put("gameOwner",jsonIdentity(WorldProcessIdentities.WORKER));
        return new JsonObject(value);
    }
    private static WorldRunPaths gamePaths() {
        Path world=Path.of("worldRoot").toAbsolutePath();
        return new WorldRunPaths(world.toString(),world.resolve("sushieric/server.sqlite").toString(),
                Path.of("shopDirectory").toAbsolutePath().toString());
    }
    private static WorldRunStatus initialGame() {
        return new WorldRunStatus("a".repeat(64),new WorldRunState(1,ID,ID,ID,0,gamePaths(),
                "0".repeat(64),null,new WorldRunPhase.Ready("c".repeat(64),"0".repeat(64))));
    }
    private static void reset() {
        WorldProcessIdentities.parentAlive=WorldProcessIdentities.selfMatches=WorldProcessIdentities.controllerAlive=true;
        WorldProcessIdentities.actorPid=ProcessHandle.current().pid(); WorldProcessIdentities.workerAlive=true;
        WorldProcessIdentities.failAfterOpen=false; WorldProcessIdentities.parentChecks=0; WorldProcessIdentities.exited.clear();
        WorldProcessIdentities.exited.add(identity(WorldProcessIdentities.WORKER));
        WorldRunLifecycle.opens=WorldRunLifecycle.assigns=WorldRunLifecycle.closes=WorldRunLifecycle.completes=0;
        WorldRunLifecycle.begins=WorldRunLifecycle.dbOpens=WorldRunLifecycle.edits=WorldRunLifecycle.dbCloses=WorldRunLifecycle.finishes=0;
        WorldRunLifecycle.gameExitCalls=WorldRunLifecycle.gameExits=0; WorldRunLifecycle.lastGameCandidate=null;
        WorldRunLifecycle.failGameExitAfterPublish=WorldRunLifecycle.dbOpen=false; WorldRunLifecycle.sealMatches=true;
        WorldRunLifecycle.gameWriters=List.of(identity(WorldProcessIdentities.CONTROLLER));
        WorldProcessIdentities.exited.add(identity(WorldProcessIdentities.CONTROLLER));
        WorldRunLifecycle.binding=null;
        WorldRunLifecycle.current=initialGame();
        WorldRunLifecycle.current=WorldRunLifecycle.advance("1",RUN,new WorldRunPhase.Running(identity(WorldProcessIdentities.WORKER),management()));
        check(WorldRunLifecycle.current.getState().getStateRevision()==1 && WorldRunLifecycle.current.getState().getPreviousStateSha256().equals("a".repeat(64)));
        WorldRunLifecycle.current=WorldRunLifecycle.advance("4",RUN,new WorldRunPhase.ManagedSaveRequested(identity(WorldProcessIdentities.WORKER),management(),OTHER));
        check(WorldRunLifecycle.current.getState().getStateRevision()==2 && WorldRunLifecycle.current.getState().getPreviousStateSha256().equals("1".repeat(64)));
        replace(ID,ID,RUN,3,"4".repeat(64),new WorldRunPhase.SavedPendingExit(identity(WorldProcessIdentities.WORKER),"7".repeat(64),management()));
    }
    private static void replace(String world, String generation, String run, long revision, String previous, WorldRunPhase phase) {
        WorldRunLifecycle.current=new WorldRunStatus(CANDIDATE,new WorldRunState(1,world,generation,run,revision,
                gamePaths(),
                "0".repeat(64),previous,phase));
    }
    private static WorldRunPhase.SavedPendingExit saved() { return (WorldRunPhase.SavedPendingExit)WorldRunLifecycle.current.getState().getPayload(); }
    private static byte[] bytes(JsonObject... frames) throws IOException {
        ByteArrayOutputStream result=new ByteArrayOutputStream(); DataOutputStream output=new DataOutputStream(result);
        for (JsonObject frame:frames) { byte[] body=frame.toString().getBytes(StandardCharsets.UTF_8); output.writeInt(body.length); output.write(body); }
        return result.toByteArray();
    }
    private static List<JsonObject> packets(byte[] raw) throws Exception {
        DataInputStream input=new DataInputStream(new ByteArrayInputStream(raw)); List<JsonObject> result=new ArrayList<>();
        while (input.available()>0) result.add(OfflineControllerMain.read(input)); return result;
    }
    private static List<JsonObject> run(boolean failed, JsonObject first) throws Exception {
        ByteArrayInputStream input=new ByteArrayInputStream(bytes(first,frame())); ByteArrayOutputStream output=new ByteArrayOutputStream();
        boolean thrown=false;
        try { new OfflineControllerMain(input,output).run(); } catch (IOException expected) { thrown=true; }
        check(thrown==failed); check(input.available()==bytes(frame()).length);
        return packets(output.toByteArray());
    }
    private static void effects(int calls, int published) {
        check(WorldRunLifecycle.gameExitCalls==calls && WorldRunLifecycle.gameExits==published);
        check(WorldRunLifecycle.opens==0 && WorldRunLifecycle.assigns==0 && WorldRunLifecycle.closes==0 && WorldRunLifecycle.completes==0);
        check(WorldRunLifecycle.begins==0 && WorldRunLifecycle.dbOpens==0 && WorldRunLifecycle.edits==0 && WorldRunLifecycle.dbCloses==0 && WorldRunLifecycle.finishes==0);
    }
    private static void rejected(JsonObject first, int calls) throws Exception {
        WorldRunStatus original=WorldRunLifecycle.current;
        check(run(true,first).get(0).get("status").equals(text(calls==0?"REJECTED":"UNKNOWN")));
        effects(calls,0); check(WorldRunLifecycle.current==original);
    }
    private static WorldProcessIdentity other(WorldProcessIdentity value, String field) {
        return new WorldProcessIdentity(field.equals("hostHash")?"b".repeat(64):value.getHostHash(),
                field.equals("bootId")?"other-boot":value.getBootId(),field.equals("pid")?value.getPid()+1:value.getPid(),
                field.equals("startId")?"other-start":value.getStartId());
    }
    private static void rejectedState() throws Exception {
        WorldRunLifecycle.current=new WorldRunStatus("b".repeat(64),WorldRunLifecycle.current.getState());
        rejected(changed(frame(),"expectedStateSha256",text("b".repeat(64))),0);
    }
    private static void stateCases() throws Exception {
        for (String field:List.of("world","generation","run","revision","previous","phase","proof")) {
            reset(); WorldRunPhase.SavedPendingExit phase=saved();
            replace(field.equals("world")?OTHER:ID,field.equals("generation")?OTHER:ID,field.equals("run")?OTHER:RUN,
                    field.equals("revision")?4:3,field.equals("previous")?"b".repeat(64):"4".repeat(64),
                    field.equals("phase")?new WorldRunPhase.Ready("5".repeat(64),"8".repeat(64)):
                    new WorldRunPhase.SavedPendingExit(phase.getOwner(),field.equals("proof")?"b".repeat(64):phase.getSaveProofSha256(),phase.getManagement()));
            rejectedState();
        }
        for (String field:List.of("hostHash","bootId","pid","startId")) {
            reset(); WorldRunPhase.SavedPendingExit phase=saved();
            replace(ID,ID,RUN,3,"4".repeat(64),new WorldRunPhase.SavedPendingExit(other(phase.getOwner(),field),phase.getSaveProofSha256(),phase.getManagement()));
            rejectedState();
        }
        for (String field:List.of("lease","epoch","generation","controller.hostHash","controller.bootId","controller.pid","controller.startId","drain","null")) {
            reset(); WorldRunPhase.SavedPendingExit phase=saved(); WorldManagementBinding m=phase.getManagement();
            WorldManagementBinding changed=new WorldManagementBinding(field.equals("lease")?OTHER:ID,field.equals("epoch")?2:1,
                    field.equals("generation")?OTHER:ID,field.startsWith("controller.")?other(m.getController(),field.substring(11)):m.getController(),
                    field.equals("drain")?"b".repeat(64):m.getDrainSha256());
            replace(ID,ID,RUN,3,"4".repeat(64),new WorldRunPhase.SavedPendingExit(phase.getOwner(),phase.getSaveProofSha256(),field.equals("null")?null:changed));
            rejectedState();
        }
    }
    private static JsonObject gameFrame(WorldProcessIdentity game) {
        WorldRunPhase.SavedPendingExit phase=saved();
        WorldRunLifecycle.current=initialGame();
        WorldRunLifecycle.current=WorldRunLifecycle.advance("2",RUN,new WorldRunPhase.Running(game,phase.getManagement()));
        WorldRunLifecycle.current=WorldRunLifecycle.advance("3",RUN,new WorldRunPhase.ManagedSaveRequested(game,phase.getManagement(),OTHER));
        check(WorldRunLifecycle.current.getState().getStateRevision()==2 && WorldRunLifecycle.current.getState().getPreviousStateSha256().equals("2".repeat(64)));
        check(WorldRunLifecycle.current.getState().getPaths().equals(gamePaths()) && WorldRunLifecycle.current.getState().getOriginSha256().equals("0".repeat(64)));
        replace(ID,ID,RUN,3,"3".repeat(64),new WorldRunPhase.SavedPendingExit(game,phase.getSaveProofSha256(),phase.getManagement()));
        WorldRunLifecycle.current=new WorldRunStatus("b".repeat(64),WorldRunLifecycle.current.getState());
        return changed(changed(changed(frame(),"expectedStateSha256",text("b".repeat(64))),"requestedStateSha256",text("3".repeat(64))),"gameOwner",jsonIdentity(game));
    }
    private static void gameActorCases() throws Exception {
        for (long pid:List.of(ProcessHandle.current().pid(),ProcessHandle.current().parent().orElseThrow().pid())) {
            reset(); rejected(gameFrame(identity(pid)),0);
        }
        reset();
        WorldProcessIdentity oldGame=other(identity(ProcessHandle.current().pid()),"startId");
        WorldProcessIdentities.exited.add(oldGame);
        JsonObject request=gameFrame(oldGame); WorldRunStatus candidate=WorldRunLifecycle.current;
        JsonObject response=run(false,request).get(0); effects(1,1);
        check(WorldRunLifecycle.lastGameCandidate==candidate);
        check(response.get("status").equals(text("GAME_EXITED")) && response.get("candidateStateSha256").equals(text("b".repeat(64))));
        check(response.get("stateSha256").equals(text("9".repeat(64))) && response.get("stateRevision").equals(number(4)));
        check(response.get("runUuid").equals(text(RUN)) && WorldRunLifecycle.current.getState().getPreviousStateSha256().equals("b".repeat(64)));
    }
    private static void lost(boolean printStream) throws Exception {
        reset(); OutputStream failed=new OutputStream() { public void write(int value) throws IOException { throw new IOException("fixture 応答喪失"); } };
        ByteArrayInputStream input=new ByteArrayInputStream(bytes(frame(),frame())); boolean thrown=false;
        try { new OfflineControllerMain(input,printStream?new PrintStream(failed,true,StandardCharsets.UTF_8):failed).run(); }
        catch (IOException expected) { thrown=true; }
        check(thrown); effects(1,1); check(input.available()==bytes(frame()).length);
        check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.Ready);
    }
    public static void main(String[] args) throws Exception {
        reset(); WorldRunStatus candidate=WorldRunLifecycle.current;
        JsonObject result=run(false,frame()).get(0); effects(1,1);
        check(WorldRunLifecycle.lastGameCandidate==candidate);
        check(result.get("status").equals(text("GAME_EXITED")) && result.get("operationId").equals(frame().get("operationId")) && result.get("requestHash").equals(frame().get("requestHash")));
        check(result.get("sessionId").equals(frame().get("sessionId")) && result.get("nonce").equals(frame().get("nonce")));
        check(result.get("worldUuid").equals(text(ID)) && result.get("runUuid").equals(text(RUN)));
        check(result.get("candidateStateSha256").equals(text(CANDIDATE)) && result.get("saveProofSha256").equals(text("7".repeat(64))));
        check(result.get("stateSha256").equals(text("9".repeat(64))) && result.get("stateRevision").equals(number(4)));
        check(WorldRunLifecycle.current.getState().getPreviousStateSha256().equals(CANDIDATE));
        check(WorldRunLifecycle.current.getState().getPaths().equals(gamePaths()) && WorldRunLifecycle.current.getState().getOriginSha256().equals("0".repeat(64)));
        check(((WorldRunPhase.Ready)WorldRunLifecycle.current.getState().getPayload()).getSealSha256().equals("5".repeat(64)));
        check(WorldRunLifecycle.observedWorld.equals(Path.of("worldRoot").toAbsolutePath()) && WorldRunLifecycle.observedShop.equals(Path.of("shopDirectory").toAbsolutePath()) && WorldRunLifecycle.observedWorldUuid.equals(UUID.fromString(ID)));
        for (String field:frame().keySet()) {
            if (field.equals("command")) continue;
            reset(); Map<String,JsonElement> missing=new LinkedHashMap<>(frame()); missing.remove(field); rejected(new JsonObject(missing),0);
        }
        for (String field:List.of("expectedStateSha256","saveProofSha256","requestedStateSha256","extra")) { reset(); rejected(changed(frame(),field,text("b".repeat(64))),0); }
        reset(); rejected(changed(frame(),"expectedRevision",number(2)),0);
        reset(); rejected(changed(frame(),"expectedRevision",text("3")),0);
        reset(); WorldProcessIdentities.parentAlive=false; rejected(frame(),0);
        reset(); WorldProcessIdentities.selfMatches=false; rejected(frame(),0);
        stateCases(); gameActorCases();
        reset(); WorldProcessIdentities.exited.remove(identity(WorldProcessIdentities.WORKER)); rejected(frame(),1);
        reset(); WorldProcessIdentities.exited.remove(identity(WorldProcessIdentities.CONTROLLER)); rejected(frame(),1);
        reset(); WorldRunLifecycle.dbOpen=true; rejected(frame(),1);
        reset(); WorldRunLifecycle.sealMatches=false; rejected(frame(),1);
        reset(); WorldRunLifecycle.failGameExitAfterPublish=true;
        check(run(true,frame()).get(0).get("status").equals(text("UNKNOWN"))); effects(1,1);
        check(WorldRunLifecycle.current.getState().getPayload() instanceof WorldRunPhase.Ready);
        lost(false); lost(true);
        System.out.println("offline-game-exit-vectors="+count);
    }
}
