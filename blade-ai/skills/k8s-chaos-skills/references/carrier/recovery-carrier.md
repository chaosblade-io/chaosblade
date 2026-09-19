# 恢复载体标准件（recovery-carrier）

**定位**：当故障注入的恢复动作落在 **API 平面**（patch deployment / delete PVC / patch configmap / scale 等修改 K8s API 对象），且集群内没有可用的定时器宿主时，用一套**自建的临时资产栈**承载定时自恢复——不假设集群预装任何工具，不借用任何既有 Pod。这是通用化的标准路径：目标命名空间里四条 `kubectl` 命令建栈、一条 exec 武装、四条 delete 全清。API 平面恢复现有两条路由：apiserver-write 域 case（front matter `recovery_channel: apiserver-write` 声明）优先走 CR 通道（第十一节），CRD 不可装降级本标准件——两形态互斥不叠加，降级链互指见第十一节。

**适用判据**（三条全中才用本标准件）：
1. 恢复动作是 API 对象写（不是节点本地文件/进程操作，也不是 blade 实验回滚）——即恢复住址三分类（第十一节）的 apiserver 写域；此类 case 已声明 `recovery_channel: apiserver-write`，**先试 CR 通道（第十一节）**，CRD 不可装（判定族见第十一节降级链）才落到本标准件
2. 集群无常驻可用宿主（无 ChaosBlade 工具 Pod、无带权限的调试载体）
3. 恢复通道本身未被故障切断（DNS/网络类故障使 apiserver 不可达时，见「降级路径」）

---

## 一、资产栈构造（四对象同名）

**命名约定**：Pod / SA / Role / RoleBinding **四对象共用一个名字** `drill-rc-<task短哈希>`（默认前缀 `drill-rc-`，可用 `BLADE_AI_RECOVERY_CARRIER_NAME_PREFIX` 配置）。同名是任务侧资产归属的登记依据——RBAC 三件按精确同名挂到载体名下参与全清；**禁止**给演练资源加任何演练标记 label（演练必须对集群呈现为真实事故，任务侧追踪承担身份）。

**位置约束**：SA 与载体 Pod 必须建在**被批准的靶点命名空间**内（漂移守卫以二级范围+命名空间锚定放行；建到其他命名空间会被 scope/namespace drift 拦截）。恢复对象在靶 ns 之外时（如还原 kube-system 的 CoreDNS），Role/RoleBinding 须落恢复 ns——见「十、跨命名空间变体」，且必须由用例 mechanism_writes 立法（role/rolebinding @ 恢复 ns + 本标准件前缀）。

```bash
# 1) SA（必须先于 Pod 创建：overrides 挂接的 Pod 引用它，SA 不存在时
#    kubelet 挂不上 token，Pod 卡 ContainerCreating 直到 SA 出现）
kubectl create sa drill-rc-<hash> -n <namespace>

# 2) Role：最小权限，按「verb×resource 推导表」取本用例恢复动作所需。
#    单一资源族直用下方形态；恢复动作跨资源族（如 deployments+secrets）
#    禁止在同一命令叠多组 --verb/--resource——见下方「Role 跨资源族构造立法」
kubectl create role drill-rc-<hash> -n <namespace> \
  --verb=get,patch --resource=deployments

# 3) RoleBinding
kubectl create rolebinding drill-rc-<hash> -n <namespace> \
  --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>

# 4) 载体 Pod：以 drill-rc SA 运行（overrides 挂接，见下文「SA 挂接」），
#    sleep 骨架自过期（骨架时长 = 故障窗口 + 1800s 取证缓冲（2026-09-17 立法升级：#46/#48/#51 三现 restore.log 不可回读——600s 不够 fire 后带外取证节奏，1800s 覆盖「发现异常→诊断链→取证」全程），上限 86400）
#    五条件缺一不可：drill-rc- 前缀 / --restart=Never / --command -- sleep N /
#    镜像在允许集（配置默认集 busybox/curlimages-curl ∪ 任务启动自动发现集——
#    健康 DaemonSet 镜像=全节点缓存，探测消息直接给出候选清单；VPC/受限网络
#    集群优先从自动发现候选中选，零人工配置。人工入口
#    BLADE_AI_RECOVERY_CARRIER_ALLOWED_IMAGES 仅兜底探测失败等少数场景）/
#    镜像工具链硬要求：sh+sleep+curl（第四节武装命令全靠它们）/
#    flag 白名单——只允许 -n/--namespace、--image、--restart、--command、
#    --overrides（见「SA 挂接与调度容忍」），其余任何 flag（--serviceaccount/--env/
#    --nodename/--labels/...）一律 fail-closed（SA 挂接只有 overrides 一条路）
kubectl run drill-rc-<hash> -n <namespace> --image=busybox:1.36 \
  --restart=Never \
  --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>","tolerations":[{"key":"<taint-key>","operator":"Equal","value":"<taint-value>","effect":"NoSchedule"}]}}' \
  --command -- sleep <骨架时长>
```

**Role 跨资源族构造立法（两步法，2026-09-18 实测定案）**：恢复动作跨多个资源族（如 apps/deployments + core/secrets）时，Role 必须收口**单 Role 多 rule**。构造路径按各族动词集是否相同二分——**均匀**（各族同一动词集）单命令天然干净（`--verb=get,patch --resource=configmaps,deployments` 即可，实测各 rule 恰好只携带该动词集）；**非均匀**（各族动词集不同——恢复场景的常态，如 deployments 要 patch 而 secrets 要 delete）单命令只有一条合法路径——两步法：

```bash
# 步骤 1：create 单对第一族（与建栈第 2 步同发）
kubectl create role drill-rc-<hash> -n <namespace> \
  --verb=get,update --resource=deployments
# 步骤 2：四件套建栈、载体 Pod 注册后（patch 在载体注册前被 patch 全局禁令拦，
#        注册后合法——#51-R 实弹），逐族追加独立 rule（无交叉积）；
#        多族可合一条 patch（JSON patch 数组多 op 顺序追加，实测三族一次成型）
kubectl patch role drill-rc-<hash> -n <namespace> --type=json \
  -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["secrets"],"verbs":["get","delete"]}}]'
# 步骤 3：readback 硬门——逐 rule 动词集与设计精确一致（无并集外溢、恢复动词无缺漏；
#        追加 rule 各自独立成条、不与既有 rule 合并——与单命令的同组合并行为不同）
kubectl get role drill-rc-<hash> -n <namespace> -o jsonpath='{.rules}'
```

- **为什么非均匀动词集禁止单命令**（实测：`--verb=get,patch --resource=deployments --verb=get,delete --resource=resourcequotas` → 两条 rule 均携带 [get,patch,delete]；三族 sts/pvc/pods 形态 → 全部 rule 均携带 [get,patch,delete,list]）：kubectl create role 对 `--verb`/`--resource` 无论逗号单 flag 还是多 flag 重复，都是 **verbs 全量并集广播进每条（按 API group 分组的）rule**——「配对」语义不存在，非均匀动词集必产超授权面（delete×deployments + patch×resourcequotas 等），最小权限立法被静默击穿。均匀动词集是实测例外（各 rule 恰好干净）——但恢复场景跨族动词集几乎必然非均匀，拿不准就走两步法。另注意单命令里**同 API group 多资源合并进一条 rule**（pvc+pods 同 core 组实测共一条）；两步法追加的 rule 各自独立成条。同机理判例：下方「自删规则两步建栈法」的 pflag 污染（resourceNames 锁名同样复制进每条 rule）。
- **为什么禁止双 Role 分族**（#51-R 拦截 1 判例）：任务侧 rbac_family 只登记主注册 Role 的 verbs 并集，第二 Role 的动词层 1 对账看不见——武装载荷动词 ⊄ 主 Role verbs 即拒武装。单 Role 多 rule 恰好使全部恢复动词落在同一对账面。
- **为什么不用 manifest 一体法**：kubectl 工具的 `apply -f -` 白名单不收 RBAC kinds（命令式构造是守卫可见性硬要求，见上方「命令式构造」）。
- **SSAR 验权无需补授**：selfsubjectaccessreviews 由 system:basic-user 全局授权，Role 不需要也不应携带 create 该资源（层 1 对 review 族 POST 已立法豁免）。

⚠️ **调度容忍（tolerations，企业集群常备）**：集群常把**所有节点**打上 taint（资源池隔离 / 专用节点），无容忍的载体 Pod 会永远 Pending。需先 `kubectl get nodes -o jsonpath='{range .items[*]}{.spec.taints}{"\n"}{end}'` 探测 taint 形态，把载体需要容忍的 taint 写进 overrides 的 `spec.tolerations`（结构白名单：每项仅 key/operator/value/effect 四键，key 必填——空 toleration 匹配一切 taint 属 fail-closed；不给任何运行时特权）。（**定案免推导**：需容忍的只有**建栈时刻已存在**的固有 taint——演练**注入**的 taint 无需写 toleration：载体 Pod 先建先跑，注入时已 Running（NoSchedule 不驱逐已运行 Pod，只挡新调度）；`--restart=Never` 下载体死亡不重建（toleration 无处生效）；定时器 fire 走 apiserver REST 不经调度器。三条机理闭环，勿再推演载体与注入 taint 的竞态。）

**SA 挂接与调度容忍**：`kubectl run` 无法在命令行指定 serviceAccountName（属 Pod spec 字段），主路径用上方第 4 步的 `--overrides` 挂接——载体 Pod 以 drill-rc SA 运行，`/var/run/secrets/kubernetes.io/serviceaccount/` 下即该 SA 的 token，第三节验权与第四节武装命令直接读它，整条链自洽。

