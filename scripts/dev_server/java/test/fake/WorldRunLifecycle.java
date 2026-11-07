package io.github.sushiericworkspace.sushiericdatamodel.database.run;

import java.nio.file.Path;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterRequest;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterResponse;
import java.util.UUID;

/** DBもrun-stateも作らない、作用境界と失敗注入だけのfixture。 */
public final class WorldRunLifecycle {
    public static int begins, dbOpens, edits, dbCloses, finishes;
    public static boolean dbOpen, failBeginAfterPublish, unknownEdit, rejectedEdit, failDbClose, failFinishAfterPublish;
    public static boolean killParentAfterEdit, killControllerAfterEdit;
    public static HostAdapterRequest expectedRequest, lastBeginRequest, lastEditRequest;
    public static Path observedShop;
    public static int opens;
    public static int closes;
    public static int assigns;
    public static int completes, completeCalls;
    public static boolean receiptMatches = true, sealMatches = true, failCompleteAfterPublish;
    public static WorldOfflineCandidate lastCandidate;
    public static WorldRunStatus receiptPending;
    public static final String RECEIPT_SEAL = "5".repeat(64);
    public static HostAdapterResponse completedResponse;
    public static boolean failAssignAfterPublish;
    public static WorldOfflineOwnerBinding binding;
    public static boolean failOpen;
    public static boolean failClose;
    public static boolean failCloseAfterPublish;
    public static WorldRunStatus current = initialReady();
    public static final String ID = "00000000-0000-4000-8000-000000000001";
    public static final String RUN = "00000000-0000-4000-8000-000000000002";
    public WorldRunLifecycle(Path world, Path shop, UUID uuid, int timeout) {}
    public WorldRunStatus readStatus() { return current; }
    public WorldOfflineOperation beginOfflineEdit(WorldRunStatus expected, HostAdapterRequest request) {
        if (expected != current || !(expected.getState().getPayload() instanceof WorldRunPhase.OfflineAssigned phase)
                || !phase.getWorker().equals(WorldProcessIdentities.INSTANCE.current()) || !request.equals(expectedRequest))
            throw new IllegalStateException("fixture admission binding不一致");
        checkWorkerActors();
        lastBeginRequest=request;
        begins++;
        current=advance("f",request.getRequestId(),new WorldRunPhase.OfflinePending(phase.getWriter(),phase.getWorker(),
                phase.getOperationId(),phase.getRequestSha256(),phase.getPreviousSealSha256(),null));
        if (failBeginAfterPublish) throw new IllegalStateException("fixture Pending公開後失敗");
        return new WorldOfflineOperation(current);
    }
    public static void checkWorkerActors() {
        WorldProcessIdentities.INSTANCE.requireAlive(binding.getManagement().getController());
        WorldProcessIdentities.INSTANCE.requireAlive(binding.getOwner());
    }
    public static WorldRunStatus advance(String hash, String run, WorldRunPhase phase) {
        WorldRunState before=current.getState();
        return new WorldRunStatus(hash.repeat(64),new WorldRunState(1,before.getWorldUuid(),before.getGeneration(),run,
                before.getStateRevision()+1,before.getPaths(),before.getOriginSha256(),current.getSha256(),phase));
    }
    public WorldOfflineWriterSession openManagedOfflineWriter(WorldRunStatus status, WorldOfflineOwnerBinding binding) {
        opens++;
        if (failOpen) throw new IllegalStateException("fixture 公開後応答喪失");
        WorldRunLifecycle.binding = binding;
        current = idle(1);
        return new WorldOfflineWriterSession();
    }
    public static WorldRunStatus ready(long revision) {
        if (revision > 2 && current.getState().getPayload() instanceof WorldRunPhase.OfflineIdle idle) {
            WorldRunState before = current.getState();
            return new WorldRunStatus("9".repeat(64),new WorldRunState(1,before.getWorldUuid(),before.getGeneration(),RUN,revision,
                    before.getPaths(),before.getOriginSha256(),current.getSha256(),new WorldRunPhase.Ready(idle.getSealSha256(),"0".repeat(64))));
        }
        return ready(revision, "c", RUN);
    }
    public static WorldRunStatus initialReady() {
        return ready(0, "a", ID);
    }
    private static WorldRunStatus ready(long revision, String hash, String runUuid) {
        String value = hash.repeat(64);
        return new WorldRunStatus(value, new WorldRunState(1, ID, ID, runUuid, revision,
                new WorldRunPaths("fixture", "fixture-db", "fixture-shop"), "a".repeat(64), null,
                new WorldRunPhase.Ready(value, value)));
    }
    public static WorldRunStatus assigned(WorldProcessIdentity worker, String operationId, String requestHash) {
        WorldRunState idle = current.getState();
        WorldOfflineWriter writer = ((WorldRunPhase.OfflineIdle)idle.getPayload()).getWriter();
        return new WorldRunStatus("d".repeat(64), new WorldRunState(1, ID, ID, RUN,
                idle.getStateRevision()+1, idle.getPaths(), idle.getOriginSha256(), current.getSha256(),
                new WorldRunPhase.OfflineAssigned(writer, worker, operationId, requestHash, "a".repeat(64), null)));
    }

    public static WorldRunStatus idle(long revision) {
        return new WorldRunStatus("b".repeat(64), new WorldRunState(1, ID, ID, RUN, revision,
                new WorldRunPaths("fixture", "fixture-db", "fixture-shop"), "a".repeat(64), "a".repeat(64),
                new WorldRunPhase.OfflineIdle(new WorldOfflineWriter(RUN,
                        binding.getOwner(), binding.getManagement()),
                        "a".repeat(64), "b".repeat(64))));
    }
}
