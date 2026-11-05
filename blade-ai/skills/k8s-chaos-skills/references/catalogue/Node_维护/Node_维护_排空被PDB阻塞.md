---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2/ND9）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 解除 cordon（uncordon）+ DELETE 注入的
# 演练道具 PodDisruptionBudget）。靶为 Node（cluster-scoped）——装配器 M1 四对象栈仅支持
# namespaced 靶（Role/RoleBinding 无法授权 cluster-scoped 资源，工具对 Node 靶 fail-closed
# 拒绝），本 case 无装配器主路径。
# uncordon 不可走宿主机 systemd timer：本 case
# 批准的 fault family 是 eviction，eviction 族无 host binary 映射——guard family_mismatch
# 结构性关闭整条 host-chroot carrier 路径（read-only chroot 探针与配对 cordon&&systemd-run
# 均被 reject_banned，LLM 原文「Host path is structurally closed for the 'eviction' family」）。
# 与 node-maintenance/drain 族（可用 host timer）的关键分野即在此。故恢复走 cluster-side
# 载体五件套（SA + ClusterRole node-uncordon/pdb-delete + Binding×2 + 裸载体 Pod），载体
# 仅容忍 resource-pool 污点以调度到非靶节点（避免被本次 drain 波及），in-pod timer 到点用
# SA token curl PATCH/DELETE 打 apiserver，属 recovery-carrier spec「降级路径」的结构性形态。
# 受害者作用域参考（route A，非强制 hint）：受害者是被 PDB 保护、驱逐被拒的靶 Pod（验证
# 「PDB 可用性护栏能否保护 Pod 不被驱逐」）；node 是机制攻击面（cordon/drain 触发驱逐尝试）
# 非受害者——victim（Pod）/ mechanism（Node+PDB）/ blast-radius（cluster-wide，见「爆炸半径分类」定案）三域分离。
fault_scope: pod
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract，双派生轴）：受害者是 Pod，故障机制须写两类
# 受害者覆盖之外的对象，均用派生轴（可移植、无静态占位符）：
# (1) cordon/drain 写靶 Pod 所在宿主机 node——node 名由集群调度运行时决定、无法硬编码，用
#     name_from: victim_node（freeze 时 discover_victim_nodes 解析真实节点名、materialize 物化，
#     同 Pod_Terminating_节点宕机kubelet失联 先例）；drain 的 effective.scope=node ∉
#     OWNER_SCOPES['pod']（仅 replicaset 等 owner），缺此声明必被 drift 第 4 步 REJECT，走 3.6 豁免。
# (2) 创建演练道具 PodDisruptionBudget（drill-pdb- 前缀新对象）——pdb 不在 pod 受害靶 scope
#     及其 secondary_scopes 内（fault_registry family scopes 仅 12 个、无 pdb），同样须立法；ns 用
#     namespace_from: victim 派生（PDB 靠 selector 作用于同 ns 靶 Pod、必须建在受害者运行时所在
#     ns，可移植 case 无法硬编码；freeze 时物化为 spec.namespace，同 NetworkPolicy误配 / LimitRange误配）。
# 为何不用 victim=node + 静态 ns：node 是 cluster-scoped 无 ns 可派生，PDB 的 ns（靶 Pod ns）只能
#     静态占位，而占位符 freeze 时当字面量物化、与实际 create ns 不匹配必 REJECT_DRIFT（死路）。
mechanism_writes:
  - scope: node
    name_from: victim_node
  - scope: poddisruptionbudget
    namespace_from: victim
    name_prefix: drill-pdb-
---

**用例名称** PDB阻塞排空 导致 Node_维护

## 故障定位
配置型故障——一个 `minAvailable` 被设为等于当前健康 Pod 数的 PodDisruptionBudget 生效后，
任何对该组 Pod 的 **voluntary eviction**（`kubectl drain` / Eviction API）都会因"驱逐后可用
副本数 < minAvailable"被 apiserver 拒绝（HTTP 429 + `Cannot evict pod as it would violate
the pod's disruption budget`），**PDB 对象存活即故障存活**，贯穿整个故障窗口；窗口到期删除
PDB + uncordon 节点即自动恢复。

