---
# 受害者作用域参考（route A，非强制 hint）：本用例受害者是承载 HTTP 服务的 Deployment
# 自身——故障机制 patch 的就是该 Deployment 的 spec.template.spec.containers/0/command
# （把容器入口命令从「快响应器」换成「固定延迟响应器」），受害者域内写、名字匹配放行；
# 节点、kube-proxy、Service/Endpoints、网络 CNI 全程健康，底层 Pod 是「按被改坏的入口命令
# 忠实起一个应用层慢响应器」而非平台/网络故障。供 planner 填 finish_planning 的
# fault_scope 时参照，钉在 deployment 而非降级到 pod（deployment 已注册进 k8s_chaosblade
# family scopes，见 fault_registry.py）。这是「参考」而非「边界」——代码不校验、不覆盖，
# 模型仍须理解真实语义后自行判断。
fault_scope: deployment
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch：containers/0/command 还原为基线快响应器），
# 路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC 不可授/验权
# 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 载体由 faultdrill_assemble_carrier 程序化建栈、结构性自拆（fail-closed 路径 + 恢复句柄清理链 + 武装定时器自删 → apiserver ownerRef GC 级联）；降级路径不自建可自撤 RBAC 栈，正文无需传输载体自撤授权类知识。
carrier_teardown: assembler-managed
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者自身
# （patch Deployment 的 spec.template.spec.containers/0/command 属受害者域内，名字匹配
# 放行），无跨对象写条目——Service/Endpoints 由 K8s 控制器随 rollout 自动更新（不手动写）；
# 装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由工具内程序化
# 构建——构造保证 + fail-closed 内嵌检查（RBAC 从 restorePatches 同源推导禁通配、SA 真实
# token 验权 403 中止+清理），不经 LLM kubectl 写面，无立法条目。
---

**用例名称** 响应延迟 导致 Pod_HTTP故障

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 把 containers/0/command 还原为基线快响应器；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace                             # 把入口命令换成「固定延迟响应器」——与基线快响应器
  path: /spec/template/spec/containers/0/command   # 唯一差异是 SYSTEM 参数多了 `sleep <delay>;`
  value: ["sh","-c","apk add --no-cache socat >/dev/null 2>&1; printf 'HTTP/1.1 200 OK\\r\\nContent-Length: 3\\r\\nConnection: close\\r\\n\\r\\nok\\n' > /tmp/resp; exec socat TCP-LISTEN:<port>,fork,reuseaddr SYSTEM:'sleep <delay-seconds>; cat /tmp/resp'"]
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace                             # 逆 patch 还原为注入前记录的基线 command 数组（快响应器：
  path: /spec/template/spec/containers/0/command   # 同一 socat 形态但 SYSTEM 参数为 'cat /tmp/resp' 无 sleep）
  value: <演练步骤 1 记录的基线 command 数组>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（须覆盖 rollout + 观察窗，宁宽勿窄）
