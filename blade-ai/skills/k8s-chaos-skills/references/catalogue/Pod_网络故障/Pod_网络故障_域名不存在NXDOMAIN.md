---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 Corefile 配置），路由进程序化
# 恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（执行凭证无 cm patch/kube-system
# 建权不可授/RBAC 不可授/验权 403）时降级正文 SOP 路径（三权路由 A/B）。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本用例的故障机制需要写受害者
# 覆盖之外的对象。由 case 作者在此声明，确定性代码在意图定案时装载，确认卡
# 渲染、人工批准后冻结进守卫快照。LLM 无权扩写。同命名空间辅助资源无需
# 声明（secondary_scopes 已覆盖）；条目只约束受害者覆盖之外的跨域写。
# 主路径装配器四件套（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash>，全落
# 靶 ns kube-system）由工具内程序化构建——构造保证 + fail-closed 内嵌检查
# （RBAC 从 restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），
# 不经 LLM kubectl 写面（design ND3：程序化路径不复用 LLM 守卫），无立法条目；
# 下列条目全部服务降级路径的 LLM kubectl 写面。
mechanism_writes:
  # 降级 SOP 路径 A：LLM 直笔 patch 共享 Corefile（CoreDNS 配置本体；发行版名
  # coredns/kube-dns）的注入/还原写面——主路径下该 patch 由装配器工具内执行，
  # 不经 LLM 写面
  - scope: configmap
    namespace: kube-system
    names: [coredns, kube-dns]
  # 路径 B：独立 NXDOMAIN ConfigMap（瞬态对象；前缀约定与正文命令共享）
  - scope: configmap
    namespace: kube-system
    name_prefix: "drill-nxdomain-"
  # 两条路径：rollout restart / strategic patch CoreDNS Deployment——生效触发
  # 与路径 B 注入是 execute 计划普通步骤，走 LLM kubectl 写面
  - scope: deployment
    namespace: kube-system
    names: [coredns, kube-dns]
  # 降级 SOP 的恢复载体跨 ns 标准件：SA 与载体 Pod 建在靶 ns，Role/RoleBinding
  # 落 kube-system（把恢复动词授予靶 ns 的载体 SA）——LLM 手动建栈写面立法
  # （主路径装配器四件套全落靶 ns kube-system 且程序化构建，免本条目）
  - scope: role
    namespace: kube-system
    name_prefix: "drill-rc-"
  - scope: rolebinding
    namespace: kube-system
    name_prefix: "drill-rc-"
---

**用例名称** 域名不存在NXDOMAIN 导致 Pod_网络故障

**故障定位**：持续型故障——NXDOMAIN 规则（Corefile template 插件块）是状态型故障，
规则存活即故障存活，贯穿整个故障窗口；窗口到期定时器还原配置（`reload` 插件热加载
生效，无 reload 时滚动重启）即自动恢复。
本用例为**单手段用例（kubectl-native）**：ChaosBlade 的 `pod-network dns` action 只能把域名
劫持到指定 IP（那是 `Pod_网络故障_DNS劫持` 用例），**没有返回 NXDOMAIN rcode 的 等价
action**，语义不等价。手段按**当前执行凭证的 RBAC** 路由：主路径 = 装配器载体
配方（路径 A 形态——见下方载体配方；须 kube-system 建权 + cm patch），装配
不可用时降级正文二选一路径（`kubectl auth can-i` 直查，见资源准备第 5 条）：
- **路径 A（改 coredns ConfigMap）**：当前凭证有 `patch configmaps -n kube-system` 权限时
  优先——只动一个对象，还原链最短
- **路径 B（独立 NXDOMAIN ConfigMap + Deployment `-conf` 追加）**：当前凭证**无** cm patch
  权限但有 `create/delete configmaps` + `patch deployments`（均为 `-n kube-system`）时启用——
  利用 CoreDNS `-conf` 支持多配置文件的原生能力，**完全不碰集群共享的 coredns ConfigMap**。
  托管集群常态：执行凭证往往 deployments patch + cm create/delete 齐备却**没有** cm
  patch——路径 A 死路而路径 B 通路，RBAC 探测必须三权全查，只查一项会误判
  （见资源准备第 5 条）

