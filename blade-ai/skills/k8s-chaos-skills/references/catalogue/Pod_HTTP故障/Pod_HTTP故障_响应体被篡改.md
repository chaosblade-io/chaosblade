---
# 受害者作用域参考（route A，非强制 hint）：本用例受害者是承载 HTTP 服务的 Deployment
# 自身——故障机制 patch 的就是该 Deployment 的 spec.template.spec.containers/0/command
# （把容器入口命令从「快响应器」换成「返回被篡改响应体的响应器」：TCP 握手、HTTP 状态行、
# 报文框架全部正常，状态码仍是 200 OK，唯独响应体 body 内容被改成错误数据），受害者域内写、
# 名字匹配放行；节点、kube-proxy、Service/Endpoints、网络 CNI 全程健康，底层 Pod 是「按被
# 改坏的入口命令忠实起一个应用层返回篡改响应体的响应器」而非平台/网络故障。供 planner 填
# finish_planning 的 fault_scope 时参照，钉在 deployment 而非降级到 pod（deployment 已
# 注册进 k8s_chaosblade family scopes，见 fault_registry.py）。这是「参考」而非「边界」
# ——代码不校验、不覆盖，模型仍须理解真实语义后自行判断。
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

**用例名称** 响应体被篡改 导致 Pod_HTTP故障

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 把 containers/0/command 还原为基线快响应器；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace                             # 把入口命令换成「返回被篡改响应体的响应器」——与基线
  path: /spec/template/spec/containers/0/command   # 快响应器唯一差异是 printf 写入 /tmp/resp 的 body 段：状态行仍是 200 OK，body 从基线数据换成篡改数据
  value: ["sh","-c","apk add --no-cache socat >/dev/null 2>&1; printf 'HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\nContent-Length: <篡改body字节数>\\r\\nConnection: close\\r\\n\\r\\n<被篡改的响应体>' > /tmp/resp; exec socat TCP-LISTEN:<port>,fork,reuseaddr SYSTEM:'cat /tmp/resp'"]
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace                             # 逆 patch 还原为注入前记录的基线 command 数组（快响应器：
  path: /spec/template/spec/containers/0/command   # socat SYSTEM:'cat /tmp/resp' 返回 200 OK + 基线 body）
  value: <演练步骤 1 记录的基线 command 数组>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（须覆盖 rollout + 观察窗，宁宽勿窄）
