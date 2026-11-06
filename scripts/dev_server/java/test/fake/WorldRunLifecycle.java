package io.github.sushiericworkspace.sushiericdatamodel.database.run;

import java.nio.file.Path;
import java.util.UUID;

/** DBもrun-stateも作らない、作用境界と失敗注入だけのfixture。 */
public final class WorldRunLifecycle {
    public static int opens;
    public static int closes;
    public static boolean failOpen;
    public static boolean failClose;
    public static boolean failCloseAfterPublish;
    public static WorldRunStatus current = initialReady();
    public static final String ID = "00000000-0000-4000-8000-000000000001";
    public static final String RUN = "00000000-0000-4000-8000-000000000002";
    public WorldRunLifecycle(Path world, Path shop, UUID uuid, int timeout) {}
    public WorldRunStatus readStatus() { return current; }
    public WorldOfflineWriterSession openManagedOfflineWriter(WorldRunStatus status, WorldOfflineOwnerBinding binding) {
        opens++;
        if (failOpen) throw new IllegalStateException("fixture 公開後応答喪失");
        current = idle(1);
        return new WorldOfflineWriterSession();
    }
    public static WorldRunStatus ready(long revision) {
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
    public static WorldRunStatus idle(long revision) {
        return new WorldRunStatus("b".repeat(64), new WorldRunState(1, ID, ID, RUN, revision,
                new WorldRunPaths("fixture", "fixture-db", "fixture-shop"), "a".repeat(64), "a".repeat(64),
                new WorldRunPhase.OfflineIdle(new WorldOfflineWriter("offline-session",
                        WorldProcessIdentities.identity(ProcessHandle.current().pid()), null),
                        "a".repeat(64), "b".repeat(64))));
    }
}