```

- **注入与恢复是同一字段的镜像**：command 是声明式 API mutation，基线快响应器 ↔ 注入慢响应器只差 SYSTEM 参数里的 `sleep <delay-seconds>;` 一段，恢复通道与注入通道同源且幂等；恢复只是让下一次 rollout 用回快响应器命令重建 Pod，秒级确定性幂等操作。
- **响应器命令的引号形态**：value 数组第三个元素是一条 `sh -c` 载荷，内部含单引号（`printf '...'`、`SYSTEM:'...'`）与转义的 `\\r\\n`（printf 的 HTTP 响应分隔符）——落 JSON patch 时按 JSON 字符串规则转义内层双引号即可，`SYSTEM:` 参数**必须**固定为 `sleep <N>; cat /tmp/resp`（见注意事项第三条 corpus 安全红线，绝不可换成 shell）。
- 多容器 Pod 调整 containers/N 索引至承载 HTTP 服务的目标容器；rollout 由 patch 自动触发。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

## 故障现象
1. 对该 Pod / Service 的 HTTP 请求响应显著变慢——**首字节到达时间 `time_starttransfer` 增加约注入的延迟值**（如注入 5s 则 starttransfer 由基线 <100ms 抬到 ≈5s）
2. **TCP 连接建立正常（`time_connect` 保持基线 ≈0）**——延迟发生在应用层「接受连接之后、返回响应之前」，这是与网络层延迟（`Pod_网络延迟_网络请求延迟`，`time_connect` 随之↑）的分界
3. 上游调用方出现读超时（read timeout）、请求堆积、连接/线程池耗尽、重试放大——**此现象条件性成立**：需存在真实上游调用方且其超时预算 < 注入延迟；用裸探针（`curl -m 15` 大于 5s 延迟）测量时探针会正常收到 200 而不超时，故本现象在「无真实调用方拓扑」的演练靶上不会显现，不作为注入生效判据（判据看第 1-2 条的 starttransfer/connect 分离）
4. 若 liveness/readiness 探针配了较短的 `timeoutSeconds`，慢响应可能使探针超时 → Pod 被判不健康重启（本 case 靶不配此类探针，避免与故障判据混淆）

## 资源准备
1. 确认目标 Deployment（承载 HTTP 服务）正常运行、有 Service 暴露、副本就绪，且未被上层 Operator/Helm/ArgoCD 强 reconcile（否则 command 修改会被覆盖回滚）
2. 确认容器镜像能起一个「接受连接后延迟返回」的应用层响应器——Alpine 系镜像（如 `chaosblade-tool:1.8.0`，节点已缓存）`apk add --no-cache socat` 可装 socat，`socat TCP-LISTEN:<port>,fork,reuseaddr SYSTEM:'sleep <N>; cat /tmp/resp'` **每连接 fork 一次 SYSTEM 命令**，sleep 在 accept 时刻起算 → 纯 L7 延迟稳定可复现（busybox nc 循环响应器不可靠，见注意事项第二条，不得用）；镜像须能访问 apk 仓库（rollout 后新 Pod 每次重装 socat）
3. 记录注入前基线（restorePatches 基线值来源）：`spec.template.spec.containers[0].command` 原值、Service 名与端口、目标 label selector
4. 取延迟判据锚点——集群内 curl 该 Service 的基线耗时（正常 `time_starttransfer` <100ms、`time_connect` ≈0）：
   ```bash
   kubectl exec <探测 Pod> -n <namespace> -- curl -s -o /dev/null \
     -w 'connect=%{time_connect} starttransfer=%{time_starttransfer} total=%{time_total}\n' \
     --max-time 10 http://<service-name>.<namespace>.svc.cluster.local:<port>/
   ```
   记下基线（探测 Pod 须自带 curl 且与 Service 同集群网络可达；勿在慢响应靶自身内探测，rollout 期它可能不可用）

## 演练步骤
（主路径 = 步骤 1 基线快照（containers[0].command 原值 + Service/端口 + curl 基线 starttransfer——restorePatches 基线值来源，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、patches=command→固定延迟响应器、restorePatches=command→基线快响应器、duration_seconds=<duration>），注入+武装+readback 工具内同步完成。步骤 2 的手动 patch 序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 定位承载 HTTP 服务的目标 Deployment，完成基线快照：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].command}'
   kubectl get svc -n <namespace> -l <label-selector>
   ```
   Agent 读取并记录原始 command 数组（恢复值以它为参照）与 Service/端口；多容器 Pod 请把 containers 索引对准承载 HTTP 服务的容器
2. **降级路径手动序列**（主路径由装配器承载，此处仅兜底）——把入口命令 patch 成固定延迟响应器，rollout 自动重建 Pod：
   ```bash
   # 注入：command 换成慢响应器（与基线唯一差异 = SYSTEM 参数里多了 sleep <delay-seconds>）
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sh","-c","apk add --no-cache socat >/dev/null 2>&1; printf '\''HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nok\n'\'' > /tmp/resp; exec socat TCP-LISTEN:<port>,fork,reuseaddr SYSTEM:'\''sleep <delay-seconds>; cat /tmp/resp'\''"]}]'
   # 恢复（逆 patch，幂等；command 用步骤 1 记录的基线数组）
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":<基线 command 数组>}]'
   ```
3. 等待 rollout 完成，观察对该 Service 的 HTTP 请求响应变慢（`time_starttransfer` 抬升约注入延迟值，`time_connect` 不变）

## 注入验证
1. 确认 rollout 完成、新 Pod 承载慢响应器：**用 RS 视角判据，不要用 `kubectl rollout status`**——慢响应器的 Pod 可能因就绪探针/启动耗时使 `rollout status` 等待超时报错，按其退出码会把已生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label-selector>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数且 READY 齐
2. **（主证据，必做）** 量化 L7 延迟并与基线对比——**首选**框架内置只读工具 `http_probe`（已在 planning/verify/recover 相位可用，故资源准备第 4 条的注入前延迟基线也走同一 canonical 探针、born-structured 落时间线；按名进只读分类器白名单、不会被只读门误拒；结构化返回 `time_connect`/`time_starttransfer`/`http_code`/`exit_code`/`size_download` 并直接落观测时间线供确定性 cross-check；参数 `origin_pod`＝独立非后端探测 Pod、`url`＝`http://<service-name>.<namespace>.svc.cluster.local:<port>/`、`namespace`、`max_time`＝须 > 注入延迟值以免 curl 提前超时（如注入 5s → 取 15）、`capture` 用默认 `metadata`（`-o /dev/null` 丢弃 body、只取 5 项延迟/状态元数据，本 case 判延迟无需 body/header），「连测 2-3 次」＝独立重复下发同一调用）；**降级形态＝集群内手写 curl**（仅当 `http_probe` 不可用时）：
   ```bash
   kubectl exec <探测 Pod> -n <namespace> -- curl -s -o /dev/null \
     -w 'connect=%{time_connect} starttransfer=%{time_starttransfer} total=%{time_total}\n' \
     --max-time 15 http://<service-name>.<namespace>.svc.cluster.local:<port>/
   ```
   注入生效判据：`time_starttransfer` 比基线高出约注入的延迟值（如注入 5s → starttransfer ≈5s），**而 `time_connect` 保持基线（≈0，TCP 握手未被延迟）**。为排除偶发，连测 2-3 次确认 starttransfer 稳定≈注入延迟（socat 每连接 fork，逐连接一致；若忽快忽慢说明响应器形态选错——见注意事项第二条 nc 竞态）
