---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 缩回基线副本数 + DELETE 注入的演练
# 道具 LimitRange），但注入的关键动作是 create 新对象（LimitRange）而非 patch 既有
# 靶标——装配器 M1 只承载「patch 靶标」注入域，create 类道具注入不经装配器；恢复走
# 标准件载体 timer（curl PATCH scale + curl DELETE limitrange，均幂等）+ Agent 主动
# 兜底双通道，无装配器主路径（如实声明，与 NetworkPolicy误配 同构）。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：故障机制创建一个演练道具 LimitRange
# （新对象、带 drill-lr- 前缀、不触及既有资源）。**新对象也必须立法**——limitrange 不在
# pod/deployment 受害靶的 scope 及其 secondary_scopes 内（fault_registry 仅登记
# pod/node/container/deployment/statefulset/daemonset/service/ingress/configmap/secret），
# drift 守卫第 4 步对 approved=deployment / effective=limitrange 判 scope drift 直接拒绝，
# 注入根本到不了 execute 阶段的 armed 门（与 NetworkPolicy误配 同点 REJECT_DRIFT；
# 补 3.6 机制写豁免后放行）。namespace 走 namespace_from: victim
# 派生：LimitRange 靠命名空间级 defaultRequest 作用于同 ns 新建 Pod，必须建在受害者
# 运行时所在 ns，而可移植 case 无法硬编码该 ns（换靶即失效）；freeze 时由确定性代码
# 物化为 spec.namespace（受害者 ns 已知，无需集群查询），与 NetworkPolicy误配 同构
# （DERIVED, never re-declared）。受害者副本数逆 patch（缩回基线）属受害者域内、名字
# 匹配放行，无需独立条目；载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash>）
# 均建在受害者 ns 内、在受害靶 secondary_scopes（同 ns 二级域跳过名字校验），按
# recovery-carrier.md 标准件命令式构造，Role 两资源族 get,patch deployments/scale +
# get,delete limitranges。
mechanism_writes:
  - scope: limitrange
    namespace_from: victim
    name_prefix: drill-lr-
---

**用例名称** LimitRange误配 导致 Pod_Pending

## 故障定位
配置型故障——一个 `defaultRequest` 被误配为超大值的 LimitRange 在命名空间内生效后，
准入控制（LimitRanger admission plugin）会给该 ns 内**所有未显式声明 `resources.requests`
的新建 Pod** 自动注入这个超大 default request，调度器随即因无任何节点可满足该 request
而将 Pod 判为不可调度，**Pod 被创建但永远停在 Pending**，LimitRange 对象存活即故障存活，
贯穿整个故障窗口；窗口到期载体 timer 删除 LimitRange 并缩回副本即自动恢复。

与既有 `Pod_Pending_ResourceQuota超限` 的本质区别：ResourceQuota 是**准入阶段直接拒绝**
（`exceeded quota`），Pod 对象根本不被创建，观察不到 Pending Pod、只有 ReplicaSet 的
FailedCreate 事件；本 case 是**准入阶段突变（mutating）注入超大 request**，Pod 对象被
正常创建、进入调度队列后因 request 超过全部节点 allocatable 而卡 Pending，白盒主证是
Pod 自身 `spec.containers[].resources.requests` 被写入了并非用户声明的超大值 + 调度器
FailedScheduling 事件（`0/N nodes are available: N Insufficient cpu/memory`）。手段1
（ChaosBlade）不适用——ChaosBlade 无 LimitRange/准入配置类故障靶点，本用例为
kubectl-native 专属注入。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认；
下限须覆盖 planning+execute 全程（窗口过短会导致 verify 段首观测落在恢复之后、primary
证据无法在窗内采集，见「自证链与 duration 契约」）。

## 恢复动作配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 缩回基线副本数 + DELETE 演练道具 LimitRange。**注入含 create 新对象（LimitRange），装配器不适用**（M1 注入域只承载 patch 既有靶标）——恢复走标准件载体 timer 自治 + Agent 主动兜底双通道，配方如下：

