**用例名称** 节点宕机 导致 Node_不可用

**故障现象**：
1. 节点状态变为 NotReady
2. 节点上所有 Pod 无法访问
3. kubelet 停止上报节点状态，NodeStatus 中 LastHeartbeatTime 停止更新
4. Pod 在其他节点上被重建

**资源准备**：
1. 确认应用 A 已正常运行，且有多个副本分布在不同节点
2. 确认监控系统可观测节点状态和 Pod 状态
3. 探测确认目标应用 Pod 的 NoExecute tolerationSeconds（ACK 默认 300s）——该值决定驱逐时序，是 duration 窗口下限推导的输入；与 kubelet 停摆类 case 相反，本 case 的窗口语义是**必须大于** tolerationSeconds（要观察驱逐/重建判据），不得为缩短演练而选自定义长容忍的应用
   驱逐波及面甄别一轮探测（勿用 `pods -A -o wide` 宽查询——宽列长输出经执行通道回传可能被截断丢失 tolerations 信息；下述 jsonpath 单节点紧凑形态每 Pod 一行四要素，回传完整）：
   ```bash
   kubectl get pods -A --field-selector spec.nodeName=<node-name> -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}  hostNet={.spec.hostNetwork}  owner={.metadata.ownerReferences[0].kind}  tol={.spec.tolerations}{"\n"}{end}'
   # 输出形态（tol 为 Go 语法表示）：
   # kube-system/kube-proxy-abcde  hostNet=True  owner=DaemonSet  tol=[map[effect:NoExecute key:node.kubernetes.io/not-ready operator:Exists]]
   ```
   甄别锚点：只看 `tol` 中 effect=NoExecute 的项——tolerationSeconds 缺省（nil）= 永久免疫（owner=DaemonSet 的 Pod 由控制器自动注入此类容忍）；有数值 = 驱逐倒计时秒数；key 限定且不匹配 node.kubernetes.io/not-ready / unreachable 标准键的容忍项等同无此项；NoSchedule/PreferNoSchedule 项不参与驱逐判定。消费口径：目标应用与同节点全部负载一并入表，预期驱逐名单与免疫名单一次定案（勿逐 Pod 查询），是窗口下限推导与爆炸半径声明的直接输入

**演练步骤**：
1. 定位运行应用 A 的节点
2. 使用 chaosblade 对该节点注入网络完全丢包（node-network drop 即全量丢包，不需要 --percent），并设置 `--timeout <duration>`（到期自动恢复），模拟节点与集群失联的宕机场景
3. 观察节点状态和 Pod 调度行为变化

**注入验证**：

> ⚠️ **自断链路判读**：若通过 exec/kubectl-native 方式断网注入，注入命令自身会超时（如 task timed out after 10s）——这是预期成功信号，不要重试/换镜像；立即改从集群侧 `kubectl get nodes` 验证，拿到 NotReady + 心跳停止即可判定并收敛，勿反复探入被隔离节点。blade 方式（operator/CRD 链路）同理有回执形态风险：全量丢包后 tool Pod 与 operator 失联，实验状态上报中断——create 可能挂起/超时或停在中间态；**回执异常 + 下方效果判据成立 = 注入已成功**（效果即证据），勿据回执重发。
1. 轮询 Lease 心跳（即时主证，秒级可见冻结——kubelet 断网后 ~10s 即不再续约）：
   ```bash
   kubectl get lease <node-name> -n kube-node-lease -o jsonpath={.spec.renewTime}
   ```
   确认 renewTime 停止更新（正常约每 10s 更新一次）
2. 执行 `kubectl get nodes`，确认目标节点状态变为 NotReady——NotReady 需心跳停止满 node-monitor-grace-period（默认 40s）后才出现，注入后 40s 内仍 Ready 是预期中间态而非失败；NodeStatus `Ready.lastHeartbeatTime` 停更为粗粒度佐证——注意其刷新节奏是**状态变化时/约 5min**（nodeStatusReportFrequency），非心跳镜像，只作互证不作时序判据
3. 确认节点上应用 A 的 Pod 在其他节点上被重建（驱逐需等满 tolerationSeconds，见资源准备第 3 条）
4. 确认应用 A 的服务整体仍可访问（多副本场景）

