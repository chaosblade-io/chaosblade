---
# 受害者作用域参考（route A，非强制 hint）：本用例受害者是 Secret 自身——故障
# 机制 patch 的是该 Secret 的 data 键值（受害者域内写，名字匹配放行）。供 planner
# 填 finish_planning 的 fault_scope 时参照，把它钉在 secret 而非降级到
# workload/service：降级会使 approved.scope ≠ effective.scope=secret，被 drift
# 守卫结构性拒绝（secret 与 configmap/ingress 同注册进 k8s_chaosblade family
# 词表，scope==kind 走名字匹配放行）。这是「参考」而非「边界」——代码不校验、
# 不覆盖（无 enforcement）。
# 故障态定位（本用例的建模基准）：这是「配置对象内容错误」型故障——故障在 Secret
# 对象自身即完整成立（data 键值被篡改为错误凭证），白盒读对象即可确定取证，不依赖
# 任何消费方的状态。消费方是否加载到错误凭证属「传播 / 影响」，是下游后果、不是本
# 故障的门禁判据；且向消费方（Deployment 等）做 mutation（如 rollout restart）会
# 越出 secret 批准靶域被 drift 守卫拒，故注入期一律不写消费方。需要传播佐证时只走
# 「非 mutation 的 in-scope 通道」（volume 挂载由 kubelet 自动同步 + 只读读取）。
fault_scope: secret
# 恢复通道路由声明：本 case 恢复动作住址 = apiserver 写（逆 patch 还原 Secret
# 凭证值），路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次
# 调用：建栈+验权+武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/
# 节点不容纳/RBAC 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只写受害者自身
# （patch Secret 的 data 键值属受害者域内，名字匹配放行），恢复也只逆 patch 还原
# 同一 Secret（无 rollout、无跨对象写），故无跨对象写条目；装配器载体栈
# （SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由工具内程序化构建
# ——构造保证 + fail-closed 内嵌检查（RBAC 从 restorePatches 同源推导禁通配、
# SA 真实 token 验权 403 中止+清理），不经 LLM kubectl 写面，无立法条目。
---

**用例名称** 凭证被篡改 导致 Secret_内容错误

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 Secret 凭证值；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Secret
  name: <secret-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch；/data/<key> 值须 base64——用 <faultdrill:b64> 哨兵声明明文，装配器确定性编码，免 LLM 手算，见注意事项「base64 编码」）
- op: replace
  path: /data/<目标凭证键>
  value: <faultdrill:b64><篡改后凭证明文>   # 哨兵：装配器 §0 纯 Python base64 编码后就地回填（inject/landing/receipt 全看解析值）；填明文即可，绝不手算 base64
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace                             # value: null = 工具运行时活体捕获注入前基线 base64（字节精确、免 LLM 转录错），优于显式填值
  path: /data/<目标凭证键>
  value: null
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 本用例是「配置对象内容错误」型故障：故障态 = Secret 的 data 键值被篡改，patch 落地即故障成立、白盒可证，**无需任何传播动作**（不 rollout、不改消费方）。恢复域只逆 patch 还原同一 Secret 的 data，无跨对象动作。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。

## 故障现象
1. Secret 的 data 凭证键被篡改为错误值（白盒：`kubectl get secret -o jsonpath` 读到的 base64 解码后 ≠ 基线凭证）——这是故障本体，patch 落地即成立，与是否有消费方无关
2. 若存在消费该 Secret 的应用，其**后续**行为随消费形态而异（属下游传播/影响，非本故障门禁判据）：
   - volume 挂载消费：kubelet 分钟级把新值同步进挂载文件，应用若热加载则读到错误凭证
   - env（secretKeyRef）消费：只在 Pod 启动时解析，存量 Pod 不受影响，须重启才加载新值——而重启消费方属越界 mutation，不在本用例注入动作内
   - 真实认证下游：错误凭证可能引发认证失败（401/403、启动即崩 CrashLoopBackOff），但需存在真实认证链路才可见；合成演练靶通常无此下游，故认证失败不作为判据
3. 与资源类故障的关键区别：Pod/节点状态本身正常，故障藏在 Secret 的内容语义里，白盒读对象即确定

## 资源准备
1. 受害靶 = 目标 Secret 本身。集群无合适既有靶时，可按**专用靶自建标准件**（`references/target/drill-target.md`）带内自建一个合成凭证 Secret，靶名锚定 FaultSpec 批准名（**演练一律用合成口令**，见注意事项「安全红线」）：
   ```bash
   kubectl create secret generic <secret-name> -n <namespace> --from-literal=<KEY>=<baseline-credential>
   kubectl get secret <secret-name> -n <namespace> -o jsonpath={.data.<KEY>}
   ```
