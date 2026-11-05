---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2/ND9）：
# 本 case 恢复动作住址 = apiserver 写（DELETE 自建演练道具 ValidatingWebhookConfiguration +
# DELETE 受害 Deployment）。注入含 create 新对象（webhook 配置 + 受害 Deployment）而非 patch
# 既有靶标字段——装配器 M1 注入域只承载「patch 既有靶标」，create 类对象级增删不经装配器；恢复走
# 标准件载体 timer（cluster 五件套变体：curl DELETE webhook 配置 + 受害 Deployment，幂等）+ Agent
# 主动兜底双通道，无装配器主路径（如实声明，与 被高优先级抢占 / ServiceAccount_权限被撤销 /
# LimitRange误配 的「create 型注入 → 装配器不适用 → SOP 载体 timer」形态同构）。
recovery_channel: apiserver-write
# 受害者作用域参考（route A，非强制 hint）：本案语义受害者是「因 webhook 回调不可达而被拒绝准入、
# 无法拉起 Pod 的工作负载」，**批准靶（approved target）锚在自建的受害 Deployment** 上（见下方
# write-set 立法），故 fault_scope 填 deployment——planner 据此把 finish_planning 的靶身份声明为
# 受害 Deployment。全代码库无确定性消费者校验/覆盖此字段，纯 LLM 面向软 hint。为什么靶是受害
# Deployment 而非 webhook 配置本身：webhook 配置是 cluster-scoped 演练**道具**（故障施加者），受害
# Deployment 是本 case 唯一能合规创建的 workload（workload-create 封禁下只有 drill-target manifest
# 契约这一条 in-band 通道能建负载，且契约硬约束 metadata.name == 批准靶名——只能建一个），故批准靶
# 必须是受害 Deployment；webhook 配置在资源准备/注入阶段作为机制道具创建，在爆炸半径中显式枚举
# （victim=受害 Deployment / mechanism=webhook 配置 / blast-radius=webhook selector 命中的 Pod 准入
#  三域分离）。
fault_scope: deployment
# 机制写入集立法（write-set approval contract）：受害 Deployment 是批准靶自身
# （drill-wh-victim-<hash>，经 drill-target manifest 契约 `kubectl apply -f -` 单 Deployment 文档
# 自建，screener 以普通 drift net 锚定其 metadata.name=approved 身份、登记为 occupant_deployment
# 载体资产供清理链删除），名字匹配放行、不在本清单重复立法。故障机制另需创建一个演练道具
# ValidatingWebhookConfiguration（drill-wh- 前缀、cluster-scoped 新对象）——
# validatingwebhookconfiguration 既不在 deployment 批准靶的 OWNER_SCOPES，也不在其 secondary_scopes
# （fault_registry family scopes 无 validatingwebhookconfiguration），drift 守卫第 4 步对
# approved=deployment / effective=validatingwebhookconfiguration 判 scope drift 直接拒绝，注入到不了
# execute 阶段的 armed 门（与 priorityclass / ServiceAccount 的 RBAC 三对象、NetworkPolicy误配 同点
# REJECT_DRIFT；补机制写豁免后放行）。validatingwebhookconfiguration 是 cluster-scoped、无 namespace，
# 故本 entry 不带 namespace_from（effective.namespace 为空、entry.namespace 默认空即匹配，同
# priorityclass / node 类 cluster-scoped entry 形态）；name_prefix 锁死 drill-wh- 前缀，禁触及既有
# 生产 webhook。恢复载体栈（cluster 五件套：裸 Pod + SA + ClusterRole + ClusterRoleBinding，同名
# drill-rc-<hash>）走 cluster-scoped 授权面（删 webhook 配置）+ resourceNames 锁名的 Deployment 删除，
# 不在本清单立法。
mechanism_writes:
  - scope: validatingwebhookconfiguration
    name_prefix: drill-wh-
---

**用例名称** 回调服务不可达 导致 Webhook_准入故障

## 故障定位
准入型故障——集群里存在一个 `ValidatingWebhookConfiguration`，其 `failurePolicy: Fail` 且回调后端
（`clientConfig.service` 指向的 Service）不可达（本案建模为后端 Service 不存在，apiserver 准入时调用
webhook 直接失败）。apiserver 在处理匹配该 webhook 规则的资源准入请求时，必须先同步回调 webhook 后端
拿到 allow/deny 裁决；后端不可达 → 调用失败 → 按 `failurePolicy: Fail` 语义**拒绝该准入请求**（而非
放行）。于是所有被 webhook 规则 + selector 命中的 Pod 创建请求被拒，其上层 Deployment 的 ReplicaSet
持续 `FailedCreate`、拉不起任何 Pod（可用副本恒为 0）。**webhook 配置存活即故障存活**（只要它在位且
后端不可达，匹配资源的准入就持续被拒），贯穿整个故障窗口；窗口到期载体 timer 删除 webhook 配置，准入
放行，ReplicaSet 自动重试补齐 Pod、Deployment 自愈。