`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认；窗口须覆盖滚动重建耗时
（2 副本滚动重启约 30~60 秒），不建议低于 300 秒。**地板语义**：
按用户给定值声明（如 300）即可——300s 安全地板是兜底（backstop），显式恢复步骤按声明
窗口执行（声明 300 → ~300s 恢复，地板与声明重合，不额外延长窗口）；不要把地板值当作窗口
本身重新声明（整窗采用 300 与声明更短窗口+300 兜底两种解读中，后者更忠实用户意图）。

⚠️ **爆炸半径与通道安全（集群级影响，硬性约束）**：本故障作用于集群共享 DNS——窗口内
**全集群**所有经 CoreDNS 解析该域名的 Pod 都受影响，不只是验证探针所在的应用。因此：
1. 目标域名**必须**是外部业务域名，**严禁**选择控制通道/平台组件依赖的域名（控制通道
   若依赖目标域名做解析（如命令执行前的工件上传），故障窗口内其命令——含恢复命令——
   全部不可达；控制通道依赖被攻击域名会形成拓扑死锁）
2. 注入验证探针的查询目标与控制通道依赖域名必须不同
3. 本用例的故障形态（仅特定域名 NXDOMAIN，CoreDNS 服务本身存活）不会造成全量解析瘫痪，
   与 `Pod_网络故障_CoreDNS异常`（副本缩零、全量瘫痪、死锁高风险）有本质区别，但通道
   域名禁忌同样适用
4. ⚠️ **执行守卫边界（write-set 契约，硬性前置）**：两条路径的注入与还原都是 **kube-system
   写**（ConfigMap patch/create/delete + Deployment patch），超出受害者自身覆盖——本用例
   因此在文件头以 `mechanism_writes` frontmatter 立法声明机制写入集（无该契约时
   载体探测与注入 patch 会被 REJECT_DRIFT 拒绝、注入无法落地，
   此为立法动因）。守卫按「受害者 ∪ 机制条目」冻结快照放行（名字子集/前缀匹配；
   manifest 之外的 kube-system 写仍拒）。无人值守 CLI 对扩权契约走 AUTO 委托自动批准
   （授权时点 = manifest 立法入库时，运行时执法 = 守卫 per-name union fail-closed；
   无人值守通道发可审计 auto_approved 事件，交互通道 TUI/--confirm 仍弹卡供人过目）。经靶 Pod SA 洗白 manifest
   之外的 kube-system 写属于守卫绕过，禁止


**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 Corefile 配置；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，**全落靶 ns kube-system**，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权（GET 探测 + 写动词 SSAR 全查）→ 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告。装配不可用形态（对号入座即降级正文 SOP）：执行凭证无 kube-system 建权（栈构建 fail-closed 报告会精确指出缺哪权）/ 无 cm patch（注入 partial——载体保留至 TTL 对未注入靶标 no-op fire 后自然收敛，降级前按 receipt 的 recovery_handle 处置载体）/ SSAR 403）：

```yaml
targetRef:                                # 靶（kube-system 基础设施对象；主路径建模路径 A 形态）
  kind: ConfigMap
  name: coredns                           # 发行版差异：kube-dns 同形态换名
  namespace: kube-system
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
# Corefile 是单键整文件——replace 全文值 = 基线全文 + 顶层独立 template NXDOMAIN
# server block 追加文件末尾（与 .:53 块平级，严禁嵌入 .:53 块内部——插件链冲突
# 会使 CoreDNS BackOff crash；文本替换式注入的锚点陷阱见资源准备第 4 条，
# 必须由 Agent 从原文构造注入后完整 Corefile）
- op: replace
  path: /data/Corefile
  value: <基线 Corefile 全文 + 顶层独立 template NXDOMAIN server block>
restorePatches:                           # 恢复域：载体 TTL 到点执行；Agent 死亡后 recover 重放同源
- op: replace
  path: /data/Corefile
  value: <注入前记录的基线 Corefile 全文>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- **基线对账的机械化防线**：装配器基线校验要求 restorePatches 的 value 逐字等于活体对象当前值（restore 值 ≠ 活体即中止——armed timer would mutate the target, not restore it），Corefile 全文 replace 形态下即「注入前原文必须逐字精确」；资源准备第 4 条锚点陷阱（块形 forward 静默失配）由该防线拦成显式中止而非无声漂移。
- 目标是 kube-system 基础设施对象：targetRef.namespace 写 kube-system；装配器四件套全落 kube-system（与靶同 ns），执行凭证需 kube-system 建权（create serviceaccounts/roles/rolebindings/pods + 载体 exec）——托管集群常态不可授，即装配 fail-closed 降级正文路径；降级路径的跨域写继续受 frontmatter mechanism_writes 立法条目约束（LLM 写面）。
- CoreDNS 生效触发（reload 热加载等待 ≤2 分钟，无 reload 时 rollout restart）保留为 execute 计划普通步骤——它不是恢复动作，主路径与降级路径共用。
- **通道安全前提（主路径延续成立）**：装配器载体 TTL 恢复载荷走 `https://kubernetes.default.svc`——该域名由 `.:53` 块的 kubernetes 插件直接应答，不受 template NXDOMAIN 影响（目标域名必为外部域名的硬约束反向保证），载体恢复通道不因本故障不可达（与 `Pod_网络故障_CoreDNS异常` 的装配器死锁形态的本质区别——那边故障摧毁的就是解析链路本身）。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作（生效触发 rollout、路径 B 全域）保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. 特定域名解析返回 NXDOMAIN（域名不存在），而非解析到错误 IP（那是 DNS 劫持）
2. 依赖该域名的应用连接失败，日志出现 `Name or service not known` / `Could not resolve host`
3. 与 CoreDNS 完全不可用不同：仅针对特定域名失败，其他域名解析正常，CoreDNS Pod 全部健康
4. 模拟外部服务域名过期/DNS 记录误删场景

