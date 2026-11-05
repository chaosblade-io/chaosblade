---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2/ND9）：
# 本 case 恢复动作住址 = apiserver 写（DELETE 自建抢占者 Deployment + DELETE 演练道具
# PriorityClass）。注入含 create 新对象（抢占者 Deployment + PriorityClass）而非 patch 既有
# 靶标字段——装配器 M1 注入域只承载「patch 既有靶标」，create 类对象级增删不经装配器；恢复走
# 标准件载体 timer（cluster 五件套变体：curl DELETE 抢占者 Deployment + PriorityClass，幂等）
# + Agent 主动兜底双通道，无装配器主路径（如实声明，与 ServiceAccount_权限故障_权限被撤销 /
# LimitRange误配 的「create 型注入 → 装配器不适用 → SOP 载体 timer」形态同构）。
recovery_channel: apiserver-write
# 受害者作用域参考（route A，非强制 hint）：本案语义受害者是「被抢占后驱逐重建的低优先级 Pod」，
# 但**批准靶（approved target）锚在自建的高优先级抢占者 Deployment** 上（见下方 write-set 立法），
# 故 fault_scope 填 deployment——planner 据此把 finish_planning 的靶身份声明为抢占者 Deployment。
# 全代码库无确定性消费者校验/覆盖此字段，纯 LLM 面向软 hint。为什么靶不是被抢占的 Pod：抢占者
# 是本 case 唯一能合规创建的 workload（workload-create 封禁下，只有 drill-target manifest 契约
# 这一条 in-band 通道能建负载，且契约硬约束 metadata.name == 批准靶名——只能建一个），故批准靶
# 必须是抢占者；被抢占方是**既有**低优先级 Pod（无法再建第二个 workload 当靶），在资源准备阶段
# 显式枚举为爆炸半径（victim=既有Pod / mechanism=抢占者Deployment+PriorityClass / blast-radius=
# 被抢占方所在节点的低优先级负载 三域分离）。
fault_scope: deployment
# 机制写入集立法（write-set approval contract）：抢占者 Deployment 是批准靶自身
# （drill-preempt-<hash>，经 drill-target manifest 契约 `kubectl apply -f -` 单 Deployment 文档
# 自建，screener 以普通 drift net 锚定其 metadata.name=approved 身份、登记为 occupant_deployment
# 载体资产供清理链删除），名字匹配放行、不在本清单重复立法。故障机制另需创建一个演练道具
# PriorityClass（drill-pc- 前缀、cluster-scoped 新对象）——priorityclass 既不在 deployment 批准靶
# 的 OWNER_SCOPES，也不在其 secondary_scopes（fault_registry family scopes 无 priorityclass），
# drift 守卫第 4 步对 approved=deployment / effective=priorityclass 判 scope drift 直接拒绝，注入
# 到不了 execute 阶段的 armed 门（与 ServiceAccount 的 RBAC 三对象、NetworkPolicy误配 同点
# REJECT_DRIFT；补机制写豁免后放行）。priorityclass 是 cluster-scoped、无 namespace，故本 entry
# 不带 namespace_from（effective.namespace 为空、entry.namespace 默认空即匹配，同 node 类
# cluster-scoped entry 形态）；name_prefix 锁死 drill-pc- 前缀，禁触及既有生产 PriorityClass。
# 恢复载体栈（cluster 五件套：裸 Pod + SA + ClusterRole + ClusterRoleBinding，同名 drill-rc-<hash>）
# 走 cluster-scoped 授权面（删 PriorityClass）+ resourceNames 锁名的 Deployment 删除，不在本清单立法。
mechanism_writes:
  - scope: priorityclass
    name_prefix: drill-pc-
---

**用例名称** 被高优先级抢占 导致 Pod_被驱逐重建

## 故障定位
调度型故障——一个高优先级、资源请求超过目标节点当前空闲容量的抢占者 Pod 进入 Pending 后，
kube-scheduler 的**抢占（preemption）**逻辑被触发：它在该节点上挑选一批**优先级更低**的 Pod
作为牺牲者（victim）驱逐，为抢占者腾出资源；被驱逐的低优先级 Pod 由其上层控制器（Deployment/
ReplicaSet）在别处或稍后**重建**（被驱逐重建）。**抢占者 Deployment + PriorityClass 存活即
故障存活**（抢占者持续占住腾出的资源、被抢占方无法回到原节点），贯穿整个故障窗口；窗口到期
载体 timer 删除抢占者 Deployment + PriorityClass，资源释放，被抢占方由自身控制器重新调度回位。

