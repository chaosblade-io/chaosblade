---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 解除 cordon（uncordon）），路由进 FaultDrill
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

**用例名称** 节点排空Drain 导致 Node_维护

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 解除 cordon（uncordon）；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

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
  - op: replace
    path: /spec/unschedulable
    value: true
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  - op: remove
    path: /spec/unschedulable
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- cluster-scoped 目标（Node）：targetRef 不写 namespace；CR 本体落任务主 ns（metadata.namespace 仍必须显式写入）。
- drain 的驱逐序列（`kubectl drain --ignore-daemonsets …`）保留为 execute 计划普通步骤——CR 只承载 cordon patch 域；被驱逐 Pod 不回迁（重建留在落位节点，正文恢复验证语义不变）。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. 节点被标记为 SchedulingDisabled，不再接受新 Pod 调度
2. 节点上所有非 DaemonSet Pod 被安全驱逐
3. 被驱逐 Pod 在其他节点重建；如集群资源不足，部分 Pod 进入 Pending
4. 模拟节点维护/升级场景下的工作负载迁移

**资源准备**：
1. 确认目标节点上有业务 Pod 运行（非仅 DaemonSet Pod）
2. 确认集群中其他节点有足够资源接纳被驱逐的 Pod
3. 确认目标节点名称（通过 `kubectl get nodes` 获取）

**演练步骤**：
1. 确认目标节点当前运行的 Pod：
   ```bash
   kubectl get pods --all-namespaces --field-selector spec.nodeName=<node-name> -o wide
   ```
2. **先武装定时自恢复，再排空**（定时器到期自动 uncordon 为主，Agent 在演练结束时主动执行
   同一条命令兜底，迟到重复执行无副作用。timer 必须以宿主机 systemd transient unit 武装
   （kubectl debug node + chroot /host + systemd-run）——裸 `( sleep … ) &` 后台子 shell
   形态在 exec-form 通道不被解释，也不在 agent 守卫的载荷放行形态内；systemd-run 创建的
   transient timer 由宿主机 systemd(PID 1) 管理，不依赖 debug Pod 存活（drain 驱逐 debug
   Pod 后恢复仍生效）。timer 载荷中的 kubectl 以宿主机 kubelet.conf 为凭证，该凭证允许修改
   spec.unschedulable（uncordon 实际放行；但受 NodeRestriction 限制不能修改 taints，见
   "节点污点注入Taint"用例）。`<duration>` 需覆盖 drain 与观察窗口）：
   ```bash
   # 冲突预检（固定单元名 + 残留预检）：systemctl status blade-restore-drain.timer
   # 回执 not-found（exit 4）= 无残留可武装——这是预检通过的预期形态，勿误读为探测失败重试
   kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host systemctl status blade-restore-drain.timer
   # 武装定时自恢复（宿主机 systemd timer；先武装再 cordon/drain，保证闹钟先于故障上膛）
   # **载荷形态红线（引号红线）**：systemd-run 直接 exec kubectl + 参数（无 sh -c 包装、
   # 零引号零嵌套）——多参数经 systemd-run 原生 argv 传递不经 shell 解释，比嵌套 sh -c
   # "kubectl ..." 形态（外单引号内双引号，传输层三层契约缺口高发区）结构性更稳
   kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host systemd-run --on-active=<duration>s --unit=blade-restore-drain kubectl --kubeconfig=/etc/kubernetes/kubelet.conf uncordon <node-name>
   # 标记不可调度
   kubectl cordon <node-name>
   ```
   - **基线快照义务（迁移判据的前后对照，注入前完成）**：记录目标节点全部非 DaemonSet Pod 的
     名字 + UID + 控制器归属 + 当前节点（`kubectl get pods -A --field-selector spec.nodeName=<node-name> -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} uid={.metadata.uid}{end}'` 再对 ownerReferences 归属分诊）——验证判据 3（业务 Pod 迁移）与恢复判据 3（业务 Pod Running）都需要「驱逐前 vs 重建后」的 UID/nodeName 对照，无基线快照则迁移性不可判
   - **目标节点选择红线**：目标节点上的非 DS Pod 必须全部是演练靶资产（常驻靶 Deployment）——drain 会驱逐该节点**所有**非 DS Pod，混入第三方业务 Pod 即误伤（爆炸半径违约）。若当前无满足条件的节点，先以常驻靶调度到目标节点（或选常驻靶已在的节点），以当次探测为准