`--overrides` 会 patch 原始 Pod spec，守卫对其做**白名单解析**：spec 仅允许 `serviceAccountName`（非空字符串）与 `tolerations`（结构化调度容忍，见上方警示）两个调度键，塞入 hostNetwork/privileged/hostPath 或任何其他字段的 overrides 都会使形态判定 fail-closed（落回普通漂移审查）。

备选——显式 token：载体 Pod 保持 default SA 运行，恢复调用显式携带 `kubectl create token` 签发的目标 SA token（嵌入 exec 载荷）。注意 token 有签发期限，**须确认期限 ≥ 故障窗口 + 缓冲**，否则定时器到点 401；且此路径下第三节的验权命令也须改用显式 token（Pod 内 `/var/run` 下的 token 是 default SA 的，验它没有意义）。

**命令式构造是硬要求**：SA/Role/RoleBinding 必须用 `kubectl create` 命令构造——`apply -f` 因清单内容对守卫不可见被设计性拦截，命令式参数对守卫全可见，是天然审计面。

## 二、verb×resource 推导表（恢复动作 → 最小权限）

| 恢复动作 | API 请求 | Role 定义 |
|---|---|---|
| 缩回 workload 副本（scale 回基线） | `PATCH /apis/apps/v1/namespaces/<ns>/deployments/<name>/scale` | `--verb=get,patch --resource=deployments/scale`（或整对象 `deployments`） |
| 还原 patch 过的 Deployment 字段（摘卷/还原镜像等） | `PATCH /apis/apps/v1/namespaces/<ns>/deployments/<name>` | `--verb=get,patch --resource=deployments` |
| 还原 patch 过的 ConfigMap | `PATCH /api/v1/namespaces/<ns>/configmaps/<name>` | `--verb=get,patch --resource=configmaps` |
| 删除演练期创建的 PVC | `DELETE /api/v1/namespaces/<ns>/persistentvolumeclaims/<name>` | `--verb=get,delete --resource=persistentvolumeclaims` |
| 删除演练期创建的 ResourceQuota | `DELETE /api/v1/namespaces/<ns>/resourcequotas/<name>` | `--verb=get,delete --resource=resourcequotas` |
| 验权探测（见第三节） | `GET` 目标资源 | 目标资源的 `get`（与恢复动作同 resource） |
| 到期自断授权（可选尾步，见第四节） | `DELETE /apis/rbac.authorization.k8s.io/v1/clusterrolebindings/<自己名>`（cluster 变体；namespaced 栈为 `/api/v1/namespaces/<ns>/rolebindings/<自己名>`） | **两步建栈法独立追加**（见下方推导原则；namespaced 栈 rolebindings 同构）——独立成条后 `resourceNames` 锁名，只能删自己这一根；**严禁与主恢复 flag 合并**（pflag 并集复制污染主恢复规则，见推导原则） |

推导原则：**Role 只含恢复动作 + 验权探测所需的最小 verb 集**；注入动作本身不进 Role（注入由 Agent 主通道执行，不借载体）。自断授权行的 delete verb 同样最小化：`resourceNames` 锁死自己 Binding 的名字，与「严禁 delete 目标资源」红线不同域——那条防的是载体能删故障目标（如 PV），本行删的是载体自己的 RBAC，对象域不交叠。

**恢复形态无关总则（硬立法——#51 B85 判例：Agent 合法切换恢复形态后 RBAC 授权面未联动，timer fire 时 PATCH×2 403 静默失败 + Pod 假性恢复）**：Role verbs 以**恢复脚本载荷实际使用的全部写动词**为准，不以规划期钦定的形态为准——规划期钦定 PUT、执行期切换为 json-patch（2 PATCH + 1 DELETE）时，授权面必须按实际载荷动词重建。**REST 方法→RBAC 动词映射**：`PATCH→patch`、`PUT→update`、`DELETE→delete`、`POST→create`。武装前的判定铁律：**载荷写动词 ⊄ Role verbs 即不得武装**（例如载荷含 `-X PATCH` 而 Role 只有 `get,update` → 缺 `patch`，中止）。本表左列的 API 请求形态仅是示例推导——Agent 换形态（PATCH↔PUT、增加/减少动作步）时按实际载荷动词重新查表。

**自删规则两步建栈法（硬立法——pflag 合并污染判例，2026-09-17 真实 apiserver 实测）**：自删规则必须在建栈命令之后**独立追加**，**严禁**把 `--verb=delete --resource=<bindings 类> --resource-name=<自己名>` 合并进主授权的 `kubectl create role/clusterrole`——`kubectl create` 对重复 flag 的语义是「同类 flag 各自求并集后**复制进每一条规则**」：合并形态下主恢复规则被 `resourceNames` 锁名污染（锁名规则对未点名资源一律 403——SA 真实 token GET 目标资源 403，第三节验权硬门必中止，case 不可执行）+ delete verb 泄漏进主恢复规则（授权面扩大）。两步形态（cluster 变体；namespaced 栈把 clusterrole/clusterrolebindings 相应换成 role/rolebindings 并带 `-n <ns>`）：

```bash
# 1) 主授权照旧——只带恢复动作所需 flag，不带任何自删 flag
kubectl create clusterrole drill-rc-<hash> --verb=get,patch --resource=persistentvolumes
# 2) json-patch 追加独立自删规则（新规则条独立携带锁名与 delete）
kubectl patch clusterrole drill-rc-<hash> --type=json \
  -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":["rbac.authorization.k8s.io"],"resources":["clusterrolebindings"],"resourceNames":["drill-rc-<hash>"],"verbs":["delete"]}}]'
```

产物读回为**双规则各干净**（第四节「验权读回」两条判据）：主授权规则**无锁名**（GET 真实资源 200 的前提）、自删规则独立成条。实测对照（同栈同探针）：合并形态 SA token GET 目标资源 **403**；两步形态 **200** + timer fire → 主恢复 → 自删 Binding 全链闭环。

**六对象栈的双自删形态（五件套变体启用尾步时，级联检视 F-G2 补立法）**：六对象栈有 Role 与 ClusterRole 两条授权链、两个 Binding——第四节「六对象栈两个 Binding 都删」要求 **Role 与 ClusterRole 各追加一条自删规则**（Role 的规则删自己的 RoleBinding，ClusterRole 的规则删自己的 ClusterRoleBinding；两条授权链互不依赖对方的 Binding，先后删除互不锁死。cluster-only 四对象变体无 namespaced 链，只在 ClusterRole 追加一条——即上方 cluster 单栈形态）：

```bash
# 1) namespaced 自删规则 → Role（删自己的 RoleBinding）
kubectl patch role drill-rc-<hash> -n <namespace> --type=json \
  -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":["rbac.authorization.k8s.io"],"resources":["rolebindings"],"resourceNames":["drill-rc-<hash>"],"verbs":["delete"]}}]'
# 2) cluster 自删规则 → ClusterRole（删自己的 ClusterRoleBinding）
kubectl patch clusterrole drill-rc-<hash> --type=json \
  -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":["rbac.authorization.k8s.io"],"resources":["clusterrolebindings"],"resourceNames":["drill-rc-<hash>"],"verbs":["delete"]}}]'
```

timer 尾步挂接（fail-open 逐环节守）：最后恢复 curl 升 `-sf` 后以 `&&` 链先删 RoleBinding 再删 ClusterRoleBinding（两个 DELETE 均带 `-sf`；任一 DELETE 失败即停，剩余授权保留交收车六连删除）。字节预算挤不下两个 DELETE 时**优先删 ClusterRoleBinding**——集群级授权的残留面大于 namespaced 授权，namespaced Binding 交收车路径幂等清。单删 CRB 取舍形态下，Role 的自删规则可不追加（省一条 patch 命令与授权面；RoleBinding 交收车四连删除幂等清）。双读回命令：cluster 侧 `kubectl get clusterrole drill-rc-<hash> -o jsonpath='{.rules}'`，role 侧同式加 `-n <namespace>`。主恢复 curl 的失败取证包裹（满档/轻档两档、字节从属降档）见第四节「取证包裹律」——六对象栈预算最紧，轻档常是唯一可行档。

**五件套变体（恢复动作含 cluster-scoped 资源写时）**：恢复对象是 node（taint/label 还原）、storageclass 等集群级资源时，namespaced Role 无法授权（cluster-scoped 资源只能经 ClusterRole+ClusterRoleBinding 授权）。栈扩展为六对象：四件套 + 同名 `ClusterRole`（`kubectl create clusterrole drill-rc-<hash> --resource=nodes --verb=get,patch`——按本表推导最小权限）+ 同名 `ClusterRoleBinding`（`--clusterrole=drill-rc-<hash> --serviceaccount=<ns>:drill-rc-<hash>`）。两个集群级对象无 namespace（create/delete 不带 `-n`）。守卫已立法放行（workload 漂移守卫的 secondary_scopes 白名单含 clusterrole/clusterrolebinding）；任务侧 rbac_family 自动挂接两集群级成员，全清为**六连删除**（Pod → RoleBinding → Role → SA → ClusterRoleBinding → ClusterRole，集群级两件漏删会留全局残留）。验权清单相应覆盖**两类对象**（namespaced 目标 + cluster-scoped 目标各一次 GET，任一 403 即中止）。实例参考：`Pod_Pending_节点Taint无对应Toleration.md`（node taints/label + deployment nodeSelector 三恢复，跨两类对象）。

## 三、SA 权限真实 token 验证（武装前必做）

