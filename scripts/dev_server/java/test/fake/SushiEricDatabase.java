package io.github.sushiericworkspace.sushiericdatamodel.database;

import io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldRunLifecycle;
import java.nio.file.Path;

/** native DBを開かない、W所有接続のfixture。 */
public final class SushiEricDatabase implements AutoCloseable {
    public WorldShopDefinitions shopDefinitions(Path shop) {
        if (!WorldRunLifecycle.dbOpen) throw new IllegalStateException("fixture DB未open");
        WorldRunLifecycle.observedShop=shop;
        return new WorldShopDefinitions();
    }
    public void close() {
        WorldRunLifecycle.dbCloses++;
        WorldRunLifecycle.dbOpen=false;
        if (WorldRunLifecycle.failDbClose) throw new IllegalStateException("fixture DBclose後失敗");
    }
}