与既有 `Node_维护_节点排空Drain` 案的**互补关系**：该案无 PDB 护栏、drain
成功驱逐全部非 DS Pod 并异地重建（验证"维护排空能发生"）；本 case 有 PDB 护栏、drain 的
evict 被 PDB 全部拒绝、靶 Pod 保持 Running 不迁移（验证"PDB 可用性护栏能阻止排空"）——两者
是节点维护韧性的正反两面，同用 cordon+drain 触发（该案无 PDB、victim=node、驱逐直接落在 node 负载迁移；本 case victim=pod——被保护的靶 Pod 才是「验证谁韧性」的受害者，node 只是机制攻击面），PDB 道具是唯一增量与分水岭。

手段1（ChaosBlade）不适用——ChaosBlade 无 PDB/准入配置类故障靶点，本用例为 kubectl-native
专属注入。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认；下限须覆盖
planning+execute 全程（drain 的 `--timeout` 重试窗 + 观察窗，窗口过短会导致 verify 段首观测
落在恢复之后、primary 证据无法在窗内采集）。

## 恢复动作配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：uncordon 节点（逆 cordon
patch）+ DELETE 演练道具 PDB。**靶为 Node（cluster-scoped），装配器 M1 不支持**（四对象栈
Role/RoleBinding 无法授权 cluster-scoped 资源）。**uncordon 不走宿主机
systemd timer（eviction 族 host 路径被 family_mismatch 结构性关闭），
改走 cluster-side 载体五件套自治**（载体 Pod 调度到非靶节点，in-pod timer 到点用 SA token
curl PATCH uncordon + curl DELETE PDB）；Agent 演练结束时主动兜底 delete：

```yaml
targetRef:                                # 恢复主靶 = Node（uncordon；cluster-scoped 无 namespace）+ 演练道具 PDB（delete）；受害者是靶 Pod（frontmatter fault_scope），node/PDB 均为机制攻击面
  kind: Node
  name: <node-name>
patches:                                  # 注入域：cordon（json-patch 语义，实际经 kubectl cordon）+ create PDB（execute 计划 kubectl 步骤）
- op: replace
  path: /spec/unschedulable
  value: true
- op: create
  path: /apis/policy/v1/namespaces/<target-namespace>/poddisruptionbudgets
  value: <下方演练步骤 3 的完整 PDB YAML>
restorePatches:                           # 恢复域：uncordon（cluster-side 载体 timer 自治）+ DELETE PDB（载体 + Agent 兜底）
- op: remove
  path: /spec/unschedulable
- op: delete
  path: /apis/policy/v1/namespaces/<target-namespace>/poddisruptionbudgets/drill-pdb-<hash>
durationSeconds: <duration>               # 观察窗 D（框架在场契约）；cluster-side 载体 in-pod timer sleep 取 recovery_timer_seconds=D+G
```

- cluster-scoped 目标（Node）：targetRef 无 namespace 字段；uncordon 恢复不经装配器，走 cluster-side 手动载体（**不走 node-maintenance/drain 族的宿主机 timer——eviction 族 host 路径被 family_mismatch 关闭**）。
- **恢复时序红线（本 case 最大风险点）**：**drain `--timeout=25s`（<30s
  ceiling）自行超时退出（exit 1，无后台 evict 重试悬挂）**；恢复仍**必须先确认 drain 已
  超时退出（无活跃 evict 重试）→ uncordon → 再 delete PDB**——若 PDB 先删而 drain 仍活跃，
  drain 的 evict 会突然成功、真的驱逐靶 Pod（恢复动作反而落地了"Pod 被驱逐"这一故障反面）。
  安全序不可颠倒。
- drain 的驱逐序列（`kubectl drain --ignore-daemonsets …`）保留为 execute 计划普通步骤；被
  PDB 挡住故实际不驱逐，靶 Pod 不回迁也无需回迁（全程在位）。

