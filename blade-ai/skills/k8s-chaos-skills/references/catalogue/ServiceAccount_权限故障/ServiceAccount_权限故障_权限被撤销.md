---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（POST 重建被删除的演练 RoleBinding），但注入的
# 关键动作是 DELETE 既有演练对象（RoleBinding）而非 patch 靶标字段——装配器 M1 注入域
# 只承载「patch 既有靶标」，delete/create 类对象级增删不经装配器；恢复走标准件载体
# timer（curl POST 重建 RoleBinding，幂等：对象已存在时返回 409）+ Agent 主动兜底双通道，
# 无装配器主路径（如实声明，与 LimitRange误配 的「create 类注入 → 装配器不适用 → SOP
# 载体 timer」形态同构，方向相反）。
recovery_channel: apiserver-write
# 受害者 scope 软 hint（route A，非强制）：planner 读 case 时参照它把 finish_planning 的
# fault_scope 填成 deployment——受害者是消费方 Deployment（见下方 write-set 立法）。全代码库
# 无确定性消费者校验/覆盖此字段，纯 LLM 面向软 hint，仅降低同 case 在 scope 声明间摇摆的概率。
fault_scope: deployment
# 机制写入集立法（write-set approval contract）：受害者身份锚定在**消费方 Deployment**
# （drill-victim-<hash>，其 Pod 以演练 SA 运行、权限被撤销后调 apiserver 得 403 的可观测承载体）。
# 为什么不锚 SA 自身——serviceaccount / role / rolebinding 均**未**注册进 fault_registry
# 的 k8s_chaosblade family scopes（该 family 仅登记 pod/node/container/deployment/
# statefulset/daemonset/service/ingress/configmap/secret；cluster_scoped 另含
# clusterrole/clusterrolebinding/storageclass），故 SA/RoleBinding 无法作为 approved.scope
# 被 planner 声明、也无法被 resolve_by_scope 路由。消费方 Deployment 是注册域内（scope=
# deployment）、且承载必现现象（其 Pod 调 API 得 403）与恢复标准（复权后恢复 200）的最近
# 受害者代理：approved=deployment、名字匹配放行——且它经 drill-target manifest 契约
# （`kubectl apply -f -` 单 Deployment 文档、metadata.name=drill-victim-<hash>、恰一容器、
# image ∈ 载体允许集、command 不受限）自建，screener 以普通 drift net 锚定该名为 approved
# 身份、登记为 occupant_deployment 载体资产供清理链删除。故障机制 DELETE 的演练 RoleBinding
# 与建/删的演练 SA/Role 落在 deployment 受害靶覆盖域之外，逐条立法如下——namespace_from:
# victim 由确定性代码在 freeze 时物化为受害者 ns（演练 RBAC 三对象必须与消费方同 ns 才能授权
# 其在本 ns 的 API 访问，可移植 case 无法硬编码该 ns，DERIVED never re-declared，与
# LimitRange误配 同构）；name_prefix 锁死 drill- 前缀，禁触及既有生产 SA/Role/RoleBinding。
# 恢复载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash>）走受害靶同 ns 二级域
# （rbac_family 名字校验跳过），不在本清单重复立法。
mechanism_writes:
  - scope: serviceaccount
    namespace_from: victim
    name_prefix: drill-sa-
  - scope: role
    namespace_from: victim
    name_prefix: drill-role-
  - scope: rolebinding
    namespace_from: victim
    name_prefix: drill-rb-
---

**用例名称** 权限被撤销 导致 ServiceAccount_权限故障

## 故障定位
配置型故障——一个把演练 ServiceAccount 绑定到其 Role 的 RoleBinding 被删除后，RBAC
授权器在**下一次** API 请求即时查不到任何授予该 SA 相应 verb 的绑定，该 SA（及以其身份
运行的消费方 Pod）对目标资源的访问立即被拒（HTTP 403 Forbidden），**RoleBinding 缺失即
故障存活**，贯穿整个故障窗口；窗口到期载体 timer POST 重建同名 RoleBinding 即自动恢复。