> **verify 负证据事实来源（预写核对清单——只列证据来源，驳回与否由 verifier 独立裁决）**：verify 采样时点晚于设计内自恢复时刻，以下「看似反证」按来源逐项核对而非重新推导——①节点已回 Ready / Lease 正在推进：核对注入窗口期时间戳工件（NodeNotReady 事件 + Lease 冻结期的两次以上采样记录 + 驱逐事件链）是否证明 freeze→resume 转变发生过；②注入回执超时/Error：见上方自断链路判读条款（预期成功信号，非反证）；③lastHeartbeatTime 观感过期：见判据 2 的 nodeStatusReportFrequency 语义（只作互证不作时序判据）；④同节点其他 Pod（如常驻 DS 类）未驱逐：核对其 tolerations 形态（永久容忍 nil tolerationSeconds 与演练靶 0 容忍的对照）——未驱逐可为各自容忍度的预期行为；⑤NodeRepair* 事件存在（平台修复替代解释）：核对 NodeRepairStart 与 NodeNotReady 的时间先后（介入严格晚于故障发生 = 果非因）+ 基线期事件快照是否为空（注入前无既有修复轮次）；⑥timer fire 无直接回执：核对 Lease 恢复推进时刻与武装时刻 + 窗口值的吻合度（fire 即网络恢复，Lease 推进是其间接直证）。

**注入恢复**：
1. 主恢复路径（且全量丢包形态下**唯一** Agent 可依赖路径）：等待 chaosblade 实验自动超时恢复（`--timeout <duration>`）——超时定时器由节点本地 chaosblade 进程持有，不依赖网络、不受宿主 daemon-reload 重置（相对手段 2 的 systemd timer 的决定性抗干扰优势）
2. ⚠️ **`blade destroy` 兜底在全量丢包形态下不可达**：destroy 命令虽经 operator/CRD 链路秒回（operator 接受请求），但销毁指令须下发到目标节点上的 tool Pod 才执行——全量丢包下该下发链路正是被切断的对象，指令永远到不了执行方。destroy 兜底仅对部分丢包/其他 node 级故障（如 kubelet 停摆、网络正常）有效
3. 全量丢包下若定时自恢复失效（超时后节点仍未恢复），Agent 无集群内恢复手段——须 SSH/控制台/IPMI 人工带外恢复（见注意事项）；因此 `--timeout` 的设定本身是安全红线，必须确保定时器武装成功（blade create 回执 Success 或效果判据成立）后才算窗口受控

**恢复验证**：
1. 执行 `kubectl get nodes`，确认目标节点恢复 Ready
2. 确认 LastHeartbeatTime 恢复更新
3. 确认应用 A 的 Pod 恢复正常运行