**资源准备**：

**探测速查（一轮并行下发）**：选型只依赖下列五项输入，互相独立、全部只读——可同轮批查，勿分多轮串行探索（各探测命令见本节对应条目正文）：

| 输入 | 决定 | 锚点 |
|---|---|---|
| 凭证 RBAC 三权+装配器建权面+两权 | 主路径装配器面 + 路径 A/B 降级选型 + 跨 ns 载体可行性 | 第 5 条 |
| Corefile 原文全文（含 forward 形态、reload 在位与否） | 注入/还原构造基准 + 生效路径 | 第 4 条 |
| CoreDNS 副本数/标签/容器名/args | 白盒判据 + 路径 B 锚点 | 第 2、3 条 |
| 靶容器工具面（nslookup/dig/curl 哪个在） | 验证判据形态 | 第 6 条 |
| 载体镜像候选（节点缓存清单 → DaemonSet 全覆盖 → 批准靶标在用时可 exec 验工具链，判别法见标准件第九节） | 载体 `--image` | 标准件第九节 |

1. 确认应用 A 正常运行，且依赖特定域名进行服务调用；确认其 Pod 名、命名空间与
   **实际容器名**（多容器/临时调试容器混存时 `kubectl exec` 必须显式 `-c <容器名>`）
2. 确认 CoreDNS Deployment 正常运行（不同发行版叫 `coredns` 或 `kube-dns`），记录副本数：
   ```bash
   kubectl get deployment coredns -n kube-system -o jsonpath='{.spec.replicas}'
   ```
3. 获取 CoreDNS Pod 选择器标签与容器启动参数（部分发行版为 `component=coredns` 而非
   `k8s-app=kube-dns`，以当次探测为准；启动参数决定路径 B 的 `-conf` 追加锚点）：
   ```bash
   kubectl get deployment coredns -n kube-system -o jsonpath='{.spec.selector.matchLabels}'
   kubectl get deployment coredns -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}'
   ```
   路径 B 要求启动参数是 `-conf /etc/coredns/Corefile` 单文件形态（追加第二个 -conf 值即可）；
   若已是多文件或目录形态，按实际 args 结构评估追加点，无法安全追加时路径 B 判死
4. 记录当前 Corefile 配置**原文全文**（路径 A 的注入锚点与两条路径的白盒对照都用）：
   ```bash
   kubectl get configmap coredns -n kube-system -o jsonpath='{.data.Corefile}'
   ```
   ⚠️ **锚点陷阱**：`forward . /etc/resolv.conf` 在不同集群有两种形态——
   单行 `forward . /etc/resolv.conf` 与**块形** `forward . /etc/resolv.conf { ... }`（ACK
   定制版为块形且带 `prefer_udp` 子配置，插件链还含 `k8s_event`/`kubeapi` 等非标准插件）。
   任何按单行形态写死的文本替换（如 jq `sub` 单行模式）在块形集群上会**静默失配**——替换
   找不到模式时返回原文，apply 后无任何变化，故障不出现且无报错。因此路径 A 禁止文本
   替换式注入，必须由 Agent 从原文构造**注入后的完整 Corefile**（template 块插入在
   `kubernetes` 插件块之后、`forward` 块之前）整体 merge patch 写入，注入后回读全文比对
   确认 template 块在位
