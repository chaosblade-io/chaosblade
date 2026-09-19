---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 缩回基线副本数），路由进 FaultDrill
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

**用例名称** ResourceQuota超限 导致 Pod_Pending

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 缩回基线副本数；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

```yaml
apiVersion: drill.blade-ai.io/v1alpha1   # 组名可配（faultdrill_crd_group）
kind: FaultDrill
metadata:
  name: fd-<任务派生短哈希>               # 前缀可配（faultdrill_name_prefix）；零演练签名词根
  namespace: <namespace>                  # 必须显式写入——见下方 P10 条款
spec:
  action: specPatch
  targetRef:
    kind: <workload-kind>
    name: <workload-name>
    namespace: <namespace>
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: replace
    path: /spec/replicas
    value: <基线副本数+增量>
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  - op: replace
    path: /spec/replicas
    value: <基线副本数>
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- 道具 quota（`kubectl create quota --hard=pods=<不超过当前Pod数>`——上限低于存量 Pod 数的瞬态约束对象）为 execute 计划 manifest/kubectl 步骤；收尾清理步删除；故障解除判据 = 副本回基线且全部 Running。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. 应用扩容后新副本迟迟不就绪，DESIRED > READY
2. ReplicaSet 持续报 FailedCreate 事件：`Error creating: pods "<pod>" is forbidden: exceeded quota: <quota-name>, requested: pods=1, used: pods=<N>, limited: pods=<M>`
3. 注意真实签名：Pod 被准入控制（admission）直接拒绝，**Pod 对象根本不会被创建**，因此观察不到 Pending 状态的 Pod——"扩容卡住 + FailedCreate 事件"才是本故障的必现现象

**资源准备**：
1. 确认应用 A 的 Deployment/StatefulSet 正常运行，记录当前副本数为基线
2. 确认目标命名空间当前没有已存在的 ResourceQuota（有则记录并在恢复时保留原状）
3. 确认集群内存在带 kubectl 且有该命名空间 RBAC 权限的常驻载体 Pod（用于武装定时自恢复）。**若集群无此类载体**（chaosblade-tool/operator 镜像均无 kubectl，chaosblade SA 对业务命名空间无 scale/delete quota 权限），按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈——四对象同名 `drill-rc-<hash>`、全部建在被批准的靶点命名空间内（漂移守卫以二级范围+命名空间锚定放行，建在其他命名空间会被判 scope/namespace drift 拦截）、SA/Role/RoleBinding 必须用 `kubectl create` 命令构造（`apply -f` 因清单内容对守卫不可见被设计性拦截）：
   ```bash
   kubectl create sa drill-rc-<hash> -n <namespace>
   kubectl create role drill-rc-<hash> -n <namespace> --verb=get,patch --resource=deployments/scale
   kubectl create rolebinding drill-rc-<hash> -n <namespace> --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=busybox:1.36 --restart=Never --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   # 载体 Pod 注册后追加第二资源族（两步法——多 flag 单命令 verbs 并集广播，
   # 会产生 delete×deployments/scale + patch×resourcequotas 超授权面，见标准件第一节立法）
   kubectl patch role drill-rc-<hash> -n <namespace> --type=json \
     -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["resourcequotas"],"verbs":["get","delete"]}}]'
   ```
   （镜像按标准件第九节判别法选定——受限网络（VPC 无法拉 docker.io）时选节点已缓存且含 curl/sh/sleep 工具链的镜像；节点带 taint 时 overrides 需加 tolerations，见标准件第一节。SA/Role/RoleBinding 须先于载体 Pod 创建：overrides 挂接的 Pod 引用该 SA，SA 不存在时 Pod 卡 ContainerCreating。）
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节，含写动词 SSAR 对账——Role verbs 以恢复脚本实际载荷动词为准，换恢复形态时按第二节形态无关总则重建）。定时器用载体内 SA token 走集群内 apiserver REST：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; curl -s -X PATCH --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" -H "Content-Type: application/merge-patch+json" -d "{\"spec\":{\"replicas\":<基线副本数>}}" https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<name>/scale; curl -s -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" https://kubernetes.default.svc/api/v1/namespaces/<namespace>/resourcequotas/<quota-name> ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （自断授权尾步——标准件第四节立法：**最后一个**恢复 curl（quota DELETE）升 `curl -sf` 并以 `&&` 链自删 DELETE 自己的 Binding（复用本模板的完整 cacert/token 头或同构变量形态；本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`；前序动作保持 `;` 串联不中断），建栈 Role 后按标准件第二节两步建栈法 json-patch 追加独立自删规则（**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行）。fail-open：主恢复未确认成功则授权保留；字节挤不下就不带——完整形态与五纪律以标准件第四节为准）
   倒计时从武装时刻起算：武装与注入必须是紧邻步骤（≤60s）；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f resourcequota[s]; true'`（定位不到定时器进程即中止本次演练改人工恢复，见 SKILL.md 安全红线「故障窗口完整」）。演练结束后按标准件第六节四连删除并带外核实零残留：
   ```bash
   kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete rolebinding drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete role drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
   ```

