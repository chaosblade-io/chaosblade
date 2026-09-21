---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 移除注入的调度约束并还原副本数与 maxUnavailable），
# 路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+
# 武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者自身
# （patch Deployment 的 affinity/replicas/maxUnavailable 属受害者域内，名字匹配
# 放行），无跨对象写条目；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名
# drill-rc-<hash> 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌检查
# （RBAC 从 restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），
# 不经 LLM kubectl 写面，无立法条目。
---

**用例名称** 拓扑约束过严 导致 Pod_Pending

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 移除注入的调度约束并还原副本数与 maxUnavailable；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
# 首选反亲和形态（正文立法：topologySpreadConstraints 形态 Pending 判据结构性不可达）
  - op: add
    path: /spec/template/spec/affinity
    value:
      podAntiAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector: {matchLabels: {app: <app-name>}}
          topologyKey: kubernetes.io/hostname
  - op: replace
    path: /spec/replicas
    value: <超出节点数的副本数>
  - op: replace
    path: /spec/strategy/rollingUpdate/maxUnavailable
    value: "100%"
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
# 逆序还原（正文恢复顺序立法）：affinity → replicas → maxUnavailable
  - op: remove
    path: /spec/template/spec/affinity
  - op: replace
    path: /spec/replicas
    value: <基线副本数>
  - op: replace
    path: /spec/strategy/rollingUpdate/maxUnavailable
    value: <基线值>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- maxUnavailable 还原须待恢复流程移除约束且第二次滚动完成后（正文「暂缓还原」纪律）——载体按 restorePatches 顺序执行，逆序排列已满足该纪律。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 状态为 Pending，无法被调度
2. Pod Events 中显示 `didn't match pod topology spread constraints` 或 `didn't match pod anti-affinity rules`
3. 由于拓扑分布约束或反亲和规则过严，调度器无法找到满足条件的节点

**资源准备**：
1. 确认应用 A 已正常运行
2. 记录可调度节点数——反亲和 required 以节点为拓扑域上限，副本扩容目标超过该值时 Pending 必现（演练步骤 4/7 的参数依据）：
   ```bash
   kubectl get nodes --no-headers | grep -vc SchedulingDisabled
   ```
3. 按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈——四对象同名 `drill-rc-<hash>`、全部建在被批准的靶点命名空间内（漂移守卫以二级范围+命名空间锚定放行），SA/Role/RoleBinding 必须用 `kubectl create` 命令构造（`apply -f` 因清单内容对守卫不可见被设计性拦截）：
   ```bash
   kubectl create sa drill-rc-<hash> -n <namespace>
   kubectl create role drill-rc-<hash> -n <namespace> --verb=get,patch --resource=deployments
   kubectl create rolebinding drill-rc-<hash> -n <namespace> --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=busybox:1.36 --restart=Never --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   ```
   （镜像按标准件第九节判别法选定——受限网络（VPC 无法拉 docker.io）时选节点已缓存且含 curl/sh/sleep 工具链的镜像；**第九节「本集群实证档案」中精确 tag 匹配的镜像可直接引用免工具链探测**（探测需额外约 150s 的规划轮）；节点带 taint 时 overrides 需加 tolerations，见标准件第一节。SA/Role/RoleBinding 须先于载体 Pod 创建：overrides 挂接的 Pod 引用该 SA，SA 不存在时 Pod 卡 ContainerCreating。）
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节，含写动词 SSAR 对账——Role verbs 以恢复脚本实际载荷动词为准，换恢复形态时按第二节形态无关总则重建）。定时器用载体内 SA token 直调 apiserver REST——本用例四个恢复动作，**必须用标准件第四节「紧凑变量形态」的 for 循环变体**（完整形态四 curl 约 1.6KB 超通道 1024B 上限不可派发；逐 curl 变量形态同参数约 1118B **仍超限**——勿用；for 循环变体约 657B 在限内。**禁止改写为 printf/heredoc 写脚本文件形态**——写入层把 `$T`/`$C` 空展开固化，恢复 curl 无认证静默失败，见标准件第四节形态纪律。恢复动作全部是对 deployment 主资源的 merge-patch——约束字段置 null 即删除（基线为空时）或写回基线值（基线非空时）、replicas 与 maxUnavailable 同 patch 还原，故 Role 只需 deployments 的 get/patch，无需 scale 子资源授权）：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); U=https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<deployment-name>; for d in "{\"spec\":{\"template\":{\"spec\":{\"affinity\":<基线JSON或null>}}}}" "{\"spec\":{\"template\":{\"spec\":{\"topologySpreadConstraints\":<基线JSON或null>}}}}" "{\"spec\":{\"replicas\":<基线副本数>}}}" "{\"spec\":{\"strategy\":{\"rollingUpdate\":{\"maxUnavailable\":<基线值或null>}}}}"; do curl -s -X PATCH --cacert $C -H "Authorization: Bearer $T" -H "Content-Type: application/merge-patch+json" -d "$d" $U; done ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （自断授权尾步——标准件第四节立法：for 循环内每个恢复 curl 升 `curl -sf`，`done` 后以 `&& curl -sf -X DELETE` 自删自己的 Binding（本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`；两环 `-f` 缺一不可——循环退出码是最后执行命令的退出码，循环内 curl 不带 -f 则 `done` 恒 0、`&&` 恒真，主恢复失败照样自删）；建栈 Role 后按标准件第二节两步建栈法 json-patch 追加独立自删规则（**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行）。字节挤不下就不带——完整形态与五纪律以标准件第四节为准）
   （merge-patch 对不存在的字段置 null 无操作——两种注入形态下四个 patch 均幂等：仅反亲和注入时 topologySpreadConstraints 的 null patch 无效果；curl 顺序即安全顺序——约束移除必须先于 maxUnavailable 还原，否则第二次滚动死锁，见演练步骤 6。`>/tmp/restore.log` 保留恢复取证证据：fire 后带外 `kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log` 确认恢复 curl 实际执行情况。）
   倒计时从武装时刻起算：武装与注入必须是紧邻步骤（≤60s）；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f deployment[s]; true'`（定位不到定时器进程即中止本次演练改人工恢复，见 SKILL.md 安全红线「故障窗口完整」）。演练结束后按标准件第六节四连删除并带外核实零残留：
   ```bash
   kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete rolebinding drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete role drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
   ```