受害者身份 vs 机制攻击面 vs 爆炸半径三域分离：**受害者**是被拒绝准入、无法拉起 Pod 的自建受害
Deployment（其必现现象 = 0/1 可用、ReplicaSet `FailedCreate` 事件含 `failed calling webhook ...
service not found`，验证「应用依赖的准入 webhook 后端不可达时能否被及时发现与自愈」）；**机制攻击面**
是自建的演练道具 ValidatingWebhookConfiguration（drill-wh-<hash>，failurePolicy=Fail + 不可达后端）；
**爆炸半径**限于 webhook 的 `objectSelector`（+ 可选 `namespaceSelector`）命中的那批 Pod 准入请求——
本案 objectSelector 锁死受害 Deployment 自身的 `app` 标签，故波及面收敛到「仅受害 Deployment 的 Pod
CREATE」，集群其余一切资源准入不受影响（资源准备阶段显式核实）。

手段结论：手段1（ChaosBlade）**不适用**——ChaosBlade 无准入 webhook 类故障靶点（其 K8s 执行器面向
pod/node/container 的 cpu/mem/network/disk/process，不含 admissionregistration 准入面），本用例为
kubectl-native 专属注入（create ValidatingWebhookConfiguration + create 受害 Deployment）。注入=建两个
新对象、恢复=删这两个对象，均非「patch 既有靶标字段」，故装配器 M1 不适用，恢复走标准件载体 timer。

判据可达性：准入拒绝是 apiserver 在 webhook 后端不可达时**即时**（受 `timeoutSeconds` 上界约束，本案
设 5s 快速失败）做出的裁决，不像 volume/env 类需 kubelet 同步、也不像抢占需调度收敛窗——webhook 配置
一旦在位，受害 Deployment 的 Pod CREATE 立即被拒、ReplicaSet 立即 `FailedCreate`。**机制判据**（白盒）
= webhook 配置在位且 failurePolicy=Fail、回调后端不可达；**行为判据**（业务影响，本案主证）= 受害
Deployment 可用副本恒 0、ReplicaSet `FailedCreate` 事件含 webhook 拒绝信息。二者在故障存活期内均可采集。

自证链与 duration 契约：`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认；下限取系统最小
窗口地板并宁宽勿窄——窗口须覆盖 planning+execute 全程 + 一个观察窗（准入拒绝即时生效，无需额外收敛等待）。

## 恢复动作配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：DELETE 演练道具 webhook 配置
（**故障恢复的充分动作**——webhook 一旦删除，准入放行、ReplicaSet 自动重试补齐 Pod）+ DELETE 受害
Deployment（drill 零残留清理）。**注入含 create 新对象、恢复含 DELETE 对象，装配器不适用**（M1 注入域
只承载 patch 既有靶标字段）——恢复走标准件载体 timer（cluster 五件套变体）自治 + Agent 主动兜底双通道，
配方如下：

```yaml
targetRef:                                # 恢复主靶 = webhook 配置（删它即准入放行=故障恢复）；受害 Deployment 为 drill 残留清理
  kind: ValidatingWebhookConfiguration
  name: drill-wh-<hash>
patches:                                  # 注入域：create webhook 配置 + create 受害 Deployment（execute 计划 kubectl 步骤，非 json-patch）
- op: create
  path: /apis/admissionregistration.k8s.io/v1/validatingwebhookconfigurations
  value: <下方演练步骤 4 的完整 ValidatingWebhookConfiguration YAML>
- op: create
  path: /apis/apps/v1/namespaces/<namespace>/deployments
  value: <下方演练步骤 5 的完整受害 Deployment YAML>
restorePatches:                           # 恢复域：载体 TTL 到点自治 DELETE 两对象；Agent 死亡后 recover 从台账重放同源
- op: delete
  path: /apis/admissionregistration.k8s.io/v1/validatingwebhookconfigurations/drill-wh-<hash>
- op: delete
  path: /apis/apps/v1/namespaces/<namespace>/deployments/drill-wh-victim-<hash>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 恢复由标准件载体 TTL 自治承载（curl DELETE，幂等——对象已不存在时返回 404，restore.log 如实记录即
  「已恢复」证据）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行
  （先到先收敛、后到读回 404/no-op）。**删除 webhook 配置是故障恢复的充分动作**（准入放行→ReplicaSet
  自动重试补齐 Pod→受害 Deployment 自愈 1/1）；删除受害 Deployment 是 drill 零残留清理。