3. 排空节点上所有 Pod（安全驱逐）：
   ```bash
   kubectl drain <node-name> \
     --ignore-daemonsets \
     --delete-emptydir-data \
     --grace-period=30 \
     --timeout=120s
   ```
   - **unmanaged Pod 清障义务（execute 计划的前置 mutation step，drain 发出前完成）**：
     drain 的过滤链对无控制器归属的 Pod（unmanaged，典型如 planning/预检期遗留的 Completed 态
     one-shot node-debugger-* debug Pod）默认报错拒绝继续（要求 `--force`，而 Agent 的 kubectl
     工具接口对 drain 拒绝 `--force` 透传）。因此注入方 execute 计划须把清障编为 drain 前的显式
     step：发现残留即 `kubectl delete pod node-debugger-* -n <ns> --force --grace-period=0`
     （演练自有中间产物，合法 mutation），删除后以名册复查零 unmanaged Pod 再发 drain。
     勿依赖 one-shot debug Pod 的 self-clean 时序恰好先行（self-clean 恰好先完成是
     运气而非保证）；零 unmanaged Pod 名册 = 无需 `--force` 的结构性证明
4. 观察被驱逐 Pod 的重建情况

**注入验证**：
0. **drain 命令回执是一等 L1 证据**：输出含 `evicting pod <ns>/<pod>` 逐行记录（每个非 DS Pod 一条）+ 节点 `drained` 收尾行——回执本身即驱逐过程直接证据（与 df/字节数同级的直证），勿只依赖事后状态推断。**回执截断预期**：drain 是长命令（grace-period 等待逐个驱逐落地），执行 harness 的 30s task ceiling 会先于 kubectl 自身 `--timeout=120s` 触发截断——截断 ≠ 驱逐失败。等价判据双收：①事件面新 `Killing` 记录（`kubectl get events -n <ns> --field-selector involvedObject.name=<pod>`——REASON=Killing、COUNT 较 baseline Δ+1、FIRST SEEN 落在注入窗内，驱逐的集群侧工件）；②节点名册 Pod GONE（`--field-selector spec.nodeName=<node>` 列表中原 Pod 消失）。**勿重发 drain**——驱逐已落地后重发对已删除 Pod 报 not found，且重复 mutation 违反最小动作纪律
1. 执行 `kubectl get nodes`，确认目标节点状态为 `Ready,SchedulingDisabled`（权威形态：`kubectl get node <node-name> -o jsonpath='{.spec.unschedulable}'` = `true`——SchedulingDisabled 是展示层标签，判据读 spec 字段）
2. 执行 `kubectl get pods --field-selector spec.nodeName=<node-name> --all-namespaces`，确认仅剩 DaemonSet Pod
3. 执行 `kubectl get pods -n <namespace> -l <label-selector> -o wide`，确认业务 Pod 已迁移到其他节点
4. 检查是否有 Pod 因资源不足进入 Pending：
   ```bash
   kubectl get pods --all-namespaces --field-selector status.phase=Pending
   ```

**注入恢复**：
1. 等待 `<duration>` 到期，定时器自动恢复节点为可调度状态；演练提前结束时由 Agent 主动执行
   同一条恢复命令（幂等，定时器迟到再执行一次无副作用），并停掉已武装的 timer 避免迟到重放：
   ```bash
   kubectl uncordon <node-name>
   kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host systemctl stop blade-restore-drain.timer
   ```
2. 等待调度器将 Pending Pod（如有）重新调度

> ⚠️ `kubectl cordon/drain` 本身**没有自动恢复机制**，自恢复依赖注入前武装的宿主机 systemd
> transient timer（由 PID 1 管理，debug Pod 被驱逐/删除不影响；timer 载荷凭证为宿主机
> kubelet.conf——对 uncordon 实际放行）。Agent 主动 uncordon 兜底始终有效。被驱逐的 Pod
> 不会自动迁回本节点（uncordon 后仅恢复可调度性，新 Pod 与再平衡由调度器决定）。

**恢复验证**：
1. 执行 `kubectl get nodes`，确认目标节点状态恢复为 `Ready`（无 SchedulingDisabled；权威形态：`.spec.unschedulable` 缺失或 false）
2. 执行 `kubectl get pods --all-namespaces --field-selector status.phase=Pending`，确认无 Pending Pod
3. 确认业务 Pod 全部 Running 且 Ready——**语义边界：被驱逐 Pod 不会迁回原节点**（uncordon 仅恢复可调度性，重建 Pod 留在驱逐后落位的节点，由调度器决定；判「在其他节点 Running」而非「回原节点」），以基线快照 UID 对照确认重建完成（新 UID + 新 nodeName + Running = 迁移成功）

**基准事实**：
- **根因**：节点被 cordon + drain 标记为不可调度并驱逐所有工作负载，模拟节点维护场景下的 Pod 迁移行为
- **必现现象**：节点状态为 SchedulingDisabled；非 DaemonSet Pod 被驱逐并在其他节点重建；drain 命令输出 evicting/evicted 信息