受害者身份 vs 机制攻击面 vs 爆炸半径三域分离：**受害者**是被抢占后驱逐重建的既有低优先级 Pod
（其必现现象 = 被 Evicted/Preempted + 由控制器重建换新 UID，验证「应用被高优先级负载抢占后能否
自愈重建」）；**机制攻击面**是自建的抢占者 Deployment（drill-preempt-<hash>，高优先级 + 超额
resource requests）与演练道具 PriorityClass（drill-pc-<hash>）；**爆炸半径**限于抢占者被约束的
目标节点上、优先级低于抢占者的那批 Pod（资源准备阶段显式枚举）。

手段结论：手段1（ChaosBlade）**不适用**——ChaosBlade 无调度优先级/抢占类故障靶点（其 K8s 执行器
面向 pod/node/container 的 cpu/mem/network/disk/process，不含 PriorityClass/调度器抢占面），本
用例为 kubectl-native 专属注入（create PriorityClass + create 抢占者 Deployment）。注入=建两个
新对象、恢复=删这两个对象，均非「patch 既有靶标字段」，故装配器 M1 不适用，恢复走标准件载体 timer。

判据可达性：**机制判据**（白盒）= 抢占者 Pod 由 Pending 转 Running（调度成功即抢占已发生）+
PriorityClass 在位且被抢占者引用；**行为判据**（业务影响，本案主证）= 被抢占方 Pod 出现
`Preempted` 驱逐事件、原 Pod 消失、控制器重建出带新 UID 的替身。二者在故障存活期内均可采集。

自证链与 duration 契约：抢占由调度器在抢占者进入 Pending 后**秒级**决策（不同于 volume/env 类
需 kubelet 同步），但被抢占方的驱逐（graceful termination）+ 控制器重建 + 重新调度存在收敛窗，
`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认；下限取系统最小窗口地板并宁宽勿窄
——窗口须覆盖 planning+execute 全程 + 抢占收敛 + 观察窗，否则 verify 段首观测可能落在恢复之后。

## 恢复动作配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：DELETE 抢占者 Deployment +
DELETE 演练道具 PriorityClass。**注入含 create 新对象、恢复含 DELETE 对象，装配器不适用**（M1
注入域只承载 patch 既有靶标字段）——恢复走标准件载体 timer（cluster 五件套变体）自治 + Agent
主动兜底双通道，配方如下：

```yaml
targetRef:                                # 恢复主靶 = 抢占者 Deployment（删它即释放资源=故障恢复）；PriorityClass 为 cluster-scoped 残留清理
  kind: Deployment
  name: drill-preempt-<hash>
  namespace: <namespace>
patches:                                  # 注入域：create PriorityClass + create 抢占者 Deployment（execute 计划 kubectl 步骤，非 json-patch）
- op: create
  path: /apis/scheduling.k8s.io/v1/priorityclasses
  value: <下方演练步骤 3 的完整 PriorityClass YAML>
- op: create
  path: /apis/apps/v1/namespaces/<namespace>/deployments
  value: <下方演练步骤 5 的完整抢占者 Deployment YAML>
restorePatches:                           # 恢复域：载体 TTL 到点自治 DELETE 两对象；Agent 死亡后 recover 从台账重放同源
- op: delete
  path: /apis/apps/v1/namespaces/<namespace>/deployments/drill-preempt-<hash>
- op: delete
  path: /apis/scheduling.k8s.io/v1/priorityclasses/drill-pc-<hash>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 恢复由标准件载体 TTL 自治承载（curl DELETE，幂等——对象已不存在时返回 404，restore.log 如实
  记录即「已恢复」证据）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体
  幂等双执行（先到先收敛、后到读回 404/no-op）。**删除抢占者 Deployment 是故障恢复的充分动作**
  （资源释放→被抢占方由自身控制器重新调度回位）；删除 PriorityClass 是 cluster-scoped 残留清理
  （PriorityClass 无 Pod 引用后即惰性，但留着违反零残留纪律）。