5. **RBAC 前置检查（决定路径选型）**：用当前执行凭证跑 `kubectl auth can-i`
   **三权全查**（只查一项会误判路径——常见组合是 `deployments patch=yes` 而
   `configmaps patch=no`，单看前者会误选路径 A 导致注入 Forbidden）：
   ```bash
   kubectl auth can-i patch configmap coredns -n kube-system
   kubectl auth can-i create configmaps -n kube-system
   kubectl auth can-i delete configmaps -n kube-system
   kubectl auth can-i patch deployments -n kube-system
   ```
   RBAC 检查结果路由（主路径优先）：`cm_patch=yes` 且 kube-system 建权可授
   （装配器四件套全落 kube-system——`create serviceaccounts`/`create roles`/
   `create rolebindings`/`create pods` 四权各查一次 + 载体 `exec pods`）→
   装配器主路径（载体配方）；任一不可授 → 装配器 fail-closed 降级正文路径：
   `cm_patch=yes` → 路径 A（LLM 直笔优先）；`cm_patch=no` 且 `cm_create=yes` +
   `cm_delete=yes` + `deploy_patch=yes` → 路径 B。
   ⚠️ **恢复定时器宿主（降级 SOP 素材——主路径装配器载体 TTL 自治承载免手动建栈；先武装定时恢复，再注入——见各路径步骤 2）**：本用例恢复对象在
   kube-system（超出靶 ns），定时器按**恢复载体标准件「跨命名空间变体」**承载（`references/carrier/recovery-carrier.md` 第十节）——SA 与载体 Pod 建在靶 ns，Role/RoleBinding 落 kube-system（恢复动词授予载体 SA；本用例 frontmatter 的 mechanism_writes 已立法 role/rolebinding @ kube-system + drill-rc- 前缀，无需运行时扩权）。**通道安全前提**：载体恢复命令走 `https://kubernetes.default.svc`——该域名由 `.:53` 块的 kubernetes 插件直接应答，不受 template NXDOMAIN 影响（目标域名必为外部域名的通道安全约束反向保证），载体的恢复通道不因本故障不可达。跨 ns 变体的执行凭证硬门（与上方三权一并查）：
   ```bash
   kubectl auth can-i create roles -n kube-system
   kubectl auth can-i create rolebindings -n kube-system
   kubectl auth can-i patch roles -n kube-system
   ```
   前两权任一为 no 时不可强建载体（走第八节降级路径——任务如实失败收尾，不得注入）；第三权为 no 时启用自断授权尾步则两步建栈第二步（json-patch 追加自删规则）追加不上、timer 自删 DELETE 必 403——尾步降级为不带（授权清退交收车路径，不影响主恢复）。备选宿主：集群内确有具备 kubectl 与集群凭证的长寿工具 Pod 时可直接武装于该宿主（恢复所需 RBAC 以宿主内 `can-i` 探测为准——注意宿主 SA 权限不足时还原因 Forbidden 静默失败，输出被 `>/dev/null 2>&1` 丢弃，表面 armed 实际永不还原）
   ⚠️ **机制锁定（严禁降级改机制）**：任何受阻情况下（守卫拒绝、宿主不可用、RBAC
   受限），**严禁**降级为靶 Pod 内 DNS 劫持类手段（改 /etc/resolv.conf 指向自建
   responder、socat UDP 代理、/etc/hosts 注入等）——那是**另一个故障**（单 Pod DNS
   配置错误，效果面只有靶 Pod，其余集群 Pod 不受影响；本 case 机制是集群级 CoreDNS
   行为变更），且 socat UDP responder 对 curl（c-ares 解析器）不生效。受阻即降级**恢复宿主**（换备选宿主或标准件跨 ns 变体——恢复命令幂等；宿主与标准件均不可用时按第八节收尾，不得注入），机制本身不降级
6. 确认目标域名当前可正常解析（基线）。判据工具**以验证探针容器实际具备的为准**——
   精简业务镜像常态只有 `curl`（无 nslookup/dig/wget）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'which nslookup dig curl 2>/dev/null; true'
   ```
   - 有 nslookup：`nslookup <target-domain>`，记录解析 IP
   - 只有 curl：`curl -sv --max-time 5 http://<target-domain> -o /dev/null`，从 `*   Trying <IP>:80`
     行读取解析 IP 作为基线

**演练步骤**（主路径 = 基线捕获（步骤 1 + 资源准备第 4 条 Corefile 原文——restorePatches 基线值来源，两条路径共用）→ 资源准备第 5 条 RBAC 探测（主路径装配器面：kube-system 建权四权 + cm patch；不可授即降级三权路由）→ 调 faultdrill_assemble_carrier（参数取自载体配方：target_kind=configmap、target_name=coredns、target_namespace=kube-system、patches=…、restorePatches=…、duration_seconds=<duration>），注入+武装+readback 工具内同步完成 → 生效触发（reload 等待/rollout）为 execute 计划普通步骤接续。路径 A 步骤 2-4 的手动载体标准件序列与路径 B 全域仅当装配器 fail-closed/partial 报告不可用时作降级兜底——三权路由（cm_patch=yes → 路径 A；cm_patch=no 且 cm_create+cm_delete+deploy_patch=yes → 路径 B）即降级路由表）：

1. 记录注入前基线（两条路径共用）：
   ```bash
   kubectl get pods -n kube-system -l <coredns-label> -o wide
   ```
   记录 CoreDNS Pod 列表（恢复验证对照）；路径 A 另需步骤 4 的 Corefile 原文，路径 B 另需
   步骤 3 的 args 原文（还原 patch 的对照基准）

**路径 A —— 改 coredns ConfigMap（降级形态：装配器不可用且执行器 `cm_patch=yes` 时优先）**

**执行序列速查**（四件套首查 NotFound 是预期初始态——不存在即直接创建，不是错误；命令全文与警示见本节正文）：基线读取（Corefile 原文 + CoreDNS Pod 列表）→ create sa（靶 ns）→ create role（kube-system）→ create rolebinding（kube-system）→ kubectl run 载体（overrides 挂 SA + tolerations，完整序列见标准件第十节）→ 载体 Running 确认 → 验权 GET 200 硬门 → 武装定时器（restore 脚本 base64 折叠 + `sh -n` 校验）→ 注入 merge patch → 白盒回读比对