```

- **注入与恢复是同一字段的镜像**：command 是声明式 API mutation，基线快响应器 ↔ 注入篡改响应器只差 printf 写进 `/tmp/resp` 的 **body 段**（状态行 `HTTP/1.1 200 OK` 两者相同，唯 body 从 `<基线响应体>` 换成 `<被篡改的响应体>`），socat 形态与 `SYSTEM:'cat /tmp/resp'` 完全相同，恢复通道与注入通道同源且幂等；恢复只是让下一次 rollout 用回快响应器命令重建 Pod，秒级确定性幂等操作。
- **篡改响应体是参数化的（本 case 与 1.5 的分水岭）**：本 case 篡改的是 **HTTP 响应体 body 内容，状态码保持 `200 OK` 不变**——这是它与同域 `Pod_HTTP故障_状态码被篡改`（1.5，改状态行、body 空）的根本区别。规范示例：基线返回 `{"status":"ok","data":"<基线值>"}`，篡改后返回 `{"status":"ok","data":"<篡改值>"}`（状态码 200、JSON 结构完好，唯 data 字段值被改）；同一 printf body 替换也可构造任意「状态码正常但内容错误」的响应体（返回陈旧缓存、损坏/截断的数据、错误的配置或数值），判据形态一致（`http_code=200`、curl exit 0、connect/starttransfer 正常，**body 内容 ≠ 基线**），选靶与判据锚定实际注入的那一段篡改 body 即可。
- **`Content-Length` 必须等于篡改 body 的真实字节数（本 case 专属技术红线）**：printf 写入的状态行 `Content-Length: <N>` 中的 `<N>` **必须**精确等于 `\\r\\n\\r\\n` 之后 body 段的字节数——不一致会使 curl 报 `transfer closed with N bytes remaining`（声明长度 > 实际）或提前截断（声明 < 实际），污染判据（把「body 被篡改」误判成「传输层出错」）。篡改 body 定稿后须重新数准字节数回填 `<N>`；body 含多字节字符时按 UTF-8 字节数计（非字符数）。
- **响应器命令的引号形态**：value 数组第三个元素是一条 `sh -c` 载荷，内部含单引号（`printf '...'`、`SYSTEM:'...'`）与转义的 `\\r\\n`（printf 的 HTTP 响应分隔符）——落 JSON patch 时按 JSON 字符串规则转义内层双引号即可（body 若是 JSON，其内层双引号在 printf 单引号包裹下无需再转义，但整个 value 落 JSON patch 时要按 JSON 转义），`SYSTEM:` 参数**必须**固定为 `cat /tmp/resp`（见注意事项第三条 corpus 安全红线，绝不可换成 shell）。
- 多容器 Pod 调整 containers/N 索引至承载 HTTP 服务的目标容器；rollout 由 patch 自动触发。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

## 故障现象
1. 对该 Pod / Service 的 HTTP 请求——**TCP 握手正常、HTTP 状态行仍是 `200 OK`、报文框架完整，但服务端返回的响应体 body 内容被篡改**：集群内 curl 报 **`http_code=200`（状态码正常）、curl exit 0**，`time_connect` 与 `time_starttransfer` 均保持基线（≈0，无延迟、无拆连接），**唯独响应 body 的数据与基线不一致**（返回了错误/陈旧/损坏的内容）
2. **「码对但内容错」是与相邻形态的分界**——状态码被篡改（1.5，http_code=503、body 空）是状态行错；连接被重置（1.4，curl exit 52 / http_code 000）是零响应；响应延迟（1.3，http_code 200 / starttransfer↑ / body 对）是慢但对；本 case 是**即时返回一个状态码正常、协议完整、但 body 数据被篡改的响应**（http_code 200 / exit 0 / connect·starttransfer 正常 / **body ≠ 基线**），四者签名正交（见注入验证第 4 条）
3. 上游调用方拿到错误数据却判请求成功（状态码 200）——错误数据被当真值消费：缓存被污染、下游按错误数值决策、展示层显示错误内容、对账/校验逻辑被绕过。**此现象条件性成立**：需存在真实上游调用方消费该 body；用裸探针（curl）测量时探针直接读到被篡改的 body 内容，故本现象在「无真实调用方拓扑」的演练靶上表现为探针读到错误 body 而非上游数据污染，不作为注入生效判据（判据看第 1-2 条的 http_code=200/exit0 + body≠基线 分离）
4. **就绪探针类型决定 Pod 是否被摘流量（本 case 关键环境事实）**：若 readiness 探针是 `tcpSocket`（本域常驻靶即此形态）或 `httpGet` 只校验状态码（200 即判就绪、不校验 body 内容），则返回篡改 body 的 Pod 仍判 Ready、仍留在 Service Endpoints 里持续供错数据——k8s 平台层完全看不见这个应用层数据错误（状态码是 200）；只有当 readiness 是 `httpGet` 且其 body 内容校验逻辑恰好命中被篡改字段时才会摘流量（罕见）。选靶与判读须先确认靶的就绪探针类型

## 资源准备
1. 确认目标 Deployment（承载 HTTP 服务）正常运行、有 Service 暴露、副本就绪，且未被上层 Operator/Helm/ArgoCD 强 reconcile（否则 command 修改会被覆盖回滚）
2. 确认容器镜像能起一个「接受连接后返回固定 HTTP 响应」的应用层响应器——Alpine 系镜像（如 `chaosblade-tool:1.8.0`，节点已缓存）`apk add --no-cache socat` 可装 socat，`printf '<HTTP 响应字节>' > /tmp/resp; socat TCP-LISTEN:<port>,fork,reuseaddr SYSTEM:'cat /tmp/resp'` **每连接 fork 一次、把 /tmp/resp 原样回给客户端** → 响应体完全由 printf 写入的 body 段决定（把基线 body 换成篡改 body、状态行保持 200 OK 即得响应体篡改响应器）；镜像须能访问 apk 仓库（rollout 后新 Pod 每次重装 socat）——**探测 apk 可达性勿用裸 curl 默认 UA 下结论**：部分镜像站会对非 apk UA 施 WAF 过滤，`curl <repo>/APKINDEX.tar.gz` 默认 UA 返回 **403** 而带 `-A 'apk-tools/2.14.6'`（apk 自身 UA）返回 **200**、目录列表亦 200 ⇒ 403 ≠ 仓库不可达，`apk add` 自带 apk-tools UA 故实际可装；判定 apk 是否真不可达须用 apk UA 复测或看目录列表，勿据默认 UA 的 403 误判机制不可行而擅自降级响应器形态
3. 记录注入前基线（restorePatches 基线值来源）：`spec.template.spec.containers[0].command` 原值、Service 名与端口、目标 label selector、就绪探针类型（tcpSocket / httpGet）、**以及基线响应体 body 的确切内容**（本 case 判据核心——篡改前后 body 比对的参照，须逐字节记录基线 body，不能只记状态码）
4. 取判据锚点——用框架内置只读探针 `http_probe` 读该 Service 的基线（正常返回 `http_code=200`、`exit_code=0`、`time_connect`≈0、`time_starttransfer`<100ms、**body = 基线响应体内容**）：
   ```
   http_probe(origin_pod=<独立非后端探测 Pod>, url=http://<service-name>.<namespace>.svc.cluster.local:<port>/, namespace=<namespace>, max_time=10, capture=body)
   ```
   **`capture=body` 是本 case 的关键参数、不可省**——`http_probe` 默认 `capture=metadata` 会 `-o /dev/null` 丢弃响应体、拿不到 body（本 case 判据核心即在此）；只有 `capture=body` 才去掉丢弃汇让 body 直出（哨兵行 `===HTTPPROBE_META===` 与 `-w` 元数据分隔），框架把 body 落 **`body` exact-key** 进观测时间线供 cross-check 精确查表。记下基线（**`body` 键内容 + 元数据**；探测 Pod 须与 Service 同集群网络可达；**勿从后端 Pod 自身探测**——kube-proxy hairpin 会使后端 Pod 探测自身 Service ClusterIP 超时得到 exit_code=28/http_code=000 假象，探测点必须是独立非后端 Pod）。**本 case 探针与 1.5/1.7 的关键差异：`capture=body` 读响应体**——1.5 只判状态码用默认 `capture=metadata`（丢 body 与 header）、1.7 判 header 用 `capture=headers`（丢 body、结构化 dump 响应头），本 case 判 body 篡改故用 `capture=body`（body 直出供判读）。**降级形态（仅当 `http_probe` 不可用）＝手写 plain curl** `kubectl exec <探测 Pod> -- curl -s -w '\n---\nhttp=%{http_code} exit=%{exitcode}\n' --max-time 10 http://...`（去掉 `-o /dev/null` 让 body 直出 stdout；详见注意事项第五条 verify 相位探针形态约束）

## 演练步骤
（主路径 = 步骤 1 基线快照（containers[0].command 原值 + Service/端口 + 就绪探针类型 + curl 基线 http=200/exit0 + **基线 body 内容**——restorePatches 基线值来源，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、patches=command→篡改响应体响应器、restorePatches=command→基线快响应器、duration_seconds=<duration>），注入+武装+readback 工具内同步完成。步骤 2 的手动 patch 序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 定位承载 HTTP 服务的目标 Deployment，完成基线快照：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].command}'
   kubectl get svc -n <namespace> -l <label-selector>
   ```
   Agent 读取并记录原始 command 数组（恢复值以它为参照）、Service/端口、就绪探针类型、**以及 curl 基线 body 内容**（判据参照）；多容器 Pod 请把 containers 索引对准承载 HTTP 服务的容器
2. **降级路径手动序列**（主路径由装配器承载，此处仅兜底）——把入口命令 patch 成篡改响应体响应器（状态行保持 200 OK、只换 body 段），rollout 自动重建 Pod：
   ```bash
   # 注入：command 换成篡改响应体响应器（与基线唯一差异 = printf 写入的 body 段从基线数据换成篡改数据，状态行仍 200 OK）
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sh","-c","apk add --no-cache socat >/dev/null 2>&1; printf '\''HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: <篡改body字节数>\r\nConnection: close\r\n\r\n<被篡改的响应体>'\'' > /tmp/resp; exec socat TCP-LISTEN:<port>,fork,reuseaddr SYSTEM:'\''cat /tmp/resp'\''"]}]'
   # 恢复（逆 patch，幂等；command 用步骤 1 记录的基线数组）
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":<基线 command 数组>}]'
   ```
3. 等待 rollout 完成，观察对该 Service 的 HTTP 请求返回状态码正常（`http_code=200`、curl exit 0）但 **body 内容被篡改**（≠ 基线响应体）

## 注入验证
1. 确认 rollout 完成、新 Pod 承载篡改响应体响应器：**用 RS 视角判据，不要用 `kubectl rollout status`**——响应器 Pod 可能因就绪探针/启动耗时使 `rollout status` 等待超时报错，按其退出码会把已生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label-selector>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数且 READY 齐
2. **（主证据，必做）** 用 `http_probe(capture=body)` 读该 Service 响应体，量化「状态码正常 + body 被篡改」并与基线对比：
   ```
   http_probe(origin_pod=<独立非后端探测 Pod>, url=http://<service-name>.<namespace>.svc.cluster.local:<port>/, namespace=<namespace>, max_time=10, capture=body)
   ```
   注入生效判据：**`http_code=200`（状态码保持正常，未被篡改）、`exit_code=0`、`time_connect`/`time_starttransfer` 保持基线，而框架落进时间线的 `body` 键 = 注入的篡改值、≠ 资源准备第 4 条记录的基线 body**。**metric_claim 必须引这个 exact-key（`body=<篡改值>`）而非派生表述**（如"body 长度 2→15""body 是否等于基线 true→false"）——框架只把 `body` 精确键落时间线，确定性 cross-check 对时间线做 byte-exact 查表；派生表述不在时间线上，会触发"LLM cited metric ... absent from the observation timeline"覆盖告警（虽不降级 verdict、absence 不算 contradiction，但丢了确定性交叉核对，退化成 LLM-only 直读）。为排除偶发，**把同一次 `http_probe(capture=body)` 调用独立重复下发 2-3 次**确认稳定 http_code=200 / exit_code=0 / body=篡改值（socat 每连接 fork，逐连接一致；重复＝独立多次调用同一探针，**切勿改写成 `sh -c` 里的 `for` 循环**——见注意事项第五条）；探测点须是独立非后端 Pod（见资源准备第 4 条 hairpin 约束）。**降级形态（仅当 `http_probe` 不可用）＝手写 plain curl** `-w '\n---\nhttp=%{http_code} exit=%{exitcode}\n'`（去 `-o /dev/null` 让 body 直出、单条、勿套 `sh -c`、勿 `for` 循环、勿 `| grep` 筛 body；详见注意事项第五条）
3. 读取 Deployment 模板确认 command 已替换为篡改响应体响应器（机制主证——模板层写入已落地，printf 的 body 段含篡改数据、状态行仍是 `200 OK`）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].command}'
   ```
4. **与相邻 HTTP 故障形态的分界复核（本 case 专属）**：确认 curl **exit 是 0（收到一个格式完好的响应）而非 52（Empty reply / 零响应，连接被重置域 1.4）或 7（connection refused）或 28（超时挂起）**，且 **`http_code` 是 200（状态码正常，区别于状态码篡改域 1.5 的 503）而非 000 或 5xx**，**而响应体 body 内容 ≠ 基线**——本 case 签名严格是「握手成功 + 即时返回一个状态码正常、协议完整、但 body 数据被篡改的响应」= exit 0 / http=200 / connect·starttransfer 正常 / **body 内容错**；若 http=200 且 body 也对而 starttransfer↑ 则命中的是响应延迟域（1.3，打错案）；若 http=503 且 body 空则命中的是状态码篡改域（1.5，打错案）

**持续性检查**——故障窗口内故障必须持续存活（配置型故障：篡改响应体响应器 command 在 Deployment 模板里，字段在即故障在，每个新 Pod 复制该模板起篡改响应体响应器）：
以「注入生效确认」为时点锚（注入验证第 1-3 条通过 = 生效：新 RS 齐 + curl http=200/exit0 而 body≠基线 + 模板 command 含篡改 body）——生效确认只证明某一刻故障已落地，不证明它在整个窗口持续存活；持续性复测要把「稳态故障」与「传播收敛窗内的瞬时态」分开：待传播收敛后再次观测仍见故障态才算坐实持续。本故障传播特性：本案为字段型即时生效故障、无传播过程。隔多久复测、复测几次由你依该传播特性与故障形态自行判断（无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复后无法再采集。**时序硬约束**：持续性复测必须在载体 TTL `recovery_deadline`（fire 时刻）**之前留足余量**完成，否则复测会落在逆 patch 已应用之后、只观测到恢复态而非故障存活态。因此 `durationSeconds` 须覆盖「注入 + rollout 重建 + 生效确认 + 复测间隔 + 复测下发」全链并额外留出 fire 前安全余量（宁宽勿窄；窗口过短是持续性证据退化的直接根因）：
1. 白盒复查：篡改响应体响应器 command 仍在模板（`kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].command}'` 输出含篡改 body、状态行仍 `200 OK`，配置即状态）
2. 行为复查：`http_probe(capture=body)` 读 Service 仍 http_code=200 / exit_code=0 / `body` 键=篡改值（同注入验证第 2 条探针）

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原 command 基线（restorePatches 的快响应器 command 由载体内 timer 到点执行逆 patch，触发 rollout 用回快响应器；fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下 Agent 主动恢复为降级兜底形态）：
1. **降级路径 = Agent 主动执行逆 patch**（幂等，重复执行无副作用；把 command 还原为注入前基线快响应器，body 段随之回基线数据）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":<演练步骤 1 记录的基线 command 数组>}]'
   ```
2. 恢复是秒级——逆 patch 触发 rollout 逐副本用回快响应器重建 Pod，无需清理存量对象；json patch 按字段精确替换，天然规避 resourceVersion 乐观锁问题，也不像 apply 三方合并那样保留注入后新增字段

## 恢复验证
1. 执行 `kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].command}'`，确认 command 回到步骤 1 基线快响应器（printf 的 body 段回基线数据、不再含篡改值）
2. 用 `http_probe(capture=body)` 读该 Service，确认恢复返回 `http_code=200`、`exit_code=0`、**时间线 `body` 键回到基线响应体**（不再是被篡改值；用演练步骤 4 的**同一探针 `http_probe(capture=body)`** 与当时记录的基线 `body` 键直接逐字节对比）：
   ```
   http_probe(origin_pod=<独立非后端探测 Pod>, url=http://<service-name>.<namespace>.svc.cluster.local:<port>/, namespace=<namespace>, max_time=10, capture=body)
   ```
   （降级形态＝手写 plain curl `-w '\n---\nhttp=%{http_code} exit=%{exitcode}\n'`，详见注意事项第五条）
