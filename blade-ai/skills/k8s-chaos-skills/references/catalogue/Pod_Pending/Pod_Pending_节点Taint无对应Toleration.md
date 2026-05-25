**用例名称** 节点Taint无对应Toleration 导致 Pod_Pending

**故障现象**：
1. Pod 状态为 Pending，无法被调度到任何节点
2. Pod Events 中显示 `0/N nodes are available: N node(s) had untolerated taint`
3. 所有可用节点均带有 Pod 无法容忍的污点

**RCA症状**：
1. Pod 状态为 Pending，无法被调度到任何节点
2. Pod Events 中显示 `0/N nodes are available: N node(s) had untolerated taint`
（以上为kubectl直接可观测的现象，不包含诊断结论）

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群中有多个可调度节点

**演练步骤**：
1. 记录当前所有节点的 taint 信息（用于恢复）
2. 使用 kubectl 给所有可调度节点添加自定义污点：`kubectl taint node <node> chaos-test=true:NoSchedule`
3. 删除应用 A 的一个 Pod，触发重建调度
4. 观察新 Pod 的调度状态

**注入验证**：
1. 执行 `kubectl get pods`，确认新 Pod 状态为 Pending
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 中显示 taint 相关的调度失败信息
3. 执行 `kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints`，确认所有节点均有无法容忍的 taint

**注入恢复**：
1. 移除所有节点上添加的自定义污点：`kubectl taint node <node> chaos-test=true:NoSchedule-`
2. 等待 Pod 自动被调度

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态变为 Running
2. 确认节点 taint 已恢复到演练前状态

**基准事实**：
- **根因**：所有可用节点被标记了 Taint，而 Pod 未配置对应的 Toleration，导致调度器无法找到合适节点
- **必现现象**：Pod Pending；Events 显示 untolerated taint；所有节点带有不可容忍的污点
