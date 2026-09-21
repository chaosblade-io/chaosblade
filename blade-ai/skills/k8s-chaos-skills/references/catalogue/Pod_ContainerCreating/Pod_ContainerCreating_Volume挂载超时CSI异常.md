---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 移除注入的卷挂载并还原 maxUnavailable），路由进程序化
# 恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本用例的故障机制需要写受害者
# 覆盖之外的对象——注入创建 / 恢复删除演练用 PVC（静态绑定指向不存在云盘的
# PV，瞬态对象）。PVC 写入不在守卫二级范围网内，走本立法通道（名字级精度）；
# 测试 PV（cluster-scoped）仍由网放行，无需立法。由 case 作者在此声明，确定性
# 代码在意图定案时装载，确认卡渲染、人工批准后冻结进守卫快照。LLM 无权扩写。
# 主路径装配器四件套（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash>）由
# 工具内程序化构建——构造保证 + fail-closed 内嵌检查（RBAC 从 restorePatches
# 同源推导禁通配、SA 真实 token 验权 403 中止+清理），不经 LLM kubectl 写面
# （design ND3：程序化路径不复用 LLM 守卫），无立法条目。
mechanism_writes:
  # 道具 PVC（静态绑定假盘 PV 的瞬态对象——注入创建/收尾删除走 LLM kubectl
  # apply/delete 写面）
  - scope: persistentvolumeclaim
    namespace: default
    names: [archive-vol-claim]
---

**用例名称** Volume挂载超时CSI异常 导致 Pod_ContainerCreating

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 移除注入的卷挂载并还原 maxUnavailable；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
# 基线无卷 → add 整键（带注入的假卷引用）；基线有卷 → add /…/volumes/- 追加
- op: add
  path: /spec/template/spec/volumes
  value: [{name: archive-vol, persistentVolumeClaim: {claimName: archive-vol-claim}}]
- op: add
  path: /spec/template/spec/containers/0/volumeMounts
  value: [{name: archive-vol, mountPath: /archive}]
# MU 100% 并入注入域（防滚动死锁：注入期新 Pod 永不 Ready——卡 ContainerCreating
# 卷挂载失败，默认策略下 K8s 不会终止旧 Pod，滚动更新死锁——与 PVC/OOM/
# ReadinessProbe case 既有模式一致）
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: "100%"
restorePatches:                           # 恢复域：载体 TTL 到点执行；Agent 死亡后 recover 重放同源
# 基线空 → remove 整键；基线非空 → replace <基线数组>；maxUnavailable 还原注入前原值
- op: remove
  path: /spec/template/spec/volumes
- op: remove
  path: /spec/template/spec/containers/0/volumeMounts
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: <基线值>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- **remove 对账前提**：装配器基线校验要求 remove op 的路径在活体基线中不存在——本案 remove volumes/volumeMounts 通过校验的前提正是靶形态「无既有卷挂载」（资源准备第 3 条选型约束：注入的假卷是唯一变量）；基线带既有卷的靶不适用本配方 remove 形态，须按 replace 基线完整数组重写配方（另选靶或改配方，勿带病装配）。
- **恢复域拆分（patch 域与非 patch 域）**：载体 TTL 只管 Deployment patch 域（volumes/volumeMounts/MU 还原）；道具 PVC/PV 清理是 DELETE 动作、超出 json-patch 语义，不进载体配方——走收尾步（execute 计划普通 kubectl 步骤，两路径共用：先 PVC 后 PV；PV 卡 Terminating 时 json-patch 移除 finalizers 兜底——正文恢复段第 5-6 步）。道具 PV/PVC 创建同为 execute 计划 manifest 步骤（先 PV 后 PVC 分两次 apply——多文档禁令立法不变，PVC 走 frontmatter mechanism_writes 立法条目）。
- **MU 100% 的承载分工**：主路径下步骤 1 的手动置 100% 由配方注入域承载（一次 patch 面内原子并入，无需单独执行），其还原随 restorePatches 由载体 TTL 还原；降级路径下步骤 1/7 的手动序列照常执行。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作（PV/PVC 道具创建与清理）保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `FailedMount` 或 `FailedAttachVolume`，提示 CSI driver 超时或 attach 失败
3. PV 指向的云盘不存在或不可用，CSI 驱动无法完成 attach/mount