受害者身份 vs 机制攻击面 vs 爆炸半径三域分离：受害者是**以该 SA 运行的消费方 Deployment**
（drill-victim-<hash>，其 Pod 的必现现象 = 调 apiserver 得 403、恢复标准 = 复权后恢复 200）；
机制物理上删除的是 RoleBinding（RBAC 对象），因 serviceaccount/role/rolebinding 未注册进
family scopes，受害者只能锚在注册域内（scope=deployment）的消费方 Deployment（见 frontmatter
write-set 立法）；爆炸半径限于**使用该演练 SA 的消费方**——不波及其他 SA、其他 workload、
其他命名空间。

手段结论：手段1（ChaosBlade）**不适用**——ChaosBlade 无 RBAC/权限类故障靶点（其 K8s
执行器面向 pod/node/container 的 cpu/mem/network/disk/process，不含授权面对象），本用例为
kubectl-native 专属注入（`kubectl delete rolebinding`）。注入=删既有演练对象、恢复=POST
重建，均非「patch 既有靶标字段」，故装配器 M1 不适用，恢复走标准件载体 timer SOP。

判据可达性：**机制判据**（白盒，只读，故障本体）= `kubectl get rolebinding drill-rb-<hash>`
NotFound + `kubectl auth can-i list pods --as=system:serviceaccount:<ns>:drill-sa-<hash>`
由 yes 翻 no（SubjectAccessReview 实时查授权面，是权限撤销的权威白盒证）；**行为判据**
（业务影响）= 消费方 Pod 日志内 HTTP 状态码由 200 翻 403。二者在故障存活期内均可采集。

环境适配：本 case 需**自建合成演练靶**（演练 SA + Role + RoleBinding 用 `kubectl create` 构造，
消费方 Deployment 经 drill-target manifest 契约 `kubectl apply -f -` 自建，全部 drill- 前缀、
建在被批准靶命名空间内）——直接删既有生产 RoleBinding 不可移植（换集群即失效）且风险高
（误伤真实 workload 授权面），故一律合成。合成靶使故障自证链闭合、恢复可完整重建
（RoleBinding 规格确定：roleRef 指演练 Role、subjects 指演练 SA）。

自证链与 duration 契约：权限撤销**即时生效**——RBAC authorizer 每个请求实时评估绑定，删
RoleBinding 后 SA 的下一个 API 调用立即 403，无传播/收敛窗（不同于 volume/env 类需 kubelet
同步或 Pod 重启）。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认；下限取
系统最小窗口地板（600s）并宁宽勿窄——窗口须覆盖 planning+execute 全程，否则 verify 段首
观测会落在载体 fire 之后、primary 证据（403 + can-i no）无法在窗内自采。

## 恢复动作配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：POST 重建被删除的演练 RoleBinding。**注入含 DELETE 既有对象、恢复含 POST 重建，装配器不适用**（M1 注入域只承载 patch 既有靶标字段）——恢复走标准件载体 timer 自治 + Agent 主动兜底双通道，配方如下：