**基准事实**：
- **根因**：节点网络完全中断，导致 kubelet 无法与 API server 通信，停止上报节点状态
- **必现现象**：节点 NotReady；LastHeartbeatTime 停止更新；Pod 在其他节点被重建
- **环境事实（外部节点自愈对断网形态的适配）**：部分集群存在平台侧节点自愈通道（见同目录 kubelet停止 case）。断网形态：NotReady 后 **约 217s** NodeRepairStart/NodePoolRepairStart 介入（与 kubelet 停摆形态的 257-292s 同量级）——诊断是**症状级**而非根因级（「Kubelet stopped posting node status + healthz connection refused」，修复不了网络层根因）；若演练窗口足够短（timer fire 早于修复介入），修复动作落地时故障已清除，事件链收尾为 NodeRepairSucceed（no-op 修复，fire 后约 20s 介入即此形态）；另 300s 默认容忍的 Pod 在驱逐倒计时中遇节点恢复会被 `TaintManagerEviction Cancelling deletion` 取消驱逐。风险推演不变：多轮修复或升级动作（drain/重置）的具体策略仍未知，窗口设计须按「自愈介入前完成恢复」校准（见下方短容忍加速形态）
- **短容忍加速形态**：演练靶若配双 NoExecute taint `tolerationSeconds: 0`，驱逐判据时序 = NotReady taint 时刻（taint→eviction 约 5s、evict→重建 Running 约 15s），全判据链 ~110s 内达成——恢复窗口可压缩至 240s，**显著小于外部自愈介入周期（~217-292s）**，从设计上规避 transient timer 被周期性 daemon-reload 重置基准的结构性风险（timer 在自愈介入前已 fire 完成使命，fire 先于 NodeRepairStart 约 20s）；生产默认 300s 容忍场景的 420s 窗口下限推导仍适用，但须直面该结构性风险——两难时优先短容忍形态

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。若前置探测已确证 ChaosBlade 在当前集群判死（如 operator/tool Pod 长期 ImagePullBackOff 等不可用形态），planning 直接引用判死结论走本路径，勿重复完整 probe-verified 推导链（时效核验单命令轻探即可）。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择当前集群已验证可拉取且包含 `chroot`/`sh` 的 debug 镜像；宿主机需包含 `iptables` 和 `systemd`。宿主机变更必须使用 `--profile=sysadmin`；禁止使用 `-it`、禁止硬编码 debug Pod 名、不要假定 Pod 位于 `default`。