2. （可选，仅用于非门禁的传播佐证）若演练方希望额外观察「消费方读到错误凭证」，可**预先**准备一个 volume 挂载消费该 Secret 的应用（volume 由 kubelet 自动同步，注入期无需 mutation 消费方、只读即可观测）。**本用例不要求、也不创建消费方**——门禁判据只落在 Secret 自身（见注入验证第 1 条）；env 形态消费方不作为验证路径（须重启消费方才显形，而重启越出 secret 批准靶域）。
3. 按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈——四对象同名 `drill-rc-<hash>`、全部建在被批准的靶点命名空间内（漂移守卫以二级范围+命名空间锚定放行；建在其他命名空间会被判 scope/namespace drift 拦截），SA/Role/RoleBinding 必须用 `kubectl create` 命令构造（`apply -f` 因清单内容对守卫不可见被设计性拦截）。本用例恢复对象只有靶 Secret 一类（namespaced），Role 动词集 `get,patch` 单资源即可：
   ```bash
   kubectl create serviceaccount drill-rc-<hash> -n <namespace>
   kubectl create role drill-rc-<hash> -n <namespace> --verb=get,patch --resource=secrets
   kubectl create rolebinding drill-rc-<hash> -n <namespace> --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=curlimages/curl:latest --restart=Never --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   ```
   （镜像按标准件第九节判别法选定——受限网络（VPC 无法拉 docker.io）时选节点已缓存且含 curl/sh/sleep 工具链的镜像：候选清单 ∩ 实证档案非空取第一个命中即定案；SA/Role/RoleBinding 须先于载体 Pod 创建：overrides 挂接的 Pod 引用该 SA，SA 不存在时 Pod 卡 ContainerCreating。定名后按标准件第九节做精确同名冲突预检：`kubectl get sa,role,rolebinding,pod drill-rc-<hash> -n <namespace> -o name`——NotFound 即无冲突。）
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节，含写动词 SSAR 对账——Role verbs 以恢复脚本实际载荷动词为准；验权清单：secret GET 200 且 secret PATCH 可授，任一 403 即中止）。定时器用载体内 SA token 直调 apiserver REST，单条 merge-patch curl 还原 Secret data 即完成全部恢复（本用例无 rollout 第二步）：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); S="Content-Type: application/merge-patch+json"; M=https://kubernetes.default.svc/api/v1/namespaces/<namespace>/secrets/<secret-name>; curl -sf -X PATCH --cacert $C -H "Authorization: Bearer $T" -H "$S" -d "{\"data\":{\"<KEY>\":\"<baseline-base64>\"}}" $M && curl -sf -X DELETE --cacert $C -H "Authorization: Bearer $T" https://kubernetes.default.svc/apis/rbac.authorization.k8s.io/v1/namespaces/<namespace>/rolebindings/drill-rc-<hash> ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （Secret 的 `data.<KEY>` 值是 **base64**——还原载荷里的 `<baseline-base64>` 必须是注入前记录的基线值的 base64 编码，不是明文；见注意事项「base64 编码」。**禁用 `kubectl get secret -o yaml > backup` + `apply` 还原**——备份固化的 resourceVersion 与集群现状不一致会报 Conflict 失败，故障无法恢复；还原只走 patch 基线值路径。）
   （自断授权尾步——标准件第四节立法：**最后一个**恢复 curl（本案即 Secret data 还原，其后无其他恢复动作）升 `curl -sf` 并以 `&&` 链自删 DELETE 自己的 Binding（复用 `$C`/`$T` 变量；本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`），建栈 Role 后按标准件第二节两步建栈法 json-patch 追加独立自删规则（**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行）。fail-open：主恢复未确认成功则授权保留；字节挤不下就不带——完整形态与五纪律以标准件第四节为准）
   （`>/tmp/restore.log 2>&1` 是取证硬要求，禁止改回 `/dev/null`——fire 后带外 `kubectl exec <载体Pod> -- cat /tmp/restore.log` 确认恢复 curl 的实际执行情况；三禁一许纪律见标准件第四节。倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）——本用例注入序列无长等待间隔（patch secret 为快命令），武装紧邻注入动作前执行即可；武装后发生任何修复须先停旧定时器再全额重武装——定位进程用 `pkill -f 'kubernetes.default.svc'` 匹配 payload 含 apiserver URL 的定时器 shell（见 SKILL.md 安全红线「故障窗口完整」）。演练结束后按标准件第六节四连删除并带外核实零残留：
   ```bash
   kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete rolebinding drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete role drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
   ```

## 演练步骤
（主路径 = 基线捕获（步骤 1）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Secret、patches=data 键篡改、restorePatches=基线还原 value:null、duration_seconds=<duration>），Secret 注入+武装+readback 工具内同步完成，patch 落地即故障成立；步骤 2-3 的手动序列仅当装配器 fail-closed 报告不可用时作降级兜底。**本用例无传播步骤**——不 rollout、不改任何消费方）：

> **爆炸半径分类（定案，defer recovery-carrier §9）**：声明 `blast_radius_scope=namespace-wide`——本 case 走 apiserver-write 四件套 carrier（Pod/SA/Role/RoleBinding），新增 namespaced RBAC 授权面，按 §9①「四件套 → namespace-wide」定案免推导，勿在 target-only↔namespace-wide 间重复仲裁。故障本体写入集仅靶 Secret 自身（target-only 语义，被修改的既有资源仅此一个），此事实写进 `blast_radius_detail`；凭证类下游影响面（哪些应用因认证失败受影响）由**谁消费该 Secret**决定，资源准备阶段须逐一枚举消费方用于爆炸半径评估，但注入/恢复动作只作用于 Secret 本身、不触碰消费方。

1. **基线捕获**：记录 Secret 原始 data 值（base64）与解码后明文（restorePatches 的基线值来源；主路径下装配器 `value:null` 活体捕获，降级路径下据此填还原载荷）
   ```bash
   kubectl get secret <secret-name> -n <namespace> -o jsonpath={.data.<KEY>}          # base64 基线（还原载荷直接用此值）
   kubectl get secret <secret-name> -n <namespace> -o jsonpath={.data.<KEY>} | base64 -d   # 解码核对明文
   ```
2. **先武装定时自恢复，再注入**（主路径 = 资源准备第 3 条的标准件 REST 武装命令——载体内无 kubectl，恢复动作用 SA token 直调 apiserver，单条 merge-patch curl 还原 Secret data。恢复命令幂等：定时器到期自动恢复为主，Agent 在演练结束时主动执行下方 kubectl 兜底命令，定时器迟到重复执行无副作用。载体 Pod 为单副本 `--restart=Never`，定位不到定时器进程即视为异常，中止本次演练改人工恢复）
   倒计时从武装时刻起算：武装与注入必须是紧邻步骤（≤60s）；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f "kubernetes.default.sv[c]"; true'`（见 SKILL.md 安全红线「故障窗口完整」）