```yaml
targetRef:                                # 恢复重建的对象 = 演练 RoleBinding（注入期被 DELETE）；受害靶 = 消费方 Deployment（其 Pod 为 403 承载体，不被写）
  kind: RoleBinding
  name: drill-rb-<hash>
  namespace: <namespace>
patches:                                  # 注入域：DELETE 演练 RoleBinding（execute 计划 kubectl 步骤，非 json-patch）
- op: delete
  path: /apis/rbac.authorization.k8s.io/v1/namespaces/<namespace>/rolebindings/drill-rb-<hash>
restorePatches:                           # 恢复域：POST 重建同名 RoleBinding（载体 TTL 到点自治执行）；Agent 死亡后 recover 从台账重放同源
- op: create
  path: /apis/rbac.authorization.k8s.io/v1/namespaces/<namespace>/rolebindings
  value: <下方资源准备第 3 条的完整 RoleBinding JSON（roleRef=drill-role-<hash>，subjects=drill-sa-<hash>）>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 恢复由标准件载体 TTL 自治承载（curl POST 重建 RoleBinding，幂等——对象已存在时 apiserver 返回 409 Conflict，restore.log 如实记录即「已恢复」证据）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行（先到先收敛、后到读回 409/no-op）。
- 载体 Role 授权面（三条独立 rule，均按 recovery-carrier.md 第二节两步建栈法 json-patch 追加、禁并入 create 命令）：主恢复 `get,create rolebindings`（POST 重建 create + GET 校验，按「恢复形态无关总则」以脚本实际载荷动词为准推导）+ **提权前置 `bind`**（resourceNames 锁 roleRef.name = drill-role-<hash>）+ 尾步自删 `delete rolebindings`。**`create rolebindings` 必要但不充分**：重建的 Binding 其 roleRef 指向 drill-role-<hash>，apiserver 提权防护要求载体对该被引用 Role 另持 `bind` 动词，否则 POST 重建被 403 拒（`... not currently held`，见第二节「RBAC 绑定重建的提权前置」）。

## 故障现象
1. 消费方 Pod（以演练 SA 运行）对目标资源的 API 调用由成功转为被拒——其日志内 HTTP 状态码由基线 `200` 翻为 `403 Forbidden`（行为判据，业务影响面）
2. **白盒主证（故障本体）**：演练 RoleBinding 消失——`kubectl get rolebinding drill-rb-<hash> -n <namespace>` 返回 NotFound；`kubectl auth can-i list pods --as=system:serviceaccount:<namespace>:drill-sa-<hash> -n <namespace>` 由 `yes` 翻 `no`
3. 演练 SA 与 Role 对象**仍存活**（注入只删 RoleBinding，不删 SA/Role）——`kubectl get sa drill-sa-<hash> / role drill-role-<hash>` 仍在，故障藏在「绑定关系缺失」里，不是账号或规则本身被销毁
4. 消费方 Pod 自身状态正常（Running、不崩溃、不重启）——认证（token 仍有效、身份仍是该 SA）不受影响，只有**授权**被撤销；故 Pod 不会 CrashLoop，只在调 API 时收 403
5. 与「Secret 凭证被篡改」的关键区别：那是凭证内容错→认证失败（401/认证链下游），本 case 是凭证/身份完好→授权绑定被删（403 Forbidden，RBAC authorizer 拒绝），二者判据与恢复路径完全不同

## 资源准备
1. **甄别/自建合格受害靶（规划期义务，本 case 硬前置）**：本 case 需一个以专用演练 SA 运行、持续调用 apiserver 的消费方 Deployment 作为可观测受害者（其 Pod 打印 HTTP 码）。集群无现成合格靶（且不应动既有生产 SA/RoleBinding），一律按**专用靶自建标准件**（`references/target/drill-target.md`）在演练 ns 内自建合成 RBAC 一套（`kubectl create`）+ 消费方 Deployment（drill-target manifest 契约 `kubectl apply -f -`，全部 drill- 前缀、全部建在被批准靶命名空间内——RBAC 三对象由漂移守卫以二级范围+命名空间锚定放行，消费方 Deployment 由 screener 以普通 drift net 锚定其 metadata.name=approved 身份并登记为 occupant_deployment 载体资产；建在其他 ns 会被判 scope/namespace drift 拦截）。
2. 记录基线授权态（注入前必须确认消费方对该资源确有权限，否则故障无从「撤销」）：
   ```bash
   kubectl auth can-i list pods --as=system:serviceaccount:<namespace>:drill-sa-<hash> -n <namespace>
   ```
   预期 `yes`（基线有权）——若为 `no` 说明 RoleBinding 未生效或 Role 规则不含该 verb，须先修正再注入。
3. 自建演练 RBAC 三对象 + 消费方 Deployment（SA/Role/RoleBinding 必须用 `kubectl create` 命令构造，`apply -f` 对 RBAC kinds 因白名单不收被设计性拦截；消费方 Deployment 则相反——它经 drill-target manifest 契约 `kubectl apply -f -` 自建，这是守卫为「专用靶自建」开的唯一 in-band 通道，命令式 `kubectl create deploy`/`kubectl run` 均被拒；RBAC 三对象须先于消费方 Deployment 创建——Deployment 的 Pod 模板挂接该 SA，SA 不存在时 Pod 卡 ContainerCreating）：
   ```bash
   kubectl create serviceaccount drill-sa-<hash> -n <namespace>
   kubectl create role drill-role-<hash> -n <namespace> --verb=get,list --resource=pods
   kubectl create rolebinding drill-rb-<hash> -n <namespace> --role=drill-role-<hash> --serviceaccount=<namespace>:drill-sa-<hash>
   ```
   消费方 Deployment（drill-target manifest 契约：单 Deployment 文档、metadata.name=drill-victim-<hash> 显式声明〔= approved 身份〕、恰一容器无 initContainers、image ∈ 载体允许集、command 不受限〔受害 workload 可做真实工作——那正是被注入故障的对象〕、serviceAccountName 挂演练 SA；其 Pod 以演练 SA 身份循环调 apiserver list pods，逐次打印 HTTP 状态码：200 基线 → 403 撤销后）：
   ```bash
   cat <<'EOF' | kubectl apply -f -
   apiVersion: apps/v1
   kind: Deployment
   metadata:
     name: drill-victim-<hash>
     namespace: <namespace>
   spec:
     replicas: 1
     selector:
       matchLabels:
         app: drill-victim-<hash>
     template:
       metadata:
         labels:
           app: drill-victim-<hash>
       spec:
         serviceAccountName: drill-sa-<hash>
         containers:
         - name: consumer
           image: curlimages/curl:latest
           command: ["/bin/sh", "-c"]
           args:
           - |
             C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
             T=/var/run/secrets/kubernetes.io/serviceaccount/token
             M=https://kubernetes.default.svc/api/v1/namespaces/<namespace>/pods?limit=1
             while true; do
               echo "$(date -u) HTTP $(curl -s -o /dev/null -w %{http_code} --cacert $C -H "Authorization: Bearer $(cat $T)" $M)"
               sleep 5
             done
   EOF
   ```
   （镜像字面量 `curlimages/curl:latest` 同时满足消费方与恢复载体两处的载体允许集，且自带 curl/sh/sleep/date 工具链——`-w %{http_code}` 取状态码依赖 curl，busybox 无 curl 不可用；受限网络〔VPC 无法拉 docker.io〕时按标准件第九节改选节点已缓存、含同套工具链且 ∈ 允许集的镜像。定名后按标准件第九节做精确同名冲突预检：`kubectl get sa,role,rolebinding,deploy drill-sa-<hash> drill-role-<hash> drill-rb-<hash> drill-victim-<hash> -n <namespace> -o name` 中对应名 NotFound 即无冲突。）
   重建 RoleBinding 用的完整 JSON（恢复载荷体，restorePatches.value 与载体 timer curl -d 同源；roleRef 创建后不可变，重建时须与基线一致）：
   ```json
   {"apiVersion":"rbac.authorization.k8s.io/v1","kind":"RoleBinding","metadata":{"name":"drill-rb-<hash>","namespace":"<namespace>"},"roleRef":{"apiGroup":"rbac.authorization.k8s.io","kind":"Role","name":"drill-role-<hash>"},"subjects":[{"kind":"ServiceAccount","name":"drill-sa-<hash>","namespace":"<namespace>"}]}
   ```
4. 按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈——四对象同名 `drill-rc-<hash>`、全部建在被批准的靶点命名空间内，SA/Role/RoleBinding 必须用 `kubectl create` 命令构造。本用例恢复动作是「POST 重建一个 namespaced RoleBinding」（roleRef 指向 drill-role-<hash>），载体 Role 主授权 `get,create rolebindings`；**但 `create rolebindings` 必要不充分**——apiserver 提权防护要求载体对被引用 Role（drill-role-<hash>）另持 `bind` 动词，否则 fire 时 POST 重建被 403 拒（标准件第二节「RBAC 绑定重建的提权前置」）。故建栈后须在载体 Pod 注册后按两步建栈法 json-patch 追加 bind 规则（resourceNames 锁 drill-role-<hash>）：
   ```bash
   kubectl create serviceaccount drill-rc-<hash> -n <namespace>
   kubectl create role drill-rc-<hash> -n <namespace> --verb=get,create --resource=rolebindings
   kubectl create rolebinding drill-rc-<hash> -n <namespace> --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=curlimages/curl:latest --restart=Never --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   # 提权前置（标准件第二节）：create rolebindings 必要不充分，载体 Pod 注册后追加 bind 规则
   # （resourceNames 锁被引用 Role = drill-role-<hash>，即重建 Binding 的 roleRef.name），否则
   # fire 时 POST 重建被提权防护 403 拒（... not currently held）；禁并入上方 create 命令
   kubectl patch role drill-rc-<hash> -n <namespace> --type=json \
     -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":["rbac.authorization.k8s.io"],"resources":["roles"],"resourceNames":["drill-role-<hash>"],"verbs":["bind"]}}]'
   ```
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节，含写动词 SSAR 对账——Role verbs 以恢复脚本实际载荷动词为准：验权清单 = step 1 对目标 rolebinding GET（武装发生在注入前、drill-rb-<hash> 仍在，预期 200）+ step 2 对 rolebindings 的 create SSAR 可授 + **step 3 内容感知 dry-run 武装门**（对与 timer 载荷同源的 POST 重建请求加 `?dryRun=All` 发一次，见标准件第三节；create SSAR 只答「能否 create rolebindings」、对提权失败给假阳性 `allowed:true`，dry-run 跑完整 apiserver 管线含提权防护才暴露内容相关拒绝——bind 已授时对已存在的 drill-rb-<hash> 回 409 即通过、缺 bind 时回 403 即中止），任一 403 即中止并清理）。**注意区分**：此处禁的是用 `can-i --as` 冒充「载体 SA 真实 token 能否写」的验权（假放行）；而故障现象/注入验证里对**受害 SA** 用 `can-i --as=system:serviceaccount:...` 判其授权态是合法白盒机制判据（SSAR 只读、权威），两者语境不同、不冲突。
   定时器用载体内 SA token 直调 apiserver REST，单条 POST curl 重建 RoleBinding 即完成全部恢复：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); S="Content-Type: application/json"; M=https://kubernetes.default.svc/apis/rbac.authorization.k8s.io/v1/namespaces/<namespace>/rolebindings; curl -sf -X POST --cacert $C -H "Authorization: Bearer $T" -H "$S" -d "{\"apiVersion\":\"rbac.authorization.k8s.io/v1\",\"kind\":\"RoleBinding\",\"metadata\":{\"name\":\"drill-rb-<hash>\",\"namespace\":\"<namespace>\"},\"roleRef\":{\"apiGroup\":\"rbac.authorization.k8s.io\",\"kind\":\"Role\",\"name\":\"drill-role-<hash>\"},\"subjects\":[{\"kind\":\"ServiceAccount\",\"name\":\"drill-sa-<hash>\",\"namespace\":\"<namespace>\"}]}" $M && curl -sf -X DELETE --cacert $C -H "Authorization: Bearer $T" https://kubernetes.default.svc/apis/rbac.authorization.k8s.io/v1/namespaces/<namespace>/rolebindings/drill-rc-<hash> ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （自断授权尾步——标准件第四节立法：**最后一个**恢复 curl（本案即 RoleBinding POST 重建，其后无其他恢复动作）升 `curl -sf` 并以 `&&` 链自删 DELETE 自己的 Binding（复用 `$C`/`$T`；本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`），建栈 Role 后按标准件第二节两步建栈法 json-patch 追加独立自删规则：
   ```bash
   kubectl patch role drill-rc-<hash> -n <namespace> --type=json \
     -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":["rbac.authorization.k8s.io"],"resources":["rolebindings"],"verbs":["delete"]}}]'
   ```
   （**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会把 delete 广播进主恢复规则、污染授权面。fail-open：主恢复（POST）未确认成功则授权保留——Agent 提前恢复后定时器 POST 得 409、`curl -sf` 视其为失败即不删自身 Binding，残留由演练结束四连删除兜底，与 LimitRange误配 的 404/no-op fail-open 同构；字节挤不下就不带——完整形态与五纪律以标准件第四节为准。）
   （`>/tmp/restore.log 2>&1` 是取证硬要求，禁止改回 `/dev/null`——fire 后带外 `kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log` 确认恢复 curl 的实际执行情况；三禁一许纪律见标准件第四节。倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）——本用例注入序列无长等待间隔（delete rolebinding 为快命令），武装紧邻注入动作前执行即可；武装后发生任何修复须先停旧定时器再全额重武装——定位进程用 `pkill -f 'kubernetes.default.svc'` 匹配 payload 含 apiserver URL 的定时器 shell（见 SKILL.md 安全红线「故障窗口完整」）。演练结束后按标准件第六节四连删除并带外核实零残留：
   ```bash
   kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete rolebinding drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete role drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
   ```