- 载体授权面（cluster 五件套变体，recovery-carrier.md 第二节 cluster 单栈形态）：ClusterRole 两条
  resourceNames 锁名规则——`get,delete validatingwebhookconfigurations`（锁 drill-wh-<hash>，cluster-scoped）
  + `get,delete deployments`（锁 drill-wh-victim-<hash>），ClusterRoleBinding 绑集群级；锁名后即便集群级
  绑定也只能删这两根演练对象。自断授权尾步（第四节）以两步建栈法追加 `delete clusterrolebindings`
  （resourceNames 锁 drill-rc-<hash>）删自己的 Binding。
- 受害 Deployment **不被本 case 直接修改**（准入拒绝是 apiserver 行为，本案只创建触发它的 webhook 配置）
  ——故无受害 Deployment 逆 patch 条目；其恢复（Pod 补齐）由 ReplicaSet 控制器在 webhook 删除后自动重试。

## 故障现象
1. 演练道具 ValidatingWebhookConfiguration 在位、`failurePolicy=Fail`、`clientConfig.service` 指向一个
   不存在/无端点的后端 Service（回调不可达）
2. **（本案主证）** 受害 Deployment 可用副本恒为 0（`kubectl get deploy` 显示 0/1），其 ReplicaSet 反复
   `FailedCreate`：`kubectl get events` 出现 reason=`FailedCreate` 事件，message 含
   `failed calling webhook "drill-unreachable.chaos.local": ... service "drill-wh-backend-<hash>" not found`
   （或 `no endpoints available` / `connection refused`，取决于后端不可达的具体形态）
3. 受害 Deployment 名下**零 Pod**（`kubectl get pods -l app=drill-wh-victim-<hash>` 空）——每个 Pod CREATE
   请求都被 webhook 拒绝，从未成功建出
4. **爆炸半径收敛确证**：集群其余命名空间/其余标签的 Pod 创建**不受影响**（`kubectl get events -A
   --field-selector reason=FailedCreate` 只有 drill 受害 Deployment 一条，无其他 workload 被误拦）
5. **webhook 配置自身正常在位**（不崩溃、不报错）——它是故障的施加者而非受害者；受害 Deployment 对象
   本身也成功创建（webhook 规则只拦 pods CREATE、不拦 deployments CREATE），故判据锚在「Pod 拉不起 +
   ReplicaSet FailedCreate」而非 Deployment 对象缺失

## 资源准备
1. **确认回调后端不可达形态（规划期义务）**：本案把「回调服务不可达」建模为 `clientConfig.service` 指向
   一个**不存在**的后端 Service（`drill-wh-backend-<hash>`，全程不创建它）——apiserver 准入调用 webhook
   时解析不到该 Service，立即失败（`service not found`），按 failurePolicy=Fail 拒绝准入。预检确认该
   Service 名当前不存在（`kubectl get svc drill-wh-backend-<hash> -n <namespace> -o name` 预期 NotFound），
   避免与既有 Service 撞名导致「后端实际可达、故障不发生」。
2. **枚举爆炸半径（收敛核实的直接输入，tasks 硬要求）**：webhook 的 `objectSelector` 锁死受害 Deployment
   自身的 `app: drill-wh-victim-<hash>` 标签（hash 唯一），故被拦范围 = 仅该 Deployment 的 Pod CREATE。
   预检确认集群内**无任何其他 Pod 带此标签**（`kubectl get pods -A -l app=drill-wh-victim-<hash>` 预期
   空）——若有则说明 hash 撞车，须换 hash 重规划。此即爆炸半径声明：**仅受害 Deployment 的 Pod 准入**，
   严禁把 objectSelector 写成空 matchLabels（空选择器 = 命中全集群所有 Pod = 准入全面瘫痪，见注意事项）。
3. 确认集群无同名演练 webhook / 受害 Deployment（命名冲突预检：`kubectl get
   validatingwebhookconfiguration drill-wh-<hash> -o name` 与 `kubectl get deploy drill-wh-victim-<hash>
   -n <namespace> -o name` 均 NotFound 即无冲突）。
