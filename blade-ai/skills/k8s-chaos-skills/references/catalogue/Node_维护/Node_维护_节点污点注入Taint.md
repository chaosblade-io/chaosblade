**用例名称** 节点污点注入Taint 导致 Node_维护

**故障现象**：
1. 节点被添加 NoExecute 类型 Taint，不容忍该 Taint 的 Pod 被驱逐
2. 新 Pod 无法调度到该节点（除非声明了对应 Toleration）
3. 被驱逐 Pod 在其他节点重建
4. 模拟节点故障标记/隔离场景

**资源准备**：
1. 确认目标节点上有业务 Pod 运行，且 Pod 未声明通配 Toleration
2. 确认集群中其他节点有足够资源接纳被驱逐的 Pod
3. 确认目标节点名称（通过 `kubectl get nodes` 获取）

**演练步骤**：
1. 确认目标节点当前 Taints 和运行的 Pod：
   ```bash
   kubectl describe node <node-name> | grep -A 5 Taints
   kubectl get pods --field-selector spec.nodeName=<node-name> -n <namespace> -o wide
   ```
2. 为节点添加 NoExecute 类型 Taint（将驱逐不容忍该 Taint 的已有 Pod）：
   ```bash
   kubectl taint nodes <node-name> chaos-drill=true:NoExecute
   ```
   说明：
   - `NoExecute`：驱逐所有不容忍该 Taint 的已有 Pod（激进模式）
   - `NoSchedule`：仅阻止新 Pod 调度，不驱逐已有 Pod（温和模式）
   - `PreferNoSchedule`：尽量不调度，软约束
3. 观察节点上 Pod 的驱逐行为

**注入验证**：
1. 执行 `kubectl describe node <node-name>` 确认 Taints 字段包含 `chaos-drill=true:NoExecute`
2. 执行 `kubectl get pods --field-selector spec.nodeName=<node-name> -n <namespace>`，确认不容忍该 Taint 的 Pod 已被驱逐
3. 执行 `kubectl get pods -n <namespace> -l <label-selector> -o wide`，确认被驱逐 Pod 已在其他节点重建
4. 执行 `kubectl get events -n <namespace> --sort-by='.lastTimestamp'`，确认出现 Taint-related eviction 事件

**注入恢复**：
1. 移除添加的 Taint（注意末尾的 `-` 表示删除）：
   ```bash
   kubectl taint nodes <node-name> chaos-drill=true:NoExecute-
   ```
2. 等待调度器重新平衡工作负载

**恢复验证**：
1. 执行 `kubectl describe node <node-name>` 确认 Taints 字段中 `chaos-drill=true:NoExecute` 已移除
2. 执行 `kubectl get nodes`，确认节点状态正常（Ready，无异常 Condition）
3. 确认业务 Pod 全部 Running 且 Ready

**基准事实**：
- **根因**：节点被添加 NoExecute 类型 Taint，Kubernetes 驱逐所有不容忍该 Taint 的 Pod，模拟节点故障隔离场景
- **必现现象**：节点 Taints 含 `chaos-drill=true:NoExecute`；不容忍该 Taint 的 Pod 被驱逐；Events 显示 Taint-based eviction
