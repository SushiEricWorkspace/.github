package io.github.sushiericworkspace.sushiericdatamodel.database.run;

import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterRequest;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterResponse;

public final class WorldOfflineWriterSession {
    public static HostAdapterRequest lastRequest;
    public static WorldOfflineWriterSession lastSession;
    public WorldOfflineWriterSession() { lastSession = this; }
    private WorldRunStatus status = WorldRunLifecycle.current;
    public WorldRunStatus getStatus() { return status; }
    public WorldRunStatus assignWorker(WorldProcessIdentity worker, HostAdapterRequest request, WorldRunStatus expected) {
        if (expected != WorldRunLifecycle.current || !(expected.getState().getPayload() instanceof WorldRunPhase.OfflineIdle))
            throw new IllegalStateException("fixture 旧stateまたは二重登録");
        WorldProcessIdentities.INSTANCE.requireAlive(worker);
        lastRequest = request;
        WorldRunLifecycle.assigns++;
        WorldRunLifecycle.current = WorldRunLifecycle.assigned(worker, request.getRequestId(), "e".repeat(64));
        if (WorldRunLifecycle.failAssignAfterPublish) throw new IllegalStateException("fixture Assigned公開後失敗");
        status = WorldRunLifecycle.current;
        return status;
    }
    public HostAdapterResponse complete(WorldOfflineCandidate candidate) {
        WorldRunLifecycle.completeCalls++;
        WorldRunStatus expected = WorldRunLifecycle.current;
        if (!(expected.getState().getPayload() instanceof WorldRunPhase.OfflineSavedPendingExit saved) ||
                !(status.getState().getPayload() instanceof WorldRunPhase.OfflineAssigned assigned) ||
                !saved.getWriter().equals(assigned.getWriter()) ||
                !candidate.equals(new WorldOfflineCandidate(expected.getSha256(), saved.getReceiptSha256(),
                        expected.getState().getWorldUuid(), expected.getState().getRunUuid(), expected.getState().getStateRevision())))
            throw new IllegalStateException("fixture complete binding不一致");
        if (!saved.getWriter().getOwner().equals(WorldProcessIdentities.INSTANCE.current()))
            throw new IllegalStateException("fixture C本人不一致");
        WorldProcessIdentities.INSTANCE.requireAlive(saved.getWriter().getManagement().getController());
        WorldProcessIdentities.INSTANCE.requireExited(saved.getWorker());
        WorldRunStatus receipt = WorldRunLifecycle.receiptPending;
        WorldRunPhase.OfflinePending pending = (WorldRunPhase.OfflinePending) receipt.getState().getPayload();
        if (!receipt.getState().getWorldUuid().equals(expected.getState().getWorldUuid()) ||
                !receipt.getState().getRunUuid().equals(expected.getState().getRunUuid()) ||
                receipt.getState().getStateRevision() != expected.getState().getStateRevision()-1 ||
                !pending.getWorker().equals(saved.getWorker()) ||
                !expected.getState().getGeneration().equals(saved.getWriter().getManagement().getGeneration()) ||
                WorldRunLifecycle.dbOpen || !WorldRunLifecycle.receiptMatches || !WorldRunLifecycle.sealMatches)
            throw new IllegalStateException("fixture DBclose/receipt/seal不一致");
        WorldRunLifecycle.lastCandidate = candidate;
        WorldRunLifecycle.completes++;
        WorldRunLifecycle.current = WorldRunLifecycle.advance("8", saved.getWriter().getSessionId(),
                new WorldRunPhase.OfflineIdle(saved.getWriter(), WorldRunLifecycle.RECEIPT_SEAL, "7".repeat(64)));
        if (WorldRunLifecycle.failCompleteAfterPublish) throw new IllegalStateException("fixture Idle公開後失敗");
        status = WorldRunLifecycle.current;
        return WorldRunLifecycle.completedResponse;
    }
    public void close() {
        if (status != WorldRunLifecycle.current || !(getStatus().getState().getPayload() instanceof WorldRunPhase.OfflineIdle))
            throw new IllegalStateException("fixture 未完了Assignedをcloseできません");
        WorldRunLifecycle.closes++;
        if (WorldRunLifecycle.failClose) throw new IllegalStateException("fixture close不明");
        WorldRunLifecycle.current = WorldRunLifecycle.ready(status.getState().getStateRevision()+1);
        if (WorldRunLifecycle.failCloseAfterPublish) throw new IllegalStateException("fixture 公開後応答喪失");
        status = WorldRunLifecycle.current;
    }
}