4. **守卫登记前置核实（本 case 硬前置，否则 apply 被拦）**：确认 `validatingwebhookconfiguration` 已进
   `ALLOWED_MANIFEST_KINDS`（否则 `kubectl apply -f -` 建 webhook 在工具层被 BANNED）+ `KIND_ALIASES`
   （singular/plural/abbrev，供 mechanism_writes scope 与恢复 DELETE 的 canonicalise 匹配）+
   `CLUSTER_SCOPED_KINDS`（正确表达其 cluster-scoped 命名空间拓扑）——与 priorityclass/networkpolicy/
   limitrange 加白名单同构（3 处），详见注意事项「守卫改动前置」。
5. 按**恢复载体标准件**（`references/carrier/recovery-carrier.md` 第二节 cluster 单栈变体）自建 cluster
   五件套——同名 `drill-rc-<hash>`，SA/ClusterRole/ClusterRoleBinding 必须用 `kubectl create` 构造
   （`apply -f` 对 RBAC kinds 因白名单不收被设计性拦截）。**载体必须在 webhook 配置之前部署**（时序约束①，
   见注意事项）；本案 objectSelector 已在 Pod 粒度排除载体（载体 Pod 无 `app: drill-wh-victim-<hash>`
   标签），故载体可与受害靶同命名空间而不被 webhook 拦截（时序约束③）：
   ```bash
   kubectl create sa drill-rc-<hash> -n <namespace>
   kubectl create clusterrole drill-rc-<hash> \
     --verb=get,delete --resource=validatingwebhookconfigurations --resource-name=drill-wh-<hash> \
     --verb=get,delete --resource=deployments --resource-name=drill-wh-victim-<hash>
   kubectl create clusterrolebinding drill-rc-<hash> --clusterrole=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=curlimages/curl:latest --restart=Never \
     --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   ```
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节——验权探针
   GET 目标 webhook 配置/Deployment，对象尚未创建时 404 属预期、判据是「非 403」）；自断授权尾步按标准件
   第二节两步建栈法 json-patch 给 ClusterRole 追加一条 `delete clusterrolebindings`（resourceNames 锁
   drill-rc-<hash>），**严禁并入 create 命令**（pflag 并集复制污染主授权规则）。演练结束后按标准件第六节
   四连删除并带外核实零残留（Pod/ClusterRoleBinding/ClusterRole/SA）。

## 演练步骤
（主路径 = 基线捕获（步骤 1）→ 标准件武装（步骤 3，`<recovery-seconds>` 取 prompt 下发的
recovery_timer_seconds）→ create webhook 配置（步骤 4）→ create 受害 Deployment（步骤 5）；本 case 注入
是 create 两个新对象，无装配器调用。**创建序不可颠倒**：①载体必须先于 webhook 部署并武装（时序约束①+
armed-before-inject 门）；②webhook 必须先于受害 Deployment 存在，否则受害 Deployment 的 Pod 会先被正常
创建、故障不显形）：

> **爆炸半径分类（定案，defer SKILL.md L41 + recovery-carrier §9）**：声明
> `blast_radius_scope=cluster-wide`——双重依据：①故障本体写入集含新建 **cluster-scoped** 演练道具
> ValidatingWebhookConfiguration（跨命名空间的集群级资源写入，按 SKILL.md L41 抬到 cluster-wide）；
> ②恢复走 cluster 五件套 carrier，新增集群级 RBAC 授权面（§9）。consequence 影响面 = 仅 objectSelector
> 命中的受害 Deployment 的 Pod 准入被拒（资源准备第 2 条核实），写入 `blast_radius_detail`；**注意
> cluster-wide 的是「写入集/授权面」维度，实际准入影响面被 objectSelector 收敛到单一受害 Deployment**
> （二者不矛盾：webhook 对象本身是集群级资源，但其拦截作用域由 selector 严格限定）。

1. **基线捕获**：记录命名空间名、受害 Deployment 与 webhook 配置均不存在、回调后端 Service 不存在、
   集群内无 Pod 带 `app: drill-wh-victim-<hash>` 标签（两条路径共用）。
2. **确认 hash 与命名**：drill-wh-<hash>（webhook 配置）/ drill-wh-victim-<hash>（受害 Deployment）/
   drill-wh-backend-<hash>（不存在的回调后端）/ drill-rc-<hash>（恢复载体）四名同 hash 前缀，登记为
   任务侧资产归属。