**机制警示（真实演练教训，违反则故障现象必然退化）**：
- **严禁给 PV 添加 nodeAffinity**：一旦 PV 带 `nodeAffinity`，调度器的 VolumeBinding 过滤器会在**调度阶段**前置过滤节点（Events 显示 `volume node affinity conflict`，Pod 呈 `Pending` + `FailedScheduling`）。CSI 驱动根本不会被调用，本用例承诺的 ContainerCreating 现象永远无法复现
- 正确机制：PV **不带任何拓扑约束**让调度正常通过，`volumeHandle` 指向不存在/不可用的云盘，使 CSI 驱动在 **attach 阶段**失败，Pod 才会卡在 ContainerCreating

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群 CSI 驱动（如 `diskplugin.csi.alibabacloud.com`）在位且正常工作：`kubectl get csidriver` 列出驱动对象、`kubectl get pod -n kube-system -l app=csi-plugin` 确认组件 Running（反证条件：集群无磁盘型 CSI 驱动时本用例不可达，勿强行注入）
3. **靶选型（guard 契约约束，定案免推导）**：注入对象是已存在的 workload 或专用靶——**专用靶可带内自建**：单文档 Deployment manifest 过 drill-target 契约门即放行（`spec.template.spec` 恰一个容器且无 initContainers、无 hostNetwork/hostPID/hostIPC/privileged/capabilities/hostPath 特权面、镜像 ∈ 载体镜像允许集、卷仅 persistentVolumeClaim/configMap/secret；注意容器/卷/host 标志在 `spec.template.spec` 内层非 spec 直层），且 manifest 名字必须等于 FaultSpec 批准靶名、命名空间一致（名字锚定，相似名/前缀变体拒）；违约是可重塑的 form issue（修清单重 apply 同一 manifest 即过，非机制禁令）。合规模板与选型指引见 `references/target/drill-target.md` 标准件。靶形态要求：单副本、无既有卷挂载（注入的假卷是唯一变量）、镜像节点缓存可靠。带外预建仍为合法备选（复杂靶形态或意图不动时）；其他 workload kind（StatefulSet/DaemonSet/Job 等）仍是机制级禁令——对已存在的此类 workload 注入是合法路径
4. **守卫 scope 判型（同域已验立法直接引用）**：本用例写入集含 PV（cluster-scoped）创建——意图须按目标 Deployment 叙述使 scope 判为 workload（workload 网含 pv/persistentvolume，cluster-scoped 豁免 ns 检查后放行）；执行身份须有 `create persistentvolumes` 权限（`kubectl auth can-i create persistentvolumes` 预检）
5. **靶可调度性免推导（#38 四测实测立法，资源探测段动作指引）**：专用靶（裸靶——无 nodeSelector/affinity，本用例 PV 明确不带 nodeAffinity）的可调度性**无需探测节点 taint 拓扑**——常驻靶名册 Running（drill-hpa-target / drill-pvc-target 等）即集群可调度的存在性证明，默认调度器自动避开污点节点。**勿在 planning 段拉全量节点 taint 列表做拓扑推演**（四测证据：立法落在通用件 drill-target.md 时 taint 探测仍被发出——调用决策先于立法到达上下文的时序击穿；本条立法落在本 case 首读位置后先于任何探测调用决策生效）；计划产物中的调度定案照写一句「无 taint 约束、多节点集群可调度」即可

**演练步骤**（主路径 = 步骤 1 的基线读取（maxUnavailable 值——restorePatches 基线值来源，两条路径共用）→ 步骤 2 创建 PV/PVC 道具（两路径共用，execute 计划 manifest 步骤）→ 调 faultdrill_assemble_carrier（参数取自载体配方：target_kind=deployment、target_name=<deployment-name>、target_namespace=<namespace>、patches=…（add volumes/volumeMounts + MU 100% 注入域）、restorePatches=…（remove volumes/volumeMounts + MU 基线 replace）、duration_seconds=<duration>），注入+武装+readback 工具内同步完成——步骤 1 的写入部分与步骤 5 的手动模板 patch 均由配方承载，无需单独执行 → 步骤 6 等待滚动更新完成（两路径共用）→ 步骤 8 观察现象（两路径共用）。步骤 3-4 的五件套载体序列与步骤 1 写入部分/步骤 5/7 的手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. （仅 Deployment 目标）记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）——**降级路径形态；主路径下读取部分（restorePatches 基线值来源）两条路径共用，写入 100% 由载体配方注入域承载，无需单独执行**：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
   StatefulSet 目标跳过此步，见「StatefulSet 目标工作负载」一节。