**演练步骤**（主路径 = 基线捕获后调 `faultdrill_assemble_carrier`（参数取自载体配方），注入+武装+readback 工具内同步完成；以下手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 记录还原基线（基线捕获：Agent 读取输出并记录以下字段的原始值，恢复时使用；
   topologySpreadConstraints/affinity 原本无约束时输出为空）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.topologySpreadConstraints}'
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.affinity}'
   kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.replicas}'
   ```
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. **先武装定时自恢复，再注入**（主路径 = 资源准备第 3 条的标准件 REST 武装——载体内无 kubectl，恢复动作用 SA token 直调 apiserver。恢复命令幂等：定时器到期自动恢复为主，Agent 在演练结束时主动执行同一组命令兜底，定时器迟到重复执行无副作用。载体内 `sh -c` 同时解决 exec-form 通道不解释裸 `( sleep … ) &` 语法的问题；载体 Pod 为单副本 `--restart=Never`，定位不到定时器进程即视为异常，中止本次演练改人工恢复。**`<duration>` 必须覆盖注入滚动、扩容观察与恢复滚动全程**——本用例恢复含第二次滚动，窗口比状态型故障更宽，宁宽勿窄）：
   ```bash
   # 主路径：标准件武装（= 资源准备第 3 条「定时器用载体内 SA token」下的完整 kubectl exec 命令，四个 merge-patch 顺序执行，原样复制）
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( …见资源准备第 3 条：affinity → topologySpreadConstraints → replicas → maxUnavailable 四个 PATCH… ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f deployment[s]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
4. 修改应用 A 的 Deployment，注入调度约束（**首选反亲和形态**——topologySpreadConstraints
   的 `maxSkew: 1` 语义是"任意两拓扑域副本数差 ≤ 1"，资源充足时任意副本数都能均匀铺开，
   Pending 判据结构性不可达（如 8 节点铺 12 副本得 4×2+4×1、铺 17 副本得 1×3+7×2，
   全部调度成功）；反亲和 required 语义是每拓扑域排他，副本数 > 节点数时 Pending 必现）：
   ```yaml
   # 首选：podAntiAffinity（required = 每拓扑域排他 → 副本数 > 节点数时 Pending 必现）
   affinity:
     podAntiAffinity:
       requiredDuringSchedulingIgnoredDuringExecution:
       - labelSelector:
           matchLabels:
             app: <app-name>
         topologyKey: kubernetes.io/hostname
   ```
   若确需验证 topologySpreadConstraints 形态（判据不可达，仅作约束生效性观察——已调度 Pod
   呈均匀分布，Pending 不出现）：
   ```yaml
   topologySpreadConstraints:
   - maxSkew: 1
     topologyKey: kubernetes.io/hostname
     whenUnsatisfiable: DoNotSchedule
     labelSelector:
       matchLabels:
         app: <app-name>
   ```
5. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
6. 注入验证完成后暂缓还原 maxUnavailable——**必须等恢复流程移除约束且第二次滚动完成后再还原**
   （反亲和形态下若注入后立即还原为默认 25%，恢复时移除约束触发的第二次滚动中，新 RS
   Pod 会被仍在运行的旧 RS Pod 的反亲和规则挡住（Events：`didn't satisfy existing pods
   anti-affinity rules`），新 Pod Pending + 旧 RS 滞留形成滚动死锁，可持续数分钟才自行
   破局。maxUnavailable 保持 100% 直至恢复完成是防死锁的关键）
7. 将应用 A 的副本数扩大到超过集群节点数
8. 观察无法调度的 Pod 状态

**注入验证**：
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 永不被调度（无节点可满足拓扑约束，故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 处于 Pending）
2. 执行 `kubectl get pods`，确认部分或全部 Pod 状态为 Pending
3. 执行 `kubectl describe pod <pending-pod>`，确认 Events 显示拓扑约束或反亲和相关的调度失败原因
   （**事件时间戳形态注**：部分集群 events 呈新式字段形态——lastTimestamp 为 null、count 为空，属 API 字段差异**非数据缺失**；判读以 eventTime 与 series.lastObservedTime 为准。直接解读即可，**无需为重建时间线做交叉推算**）

**持续性检查（必做）**——故障窗口内故障必须持续存活（约束是配置型故障，字段在 spec 即故障在）：
以「注入生效确认」为时点锚（注入验证第 1-3 条通过 = 生效：滚动完成 + 扩容副本 Pending + Events 见调度失败），生效后一次 `time_wait 60`，到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：约束字段仍在 spec（`kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.affinity}'` 输出非空，配置即状态）
2. 行为复查：超节点数扩出的副本仍 Pending（`kubectl get pods -n <namespace> -l <app-label>` 中 Pending 数不变）
3. 事件复查：FailedScheduling 事件 LAST SEEN 相比注入时有新增（调度器仍在周期性重试）

**注入恢复**（主路径下恢复无需 Agent 执行动作——载体 TTL 自治按 restorePatches 逆序还原，fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动还原拓扑约束与副本数；演练提前结束时由 Agent 主动执行
   同组恢复命令（幂等，定时器迟到再执行一次无副作用。json patch 按字段精确替换/移除，天然
   规避 resourceVersion 乐观锁问题，也不会像 apply 三方合并那样保留注入新增的字段。
   **顺序即安全**——约束移除必须先于 maxUnavailable 还原，否则第二次滚动死锁，见步骤 6）：
   ```bash
   # ① 还原 topologySpreadConstraints（基线非空时 replace 基线 JSON；原本为空时 remove）
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"remove","path":"/spec/template/spec/topologySpreadConstraints"}]'
   # ② 还原 affinity（同上按基线 replace/remove）
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"remove","path":"/spec/template/spec/affinity"}]'
   # ③ 副本数恢复为基线值
   kubectl scale deployment <deployment-name> -n <namespace> --replicas=<基线副本数>
   ```
2. 等待 Pod 滚动更新完成（约束已移除，新 RS Pod 不再受反亲和阻挡）
3. 最后还原 maxUnavailable 为基线值（此前的 100% 只是滚动保障手段，恢复完成后必须收回）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"<基线值>"}]'
   ```

**恢复验证**：
1. 执行 `kubectl get pods -n <namespace> -l <app-label>`，确认副本数回到基线且全部 Running/Ready、无 Pending 残留（第二次滚动已完成，无死锁）
2. 确认约束字段已移除（或还原为基线值）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.affinity}'
   ```
   输出与基线捕获一致（基线原本为空则输出为空）
3. 确认 maxUnavailable 已还原为基线值——步骤 2 临时设的 100% 只是滚动保障手段，恢复完成后必须收回：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   ```
4. 确认调度失败事件不再新增——事件列表是 append-only 的（历史事件不会消失），判据是 **LAST SEEN 时戳不再前进**：恢复后复查一次，最后一条 FailedScheduling 的 LAST SEEN 停在恢复时刻之前即确证「不再新增」：
   ```bash
   kubectl get events -n <namespace> --field-selector reason=FailedScheduling
   ```

**基准事实**：
- **根因**：topologySpreadConstraints 或 podAntiAffinity 配置过严，当副本数超过可用拓扑域时，调度器无法满足约束条件
- **必现现象**：部分 Pod Pending；Events 显示拓扑约束或反亲和规则不满足；已调度 Pod 严格按约束分布
