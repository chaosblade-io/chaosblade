---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 ConfigMap 配置值），路由进 FaultDrill
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

**用例名称** 关键配置被篡改 导致 ConfigMap_内容错误

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 ConfigMap 配置值；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

```yaml
apiVersion: drill.blade-ai.io/v1alpha1   # 组名可配（faultdrill_crd_group）
kind: FaultDrill
metadata:
  name: fd-<任务派生短哈希>               # 前缀可配（faultdrill_name_prefix）；零演练签名词根
  namespace: <namespace>                  # 必须显式写入——见下方 P10 条款
spec:
  action: specPatch
  targetRef:
    kind: ConfigMap
    name: <configmap-name>
    namespace: <namespace>
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: replace
    path: /data/<目标配置键>
    value: <篡改后的配置值>
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  - op: replace
    path: /data/<目标配置键>
    value: <注入前记录的基线配置值>
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- 配置生效触发（Pod 重启或应用 reload）保留为 execute 计划普通步骤。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. 应用滚动更新后读取到错误的配置值（环境变量或挂载文件内容异常）
2. 依赖该配置的功能出错：日志级别失控、连接串指向错误地址、开关被误翻转等
3. Pod 本身 Running 不崩溃——这是配置类故障与资源类故障的关键区别：故障藏在配置语义里，不在 Pod 状态上

**资源准备**：
1. 确认应用 A 的 Deployment/StatefulSet 正常运行
2. 确认应用确实消费目标 ConfigMap（env from / volume mount）；若不消费，先完成接线并等待滚动完成：
   ```bash
   kubectl create cm <cm-name> -n <namespace> --from-literal=<KEY>=<baseline-value>
   kubectl set env <workload-kind>/<name> -n <namespace> --from=configmap/<cm-name>
   kubectl rollout status <workload-kind>/<name> -n <namespace> --timeout=120s
   ```
3. 按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈——四对象同名 `drill-rc-<hash>`、全部建在被批准的靶点命名空间内（漂移守卫以二级范围+命名空间锚定放行；建在其他命名空间会被判 scope/namespace drift 拦截），SA/Role/RoleBinding 必须用 `kubectl create` 命令构造（`apply -f` 因清单内容对守卫不可见被设计性拦截）。本用例两类恢复对象（configmap + deployment）均为 namespaced，Role 用**逗号合并语法**（两类资源 verb 相同；多资源语法是逗号分隔——点号分隔会被解析为不存在的资源名导致创建静默失败）：
   ```bash
   kubectl create serviceaccount drill-rc-<hash> -n <namespace>
   kubectl create role drill-rc-<hash> -n <namespace> --verb=get,patch --resource=configmaps,deployments
   kubectl create rolebinding drill-rc-<hash> -n <namespace> --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=busybox:1.36 --restart=Never --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   ```
   （镜像按标准件第九节判别法选定——受限网络（VPC 无法拉 docker.io）时选节点已缓存且含 curl/sh/sleep 工具链的镜像：候选清单 ∩ 实证档案非空取第一个命中即定案；SA/Role/RoleBinding 须先于载体 Pod 创建：overrides 挂接的 Pod 引用该 SA，SA 不存在时 Pod 卡 ContainerCreating。定名后按标准件第九节做精确同名冲突预检：`kubectl get sa,role,rolebinding,pod drill-rc-<hash> -n <namespace> -o name`——NotFound 即无冲突。）
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节，含写动词 SSAR 对账——Role verbs 以恢复脚本实际载荷动词为准；验权清单覆盖**两类对象**——configmap GET 200 且 deployment GET 200，任一 403 即中止）。定时器用载体内 SA token 直调 apiserver REST。**curl 顺序即执行顺序**：先 configmap data 还原（数据），后 deployment 注解 patch（触发滚动）——先还原数据再触发滚动，顺序颠倒会让滚动加载到未还原的错误值。两条 curl 对象不同，用**紧凑变量形态的逐 curl 变体**（变量赋值 C/T/S/M/D 后逐条引用，约 ~700B 在通道 1024B 限内）；恢复滚动触发用 kubectl 原生注解 `kubectl.kubernetes.io/restartedAt`（与 `kubectl rollout restart` 等价且是 kubectl 生态常规注解，非演练标记）：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); S="Content-Type: application/merge-patch+json"; M=https://kubernetes.default.svc/api/v1/namespaces/<namespace>/configmaps/<cm-name>; D=https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<deployment-name>; curl -s -X PATCH --cacert $C -H "Authorization: Bearer $T" -H "$S" -d "{\"data\":{\"<KEY>\":\"<baseline-value>\"}}" $M; curl -s -X PATCH --cacert $C -H "Authorization: Bearer $T" -H "$S" -d "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"kubectl.kubernetes.io/restartedAt\":\"drill-restore\"}}}}}" $D ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （自断授权尾步——标准件第四节立法：**最后一个**恢复 curl（deployment rollout 注解 patch）升 `curl -sf` 并以 `&&` 链自删 DELETE 自己的 Binding（复用 `$C`/`$T`/`$S` 变量；本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`；前序动作保持 `;` 串联不中断），建栈 Role 后按标准件第二节两步建栈法 json-patch 追加独立自删规则（**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行）。fail-open：主恢复未确认成功则授权保留；字节挤不下就不带——完整形态与五纪律以标准件第四节为准）
   （`>/tmp/restore.log 2>&1` 是取证硬要求，禁止改回 `/dev/null`——fire 后带外 `kubectl exec <载体Pod> -- cat /tmp/restore.log` 确认每个恢复 curl 的实际执行情况；三禁一许纪律见标准件第四节。倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）——本用例注入序列无长等待间隔（patch cm 与 rollout restart 均为快命令），武装紧邻注入动作前执行即可；武装后发生任何修复须先停旧定时器再全额重武装——定位进程用 `pkill -f 'kubernetes.default.svc'` 匹配 payload 含 apiserver URL 的定时器 shell（见 SKILL.md 安全红线「故障窗口完整」）。演练结束后按标准件第六节四连删除并带外核实零残留：
   ```bash
   kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete rolebinding drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete role drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
   ```