## 故障现象
1. 节点被 cordon，标记为 SchedulingDisabled（不再接受新 Pod 调度）
2. `kubectl drain` 尝试驱逐靶 Pod，被 PDB 拒绝——回执含 `Cannot evict pod as it would
   violate the pod's disruption budget` 或 Eviction API `429 Too Many Requests`，drain 在
   `--timeout` 内反复重试后超时失败（退出码非零）
3. **靶 Pod 保持 Running、未被驱逐、无异地重建**（与 node-maintenance/drain 族 drain 成功的关键区别）——这是 PDB
   可用性护栏生效的直接证据
4. 节点无法排空，模拟的维护/升级操作被 PDB 阻塞

## 资源准备
1. 确认目标节点名称（`kubectl get nodes`）+ 其上运行的非 DaemonSet 靶 Pod
2. **爆炸半径红线（同 node-maintenance/drain 族）**：目标节点上的非 DS Pod 必须全部是演练靶资产（常驻靶
   Deployment）——drain 会尝试驱逐该节点**所有**非 DS Pod，混入第三方业务 Pod 即误伤（爆炸
   半径违约）。若当前无满足条件的节点，先以常驻靶调度到目标节点（或选常驻靶已在的节点），
   以当次探测为准
   - **kruise Advanced DaemonSet caveat + 强制 --pod-selector**：
     `--ignore-daemonsets` 只识别原生 `apps/v1` DaemonSet；部分集群多个 `apps.kruise.io/v1alpha1`
     Advanced DaemonSet（terway/csi/kube-proxy/koordlet 等）不被识别。**plain drain 在 kruise
     集群行为不确定**——kruise DS 被驱逐后可能同节点自愈重建；代码级分析
     推断 daemonSetFilter 只比对 Kind 不比 apiVersion → 对 kruise owner `AppsV1().DaemonSets(ns).Get()`
     返回 NotFound → 无 `--force`（被用户约束+守卫双禁）时 drain **在发出任何 Eviction 前 abort**
     （一等 L1 证据 429 永采不到、归因被「kruise DS 查不到」污染）。两条路径均破坏判据。**故一律
     用 `--pod-selector=<靶Pod label selector>`（见演练步骤 5）**：labelFilter 先于 daemonSetFilter
     且 deletePod=false 短路，kruise DS Pod 在报错分支前即被跳过——规避 abort-vs-evict 不确定性
     + 显式收窄爆炸半径 + 结构性免除 unmanaged Pod 清障义务
3. 记录靶 Pod 的 namespace + label（PDB selector 用）+ **当前健康 Pod 数 N**（PDB
   `minAvailable` 必须 = N，确保 drain 的每一次 evict 都被拒；若 minAvailable < N 则 drain
   会部分成功、驱逐 N−minAvailable 个 Pod，故障现象从"全阻塞"退化为"部分驱逐"、判据不纯）：
   ```bash
   kubectl get pods -n <target-namespace> -l <label-selector> -o jsonpath='{range .items[*]}{.metadata.name}  phase={.status.phase}  node={.spec.nodeName}{"\n"}{end}'
   ```
4. 确认目标命名空间当前没有同名演练 PDB（命名冲突预检：`kubectl get pdb drill-pdb-<hash>
   -n <target-namespace> -o name`——NotFound 即无冲突）
5. **武装 cluster-side 恢复载体（uncordon + PDB delete 自治恢复）——不走 node-maintenance/drain 族宿主机 systemd timer（eviction 族 host 路径被 family_mismatch 结构性关闭）；载体五件套准备见演练步骤 2**

## 演练步骤
（主路径 = 基线捕获（步骤 1）→ 武装 cluster-side 恢复载体（步骤 2，uncordon + PDB delete 自治）→ create PDB 道具
（步骤 3）→ cordon（步骤 4）→ drain 被阻塞（步骤 5）；本 case 注入含 create 新对象（PDB），
无装配器主路径，恢复走 cluster-side 载体 timer + Agent 兜底）：