- 载体授权面（cluster 五件套变体，recovery-carrier.md 第二节 cluster 单栈形态）：ClusterRole 两条
  resourceNames 锁名规则——`get,delete deployments`（锁 drill-preempt-<hash>）+ `get,delete
  priorityclasses`（锁 drill-pc-<hash>），ClusterRoleBinding 绑集群级；锁名后即便集群级绑定也只能
  删这两根演练对象。自断授权尾步（第四节）以两步建栈法追加 `delete clusterrolebindings`
  （resourceNames 锁 drill-rc-<hash>）删自己的 Binding。
- 被抢占方 Pod **不被本 case 直接修改**（抢占是调度器行为，本案只创建触发它的抢占者）——故无被抢占
  方逆 patch 条目；其恢复由被抢占方自身的上层控制器（Deployment/ReplicaSet）在资源释放后自动重建。

## 故障现象
1. 自建的高优先级抢占者 Pod 短暂 Pending 后转 Running，落在被约束的目标节点上（调度成功即抢占已发生）
2. **（本案主证）** 目标节点上一批低优先级 Pod 被驱逐：`kubectl get events` 出现 `Preempted` /
   `Preempting` 事件（message 含 `Preempted by <preemptor-pod>` 语义），原 Pod 从节点消失
3. 被抢占方由其上层控制器**重建**——出现带**新 UID** 的替身 Pod，可能在目标节点（资源已腾出）或
   其他节点重新调度；重建期间该负载可用副本数短暂下降（这是「被驱逐重建」的业务影响面）
4. 演练道具 PriorityClass 在位、抢占者 Deployment 的 `spec.template.spec.priorityClassName` 引用它
5. **抢占者自身正常 Running**（不崩溃、不重启）——它是故障的施加者而非受害者；被抢占方也不崩溃
   （是被优雅驱逐后重建，非 CrashLoop），故判据锚在「驱逐事件 + 新 UID 重建」而非 Pod 异常状态

## 资源准备
1. **甄别目标节点与被抢占方（规划期义务，本 case 硬前置）**：抢占是调度器在**资源压力**下才触发的
   行为——目标节点的空闲容量必须**不足以**直接容纳抢占者，抢占者才会 Pending 进而抢占。逐个探测
   候选节点的可分配量与已用量，选一个「有低优先级可抢占负载、且空闲容量可被超额请求压穿」的节点：
   ```bash
   kubectl describe node <node-name> | grep -A6 "Allocated resources"    # 记录 requests 已用占比与空闲量
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name>  # 枚举该节点上的 Pod（候选被抢占方）
   ```
2. **枚举被抢占方（爆炸半径声明的直接输入，tasks 硬要求）**：对目标节点上每个候选 Pod 查其有效
   优先级（`priorityClassName` 解析出的 `value`，缺省为 0）——**只有优先级严格低于抢占者的 Pod 才会
   被抢占**。显式列出将被纳入抢占范围的 Pod 清单（名字 + 优先级 + 所属控制器 + requests），此清单
   即爆炸半径声明；**严禁选中关键系统负载**（system-node-critical/cluster-critical 类高优先级 Pod
   不会被抢占，但混入清单说明选点不当），优先选可安全中断的演练/测试负载所在节点：
   ```bash
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name> \
     -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,PRIO:.spec.priority,OWNER:.metadata.ownerReferences[0].kind
   ```
3. **记录目标节点空闲容量与被抢占方 requests（抢占者 sizing 的输入）**：抢占者的 `resources.requests`
   必须**同时满足**两个不等式才能确定性触发抢占——① `> 节点当前空闲可分配量`（否则抢占者直接调度
   成功、无人被抢占，故障不发生）；② `≤ 节点空闲量 + 拟被抢占 Pod 的 requests 之和`（否则即便驱逐
   被抢占方也腾不出足够空间、抢占者永远 Pending，故障退化为「抢占者调度失败」而非「被抢占方驱逐重建」）。
   取值落在两界之间并留余量（对齐被抢占方 requests 之和的量级）。
4. 确认集群无干扰因素：**集群自动扩缩容（Cluster Autoscaler）若启用，可能通过扩出新节点来消化抢占者
   而非抢占**——预检确认 CA 未启用或目标节点组不在其扩容范围；确认调度器抢占未被 feature-gate 关闭
   （`NonPreemptingPriority` 等属增强项、默认不禁用基本抢占）。
5. 确认目标命名空间当前没有同名演练 PriorityClass / 抢占者 Deployment（命名冲突预检：
   `kubectl get priorityclass drill-pc-<hash> -o name` 与 `kubectl get deploy drill-preempt-<hash>
   -n <namespace> -o name` 均 NotFound 即无冲突）。