3. **注入**：篡改 Secret 凭证键值——patch 落地即故障成立（无需 rollout、无需消费方加载）
   ```bash
   # 明文篡改（stringData 由 apiserver 自动 base64 编码，最省事、免手算 base64）：
   kubectl patch secret <secret-name> -n <namespace> --type merge -p '{"stringData":{"<KEY>":"<wrong-credential>"}}'
   # 或 data 域直接写 base64（须先 echo -n '<wrong-credential>' | base64）：
   # kubectl patch secret <secret-name> -n <namespace> --type merge -p '{"data":{"<KEY>":"<base64-of-wrong>"}}'
   ```
   （注入优先用 `stringData` 明文入口——apiserver 自动 base64 编码，规避 LLM 手算 base64 出错；readback/还原走 `data` 域，见注意事项「base64 编码」）

## 注入验证
1. **（门禁主证·白盒）确认 Secret data 已被篡改**——这是本故障完整、确定、in-scope 的判据，通过即注入成功：
   ```bash
   kubectl get secret <secret-name> -n <namespace> -o jsonpath={.data.<KEY>}
   kubectl get secret <secret-name> -n <namespace> -o jsonpath={.data.<KEY>} | base64 -d   # 解码后为错误凭证
   ```
   （主路径：装配器 landing readback 已确认 patch 落地、receipt `patches[].value` 携带程序解析的权威 base64——验证相直接比对 live `data.<KEY>` == receipt 值即可；门禁判据用**明文**表述「解码后 == <篡改后凭证明文>」，不手算 base64、不手工解码。上方 `| base64 -d` 仅降级 kubectl 手动路径适用，且 `kubectl_read` 结构化 argv 不支持管道，该解码须走 debug pod/主机）