2. **先武装定时恢复，再注入**。还原命令（reload 在位时单命令：merge patch 原文
   还原、`reload` 热加载生效（约 ≤2 分钟，无 Pod 重建）；reload 不在位时追加
   滚动重启。`<restore-json>` 为 `{"data":{"Corefile":"<步骤 4 原文，JSON 转义>"}}`，
   由 Agent 从原文构造）。定时器按恢复载体标准件跨 ns 变体承载（资源准备第 5 条）：
   ```bash
   # 载体栈四件套（SA/Pod 在靶 ns；Role/RoleBinding 在 kube-system——恢复动词
   # 落在被写对象所在 ns 才有效力；verb 清单按钦定恢复形态推导，换形态时以
   # 恢复脚本实际载荷动词为准重建——见标准件第二节形态无关总则、第三节 SSAR 对账）
   kubectl create sa drill-rc-<hash> -n <namespace>
   kubectl create role drill-rc-<hash> -n kube-system \
     --verb=get,patch --resource=configmaps \
     --verb=get,patch --resource=deployments
   kubectl create rolebinding drill-rc-<hash> -n kube-system \
     --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=<允许集内镜像> \
     --restart=Never --overrides='<SA 挂接与 tolerations，见标准件第一节>' \
     --command -- sleep <骨架时长>
   # 验权（载体 SA 真实 token 的只读 GET，200 才继续）
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c \
     'curl -s -o /dev/null -w "%{http_code}" --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" https://kubernetes.default.svc/api/v1/namespaces/kube-system/configmaps/coredns'
   ```
   还原脚本 REST 翻译（载体内无 kubectl；Corefile 原文较长，按标准件第七节用
   base64 折叠写脚本文件，写完 `sh -n` 校验再武装）：
   ```sh
   # <restore-script>：单条 merge patch（reload 在位时）；
   # reload 不在位时追加 rollout 注解 patch（strategic-merge-patch+json，
   # body {"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":"<ts>"}}}}}
   # → https://kubernetes.default.svc/apis/apps/v1/namespaces/kube-system/deployments/coredns）
   curl -s -X PATCH \
     --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
     -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
     -H "Content-Type: application/merge-patch+json" \
     -d '<restore-json>' \
     https://kubernetes.default.svc/api/v1/namespaces/kube-system/configmaps/coredns
   ```
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'echo <restore-script-b64> | base64 -d > /tmp/blade-restore-nxdomain.sh; sh -n /tmp/blade-restore-nxdomain.sh && ( sleep <duration>; sh /tmp/blade-restore-nxdomain.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   载体构建失败时按标准件第八节降级（不反复重试绕行；任务如实失败收尾，不得注入）
3. 注入——Agent 从步骤 4 原文构造注入后的完整 Corefile（template 块**作为顶层独立
   server block 追加在文件末尾，与 `.:53` 块平级**；⚠️ 严禁嵌入 `.:53` 块内部——
   嵌入 `kubernetes` 块之后的位次会因插件链冲突导致 CoreDNS BackOff
   crash，顶层独立 server block 形态可干净启动；「位于 kubernetes 插件之后」的位次
   约束在顶层形态下天然满足），整体 merge patch 一次写入（无中间文件、无顶层管道；
   **严禁**文本替换式注入，见资源准备第 4 条锚点陷阱）：
   ```bash
   kubectl patch configmap coredns -n kube-system --type=merge \
     -p '{"data":{"Corefile":"<注入后完整 Corefile，JSON 转义>"}}'
   ```
   template 块形态：
   ```
   <target-domain>:53 {
     template IN A <target-domain> {
       rcode NXDOMAIN
     }
   }
   ```
   说明：**目标域名必须是外部域名**——cluster.local 内域名由 `.:53` 块的 kubernetes
   插件直接应答，独立 server 块的 template 拦不到，注入无效
4. 使配置生效——**reload 热加载优先，rollout 兜底**：ACK 定制 Corefile
   默认带 `reload` 插件（每 ~30s 检测挂载文件变化并热加载）——在位时**无需 rollout
   restart**：patch ConfigMap 后等 kubelet 同步 + reload 检测（合计约 ≤2 分钟，无
   Pod 重建、无服务中断窗口；生效判据 = 从靶 Pod 查询目标域名返回 NXDOMAIN）。
   **安全性红利**：坏 Corefile 在 reload 模式下不 crash——reload 报错并保留旧配置
   （对比 rollout 模式：坏配置 = BackOff 全集群 DNS 中断）。
   `reload` 不在位（`grep reload <Corefile>` 确认）时退回 rollout 路径：
   ```bash
   kubectl rollout restart deployment coredns -n kube-system
   kubectl rollout status deployment/coredns -n kube-system --timeout=120s
   ```
   路径A 倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何 修复须先
   `kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f blade-restore-nxdomai[n]; true'`
   停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）

**路径 B —— 独立 NXDOMAIN ConfigMap + Deployment `-conf` 追加（降级形态：当前凭证无 cm patch 权限时）**

不碰集群共享的 coredns ConfigMap：新建一个只含 NXDOMAIN 规则的独立 ConfigMap，把它挂进
CoreDNS 容器并在启动参数追加第二个 `-conf` 文件。CoreDNS 原生支持多配置文件（各文件
server 块合并）。全程只用 `cm create/delete` + `deployments patch` 三类权限。