2. 使用 `kubectl(subcommand="apply", v_args="-f -", stdin_data="...")` 创建 PV 和 PVC（**必须使用 `stdin_data` 参数传入 YAML 且必须带 `-f -`，不要用 exec heredoc 或其他方式**；**PV 与 PVC 必须分两次 apply（先 PV 后 PVC），严禁合并为一个多文档 manifest**——多文档混 kind 调用的 scope 会塔缩到第一个 kind，第二 kind 的对象逃过身份对账（守卫混 kind 门禁按此立法拒绝，form issue）；分拆后各归各账：PV 走 workload 网 cluster-scoped 豁免，PVC 走 frontmatter mechanism_writes 立法条目名字级对账）：
   第一次 apply，PV YAML（**无 nodeAffinity、无 storageClassName、volumeHandle 为不存在的云盘 ID、reclaimPolicy 显式 Retain**）：
   ```yaml
   apiVersion: v1
   kind: PersistentVolume
   metadata:
     name: archive-vol-chaos
   spec:
     capacity:
       storage: 20Gi
     accessModes: ["ReadWriteOnce"]
     persistentVolumeReclaimPolicy: Retain
     csi:
       driver: diskplugin.csi.alibabacloud.com
       volumeHandle: <不存在的云盘ID，如 d-fake-chaos-vol-001>
       fsType: ext4
   ```
   Retain 是静态 PV 默认值，显式声明防外部 defaulting 干扰；Delete 策略下 provisioner 会尝试删除底层假盘，徒增失败噪声
   注意：`fsType` 必须缩进在 `csi:` 层级下（`spec.csi.fsType`）；若放在 `spec` 顶层则是未知字段，
   kubectl apply 的 strict 字段校验会拒绝整个 PV 创建（unknown field "fsType"），
   表现为 PV 创建失败、PVC 永远 Pending（无 SC 的 PVC 等不到目标 PV）
   第二次 apply，PVC YAML（**storageClassName 必须为空字符串**，静态绑定无 SC 的 PV，避免动态供给或 WaitForFirstConsumer 干扰）：
   ```yaml
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata:
     name: archive-vol-claim
     namespace: <namespace>
   spec:
     accessModes: ["ReadWriteOnce"]
     storageClassName: ""
     resources:
       requests:
         storage: 20Gi
     volumeName: archive-vol-chaos
   ```
3. **先武装定时恢复，再修改模板**（**降级兜底形态——主路径下由装配器载体 TTL 承载 patch 域还原、道具清理走收尾步，无需手动建栈武装**；到期自动移除注入的 volumes/volumeMounts 并清理 PV/PVC，补齐自恢复能力）。按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈，四对象同名 `drill-rc-<hash>`、全部建在被批准的靶点命名空间内，SA/Role/RoleBinding 必须用 `kubectl create` 命令构造。**本用例 RBAC 是五件套变体**（恢复动作跨 namespaced 与 cluster-scoped 两类资源）：deployment 模板还原与 PVC 清理是 namespaced 写（Role），PV 清理是 cluster-scoped 写（**必须 ClusterRole——namespaced Role 写 PV 规则对 SA token 不生效**）。恢复动作 verb×resource：`deployments get,patch` + `persistentvolumeclaims get,delete` + `persistentvolumes get,delete`
   （载体 Pod 五条件、镜像允许集、调度容忍 overrides 形态见标准件第一节；骨架时长 = 故障窗口 + 1800s 取证缓冲（2026-09-17 立法升级：#46/#48/#51 三现 restore.log 不可回读——600s 不够 fire 后带外取证节奏，1800s 覆盖「发现异常→诊断链→取证」全程））
