package io.github.sushiericworkspace.sushiericdatamodel.database.run;

/** native問い合わせを行わない、controller protocol専用fixture。 */
public final class WorldProcessIdentities {
    public static final WorldProcessIdentities INSTANCE = new WorldProcessIdentities();
    public static boolean parentAlive = true;
    public static boolean selfMatches = true;
    public static boolean workerAlive = true;
    public static boolean failAfterOpen;
    public static int parentChecks;
    public static final long WORKER = 9_999_999;
    public WorldProcessIdentity current() {
        WorldProcessIdentity current = identity(ProcessHandle.current().pid());
        return selfMatches ? current : identity(current.getPid() + 1);
    }
    public void requireAlive(WorldProcessIdentity identity) {
        WorldProcessIdentity actual = identity(ProcessHandle.current().parent().orElseThrow().pid());
        if (actual.getPid() == identity.getPid()) {
            parentChecks++;
            if (!parentAlive || !actual.equals(identity) || (failAfterOpen && parentChecks>2))
                throw new IllegalStateException("fixture P本人不一致");
        } else if (!workerAlive || !identity(WORKER).equals(identity)) {
            throw new IllegalStateException("fixture W本人不一致");
        }
    }
    public static WorldProcessIdentity identity(long pid) {
        return new WorldProcessIdentity("a".repeat(64), "fixture-boot", pid, "fixture-start");
    }
}
