**用例名称** 节点宕机kubelet失联 导致 Pod_Terminating

**故障现象**：
1. Pod 状态长时间停留在 Terminating，无法完成删除
2. 节点状态变为 NotReady，kubelet 停止上报
3. API Server 已下发删除指令，但 kubelet 无法执行实际清理操作

**资源准备**：
1. 确认应用 A 已正常运行，至少有一个副本运行在目标节点上
2. 确认监控系统可观测节点状态和 Pod 状态

**演练步骤**：
1. 定位运行应用 A 的目标节点
2. 先通过 kubectl 删除应用 A 在目标节点上的 Pod（触发 Terminating）
3. 立即使用 chaosblade 对目标节点注入网络完全丢包（--percent 100），并设置 `--timeout 300`，模拟节点宕机导致 kubelet 失联
4. 观察 Pod Terminating 状态持续时间

**注入验证**：
1. 执行 `kubectl get pods`，确认目标 Pod 状态为 Terminating 且长时间未消失
2. 执行 `kubectl get nodes`，确认目标节点状态为 NotReady
3. 查看 Pod 详情，确认 deletionTimestamp 已设置但 Pod 未被实际清理

**注入恢复**：
1. 等待 chaosblade 实验自动超时恢复（300 秒内）
2. 如超时后仍未恢复，通过 `blade destroy <UID>` 或重启节点强制恢复
3. kubelet 恢复后会自动清理 Terminating 状态的 Pod

**恢复验证**：
1. 执行 `kubectl get nodes`，确认目标节点恢复 Ready
2. 执行 `kubectl get pods`，确认 Terminating 的 Pod 已被清理
3. 确认应用 A 的新 Pod 在其他节点正常运行

**基准事实**：
- **根因**：节点宕机或 kubelet 失联，导致 API Server 下发的删除指令无法被执行，Pod 停留在 Terminating 状态
- **必现现象**：Pod 状态为 Terminating 且长时间不消失；节点 NotReady；deletionTimestamp 已设置