> **爆炸半径分类（定案，defer SKILL.md L41 + recovery-carrier §9；node-local →
> cluster-wide）**：声明 `blast_radius_scope=cluster-wide`——依据：本 case 恢复通道走
> cluster-side 载体**五件套变体**，因恢复主靶 Node 是 cluster-scoped、装配器 M1 四对象栈
> （Role/RoleBinding）无法授权 cluster-scoped 资源，必须新增 **ClusterRole + ClusterRoleBinding
> 两个集群级持久对象** → 按 SKILL.md 规则③「五件套变体（含集群级两件）→ cluster-wide」定案。
> **原 node-local 定案的 rationale「无跨 ns 持久资源写入」与 case 自身处方的恢复通道自相矛盾**。
> 须区分两个层面：①**爆炸半径 scope=cluster-wide**（恢复
> 载体写入 cluster-scoped RBAC，须走 elevated safety review，取宽——过声明仅审计精度损失、欠声明
> 饿死安全审查）；②**实际负载影响仍为 node-local**（drain 被 PDB 全挡、靶 Pod 原地不动不迁移、
> --pod-selector 钉死驱逐对象为靶 Pod、第三方业务 Pod 与系统 DS Pod 零影响）——二者不矛盾：scope
> 描述写入面，负载影响写入 `blast_radius_detail`。

1. **基线捕获**：记录注入前节点状态（Ready、无 SchedulingDisabled）+ 靶 Pod 清单（全
   Running、nodeName=<node-name>）+ PDB 不存在（恢复验证的对照锚点）
2. **先武装定时自恢复，再注入**（**cluster-side 载体五件套，不走宿主机
   systemd timer**——eviction 族无 host binary 映射，`kubectl debug node -- chroot /host
   systemd-run` 的 read-only chroot 探针与配对 cordon&&systemd-run
   均被 reject_banned[family_mismatch]（LLM 原文「Host path is structurally closed for the
   'eviction' family」）。改自建 cluster-side 载体：SA + ClusterRole（node get/patch +
   pdb get/delete）+ ClusterRoleBinding×2 + 裸载体 Pod，载体**仅容忍 resource-pool 污点**
   以调度到**非靶节点**（避免被本次 drain 波及），in-pod timer 到点用 SA token curl 打
   apiserver 执行 uncordon PATCH + PDB DELETE。载体 YAML 骨架见 recovery-carrier.md「五件套」）：
   ```bash
   # 载体调度预检：确认存在非靶节点可承载载体（载体 nodeAffinity 反选靶节点 + 仅容忍
   # resource-pool 污点，勿容忍 maintenance/unschedulable 以免落到正在被 drain 的靶节点）
   kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.unschedulable}{"\n"}{end}'
   # 武装载体（apply 五件套 → 载体 Pod Running 后 in-pod timer 起算；先武装再注入，
   # 保证闹钟先于故障上膛）。timer 载荷 = sleep <recovery-seconds> && curl PATCH uncordon
   # && curl DELETE pdb，凭证取载体 SA token（/var/run/secrets/.../token）打 apiserver
   kubectl apply -f <carrier-five-piece.yaml>
   ```
   （`<recovery-seconds>` 取 prompt 下发的 `recovery_timer_seconds`= duration + grace，需覆盖
   drain 与观察窗口；武装后发生任何修复须先拆旧载体再全额重武装，见 SKILL.md「故障窗口完整」）
3. **创建演练道具 PDB**（selector 精确匹配靶 Pod，minAvailable = 靶 Pod 数 N）：
   ```bash
   cat <<'EOF' | kubectl apply -f -
   apiVersion: policy/v1
   kind: PodDisruptionBudget
   metadata:
     name: drill-pdb-<hash>
     namespace: <target-namespace>
   spec:
     minAvailable: <N>                     # = 当前健康靶 Pod 数，使 disruptionsAllowed=0、任何 evict 都被拒
     selector:
       matchLabels:
         <靶Pod独有标签键>: <靶Pod独有标签值>
   EOF
   ```