**禁用 `kubectl auth can-i --as=system:serviceaccount:<ns>:<sa>`**：impersonation 走的是**调用方**的权限视图，历史上产生过假放行（can-i 报可执行、真实请求 403）。武装必须用**载体 SA 的真实 token 发一次只读 GET**，以 HTTP 状态码二值判定：

```bash
kubectl exec drill-rc-<hash> -n <namespace> -- sh -c \
  'TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); \
   curl -s -o /dev/null -w "%{http_code}" \
   --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
   -H "Authorization: Bearer $TOKEN" \
   https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<name>'
```

- `200` → 读权限在，继续写动词对账
- `403` → 权限缺失，**中止**（补 Role 前不得武装）
- 非 200/403（超时/000）→ 通道不可达，见「降级路径」

**写动词对账（SSAR，武装前必做——B85 判例立法：GET 200 只验读路径，恢复动作的写动词从未被验证，#51 实弹翻车 PATCH×2 403 静默）**：读探针 200 后，对**恢复脚本载荷的每个写动词**（按第二节映射表从载荷提取）发一次 SelfSubjectAccessReview。SSAR 是 K8s 原生的自我授权检查（无副作用、亚秒级），载体 SA 的授权链是我们自建的 Role→Binding→SA，SSAR 结果与 Role 定义精确一致——这里验的不是未知授权面，而是**「Role verbs 是否覆盖恢复脚本实际写动词」的对账**：

```bash
kubectl exec drill-rc-<hash> -n <namespace> -- sh -c \
  'TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); \
   curl -s --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
   -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
   -X POST https://kubernetes.default.svc/apis/authorization.k8s.io/v1/selfsubjectaccessreviews \
   -d "{\"apiVersion\":\"authorization.k8s.io/v1\",\"kind\":\"SelfSubjectAccessReview\",\"spec\":{\"resourceAttributes\":{\"namespace\":\"<namespace>\",\"verb\":\"patch\",\"group\":\"apps\",\"resource\":\"deployments\"}}}"'
```

⚠️ 字段拼写与结构纪律（#51-R 实弹翻车：首版照抄 apiGroup 错拼，apiserver 静默丢弃未知字段返 allowed:false 假阴性，险些错误中止）：ResourceAttributes 用 `group`，不是 `apiGroup`——`apiGroups` 是上节 Role rule 的拼写，两个结构相似字段勿互串。`spec` 的值必须是**对象** `{"resourceAttributes":{...}}`——丢了 `spec` 后的 `{` 载荷即非法 JSON，apiserver 直接 400 硬错误（拼写错是假阴性、结构错是硬拒绝，两者都过不了对账）。子资源目标（如 `deployments/scale`）直接把 `资源/子资源` 整体填进 `resource` 字段即可——2026-09-18 实弹载体实测与 `resource`+`subresource` 两字段分离形态判定等价（均 allowed）。

**回执自证三态判定（2026-09-18 三探针实测定案：正确/错拼/破语法三形态回执均已实测锚定）**：拿到回执先核对 `spec.resourceAttributes` 回显的 namespace/verb/group/resource 四字段与请求意图逐项一致，再做权限判定——静默忽略式 API 丢字段不报错，但被丢字段同样不会出现在回显里（实测：apiGroup 错拼 → 回显只剩三字段、managedFields 无 f:group，问题已漂移成「无 group 的授权检查」，此时 allowed 值无意义）：

- 回显字段缺失/漂移 → **请求形态错误，不进权限判定**：对照上方模板查拼写，修正重发
- 回显完整 + `"allowed": true` → 该动词已授权，验下一个
- 回显完整 + `"allowed": false` → **中止武装**（缺动词：按第二节形态无关总则补 Role 后重验）
- 通道层错误（超时/解析失败/HTTP 400 json parse error）→ 见「降级路径」

判定铁律：**全部写动词 allowed 才可武装；任一 false 即中止**——宁可在武装前中止（可修可重试），不可在 timer fire 时 403 静默（载体自治段无人监听、curl -sf 吞错、部分恢复的假性恢复比不恢复更隐蔽）。写动词清单的提取源是**恢复脚本载荷本身**（grep `-X PATCH|-X PUT|-X DELETE|-X POST`），不是规划声明——Agent 无论怎么换形态，载荷是最终真相。

## 四、武装命令模板（定时器）

载体 Pod 内 `sh -c` 定时器 + SA token 走集群内 apiserver REST。**恢复命令必须幂等**（定时器迟到与 Agent 兜底重复执行无副作用）。

**长度预算（硬约束）**：部分通道（wiz/kubewiz）对 `sh -c` 内联载荷有 **1024 字节上限**（见第七节）。恢复动作 ≤2 个 curl 用「完整形态」；**≥3 个 curl 或预算吃紧时必须用「紧凑变量形态」的 for 循环变体**（载荷内先赋 `C`/`T`/`U` 变量再引用 + curl 公共参数只写一次，四动作 1629B→657B）。两种形态均为批准形态——**规划期直接套用，勿推敲引号构造，勿自创第三种形态**。

**完整形态**（恢复动作少、总长 < 1024B）：
```bash
kubectl exec drill-rc-<hash> -n <namespace> -- sh -c \
  '( sleep <duration>; \
     curl -s -X PATCH \
       --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       -H "Content-Type: application/merge-patch+json" \
       -d "{\"spec\":{\"replicas\":<基线副本数>}}" \
       https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<name>/scale; \
     curl -s -X DELETE \
       --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       https://kubernetes.default.svc/api/v1/namespaces/<namespace>/resourcequotas/<quota-name> \
  ) >/tmp/restore.log 2>&1 & echo armed'
```

**紧凑变量形态**（多动作恢复，如约束类用例的四连 patch）——变量赋值与引用**全部写在 `sh -c` 载荷内，载体内执行时才展开**（与第三节验权的 `TOKEN` 同构，单层转义）。多动作时**必须用 for 循环变体**（curl 公共参数只写一次，payload 作双引号数组循环）——逐 curl 重复完整命令行的写法在四动作下约 1118B，仍超 1024B 限；for 循环变体同参数约 657B：
```bash
kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); U=https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<name>; for d in "{\"spec\":{<恢复动作1的patch>}}" "{\"spec\":{<恢复动作2>}}" "{\"spec\":{<恢复动作3>}}"; do curl -s -X PATCH --cacert $C -H "Authorization: Bearer $T" -H "Content-Type: application/merge-patch+json" -d "$d" $U; done ) >/tmp/restore.log 2>&1 & echo armed'
```

**形态纪律（三禁一许）**：
- ✅ **许**：上述两种内联形态原样套用（紧凑形态的变量在载体内展开，合法）
- ❌ **禁**：**有空展开点的落盘**——未加引号定界符的 heredoc 或 printf 把恢复命令写入载体内脚本文件再执行，写入层会把 `$T`/`$C` 在**中间 shell 空展开固化**（curl 行变成 `--cacert (空) -H "Authorization: Bearer (空)"`，四 curl 无认证静默失败，定时器正常 fire 但集群未恢复）。**合法落盘形态两种**（写入层无展开点、脚本执行时才展开变量）：**base64 两步法**（第七节铁律 2）与**加引号定界符 heredoc**（`<<'EOF'` 或 `<<"EOF"`——POSIX：定界符任何形式的引用都使 heredoc 字面化，`$`/`$(...)` 不在写入层展开；#44 实证：`<<"RSEOF"` 直书明文 + E9 回显目检门全过）
- ❌ **禁**：token 硬编码进任何脚本/变量（跨层转义不可控，且 token 泄漏进日志）
- ❌ **禁**：把 `>/tmp/restore.log 2>&1` 改回 `>/dev/null 2>&1`——恢复失败必须留取证证据：fire 后带外 `kubectl exec <载体Pod> -- cat /tmp/restore.log` 可确认每个恢复 curl 的实际执行情况（静默吞错 = 恢复失败不可见）

要点：
- **必须以 `sh -c` 作为 exec 载荷**：顶层裸 `( sleep … ) &` 语法 exec-form 通道不解释，直接派发 `sh -c '…'` 顶层命令会被命令守卫拦截（unknown_binary）
- 走 `https://kubernetes.default.svc`（集群内 DNS 直达 apiserver；恢复通道不依赖被攻击的集群 DNS 的场景才适用本标准件）
- **倒计时从武装时刻起算**：武装与注入必须是紧邻步骤（间隔 ≤ 60 秒）；武装前必须先过第三节 SA 真实 token 验权（同一 T 的 GET 200 硬门）——内联模板已验证免 `sh -n`，**禁止为「校验」目的把载荷落成脚本文件**（即上列第一禁）
- **多步注入序列的武装点（定案免仲裁）**：载体建栈与 token 验权属 setup，可前置完成；**武装推迟至第一个故障生效动作紧邻前**。用例步骤常把武装模板写在建栈步（如「步骤 2」）而真正的故障注入动作在后文（滚动等待、多步变更隔在中间）——此时武装点 = 最后一个 setup 步骤完成后、第一个真正引入故障的变更（taint/删 Pod/patch 生效动作等）执行前；勿按模板出现位置机械执行，也勿花推导权衡摆放
- 多个恢复动作用 `;` 串联在同一个定时器里，一个 duration 统一控制；**curl 顺序即执行顺序**——有顺序安全要求（如「约束移除先于 maxUnavailable 还原」）时按序排列
- **`-sf` 吞 body 排障律（2026-09-17 实测立法）**：`-f` 在 4xx/5xx 时丢弃响应体、只回非零退出码——fire 失败时 restore.log 里没有 apiserver 错误详情（t4 死因链在 8.14.1 逐字复现：恢复请求被拒、日志零 body，一度误诊「定时器死了」白查一小时）。**restore.log 近空 ≠ 定时器未跑**。排障顺序：先看日志有无取证包裹输出（尾步「取证包裹律」满档的错误 body / 轻档的 `FAIL rc=22` 标记——`FAIL rc=22` 即证明定时器跑了且被 HTTP 层拒绝，与「没跑」立判）；无包裹形态才带外重放定死因：重放同一条恢复 curl **去掉 `-f`**（或加 `-v`）拿完整错误 body。**勿反向修法**：把模板里的 `-sf` 去掉 = fail-open 挂接律失灵（4xx 静默转 0、恢复失败照样自删授权）；`--fail-with-body` 理论两全但**不进模板**——允许集镜像 curl 版本不齐（实测 terway 8.5.0 / 本地 chaosblade-tool 8.14.1 支持，npd 7.61.1 不识别该 flag，遇之 curl 立即退出、恢复链整条挂），仅诊断重放可用且须先验版本