## 演练步骤
（主路径 = 基线捕获（步骤 1）→ 标准件武装（资源准备第 4 条）→ DELETE 演练 RoleBinding（步骤 3）；本 case 注入为 delete 既有演练对象、恢复为 POST 重建，无装配器调用）：

> **爆炸半径分类（定案，defer SKILL.md L41 + recovery-carrier §9）**：声明 `blast_radius_scope=namespace-wide`——双重依据：①故障本体写入集含新建 ns 级演练 RBAC 三对象（drill-sa-/drill-role-/drill-rb- 前缀）+ 消费方 Deployment（drill-target 自建），按 SKILL.md L41 末句「写入集里除 target 与一次性载体外只有 ns 内对象即为 namespace-wide」（演练 SA/Role/RoleBinding 是 ns 内持久 RBAC 对象、非无 RBAC 的一次性调试载体）；②恢复走 apiserver-write 四件套 carrier，新增 namespaced RBAC 授权面（§9①）。定案免推导，勿在 target-only↔namespace-wide 间重复仲裁。consequence 影响面 = **仅使用该演练 SA 的消费方 Deployment**（drill-victim-<hash>）及其 Pod——演练 SA 是本次专用、无其他 workload 引用，故实际业务波及收敛到单一合成消费方，不触及任何既有 SA/workload/命名空间；写入 `blast_radius_detail` 注明。无跨 ns 持久资源写入，勿抬到 cluster-wide。