4. **cordon 节点**（标记不可调度）：`kubectl cordon <node-name>`
5. **drain 节点（被 PDB 阻塞）**：
   ```bash
   kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --grace-period=30 --timeout=25s --pod-selector=<靶Pod label selector>
   ```
   （**`--timeout` 取 25s**：drain 会被 harness 30s task ceiling
   截断，`--timeout` 须 < 30s 方能让 drain 在截断前自行超时退出（回执含 `global timeout
   reached: 25s` + exit 1），否则截断竞态使 evict 重试悬在后台、干扰恢复时序判定）
   - drain 内部对每个靶 Pod 发 Eviction API，被 PDB 拒绝（429），drain 重试至 `--timeout`
     失败——**这是预期的故障现象，不是注入失败**；drain 非零退出码 + PDB 阻塞回执即注入生效证据
   - **勿加 `--disable-eviction`**（守卫 `KUBECTL_DRAIN_FORBIDDEN_FLAGS` 拒绝——它绕过 PDB，
     而 PDB 正是本演练要验证的护栏）；**勿加 `--force`**（同被拒：删除无控制器 Pod 不可重建）
   - **`--pod-selector=<靶Pod label selector>` 是 kruise 集群的强制项**：labelFilter 在
     drain 过滤器链中先于 daemonSetFilter 且以 deletePod=false 短路，使 kruise Advanced DS Pod
     （owner apiVersion 不被 daemonSetFilter 识别）在报错分支前即被跳过——既规避 plain drain 的
     abort-vs-evict 不确定性（见资源准备第 2 条 caveat），又显式收窄驱逐对象为靶 Pod。语义无损：
     靶节点非 DS 业务 Pod 只有靶 Pod，加不加该 flag 驱逐对象集合相同（该 flag 被工具接受、无
     `daemonsets.apps not found` abort、drain 产出精确 429 主证）
   - **unmanaged Pod 清障义务（同 node-maintenance/drain 族；`--pod-selector` 在场时结构性免除）**：drain 对无控制器
     归属的 Pod（如 planning/预检期遗留的 Completed 态 node-debugger-* debug Pod）默认报错拒绝
     继续（要求 `--force` 而工具接口拒透传）；但 `--pod-selector` 的 labelFilter 先短路跳过不匹配
     的 unmanaged Pod，故清障义务被免除。若因故未用 --pod-selector，仍须把清障编为显式 step：
     发现残留即 `kubectl delete pod node-debugger-* -n <ns> --force --grace-period=0`

## 注入验证
0. **drain 回执是一等 L1 证据**：输出含 `Cannot evict pod as it would violate the pod's
   disruption budget` 或 `error when evicting pods/... 429`（逐 Pod 重试记录）——回执本身即
   PDB 阻塞直接证据（与 df/字节数同级直证）。**drain `--timeout=25s`（<30s）
   在 harness task ceiling 前自行超时退出（回执 `global timeout reached: 25s` + exit 1），
   非被截断——超时退出 ≠ 注入失败，等价判据见下三条状态面
1. 确认节点 cordon 生效（权威形态读 spec 字段）：
   `kubectl get node <node-name> -o jsonpath='{.spec.unschedulable}'` = `true`
2. **确认靶 Pod 保持 Running、未被驱逐（PDB 护栏主证）**：
   ```bash
   kubectl get pods -n <target-namespace> -l <label-selector> -o jsonpath='{range .items[*]}{.metadata.name}  phase={.status.phase}  node={.spec.nodeName}{"\n"}{end}'
   ```
   预期：全部靶 Pod phase=Running、nodeName 仍为 <node-name>（未迁移、无新 UID 重建）
3. **确认 PDB status 字段（确定性对账，勿只依赖 LLM 语义判断）**：
   ```bash
   kubectl get pdb drill-pdb-<hash> -n <target-namespace> -o jsonpath='{.status.currentHealthy}/{.status.desiredHealthy} allowed={.status.disruptionsAllowed}'
   ```
   预期：currentHealthy=N、desiredHealthy=N、**disruptionsAllowed=0**（0 即"不允许任何中断"
   = 驱逐必被拒的字段级铁证）
