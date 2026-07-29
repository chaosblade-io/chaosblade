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

> ⚠️ **自断链路判读**：若通过 exec/kubectl-native 方式断网注入，注入命令自身会超时（如 task timed out after 10s）——这是预期成功信号，不要重试/换镜像；立即改从集群侧 `kubectl get nodes` 验证，拿到 NotReady + 心跳停止即可判定并收敛，勿反复探入被隔离节点。
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

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择当前集群已验证可拉取且包含 `chroot`/`sh` 的 debug 镜像；宿主机需包含 `iptables` 和 `systemd`。宿主机变更必须使用 `--profile=sysadmin`；禁止使用 `-it`、禁止硬编码 debug Pod 名、不要假定 Pod 位于 `default`。

注入命令：
```bash
# 分两步执行：先创建并等待 debug Pod Ready，再用工具返回的实际 Pod 名/命名空间执行 exec。
# ⚠️ 全量/控制面 DROP 会切断 exec 依赖的通道，必须先用 systemd-run 武装定时恢复，再下 DROP。

# 方案 1：仅屏蔽与 API Server 的通信（保留 SSH，恢复通道不断）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- sleep 900
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-nodedown sh -c "
    iptables -D INPUT -s <api-server-ip> -j DROP;
    iptables -D OUTPUT -d <api-server-ip> -j DROP" &&
  iptables -I INPUT -s <api-server-ip> -j DROP &&
  iptables -I OUTPUT -d <api-server-ip> -j DROP
'

# 方案 2：全量断网（更彻底，模拟真实宕机）——必须内置 systemd 定时自恢复
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- sleep 900
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-nodedown-full sh -c "iptables -D INPUT -j DROP; iptables -D OUTPUT -j DROP" &&
  iptables -I INPUT -j DROP && iptables -I OUTPUT -j DROP
'
```

恢复命令：

主恢复路径是注入时登记的 systemd 定时器，到期由宿主机 PID 1 自动执行 `iptables -D`，Agent 无需干预，也无需保持到该节点的连接。

**提前恢复必须人工带外执行 —— Agent 不执行下面的命令。** 注入切断的正是 kubectl 到该节点的路径，所以任何经集群 API 的恢复方式（`kubectl exec` / `kubectl debug node`）此刻都不可达。若确需提前恢复，请通过 SSH / 控制台 / IPMI 手动执行：

```text
# 方案 2（全量断网）：
ssh root@<node-ip> 'iptables -D INPUT -j DROP; iptables -D OUTPUT -j DROP'
# 方案 1（精确屏蔽 API Server）：
ssh root@<node-ip> 'iptables -D INPUT -s <api-server-ip> -j DROP; iptables -D OUTPUT -d <api-server-ip> -j DROP'
```

注意事项：
- ⚠️ 全量断网后 kubectl 无法连接该节点，必须依赖内置 systemd 定时自恢复，或通过 SSH/控制台/IPMI 等带外通道恢复
- 禁止使用无自恢复机制的全量 DROP 方案（可能导致节点永久失联）
- systemd-run 创建的 transient timer 由宿主机 systemd(PID 1) 管理，debug Pod 被删除也不影响恢复
- 恢复链使用 `;` 而非 `&&`，保证每条 iptables -D 都被尝试（某条规则不存在也不中断后续）
- 建议超时设置 60-600 秒，根据 pod-eviction-timeout 与演练目标调整
