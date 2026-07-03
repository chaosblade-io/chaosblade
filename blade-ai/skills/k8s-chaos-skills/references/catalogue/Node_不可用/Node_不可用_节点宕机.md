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
2. 使用 chaosblade 对该节点注入网络完全丢包（node-network drop 即全量丢包，不需要 --percent），并设置 `--timeout 600`（600 秒后自动恢复），模拟节点与集群失联的宕机场景
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

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；或可通过 SSH 访问目标节点

注入命令：
```bash
# 方案 1：kubectl debug node 阻断网络
kubectl debug node/<node-name> -it --image=alpine -- sh -c 'chroot /host iptables -A INPUT -s <api-server-ip> -j DROP && chroot /host iptables -A OUTPUT -d <api-server-ip> -j DROP'
# 方案 2：全量网络丢包（更彻底的模拟）
kubectl debug node/<node-name> -it --image=alpine -- sh -c 'chroot /host iptables -A INPUT -j DROP && chroot /host iptables -A OUTPUT -j DROP'
```

恢复命令：
```bash
# 精确删除注入的规则（需通过 SSH 或节点直接访问，因网络已断）
# 方案 1：通过 kubectl debug（仅当部分网络保留时可用）
kubectl debug node/<node-name> -it --image=alpine -- sh -c 'chroot /host iptables -D INPUT -j DROP && chroot /host iptables -D OUTPUT -j DROP'
# 方案 2：通过 SSH：
ssh <node-ip> 'iptables -D INPUT -j DROP && iptables -D OUTPUT -j DROP'
# 方案 3：控制台直接执行：
iptables -D INPUT -j DROP && iptables -D OUTPUT -j DROP
# 注意：全量丢包后 kubectl 无法连接该节点，必须通过带外通道恢复
```

注意事项：
- 全量丢包后无法通过 kubectl 远程恢复，必须通过 SSH/控制台/IPMI 等带外通道恢复
- 与 ChaosBlade 相比，缺少 `--timeout` 自动恢复机制，强烈建议配合 `at` 或 `sleep && iptables -F` 实现定时恢复
- ChaosBlade 的优势在于其 agent 本地定时器会在超时后自动清理网络规则，无需带外介入
