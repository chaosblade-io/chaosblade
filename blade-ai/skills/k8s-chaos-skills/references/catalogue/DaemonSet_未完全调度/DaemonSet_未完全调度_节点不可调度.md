---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 摘除自定义污点并解除 cordon），路由进 FaultDrill
# CR 通道；CRD 不可装时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：CR 通道本体（FaultDrill CR）
# 落 victim ns（P10 显式写入），scope 在受害者覆盖与同 ns secondary 网之外——
# 写集准入唯一路径 = 本条目；CR 名 = fd-<任务派生短哈希>（前缀与正文 CR 模板
# 同源默认值），走 name_prefix 动态形态；条目 ns 与本 case 演练窗口 ns 对齐。
mechanism_writes:
  # CR 通道本体（FaultDrill CR 落 victim ns——P10 显式写入；名 = fd-<任务派生
  # 短哈希>，前缀与正文 CR 模板同源默认值）：scope 在受害者覆盖与同 ns secondary
  # 网之外，写集准入唯一路径 = 本立法条目（guard 3.6 mechanism-entries 分支）
  - scope: faultdrill
    namespace: default
    name_prefix: "fd-"
---

**用例名称** 节点不可调度 导致 DaemonSet_未完全调度

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 摘除自定义污点并解除 cordon；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