4. **事件面为预期阴性（anticipated negative），不得作判据**：429/PDB 拒绝路径把
   拒因返回在 Eviction **response body** 给 drain client，**不 persist pod Event**——verify
   段集群级核实 `kubectl get events -A --field-selector reason=FailedEviction` 零命中、靶 Pod 无
   驱逐事件。故一等 L1 证据是**第 0 条的 drain 回执文本本身**（`Cannot evict pod ... disruption
   budget` / 429），事件检查恒阴性、**不得据事件缺失判注入失败**

> ⚠️ 验证纪律：
> - drain 非零退出 + 超时截断是**预期故障形态**，不得误判为注入失败而重发 drain（重发对已
>   cordon 节点重复 evict 重试，违反最小动作纪律）。
> - 同一事实（PDB 是否在位、Pod 是否 Running）确认一次即可，不要重复查询。

**持续性检查**——故障窗口内 PDB 阻塞必须持续存活（配置型：PDB 在即阻塞在）：
以"注入生效确认"为时点锚（注入验证第 1-3 条通过 = 生效），生效后隔多久复测、复测几次由你依本故障形态自判（配置型：接线在即故障在，无传播过程；无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**（效果证据须在故障存活期内采集，恢复后无法再采）：
1. 白盒复查：PDB 对象仍存在且 disruptionsAllowed=0（同注入验证第 3 条）
2. 行为复查：靶 Pod 仍 Running、nodeName 未变（同注入验证第 2 条）
3. 稳定性复查：节点仍 SchedulingDisabled、靶 Pod RESTARTS 计数无增长

## 注入恢复
（主路径下 uncordon + PDB DELETE 由 cluster-side 载体 in-pod timer 自治承载；Agent 演练结束
时主动兜底。**安全序：确认 drain 已超时退出 → uncordon → delete PDB**，避免删 PDB 时活跃
drain 突然驱逐成功。以下手动命令为降级兜底形态）：
1. 确认 drain 已超时退出（无活跃 evict 重试）；演练提前结束由 Agent 主动执行恢复（幂等——
   `--ignore-not-found` 对已删 PDB 无副作用，uncordon 对已可调度节点重复执行无害），并拆掉
   已武装的 cluster-side 载体避免 in-pod timer 迟到重放：
   ```bash
   kubectl uncordon <node-name>
   kubectl delete pdb drill-pdb-<hash> -n <target-namespace> --ignore-not-found
   kubectl delete -f <carrier-five-piece.yaml> --ignore-not-found   # 拆载体五件套（SA/ClusterRole/Binding×2/Pod）
   ```
   （**无需 `chroot /host systemctl stop` 拆线**：eviction 族 host 路径被
   family_mismatch 关闭，且 uncordon 幂等使载体 timer 迟到 fire 无害，无需 host 侧停 timer）
2. 靶 Pod 全程 Running 未被驱逐，无需重建/回迁（与 node-maintenance/drain 族 drain 成功案的"异地重建"恢复语义不同）

> ⚠️ `kubectl cordon/drain` 本身**没有自动恢复机制**，uncordon 依赖注入前武装的
> **cluster-side 载体 in-pod timer**（**不走宿主机 systemd timer——eviction
> 族 host 路径被 family_mismatch 结构性关闭**；载体调度到非靶节点，不被本次 drain 波及，
> timer 载荷凭证为载体 SA token 打 apiserver）；PDB 道具由载体 timer + Agent 主动 delete
> 兜底。Agent 主动 uncordon + delete pdb 兜底始终有效。

## 恢复验证
1. 节点恢复可调度：`kubectl get node <node-name> -o jsonpath='{.spec.unschedulable}'` 缺失或
   false（`kubectl get nodes` 无 SchedulingDisabled）
2. PDB 道具已删除：`kubectl get pdb drill-pdb-<hash> -n <target-namespace> -o name` 预期
   NotFound（404 即删除确证）