3. 确认 rollout 完成、Pod 全部 Running（`kubectl get pods -n <namespace> -l <label-selector>`）；当前代 RS hash 回到注入前基线值（RS hash 是 pod template 全字段相等性的免费校验和——command 还原后 template 与基线逐字段相等 ⇒ hash 必回基线值；hash 不回 = 模板有残留字段，不必猜哪个字段）

## 基准事实
- **根因**：应用层返回一个**状态码正常（200 OK）、协议完整，但响应体数据被篡改**的 HTTP 响应——真实场景为应用 bug 返回错误字段值、缓存层污染返回陈旧/错误内容、中间人或反向代理重写响应体、序列化/编码错误返回损坏数据、feature-flag 或配置误配返回错误 payload、数据库脏读回错误记录；TCP 层、HTTP 状态行、报文框架全正常（握手成功、状态码 200、curl exit 0），故障纯在「响应体数据内容」这一层——是四类 HTTP 故障里最隐蔽的一类（客户端凭状态码判成功，实则消费了错误数据）
- **必现现象**：curl 该 Service 返回 `http_code=200`（状态码正常）、curl exit 0，`time_connect`/`time_starttransfer` 保持基线，**而响应体 body 内容 = 注入的篡改值 ≠ 基线**；Deployment 模板 command 呈篡改响应体响应器（printf body 段含篡改数据、状态行仍 `200 OK`）；tcpSocket（或只校验状态码的 httpGet）就绪探针下 Pod 仍 Ready 仍在 Endpoints 持续供错数据；上游拿到错误数据却判请求成功；恢复后 curl body 回基线、command 回快响应器、RS hash 回基线
- **响应器形态（同域 1.3/1.4/1.5 骨架）**：socat `TCP-LISTEN,fork,reuseaddr` + `SYSTEM:'cat /tmp/resp'` 每连接 fork 一次、把 printf 预写入 /tmp/resp 的响应字节原样回给客户端 → 响应体由 printf 的 body 段唯一决定（状态行保持 `HTTP/1.1 200 OK`、把 body 从基线数据换成篡改数据即得响应体篡改响应器）；time_connect 正常（内核在监听 socket 上完成握手）、响应即时（无 sleep）、http_code = 状态行里的 200、body = printf 写入的内容