6. 按**恢复载体标准件**（`references/carrier/recovery-carrier.md` 第二节 cluster 单栈变体）自建
   cluster 五件套——同名 `drill-rc-<hash>`，SA/ClusterRole/ClusterRoleBinding 必须用 `kubectl create`
   构造（`apply -f` 对 RBAC kinds 因白名单不收被设计性拦截）：
   ```bash
   kubectl create sa drill-rc-<hash> -n <namespace>
   kubectl create clusterrole drill-rc-<hash> \
     --verb=get,delete --resource=deployments --resource-name=drill-preempt-<hash> \
     --verb=get,delete --resource=priorityclasses --resource-name=drill-pc-<hash>
   kubectl create clusterrolebinding drill-rc-<hash> --clusterrole=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=curlimages/curl:latest --restart=Never \
     --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   ```
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节——验权
   探针 GET 目标 Deployment/PriorityClass，对象尚未创建时 404 属预期、判据是「非 403」）；自断授权
   尾步按标准件第二节两步建栈法 json-patch 给 ClusterRole 追加一条 `delete clusterrolebindings`
   （resourceNames 锁 drill-rc-<hash>），**严禁并入 create 命令**（pflag 并集复制污染主授权规则）。
   演练结束后按标准件第六节四连删除并带外核实零残留（Pod/ClusterRoleBinding/ClusterRole/SA）。

## 演练步骤
（主路径 = 基线捕获（步骤 1）→ create PriorityClass（步骤 3）→ 标准件武装（步骤 4，
`<recovery-seconds>` 取 prompt 下发的 recovery_timer_seconds）→ create 抢占者 Deployment（步骤 5）；
本 case 注入是 create 两个新对象，无装配器调用。**创建序不可颠倒**：PriorityClass 必须先于抢占者
Deployment 存在，否则抢占者 Pod 引用不存在的 PriorityClass 会被 apiserver 拒绝创建）：

> **爆炸半径分类（定案，defer SKILL.md L41 + recovery-carrier §9）**：声明
> `blast_radius_scope=cluster-wide`——双重依据：①故障本体写入集含新建 **cluster-scoped** 演练道具
> PriorityClass（跨命名空间的集群级资源写入，按 SKILL.md L41 抬到 cluster-wide）；②恢复走
> cluster 五件套 carrier，新增集群级 RBAC 授权面（§9）。consequence 影响面 = 目标节点上被枚举的
> 低优先级 Pod 被驱逐重建（资源准备第 2 条清单），写入 `blast_radius_detail`；实际驱逐对象由调度器
> 抢占算法在其候选集内选定，可能不完全等于枚举清单（见注意事项），故清单是「预期波及面上界」。

1. **基线捕获**：记录目标节点名、其上被抢占方 Pod 清单（名字/UID/nodeName/priority，恢复验证的
   对照锚点）、节点空闲容量、PriorityClass 与抢占者 Deployment 均不存在（两条路径共用）。
2. **计算抢占者 requests**：按资源准备第 3 条的双不等式定出 CPU/内存请求值（大于节点空闲、不超过
   空闲+被抢占方 requests 之和），写入步骤 5 的 manifest。
3. **创建演练道具 PriorityClass**（高优先级值，非全局默认）：
   ```bash
   cat <<'EOF' | kubectl apply -f -
   apiVersion: scheduling.k8s.io/v1
   kind: PriorityClass
   metadata:
     name: drill-pc-<hash>
   value: 100000                 # 高于被抢占方（缺省 0）、低于系统关键级（system-*-critical 为 2e9），避免波及系统负载
   globalDefault: false          # 严禁设 true——否则集群所有无显式优先级的新 Pod 都继承此值，污染全局调度
   description: "drill preemption probe"
   EOF
   ```
4. **先武装定时自恢复，再注入抢占者**（资源准备第 6 条的 cluster 五件套武装命令——恢复命令幂等：
   定时器到期 DELETE 抢占者 Deployment + PriorityClass 为主，Agent 演练结束时主动执行同两条 DELETE
   兜底，对象已不存在时 404 即「已恢复」证据，重复执行无副作用）：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <recovery-seconds>; \
     curl -s -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/drill-preempt-<hash>; \
     curl -sf -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       https://kubernetes.default.svc/apis/scheduling.k8s.io/v1/priorityclasses/drill-pc-<hash> && \
     curl -sf -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       https://kubernetes.default.svc/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/drill-rc-<hash> \
   ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （倒计时从武装时刻起算：先校验后武装、与注入紧邻 ≤60s；武装后发生任何修复须先停旧定时器再全额
   重武装：`kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f drill-preempt-<has[h]; true'`，
   见 SKILL.md 安全红线「故障窗口完整」。自断授权尾步为最后一条 `curl -sf ... clusterrolebindings`。）
