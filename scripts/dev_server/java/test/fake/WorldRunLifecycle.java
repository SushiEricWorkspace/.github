package io.github.sushiericworkspace.sushiericdatamodel.database.run;

import java.nio.file.Path;
import java.util.UUID;

/** DBもrun-stateも作らない、作用境界と失敗注入だけのfixture。 */
public final class WorldRunLifecycle {
    public static int opens;
    public static int closes;
    public static boolean failOpen;
    public static boolean failClose;
    public static final String ID = "00000000-0000-4000-8000-000000000001";
    public WorldRunLifecycle(Path world, Path shop, UUID uuid, int timeout) {}
    public WorldRunStatus readStatus() { return status(0); }
    public WorldOfflineWriterSession openManagedOfflineWriter(WorldRunStatus status, WorldOfflineOwnerBinding binding) {
        opens++;
        if (failOpen) throw new IllegalStateException("fixture 公開後応答喪失");
        return new WorldOfflineWriterSession();
    }
    public static WorldRunStatus status(long revision) {
        return new WorldRunStatus("a".repeat(64), new WorldRunState(1, ID, ID, ID, revision,
                new WorldRunPaths("fixture", "fixture-db", "fixture-shop"), "a".repeat(64), null,
                new WorldRunPhase.Ready("a".repeat(64), "a".repeat(64))));
    }
}