1. **基线捕获**：记录受害 SA 的基线授权态与消费方基线 HTTP 码（两条路径共用的恢复对账基准）
   ```bash
   kubectl auth can-i list pods --as=system:serviceaccount:<namespace>:drill-sa-<hash> -n <namespace>
   kubectl logs deploy/drill-victim-<hash> -n <namespace> --tail=3
   ```
   预期 can-i = `yes`、日志近几条为 `HTTP 200`
2. **先武装定时自恢复，再注入**（资源准备第 4 条的标准件武装命令——恢复命令幂等：定时器到期 POST 重建为主，Agent 在演练结束时主动执行同源重建兜底，定时器迟到重复 POST 得 409 无副作用）
3. **注入**：删除演练 RoleBinding，撤销 SA 的授权绑定
   ```bash
   kubectl delete rolebinding drill-rb-<hash> -n <namespace>
   ```

## 注入验证
1. **（机制主证据，必做）** 白盒确认授权绑定已撤销：
   ```bash
   kubectl get rolebinding drill-rb-<hash> -n <namespace> -o name
   kubectl auth can-i list pods --as=system:serviceaccount:<namespace>:drill-sa-<hash> -n <namespace>
   ```
   预期：RoleBinding 查询 NotFound；can-i 由基线 `yes` 翻 `no`
