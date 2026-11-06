package io.github.sushiericworkspace.sushiericdatamodel.database.run;

import io.github.sushiericworkspace.sushiericdatamodel.database.host.HostAdapterRequest;

public final class WorldOfflineWriterSession {
    public static HostAdapterRequest lastRequest;
    public WorldRunStatus getStatus() { return WorldRunLifecycle.current; }
    public WorldRunStatus assignWorker(WorldProcessIdentity worker, HostAdapterRequest request, WorldRunStatus expected) {
        if (expected != WorldRunLifecycle.current || !(expected.getState().getPayload() instanceof WorldRunPhase.OfflineIdle))
            throw new IllegalStateException("fixture 旧stateまたは二重登録");
        WorldProcessIdentities.INSTANCE.requireAlive(worker);
        lastRequest = request;
        WorldRunLifecycle.assigns++;
        WorldRunLifecycle.current = WorldRunLifecycle.assigned(worker, request.getRequestId(), "e".repeat(64));
        if (WorldRunLifecycle.failAssignAfterPublish) throw new IllegalStateException("fixture Assigned公開後失敗");
        return WorldRunLifecycle.current;
    }
    public void close() {
        if (!(getStatus().getState().getPayload() instanceof WorldRunPhase.OfflineIdle))
            throw new IllegalStateException("fixture 未完了Assignedをcloseできません");
        WorldRunLifecycle.closes++;
        if (WorldRunLifecycle.failClose) throw new IllegalStateException("fixture close不明");
        WorldRunLifecycle.current = WorldRunLifecycle.ready(2);
        if (WorldRunLifecycle.failCloseAfterPublish) throw new IllegalStateException("fixture 公開後応答喪失");
    }
}
