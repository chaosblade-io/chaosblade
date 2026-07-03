**用例名称** 节点排空Drain 导致 Node_维护

**故障现象**：
1. 节点被标记为 SchedulingDisabled，不再接受新 Pod 调度
2. 节点上所有非 DaemonSet Pod 被安全驱逐
3. 被驱逐 Pod 在其他节点重建；如集群资源不足，部分 Pod 进入 Pending
4. 模拟节点维护/升级场景下的工作负载迁移

**资源准备**：
1. 确认目标节点上有业务 Pod 运行（非仅 DaemonSet Pod）
2. 确认集群中其他节点有足够资源接纳被驱逐的 Pod
3. 确认目标节点名称（通过 `kubectl get nodes` 获取）

**演练步骤**：
1. 确认目标节点当前运行的 Pod：
   ```bash
   kubectl get pods --all-namespaces --field-selector spec.nodeName=<node-name> -o wide
   ```
2. 标记节点为不可调度：
   ```bash
   kubectl cordon <node-name>
   ```
3. 排空节点上所有 Pod（安全驱逐）：
   ```bash
   kubectl drain <node-name> \
     --ignore-daemonsets \
     --delete-emptydir-data \
     --grace-period=30 \
     --timeout=120s
   ```
4. 观察被驱逐 Pod 的重建情况

**注入验证**：
1. 执行 `kubectl get nodes`，确认目标节点状态为 `Ready,SchedulingDisabled`
2. 执行 `kubectl get pods --field-selector spec.nodeName=<node-name> --all-namespaces`，确认仅剩 DaemonSet Pod
3. 执行 `kubectl get pods -n <namespace> -l <label-selector> -o wide`，确认业务 Pod 已迁移到其他节点
4. 检查是否有 Pod 因资源不足进入 Pending：
   ```bash
   kubectl get pods --all-namespaces --field-selector status.phase=Pending
   ```

**注入恢复**：
1. 恢复节点为可调度状态：
   ```bash
   kubectl uncordon <node-name>
   ```
2. 等待调度器将 Pending Pod（如有）重新调度

**恢复验证**：
1. 执行 `kubectl get nodes`，确认目标节点状态恢复为 `Ready`（无 SchedulingDisabled）
2. 执行 `kubectl get pods --all-namespaces --field-selector status.phase=Pending`，确认无 Pending Pod
3. 确认业务 Pod 全部 Running 且 Ready

**基准事实**：
- **根因**：节点被 cordon + drain 标记为不可调度并驱逐所有工作负载，模拟节点维护场景下的 Pod 迁移行为
- **必现现象**：节点状态为 SchedulingDisabled；非 DaemonSet Pod 被驱逐并在其他节点重建；drain 命令输出 evicting/evicted 信息
