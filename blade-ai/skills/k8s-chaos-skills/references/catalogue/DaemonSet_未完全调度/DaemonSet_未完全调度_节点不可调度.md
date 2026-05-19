**用例名称** 节点不可调度 导致 DaemonSet_未完全调度

**故障现象**：
1. DaemonSet 的 Ready 副本数小于期望副本数
2. 部分节点上没有运行 DaemonSet Pod
3. `kubectl get daemonset` 显示 DESIRED 与 READY 数量不一致

**资源准备**：
1. 确认 DaemonSet 应用 A 已正常运行，且所有节点均有副本
2. 确认监控系统可观测 DaemonSet 副本状态

**演练步骤**：
1. 选取一个运行 DaemonSet Pod 的节点
2. 使用 kubectl 将该节点标记为不可调度（cordon）：`kubectl cordon <node>`
3. 给该节点添加一个 DaemonSet 未配置容忍的自定义污点：`kubectl taint nodes <node> chaos-drill/unschedulable=true:NoSchedule`
4. 删除该节点上的 DaemonSet Pod，观察 Pod 是否被重建
5. 观察 DaemonSet 副本数变化

**注入验证**：
1. 执行 `kubectl get nodes`，确认目标节点标记为 SchedulingDisabled
2. 执行 `kubectl describe node <node>`，确认自定义污点 `chaos-drill/unschedulable=true:NoSchedule` 存在
3. 执行 `kubectl get daemonset`，确认 DESIRED 与 READY 数量不一致
4. 确认目标节点上的 DaemonSet Pod 删除后无法被重建

**注入恢复**：
1. 使用 kubectl 取消节点不可调度标记（uncordon）：`kubectl uncordon <node>`
2. 移除自定义污点：`kubectl taint nodes <node> chaos-drill/unschedulable=true:NoSchedule-`
3. 等待 DaemonSet Pod 在该节点重建

**恢复验证**：
1. 执行 `kubectl get nodes`，确认目标节点恢复为可调度状态
2. 执行 `kubectl describe node <node>`，确认自定义污点已被移除
3. 执行 `kubectl get daemonset`，确认 DESIRED 与 READY 数量一致
4. 确认目标节点上 DaemonSet Pod 已正常运行

**注意事项**：
- Kubernetes ≥ 1.12 的 DaemonSet 默认容忍 `node.kubernetes.io/unschedulable` 污点，因此 `kubectl cordon` 不会阻止 DaemonSet Pod 调度
- 必须添加自定义污点（DaemonSet 未配置容忍的），才能有效阻止 Pod 调度
- 恢复时需一并移除自定义污点，否则 Pod 仍无法调度

**基准事实**：
- **根因**：节点被标记为不可调度（cordon）且存在 DaemonSet 未容忍的自定义污点，导致 DaemonSet 无法在该节点创建 Pod，副本数小于期望数
- **必现现象**：DaemonSet DESIRED 与 READY 不一致；节点 SchedulingDisabled；节点存在自定义污点；节点上缺少 DaemonSet Pod