3. **先武装定时自恢复，再注入 webhook**（资源准备第 5 条的 cluster 五件套武装命令——恢复命令幂等：
   定时器到期 DELETE webhook 配置 + 受害 Deployment 为主，Agent 演练结束时主动执行同两条 DELETE 兜底，
   对象已不存在时 404 即「已恢复」证据，重复执行无副作用）：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <recovery-seconds>; \
     curl -sf -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       https://kubernetes.default.svc/apis/admissionregistration.k8s.io/v1/validatingwebhookconfigurations/drill-wh-<hash>; \
     curl -sf -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/drill-wh-victim-<hash>; \
     curl -sf -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       https://kubernetes.default.svc/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/drill-rc-<hash> \
   ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （倒计时从武装时刻起算：先校验后武装、与注入紧邻 ≤60s；武装后发生任何修复须先停旧定时器再全额重武装：
   `kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f drill-wh-<has[h]; true'`，见 SKILL.md
   安全红线「故障窗口完整」。自断授权尾步为最后一条 `curl -sf ... clusterrolebindings`。）
4. **注入：创建演练道具 ValidatingWebhookConfiguration**（failurePolicy=Fail + 不可达后端 + selector 双重
   收敛；**rules 只匹配 core "" 的 pods CREATE，绝不匹配 admissionregistration.k8s.io**——时序约束②，
   否则 webhook 拦截自身创建/删除即死锁）：
   ```bash
   cat <<'EOF' | kubectl apply -f -
   apiVersion: admissionregistration.k8s.io/v1
   kind: ValidatingWebhookConfiguration
   metadata:
     name: drill-wh-<hash>
   webhooks:
   - name: drill-unreachable.chaos.local      # webhook 名（DNS 子域形态，apiserver 要求）
     failurePolicy: Fail                      # 核心：Fail=回调失败即拒绝准入（对照 Ignore=忽略失败放行）
     sideEffects: None                        # 无副作用声明（apiserver v1 必填）
     admissionReviewVersions: ["v1"]          # 支持的 AdmissionReview 版本（v1 必填）
     timeoutSeconds: 5                        # 快速失败上界，避免准入长时间挂起
     objectSelector:                          # 收敛（本案主收敛轴）：只拦带受害 Deployment app 标签的 Pod
       matchLabels:                           #   ——载体 Pod / 集群其余 Pod 无此标签故永不被拦（时序约束③）
         app: drill-wh-victim-<hash>          #   hash 唯一；严禁空 matchLabels（=命中全集群 Pod=准入瘫痪）
     rules:                                   # 时序约束②：只匹配 pods CREATE，不碰 admissionregistration
     - apiGroups: [""]
       apiVersions: ["v1"]
       operations: ["CREATE"]
       resources: ["pods"]
       scope: "Namespaced"
     clientConfig:
       service:
         name: drill-wh-backend-<hash>        # 不存在的后端 Service = 回调服务不可达（资源准备第 1 条）
         namespace: <namespace>
         port: 443
         path: /validate
   EOF
   ```
   （生产环境**推荐叠加 `namespaceSelector`**（matchLabels 锁定演练命名空间）与 objectSelector 组成双重
   收敛——即便某一 selector 误配，另一层仍限定作用域；本案 objectSelector 的 hash 唯一标签已收敛到单一
   受害 Deployment，等价于最严格的作用域限定，故未新建/标记专用 ns 以免污染既有命名空间，详见注意事项。）
5. **注入：创建受害 Deployment**（drill-target manifest 契约：单 Deployment 文档、
   metadata.name=drill-wh-victim-<hash> 显式声明〔= approved 身份〕、恰一容器无 initContainers、
   image ∈ 载体允许集、无 host*/privileged/capabilities/hostPath、零卷或仅 PVC/configMap/secret 卷；
   pod 模板带 `app: drill-wh-victim-<hash>` 标签〔= 步骤 4 objectSelector 命中轴，亦是 Deployment 自身
   selector，功能必需、非额外演练标记〕）：
   ```bash
   cat <<'EOF' | kubectl apply -f -
   apiVersion: apps/v1
   kind: Deployment
   metadata:
     name: drill-wh-victim-<hash>
     namespace: <namespace>
   spec:
     replicas: 1
     selector:
       matchLabels:
         app: drill-wh-victim-<hash>
     template:
       metadata:
         labels:
           app: drill-wh-victim-<hash>        # objectSelector 命中此标签 → 该 Deployment 的 Pod CREATE 被 webhook 拦截
       spec:
         containers:
         - name: victim
           image: busybox:1.36                # ∈ 载体允许集（drill-target 契约 T4 要求）；受害负载只 sleep，busybox 足够
           command: ["sleep", "<duration+1800>"]
   EOF
   ```
   预期：Deployment **对象**创建成功（webhook 规则只拦 pods、不拦 deployments），但其 Pod CREATE 被
   webhook 拒绝 → ReplicaSet `FailedCreate` → Deployment 卡 0/1（这正是故障形态，非注入失败）。