4. （载体验权后）武装定时器（**降级兜底形态**）：恢复合三个 REST 动作，**curl 顺序即执行顺序，PVC 先删、PV 后删**（PVC 存在时 PV 删除会被绑定关系阻塞），用紧凑变量形态的逐 curl 变体（变量赋值与引用全部写在 `sh -c` 载荷内）。`<duration>` 需覆盖滚动更新与观察窗口：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); D=https://kubernetes.default.svc; curl -s -X PATCH --cacert $C -H "Authorization: Bearer $T" -H "Content-Type: application/strategic-merge-patch+json" -d "{\"spec\":{\"strategy\":{\"rollingUpdate\":{\"maxUnavailable\":<基线或null>}},\"template\":{\"spec\":{\"volumes\":<null或基线数组>,\"containers\":[{\"name\":\"<容器名>\",\"volumeMounts\":<null或基线数组>}]}}}}" $D/apis/apps/v1/namespaces/<ns>/deployments/<name>; curl -s -X DELETE --cacert $C -H "Authorization: Bearer $T" $D/api/v1/namespaces/<ns>/persistentvolumeclaims/archive-vol-claim; curl -s -X DELETE --cacert $C -H "Authorization: Bearer $T" $D/api/v1/persistentvolumes/archive-vol-chaos ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （自断授权尾步——标准件第四节立法：**最后一个**恢复 curl（假 PV DELETE）升 `curl -sf` 并以 `&&` 链自删 DELETE 自己的 Binding（复用 `$C`/`$T`/`$D` 变量；**本用例是五件套六对象栈——两个 Binding 都删**：`&&` 链 RoleBinding 在前、ClusterRoleBinding 在后，两个 DELETE 均带 `-sf`，任一失败即停剩余授权交收车六连删除；前序动作保持 `;` 串联不中断；本用例恢复动作已含 DELETE PVC/PV——尾步自删 Binding 是另一对象域，互不干扰），建栈后按标准件第二节两步建栈法 + **六对象栈双自删形态**，Role 与 ClusterRole 各 json-patch 追加一条独立自删规则（Role 的规则锁名删自己的 RoleBinding、ClusterRole 的规则锁名删自己的 ClusterRoleBinding；**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行；字节挤不下两个 DELETE 时按标准件第二节优先删 ClusterRoleBinding——集群级授权残留面更大。**本模板预算定案（免现场重算，2026-09-17 实算）**：本用例三动作恢复载荷代入典型值已约 800B，双删满尾步实测约 1061B——**超 1024B 硬上限 37B，双删不可行，定案走单删 ClusterRoleBinding 形态**（约 938B 限内；Role 自删规则相应不追加，RoleBinding 交收车幂等清）。fail-open：主恢复未确认成功则授权保留——完整形态与五纪律以标准件第四节为准）
   （deployment 还原的 Content-Type 用 **`application/strategic-merge-patch+json`**：`containers` 数组按 `name` 合并键合并——`volumeMounts: null` 只删该字段、`image` 等未提及字段天然保留；`volumes` 基线为空用 null 删键、非空写基线完整数组，规划期已知基线直接代入；maxUnavailable 还原合入同一次 patch。**禁用 `application/merge-patch+json` 承载本 patch**：RFC 7386 对数组是整组替换语义，`containers:[{"name":...,"volumeMounts":null}]` 会把容器替换成无 `image` 的对象，API 以 `image: Required value` 原子拒绝整个 PATCH（curl -s 对 422 无感、错误只落 restore.log），而 DELETE 独立生效——timer 照常删 PVC/PV 但模板未还原，形成「卷在模板→Pod 挂 PVC→PVC 等 Pod 释放」死锁，恢复悬空至人工介入。武装前必须先过标准件第三节 SA 真实 token 验权（deployment GET 200 且 PV GET 200/404——403 即中止），倒计时从武装时刻起算、与第一个注入动作紧邻；武装后发生任何修复须先停旧定时器再全额重武装）
5. 修改应用 A 的工作负载模板，添加引用该 PVC 的 volume 和 volumeMount——**降级路径形态；主路径下注入 patch（add volumes/volumeMounts + MU 100%）由装配器工具内同步执行**
6. （Deployment）等待滚动更新完成，确认所有旧 Pod 已被替换；（StatefulSet）删除目标 Pod 触发重建
7. （仅 Deployment）滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）——**降级路径形态；主路径下此项随载体配方 restorePatches 由载体 TTL 还原，无需手动执行**
8. 观察 Pod 的 ContainerCreating 状态

**StatefulSet 目标工作负载**：
- StatefulSet 没有 maxUnavailable；滚动更新控制参数是 `spec.updateStrategy.rollingUpdate.partition`
- partition 语义：**ordinal >= partition 的 Pod 使用新模板，ordinal < partition 的保持旧模板；丢失 Pod 的重建同样遵循此规则**
- 由此推出硬约束：**无法隔离"仅中间单个 Pod 受影响"**——patch 模板后，目标 Pod 与所有更高 ordinal 的 Pod 都会用新模板重建（从高 ordinal 向低 ordinal 依次进行）
- 爆炸半径声明必须按实际受影响的 ordinal 区间如实告知用户，严禁声称 target-only
- 新 Pod 永不 Ready 时 StatefulSet 滚动更新同样会卡住（控制器等待新 Pod Ready 才继续），这是本故障的预期现象，无需也不可用 maxUnavailable 类技巧绕过