**自断授权尾步（可选优化步，#36 inject-dfee9d3d 立法）**：恢复全链确认成功后，定时器顺带 DELETE 自己栈里的 Binding（namespaced 栈删 `rolebindings/<自己名>`，cluster 变体删 `clusterrolebindings/<自己名>`；六对象栈两个 Binding 都在字节预算内时都删），把「授权残留窗口」从骨架时长压到 fire 时刻（六对象栈两个 Binding 都删的自删规则构造见第二节「六对象栈的双自删形态」）。挂接形态——**最后一个恢复动作**升级为 `curl -sf` 并以 `&&` 链接自删：

```bash
     curl -sf -X PATCH \
       --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       -H "Content-Type: application/merge-patch+json" \
       -d '{<最后一个恢复动作的patch>}' \
       https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<name> && \
     curl -sf -X DELETE \
       --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       https://kubernetes.default.svc/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/drill-rc-<hash>
```

（紧凑变量形态挂接——R13 检视修正：原「同构」措辞未验证循环退出码语义，实际不构成 fail-open。正确形态**两环 `-f` 缺一不可**：① for 循环内每个恢复 curl 升级为 `curl -sf`；② 自删 DELETE 以 `done && curl -sf -X DELETE --cacert $C -H "Authorization: Bearer $T" https://…/clusterrolebindings/<自己名>` 挂在循环之后，复用 `$C`/`$T` 变量。退出码链条：for 循环的退出码是**最后执行的命令**的退出码——循环内 curl 不带 `-f` 则 4xx/5xx 静默转 0，`done` 恒 0，`&&` 恒真，主恢复失败照样自删、fail-open 形同虚设。）五条纪律：
- **fail-open 挂接律**：`curl -sf` + `&&` 成对出现——`-f` 让 4xx/5xx 转非零退出（DELETE 403/404 不再静默成功），`&&` 保证主恢复未确认成功则授权保留（人工兜底恢复路径完整）。**边界诚实（`&&` 只门末动作）**：`&&` 链感知的只是**最后一个**恢复动作的退出码——前序动作用 `;` 串联且不带 `-f` 时（完整形态），前序失败（4xx 转退出 0）而末动作成功则自删照样执行：授权已删、恢复残缺，载体无法补做，此角落**不在 fail-open 保护内**（不是「授权多活」的反向；紧凑形态 for 循环同此——循环退出码是最后执行命令的退出码），兜底是带外 `blade-ai recover`（Layer1 补恢复幂等重执行残缺动作，finalize 四连删除清残壳）
- **字节预算从属律**：自删 DELETE 计入 1024B 预算；挤不下就不带（恢复完整性优先——第七节铁律 1 禁止缩字段域挤字节，自删同样不得反向侵蚀），授权清退交给收车路径
- **自删对象 = 自己的 Binding，非 Role/SA**：删 Binding 授权链即断（残留惰性 Role/SA 零权限，第六节空壳中间态）；先删 Role 会留悬空 Binding（指向不存在 Role 的迷惑形态）；先删 SA 则 token 立即失效一件都删不成——**自删上限就是一件（或每类 Binding 一件），这是「用自己授权删自己授权」的结构性死锁，非实现妥协**
- **验权读回（双规则各自干净）**：启用尾步时，建栈后 `kubectl get clusterrole drill-rc-<hash> -o jsonpath='{.rules}'`（namespaced 栈读 role、cluster 变体读 clusterrole、**六对象栈 Role 与 ClusterRole 各读一次**——各自适用判据①②）读回 rules，目检**两条判据**：① 主授权规则**无 `resourceNames` 锁名**——锁名在场 = 两步建栈法被违反（自删 flag 被合并进 create 命令），主恢复 GET 目标资源将 403、case 不可执行，中止并按两步建栈法重建栈；② 自删规则独立成条（delete verb + `resourceNames` 锁名在场）。Role 是刚建的、rules 显式，读回即确定性证明（delete verb 无法用 GET 事前实测）
- **取证包裹律（2026-09-17 立法，实测依据同要点区排障律）**：主恢复 curl 可包裹失败取证，两档——**满档**（同参数去 `-f` 重放、错误 body 落 restore.log）：`curl -sf <参数> $U || { curl -s <同参数> $U; false; }`（同参数含 Content-Type，缺了重放变 415、诊断的是另一个错）；**轻档**（贴限保底）：`|| { echo "FAIL rc=$? $d"; false; }`——定位失败动作与错误类别（rc=22 HTTP 层拒绝 / rc=6,7 连接层 / rc=28 超时；紧凑形态 +37B/处，完整形态无 `$d` 变体 33B/处）。**包裹对象 = 带 `-sf` 的 curl**（K1 检视收口：`-f` 才吞 body——裸 `-s` curl 失败时 body 本来就落 restore.log，包裹它纯属浪费字节；完整形态即升 `-sf` 的末动作一处，紧凑 for 形态循环体一处覆盖全部循环内 curl，前序裸 `-s` curl 勿包裹）。**两档的 `false` 都是承重件**：`||` 右侧回 0 会掩掉失败（无 `-f` 的取证 curl 4xx 也回 0、echo 回 0）→ `&&` 链恒真、fail-open 失灵——与上方 R13 修正同病，实测反证：`curl -sf … || echo …` 后链退出码为 0；包裹与 `&&` 链接续语义已验（POSIX `||`/`&&` 等优先级左结合，`A || { …; false; } && C` 中 A 败则 C 不执行）。**字节从属**：包裹计入第七节预算校验，挤不下先降档（满档→轻档）再弃包裹（裸 `-sf`）——满档紧凑形态整载荷 +134B（循环体只写一处、curl 全变量引用故增量与取值无关；四动作 657B 口径 +满档 ≈ 791B 限内；J1 定案用例 ~938B +满档 1072B 超限、+轻档 975B 限内），完整形态逐 curl +342B（`$(cat token)` 直书膨胀，基本只配轻档）；与自删二选一时**自删优先**（自删成功路径每跑兑现、取证只在失败路径兑现且有带外重放兜底）；恢复完整性仍居首（同字节预算从属律）。**自删 DELETE 不包**：其失败属授权残留（收车六连删除兜底）非恢复路径诊断重点。边界诚实：只覆盖「跑了但失败无证据」半边；定时器根本没跑（载体 Pod 死/驱逐）仍靠带外核实载体进程

## 五、重武装纪律

武装后发生**任何**修复（脚本改错、参数重填）必须先停旧定时器再**全额重武装**——修复脚本文件不等于重置倒计时：

```bash
# 停旧定时器（括号法防 pkill 自匹配）
kubectl exec drill-rc-<hash> -n <namespace> -- sh -c \
  'pkill -f "sleep <旧duration数值带一位括号>"; true'
# 例：旧定时器 sleep 600 → pkill -f "sleep 60[0]"
```

随后按第四节模板全额重武装。载体为单副本（--restart=Never 天然单副本），定位不到定时器进程即视为异常，中止本次演练改人工恢复（见 SKILL.md 安全红线「故障窗口完整」）。

## 六、全清（四连删除 + 零残留核实）

**带外收尾首选工具**：`blade-ai recover --task-id <task_id>`（任务记录 SQLite 持久化，注入进程退出后仍可用）对载体任务是完整三层收尾——Layer1 按本 skill 恢复段亲自执行/确认反向恢复（定时器已正确 fire 则确认即过，fire 失败则补执行），Layer2 只读核实恢复效果（带外验证器），finalize 程序化四连删除（载体资产已登记进 execution artifacts，按各自记录的 ns 删，覆盖第十节跨 ns 变体）。下方手动 kubectl 四连删除仅在 CLI/任务记录不可达时兜底。

演练结束（恢复完成或中止）后删除全套资产，顺序 Pod → RoleBinding → Role → SA（先停宿主，再摘授权链）：

```bash
kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
kubectl delete rolebinding drill-rc-<hash> -n <namespace> --ignore-not-found
kubectl delete role drill-rc-<hash> -n <namespace> --ignore-not-found
kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
```

**带外核实零残留**（核实命令不走 Agent 主通道）：

```bash
kubectl get pods,sa,role,rolebinding -n <namespace> | grep drill-rc
# 期望：无输出
```

兜底事实：即使全清命令未落地（通道被故障切断），载体 Pod 的 sleep 骨架到时自过期；未挂 binding 的 Role/SA 是惰性对象（无授权效力）。任务内清理链会在武装到期后的清理轮（verify 收尾反复重入）自动执行同一组四连删除；但任务终态时窗口仍在倒计时的载体，系统侧不再有到期触发点——其收尾依赖骨架自过期（Pod）+ 上方带外核实兜底，RBAC 三件的清理由本节四连删除命令承担。