## 注入验证
1. **（机制判据）** webhook 配置在位、failurePolicy=Fail、回调后端不可达：
   ```bash
   kubectl get validatingwebhookconfiguration drill-wh-<hash> \
     -o jsonpath='{.webhooks[0].failurePolicy}{"\n"}{.webhooks[0].clientConfig.service.name}{"\n"}'
   kubectl get svc drill-wh-backend-<hash> -n <namespace> -o name 2>&1 | grep -i "not found"
   ```
   预期：failurePolicy=`Fail`、clientConfig.service.name=`drill-wh-backend-<hash>`；后端 Service NotFound
   （回调不可达确证）。
2. **（行为主证，必做）** 受害 Deployment 卡 0/1、ReplicaSet `FailedCreate` 含 webhook 拒绝信息：
   ```bash
   kubectl get deploy drill-wh-victim-<hash> -n <namespace> \
     -o jsonpath='ready={.status.readyReplicas} available={.status.availableReplicas}{"\n"}'
   kubectl get pods -n <namespace> -l app=drill-wh-victim-<hash> --no-headers   # 预期 0 行（无 Pod 建出）
   kubectl get events -n <namespace> --field-selector reason=FailedCreate -o wide
   kubectl describe rs -n <namespace> -l app=drill-wh-victim-<hash> | grep -iA1 "failed calling webhook"
   ```
   预期：readyReplicas 空/0、availableReplicas 空/0；零 Pod；至少一条 `FailedCreate` 事件，message 含
   `failed calling webhook "drill-unreachable.chaos.local"` + `service "drill-wh-backend-<hash>" not found`
   （或 `no endpoints available` / `connection refused`）。**采集时机**：准入拒绝即时（受 timeoutSeconds=5
   上界），注入后数秒内 ReplicaSet 即 `FailedCreate`，无需长收敛窗。
3. **（爆炸半径收敛确证，必做）** 集群其余 workload 准入不受影响：
   ```bash
   kubectl get events -A --field-selector reason=FailedCreate -o custom-columns=NS:.metadata.namespace,OBJ:.involvedObject.name | sort -u
   ```
   预期：`FailedCreate` 仅出现在 drill 命名空间、仅涉受害 Deployment 的 ReplicaSet；无任何其他 ns/workload
   被误拦（objectSelector 收敛生效的确证）。

> ⚠️ 验证纪律：受害 Deployment 卡 0/1 + ReplicaSet FailedCreate 是**预期故障态**（正是 webhook 拦截的
> 结果），不得因见「Pod 拉不起」就误判注入失败而重发；同一事实（webhook 在位、Deployment 0/1、拒绝事件）
> 确认一次即可，不要重复轮询。

**持续性检查**——故障窗口内 webhook 拦截必须持续存活（webhook 配置在位 + 后端不可达即准入持续被拒）：
以「注入生效确认」为时点锚（注入验证第 1-3 条通过 = 生效），生效后隔多久复测、复测几次由你依本故障形态自判（配置型：接线在即故障在，无传播过程；无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**（效果证据须在故障存活期内采集，恢复后无法再采）：
1. 白盒复查：webhook 配置仍存在、failurePolicy 仍 Fail（同注入验证第 1 条）
2. 行为复查：受害 Deployment 仍 0/1、ReplicaSet 仍 FailedCreate、仍零 Pod（无迟到的成功创建）
3. 收敛复查：全集群 `FailedCreate` 仍仅限受害 Deployment（爆炸半径未外溢）

## 注入恢复
（主路径下恢复由载体 TTL 自治承载：删除 webhook 配置（准入放行=故障恢复）+ 删除受害 Deployment（零残留）。
Agent 演练结束时主动执行同两条 DELETE 兜底，幂等——对象已不存在时 404 即「已恢复」证据；并停掉已武装的
timer 避免迟到重放。以下手动命令为降级兜底形态）：
1. 删除演练道具 webhook 配置（**故障恢复的充分动作**——准入放行后 ReplicaSet 自动重试补齐 Pod、受害
   Deployment 自愈）：
   ```bash
   kubectl delete validatingwebhookconfiguration drill-wh-<hash> --ignore-not-found
   ```
2. 删除受害 Deployment（drill 零残留清理）：
   ```bash
   kubectl delete deploy drill-wh-victim-<hash> -n <namespace> --ignore-not-found
   ```
3. 受害 Deployment 的 Pod 无需手动重建——若只删 webhook 配置（步骤 1）而暂留 Deployment，其 ReplicaSet
   控制器会在准入放行后自动补齐副本、Pod 转 Running（这是「准入恢复」的活体确证，见恢复验证第 2 条）。
