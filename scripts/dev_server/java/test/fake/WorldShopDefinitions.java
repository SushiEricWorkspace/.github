package io.github.sushiericworkspace.sushiericdatamodel.database;

import io.github.sushiericworkspace.sushiericdatamodel.database.host.*;
import io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldRunLifecycle;
import io.github.sushiericworkspace.sushiericdatamodel.database.run.WorldProcessIdentities;
import kotlin.jvm.functions.Function0;
import java.util.List;

public final class WorldShopDefinitions {
    public HostAdapterResponse execute(HostAdapterRequest request, Function0<Boolean> cancelled) {
        if (!WorldRunLifecycle.dbOpen || !request.equals(WorldRunLifecycle.expectedRequest) || cancelled.invoke())
            throw new IllegalStateException("fixture 型付き要求不一致");
        WorldRunLifecycle.checkWorkerActors();
        WorldRunLifecycle.edits++;
        WorldRunLifecycle.lastEditRequest=request;
        if (WorldRunLifecycle.killParentAfterEdit) WorldProcessIdentities.parentAlive=false;
        if (WorldRunLifecycle.killControllerAfterEdit) WorldProcessIdentities.controllerAlive=false;
        HostAdapterStatus status=WorldRunLifecycle.unknownEdit?HostAdapterStatus.UNKNOWN:
                WorldRunLifecycle.rejectedEdit?HostAdapterStatus.REJECTED:HostAdapterStatus.SUCCESS;
        return new HostAdapterResponse(1,request.getRequestId(),status,
                status==HostAdapterStatus.SUCCESS?new HostAdapterResult.Edited(List.of(7L)):null,
                null,null,status==HostAdapterStatus.REJECTED);
    }
}