**演练步骤**：

> **爆炸半径分类（定案）**：`target-only`——被修改的既有资源仅靶 Deployment 自身（CM、载体四件套均为演练新建资产，不触及任何既有非靶资源；新建资产不构成"影响其他资源"，与既有节点 taint 的 cluster-wide 场景不同），勿纠结是否升格。

1. **基线捕获**：记录 ConfigMap 原始键值与当前生效值（定时器与主动恢复均使用）
   ```bash
   kubectl get cm <cm-name> -n <namespace> -o jsonpath={.data.<KEY>}
   kubectl exec <app-pod> -n <namespace> -c <container> -- printenv <KEY>
   ```
2. **先武装定时自恢复，再注入**（主路径 = 资源准备第 3 条的标准件 REST 武装命令——载体内无 kubectl，恢复动作用 SA token 直调 apiserver，紧凑变量形态两条 merge-patch curl。恢复命令幂等：定时器到期自动恢复为主，Agent 在演练结束时主动执行下方 kubectl 兜底命令，定时器迟到重复执行无副作用。载体 Pod 为单副本 `--restart=Never`，定位不到定时器进程即视为异常，中止本次演练改人工恢复）
   倒计时从武装时刻起算：武装与注入必须是紧邻步骤（≤60s）；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f "kubernetes.default.sv[c]"; true'`（见 SKILL.md 安全红线「故障窗口完整」）
3. **注入**：篡改 ConfigMap 键值，再滚动重启使新值生效（env 形式的配置只在 Pod 启动时解析，patch ConfigMap 本身不会影响存量 Pod，必须 rollout restart）
   ```bash
   kubectl patch cm <cm-name> -n <namespace> --type merge -p '{"data":{"<KEY>":"<wrong-value>"}}'
   kubectl rollout restart <workload-kind>/<name> -n <namespace>
   kubectl rollout status <workload-kind>/<name> -n <namespace> --timeout=120s
   ```

**注入验证**：
1. 确认 ConfigMap 已被篡改：
   ```bash
   kubectl get cm <cm-name> -n <namespace> -o jsonpath={.data.<KEY>}
   ```
2. 确认新 Pod 已加载错误值（滚动完成后取最新 Pod）：
   ```bash
   kubectl exec <app-pod> -n <namespace> -c <container> -- printenv <KEY>
   ```