**空壳中间态（自断授权尾步执行后的合法残留形态）**：尾步（第四节）执行后，栈中 Binding 已死，SA + Role/ClusterRole 成为零授权惰性对象，载体 Pod 骨架自过期——**授权残留为零，对象残留为空壳**。空壳是**中间态**而非终态：收尾首选 `blade-ai recover --task-id <task_id>`（finalize 四连删除幂等清壳，`--ignore-not-found` 对已删 Binding 静默跳过）；批量积累碍眼时前缀一把扫（演练集群低频卫生操作）：

```bash
kubectl delete $(kubectl get sa,role,clusterrole -A -o name | grep drill-rc-) --ignore-not-found
```

合规严格环境不接受任何惰性残留时：不启用自断尾步，直接走本节全清。

## 七、通道载荷上限警示

部分命令通道单条载荷有 **1024 字节级上限**。武装命令天然较长（token 路径 + 双 HTTP 头 + URL），长 patch payload（如整段 ConfigMap 还原内容）会超限。

**恢复 patch 三条铁律（B58/B80 家族立法——先于一切字节优化）**：
1. **同语义对称律**：恢复 patch 与注入 patch **同类型、同字段域**，值取基线快照。换 patch 类型 = 换覆盖语义——merge/SMP 的「未写字段保留现值」在恢复场景下是「未写字段保留**注入值**」，部分字段恢复 = 语义级残留（B58：merge-patch 数组整组替换 → 422 原子拒绝、恢复死锁；B80：SMP 两字段恢复 json-patch 整对象注入 → failureThreshold 残留静默存活、恢复代 RS hash 不回基线）。**禁止以「某字段注入值恰等于基线值」为由缩小恢复字段域**——同值巧合不可靠（B80 中 timeoutSeconds 恰同值掩盖了另一半漂移）。
2. **字节预算与逃生通道**：恢复命令规划期构造时预算字节（1024B 硬上限，安全线 ~900B 留引号转义余量）。超限处置按优先级：紧凑变量形态（第四节）→ 拆多个短 patch 串联（同一 `sh -c` 内顺序执行，不拆定时器）→ **base64 两步法**（恢复命令 base64 编码落盘为自包含脚本 + 定时器 fire 时 `sh` 执行；编码后仍超限时分段落盘拼接——多次 exec 各落一段再 cat 合并，同一形态的多段变体。合法前提：编码内容不含中间展开点，脚本内 `$(...)` 在执行时才展开——与第四节第一禁「printf/heredoc 落盘」的区别就在写入层有无变量展开点）。**唯一非法降级 = 缩小 patch 字段域挤字节**（删字段/少写字段/换更短类型但缩域）——字段域被铁律 1 锁定，字节压力不得侵蚀语义完整性（B80 即 1067B 超限后删两字段挤进限制的直接后果）。
3. **恢复验证锚基线律**：恢复验证判据从**基线快照**派生，不从「恢复命令写了什么」派生——注入涉及的**整个子树** jsonpath 读回逐字段比对基线（勿只挑恢复命令涉及的字段）；Deployment template 被改过的 case 加 **RS hash 回基线值**判据（RS hash 是 pod template 全字段相等性的免费校验和：template 逐字段回基线 ⇒ Deployment controller 复用基线 RS ⇒ hash 必然回基线值；hash 不回 = 有残留，不必猜哪个字段）。判据跟着恢复命令字段域走 = 判据域同源窄化、恢复写窄验证跟着窄（B80 的验证层失效机制）。

处置（按优先级）：
- **首选换第四节「紧凑变量形态」**（C/T/U 载荷内赋值，消除逐 curl 重复的 auth 头字节——多动作恢复的主要超限来源）
- **Content-Type 按还原字段层级选型**：顶层/标量字段（replicas、maxUnavailable、annotations 等）用 `application/merge-patch+json`；**还原动作触及数组元素内部字段（如 `containers` 内的 volumeMounts/args/env）时必须 `application/strategic-merge-patch+json`**（按 `name` 合并键合并，未提及字段保留现值——恢复场景下现值即注入值，字段域对称要求见铁律 1）或 json patch 精确路径——RFC 7386 merge-patch 对数组是**整组替换**语义：`containers:[{"name":...,"volumeMounts":null}]` 把容器替换成无 `image` 的对象，API 以 `image: Required value` 原子拒绝整个 PATCH（curl -s 对 422 无感，错误只落 restore.log），同载荷 DELETE 独立生效——模板未还原 + 卷对象已删，恢复死锁至人工介入
- 仍超限按铁律 2 降级（拆串联 / base64 两步法）
- 禁止为绕开上限改用 `apply -f`（守卫设计性拦截清单不可见路径）；**禁止有空展开点的落盘形态绕限**（跨层转义翻车见第四节形态纪律——合法落盘形态仅 base64 两步法与加引号定界符 heredoc 两种）

**恢复脚本落盘形态（四档定案——按明文字节数查表，免现场重推导）**：

Phase 1（planner）在计划里只写恢复脚本**明文** + 档位指令，**不手算 base64 长度、不写字节预估**——Phase 1 无 base64 生成器可绑定，`<restore-b64>` 类占位符无法填充（#44 判例：Phase 1 三轮手算 b64 字节数全数作废，Phase 2 现场重设计落盘形态又耗 126s 巨轮——占位符契约缺陷）；档位判断以**明文字节数**为唯一输入，Phase 2 落盘后按回显目检验证实际形态：

| 档 | 明文字节数 | 形态 | 判据 |
|---|---|---|---|
| ① | ≤ ~400B 且无引号嵌套冲突 | 内联 `sh -c` 直书（第四节紧凑变量形态原样套用） | 单条载荷 ≤ 900B 安全线 |
| ② | ≤ ~600B | 加引号定界符 heredoc 直书（`sh -c 'cat > /tmp/blade-restore-<名>.sh <<"EOF"\n<明文>\nEOF'`——**外层单引号**包裹、内层 `<<"EOF"` 字面保留；定界符加引号即字面化，无展开点。#44/#45 实证形态；外层双引号+反斜杠转义形态经 2026-09-16 方案 A 分词器修复后亦可安全使用（真实集群四形态实测），但单引号形态仍是首推——更简、无转义心智负担，见下引号保真律） | 落盘后 `cat` 回显目检 |
| ③ | 更长或引号复杂 | 载体内 base64 编码（#43-R 实证形态：明文经引号安全形态传入载体内 → 载体内 `base64 -w0` 编码落盘 → 解码回显目检；编码产物不出载体，不受 exec 通道 1024B cap 约束） | 载体内编码 + 回显目检 |
| ④ | ②仍超限（明文 > ~700B） | 分块并行写入（#43-R2 第五跑实证：明文分段独立文件并行落盘 → 载体内 `cat` 拼接 → 回显目检；mid-token-split 边界对空白裁剪免疫） | 拼接后回显目检 |

各档目检门不变（第四节「先校验后武装」）：回显全文中键路径/字段域/Content-Type/`$` 变量字面量逐项核对才武装；档位边界处（如 550B）取更保守档不违法。

**exec 载荷引号保真律（#45 实证立法；2026-09-16 二轮归因修正：根因在本地分词器，非 wiz 通道；同日方案 A 修复落地）**：引号损毁的第一现场在**本地 kubectl 分词器 `_split_args`**（tools/kubectl.py：plain quoted region 逐字符扫描遇同类引号即闭合，**不识别双引号区内的反斜杠转义**）——外层双引号+转义形态（`\"`/`\$`）的 exec payload 被静默碎片化成多 token（`\"` 剥引号留反斜杠、token 在 `\Authorization:` 处截断、`Bearer` 沦为容器 shell `$0`），碎片经 wiz 通道（shlex.quote 逐词重组）**忠实**传输、容器 shell 再把残骸解析出错（残留 `\`+换行成行续接吞行——#45 corrupt file 209B≠正确 206B 三特征经四形态矩阵探针（.codex_work/probe_splitargs_45.py）逐字复现）；「外层单引号保真」的真因 = 分词器 verbatim-until-`'` 恰是 shell 单引号精确语义（无转义概念、无假闭合点），**wiz 通道对两种形态均无损**（同文件同型归因失误判例 d9117d36：从远端回显反推平台行为的坑两次——归因纪律：从 guard 派发最早的本地证据起比对）。**2026-09-16 方案 A 修复（用户裁决保留，实测四层验证）**：`_split_args` 双引号区已按 POSIX 解转义（`\"` `\\` `\$` `\`` → 字面字符、`\<newline>` 行续接、其他 `\x` 字面保留）——转义形态经真实集群 live-path 四形态实测 INTACT（修复前同 payload 11 token 碎片化 + 容器静默零输出；修复后 7 token 与单引号形态逐字节等价）；真实 /bin/sh oracle 9/9 一致；8351 项回归零破坏。**当前防御纪律**（修复后从硬律降为形态建议）：①落盘命令外层**首推单引号**（更简、无转义心智负担；转义形态已可用但两形态 token 等价，无收益）；②**单引号外层形态 payload 零单引号字符——此洞方案 A 不修**：外层 `'` 在 payload 首个 `'` 处假闭合，其后 JSON 双引号被**静默剥除**（`{"op":"replace"}` → `{op:replace}`，产物「看似合法裸文本」，比可见残骸更难目检）——payload 需要单引号时（如 awk 脚本）改走③或转义形态；③payload 含双引号/引号嵌套冲突时用 quoted heredoc 直书或 `-d @file` 分离（脚本与 payload 分两个文件各自 heredoc 落盘，curl `-d @/tmp/patch.json` 引用——payload 字节原样落盘，字段域零缩窄，#45 实证形态）；④载体内 base64 编码（档③）天然免疫。