2. **（行为证据，必做）** 消费方 Pod 的 API 调用被拒：
   ```bash
   kubectl logs deploy/drill-victim-<hash> -n <namespace> --tail=5
   ```
   预期日志出现 `HTTP 403`（由基线 `HTTP 200` 翻转）——403 Forbidden 是 RBAC authorizer 拒绝的直接业务证据
3. 佐证 SA/Role 对象仍存活（区分「绑定被删」与「账号/规则被销毁」）：
   ```bash
   kubectl get sa drill-sa-<hash> -n <namespace> -o name
   kubectl get role drill-role-<hash> -n <namespace> -o name
   ```
   预期二者均仍在（注入只删 RoleBinding）

> ⚠️ 验证纪律：
> - 403 是**授权层**结果，消费方 Pod 本身 Running 正常、不崩溃——不要因 Pod 无重启/无 CrashLoop 就判故障未生效；判据在 HTTP 码与 can-i，不在 Pod 相位。
> - 同一事实（RoleBinding 是否缺失、can-i 是否翻 no、日志是否 403）确认一次即可，不要重复查询。
> - `can-i --as` 对**受害 SA** 是合法机制判据（SSAR 只读）；不要与载体 SA 武装前验权（禁 can-i --as 假放行）混淆——后者验的是载体真实 token 的写路径。

