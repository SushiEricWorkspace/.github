package io.github.sushiericworkspace.sushiericdatamodel.database.run;

/** native問い合わせを行わない、controller protocol専用fixture。 */
public final class WorldProcessIdentities {
    public static final WorldProcessIdentities INSTANCE = new WorldProcessIdentities();
    public static boolean parentAlive = true;
    public WorldProcessIdentity current() { return identity(ProcessHandle.current().pid()); }
    public void requireAlive(WorldProcessIdentity identity) {
        if (!parentAlive) throw new IllegalStateException("fixture P死亡");
    }
    public static WorldProcessIdentity identity(long pid) {
        return new WorldProcessIdentity("a".repeat(64), "fixture-boot", pid, "fixture-start");
    }
}