2. 创建独立 NXDOMAIN ConfigMap（键名 `nxdomain.corefile`；**先武装定时恢复，再注入**）：
   ```bash
   kubectl create configmap drill-nxdomain-tmp -n kube-system --from-literal=nxdomain.corefile='<target-domain>:53 {
     template IN A <target-domain> {
       rcode NXDOMAIN
     }
   }'
   ```
   还原脚本（三步：移除 -conf 追加项、移除 volume 挂载与卷定义、删除独立 ConfigMap、
   滚动重启。⚠️ JSON patch 的数组下标必须在武装时
   从**当前** deployment spec 实际探测确定——`-conf` 追加后 `args` 下标与 `volumeMounts`/
   `volumes` 下标因发行版而异，写死下标会删错元素；还原脚本里写的是武装时探测的下标）。
   定时器同样按恢复载体标准件跨 ns 变体承载（资源准备第 5 条）；载体栈构造同路径 A
   唯 Role 权限集不同（路径 B 恢复 = json patch deployment + 删 ConfigMap）：
   ```bash
   # 两步法（多 flag 单命令 verbs 并集广播 → delete×deployments +
   # patch×configmaps 超授权面，见标准件第一节立法）
   kubectl create role drill-rc-<hash> -n kube-system --verb=get,patch --resource=deployments
   # 载体 Pod 注册后追加第二族：
   kubectl patch role drill-rc-<hash> -n kube-system --type=json \
     -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["configmaps"],"verbs":["get","delete"]}}]'
   ```
   （SA/RoleBinding/载体 Pod 构造与验权同路径 A；验权只读 GET 目标换
   `https://kubernetes.default.svc/apis/apps/v1/namespaces/kube-system/deployments/coredns`）
   还原脚本 REST 翻译（载体内无 kubectl；下标为武装时探测值）：
   ```sh
   # 1) remove 三连（Content-Type: application/json-patch+json）
   curl -s -X PATCH \
     --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
     -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
     -H "Content-Type: application/json-patch+json" \
     -d '[{"op":"remove","path":"/spec/template/spec/containers/0/args/<追加项下标>"},{"op":"remove","path":"/spec/template/spec/containers/0/volumeMounts/<挂载下标>"},{"op":"remove","path":"/spec/template/spec/volumes/<卷下标>"}]' \
     https://kubernetes.default.svc/apis/apps/v1/namespaces/kube-system/deployments/coredns
   # 2) 删独立 ConfigMap
   curl -s -X DELETE \
     --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
     -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
     https://kubernetes.default.svc/api/v1/namespaces/kube-system/configmaps/drill-nxdomain-tmp
   # 3) 滚动重启（strategic-merge-patch+json，body 同路径 A 的 rollout 注解 patch）
   ```
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'echo <restore-script-b64> | base64 -d > /tmp/blade-restore-nxdomain.sh; sh -n /tmp/blade-restore-nxdomain.sh && ( sleep <duration>; sh /tmp/blade-restore-nxdomain.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （自断授权尾步——标准件第四节立法：还原脚本内**最后一个**恢复 curl（滚动重启 patch）升 `curl -sf` 并以 `&&` 链自删 DELETE 自己的 Binding（本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`），连同脚本一并 base64 编入；建栈 Role 后按标准件第二节两步建栈法 json-patch 追加独立自删规则（**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行）。fail-open：主恢复未确认成功则授权保留；字节挤不下就不带——完整形态与五纪律以标准件第四节为准）
   （载体构建失败时按标准件第八节降级——任务如实失败收尾，不得注入）
3. 注入——挂载 + `-conf` 追加（一条 strategic patch；`<追加项下标>`=当前 args 长 度，
   `<挂载下标>`=当前 volumeMounts 长度，`<卷下标>`=当前 volumes 长度，注入前探测）：
   ```bash
   kubectl patch deployment coredns -n kube-system --type=strategic -p \
     '{"spec":{"template":{"spec":{"containers":[{"name":"coredns","args":["-conf","/etc/coredns/nxdomain-drill/nxdomain.corefile"],"volumeMounts":[{"name":"nxdomain-drill","mountPath":"/etc/coredns/nxdomain-drill","readOnly":true}]}],"volumes":[{"name":"nxdomain-drill","configMap":{"name":"drill-nxdomain-tmp"}}]}}}}'
   ```
   ⚠️ strategic patch 对 `containers` 数组按 `name` 合并——`name` 必须是 CoreDNS 容器的
   **实际容器名**（多数发行版为 `coredns`，以 `kubectl get deployment coredns -n kube-system
   -o jsonpath='{.spec.template.spec.containers[0].name}'` 探测为准，写错会凭空新增容器）；
   `args` 数组无合并键，strategic patch 是**整体替换**——必须提交「原 args + 追加项」的
   完整数组，只提交追加项会丢掉 `-conf /etc/coredns/Corefile` 主配置
4. 等待滚动重建完成：
   ```bash
   kubectl rollout status deployment/coredns -n kube-system --timeout=120s
   ```
   路径B 倒计时纪律同路径 A（武装先于注入、紧邻下发、重武装须先停旧定时器）