**演练步骤**：
1. **基线捕获**：记录 Pod 总数与 workload 副本数（定时器与主动恢复均使用）
   ```bash
   kubectl get pods -n <namespace> --no-headers
   kubectl get <workload-kind> <name> -n <namespace> -o jsonpath={.spec.replicas}
   ```
2. **先武装定时自恢复，再注入**（主路径 = 资源准备第 3 条的标准件 REST 武装——载体内无 kubectl，恢复动作用 SA token 直调 apiserver。恢复命令幂等：定时器到期自动恢复为主，Agent 在演练结束时主动执行同一组命令兜底，定时器迟到重复执行无副作用。载体内 `sh -c` 同时解决 exec-form 通道不解释裸 `( sleep … ) &` 语法的问题；载体 Pod 为单副本 `--restart=Never`，定位不到定时器进程即视为异常，中止本次演练改人工恢复）
   ```bash
   # 主路径：标准件武装（资源准备第 3 条的完整命令，REST 形态）
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; curl -s -X PATCH --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" -H "Content-Type: application/merge-patch+json" -d "{\"spec\":{\"replicas\":<基线副本数>}}" https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<name>/scale; curl -s -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" https://kubernetes.default.svc/api/v1/namespaces/<namespace>/resourcequotas/<quota-name> ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：武装与注入必须是紧邻步骤（≤60s）；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f resourcequota[s]; true'`（见 SKILL.md 安全红线「故障窗口完整」）
3. **注入**：创建上限低于当前 Pod 数的配额，再调高 workload 副本数，新副本即被准入控制拒绝
   ```bash
   kubectl create quota <quota-name> --hard=pods=<不超过当前Pod总数> -n <namespace>
   kubectl scale <workload-kind> <name> -n <namespace> --replicas=<基线副本数+增量>
   ```

**注入验证**：
1. 执行 `kubectl get <workload-kind> <name> -n <namespace>`，确认 READY < DESIRED 且持续不收敛
2. 确认 FailedCreate 事件（根因证据，消息中含 `exceeded quota`）：
   ```bash
   kubectl get events -n <namespace> --field-selector reason=FailedCreate
   ```
3. 确认配额用量已超限：
   ```bash
   kubectl get resourcequotas.v1. <quota-name> -n <namespace>
   ```

**持续性检查（必做）**——故障窗口内故障必须持续存活（quota 是状态型故障，quota 存活即故障存活）：
以「注入生效确认」为时点锚（注入验证第 1-2 条通过 = 生效；RS 控制器 FailedCreate 重试有 backoff，事件非秒级连发属正常，READY 不收敛即持续中），生效后一次 `time_wait 60`，到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：quota 仍存在且 `used` 仍顶在 `hard`（配置即状态）
2. 行为复查：READY 仍 < DESIRED 持续不收敛（同注入验证第 1 条形态）
3. 事件复查：FailedCreate 事件 LAST SEEN 相比注入时有新增（仍在被准入拒绝）

**注入恢复**：
1. 等待 `<duration>` 到期，定时器自动缩回基线副本并删除配额；演练提前结束时由 Agent 主动执行同一组恢复命令（幂等，定时器迟到再执行一次无副作用）：
   ```bash
   kubectl scale <workload-kind> <name> -n <namespace> --replicas=<基线捕获的原始副本数>
   kubectl delete resourcequotas.v1. <quota-name> -n <namespace>
   ```
2. 等待新副本自动创建并 Running

**恢复验证**：
1. 执行 `kubectl get pods -n <namespace> -l <app-label>`，确认 Pod 数与副本数回到基线且全部 Running/Ready
2. 确认 FailedCreate 事件不再新增——事件列表是 append-only 的（历史事件不会消失），判据是 **LAST SEEN 时戳不再前进**：恢复后复查一次，最后一条 FailedCreate 的 LAST SEEN 停在恢复时刻之前即确证「不再新增」：
   ```bash
   kubectl get events -n <namespace> --field-selector reason=FailedCreate
   ```
3. 确认配额已删除（或还原为演练前状态）：
   ```bash
   kubectl get resourcequotas.v1. -n <namespace>
   ```

**基准事实**：
- **根因**：命名空间 ResourceQuota 上限低于 workload 期望副本所需，新 Pod 在准入控制阶段被拒绝创建
- **必现现象**：READY < DESIRED 持续不收敛；ReplicaSet FailedCreate 事件含 `exceeded quota`；无 Pending Pod 对象产生

**注意事项**：
- chaosblade 无 ResourceQuota/准入类故障靶点，本用例为 kubectl-native 专属注入
- 优先选择无 HPA 的 workload 作靶点：存在 HPA 时其会在约 15s 内收回手动扩出的副本，抢占故障窗口与定时器动作
- 部分托管集群安装了抢占短名的 CRD（如 `quotas.quotas.alibabacloud.com`），`kubectl get/delete quota` 会命中 CRD 并报 NotFound——必须使用组限定写法 `resourcequotas.v1.`
- 注入不改变任何已有 Pod，仅阻止新 Pod 创建，爆炸半径限于目标 workload 的扩容能力；恢复后由 Deployment 控制器自动补齐副本，无需人工干预
- 若命名空间已有 ResourceQuota，注入前先记录其内容，恢复阶段不得误删他人配额——优先使用独立命名的演练配额并在恢复时仅删除该配额