## 八、降级路径

本节治理**本标准件自身**的建栈失败。CR 通道（第十一节）的降级目标即本标准件一~十节全流程——那侧的降级判定族与 replan 纪律见第十一节「降级链」，不在本节展开（防双源漂移）。

载体自建失败（镜像不可拉取 / 验权 403 / 通道不可达）时：
1. **不得**反复重试绕行（换名重建、换命名空间、改清单路径均属绕行）
2. 如实报告失败原因，**任务如实失败收尾——恢复定时器未武装即不得注入**（「武装与注入紧邻」时序立法的硬序面：无定时器注入 = 故障窗口期内无任何自动恢复保障）。**不存在「Agent 在线保活」降级形态**——Agent 会话不承担恢复执行，在线段职责恒定（武装 → 注入 → 生效确认 → 窗口内采样 → 提交结论即收尾，见第九节「任务生命周期契约」）
3. 故障已落地后才发现载体不可用 / 定时器丢失的，恢复移交带外段：`blade-ai recover --task-id`（第六节三层：补恢复/确认 + 恢复效果核实 + 程序化四连删除）——结果报告如实记录降级原因与移交状态

恢复通道被故障本身切断的场景（如集群 DNS 故障使 `kubernetes.default.svc` 不可解析）：**先备好带外恢复手段才可注入**（直连 kubeconfig 走 API server IP，或控制台手动）——此为拓扑死锁，载体定时器与带外恢复双双不可达，忙等无意义。

## 九、规划期决策速查（通用判别法）

以下全部是**通用方法论**——不绑定任何特定集群/厂商/镜像，一切环境值以当次探测结果为唯一权威；本节只给判别路径，避免逐次重新推导（唯一例外：末尾「本集群实证档案」为环境特定经验记忆——tag 锁定、只证工具链不证可用性，可用性仍以当次探测为准）：

**任务生命周期契约（标准件路径——规划期一次定案，免逐次重新推导；CR 通道 case 的同构变体见第十一节，判据以彼节为准）**：

| 段 | 执行者 | 职责边界 |
|---|---|---|
| Agent 在线段 | 本任务 | 武装 → 注入 → 生效确认 → 窗口内采样 → 提交结论即收尾退出 |
| 载体自治段 | 载体定时器（恢复单点） | 窗口到期自动恢复——Agent 是否存活不影响触发 |
| 带外段 | 人工/下一任务 | 首选 `blade-ai recover --task-id`（三层：补恢复/确认 + 恢复效果核实 + 程序化四连删除，第六节）；兜底手动核实 + 四连删除（第六节/第十节变体） |

四条免推导结论：
1. **verify 不等窗口到期**：恢复由载体定时器单点负责，不是本任务的验证对象——效果证据在窗口存活期采集，计划里不设 post-window 恢复等待轮
2. **全清触发是条件式的**：窗口在任务存活期内到期 → 任务内清理链自动四连删除；任务先于窗口到期收尾（常态，见结论 1）→ 系统侧无到期触发点，全清依赖带外收尾（首选 `blade-ai recover --task-id`，第六节）+ Pod 骨架自过期 + RBAC 惰性对象兜底（第六节兜底事实）
3. **Agent 的收尾义务 = 如实报告**：结果报告 cleanup 清单列出载体资产即可；不在线等恢复、不等骨架自过期
4. **Agent 在线段不设计 early recovery（#52 实测立法，2026-09-18）**：载体武装 timer 后即处于 `recovery_armed` 锁定态，fire 前 target_guard 拒绝对该载体的第二次 mutation（含提前执行恢复动作），新建第二载体绕锁违反安全设计意图，勿走此路。daemon 挂起类故障按批准 duration **全窗设计**：提前恢复会使 verify 失去故障态采样（T 态进程、事件累积计数都在窗口存活期取得），且窗口尾部与 verify 报告生成本就天然重叠（#52 实测：fire 落在终报告生成中途，fire 后 35s 收尾，全窗零浪费）——计划里出现「T0+N 提前恢复」步骤即为立法违背，规划期删除

（降级路径见第八节——载体建不起来即任务失败收尾，不改变本契约三段边界：Agent 段从不承担恢复执行。）

**端到端建栈 SOP 填空模板（执行序一次拼装，免逐节跳转重排——planner 照此序填空成文，#51-R3 实测定案）**：标准件全生命周期的执行顺序骨架如下，每步右侧引用细节立法所在节（判据正文以各节为准，本模板不复制判据防双源漂移）——计划按此序成文，勿在规划期重新推导步骤间的次序与拼装：

| # | 步骤 | 形态 | 立法出处 |
|---|---|---|---|
| 1 | 定名 + 冲突预检 | `drill-rc-<语义短后缀>`；四件套精确同名一条 get（五件套变体另加集群级一条） | 本节「命名与冲突预检」 |
| 2 | 遗留资产拆除（仅预检有输出时） | 按第六节全清序拆旧建新 | 本节「遗留同名资产处置预案」 |
| 3 | 执行凭证预检 | can-i 一次发全：建栈集 ∪ 注入集 ∪ 验权集（启用自删尾步加 patch roles/clusterroles） | 本节「执行凭证预检」 |
| 4 | taint 形态探测 → tolerations 定案 | nodes jsonpath 枚举 taints；资源池类容忍 / 维护窗口类不容忍 | 第一节⚠️ + 本节「载体 tolerations 取舍」 |
| 5 | SA | `kubectl create sa`（先于载体 Pod——token 挂接前提） | 第一节步骤 1 |
| 6 | Role（单族直建 / 跨族两步法步骤 1） | `kubectl create role --verb --resource`（verbs 以恢复载荷实际写动词为准，第二节映射表） | 第一节 + 第二节形态无关总则 |
| 7 | RoleBinding | `kubectl create rolebinding --role --serviceaccount` | 第一节步骤 3 |
| 8 | 载体 Pod | `kubectl run` + overrides（SA 挂接 + tolerations）+ `--command -- sleep <骨架>` | 第一节步骤 4 |
| 9 | Role 两步法步骤 2+3（跨族非均匀动词集时） | 载体注册后 json-patch 逐族追加 `/rules/-` + readback 逐 rule EXACT | 第一节「Role 跨资源族构造立法」 |
| 10 | SA 真实 token 验权（GET 硬门） | exec curl GET 目标资源 → 200 继续 / 403 中止 | 第三节 |
| 11 | SSAR 写动词对账（逐写动词） | exec curl POST selfsubjectaccessreviews + 回执自证三态判定 | 第三节 |
| 12 | 武装（推迟至第一个故障生效动作紧邻前） | exec `sh -c '( sleep N; <恢复动作> ) >/tmp/restore.log 2>&1 & echo armed'` | 第四节（形态/时序/字节预算三查） |

顺序硬约束三条（其余顺序即表序）：**SA(5) 先于载体 Pod(8)**；**Role 追加(9) 须在载体注册(8) 之后**（patch 全局禁令在载体注册前生效）；**武装(12) 紧邻第一个故障生效动作之前**（勿按命令模板在计划文本中的出现位置机械摆放，见第四节「多步注入序列的武装点」）。可选变体注记：启用自断授权尾步时，自删规则的两步追加（json-patch `/rules/-` + resourceNames 锁名）插在步骤 6/9 同期（主授权建栈之后独立追加，第二节「自删规则两步建栈法」），尾步 DELETE 的 `curl -sf &&` 挂接在步骤 12 载荷末（第四节五条纪律）；五件套/六对象栈的集群级链同步。收尾侧（verify/全清/带外）不进本表——见本节顶部任务生命周期契约三段表。

**镜像选型（覆盖优先——无法钉节点）**：overrides 白名单不含 nodeSelector/nodeName，载体可能调度到**任意可调度节点**，镜像必须「任意节点都拿得到」。**主路径是零配置自动发现**：任务启动探测（preplan_probe）枚举健康 DaemonSet（desired==ready>0）的镜像——全节点缓存、无需网络拉取——自动并入载体允许集并在探测消息中给出候选清单，规划期直接选用（按第 3 条确认工具链即可）。探测消息不含候选或需自行核实时，按以下判别法：
1. 权威缓存清单：`kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{": "}{.status.images[*].names}{"\n"}{end}'`（kubelet 上报的每节点镜像缓存）；**DaemonSet 全节点覆盖（每个节点都有 Running 副本）的镜像 = 全节点缓存**，是天然候选
2. 或选集群可达 registry 的镜像——可达性判据：有运行中 Pod 正在使用该 registry 前缀（未缓存也可拉取）
3. 工具链预检属尽力而为：批准靶标自身若正在使用候选镜像，可对它 exec `command -v sh curl sleep`（域外 Pod 的 exec 会被身份漂移守卫拒绝）；预检不了也无妨——第三节验权 exec 本身就是硬门（curl 缺失 → 无 200 → 中止，故障尚未注入）
4. 允许集语义（硬门）：配置默认集（busybox/curl；`BLADE_AI_RECOVERY_CARRIER_ALLOWED_IMAGES` 可覆盖——人工兜底入口，仅探测失败等少数场景需要）∪ 自动发现集（每任务重探、进程内合并、不持久化）；任务运行中不可改。不在允许集的镜像进不了标准件形态——SHAPE 拒绝消息会透传「镜像 X 不在允许集 + 当次发现集 + 人工入口」，失败可诊断
5. **既往实证可引用（免工具链探测）**：镜像内容不可变——精确 tag 匹配的既往实证（载体定时器真实 fire 过，非仅 command -v 探测）是工具链的充分证据，规划期直接选用无需 debug 探测（探测需额外约 150s 的规划轮，且有 debug Pod 60s 存活期竞态）。注意实证只证工具链：当次探测候选清单仍是可用性的唯一权威——实证 tag 不在候选清单说明 DS 已滚动，实证失效需重探。**候选清单 ∩ 实证档案非空时取第一个命中项即定案**——多候选同命中时不做优劣对比（DS 健康度已由探测保证），档案命中但不在候选清单的镜像按失效论

