package io.github.sushiericworkspace.sushiericdatamodel.database.run;

import io.github.sushiericworkspace.sushiericdatamodel.database.SushiEricDatabase;
import io.github.sushiericworkspace.sushiericdatamodel.database.host.*;

/** DB作用・close・候補公開の境界だけを表し、保存処理は実装しない。 */
public final class WorldOfflineOperation {
    private final WorldRunStatus status;
    WorldOfflineOperation(WorldRunStatus status) { this.status=status; }
    public SushiEricDatabase openDatabase(boolean create, int timeout) {
        if (create || status!=WorldRunLifecycle.current || WorldRunLifecycle.dbOpen)
            throw new IllegalStateException("fixture DB受付不一致");
        WorldRunLifecycle.checkWorkerActors();
        WorldRunLifecycle.dbOpens++;
        WorldRunLifecycle.dbOpen=true;
        return new SushiEricDatabase();
    }
    public WorldOfflineCandidate finish(HostAdapterResponse response) {
        if (status!=WorldRunLifecycle.current || WorldRunLifecycle.dbOpen || response.getStatus()==HostAdapterStatus.UNKNOWN ||
                !response.getRequestId().equals(WorldRunLifecycle.expectedRequest.getRequestId()))
            throw new IllegalStateException("fixture 未closeまたは結果不明");
        WorldRunLifecycle.checkWorkerActors();
        WorldRunPhase.OfflinePending pending=(WorldRunPhase.OfflinePending)status.getState().getPayload();
        WorldRunLifecycle.finishes++;
        WorldRunLifecycle.current=WorldRunLifecycle.advance("c",status.getState().getRunUuid(),
                new WorldRunPhase.OfflineSavedPendingExit(pending.getWriter(),pending.getWorker(),"1".repeat(64)));
        if (WorldRunLifecycle.failFinishAfterPublish) throw new IllegalStateException("fixture 候補公開後失敗");
        WorldRunStatus saved=WorldRunLifecycle.current;
        return new WorldOfflineCandidate(saved.getSha256(),"1".repeat(64),saved.getState().getWorldUuid(),
                saved.getState().getRunUuid(),saved.getState().getStateRevision());
    }
}