## 注意事项
- **判据锚定「http_code=200 + curl exit 0 + connect·starttransfer 正常 + body 内容 ≠ 基线」——本 case 的核心独立价值**：响应体篡改是「服务在正常响应、状态码也对、但返回的数据内容是错的」，与同域另三案正交——① vs `Pod_HTTP故障_状态码被篡改`（1.5）：1.5 状态行错（http=503、body 空），本 case 状态行对（http=200）但 body 数据错；② vs `Pod_HTTP故障_响应延迟`（1.3）：1.3 慢但仍返回 200 + 正确 body（starttransfer↑），本 case 即时返回 200 但 body 被篡改（starttransfer 正常）；③ vs `Pod_HTTP故障_连接被重置`（1.4）：1.4 零响应（exit 52 / http=000），本 case 返回一个格式完整、状态码正常的响应（exit 0 / http=200）——报文层与状态层无瑕疵、唯数据层 body 被改。注入验证第 4 条即以 exit 码 + http_code + body 内容三者形态作分界复核
- **本 case 判据须读 body（与 1.5/1.7 的探针形态差异，不可照抄）**：1.5 只判状态码用默认 `capture=metadata`（丢 body 与 header）；1.7 判 header 用 `capture=headers`（丢 body、结构化 dump 响应头）；本 case 判 body 篡改用 **`capture=body`**（去 `-o /dev/null` 让 body 直出 stdout）。切勿照抄 1.5/1.7 的丢弃 body 形态——那样 body 被丢弃，无法判定篡改，判据失效。**首选形态是 `http_probe(capture=body)`**（born-structured、body 落时间线 `body` exact-key 供 cross-check 精确查表，见注意事项第五条），手写 plain curl（去 `-o /dev/null`、body 在前元数据在后）为降级形态
- **`Content-Length` 与 body 字节数必须一致（本 case 专属技术红线，实测易错点）**：printf 状态行的 `Content-Length: <N>` 必须精确等于 body 段字节数（多字节字符按 UTF-8 字节计），否则 curl 报 `transfer closed with N bytes remaining` 或截断，把「body 被篡改」污染成「传输层出错」的假象、判据失真。篡改 body 定稿后须重新数准 `<N>` 回填；恢复用基线 command 时其 Content-Length 天然与基线 body 匹配（无需另算）
- **SYSTEM 参数的严格立法（corpus 安全红线，不可违背，同 1.5）**：本 case 的 socat `SYSTEM:` 参数**必须**固定为非交互命令 `cat /tmp/resp`（响应字节由 printf 预先写入固定文件），**绝不可**是 `/bin/sh`、`bash` 或任何接受 stdin 的交互式 shell——后者是经典 bind-shell（`socat TCP-LISTEN:9100 EXEC:/bin/sh`），是 target_guard 在 **exec/host-command 路径**（`carriers.classify_host_operation`）显式封禁的攻击形态。本 case 之所以能把 socat SYSTEM 写进 Deployment command，是因为它是 **API 平面 patch 的 manifest 体**——守卫的 EXEC:/SYSTEM:/SHELL: 危险扫描只绑定 exec 侧宿主命令（chroot/nsenter 进入的 host_command），不扫 `kubectl patch` 的 JSON patch value（manifest 体不经该分类器，`recoverability` 亦明写「EXEC:/SYSTEM:/SHELL: danger is NOT re-checked here」）。**这是守卫的已知盲点，不等于该形态被判定为安全**：case 层以本条立法把响应器锁死为固定 `cat /tmp/resp`（响应体只由 printf body 段决定），防止此形态被泛化成往容器里塞交互 shell 的后门。任何把 SYSTEM/EXEC 参数改成 shell、或让它读取 socket 输入的做法都违反本 case 契约
- **就绪探针类型是判据前提（本 case 专属环境依赖）**：`tcpSocket` 就绪探针不校验 HTTP 内容 → 返回篡改 body 的 Pod 仍 Ready、仍在 Endpoints 供错数据，客户端稳定读到 http=200 + 篡改 body（本域常驻靶即此形态，判据成立）；`httpGet` 就绪探针通常只校验状态码（200 即就绪、不校验 body 内容）→ 同样仍 Ready 供错数据；只有当 httpGet 探针的 body 内容校验恰好命中被篡改字段时才可能判不就绪摘流量（罕见，须据实际探针逻辑校准判据）。选靶时确认探针类型并据此校准判据
- **HTTP 边界（对标 Chaos Mesh HTTPChaos / Litmus 的能力边界）**：本形态作用于**明文 HTTP**；不支持 HTTPS/TLS（socat 明文响应器不终结 TLS，TLS 流量需证书/sidecar 面，另案）；注入经 rollout 生效，**存量长连接 / 已建立的 TCP 连接不命中**（只对此后新建连接生效）；与 Istio/Envoy sidecar 注入的服务网格冲突（sidecar 接管流量、可能自身重写响应体而非透传），网格环境须另议
- **verify 相位探针首选框架内置只读工具 `http_probe(capture=body)`**：注入验证第 2 条 / 恢复验证第 2 条 / 资源准备第 4 条的 body+状态码观测，**首选** `http_probe`（参数 `origin_pod`＝独立非后端探测 Pod、`url`＝`http://<service-name>.<namespace>.svc.cluster.local:<port>/`、`namespace`、`max_time`、`capture=body`）——它按名进只读分类器白名单（不会被只读门误拒），`capture=body` 去掉默认的 `-o /dev/null` 丢弃汇让 body 直出 stdout（哨兵行 `===HTTPPROBE_META===` 与 `-w` 元数据分隔；探针恒为**单条 shell-free plain curl、不含任何管道**——exec-form 通道不经 shell，`| head` 之类管道会退化成字面 argv token 被 curl 误当额外 URL，故 body 尺寸由框架输出安全阀兜底、不在 payload 里用 shell 管道限界），框架解析出结构化 `http_code`/`time_connect`/`time_starttransfer`/`exit_code`/`size_download` **外加 `body` 键**（本 case 签名 http_code=200 / exit_code=0 / body=篡改值被如实报为 DATA 而非工具错误），全部落进观测时间线供确定性 cross-check 消费；「连测 2-3 次」＝把同一次 `http_probe` 调用独立重复下发。`http_probe` 已在 planning 相位可用，故资源准备第 4 条的注入前基线 body 也走同一 canonical 探针、born-structured 落时间线。**降级形态＝手写 plain curl（仅当需 byte-exact 全量取证、body 超框架输出安全阀上限、或 `http_probe` 不可用时）**：**必须**是「plain 单条命令」形态——`kubectl exec <探测 Pod> -- curl -s -w '...' http://...`，用 curl 自身输出直接取 body 与 `%{http_code}`/`%{exitcode}`，**不要**套 `sh -c '...'` wrapper、**不要**用 `echo curl_exit=$?`、**不要**把「连测 2-3 次」写成 `for` 循环、**不要**用 `| grep` 管道筛 body（body 判读交给 verifier 读 stdout 原值，不在 shell 里加工）。原因：verify 相位只读门对 `sh -c` wrapper + 循环/重定向/命令替换/管道无法静态证明只读 → fail-closed 拒绝（`readonly_phase_violation` / `unknown_syntax`），verifier 会反复重试后才收敛为 plain curl。plain 单条 curl（GET）本身被只读门放行、body 直出 stdout + `%{http_code}` 免 shell 即得状态码，是手写降级时 verify 相位友好的稳定形态；「连测 2-3 次」= 把同一条 plain 命令独立重复下发，而非在一条命令里循环
- 爆炸半径限于该 Deployment 自身（改的是它自己的入口命令），rollout 逐副本替换；**勿选承载真实写操作或真实数据消费的 HTTP 服务作靶**——响应体篡改比状态码篡改更危险：上游凭 200 状态码判成功、会把篡改后的错误数据当真值消费（污染缓存、错误决策、对账被绕过），而不像 5xx 那样触发上游重试/熔断的自我保护；务必用 expendable 自建演练靶，勿碰真实业务服务；rollout 期新 Pod 每次重装 socat 依赖 apk 仓库可达，仓库不可达时新 Pod 起不来（属环境依赖，须在选靶时确认）