2. **（可选·非门禁佐证）volume 挂载消费方读到错误凭证**——仅当资源准备第 2 步预置了 volume 消费方时适用；kubelet 分钟级同步后**只读**读取挂载文件（读消费方是观测、in-bounds，不改消费方）：
   ```bash
   kubectl exec <consumer-pod> -n <namespace> -c <container> -- cat <mount-path>/<KEY>
   ```
   （无 volume 消费方、或消费方为 env 形态时，本条标 `expected`/N-A 跳过——env 须重启消费方才显形，而重启越出 secret 批准靶域；**不得为凑此证据去 rollout 消费方**）
3. **（非门禁）真实认证下游失败**：仅当存在真实认证链路时可观察（401/403、CrashLoopBackOff）；合成演练靶通常无此下游，标 `expected` 跳过，勿为找证据反复更换查询方式

**持续性检查**——故障窗口内故障必须持续存活（配置对象内容型故障：Secret data 里错误凭证在即故障在）：
以「注入生效确认」为时点锚（注入验证第 1 条通过 = 生效：Secret data 为错误 base64）——生效确认只证明某一刻故障已落地，不证明它在整个窗口持续存活；持续性复测要把「稳态故障」与「传播收敛窗内的瞬时态」分开：待传播收敛后再次观测仍见故障态才算坐实持续。本故障传播特性：本案为字段型即时生效故障，无传播过程。隔多久复测、复测几次由你依该传播特性与故障形态自行判断（无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：Secret data.<KEY> 仍为错误 base64（`kubectl get secret <secret-name> -n <namespace> -o jsonpath={.data.<KEY>}`）
2. （若有 volume 消费方）只读复查挂载文件仍为错误凭证
3. 稳定性复查：靶 Secret 未被外部控制器轮换/还原（错误值在位、resourceVersion 未因第三方 reconcile 回落）——白盒字段在位即故障持续

## 注入恢复
（主路径下 Secret 还原无需 Agent 执行动作——载体 TTL 自治逆 patch 还原 Secret data（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。**本用例恢复只作用于 Secret 自身**，无 rollout、无消费方动作。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动还原 Secret data（单条 merge-patch curl）；演练提前结束时由 Agent 主动执行同一还原命令（幂等，定时器迟到再执行一次无副作用）：
   ```bash
   kubectl patch secret <secret-name> -n <namespace> --type merge -p '{"data":{"<KEY>":"<baseline-base64>"}}'
   ```
   （还原载荷用注入前记录的**基线 base64 值**，不是明文；勿用 `get -o yaml` 备份 + `apply` 还原——resourceVersion 冲突会失败。恢复效果核实注意 restore.log 取证（`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`——定时器 fire 的直接证据，PATCH 响应回显）与状态转移证据（Secret data 回基线）互为补充。演练全清（载体四件套四连删除）按标准件第六节，首选 `blade-ai recover --task-id` 带外收尾）

## 恢复验证
1. **（门禁主证·白盒）确认 Secret 键值已回到基线**（data base64 与注入前一致，解码后为原凭证）：
   ```bash
   kubectl get secret <secret-name> -n <namespace> -o jsonpath={.data.<KEY>}
   kubectl get secret <secret-name> -n <namespace> -o jsonpath={.data.<KEY>} | base64 -d
   ```
2. （可选·非门禁）若有 volume 消费方，kubelet 同步后只读复查挂载文件回基线凭证
3. 载体四件套已按标准件第六节四连删除、带外核实零残留

## 基准事实
- **根因**：Secret 凭证内容被错误修改（人为误操作/流水线错误/凭证轮换失误），对象的 data 键值偏离基线
- **必现现象**：Secret data 与基线不一致（base64 已变、解码为错误凭证）——白盒读对象即确定成立，与消费方无关
- **随传播而变的下游现象（非门禁）**：
  - volume 挂载消费：kubelet 分钟级同步后挂载文件为错误值（只读可观测）
  - env 消费：须重启消费方才加载错误值（重启属越界 mutation，不在本用例动作内，不作验证路径）
  - 真实认证下游：错误凭证引发认证失败（401/403、CrashLoopBackOff），需真实认证链路才可见
- **作用域边界**：仅靶 Secret 自身被修改；影响面 = 消费该 Secret 的应用集合（资源准备阶段枚举用于爆炸半径评估，但注入/恢复不触碰它们）