5. **注入：创建抢占者 Deployment**（drill-target manifest 契约：单 Deployment 文档、
   metadata.name=drill-preempt-<hash> 显式声明〔= approved 身份〕、恰一容器无 initContainers、
   image ∈ 载体允许集、无 host*/privileged/capabilities/hostPath、零卷或仅 PVC/configMap/secret 卷；
   `priorityClassName` 引用步骤 3 的 PriorityClass、`resources.requests` 取步骤 2 算出的超额值、
   `nodeSelector` 用 `kubernetes.io/hostname: <node-name>` 把抢占者约束到目标节点——**用 nodeSelector
   而非 nodeName**：nodeName 是静态绑定会绕过调度器、抢占不发生，nodeSelector 仍走调度器故触发抢占）：
   ```bash
   cat <<'EOF' | kubectl apply -f -
   apiVersion: apps/v1
   kind: Deployment
   metadata:
     name: drill-preempt-<hash>
     namespace: <namespace>
   spec:
     replicas: 1
     selector:
       matchLabels:
         app: drill-preempt-<hash>
     template:
       metadata:
         labels:
           app: drill-preempt-<hash>
       spec:
         priorityClassName: drill-pc-<hash>
         nodeSelector:
           kubernetes.io/hostname: <node-name>
         containers:
         - name: preemptor
           image: busybox:1.36            # ∈ 载体允许集（drill-target 契约 T4 要求）；抢占者只 sleep 占资源，busybox 足够
           command: ["sleep", "<duration+1800>"]
           resources:
             requests:
               cpu: "<超额值，> 节点空闲>"
               memory: "<超额值>"
             limits:
               cpu: "<同 requests 量级>"
               memory: "<同 requests 量级>"
   EOF
   ```

## 注入验证
1. **（机制判据）** 抢占者 Pod 由 Pending 转 Running 并落在目标节点——调度成功即抢占已发生：
   ```bash
   kubectl get pods -n <namespace> -l app=drill-preempt-<hash> \
     -o jsonpath='{range .items[*]}{.metadata.name}  phase={.status.phase}  node={.spec.nodeName}{"\n"}{end}'
   ```
   预期：phase=Running、node=<node-name>。若长时间 Pending 且 describe 显示 `Insufficient cpu/memory`
   而无抢占事件，说明 requests 超过「空闲+被抢占方之和」上界（资源准备第 3 条不等式②被违反），
   抢占者腾不出空间——须下调 requests 重注入（非注入成功的故障形态）。
2. **（行为主证，必做）** 被抢占方出现抢占驱逐事件：
   ```bash
   kubectl get events -A --field-selector reason=Preempted -o wide    # 或 reason=Preempting（调度器发起端）
   kubectl describe node <node-name> | grep -iA3 preempt               # 节点侧抢占记录
   ```
   预期：至少一条 `Preempted`/`Preempting` 事件，message 指向被驱逐的低优先级 Pod 与抢占者
   （`Preempted by <namespace>/drill-preempt-<hash>-xxxxx`）。
3. **（被驱逐重建确证）** 对照基线捕获的被抢占方清单，确认原 Pod 消失、控制器重建出新 UID 替身：
   ```bash
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name>   # 被抢占方原 Pod 不再在此节点
   kubectl get pod <victim-pod> -n <victim-ns> -o jsonpath='{.metadata.uid}'  # 与基线 UID 比对（新 UID = 已重建）
   ```
   预期：被抢占方原 Pod 名/UID 从目标节点消失；其控制器（Deployment/RS）在别处或本节点重建出
   新 UID 的 Pod（被驱逐重建）。**采集时机**：抢占决策秒级、但驱逐的 graceful termination + 重建 +
   重调度有收敛窗，注入后须等待至少一个终止宽限期再判读，不可因「尚未见重建替身」即误判注入失败。