3. 读取 Deployment 模板确认 command 已替换为慢响应器（机制主证——模板层写入已落地，含 `sleep <delay-seconds>`）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].command}'
   ```
4. **与网络延迟案的分界复核（本 case 专属）**：确认 `time_connect` 未随注入抬升——若 time_connect 也↑约延迟值，说明命中的是网络层延迟（tc netem，`Pod_网络延迟_网络请求延迟` 域）而非本 case 的应用层响应延迟，属打错案

**持续性检查**——故障窗口内故障必须持续存活（配置型故障：慢响应器 command 在 Deployment 模板里，字段在即故障在，每个新 Pod 复制该模板起慢响应器）：
以「注入生效确认」为时点锚（注入验证第 1-3 条通过 = 生效：新 RS 齐 + starttransfer↑而 connect 正常 + 模板 command 含 sleep）——生效确认只证明某一刻故障已落地，不证明它在整个窗口持续存活；持续性复测要把「稳态故障」与「传播收敛窗内的瞬时态」分开：待传播收敛后再次观测仍见故障态才算坐实持续。本故障传播特性：本案为字段型即时生效故障、无传播过程。隔多久复测、复测几次由你依该传播特性与故障形态自行判断（无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复后无法再采集。**时序硬约束**：持续性复测必须在载体 TTL `recovery_deadline`（fire 时刻）**之前留足余量**完成，否则复测会落在逆 patch 已应用之后、只观测到恢复态而非故障存活态（复测若拖到 fire 之后 → 持续性只剩 fire 前单样本、丧失「两个时间分离样本」证据力）。因此 `durationSeconds` 须覆盖「注入 + rollout 重建 + 生效确认 + 复测间隔 + 复测下发」全链并额外留出 fire 前安全余量（宁宽勿窄；窗口过短是持续性证据退化的直接根因）：
1. 白盒复查：慢响应器 command 仍在模板（`kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].command}'` 输出含 `sleep <delay-seconds>`，配置即状态）
2. 行为复查：curl Service 的 `time_starttransfer` 仍≈注入延迟（同注入验证第 2 条探针）

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原 command 基线（restorePatches 的快响应器 command 由载体内 timer 到点执行逆 patch，触发 rollout 用回快响应器；fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下 Agent 主动恢复为降级兜底形态）：
1. **降级路径 = Agent 主动执行逆 patch**（幂等，重复执行无副作用；把 command 还原为注入前基线快响应器）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":<演练步骤 1 记录的基线 command 数组>}]'
   ```
2. 恢复是秒级——逆 patch 触发 rollout 逐副本用回快响应器重建 Pod，无需清理存量对象；json patch 按字段精确替换，天然规避 resourceVersion 乐观锁问题，也不像 apply 三方合并那样保留注入后新增字段

## 恢复验证
1. 执行 `kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].command}'`，确认 command 回到步骤 1 基线快响应器（不再含 `sleep <delay-seconds>`）
2. 集群内 curl 该 Service，确认 `time_starttransfer` 回落到基线水平（不再≈注入延迟；用演练步骤 4 的**同一探针**与当时记录的基线直接对比）：
   ```bash
   kubectl exec <探测 Pod> -n <namespace> -- curl -s -o /dev/null \
     -w 'connect=%{time_connect} starttransfer=%{time_starttransfer} total=%{time_total}\n' \
     --max-time 10 http://<service-name>.<namespace>.svc.cluster.local:<port>/
   ```
3. 确认 rollout 完成、Pod 全部 Running（`kubectl get pods -n <namespace> -l <label-selector>`）；当前代 RS hash 回到注入前基线值（RS hash 是 pod template 全字段相等性的免费校验和——command 还原后 template 与基线逐字段相等 ⇒ hash 必回基线值；hash 不回 = 模板有残留字段，不必猜哪个字段）