```yaml
targetRef:                                # 靶 = 受害者 workload（副本数被逆 patch）+ 演练道具 LimitRange（新建对象）
  kind: Deployment
  name: <victim-deployment>
  namespace: <namespace>
patches:                                  # 注入域：LimitRange 为 create 整个对象（execute 计划 kubectl 步骤，非 json-patch）；副本扩容为 victim-own patch
- op: create
  path: /api/v1/namespaces/<namespace>/limitranges
  value: <下方演练步骤 3 的完整 YAML>
restorePatches:                           # 恢复域：载体 TTL 到点自治执行（缩回基线副本 + DELETE LimitRange）；Agent 死亡后 recover 从台账重放同源
- op: replace
  path: /spec/replicas
  value: <baseline-replicas>
- op: delete
  path: /api/v1/namespaces/<namespace>/limitranges/drill-lr-<hash>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 恢复由标准件载体 TTL 自治承载（curl PATCH scale 缩回基线 + curl DELETE LimitRange，均幂等——对象已不存在时 DELETE 返回 404、缩回同值 PATCH 为 no-op，restore.log 如实记录即「已恢复」证据）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。
- 载体 Role 动词集：`get,patch deployments/scale` + `get,delete limitranges`（两资源族——恢复载荷实际动词 = PATCH scale + DELETE limitrange，按 recovery-carrier.md 第二节恢复形态无关总则推导；两族必须两步建栈法分别声明，禁多 flag 单命令并集广播）。

## 故障现象
1. 受害者 workload 扩容后新副本迟迟不就绪，DESIRED > READY，新 Pod 长期停在 `Pending`
2. `kubectl describe pod <pending-pod>` 的 Events 出现 `FailedScheduling`：`0/N nodes are available: N Insufficient cpu, N Insufficient memory`（N = 集群节点数）
3. **白盒主证**：Pending Pod 的 `spec.containers[].resources.requests` 被写入了一个**用户从未声明**的超大值（= LimitRange 的 `defaultRequest`）——这是与「节点资源真不足」的关键区分：request 是被准入插件突变注入的，不是应用自己写的
4. 已运行的旧 Pod 不受影响（LimitRange default 只在 Pod **创建时**注入，不追溯既有 Pod）；Pod 不崩溃、不重启，纯调度层卡住

## 资源准备
1. **甄别合格受害靶（规划期义务，本 case 硬前置）**：LimitRange 的 `defaultRequest` 只对**未显式声明 `resources.requests` 的 Pod** 生效。必须选一个 pod 模板**未写 requests** 的 Deployment 作靶（`kubectl get deploy <name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[*].resources}'` 输出为空或只有 limits 无 requests 即合格）。**若目标 ns 内所有 workload 都显式声明了 requests，本 case 对其结构性不可达**——default 不会注入、扩容副本照常调度成功、故障不出现；此时应在演练 ns 内部署一个最小无 requests 的 drill 受害 Deployment（`kubectl create deploy drill-lr-victim-<hash> -n <namespace> --image=busybox:1.36 -- sleep <duration+1800>`，模板天然无 requests）作为合格靶，并在恢复阶段一并删除。**【定案免推导】** LimitRanger 是内建 mutating 准入插件、运行在 mutating webhook **之前**；集群是否有 webhook 注入 cpu/memory requests 属集群事实——以基线受害靶 Pod 探测 `spec.containers[*].resources.requests` 为准（本条甄别即覆盖：基线靶 requests 为空、或仅含非 cpu/memory 扩展资源〔如 CNI member-eni〕→ 无 webhook 注入 cpu/memory、defaultRequest 必生效）。勿在规划期反复推导 webhook×LimitRanger 排序（机理已定案）。
2. **量定超大 defaultRequest**：读取集群最大单节点可分配量，defaultRequest 须**超过最大节点 allocatable** 才能保证无节点可容纳（`kubectl describe node <最大节点> | grep -A6 Allocatable` 取 cpu/memory；defaultRequest 取其上界之上，如最大节点 32 核则 cpu 设 `64`、memory 设 `256Gi`）。值只需"必然调度失败"，不必荒诞到触发 LimitRange 自身校验。
3. 记录受害靶基线副本数（restorePatches 的基线值来源）：`kubectl get deploy <victim-deployment> -n <namespace> -o jsonpath={.spec.replicas}`
4. 确认目标命名空间当前没有同名的演练 LimitRange（命名冲突预检：`kubectl get limitrange drill-lr-<hash> -n <namespace> -o name`——NotFound 即无冲突）；**若 ns 已有他人 LimitRange，注入前记录其内容，恢复阶段只删自己的 drill-lr-<hash>，严禁误删既有对象**。
5. 按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈——四对象同名 `drill-rc-<hash>`、全部建在被批准的靶点命名空间内，SA/Role/RoleBinding 必须用 `kubectl create` 命令构造：
   ```bash
   kubectl create sa drill-rc-<hash> -n <namespace>
   kubectl create role drill-rc-<hash> -n <namespace> --verb=get,patch --resource=deployments/scale
   kubectl create rolebinding drill-rc-<hash> -n <namespace> --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=curlimages/curl:latest --restart=Never --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   # 载体 Pod 注册后追加第二资源族（两步法——多 flag 单命令 verbs 并集广播会污染授权面，见标准件第一节立法）
   # 【定案免推导】此武装前 patch 追加主恢复族（limitranges）合法放行——armed-before-inject 门豁免「载体自身已登记 Role 的 add-only 追加动词 patch」（patched→armed）；勿再纠结「武装前 patch 会不会被 REJECT_BANNED」（旧措辞已作废）
   kubectl patch role drill-rc-<hash> -n <namespace> --type=json \
     -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["limitranges"],"verbs":["get","delete"]}}]'
   ```
   （镜像按标准件第九节判别法选定——受限网络时选节点已缓存且含 curl/sh/sleep 工具链的镜像；节点带 taint 时 overrides 需加 tolerations。SA/Role/RoleBinding 须先于载体 Pod 创建：overrides 挂接的 Pod 引用该 SA，SA 不存在时 Pod 卡 ContainerCreating。）
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节——Role verbs 以恢复脚本实际载荷动词 PATCH scale + DELETE limitrange 为准；验权探针 GET 目标 limitrange 404 属预期——对象尚未创建，验权判据是「非 403」）。定时器用载体内 SA token 走集群内 apiserver REST：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; curl -s -X PATCH --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" -H "Content-Type: application/merge-patch+json" -d "{\"spec\":{\"replicas\":<baseline-replicas>}}" https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<victim-deployment>/scale; curl -sf -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" https://kubernetes.default.svc/api/v1/namespaces/<namespace>/limitranges/drill-lr-<hash> && curl -sf -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" https://kubernetes.default.svc/apis/rbac.authorization.k8s.io/v1/namespaces/<namespace>/rolebindings/drill-rc-<hash> ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （自断授权尾步——标准件第四节立法：**最后一个**恢复动作（limitrange DELETE）升 `curl -sf` 并以 `&&` 链自删自己的 Binding（本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`），前序 scale PATCH 保持 `;` 串联不中断；**自删规则须在武装之后**按标准件第二节两步建栈法 json-patch 追加，不可在建栈 Role 时同期追加（此为 §9 执行序约定、非门强制——armed-before-inject 门已**放行**「载体自身已登记 Role 的 add-only〔json-patch op:add〕追加动词 patch」，仅拒受害靶/未登记同名 role/replace-remove 收缩；故上方资源准备第 5 条主恢复族〔limitranges get,delete〕的**武装前**追加合法放行。自删排武装后的真实理由 = 它是恢复链 fail-open 尾步、豁免于武装前 SSAR 硬门，按 §9 排在武装后 fire 前就位）；**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则。fail-open：主恢复未确认成功则授权保留；字节挤不下就不带——完整形态与五纪律以标准件第四节为准。）
   倒计时从武装时刻起算：武装与注入必须是紧邻步骤（≤60s）；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f limitrange[s]; true'`（定位不到定时器进程即中止本次演练改人工恢复，见 SKILL.md 安全红线「故障窗口完整」）。演练结束后按标准件第六节四连删除并带外核实零残留：
   ```bash
   kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete rolebinding drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete role drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
   ```

## 演练步骤
（主路径 = 基线捕获（步骤 1）→ 标准件武装（资源准备第 5 条）→ create 演练道具 LimitRange + 扩容受害靶（步骤 3）；本 case 注入含 create 新对象，无装配器调用）：

> **爆炸半径分类（定案，defer SKILL.md L41 + recovery-carrier §9）**：声明 `blast_radius_scope=namespace-wide`——双重依据：①故障本体写入集含新建 ns 级演练道具 LimitRange（drill-lr- 前缀）+ 受害靶副本数 patch，按 SKILL.md L41 末句「写入集里除 target 与一次性载体外只有 ns 内对象即为 namespace-wide」（新 LimitRange 是 ns 内对象、非无 RBAC 的一次性调试载体）；②恢复走 apiserver-write 四件套 carrier，新增 namespaced RBAC 授权面（§9①）。定案免推导，勿在 target-only↔namespace-wide 间重复仲裁。consequence 影响面 = **该命名空间内所有未声明 requests 的新建 Pod**（LimitRange 是 ns 级默认，窗口内任何无 requests 的新 Pod 都会被注入超大 request 而 Pending），写入 `blast_radius_detail` 注明；无跨 ns 持久资源写入，勿抬到 cluster-wide（既有已运行 Pod 与显式声明 requests 的 Pod 不受影响）。

1. **基线捕获**：记录受害靶副本数与调度基线（restorePatches 的基线值来源，两条路径共用）
   ```bash
   kubectl get deploy <victim-deployment> -n <namespace> -o jsonpath={.spec.replicas}
   kubectl get pods -n <namespace> -l <victim-selector> --no-headers
   ```
2. **先武装定时自恢复，再注入**（资源准备第 5 条的标准件武装命令——恢复命令幂等：定时器到期 PATCH scale + DELETE limitrange 为主，Agent 在演练结束时主动执行同一组命令兜底，定时器迟到重复执行无副作用）
3. **注入**：创建 defaultRequest 超大的 LimitRange，再扩容受害靶，新副本即被注入超大 request 而卡 Pending
   ```bash
   cat <<'EOF' | kubectl apply -f -
   apiVersion: v1
   kind: LimitRange
   metadata:
     name: drill-lr-<hash>
     namespace: <namespace>
   spec:
     limits:
     - type: Container
       defaultRequest:      # 未声明 requests 的新建 Pod 被注入此超大 request → 无节点可容纳 → Pending
         cpu: "<超大cpu>"     # 超过集群最大节点 allocatable.cpu（资源准备第 2 条量定）
         memory: "<超大memory>"
   EOF
   kubectl scale deploy <victim-deployment> -n <namespace> --replicas=<baseline-replicas+1>
   ```

## 注入验证
1. **（主证据，必做）** 确认新副本卡 Pending 且 request 被突变为超大值：
   ```bash
   kubectl get pods -n <namespace> -l <victim-selector> --field-selector status.phase=Pending
   kubectl get pod <pending-pod> -n <namespace> -o jsonpath='{.spec.containers[*].resources.requests}'
   ```
   预期输出 = LimitRange 的 defaultRequest（超大 cpu/memory），而非受害靶模板声明值（模板无 requests）
2. 根因证据——调度器 FailedScheduling 事件（含 `Insufficient cpu`/`Insufficient memory`）：
   ```bash
   kubectl get events -n <namespace> --field-selector reason=FailedScheduling
   ```
3. 白盒确认 LimitRange 对象在位且 defaultRequest 与注入意图一致：
   ```bash
   kubectl get limitrange drill-lr-<hash> -n <namespace> -o jsonpath='{.spec.limits[*].defaultRequest}'
   ```

> ⚠️ 验证纪律：
> - Pending 是**调度层**状态，Pod 无容器运行、无日志可看——`kubectl logs` 对 Pending Pod 报错属必然，不要反复取日志找证据。
> - 同一事实（LimitRange 是否在位、request 是否被突变）确认一次即可，不要重复查询。

**持续性检查**——故障窗口内故障必须持续存活（配置型故障：LimitRange 对象在即故障在）：
以「注入生效确认」为时点锚（注入验证第 1-2 条通过 = 生效）——生效确认只证明某一刻故障已落地，不证明它在整个窗口持续存活；持续性复测要把「稳态故障」与「传播收敛窗内的瞬时态」分开：待传播收敛后再次观测仍见故障态才算坐实持续。本故障传播特性：LimitRange 准入突变为 Pod 创建时即时生效、FailedScheduling 由调度器 backoff 重试非秒级连发。隔多久复测、复测几次由你依该传播特性与故障形态自行判断（无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：LimitRange 对象仍存在且 defaultRequest 未变（同注入验证第 3 条）
2. 行为复查：Pending Pod 仍 Pending、DESIRED > READY 持续不收敛（同注入验证第 1 条形态）
3. 事件复查（**持续性实体主判据 = 第 2 条 Pod 持续 Pending/unassigned**；事件新增为 **conditional、非门**）：静态集群上 30s 窗内 FailedScheduling 事件**预期无新增**——调度器把不可调度 Pod 移入 backoff/unschedulable 队列，仅在相关集群事件或 backoff 到期时才重发，静态集群 30s < requeue 周期，故 COUNT 恒 1、LAST SEEN 不前进属**预期正常**（≠ 故障消失）。「仍失败」的实体由第 2 条（Pod 持续 Pending、READY 卡 1/2、RESTARTS=0）独立证明；**勿因「无事件新增」判 partial/failed**（verifier 据实体证据 dismiss 为 expected）。事件字段读法【定案免推导】：本类集群用 events.k8s.io/v1 风格，legacy `.count`/`.lastTimestamp` 为 null，须读 `.eventTime`（或 `-o wide` 的 LAST SEEN 列），勿用 legacy jsonpath 空转重读

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治缩回副本 + DELETE 演练道具 LimitRange（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动缩回基线副本并删除 LimitRange；演练提前结束时由 Agent 主动执行同一组恢复命令（幂等——`--ignore-not-found` 对已删除对象无副作用、缩回同值 scale 为 no-op，定时器迟到重复执行无害）：
   ```bash
   kubectl delete limitrange drill-lr-<hash> -n <namespace> --ignore-not-found
   kubectl scale deploy <victim-deployment> -n <namespace> --replicas=<baseline-replicas>
   ```
2. **关键顺序**：必须先删 LimitRange 再（或同时）缩回副本——Pending Pod 的超大 request 已在创建时被突变写入其 spec，删 LimitRange **不会**追溯修正已存在的 Pending Pod；缩回副本（删掉那个 Pending 副本）后由 Deployment 控制器按基线重建，此时 LimitRange 已删、新 Pod 不再被注入超大 request，正常调度 Running。**【定案免推导】缩容 N+1→N 时 ReplicaSet 缩容排序优先删未调度/Pending 副本（ranking: unassigned < pending < not-ready < ready），故删掉的正是被注入超大 request 的 Pending 副本、保留 Running 基线副本，无需指定删哪个 Pod（勿重推）。**（若采用「部署 drill 受害靶」形态，恢复改为删 LimitRange + `kubectl delete deploy drill-lr-victim-<hash> -n <namespace> --ignore-not-found`。）

## 恢复验证
1. 白盒确认 LimitRange 已删除：
   ```bash
   kubectl get limitrange drill-lr-<hash> -n <namespace> -o name
   ```
   预期 NotFound（404 即删除确证）
2. 行为确认——受害靶副本回到基线且全部 Running/Ready、无 Pending：
   ```bash
   kubectl get deploy <victim-deployment> -n <namespace>
   kubectl get pods -n <namespace> -l <victim-selector> --field-selector status.phase=Pending --no-headers
   ```
   预期 READY==DESIRED==基线；Pending 查询空输出
3. 确认 FailedScheduling 事件不再新增——事件列表 append-only（历史事件不消失），判据是 **LAST SEEN 时戳不再前进**：
   ```bash
   kubectl get events -n <namespace> --field-selector reason=FailedScheduling
   ```
4. 确认载体 restore.log 取证（`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`——定时器 fire 的直接证据，PATCH+DELETE 响应回显；404/no-op 形态 = Agent 提前恢复后定时器迟到空触发，同为已恢复证据）

## 基准事实
- **根因**：命名空间 LimitRange 的 `defaultRequest` 被误配为超过全部节点 allocatable 的超大值，LimitRanger 准入插件给该 ns 内所有未声明 requests 的新建 Pod 注入此超大 request，调度器无节点可容纳 → Pod 卡 Pending。模拟运维误设默认资源下限/多租户配额模板下发错误/资源基线配置漂移场景。
- **必现现象**：新 Pod Pending；`describe pod` 见 FailedScheduling `Insufficient cpu/memory`；Pending Pod 的 `spec.containers[].resources.requests` 是被突变注入的超大值（非应用声明）；LimitRange 对象在位且 defaultRequest 超大。
- **与 ResourceQuota超限 的判据分野**：本 case Pod 对象**被创建**并卡 Pending（mutating 注入 request）；ResourceQuota 是 Pod **不被创建**（validating 拒绝 `exceeded quota`）、只有 ReplicaSet FailedCreate。两者恢复也不同：删 LimitRange 后须重建/缩回被突变的 Pending Pod，删 quota 后控制器自动补齐。
- **作用域边界**：仅该命名空间内**未显式声明 requests 的新建 Pod** 受影响；已运行 Pod、显式声明了 requests 的 Pod、其他命名空间 Pod 均不受影响。
- **自证链与 duration 契约**：故障为配置型即时生效（LimitRange 一在位，下一个新建 Pod 立即被突变），无传播窗；duration 下限取系统最小窗口地板（600s）并宁宽勿窄——窗口须覆盖 planning+execute 全程，否则 verify 段首观测会落在载体 fire 之后、primary 证据（Pending + 超大 request）无法在窗内自采（窗口过短时序错配会致 partial）。

## 注意事项
- chaosblade 无 LimitRange/准入配置类故障靶点，本用例为 kubectl-native 专属注入。
- `defaultRequest`（注入 requests，驱动调度）与 `default`（注入 limits，驱动 OOM/限流）语义不同——本 case 用 `defaultRequest` 才能触发 Pending；若误用 `default` 只设 limits 不设 requests，Pod 仍可调度（limits 不参与调度决策），故障不出现。
- 受害靶合格性是硬前置（资源准备第 1 条）：pod 模板已显式声明 requests 的 workload 对 LimitRange default 免疫，注入静默无效——预检不通过时不得强行推进，换无 requests 的靶或部署 drill 受害靶。
- 演练道具命名带 `drill-lr-` 前缀（任务侧资产归属登记依据），**禁止**给演练资源加任何演练标记 label（演练必须对集群呈现为真实事故，见 recovery-carrier.md 第一节命名约定）。
- 恢复顺序陷阱（注入恢复第 2 条）：删 LimitRange 不追溯修正已 Pending 的 Pod（其 request 已在创建时突变写死），必须删/缩该 Pending Pod 让控制器在 LimitRange 消失后重建，否则残留一个永久 Pending Pod。
- 若命名空间已有他人 LimitRange，注入前记录其内容，恢复阶段仅删除 drill-lr-<hash>，不得误删既有对象；一个 ns 可有多个 LimitRange，各自独立作用于匹配的容器类型。