4. 确认演练道具 PriorityClass 在位且被抢占者引用：
   `kubectl get priorityclass drill-pc-<hash> -o jsonpath='{.value}'`（= 100000）；抢占者 Deployment
   `.spec.template.spec.priorityClassName` = drill-pc-<hash>。

> ⚠️ 验证纪律：抢占者短暂 Pending 是**预期中间态**（正是它触发抢占），不得因见 Pending 就重发
> 注入；同一事实（抢占者是否 Running、被抢占方是否重建）确认一次即可，不要重复轮询。

**持续性检查**——故障窗口内抢占压力必须持续存活（抢占者在位即资源被占、被抢占方回不到原位）：
以「注入生效确认」为时点锚（注入验证第 1-3 条通过 = 生效），生效后隔多久复测、复测几次由你依本故障形态自判（配置型：接线在即故障在，无传播过程；无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**（效果证据须在故障存活期内采集，恢复后无法再采）：
1. 白盒复查：抢占者 Deployment + PriorityClass 仍存在，抢占者 Pod 仍 Running 在目标节点（同注入验证第 1 条）
2. 行为复查：被抢占方替身 Pod 的调度落点（是否因目标节点资源被抢占者占住而无法回到原节点）
3. 稳定性复查：抢占者 Pod RESTARTS 计数无增长（抢占者自身不应崩溃）

## 注入恢复
（主路径下恢复由载体 TTL 自治承载：删除抢占者 Deployment（释放资源）+ 删除演练道具 PriorityClass。
Agent 演练结束时主动执行同两条 DELETE 兜底，幂等——对象已不存在时 404 即「已恢复」证据；并停掉
已武装的 timer 避免迟到重放。以下手动命令为降级兜底形态）：
1. 删除抢占者 Deployment（**故障恢复的充分动作**——资源释放后被抢占方由自身控制器重新调度回位）：
   ```bash
   kubectl delete deploy drill-preempt-<hash> -n <namespace> --ignore-not-found
   ```
2. 删除演练道具 PriorityClass（cluster-scoped 残留清理）：
   ```bash
   kubectl delete priorityclass drill-pc-<hash> --ignore-not-found
   ```
3. 被抢占方 Pod 无需手动重建——其上层控制器（Deployment/ReplicaSet）在资源释放后自动补齐副本、
   重新调度（可能回到目标节点或落在其他有空闲的节点）。
4. 载体五件套按资源准备第 6 条尾四连删除（Pod/ClusterRoleBinding/ClusterRole/SA），带外核实零残留。

> ⚠️ 抢占者 Deployment 无 activeDeadlineSeconds 自到期能力（不同于 occupant pod 契约的 C4），
> 删除**完全依赖**注入前武装的载体 timer（Agent 死亡时自治）或 `blade-ai recover`（台账重放）
> 或 Agent 主动兜底；未武装即注入会导致抢占者长期占资源、被抢占方无法回位（故障滞留）。

## 恢复验证
1. 抢占者已删除：`kubectl get deploy drill-preempt-<hash> -n <namespace> -o name` 预期 NotFound（404 即删除确证）
2. PriorityClass 已删除：`kubectl get priorityclass drill-pc-<hash> -o name` 预期 NotFound
3. 被抢占方恢复：对照基线清单，被抢占方控制器的可用副本数回到基线、Pod 全部 Running（重新调度
   回目标节点或落在其他节点均可——抢占是资源竞争结果，恢复判据是「被抢占方自愈重建成功」而非
   「必须回到原节点」）；目标节点空闲容量回升（`kubectl describe node <node-name>` 的 Allocated 占比下降）
4. 确认载体/timer 取证：`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`（若载体尚在）或
   直接核实四件套已删；ClusterRoleBinding 自断授权尾步已消费（无迟到重放）
5. 全集群零残留核实：drill-pc-<hash> / drill-preempt-<hash> / drill-rc-<hash> 四件套全部 NotFound

## 基准事实
- **根因**：一个高优先级、资源请求超过目标节点空闲容量的 Pod 进入 Pending，触发 kube-scheduler
  抢占逻辑——驱逐同节点上优先级更低的 Pod 为高优先级 Pod 腾资源，模拟「关键高优先级负载挤占
  普通负载资源」的真实调度竞争场景（如业务高峰高优先级任务上线、节点资源碎片化下的优先级倒挂）
- **必现现象**：抢占者 Pod 由 Pending 转 Running；被抢占方 Pod 出现 `Preempted` 驱逐事件、原 Pod
  消失、控制器重建出新 UID 替身；目标节点资源被抢占者占住