```yaml
apiVersion: drill.blade-ai.io/v1alpha1   # 组名可配（faultdrill_crd_group）
kind: FaultDrill
metadata:
  name: fd-<任务派生短哈希>               # 前缀可配（faultdrill_name_prefix）；零演练签名词根
  namespace: <namespace>                  # 必须显式写入——见下方 P10 条款
spec:
  action: specPatch
  targetRef:
    kind: Node
    name: <node-name>
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: add
    path: /spec/taints/-
    value: {key: node.ops/maintenance, value: "true", effect: NoSchedule}
  - op: replace
    path: /spec/unschedulable
    value: true
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  # 基线空 → remove 整键；基线非空 → replace <基线完整数组>（严禁整组清空——误删集群固有污点）
  - op: remove
    path: /spec/taints
  - op: replace
    path: /spec/unschedulable
    value: false
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- cluster-scoped 目标（Node）：targetRef 不写 namespace；CR 本体落任务主 ns（metadata.namespace 仍必须显式写入）。
- 删除该节点上的 DS Pod（呈现「节点缺 Pod」的必要触发步）保留为 execute 计划普通步骤。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. DaemonSet 的 desiredNumberScheduled 相对基线降 1——DS 控制器把未容忍污点节点从
   应调度节点集合除名（desired 的变化即机制执法的直接回显）
2. 该节点上没有运行 DaemonSet Pod（Pod 被删除后控制器不重建——DS 控制器只为
   eligible 节点创建 Pod，**无 Pending replacement**，与 Deployment 的 Pending 现象面不同）
3. `kubectl get daemonset` 显示 DESIRED 数相对基线缺失（注意：本机制下 DESIRED 与
   READY **同步下降、保持一致**——「DESIRED != READY」不是本故障的判据）；节点
   SchedulingDisabled + 存在自定义污点

**资源准备**：
1. 确认 DaemonSet 应用 A 已正常运行，且所有节点均有副本。**集群内无良性可破坏 DS 时，
   由运维带外预置常驻演练靶 DS**（同 drill-pvc-target 常驻 Deployment 模式——DS 形态：
   节点缓存镜像 + 长 sleep 保活 + 微量 requests，**不配置任何自定义容忍**——DS 控制器
   自动追加的内置节点污点容忍不含自定义键，演练污点才能有效阻断；无容忍的副作用：
   集群存量污点节点同样被排除，**desired 基线 < 节点总数是正常形态，以当次探测值为准**）；
   镜像选型消费节点缓存镜像定案（`references/environment/node-cached-images.md`——
   与 terway-eniip DS 同 tag 即全节点免拉取，规划期至多单命令复核 ready/desired）。
   **Agent 只注入既有 DS，勿在执行计划里创建工作负载**——manifest 通道对 workload
   kind（DaemonSet/Deployment/Pod/Job…）是守卫的机制级禁令（守卫无法圈定新建工作负载
   的爆炸半径，拒绝消息即指引「inject into a workload that already exists」）；常驻靶
   不属一次性演练资产，演练结束**不删除**（跨 case 复用，恢复生命周期只做节点级还原
   与调试载体清理）
2. 确认监控系统可观测 DaemonSet 副本状态
3. **靶节点选型**：选 DS 基线 eligible 集合内（无污点）、不载其他演练靶的 Ready 节点
   （基线零污点使「污点摘除」恢复验证语义干净）；**爆炸半径声明**：cordon + 自定义
   污点在窗口内阻塞该节点上**一切未容忍新调度**（非仅靶 DS）——窗口由定时器封顶，
   存量 Pod 不受 NoSchedule 影响

**演练步骤**：
1. 选取一个运行 DaemonSet Pod 的节点
2. **先武装定时自恢复，再注入**（确认节点基线状态后武装定时器。必须用宿主机 systemd
   transient timer 武装（kubectl debug node + chroot /host + systemd-run）：`( sleep …; … ) &`
   后台子 shell 形态在 exec-form 通道不被解释，也不在 agent 守卫的载荷放行形态内；
   systemd-run 创建的 transient timer 由宿主机 systemd(PID 1) 管理，不依赖 debug Pod 存活。
   **timer 载荷只含 uncordon**——载荷 kubectl 以宿主机 kubelet.conf 为凭证，该凭证允许修改
   spec.unschedulable（uncordon 实际放行）但受 NodeRestriction 限制**不能修改 taints**，
   taint- 载荷到期只会得到 Forbidden、污点残留；污点摘除由 Agent 在演练结束时主动兜底。
   通道三要素/凭证边界/载体落位 ns 消费宿主机 timer 通道定案
   （`references/environment/host-timer-channel.md`——武装前单次 debug 探针即免推导，
   systemctl 探针返回 exit 4 unit-not-found 即通道通的证明，勿误读为探针失败重试）：
   ```bash
   # 基线确认：记录节点当前 unschedulable/taints 状态（恢复判据）
   kubectl get node <node> -o jsonpath='{.spec.unschedulable} {.spec.taints}'
   # 武装定时自恢复（宿主机 systemd timer；载荷仅 uncordon——taint- 受凭证限制不可自恢复）
   kubectl debug node/<node> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
     'systemd-run --on-active=<duration>s --unit=blade-restore-ds sh -c "kubectl --kubeconfig=/etc/kubernetes/kubelet.conf uncordon <node>"'
   ```
3. 使用 kubectl 将该节点标记为不可调度（cordon）：`kubectl cordon <node>`
4. 给该节点添加一个 DaemonSet 未配置容忍的自定义污点：`kubectl taint nodes <node> node.ops/maintenance=true:NoSchedule`
5. 删除该节点上的 DaemonSet Pod（**必要步**：NoSchedule 污点只阻断新调度、不驱逐存量
   Pod——污点后存量 Pod 仍 Running，须删除才呈现「节点缺 Pod」现象）。观察：Pod
   **不被重建**且**无 Pending replacement**（DS 控制器只为 eligible 节点建 Pod——
   与 Deployment 删 Pod 出现 Pending replacement 的现象面不同）
6. 观察 DaemonSet 副本数变化（desiredNumberScheduled 相对基线降 1）

**注入验证**：
1. 执行 `kubectl get nodes`，确认目标节点标记为 SchedulingDisabled
2. 执行 `kubectl describe node <node>`，确认自定义污点 `node.ops/maintenance=true:NoSchedule` 存在
3. **DS 计数直证（机制执法的直接回显）**：`kubectl get ds <ds-name> -n <ns>
   -o jsonpath='{.status.desiredNumberScheduled} {.status.numberReady}'`——desired 为
   **基线 - 1**（DS 控制器把未容忍污点节点从应调度集合除名；READY/AVAILABLE 同步为
   基线 - 1，DESIRED 与 READY 保持一致——勿以「不一致」为判据）
4. **节点缺 Pod + 无重建（状态型最强白盒证据）**：`kubectl get pods -n <ns> -l <ds-label>
   --field-selector spec.nodeName=<node>` 为空（节点上无该 DS Pod）；全量 `-l <ds-label>`
   Pod 无 Pending 态（无 replacement 产生——DS 控制器不为 ineligible 节点建 Pod，
   不存在 FailedScheduling 事件路径）；探针一律字面形态（Pod 名先解析后字面引用，
   勿用 `$()` 内联替换——verify 只读门禁拒命令替换）

**注入恢复**：
1. 等待 `<duration>` 到期，systemd-run 定时器自动恢复节点可调度（**污点不会随 timer 摘除**
   ——kubelet.conf 凭证不能修改 taints，报 Forbidden；污点残留期间 DaemonSet Pod 仍无法
   调度到该节点）。演练结束时由 Agent 主动执行同组恢复命令（幂等，定时器迟到再执行一次
   无副作用；两条命令独立执行），并停掉定时器避免迟到重放：
   ```bash
   kubectl uncordon <node>
   kubectl taint nodes <node> node.ops/maintenance=true:NoSchedule-
   kubectl debug node/<node> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host systemctl stop blade-restore-ds.timer
   ```
2. 等待 DaemonSet Pod 在该节点重建（uncordon + 污点摘除后 DS 控制器随即为该节点重建
   Pod、desiredNumberScheduled 回基线——常驻靶形态下 DS 不删除，恢复态判据
   **不随演练消失**，无需「最后取样窗口」式的拆线前取样义务）
3. **载体清理（归恢复生命周期）**：清理残留的 node-debugger-* 调试载体 Pod
   （kubectl debug 不带 -n 时载体落在当前命名空间——以执行通道当次探测为准；一次性
   debug 载体在命令结束后多自清，恢复期按 node-debugger 前缀扫描兜底）；常驻演练靶 DS
   **保留不删**（跨 case 复用资产，无拆线动作）

**恢复验证**：
1. 执行 `kubectl get nodes`，确认目标节点恢复为可调度状态
2. 执行 `kubectl describe node <node>`，确认自定义污点已被移除
3. 执行 `kubectl get daemonset`，确认 desiredNumberScheduled 回基线值（基线 - 1 →
   基线；READY 同步回基线）
4. 确认目标节点上 DaemonSet Pod 已正常运行（field-selector spec.nodeName=<node>
   查询非空且 Running）
5. **常驻靶形态的验证独立性**：靶 DS 跨演练存续，第 3/4 条判据恢复完成后随时可独立
   复核（区别于一次性载体的 teardown 超越裁决路径——本形态不适用）；载体清理后
   node-debugger 零残留；节点回基线（无污点、可调度、无 SchedulingDisabled）

**注意事项**：
- Kubernetes ≥ 1.12 的 DaemonSet 默认容忍 `node.kubernetes.io/unschedulable` 污点，因此 `kubectl cordon` 不会阻止 DaemonSet Pod 调度
- 必须添加自定义污点（DaemonSet 未配置容忍的），才能有效阻止 Pod 调度
- 恢复时需一并移除自定义污点，否则 Pod 仍无法调度
- NoSchedule 污点不驱逐存量 Pod（只阻断新调度）——「节点缺 Pod」现象须删除存量 Pod
  呈现；删除后 DS 控制器不重建（无 Pending replacement，无 FailedScheduling 事件路径
  ——DS 与 Deployment 在此机制下的现象面根本不同）

**基准事实**：
- **根因**：节点被标记为不可调度（cordon）且存在 DaemonSet 未容忍的自定义污点——DS 控制器把该节点从应调度集合（eligible nodes）除名，desiredNumberScheduled 降 1；节点上的存量 Pod 被删除后不再重建
- **必现现象**：DaemonSet desiredNumberScheduled 相对基线降 1（DESIRED 与 READY 同步、保持一致）；节点 SchedulingDisabled；节点存在自定义污点；节点上缺少 DaemonSet Pod 且无 Pending replacement
