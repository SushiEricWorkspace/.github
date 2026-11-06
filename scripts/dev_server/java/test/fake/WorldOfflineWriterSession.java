package io.github.sushiericworkspace.sushiericdatamodel.database.run;

public final class WorldOfflineWriterSession {
    private boolean closed;
    public WorldRunStatus getStatus() { return closed ? WorldRunLifecycle.current : WorldRunLifecycle.idle(1); }
    public void close() {
        WorldRunLifecycle.closes++;
        if (WorldRunLifecycle.failClose) throw new IllegalStateException("fixture close不明");
        closed = true;
        WorldRunLifecycle.current = WorldRunLifecycle.ready(2);
        if (WorldRunLifecycle.failCloseAfterPublish) throw new IllegalStateException("fixture 公開後応答喪失");
    }
}