## 基准事实
- **根因**：应用层 HTTP 响应变慢——真实场景为下游依赖变慢、GC/锁竞争、线程池/连接池耗尽、慢查询、或（本 case 手段）容器入口命令被改成固定延迟响应器；TCP 层连通正常，慢在「接受连接后、产出响应前」的应用处理阶段
- **必现现象**：curl 该 Service 的 `time_starttransfer` 抬升约注入延迟值而 `time_connect` 保持基线（≈0）；Deployment 模板 command 呈慢响应器（含 `sleep`）；上游出现读超时/请求堆积；恢复后 starttransfer 回基线、command 回快响应器、RS hash 回基线
- **响应器形态**：socat `TCP-LISTEN,fork,reuseaddr` + `SYSTEM:'sleep <N>; cat <file>'` 每连接 fork 一次命令，sleep 在 accept 时刻起算 → time_connect 正常（内核在监听 socket 上完成握手）、time_starttransfer ≈ N（如 connect≈0.0001s / starttransfer≈5.01s，逐连接一致）

## 注意事项
- **与 `Pod_网络延迟_网络请求延迟`（#58）的分界（L7 vs L4，本 case 的核心独立价值）**：#58 是 tc netem **网络层**延迟，延迟链路上所有包 → curl `time_connect` 随之↑（握手包也被延迟）；本 case 是**应用层**响应延迟，TCP 握手正常完成 → `time_connect` 保持基线而 `time_starttransfer`↑。两案判据正交，不得互相套用；注入验证第 4 条即以 time_connect 是否抬升作分界复核
- **响应器形态（选型依据，勿改）**：socat TCP-LISTEN,fork + SYSTEM 每连接 fork 一次延迟命令，sleep 在 accept 时刻起算 → time_starttransfer 稳定≈注入延迟（逐连接一致）。**busybox nc 循环响应器（`( sleep <N>; printf ... ) | nc -l -p <port>`）不可靠、禁用**：sleep 在 while 循环迭代起点就开始跑（客户端连接之前），nc 已监听超过 N 秒后 printf 输出早已缓存在管道里 → 连接即秒回，只有恰好在 sleep 窗口内连接才延迟，延迟呈竞态（starttransfer 在 0.0002s 与 3.38s 间跳变、不一致），无法作为稳定判据
- **SYSTEM 参数的严格立法（corpus 安全红线，不可违背）**：本 case 的 socat `SYSTEM:` 参数**必须**固定为非交互命令 `sleep <N>; cat <固定响应文件>`，**绝不可**是 `/bin/sh`、`bash` 或任何接受 stdin 的交互式 shell——后者是经典 bind-shell（`socat TCP-LISTEN:9100 EXEC:/bin/sh`），是 target_guard 在 **exec/host-command 路径**（`carriers.classify_host_operation`）显式封禁的攻击形态。本 case 之所以能把 socat SYSTEM 写进 Deployment command，是因为它是 **API 平面 patch 的 manifest 体**——守卫的 EXEC:/SYSTEM:/SHELL: 危险扫描只绑定 exec 侧宿主命令（chroot/nsenter 进入的 host_command），不扫 `kubectl patch` 的 JSON patch value（manifest 体不经该分类器，`recoverability` 亦明写「EXEC:/SYSTEM:/SHELL: danger is NOT re-checked here」）。**这是守卫的已知盲点，不等于该形态被判定为安全**：case 层以本条立法把响应器锁死为固定 `sleep+cat`，防止此形态被泛化成往容器里塞交互 shell 的后门。任何把 SYSTEM/EXEC 参数改成 shell、或让它读取 socket 输入的做法都违反本 case 契约
- **HTTP 边界（对标 Chaos Mesh HTTPChaos / Litmus pod-http-latency 的能力边界）**：本形态作用于**明文 HTTP**；不支持 HTTPS/TLS（socat 明文响应器不终结 TLS，TLS 流量需证书/sidecar 面，另案）；注入经 rollout 生效，**存量长连接 / 已建立的 TCP 连接不命中**（只对此后新建连接生效）；与 Istio/Envoy sidecar 注入的服务网格冲突（sidecar 接管流量，chaos-mesh #4389 同类问题），网格环境须另议
- 爆炸半径限于该 Deployment 自身（改的是它自己的入口命令），rollout 逐副本替换；勿选承载真实写操作的 HTTP 服务作靶（慢响应可能触发上游重试放大写压力）；rollout 期新 Pod 每次重装 socat 依赖 apk 仓库可达，仓库不可达时新 Pod 起不来（属环境依赖，须在选靶时确认）