4. 载体五件套按资源准备第 5 条尾四连删除（Pod/ClusterRoleBinding/ClusterRole/SA），带外核实零残留。

> ⚠️ webhook 配置无 activeDeadlineSeconds 自到期能力（不同于 occupant pod 契约的 C4），删除**完全依赖**
> 注入前武装的载体 timer（Agent 死亡时自治）或 `blade-ai recover`（台账重放）或 Agent 主动兜底；未武装即
> 注入会导致 webhook 长期拦截准入（故障滞留），且若 rules 误含 admissionregistration 会自锁无法删除——
> 故「恢复武装先行 + rules 不碰 admissionregistration」是本案 IMMUTABLE 红线。

## 恢复验证
1. webhook 配置已删除：`kubectl get validatingwebhookconfiguration drill-wh-<hash> -o name` 预期 NotFound
   （404 即删除确证）
2. **准入恢复确证**：删 webhook 后（若受害 Deployment 尚在）其 ReplicaSet 自动重试、Pod 成功创建、
   Deployment 回到 1/1——`kubectl get deploy drill-wh-victim-<hash> -n <namespace> -o
   jsonpath='{.status.readyReplicas}'` 预期 1；或新建一个探针 Pod（`kubectl run drill-probe-<hash>
   --image=busybox:1.36 -n <namespace> --restart=Never --command -- sleep 30`）能被正常准入创建（webhook
   已删、准入放行），随后删除探针
3. 受害 Deployment 已删除（零残留）：`kubectl get deploy drill-wh-victim-<hash> -n <namespace> -o name`
   预期 NotFound
4. 确认载体/timer 取证：`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`（若载体尚在）
   或直接核实四件套已删；ClusterRoleBinding 自断授权尾步已消费（无迟到重放）
5. 全集群零残留核实：drill-wh-<hash> / drill-wh-victim-<hash> / drill-rc-<hash> 四件套 / 探针 Pod 全部
   NotFound

## 基准事实
- **根因**：一个 `failurePolicy: Fail` 的 ValidatingWebhookConfiguration，其回调后端（clientConfig.service
  指向的 Service）不可达；apiserver 准入匹配资源时同步回调 webhook 失败，按 Fail 策略拒绝该准入请求，
  模拟「准入 webhook 后端宕机/网络不可达导致集群无法创建匹配资源」的真实事故（如策略准入 webhook 的
  后端 Pod 全挂、证书/网络故障致回调超时）
- **必现现象**：webhook 配置在位且 failurePolicy=Fail、回调后端不可达；被 objectSelector 命中的受害
  Deployment 的 Pod CREATE 被拒、ReplicaSet `FailedCreate`（message 含 `failed calling webhook ...
  service not found`）、Deployment 卡 0/1、零 Pod
- **failurePolicy 语义分界（本案核心教学点）**：`Fail`=回调失败即**拒绝**准入（保守，故障显形为止损）；
  `Ignore`=回调失败即**放行**准入（宽松，故障静默、准入不受影响）。本案用 Fail 才能观测到「回调不可达→
  准入被拒」；若改 Ignore，则受害 Deployment 的 Pod 会被正常创建、故障不显形（可作反证对照，但本案保持 Fail）
- **与 2.2 Webhook 证书过期的分界**：本案是回调**连接层**不可达（service not found / no endpoints /
  connection refused）；证书过期案是回调**TLS 层**失效（`x509: certificate has expired` / TLS handshake
  failure）——两者都触发 failurePolicy=Fail 拒绝准入，但失败原因（连接 vs 证书）、修复手段（恢复后端
  可达 vs 更换证书）不同
- **与 Ingress TLS 证书 / kubelet 证书的分界**：webhook 证书影响**集群内准入变更**（apiserver→webhook
  东西向调用）；Ingress TLS 证书影响**外部入站访问**（南北向）；kubelet 证书影响**节点通信**——三类证书
  故障影响链完全不同
- **作用域边界**：只有 objectSelector（+ 可选 namespaceSelector）命中的 Pod CREATE 被拦；其他命名空间、
  其他标签的 Pod、非 CREATE 操作（UPDATE/DELETE）、非 pods 资源（deployments/services 等）均不受影响；
  webhook 配置被删除后故障即消

## 注意事项
- ChaosBlade 无准入 webhook 类故障靶点，本用例为 kubectl-native 专属注入（create webhook 配置 + create
  受害 Deployment）