**注入验证**：
1. （Deployment）确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 永不 Ready（卡在 ContainerCreating 卷挂载失败，故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 处于 ContainerCreating）
2. 执行 `kubectl get pods`，确认目标 Pod 状态为 ContainerCreating
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 FailedMount 或 FailedAttachVolume 或 CSI attach 超时
   （**事件时间戳形态注**：部分集群 events 呈新式字段形态——lastTimestamp 为 null、count 为空，属 API 字段差异**非数据缺失**；判读以 eventTime 与 series.lastObservedTime 为准，直接解读即可，无需交叉推算重建时间线）
   （**sustained 判据一次采样等价形态（#38 三测实测立法）**：「Pod 实时 ContainerCreating（现在时证据）+ FailedMount count ≥ 5（过去时证据——count 是重试次数的积分，实测 2m31s 时 count 已达 9，重试退避从密到疏）」一次采样即等价「两次采样证持续」，无需等待窗口后二次采样——本故障机制为确定性失败（假盘 ID 恒 NotFound，不存在间歇性成功的物理路径），count 高即已编码「持续失败 ≥1 分钟」；两次采样能提供的唯一增量信息「采样后未恢复」属 recover 阶段职责，不是 verify 义务）
4. **如果观察到 Pending + FailedScheduling（事件含 `volume node affinity conflict`），说明 PV 带了 nodeAffinity，机制错误——不可判定为 verified，必须删除 PV/PVC 并按本用例模板（无 nodeAffinity）重新注入**

**注入恢复**（主路径下模板还原无需 Agent 执行动作——载体 TTL 自治还原（restorePatches 的 remove volumes/volumeMounts + MU 基线 replace 由载体内 timer 到点执行，恢复自动触发回滚滚动；fire 证据落载体 /tmp/restore.log + 任务台账 recovery_handle）；演练提前结束时 blade-ai recover 从台账重放同源配方提前收敛，与载体幂等双执行。道具 PVC/PV 清理为收尾步（非 patch 域动作，两路径共用——第 5-6 步）；降级路径下载体 timer 三动作载荷兼清理 PVC/PV。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动移除注入的 volumes/volumeMounts 并清理 PV/PVC（主路径下载体 TTL 只还原 patch 域，PVC/PV 清理交收尾步第 5-6 步）；如需提前恢复，Agent 直接执行下列第 2–6 步恢复命令（幂等，定时器迟到再执行一次无副作用）
2. 恢复应用 A 的工作负载模板，移除注入时添加的 volumes 和 volumeMounts（两者都需移除，只移除其中一个会导致配置错误）
3. 等待 Pod 滚动更新/重建完成，确认 Pod 恢复 Running
4. （Deployment）还原 maxUnavailable 为演练前记录的原始值
5. 清理测试 PVC：`kubectl delete pvc archive-vol-claim -n <namespace>`（**先于 PV**——PVC 存在时 PV 删除被绑定关系阻塞）——**收尾步（两路径共用，非 patch 域动作）**
6. 清理测试 PV：`kubectl delete pv archive-vol-chaos`；**若 PV 卡 Terminating**（CSI external-attacher 的 finalizer 因假盘 detach 无法正常释放），json patch 移除 finalizers 是标准兑底：`kubectl patch pv archive-vol-chaos --type='json' -p '[{"op":"remove","path":"/metadata/finalizers"}]'`（此时底层盘本就不存在，无真实数据风险）——**收尾步（两路径共用，非 patch 域动作）**

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running
2. 确认测试资源已清理
3. 确认应用 A 存储功能正常

**基准事实**：
- **根因**：CSI 驱动 attach/mount 失败（云盘不存在或不可用），Pod 无法完成存储卷挂载
- **必现现象**：Pod ContainerCreating；Events 显示 FailedMount/FailedAttachVolume/CSI 超时
- **必不出现**：若 PV 带 nodeAffinity，现象退化为 Pending + FailedScheduling（调度器前置过滤），CSI 流程未被触发——这不是本用例的合格形态
