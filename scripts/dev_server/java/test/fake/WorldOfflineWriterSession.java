package io.github.sushiericworkspace.sushiericdatamodel.database.run;

public final class WorldOfflineWriterSession {
    private boolean closed;
    public WorldRunStatus getStatus() { return WorldRunLifecycle.status(closed ? 2 : 1); }
    public void close() {
        WorldRunLifecycle.closes++;
        if (WorldRunLifecycle.failClose) throw new IllegalStateException("fixture close不明");
        closed = true;
    }
}