**注入验证**（两条路径共用——底层都是 template NXDOMAIN 规则生效）：
1. 白盒确认规则已落位（机制主证）：
   - 路径 A：回读 Corefile 全文，确认 template 块在位（同时确认其他插件块未被破坏）：
     ```bash
     kubectl get configmap coredns -n kube-system -o jsonpath='{.data.Corefile}'
     ```
   - 路径 B：确认独立 ConfigMap 存在、deployment args 含第二个 `-conf` 且主配置仍在：
     ```bash
     kubectl get configmap drill-nxdomain-tmp -n kube-system -o jsonpath='{.data.nxdomain\.corefile}'
     kubectl get deployment coredns -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}'
     ```
2. 确认 CoreDNS Pod 已滚动重建完成且全部 Running/Ready（使用资源准备第 3 条探测到的标签）：
   ```bash
   kubectl get pods -n kube-system -l <coredns-label>
   ```
3. 效果确认——在应用 Pod 内验证目标域名解析失败，**判据工具以容器实际具备的为准**：
   - 有 nslookup（⚠️ 判据陷阱：busybox nslookup 退出码不稳定——NXDOMAIN 与超时均 RC=1，
     但 **NOERROR 空应答（`*** Can't find ...: No answer`）时 RC=0**——不能以退出码判读，
     必须 grep 输出文本）：
     ```bash
     kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'nslookup <target-domain> 2>&1 | grep -i nxdomain'
     ```
     确认输出 `** server can't find <target-domain>: NXDOMAIN`
   - 只有 curl（精简镜像常态）——`could not resolve host` 形态即 NXDOMAIN/解析失败的
     确证（exit code 6）：
     ```bash
     kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'curl -sv --max-time 5 http://<target-domain> -o /dev/null 2>&1 | grep -iE "could not resolve|Trying"'
     ```
     注入前基线是 `*   Trying <IP>:80`（解析成功直连），注入后应只剩
     `curl: (6) Could not resolve host: <target-domain>` 而无 Trying 行
4. 验证其他域名解析仍正常（确认故障范围可控）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'nslookup kubernetes.default.svc.cluster.local 2>&1 | tail -3'
   ```
   （无 nslookup 时改用集群内任一可 curl 的 Service 域名，或以第 1 条白盒 + 第 3 条组合为准）
5. 检查应用日志出现 DNS 解析失败相关错误（可观察项；应用无对外调用时以前四条为准）

**verify 判据卡（裁决最小集）**：白盒（第 1 条）+ 行为（第 3 条）+ 对照（第 4 条）
三证齐即裁「故障生效」，不再扩展挖掘——执行器窗口内采样记录采信为 primary evidence，
日志考古对本用例结构性无效（下方盲区警示）；第 2 条 CoreDNS Pod 健康、第 5 条应用
日志属佐证/观察项，缺之不阻塞裁决；机制卫生项（载体资产在位等）进 warnings 注记，
不为其追加采样

⚠️ **CoreDNS query log 盲区（verify 阶段适用）**：CoreDNS 的 query
   logging 只在 `.:53` 块的 `log` 插件里——**顶层独立 server block（template NXDOMAIN）
   未配 log 插件，该块的查询不写 CoreDNS 日志**。因此「从 CoreDNS 日志找窗口内目标域名
   NXDOMAIN 响应行」这条路对本用例**结构性无效**（日志里只有 `.:53` 块处理的其它域名
   流量）；效果证据的唯一可靠来源是**行为学探针**（执行器中段采样记录，verifier 应采信
   为窗口内 primary evidence 而非仅凭自身事后观察）。verify 消耗在日志考古上的预算应转向：白盒恢复
   确认（Corefile 回读比对）+ 事后解析恢复探针 + 执行器轨迹里的中段采样记录

**持续性检查（必做）**——故障窗口内故障必须持续存活：
1. 白盒主证复查：路径 A 回读 Corefile template 块仍在；路径 B 独立 ConfigMap 仍在且
   args 仍含追加项——**配置即状态**，配置存活即故障存活（新 Pod 滚动重建也会带注入配置，
   不存在单点失效形态）
2. 窗口中段复查（时点公式 + 单批采集）：以「注入生效确认」为时点锚（生效确认探针
   即注入验证第 3 条形态；reload 路径生效 ≤2 分钟，未生效时短间隔重探而非盲等），
   生效后一次 `time_wait 60`（避 CoreDNS 缓存 30s），到点**同轮下发**三条探针：
   白盒复查（本节第 1 条形态）+ 行为复查（注入验证第 3 条形态）+ 对照复查
   （注入验证第 4 条形态），**具体记录命令与输出**——效果证据须在故障存活期内
   采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告

**注入恢复**（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原（restorePatches 的 Corefile replace 由载体内 timer 到点 curl 执行；kubernetes.default.svc 由 kubernetes 插件直接应答、不受 template NXDOMAIN 影响，恢复通道照常可达；fire 证据落载体 /tmp/restore.log + 任务台账 recovery_handle）；reload 热加载生效约 ≤2 分钟；演练提前结束时 blade-ai recover 从台账重放同源配方提前收敛，与载体幂等双执行。路径 A 手动还原与路径 B 恢复域（remove 三连+删 ConfigMap+滚动重启）为降级兜底形态）：
1. 等待 `<duration>` 到期，武装的定时器自动还原（路径 A：merge patch 原文——Corefile
   内容变更由 `reload` 热加载生效（约 ≤2 分钟，无 Pod 重建），无 reload 时加滚动
   重启；路径 B：remove patch 三连 + 删独立 ConfigMap + 滚动重启——deployment spec
   变更必须 rollout）；提前恢复或定时器不可用时，
   由 Agent 主动执行**同一条还原命令**（幂等：路径 A 的 merge patch 迟到重复执行只是把
   原文再写一遍；路径 B 的 JSON patch remove 对已移除的路径报错无害、`--ignore-not-found`
   删除同理，迟到触发无副作用。载体定时器无 pidfile 可终止，重武装前先 pkill）
   ⚠️ 路径 A 还原**不要 `kubectl apply` ConfigMap 的 yaml 备份**——备份中的
   resourceVersion 等元数据会与集群现状冲突（报 `error when applying patch: the object
   has been modified`），必须用注入前记录的原始 Corefile 文本做 merge patch 还原：
   ```bash
   kubectl patch configmap coredns -n kube-system --type=merge \
     -p '{"data":{"Corefile":"<步骤 4 记录的原文，JSON 转义>"}}'
   # reload 在位时到此为止；无 reload 时才需要：
   # kubectl rollout restart deployment coredns -n kube-system
   ```
2. 等待还原生效（reload 模式：kubelet 同步 + reload 检测 ≤2 分钟，无 Pod 重建；rollout
   模式：等待 CoreDNS Pod 重新就绪）：
   ```bash
   kubectl rollout status deployment/coredns -n kube-system --timeout=120s  # 仅 rollout 模式
   ```

**恢复验证**：
1. 白盒确认还原到位：
   - 路径 A：回读 Corefile 与注入前原文一致（无 template 块）
   - 路径 B：独立 ConfigMap 已不存在（`Error from server (NotFound)` 即确证）、
     deployment args 回到基线（仅 `-conf /etc/coredns/Corefile`）、无残留卷挂载
2. 在应用 Pod 内验证目标域名恢复解析（配置刚生效时多副本/缓存间可能存在间歇性
   空应答噪声，判读以多次查询的稳定形态为准）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'curl -sv --max-time 5 http://<target-domain> -o /dev/null 2>&1 | grep -E "Trying|Connected"'
   ```
   确认重新出现 `*   Trying <IP>:80`（解析恢复；IP 为 CDN 轮换段，与基线不同值属正常）