## 注意事项
- chaosblade 无 Secret/凭证类故障靶点，本用例为 kubectl-native 专属注入
- **故障态在对象自身即完整（本用例建模基准）**：这是「配置对象内容错误」型故障——patch 落地、Secret data 偏离基线，故障即完整成立且白盒可确定取证，**不依赖消费方加载**。门禁判据只钉在 Secret data 上；消费方行为是下游传播/影响，一律非门禁。
- **传播只能走非 mutation 的 in-scope 通道**：配置内容故障要显形到消费方，靠的是消费方自身的热同步/热加载（volume 挂载由 kubelet 分钟级自动同步、应用 reload 类插件热加载），**不是靠改消费方**。env（secretKeyRef）形态只在 Pod 启动时解析，须重启消费方才显形——而重启（rollout restart）是对 Deployment 的 mutation，越出 secret 批准靶域，故 env 形态**不作为本用例的验证路径**；需要传播佐证时用 volume 挂载消费方（只读观测 in-bounds）。
- **想演示「应用被错误凭证搞崩」是另一种故障**：那类故障的受害靶是**应用**（scope=deployment/pod）、门禁判据是应用行为（认证失败/CrashLoopBackOff），重启应用在其自身 scope 内合法——它与本用例（受害靶=Secret、白盒判据、不碰消费方）是两个不同的故障，不要在本用例里靠 mutation 消费方去凑行为证据。
- **base64 编码（主路径免手算）**：Secret 的 `.data.<key>` 值是 base64 编码（`.stringData.<key>` 是明文写入口，apiserver 写入时自动转 base64、读时不回填 stringData）。**主路径（装配器）注入用 `<faultdrill:b64><明文>` 哨兵声明篡改值**——装配器在建栈前（§0）纯 Python `base64.b64encode` 编码后就地回填 patch，inject/landing readback/receipt 全看解析值，故 base64 常数由程序派生、绝不经 LLM 手算（手算既慢，又会把错误常数写进 plan 门禁，且 landing readback 只验「patch 落地」不验「合意图」，错值照样通过 → false-negative 隐患）。**还原/restorePatches 走 `data` 域必须用 base64**（json-patch readback 读的是 `data`，`stringData` 不持久化）——装配器 restorePatches 用 `value: null` 活体捕获基线 `data` 值字节精确，同属「确定性字节不过 LLM 手」原则。降级路径（正文 kubectl 手动注入）无哨兵机制：注入优先用 `stringData` 明文入口（apiserver 自动编码），仅 readback/还原走 `data` 域时才 `echo -n <明文> | base64` 现算
- **禁用备份 apply 还原**：`kubectl get secret -o yaml > backup` + `kubectl apply -f backup` 会因备份固化的 resourceVersion 与集群现状不一致报 Conflict 失败，导致故障无法恢复；还原只走「注入前提取基线值 + patch 还原」路径
- **安全红线**：演练一律用**合成凭证**（自建 drill Secret + 合成口令），严禁篡改真实生产凭证（DB 密码/云 AK-SK/registry token）——真实凭证被篡改即使可恢复，故障窗内的认证失败也会波及真实业务，且凭证值落进任务台账/日志构成泄露面
- **边界（与其他凭证形态区分）**：本用例聚焦**应用消费型 Opaque 凭证**（DB 密码/API token/下游服务口令）被篡改导致的内容错误；镜像仓库拉取凭证（`kubernetes.io/dockerconfigjson` 型 Secret）缺失/过期导致 `ImagePullBackOff` 属**镜像拉取失败域**，TLS 证书型 Secret（`kubernetes.io/tls`）过期属**证书形态域**，均不在本用例范围——三者的判据（内容白盒 vs 拉取失败 vs TLS 握手失败）与恢复路径各不相同
- 若目标 Secret 被多个 workload 消费，影响面需在资源准备阶段逐一枚举用于爆炸半径评估（但注入/恢复只作用于 Secret，不逐个改消费方）
- 演练自行创建的合成 Secret（资源准备第 1 步）属于演练资产，演练结束后应删除（`kubectl.kubernetes.io/restartedAt` 一类注解本用例不产生——无 rollout；真正的演练资产是 Secret + 载体四件套，收尾以拆这两类为准）：
   ```bash
   kubectl delete secret <secret-name> -n <namespace> --ignore-not-found
   ```
- `printenv`/`cat` 属 busybox/coreutils 生态（alpine/debian 系镜像均有）；若用 volume 消费方佐证，读挂载文件用 `cat <mount-path>/<KEY>`（只读，in-bounds）
