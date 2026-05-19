**用例名称** 节点宕机 导致 Node_不可用

**故障现象**：
1. 节点状态变为 NotReady
2. 节点上所有 Pod 无法访问
3. kubelet 停止上报节点状态，NodeStatus 中 LastHeartbeatTime 停止更新
4. Pod 在其他节点上被重建

**资源准备**：
1. 确认应用 A 已正常运行，且有多个副本分布在不同节点
2. 确认监控系统可观测节点状态和 Pod 状态

**演练步骤**：
1. 定位运行应用 A 的节点
2. 使用 chaosblade 对该节点注入网络完全丢包（--percent 100），并设置 `--timeout 600`（600 秒后自动恢复），模拟节点与集群失联的宕机场景
3. 观察节点状态和 Pod 调度行为变化

**注入验证**：
1. 执行 `kubectl get nodes`，确认目标节点状态变为 NotReady
2. 查看 NodeStatus，确认 LastHeartbeatTime 停止更新
3. 确认节点上应用 A 的 Pod 在其他节点上被重建
4. 确认应用 A 的服务整体仍可访问（多副本场景）

**注入恢复**：
1. 等待 chaosblade 实验自动超时恢复（600 秒内），agent 本地定时器会自动清理网络规则
2. 如超时后仍未恢复，通过 `blade destroy <UID>` 或重启节点强制恢复

**恢复验证**：
1. 执行 `kubectl get nodes`，确认目标节点恢复 Ready
2. 确认 LastHeartbeatTime 恢复更新
3. 确认应用 A 的 Pod 恢复正常运行

**基准事实**：
- **根因**：节点网络完全中断，导致 kubelet 无法与 API server 通信，停止上报节点状态
- **必现现象**：节点 NotReady；LastHeartbeatTime 停止更新；Pod 在其他节点被重建
