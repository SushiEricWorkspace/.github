package io.github.sushiericworkspace.devserver;

import io.github.sushiericworkspace.sushiericdatamodel.database.run.*;
import kotlinx.serialization.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.*;

/** 固定Dataの算法を複製せず、公式読取への委譲・期待値・本人境界だけを合成する。 */
public final class OfflineReadyVectors {
    private static final String ID = WorldRunLifecycle.ID;
    private static int count;
    private static void check(boolean value) { if (!value) throw new AssertionError(); count++; }
    private static JsonElement text(String value) { return JsonElementKt.JsonPrimitive(value); }
    private static JsonObject changed(JsonObject value, String key, JsonElement replacement) {
        Map<String,JsonElement> values=new LinkedHashMap<>(value);
        values.put(key,replacement); return new JsonObject(values);
    }
    private static JsonObject identity(long pid) {
        WorldProcessIdentity value=WorldProcessIdentities.identity(pid);
        return new JsonObject(Map.of("hostHash",text(value.getHostHash()),"bootId",text(value.getBootId()),
                "pid",JsonElementKt.JsonPrimitive(value.getPid()),"startId",text(value.getStartId())));
    }
    private static JsonObject frame() {
        Map<String,JsonElement> values=new LinkedHashMap<>();
        for (String key:List.of("command","operationId","requestHash","sessionId","nonce","generation",
                "worldRoot","shopDirectory","worldUuid","runUuid","expectedStateSha256"))
            values.put(key,text(switch(key) {
                case "command" -> "verify-ready";
                case "requestHash", "expectedStateSha256" -> "a".repeat(64);
                case "generation", "worldUuid", "runUuid" -> ID;
                case "worldRoot", "shopDirectory" -> Path.of(key).toAbsolutePath().toString();
                default -> key.toLowerCase(Locale.ROOT);
            }));
        values.put("expectedRevision",JsonElementKt.JsonPrimitive(0));
        values.put("controller",identity(ProcessHandle.current().parent().orElseThrow().pid()));
        values.put("owner",identity(ProcessHandle.current().pid()));
        return new JsonObject(values);
    }
    private static void reset() {
        WorldRunLifecycle.reads=WorldRunLifecycle.opens=WorldRunLifecycle.closes=WorldRunLifecycle.gameExitCalls=0;
        WorldRunLifecycle.failRead=WorldRunLifecycle.loseParentAfterRead=false;
        WorldRunLifecycle.current=WorldRunLifecycle.initialReady();
        WorldProcessIdentities.parentAlive=WorldProcessIdentities.selfMatches=true;
        WorldProcessIdentities.actorPid=ProcessHandle.current().pid();
        WorldProcessIdentities.failAfterOpen=false;
    }
    private static byte[] bytes(JsonObject frame) throws Exception {
        byte[] body=frame.toString().getBytes(StandardCharsets.UTF_8);
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();
        DataOutputStream output=new DataOutputStream(bytes);
        output.writeInt(body.length); output.write(body); return bytes.toByteArray();
    }
    private static JsonObject run(byte[] input, boolean accepted) throws Exception {
        ByteArrayOutputStream output=new ByteArrayOutputStream();
        boolean failed=false;
        try { new OfflineControllerMain(new ByteArrayInputStream(input),output).runReady(); }
        catch (IOException rejected) { failed=true; }
        check(failed != accepted);
        DataInputStream result=new DataInputStream(new ByteArrayInputStream(output.toByteArray()));
        JsonObject response=OfflineControllerMain.read(result);
        check(result.read()==-1);
        check(response.get("status").equals(text(accepted ? "READY_VERIFIED" : "REJECTED")));
        check(WorldRunLifecycle.opens==0 && WorldRunLifecycle.closes==0 && WorldRunLifecycle.gameExitCalls==0);
        return response;
    }
    private static void vector(String name) throws Exception {
        reset();
        JsonObject request=frame();
        switch(name) {
            case "success" -> {
                byte[] once=bytes(request);
                ByteArrayOutputStream twice=new ByteArrayOutputStream(); twice.write(once); twice.write(once);
                JsonObject response=run(twice.toByteArray(),true);
                check(WorldRunLifecycle.reads==1);
                for (String key:List.of("operationId","requestHash","sessionId","nonce","worldUuid","generation","runUuid"))
                    check(response.get(key).equals(request.get(key)));
                check(response.get("stateSha256").equals(request.get("expectedStateSha256")));
                check(response.get("stateRevision").equals(request.get("expectedRevision")));
                check(response.keySet().equals(Set.of("status","operationId","requestHash","sessionId","nonce",
                        "worldUuid","generation","runUuid","stateSha256","stateRevision")));
                check(WorldRunLifecycle.current.getState().getStateRevision()==0);
                check(WorldRunLifecycle.observedWorld.equals(Path.of("worldRoot").toAbsolutePath()));
                check(WorldRunLifecycle.observedShop.equals(Path.of("shopDirectory").toAbsolutePath()));
                check(WorldRunLifecycle.observedWorldUuid.toString().equals(ID));
                return;
            }
            case "chain" -> WorldRunLifecycle.failRead=true;
            case "parent-after-read" -> WorldRunLifecycle.loseParentAfterRead=true;
            case "parent-before-read" -> WorldProcessIdentities.parentAlive=false;
            case "self" -> WorldProcessIdentities.selfMatches=false;
            case "phase" -> WorldRunLifecycle.current=new WorldRunStatus("a".repeat(64),
                    new WorldRunState(1,ID,ID,ID,0,WorldRunLifecycle.current.getState().getPaths(),"a".repeat(64),null,
                            new WorldRunPhase.Running(WorldProcessIdentities.identity(ProcessHandle.current().pid()),null)));
            case "revision" -> request=changed(request,"expectedRevision",JsonElementKt.JsonPrimitive(1));
            case "float" -> request=changed(request,"expectedRevision",JsonElementKt.JsonPrimitive(0.0));
            case "bool" -> request=changed(request,"expectedRevision",JsonElementKt.JsonPrimitive(false));
            case "negative" -> request=changed(request,"expectedRevision",JsonElementKt.JsonPrimitive(-1));
            case "overflow" -> request=changed(request,"expectedRevision",Json.Default.parseToJsonElement("9223372036854775808"));
            case "hash" -> request=changed(request,"expectedStateSha256",text("b".repeat(64)));
            case "world", "generation", "run" -> request=changed(request,
                    name.equals("world") ? "worldUuid" : name.equals("run") ? "runUuid" : "generation",
                    text(WorldRunLifecycle.RUN));
            case "owner-start" -> request=changed(request,"owner",changed((JsonObject)request.get("owner"),"startId",text("other")));
            case "parent-start" -> request=changed(request,"controller",changed((JsonObject)request.get("controller"),"startId",text("other")));
            case "parent-pid" -> request=changed(request,"controller",identity(WorldProcessIdentities.WORKER));
            case "host" -> request=changed(request,"controller",changed((JsonObject)request.get("controller"),"hostHash",text("b".repeat(64))));
            case "boot" -> request=changed(request,"controller",changed((JsonObject)request.get("controller"),"bootId",text("other")));
            case "relative-world" -> request=changed(request,"worldRoot",text("relative"));
            case "relative-shop" -> request=changed(request,"shopDirectory",text("relative"));
            case "command" -> request=changed(request,"command",text("open"));
            case "unknown" -> request=changed(request,"allowStart",JsonElementKt.JsonPrimitive(true));
            case "missing" -> { Map<String,JsonElement> values=new LinkedHashMap<>(request); values.remove("generation"); request=new JsonObject(values); }
            case "malformed" -> { run(new byte[]{0,0,0,2,123},false); return; }
            default -> throw new AssertionError(name);
        }
        run(bytes(request),false);
        if (name.equals("chain") || name.equals("parent-after-read")) check(WorldRunLifecycle.reads==1);
        if (name.equals("parent-before-read") || name.equals("self") || name.equals("parent-pid")) check(WorldRunLifecycle.reads==0);
    }
    public static void main(String[] args) throws Exception {
        List<String> names=args.length==0 ? List.of("success","chain","parent-after-read","parent-before-read","self","phase",
                "revision","float","bool","negative","overflow","hash","world","generation","run","owner-start",
                "parent-start","parent-pid","host","boot","relative-world","relative-shop","command","unknown","missing","malformed") : List.of(args);
        for (String name:names) vector(name);
        System.out.println("offline-ready-vectors="+names.size()+" checks="+count);
    }
}