**载体 tolerations 取舍（定案免推导）**：探得集群 taint 形态后，**资源池隔离类 NoSchedule taint（如 sigma.ali/*）加对应 tolerations**（带此 taint 的节点是正常工作节点，DS 覆盖即可证健康）；**运维窗口类 taint（如 ops-maintenance）不加容忍**——被标记维护的节点不是好载体宿主，容忍它反而扩大落点风险。无 taint 可调度节点充裕（>10）时零容忍也可（默认调度面足够）。**未列举类别（如 dedicated 专用硬件类）按语义就近归类，归不了类取零容忍**——漏容忍的失败模式是载体 Pending → 第八节降级中止（fail-closed），错加容忍的失败模式是载体落上不该落的节点（fail-open），fail-closed 恒优于 fail-open；取舍权衡勿再展开三轮论证（#44 判例：全容忍/零容忍/子集容忍三轮自我辩论 ~50s，定案后免）。

**载体建栈的 blast_radius_scope 定性（定案免推导，按栈形态分流）**：以**新增授权对象的写入集**为判据——①**四件套**（Pod/SA/Role/RoleBinding 全在靶 ns、Role 授予 namespaced get/patch）→ **namespace-wide**（故障本体仅变更靶资源，但 namespaced 新增 RBAC grant 保守声明；additive scaffolding 不算 mutation 但授权面属实）；②**五件套变体**（含 ClusterRole/ClusterRoleBinding 集群级两件）与**第十节跨 ns 变体**（role/rolebinding 落靶 ns 之外的恢复 ns）→ 按 SKILL.md 安全红线「爆炸半径 scope 枚举映射」规则③声明 **cluster-wide**（跨命名空间持久资源写入；#15 五件套实测定案同此）。此定性一次定案，勿在 planning 与 finish_planning 两轮反复辩论（#44 判例：两轮辩论两次结论不同 target-only→namespace-wide，~110s，定案后免）。

**task_id 命名（定案免推导）**：save_fault_plan 的 task_id 用 **ASCII 连字符短名**，推荐「<域目录小写>-<case 短名>」形态（如 pod-terminating-finalizers）——task_id 会成为任务文件名与台账检索键，非 ASCII/中文有文件名兼容风险；按此式一次定案，勿起多候选轮转纠结（#48 实测：Iter 8 内三个候选方案轮转 ~100s，无立法指引时 LLM 自行权衡描述性/安全性/简洁性）。载体名沿用 drill-rc-<短名> 独立命名，两者不需一致性仲裁。

**本集群实证档案（环境特定记忆，随台账更新——新实证在 CASE_实测记录.md 归档后追加到此）**：

| 镜像（精确 tag） | 工具链 | 依据 |
|---|---|---|
| registry-cn-shanghai-cloudspe-vpc.ack.aliyuncs.com/acs/terway:v1.12.1-f2d6cd5 | sh + curl + sleep 齐备 | 载体定时器真实 fire，四 REST 全部执行 |
| registry-cn-shanghai-cloudspe-vpc.ack.aliyuncs.com/acs/ack-node-problem-detector:v0.8.17-952071f-aliyun | sh + curl + sleep 齐备 | restore.log 全链取证（四 patch 原始回显） |

（两镜像 pkill/ps 可用性未确认——仅影响 re-arm 异常路径，缺则按第八节 abort 转人工恢复，不影响主路径。）

**duration 双轨**：`finish_planning` 的 duration_seconds 声明**用户意图窗口**；系统 300s 安全地板只抬升任务侧外层（自动恢复兜底/审计账本），**载体定时器按用户意图窗口武装**——勿拿抬升后的地板值武装定时器（那会擅自延长故障窗口）；定时器先到先恢复，外层兜底幂等无冲突

**恢复 REST 细节**：
- json-patch 幂等性：`remove` 二次触发时报 path 不存在（无害噪音）；`replace` 幂等但要求路径已存在——按用例指引择一即可，无需重推
- apiserver **先鉴权后查存在性**：GET 返回 404 = 有权限但资源不存在，403 = 无权限；GET 只证明 get 这一个 verb，其余 verb 以 Role 的显式定义为准，不必逐 verb 探测
- 武装命令（双 HTTP 头 + URL + patch 体）内联形式在数百字节量级；更长载荷按第七节拆分或先写脚本文件再定时执行

**exec 通道换行**：多行脚本经命令参数层传输时换行不保证存活（heredoc 会被压平为单行）——载体内写脚本文件统一用单行形式：`printf '%s\n%s\n' '行1' '行2' > /tmp/restore.sh`（或 base64 解码），写完先 `sh -n` 校验再武装

**命名与冲突预检**：`drill-rc-<语义短后缀>`（场景缩写或窗口秒数），一次定名，四/六对象同名。**定名后立即做精确同名冲突预检**（一轮完成，勿拖到后续轮次）：四件套 `kubectl get sa,role,rolebinding,pod drill-rc-<名> -n <ns> -o name` 一条；五件套变体另加 `kubectl get clusterrole,clusterrolebinding drill-rc-<名> -o name` 一条——NotFound/Error 输出即无冲突（对象不存在正是期望态）。**勿用全列表查冲突**（`get pod,sa,role,rolebinding -n <ns> -o name` 不带名字）：输出超限截断后，截断点之后的 kind 根本没显示，冲突结论不成立还得多花一整轮复查

**遗留同名资产处置预案（预检撞到即拆旧建新——定案免推导）**：冲突预检**有输出**（同名遗留存在：上一任务的载体 Pod 已 Succeeded/Completed 自过期 + RBAC 三件常驻——「常驻无害」指惰性对象无风险，不等于可复用；常驻必然使下次同名演练再撞冲突）时，处置定案 = **按第六节全清序先拆遗留（Pod → RoleBinding → Role → SA，五件套含集群级两件），再按第一节标准流程新建同名栈**，不得复用遗留资产。这是 execute 计划的 setup 步骤义务（载体建栈前执行——动作措辞显式声明执行者与阶段），理由两条：
- Completed 态 Pod 的 exec 通道不可用（第三节验权与第四节武装载荷都走 exec）——复用等于跳过验权盲武装
- 任务侧归属完整性：任务收尾的自动清理链按本任务 execution artifacts 名册执行——继承的遗留资产不在名册，本任务收尾清不掉，下次同 hash 演练再撞同一冲突；新建资产进名册，收尾自动清理完整覆盖（#43-R 实证：拆旧建新后零残留）

拆除回执预期：delete 正常 exit 0；预检与拆除之间被并发清理（NotFound）时按「确认不存在」预检的 POSIX 通过语义解读，继续新建。

**执行凭证预检（一轮发全）**：规划期对当前凭证一次并行 `kubectl auth can-i` 发全**本任务全部写动词**——建栈集（`create serviceaccounts/roles/rolebindings/pods`，五件套变体加 `create clusterroles/clusterrolebindings`；启用自断授权尾步时**加两步建栈第二步动词**——namespaced 栈 `patch roles`、cluster 变体与六对象栈的 cluster 链 `patch clusterroles`：create 有而 patch 无则自删规则追加不上、timer 自删 DELETE 必 403）∪ 注入动作集（按用例演练步骤：`patch deployments`、`patch/label nodes`、`delete pods` 等）∪ 验权只读集（`get` 目标资源）。can-i 亚秒级且可并行，**一次发全、勿分批**——漏发任何一条都会导致后续轮次补探。

**verify 期卫生项边界**：注入机制卫生（如滚动更新参数 maxUnavailable 的还原）**不是故障效果判据**——效果检查表只对效果主张取证；卫生项有执行期证据即在 warnings 注记，不为它追加采样

**滚动更新容量语义（定案免推导）**：Deployment 滚动参数的百分比取整方向相反——`maxSurge` 向上取整（ceil）、`maxUnavailable` 向下取整（floor）。1 副本默认 25%/25% ⇒ surge 允许 ceil(0.25)=1 个新 Pod、unavailable 允许 floor(0.25)=**0** 个旧 Pod 先下线——滚动退化为纯 surge，必须等新 Pod Ready 才能替换旧 Pod。凡注入使新 Pod 永不 Ready（镜像拉取失败类）或短暂 Ready 后即被杀（探针误杀类 CrashLoop，杀与滚动判定是 race）的 case，默认参数即死锁或赌 race；防御定案 = 注入前临时 patch `maxUnavailable: "100%"`（floor(1)=1，旧 Pod 可先下线，滚动确定性完成），注入生效后立即还原原值（卫生项不是效果判据，见上方「verify 期卫生项边界」）。勿在规划期重推取整方向与死锁链条——此处即定案。

## 十、跨命名空间变体（恢复对象在靶 ns 之外）

恢复动作的目标对象不在靶点命名空间时（如还原 kube-system 的 CoreDNS 配置），四件套按**身份/授权分居**落位——k8s RBAC 允许 RoleBinding 的 subject 引用外 ns 的 SA（roleRef 则只能同 ns），这正是变体的机制基础：

| 对象 | 落位 | 原因 |
|---|---|---|
| SA + 载体 Pod | 靶 ns（不变） | 载体身份锚定在靶 ns；守卫的载体形态识别与同 ns secondary 覆盖都在靶 ns |
| Role + RoleBinding | **恢复 ns**（如 kube-system） | 授权按资源所在 ns 生效——grant 必须落在被写对象身边才有效力 |

构造命令差异仅两处（role/rolebinding 的 `-n` 改恢复 ns；rolebinding 的 subject 显式带靶 ns）。完整构造序列成套如下（免跳回第一节拼装；五条件、flag 白名单、镜像选型约束同第一节与第九节）：

```bash
# 0) tolerations 形态探测（无 taint 时 overrides 只留 serviceAccountName 一键）
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{": "}{.spec.taints}{"\n"}{end}'
# 1) SA（靶 ns——必须先于载体 Pod 创建，见第一节）
kubectl create sa drill-rc-<hash> -n <靶ns>
# 2) Role（恢复 ns；verb×resource 按第二节推导表取本用例恢复动作所需）
kubectl create role drill-rc-<hash> -n <恢复ns> \
  --verb=get,patch --resource=<按第二节推导表>
# 3) RoleBinding（恢复 ns；subject 显式带靶 ns——k8s RBAC 允许跨 ns 引用 SA）
kubectl create rolebinding drill-rc-<hash> -n <恢复ns> \
  --role=drill-rc-<hash> --serviceaccount=<靶ns>:drill-rc-<hash>
# 4) 载体 Pod（靶 ns；overrides = SA 挂接 + tolerations，结构白名单见第一节；
#    受限网络按第九节判别法选定全节点可达镜像后替换 --image）
kubectl run drill-rc-<hash> -n <靶ns> --image=busybox:1.36 \
  --restart=Never \
  --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>","tolerations":[{"key":"<taint-key>","operator":"Equal","value":"<taint-value>","effect":"NoSchedule"}]}}' \
  --command -- sleep <骨架时长>
```

**立法前置（硬门）**：恢复 ns 的 role/rolebinding 写超出了靶 ns secondary 覆盖——用例 frontmatter 的 `mechanism_writes` 必须含 `role`/`rolebinding` @ 恢复 ns + `name_prefix: "drill-rc-"` 两条，否则守卫按 scope drift 拒绝（fail-closed）。SA/载体 Pod 仍在靶 ns，无需立法。

**执行凭证前提**：当前凭证须在恢复 ns 有 `create roles/rolebindings` 权限（`kubectl auth can-i create roles -n <恢复ns>` 探测；启用自断授权尾步时另查 `can-i patch roles -n <恢复ns>`——两步建栈第二步）；无权限时不可强建——走第八节降级路径。

**全清变体**：四连删除顺序不变，role/rolebinding 改在恢复 ns 删（清理侧按成员各自记录的 ns 删除，任务侧已支持）：

```bash
kubectl delete pod drill-rc-<hash> -n <靶ns> --ignore-not-found
kubectl delete rolebinding drill-rc-<hash> -n <恢复ns> --ignore-not-found
kubectl delete role drill-rc-<hash> -n <恢复ns> --ignore-not-found
kubectl delete sa drill-rc-<hash> -n <靶ns> --ignore-not-found
```

零残留核实相应分两个 ns 各查一遍。恢复 ns 中的授权对象同样是惰性对象（无 binding 的 Role 无授权效力），兜底语义与第六节一致。

**通用性**：本变体不绑定任何特定命名空间——「恢复 ns」泛指恢复对象所在的任意命名空间；一切环境值（恢复 ns 名、凭证权限）以当次探测为唯一权威。

## 十一、CR 通道（FaultDrill）——apiserver-write 域的第一路由

**定位**：恢复动作住址 = apiserver 写的 case，第一路由是 FaultDrill CR 通道——**apply 一条 CR 替代本标准件一~十节的 SA 四件套建栈 + 载体 Pod + exec 武装链全序列**（CRD 由通道程序化惰性安装，LLM 工具面只放行 CR 实例）；本标准件是该通道不可装时的降级目标，两形态互斥不叠加。

**判据（恢复动作住址三分类——case 语义，不可从 fault_spec 机械推导：同是 secret 写，恢复可能是逆 patch 也可能是删对象）**：
1. 对称撤回（blade destroy 按 UID 撤回 / `--timeout` 超时自恢复）→ 零工坊域，不走本节不走本标准件
2. 宿主本地命令（iptables/systemd/chmod/tc/kill 等节点本地操作）→ 宿主载体域，不走本节不走本标准件
3. apiserver 写（逆 patch 还原 / 还原副本数等）→ **本节（CR 通道优先）**；CRD 不可装降级本标准件一~十节全流程

三源路由（不靠 LLM 自觉）：case front matter `recovery_channel: apiserver-write` 声明（元数据第一源，已在 apiserver-write 域 case 盖章）→ planning 读声明路由（workflow 引导：声明 case 的 Execution Steps 载 CR 而非 SOP 写序列，明禁叠加 SOP）→ write-set 审批程序化校验（blade 载波词表命中 = 误路由在审批门拒绝并提示修正路由）。

**一条 CR 的结构**（模板以各 case 文档头部「CR 通道模板」段为唯一权威，本节不复制防双源漂移）：`kind: FaultDrill`（组名/实例名前缀均可配，实例名零 drill/chaos/blade 词根）；spec 五要素——`targetRef`（**单一对象** patch 域：注入/恢复跨多对象的 case 不入本通道，留在 SOP 形态）、`patches`（注入域 json-patch）、`restorePatches`（恢复域：调和器 TTL 到点执行，Agent 死亡后 recover 重放同源）、`durationSeconds`（TTL 从 Injected 相位起算，取正文演练窗口同值）、`invalidSecret`（凭证族专用：源 Secret 引用 + 变换配方，**零凭证材料内联**——CR 不在 Secret encryption-at-rest 覆盖内，内联 = 凭证明文落 etcd）。`metadata.namespace` 必须显式写入（P10 两源纪律：句柄水合链 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位，recover get NotFound 误判实验丢失）。

**恢复语义（恢复单一来源立法）**：CR 通道任务**不武装本标准件 timer**——timer 与调和器并存会双恢复赛跑。恢复由会话侧调和器承载（5s 周期现读 CR：patches/restorePatches/invalidSecret/durationSeconds 全部来自 CR，进程零硬编码，CR 是唯一事实源；TTL 判据读 `status.injectedAt` 集群状态非进程内存）；Agent 死亡/会话结束后 `blade-ai recover --task-id` 读 CR 重放同源，四态幂等收敛（Pending → 删 CR 零注入动作 / Injected → 调和恢复收敛 / Recovered → 零写 / NotFound → 跨 ns 核实后零动作告警——归因高容忍语义兼容 CRD 未装期 apply 失败残留）。注入动作连续失败达重试上限进 Failed 终态（restoreLog 记因不无限重试，可被 recover 处置）。

**会话侧调和器的局限（如实披露）**：调和器是 Agent 进程内 asyncio 后台任务——会话死亡且无人调 recover 时，恢复延迟至下一次干预；但恢复意图永存 CR（patches/restorePatches/durationSeconds 在 apiserver），对照本标准件 timer 死亡 = 配方与定时器**双双永久丢失**仍是严格改进。集群内常驻控制器（operator Deployment，恢复不依赖任何进程存活）为 Phase 3 演进方向，届时本段局限整体作废。

**降级链（双向互指）**：
- **本通道 → 本标准件**：CRD 不可装判定族（探测拒绝 ∪ 安装拒绝 ∪ Established 等待超时 ∪ 存量 CRD schema 不兼容 ∪ apply 期 Forbidden）命中任一 → planning 直接计划 SOP 形态（一~十节全流程，不产生 CR 尝试轮次，降级事件记入任务观测消息）；execute 期意外失效 replan 一次同路径。apply 失败二分：环境层 Forbidden → 降级；配方层 schema 拒收 → **中止修配方不降级**（集群零副作用，不裸注入）。另注意「已存在」≠「可用」：存量旧版 CRD 缺 preserve-unknown 声明时 CR 会被 pruning 剥离，视同不可装。
- **本标准件 → 本通道**：仅 apiserver-write 域 case（`recovery_channel: apiserver-write` 声明）走本通道；对称撤回/宿主域 case 不迁移不走本通道——write-set 审批门程序化拦截误路由（blade 词表命中拒）。

**任务生命周期契约（CR 通道变体——与第九节三段契约同构，判据以本节为准）**：

| 段 | 执行者 | 职责边界 |
|---|---|---|
| Agent 在线段 | 本任务 | apply CR → 程序化 readback 确认（patches/restorePatches 完整落地硬门，失败硬中止不进调和）→ 生效确认 → 窗口内采样 → 提交结论即收尾退出 |
| 调和器自治段 | 会话侧调和器（恢复单点） | TTL 到点自动恢复——会话存活期内 Agent 是否被占用不影响触发 |
| 带外段 | 人工/下一任务 | 首选 `blade-ai recover --task-id`（读 CR 四态收敛重放恢复 + 工件清理）——恢复意图永存 CR，迟到不丢失 |

「武装与注入紧邻」时序在本通道的对应物：**CR 落地即携带 restorePatches + TTL（恢复意图随注入原子落集群）**——不存在「已注入无恢复配方」的中间态，第九节「恢复定时器未武装即不得注入」的硬序面由此结构性满足；readback 硬门则对应本标准件第三节的验权硬门位置（apply 后紧邻，未过不进后续）。