注入命令：
```bash
# 分两步执行：先创建并等待 debug Pod Ready，再用工具返回的实际 Pod 名/命名空间执行 exec。
# ⚠️ 全量/控制面 DROP 会切断 exec 依赖的通道，必须先用 systemd-run 武装定时恢复，再下 DROP。

# 方案 1：仅屏蔽与 API Server 的通信（保留 SSH，恢复通道不断）
# ⚠️ <api-server-ip> 必须按集群实际 endpoint 全量展开：apiserver 前有 SLB/
#    多副本时是多个 VIP/IP（`kubectl get endpoints kubernetes` 可查）——每个 IP
#    各需 INPUT/OUTPUT 两条规则；漏掉任一 IP 节点仍可达 apiserver，注入不生效。
# ⚠️ <recovery-seconds> 窗口下限 ≈ 420s：完整判据链需要 NotReady 判定
#    （~40–50s）+ tolerationSeconds 默认 300s（Pod 开始驱逐）+ 驱逐/重建
#    观察与验证调用余量；窗口不足会在看到 Pod 重建前自恢复，判据链被打断。
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- sleep <duration>
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-nodedown sh -c "
    iptables -D INPUT -s <api-server-ip> -j DROP;
    iptables -D OUTPUT -d <api-server-ip> -j DROP" &&
  iptables -I INPUT -s <api-server-ip> -j DROP &&
  iptables -I OUTPUT -d <api-server-ip> -j DROP
'
# 示例（SLB 双 VIP 10.0.0.138 / 10.0.2.200 → 4 条规则，恢复链 ; 串联保证每条 -D 都尝试）：
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  systemd-run --on-active=480s --unit=blade-restore-nodedown sh -c "
    iptables -D INPUT -s 10.0.0.138 -j DROP; iptables -D OUTPUT -d 10.0.0.138 -j DROP;
    iptables -D INPUT -s 10.0.2.200 -j DROP; iptables -D OUTPUT -d 10.0.2.200 -j DROP" &&
  { iptables -I INPUT -s 10.0.0.138 -j DROP; iptables -I OUTPUT -d 10.0.0.138 -j DROP;
    iptables -I INPUT -s 10.0.2.200 -j DROP; iptables -I OUTPUT -d 10.0.2.200 -j DROP; echo INJECTED; }
'

# 方案 2：全量断网（更彻底，模拟真实宕机）——必须内置 systemd 定时自恢复
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- sleep <duration>
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-nodedown-full sh -c "iptables -D INPUT -j DROP; iptables -D OUTPUT -j DROP" &&
  iptables -I INPUT -j DROP && iptables -I OUTPUT -j DROP
'
```
倒计时从武装时刻起算：systemd-run 武装与 DROP 注入在同一载荷内 && 串联原子紧邻（先武装是防御性顺序）；注入后 exec 通道已断，武装后发生任何修复需重武装时——方案1（SSH 保留）走带外 SSH：`ssh root@<node-ip> 'systemctl stop blade-restore-nodedown.timer 2>/dev/null; systemctl stop blade-restore-nodedown.service 2>/dev/null; systemctl reset-failed blade-restore-nodedown.service'` 再重跑 systemd-run 武装命令；方案2 全量断网后一切通道丢失，无重武装手段，须接受剩余窗口被侵蚀或经控制台/IPMI 提前恢复（见 SKILL.md 安全红线「故障窗口完整」）

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
- **爆炸半径 schema 定案（planning 填表免推导）**：手段 2 方案 1 的 `blast_radius_scope = target-only`——scope 以 **mutation 写入集边界**为定义（写入仅目标节点宿主 iptables + 一个 transient timer，即 target-only）；consequence 影响面跨 ns（同节点全部 Pod 与 apiserver 通信中断）**不改变 scope 取值**，写入 `blast_radius_detail` 注明即可，勿因 consequence 抬档到 namespace-wide/cluster-wide
- 手段 2 单元名为固定名（无 `$$` 后缀——wiz 通道会把 `$$` 折叠为字面 `$`，见同目录 kubelet停止 case 的 `$$` 通道折叠条目）；**规划期须做单元名冲突预检**（宿主侧 `systemctl list-timers` / `list-units` 查 blade-restore-nodedown* 无存量——固定名重发即撞名）
- **timer/service 残留清理义务（守卫兼容形态）**：演练收尾时（节点网络恢复后——本 case 因果链与 kubelet 停摆类相反：fire/timer 到期即网络恢复本身，**fire 前无法向节点投递任何载体**）确认 blade-restore-nodedown* timer/service 无残留**是注入方 execute 计划的收尾 mutation step 义务**；⚠️ 守卫词汇约束：`systemctl` 是 target_guard 的 host-escape 禁词（stop/reset-failed 清理路径对 Agent 不可用）——清理与证明须用守卫兼容路径：①one-shot transient timer fire 后自毁，通常无需 teardown；②残留证明用只读探针（单语句 `chroot /host sh -c 'iptables -S | grep <vip>'` 与全只读复合链 `echo ...; iptables -S | grep ...; ls ...` 均可——全只读段的管道是内核管道非 mutation；含 mutation 动词（-I/-A/-D 等）的形态仍拒并要求配对逆操作；重定向/展开等零 mutation 但无法静态证明只读的复合形态仍拒并附单语句拆分指引）或文件系统列举（`ls /run/systemd/transient/`）；③逻辑等价证明——Lease renewTime 活跃推进 + KubeletReady 即「任何 apiserver VIP DROP 残留不可能存在」的结论性证据；④若确有 failed 残留须人工带外处置（禁词绕行不可由 Agent 执行）；recover 对该清理幂等兜底；恢复验证的裁决判据不变（仍须含残留清零确认——以上任一守卫兼容路径的零残留证明均可采信）
- 演练全程（注入前、窗口内、恢复后）**监控 NodeRepair* 事件链**是注入方 execute 计划的观测义务（外部自愈介入轮次、Action 演变——升级类动作即爆炸半径预警，见基准事实环境事实条目）
- 恢复链使用 `;` 而非 `&&`，保证每条 iptables -D 都被尝试（某条规则不存在也不中断后续）
- 建议超时设置：timer 窗口（`--on-active`）下限 ≈ 420s（NotReady 判定 ~40–50s +
  tolerationSeconds 默认 300s + 驱逐/重建观察与验证余量），上限根据演练目标调整；
  窗口不足会在 Pod 重建前自恢复，判据链被打断；**手段 2 须同时满足 recovery-seconds < 外部自愈修复周期（约 257-292s）以防 timer 被周期性 daemon-reload 反复重置**——与 420s 下限矛盾时优先手段 1