- **failurePolicy=Fail 是最高危配置对象（本案最大风险点）**：Fail 意味着回调失败即拒绝准入。若 selector
  未收敛（objectSelector 空 matchLabels、或无 namespaceSelector 且 objectSelector 宽泛），会拦掉**全集群**
  匹配资源的准入 → 大面积无法创建 Pod（可能波及系统组件、导致集群瘫痪）。本案用 objectSelector 锁死受害
  Deployment 自身的 hash 唯一 `app` 标签，把准入影响面收敛到「仅该 Deployment 的 Pod CREATE」；**生产环境
  强烈建议 namespaceSelector + objectSelector 双重收敛，且严禁用无 selector / 空 selector 的
  failurePolicy=Fail webhook 做演练**
- **时序自锁三约束（必须同时满足，否则注入即死锁或无法武装恢复）**：
  ① **先部署恢复载体、再 apply webhook**——载体 Pod 若晚于 webhook 创建且被规则命中，则载体自身建不出来、
     无法武装 timer（本案 objectSelector 已排除载体 Pod，但仍按①先部署载体再武装、后注入 webhook）
  ② **webhook rules 绝不匹配 `admissionregistration.k8s.io`**——否则 webhook 会拦截**自身**的创建/更新/
     删除准入请求：注入时 apply webhook 被自己拦（建不出来），恢复时 delete webhook 被自己拦（删不掉）→
     双向死锁。本案 rules 只匹配 core `""` 的 `pods` `CREATE`
  ③ **载体不被 webhook 拦截**——本案用 objectSelector（受害 Pod 的 `app` 标签）在 **Pod 粒度**排除载体
     （载体 Pod 无此标签），比「载体 ns 被 namespaceSelector 排除」的 ns 粒度排除更强（连 drill ns 内
     非受害 Pod 也一并保护），故载体可与受害靶同 ns
- **恢复武装先行（IMMUTABLE，#36 timer 立法）**：webhook 配置无自到期能力，恢复完全依赖注入前武装的载体
  timer / blade-ai recover / Agent 兜底；未武装即注入 = 故障滞留 + 潜在自锁。武装须早于所有 object-write
  步骤（webhook 配置 + 受害 Deployment 均为对象写入注入，触发 armed-before-inject 门）
- **cluster-scoped mechanism_write 待实弹确认**：validatingwebhookconfiguration 是 cluster-scoped、
  mechanism_writes entry 不带 namespace_from（空 ns 匹配），此形态与 priorityclass / node 类 cluster-scoped
  entry 同源但 catalogue 零先例——drift 第 3.6 步对 create validatingwebhookconfiguration 命中该 entry 的
  放行须实弹确认；REJECT 则回溯建模（同 PriorityClass / PDB 案的双派生轴待确认纪律）
- **守卫改动前置（注入前必须落地，否则 apply 被拦）**：`validatingwebhookconfiguration` 需进
  `ALLOWED_MANIFEST_KINDS`（否则 `kubectl apply -f -` 建 webhook 在工具层被 BANNED）+ `KIND_ALIASES`
  （singular `validatingwebhookconfiguration` / plural `validatingwebhookconfigurations` / abbrev，供
  mechanism_writes scope 与恢复 DELETE 的 canonicalise 匹配）+ `CLUSTER_SCOPED_KINDS`（正确表达其
  cluster-scoped 命名空间拓扑，使 drift 命名空间比较视其 ns 维度为 ABSENT）——与 priorityclass 加白名单
  同构（3 处，比 namespaced 的 networkpolicy/limitrange 多一处 CLUSTER_SCOPED_KINDS）
- **回调后端不可达的建模选择**：本案用「clientConfig.service 指向不存在的 Service」（service not found，
  即时失败、无需创建后端道具、避免 service-create 授权复杂度）；等价形态还有「Service 存在但零 endpoints」
  （no endpoints available）、「clientConfig.url 指向黑洞地址」（connection timeout/refused）——三者都触发
  failurePolicy=Fail 拒绝，本案取最简的「不存在 Service」形态
- 演练道具命名带 `drill-wh-` / `drill-wh-victim-` / `drill-rc-` 前缀（任务侧资产归属登记依据）；受害
  Deployment 的 `app: drill-wh-victim-<hash>` 标签是 **objectSelector 功能必需的选择器**（亦是 Deployment
  自身 selector），语义为「webhook 的作用对象选择」而非 recovery-carrier.md 第一节所禁的「演练归属标记
  label」（后者是给资产清理用的 drill- 归属标记）——二者性质不同，不冲突
- **禁止给演练资源加任何演练归属标记 label**（演练须对集群呈现为真实事故，见 recovery-carrier.md 第一节
  命名约定）；资产归属一律靠 name 前缀登记，不靠 label