3. 确认应用日志不再出现 DNS 解析失败错误
4. 确认 CoreDNS Pod 全部 Running 且 Ready，副本数回到基线

**载体全清（演练收尾，跨 ns 变体四连删除 + 零残留核实）**：

清理触发语义（标准件第九节「任务生命周期契约」，规划期直接采用免重推导）：窗口在
任务存活期内到期 → 任务内清理链自动执行四连删除；任务先于窗口到期收尾（常态——
verify 不等窗口）→ 系统侧无到期触发点，全清走带外四连删除 + 零残留核实，Agent 在
结果报告 cleanup 清单如实列出载体资产即可
```bash
kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
kubectl delete rolebinding drill-rc-<hash> -n kube-system --ignore-not-found
kubectl delete role drill-rc-<hash> -n kube-system --ignore-not-found
kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
```
零残留核实分两个 ns 各查一遍（带外核实）：
```bash
kubectl get pods,sa -n <namespace> | grep drill-rc
kubectl get role,rolebinding -n kube-system | grep drill-rc
# 期望：均无输出
```

**基准事实**：
- **根因**：CoreDNS 配置被注入 template NXDOMAIN 规则（路径 A 改共享 Corefile / 路径 B
  追加独立配置文件），对特定域名强制返回 NXDOMAIN，模拟域名不存在/DNS 记录缺失场景
- **必现现象**：目标域名解析返回 NXDOMAIN（nslookup 输出 `NXDOMAIN` / curl 报
  `(6) Could not resolve host`）；其他域名解析正常；应用日志出现
  `Name or service not known` / `Could not resolve host`；CoreDNS Pod 本身全部健康
- **不误报现象**：仅目标域名失败——集群内服务发现（cluster.local）与无关外部域名不受影响；
  已缓存解析的存量连接在缓存有效期内不受影响
- **路径选型的 RBAC 真相**：执行凭证对 coredns ConfigMap 的 patch 权限常被运维策略排除
  （常见组合 `cm_patch=no` 而 `deploy_patch=yes` + `cm_create/delete=yes`）——路 径 A 死、
  路径 B 通是常态而非例外；三权全查是路径选型的唯一依据