3. （可选，仅当演练方提供了应用访问入口时）确认依赖该配置的业务功能出现预期异常

**持续性检查（必做）**——故障窗口内故障必须持续存活（配置型故障：CM data 里错误值在即故障在，与 taint/nodeSelector 类故障同型）：
以「注入生效确认」为时点锚（注入验证第 1-2 条通过 = 生效：CM data 为错误值 + 新 Pod printenv 为错误值），生效后一次 `time_wait 60`，到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：CM data.<KEY> 仍为错误值（`kubectl get cm <cm-name> -n <namespace> -o jsonpath={.data.<KEY>}`）
2. 行为复查：Pod 内 `printenv <KEY>` 仍为错误值（env 在 Pod 启动时固化，存量 Pod 不会自行重载）
3. 稳定性复查：目标 Pod RESTARTS 计数无增长（排除应用侧 crash 自愈路径；本故障为配置型，无调度类周期事件可查——白盒字段在位即故障在位，事件探针不适用）

**注入恢复**：
1. 等待 `<duration>` 到期，定时器自动还原 ConfigMap 并触发滚动（两条 merge-patch curl：configmap data 还原基线值 + deployment restartedAt 注解触发第二次滚动）；演练提前结束时由 Agent 主动执行同一组恢复命令（幂等，定时器迟到再执行一次无副作用）：
   ```bash
   kubectl patch cm <cm-name> -n <namespace> --type merge -p '{"data":{"<KEY>":"<baseline-value>"}}'
   kubectl rollout restart <workload-kind>/<name> -n <namespace>
   kubectl rollout status <workload-kind>/<name> -n <namespace> --timeout=120s
   ```
   恢复效果核实注意 restore.log 取证（`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`——定时器 fire 的直接证据，两条 PATCH 响应回显）与状态转移证据（CM 回基线 + 新 Pod 加载基线值）互为补充。演练全清（载体四件套四连删除）按标准件第六节，首选 `blade-ai recover --task-id` 带外收尾

**恢复验证**：
1. 确认 ConfigMap 键值已回到基线：
   ```bash
   kubectl get cm <cm-name> -n <namespace> -o jsonpath={.data.<KEY>}
   ```
2. 确认滚动完成后新 Pod 加载的是基线值：
   ```bash
   kubectl exec <app-pod> -n <namespace> -c <container> -- printenv <KEY>
   ```
3. 确认 workload READY = DESIRED，无重启风暴

**基准事实**：
- **根因**：ConfigMap 内容被错误修改（人为误操作/流水线错误），应用滚动后加载了错误配置
- **必现现象**：ConfigMap data 与基线不一致；新 Pod 内生效配置为错误值；Pod 状态本身正常

**注意事项**：
- chaosblade 无 ConfigMap/配置类故障靶点，本用例为 kubectl-native 专属注入
- env 形式的 ConfigMap 消费只在 Pod 启动时解析：只 patch ConfigMap 不 rollout，故障不会显形；同理恢复也必须 patch + rollout 两步，缺一不可
- volume mount 形式的 ConfigMap 消费会被 kubelet 自动同步（分钟级延迟），但多数应用不会热加载挂载文件，是否触发故障取决于应用自身；演练前确认消费形式决定验证方式
- 若目标 ConfigMap 被多个 workload 消费，rollout 范围与影响面需在资源准备阶段逐一确认，避免恢复遗漏
- `kubectl.kubernetes.io/restartedAt` 注解是 rollout restart 的原生副产品（人工执行同样会留），不属演练残留——真正的演练资产是 CM + env 接线 + 载体四件套，收尾以拆这三类为准
- `printenv` 属 busybox/coreutils 生态（alpine/debian 系镜像均有）；目标容器无 printenv 时用普适替代 `cat /proc/1/environ | tr '\0' '\n'` 读环境变量
- 演练自行创建的接线（资源准备第 2 步）属于演练资产，演练结束后应拆除（拆除 env 条目会改变 Pod 模板触发最后一次滚动，拆完 `rollout status` 等待收敛）：
   ```bash
   kubectl set env <workload-kind>/<name> -n <namespace> <KEY>-
   kubectl rollout status <workload-kind>/<name> -n <namespace> --timeout=120s
   kubectl delete cm <cm-name> -n <namespace>
   ```