**持续性检查**——故障窗口内故障必须持续存活（配置型故障：RoleBinding 缺失即故障在）：
以「注入生效确认」为时点锚（注入验证第 1-2 条通过 = 生效）——生效确认只证明某一刻故障已落地，不证明它在整个窗口持续存活；持续性复测要把「稳态故障」与「传播收敛窗内的瞬时态」分开：待传播收敛后再次观测仍见故障态才算坐实持续。本故障传播特性：权限撤销即时生效无传播，消费方每 5s 打一次 HTTP 码。隔多久复测、复测几次由你依该传播特性与故障形态自行判断（无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：RoleBinding 仍 NotFound、can-i 仍 no（同注入验证第 1 条）
2. 行为复查：消费方日志持续输出 `HTTP 403`、无回落到 200（同注入验证第 2 条形态）
3. 消费方 Pod 相位复查：Pod 仍 Running、RESTARTS 未增（授权撤销不杀 Pod，纯 403——若 Pod 反常重启须查证是否镜像/命令问题而非本故障）

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治 POST 重建 RoleBinding（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动 POST 重建 RoleBinding；演练提前结束时由 Agent 主动执行同源重建（幂等——RoleBinding 已存在时 `kubectl create` 报 AlreadyExists、载体 POST 得 409，均为「已恢复」证据，无副作用）：
   ```bash
   kubectl create rolebinding drill-rb-<hash> -n <namespace> --role=drill-role-<hash> --serviceaccount=<namespace>:drill-sa-<hash>
   ```
2. **关键前提**：重建 RoleBinding 依赖演练 Role（drill-role-<hash>）与 SA（drill-sa-<hash>）仍存活——注入只删 RoleBinding、未删二者，故重建即恢复授权；roleRef 创建后不可变，重建的 roleRef/subjects 必须与基线一致（用资源准备第 3 条记录的同一 JSON）。恢复后 SA 的下一个 API 调用即恢复 200（RBAC 实时评估，无传播窗、无需重启消费方 Pod）。

## 恢复验证
1. 白盒确认 RoleBinding 已重建、授权已恢复：
   ```bash
   kubectl get rolebinding drill-rb-<hash> -n <namespace> -o name
   kubectl auth can-i list pods --as=system:serviceaccount:<namespace>:drill-sa-<hash> -n <namespace>
   ```
   预期：RoleBinding 查询命中；can-i 回到 `yes`
2. 行为确认——消费方 Pod 的 API 调用恢复成功：
   ```bash
   kubectl logs deploy/drill-victim-<hash> -n <namespace> --tail=5
   ```
   预期日志重新出现 `HTTP 200`（由 403 恢复）
3. 确认载体 restore.log 取证（`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`——定时器 fire 的直接证据，POST 响应回显；409/AlreadyExists 形态 = Agent 提前恢复后定时器迟到空触发，同为已恢复证据）
4. 演练结束零残留核实——删除消费方 Deployment（`kubectl delete deploy drill-victim-<hash> -n <namespace> --ignore-not-found`，级联删其 Pod；screener 已登记为 occupant_deployment 载体资产，清理链亦会自动删）与演练 RBAC 三对象（drill-sa-/drill-role-/drill-rb-）+ 载体四连删除（资源准备第 4 条尾），带外确认全部 NotFound