3. 靶 Pod 仍全部 Running、RESTARTS 与基线一致、nodeName 未变（全程未被驱逐、无新 UID 重建）
4. 确认载体取证（**不查宿主机 systemctl——eviction 族 host 路径已关闭**）：
   cluster-side 载体五件套已拆除（`kubectl get sa,clusterrole,clusterrolebinding,pod -l
   <carrier-label>` 均 NotFound）；若 Agent 提前恢复则载体 timer 为空触发，同为已恢复证据

## 基准事实
- **根因**：PDB `minAvailable` 被设为等于当前健康 Pod 数（disruptionsAllowed=0），使任何
  voluntary eviction 都被 apiserver 拒绝——模拟"PDB 配置过严阻塞节点维护排空"场景（运维为
  保护关键负载设了过严的 PDB，导致节点无法排空做维护/升级）
- **必现现象**：节点 cordon（SchedulingDisabled）；drain 的 evict 被 PDB 拒（429 /
  disruption budget violation）；靶 Pod 保持 Running 不迁移；PDB disruptionsAllowed=0
- **与 node-maintenance/drain 族的分界**：该案（无 PDB、victim=node）drain 成功驱逐 + 异地重建；本 case（有 PDB、
  victim=pod）drain 被阻塞 + Pod 原地不动——同用 cordon+drain 手段，但受害者不同（该案验证
  node 排空韧性、本 case 验证靶 Pod 受 PDB 护栏不被驱逐），PDB 道具是唯一增量与分水岭
- **作用域边界**：仅 PDB selector 匹配的靶 Pod 受保护；节点上其他非 DS Pod（若有）不受本 PDB
  保护、会被 drain 驱逐——故资源准备第 2 条红线要求 node 上非 DS Pod 全是演练靶

## 注意事项
- ChaosBlade 无 PDB/准入配置类故障靶点，本用例为 kubectl-native 专属注入
- **PDB 走标准 kubectl-native 创建路径**：`poddisruptionbudget` 已注册进守卫白名单（含
  pdb/pdbs/poddisruptionbudgets 别名），标准 `kubectl apply -f -` PDB 路径被守卫接受（回执
  `poddisruptionbudget.policy/drill-pdb-<hash> created`，无 reject_banned/无 reject_unknown）
  ——步骤 3 的 apply 形态合法，无需 apiserver-POST curl workaround
- **恢复时序是本 case 最大风险点**：**drain `--timeout=25s`（<30s ceiling）
  自行超时退出、无后台 evict 重试悬挂**；若 PDB 先删而 drain 仍活跃，drain 的 evict 会突然
  成功驱逐靶 Pod（恢复动作反而落地了故障反面）。安全序必须"确认 drain 退出 → uncordon →
  delete PDB"，不可颠倒
- `minAvailable` 必须精确 = 靶 Pod 数 N；设成 N−1 或更小会让 drain 部分成功、驱逐若干 Pod，
  故障现象从"全阻塞"退化为"部分驱逐"、判据不纯
- **双派生轴的可移植性**：victim=pod，PDB 用 `namespace_from: victim`（派生靶 Pod ns）+ node
  cordon 用 `name_from: victim_node`（派生靶 Pod 所在 node），均 freeze 期物化、无静态占位符，
  换靶/换集群自动跟随（对比 victim=node + 静态 ns 死路：占位符 freeze 当字面量必 REJECT）
- **双派生轴的建模**：node `name_from: victim_node`（同 Pod_Terminating 先例）+ PDB
  `namespace_from: victim`（同 NetworkPolicy·LimitRange 先例）两轴合于一案——drain
  （effective=node）经 name_from victim_node 授权，drift 第 3.6 步对 drain 的 node 写命中
  node entry、对 create PDB 命中 pdb entry，两轴 freeze 期物化无 REJECT，建模成立
- 演练道具命名带 `drill-pdb-` 前缀（任务侧资产归属登记依据），**禁止**给演练资源加任何演练
  标记 label（演练须对集群呈现为真实事故，见 recovery-carrier.md 第一节命名约定）