- **与 DiskPressure 驱逐的分界**：DiskPressure 案是 kubelet 因节点磁盘压力驱逐 Pod（驱逐者=kubelet、
  触发条件=资源阈值、判据=Evicted+DiskPressure=True）；本 case 是 kube-scheduler 因**优先级竞争**
  驱逐 Pod（驱逐者=调度器抢占、触发条件=高优先级 Pending、判据=Preempted 事件+抢占者转 Running）——
  两者都是「Pod 被驱逐重建」但驱逐主体、触发机制、判据完全不同
- **作用域边界**：只有优先级严格低于抢占者、且在抢占者 nodeSelector 约束的目标节点上的 Pod 会被
  抢占；其他节点、同级或更高优先级的 Pod 不受影响；抢占者被删除后故障即消

## 注意事项
- ChaosBlade 无调度优先级/抢占类故障靶点，本用例为 kubectl-native 专属注入
- **资源压力是硬可行性前置（本 case 最大风险点）**：抢占只在抢占者「调度不下」时触发。若目标节点
  空闲容量充足、抢占者能直接放下，则**无人被抢占、故障不发生**（抢占者正常 Running、被抢占方纹丝
  不动）。故 sizing 双不等式（资源准备第 3 条）必须按当次实测的节点空闲量与被抢占方 requests 动态
  计算，不得照抄固定值；集群自动扩缩容（CA）可能扩出新节点消化抢占者而非抢占，预检须排除
- **爆炸半径含真实既有 Pod（侵入性高于配置类 case）**：被抢占方是集群里**已存在**的低优先级 Pod
  （本案无法自建第二个 workload 当受害者——workload-create 封禁下只能建一个 drill-target，即抢占者），
  故注入会真实驱逐既有负载。资源准备第 2 条的枚举是强制义务，选点须避开关键业务/系统负载，优先选
  可安全中断的演练或测试负载所在节点；实际被驱逐对象由调度器抢占算法在其候选集内选定（按优先级、
  再按资源匹配），可能不完全等于枚举清单——枚举清单是波及面**上界**声明
- **受害者优先级必须严格低于抢占者**：PriorityClass `value` 缺省为 0；抢占者 value=100000 高于
  普通负载、低于 system-node-critical/cluster-critical（2e9），故系统关键 DaemonSet 不会被抢占
  （符合预期）；若目标节点上全是同级或更高优先级 Pod，抢占不发生——须换有低优先级负载的节点
- **globalDefault 严禁设 true**：PriorityClass `globalDefault: true` 会让集群所有无显式优先级的
  新建 Pod 都继承此高优先级，污染全局调度排序、影响面失控；本案一律 false，只让自建抢占者引用
- **抢占者用 nodeSelector 而非 nodeName 约束**：nodeName 是静态绑定（绕过调度器直接落到节点），
  抢占逻辑不触发；nodeSelector（`kubernetes.io/hostname`）仍走调度器过滤/打分，才会因资源不足
  进入 Pending 并触发抢占
- **cluster-scoped mechanism_write 待实弹确认**：priorityclass 是 cluster-scoped、mechanism_writes
  entry 不带 namespace_from（空 ns 匹配），此形态与 node 类 cluster-scoped entry 同源但 catalogue
  零先例——drift 第 3.6 步对 create priorityclass 命中该 entry 的放行须 step3 实弹确认；REJECT 则
  回溯建模（同 PDB 案的双派生轴待确认纪律）
- **守卫改动前置（注入前必须落地，否则 apply 被拦）**：`priorityclass` 需进
  `ALLOWED_MANIFEST_KINDS`（否则 `kubectl apply -f -` 建 PriorityClass 在工具层被 BANNED）+
  `KIND_ALIASES`（singular/plural，供 mechanism_writes scope 与恢复 DELETE 的 canonicalise 匹配）+
  `CLUSTER_SCOPED_KINDS`（正确表达其 cluster-scoped 命名空间拓扑）——与 networkpolicy/limitrange
  加白名单同构，但多一处 CLUSTER_SCOPED_KINDS
- 演练道具命名带 `drill-pc-` / `drill-preempt-` 前缀（任务侧资产归属登记依据），**禁止**给演练资源
  加任何演练标记 label（演练须对集群呈现为真实事故，见 recovery-carrier.md 第一节命名约定）
