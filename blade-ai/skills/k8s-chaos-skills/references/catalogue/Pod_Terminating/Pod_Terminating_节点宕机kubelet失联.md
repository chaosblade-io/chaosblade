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
2. 先对目标节点注入网络切断（chaosblade node-network drop 即全量丢包，不需要 --percent，设置 `--timeout <duration>`；手段2 为 iptables OUTPUT DROP dport 6443——见下方命令模板），模拟节点宕机导致 kubelet 失联
3. 注入生效后紧邻（≤60s）执行 `kubectl delete pod <pod> --wait=false` 触发 Terminating——**顺序必须注入先行（实测立法）**：删除指令经 kubelet watch 推送，事件传播 <1s，「删除先行」会被 kubelet 抢先完成清理（故障不出现）；注入先行使 kubelet 失联收不到删除事件，Pod 容器存活而对象卡 Terminating（正是本 case 故障现象：指令无法执行）。`--wait=false` 必须显式：kubelet 失联时 Pod 永不消失，默认等待会把 delete 命令挂死直至超时
4. 观察 Pod Terminating 状态持续时间

**注入验证**：

> ⚠️ **自断链路判读**：探测通过 exec 方式屏蔽 6443 后，注入命令**大概率正常返回**（不超时）
> ——exec 通道走 apiserver→kubelet:10250 的**入向连接**，不受 OUTPUT 链 dport 6443 的
> DROP 规则影响（被切断的只是 kubelet 主动发起的出向连接：watch/lease 上报）。
> 因此「exec 超时」既不是成功信号也不是失败信号；判定注入是否生效以集群侧
> `kubectl get nodes`（NotReady）+ `get pods`（Terminating）为准。
1. 执行 `kubectl get pods`，确认目标 Pod 状态为 Terminating 且长时间未消失
2. 执行 `kubectl get nodes`，确认目标节点状态为 NotReady——NotReady 需 kubelet 心跳停止满 node-monitor-grace-period（默认 40s）后才出现，注入后 40s 内仍 Ready 是预期中间态而非失败（宽限期内第 1 条的 Terminating 已可判）
3. 查看 Pod 详情，确认 deletionTimestamp 已设置但 Pod 未被实际清理

**注入恢复**：
1. 等待 chaosblade 实验自动超时恢复（`<duration>` 内）
2. 如超时后仍未恢复，通过 `blade destroy <UID>` 或重启节点强制恢复
3. kubelet 恢复后会自动清理 Terminating 状态的 Pod

**恢复验证**：
1. 执行 `kubectl get nodes`，确认目标节点恢复 Ready
2. 执行 `kubectl get pods`，确认 Terminating 的 Pod 已被清理
3. 确认应用 A 的新 Pod 在其他节点正常运行

**基准事实**：
- **根因**：节点宕机或 kubelet 失联，导致 API Server 下发的删除指令无法被执行，Pod 停留在 Terminating 状态
- **必现现象**：Pod 状态为 Terminating 且长时间不消失；节点 NotReady；deletionTimestamp 已设置

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令模拟节点宕机导致 kubelet 失联。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；debug 镜像需包含 `chroot`/`sh`；宿主机需包含 `iptables` 和 `systemd`

注入命令：
```bash
# 屏蔽节点与 API Server 的通信并启动 systemd 定时自恢复
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- sleep <duration>
# ⚠️ 关键顺序：先用 systemd-run 武装恢复（仅登记闹钟），再下 DROP——防御性实践：
# 若 DROP 与武装在同一条载荷内且武装排在后，一旦注入链路出现任何意外（如 kubelet 拦截、
# 载体异常），定时器可能未成功武装 → 永不恢复。先武装可将风险窗口归零。
# ✅ 注入后本条 exec 会正常返回（exit 0 + 武装确认回显）——exec 走 apiserver→kubelet:10250
#    入向连接，不受 OUTPUT dport 6443 DROP 影响，不会自断；判定注入生效用集群侧
#    `kubectl get nodes`（应 NotReady）与 `kubectl get pods`（应 Terminating）验证。
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-kubelet sh -c "iptables -D OUTPUT -p tcp --dport 6443 -j DROP" &&
  iptables -I OUTPUT -p tcp --dport 6443 -j DROP
'
```
倒计时从武装时刻起算：systemd-run 武装与 DROP 注入在同一载荷内 && 串联原子紧邻（先武装是防御性顺序）；本用例注入后 exec 通道仍可达（见上方结论），武装后发生任何修复需重武装时：`kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c 'systemctl stop blade-restore-kubelet.timer 2>/dev/null; systemctl stop blade-restore-kubelet.service 2>/dev/null; systemctl reset-failed blade-restore-kubelet.service'` 停旧定时器再重跑上方 systemd-run 武装命令（见 SKILL.md 安全红线「故障窗口完整」）

恢复命令：

主恢复路径是注入时登记的 systemd 定时器，到期由宿主机 PID 1 自动执行 `iptables -D`，Agent 无需干预，也无需保持到该节点的连接。

**提前恢复建议人工带外执行 —— Agent 优先不依赖集群 API。** 失联期间 `kubectl exec`/`kubectl debug node` **仍然可达**（exec 走 apiserver→kubelet:10250 入向连接，与被 DROP 的 kubelet 出向 6443 无关；DROP 规则在位时 exec echo 正常回传）——但该路径依赖 debug Pod 存活，且与注入共用同一链路；SSH 带外仍是最稳路径：

```text
ssh root@<node-ip> 'iptables -D OUTPUT -p tcp --dport 6443 -j DROP'
```

注意事项：
- 屏蔽 6443 端口后节点与控制面断开，kubectl 无法达节点，SSH 仍可达
- systemd-run 创建的 transient timer 由宿主机 PID 1 管理，debug Pod 被删除也不影响恢复
- 建议超时设置 30-120 秒（实测立法）：窗口上限的真实约束是**外部节点自愈通道竞态**——部分集群存在 NotReady 后 2-5 分钟自动介入修复的通道（本集群实测 217-292s），介入会把节点拉回 Ready、抢在注入方恢复之前，与演练恢复语义竞态；窗口设计须满足 NotReady 存续期显著短于介入线（120s 窗口时 NotReady 存续 ~80s）。超时上限公式 = 自愈介入线 − NotReady 宽限期（node-monitor-grace-period 默认 40s）− 恢复收敛余量
- 适用场景：测试当节点失联时 Pod 的 Terminating 状态会持续多久