## 基准事实
- **根因**：把演练 ServiceAccount 绑定到其 Role 的 RoleBinding 被删除，RBAC 授权器在 SA 的下一次 API 请求查不到任何授予相应 verb 的绑定 → 授权拒绝（403 Forbidden）。模拟运维误删/误回收授权绑定、GitOps 同步遗漏 RoleBinding、RBAC 重构误伤既有绑定等「权限误回收」场景。
- **必现现象**：消费方 Pod 调 apiserver 由 200 翻 403；`kubectl get rolebinding drill-rb-<hash>` NotFound；`can-i --as=system:serviceaccount:<ns>:<sa>` 由 yes 翻 no；SA 与 Role 对象仍存活、消费方 Pod 仍 Running（认证不受影响、只有授权被撤）。
- **与相邻形态的判据分野**：①与「Secret 凭证被篡改」——那是凭证内容错引发认证失败（401/认证链），本 case 凭证完好、授权绑定缺失（403/RBAC）；②与「镜像拉取失败_凭证缺失」——那是 imagePullSecret 缺失致拉镜像失败（Pod 卡 ImagePullBackOff），本 case Pod 正常运行、只在调 API 时收 403；③与「SA token 过期/失效」——那是认证层 token 无效（401 Unauthorized），本 case token 有效、授权层拒绝（403 Forbidden）。401 vs 403 是本 case 与认证类故障的硬分野。
- **作用域边界**：仅使用该演练 SA 的消费方受影响；其他 SA、其他 workload、其他命名空间、集群级授权（ClusterRole/ClusterRoleBinding）均不受影响。演练 SA 为本次专用、无外部引用，爆炸半径收敛到单一合成消费方。
- **自证链与 duration 契约**：故障为配置型即时生效（RoleBinding 一删，SA 下一个 API 请求立即 403），无传播窗；duration 下限取系统最小窗口地板（600s）并宁宽勿窄——窗口须覆盖 planning+execute 全程，否则 verify 段首观测会落在载体 fire 之后、primary 证据（403 + can-i no）无法在窗内自采。

## 注意事项
- chaosblade 无 RBAC/权限类故障靶点，本用例为 kubectl-native 专属注入（`kubectl delete rolebinding`）。
- 合成靶是硬前置（资源准备第 1-3 条）：**禁止**删除既有生产 RoleBinding 来制造本故障——不可移植（换集群失效）、且误伤真实 workload 授权面。演练 RBAC 三对象与消费方 Pod 全部 drill- 前缀、全部建在被批准靶 ns 内，恢复阶段一并清理。
- 401 vs 403 语义分野：本 case 是 **403 Forbidden**（认证通过、授权拒绝）；若消费方日志出现 **401 Unauthorized**，说明 SA token 本身失效（认证层问题），不是本故障——须查证 token 挂载/SA 是否被误删（本 case 注入不删 SA，token 应始终有效）。
- roleRef 不可变：RoleBinding 的 roleRef 创建后不可 patch 修改，恢复重建时 roleRef 必须与基线逐字一致（用资源准备第 3 条记录的 JSON），否则重建的绑定指向错误 Role、授权不恢复。
- 授权实时生效、无需重启消费方：RBAC authorizer 每请求实时评估，删/建 RoleBinding 后 SA 的下一次调用立即反映新授权态——恢复后**不需要**重启消费方 Pod，其下一个 5s 采样点即回 200。
- 演练道具命名带 `drill-sa-`/`drill-role-`/`drill-rb-`/`drill-victim-` 前缀（任务侧资产归属登记依据 + mechanism_writes name_prefix 立法锚点），**禁止**给演练资源加任何演练标记 label（演练必须对集群呈现为真实事故，见 recovery-carrier.md 第一节命名约定）。
- 消费方与载体镜像统一用字面 `curlimages/curl:latest`（∈ 载体允许集、自带 curl/sh/sleep/date）——busybox 无 curl，`-w %{http_code}` 取状态码不可行；受限网络〔VPC 无法拉 docker.io〕时按标准件第九节改选节点已缓存、含同套工具链且 ∈ 允许集的镜像。
