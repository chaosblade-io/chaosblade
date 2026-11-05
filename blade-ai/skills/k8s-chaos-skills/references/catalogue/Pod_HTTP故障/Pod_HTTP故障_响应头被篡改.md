---
# 受害者作用域参考（route A，非强制 hint）：本用例受害者是承载 HTTP 服务的 Deployment
# 自身——故障机制 patch 的就是该 Deployment 的 spec.template.spec.containers/0/command
# （把容器入口命令从「快响应器」换成「返回被篡改响应头的响应器」：TCP 握手、HTTP 状态行、
# 报文框架、响应体 body 全部正常，状态码仍是 200 OK、body 仍是基线数据，唯独某个响应头
# header 被篡改/注入了错误值），受害者域内写、名字匹配放行；节点、kube-proxy、Service/
# Endpoints、网络 CNI 全程健康，底层 Pod 是「按被改坏的入口命令忠实起一个应用层返回篡改
# 响应头的响应器」而非平台/网络故障。供 planner 填 finish_planning 的 fault_scope 时参照，
# 钉在 deployment 而非降级到 pod（deployment 已注册进 k8s_chaosblade family scopes，见
# fault_registry.py）。这是「参考」而非「边界」——代码不校验、不覆盖，模型仍须理解真实
# 语义后自行判断。
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

**用例名称** 响应头被篡改 导致 Pod_HTTP故障

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 把 containers/0/command 还原为基线快响应器；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace                             # 把入口命令换成「返回被篡改响应头的响应器」——与基线
  path: /spec/template/spec/containers/0/command   # 快响应器唯一差异是 printf 写入 /tmp/resp 的 header 段：状态行仍是 200 OK、body 仍是基线数据，只多注入/改坏一个响应头
  value: ["sh","-c","apk add --no-cache socat >/dev/null 2>&1; printf 'HTTP/1.1 200 OK\\r\\n<被篡改/注入的响应头行>\\r\\nContent-Length: <基线body字节数>\\r\\nConnection: close\\r\\n\\r\\n<基线响应体>' > /tmp/resp; exec socat TCP-LISTEN:<port>,fork,reuseaddr SYSTEM:'cat /tmp/resp'"]
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace                             # 逆 patch 还原为注入前记录的基线 command 数组（快响应器：
  path: /spec/template/spec/containers/0/command   # socat SYSTEM:'cat /tmp/resp' 返回 200 OK + 基线 header + 基线 body）
  value: <演练步骤 1 记录的基线 command 数组>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（须覆盖 rollout + 观察窗，宁宽勿窄）
```

- **注入与恢复是同一字段的镜像**：command 是声明式 API mutation，基线快响应器 ↔ 注入篡改响应器只差 printf 写进 `/tmp/resp` 的 **header 段**（状态行 `HTTP/1.1 200 OK` 与 body 段两者相同，唯 header 段多注入/改坏一个响应头行），socat 形态与 `SYSTEM:'cat /tmp/resp'` 完全相同，恢复通道与注入通道同源且幂等；恢复只是让下一次 rollout 用回快响应器命令重建 Pod，秒级确定性幂等操作。
- **篡改响应头是参数化的（本 case 与 1.5/1.6 的分水岭）**：本 case 篡改的是 **HTTP 响应头 header，状态码保持 `200 OK`、响应体 body 保持基线数据不变**——这是它与同域 `Pod_HTTP故障_状态码被篡改`（1.5，改状态行）、`Pod_HTTP故障_响应体被篡改`（1.6，改 body）的根本区别。规范示例：基线响应头不含 `Content-Type`，篡改后注入一个错误的 `Content-Type: application/json`（客户端按 JSON 解析实为纯文本的 body → 解析失败/误判类型）；同一 printf header 段替换也可构造任意「状态码正常、body 正确、但某个响应头撒谎」的篡改（注入 `Set-Cookie` 会话固定、改坏 `Cache-Control` 致陈旧缓存、重写 `Location` 重定向劫持、注入 `Access-Control-Allow-Origin` 放宽跨域、篡改 `Content-Disposition` 改文件名），判据形态一致（`http_code=200`、curl exit 0、connect/starttransfer 正常、**body 仍等于基线**，而 `%{header_json}` 里出现被篡改/注入的 header 键值），选靶与判据锚定实际注入的那一个 header 即可。
- **注入的 header 行必须是合法的 `\r\n` 结尾头行、且位于分隔空行 `\r\n\r\n` 之前（本 case 专属技术红线）**：printf 写入的篡改 header 行必须夹在状态行与 body 分隔空行之间，以 `\r\n` 结尾——若漏掉 `\r\n` 或把头行写到分隔空行之后，会破坏 HTTP 报文分帧（头被当成 body 一部分、或 body 被当成头），curl 报解析错误或 `transfer closed`，把「header 被篡改」污染成「报文分帧出错」假象。**`Content-Length` 仍必须等于 body 段真实字节数**（本 case body 保持基线不变，故 Content-Length 与基线同值，如基线 body `ok\n`=3 则仍写 3）——注入 header 不改变 body 字节数，Content-Length 不随 header 变化。
- **响应器命令的引号形态**：value 数组第三个元素是一条 `sh -c` 载荷，内部含单引号（`printf '...'`、`SYSTEM:'...'`）与转义的 `\\r\\n`（printf 的 HTTP 响应分隔符）——落 JSON patch 时按 JSON 字符串规则转义内层双引号即可，`SYSTEM:` 参数**必须**固定为 `cat /tmp/resp`（见注意事项第三条 corpus 安全红线，绝不可换成 shell）。
- 多容器 Pod 调整 containers/N 索引至承载 HTTP 服务的目标容器；rollout 由 patch 自动触发。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

## 故障现象
1. 对该 Pod / Service 的 HTTP 请求——**TCP 握手正常、HTTP 状态行仍是 `200 OK`、响应体 body 仍是基线正确数据，但某个响应头 header 被篡改/注入了错误值**：集群内 curl 报 **`http_code=200`（状态码正常）、curl exit 0**，`time_connect` 与 `time_starttransfer` 均保持基线（≈0，无延迟、无拆连接），**body 内容仍等于基线**，唯独 `%{header_json}` 里出现一个被篡改/注入的响应头（基线没有的键、或键值被改坏）
2. **「码对、body 也对、只有头在撒谎」是与相邻形态的分界**——状态码被篡改（1.5，http_code=503、body 空）是状态行错；响应体被篡改（1.6，http_code=200、body≠基线）是数据层 body 错；连接被重置（1.4，curl exit 52 / http_code 000）是零响应；响应延迟（1.3，http_code 200 / starttransfer↑ / body 对）是慢但对；本 case 是**即时返回一个状态码正常、body 正确、但响应头被篡改的响应**（http_code 200 / exit 0 / connect·starttransfer 正常 / **body = 基线** / **header ≠ 基线**），五者签名正交（见注入验证第 4 条）
3. 上游调用方拿到正确的 body 却因错误的 header 做出错误处理——**此现象条件性成立**：需存在真实上游调用方按 header 语义处理响应；用裸探针（curl）测量时探针直接读到被篡改的 header，故本现象在「无真实调用方拓扑」的演练靶上表现为探针读到错误 header 而非上游误处理，不作为注入生效判据（判据看第 1-2 条的 http_code=200/exit0 + body=基线 + header≠基线 分离）。真实危害举例：错误的 `Content-Type` 使客户端用错解析器（把纯文本当 JSON 解析失败）、注入的 `Set-Cookie` 造成会话固定、被改坏的 `Cache-Control` 致下游缓存陈旧内容、重写的 `Location` 把用户重定向到恶意站点、放宽的 `Access-Control-Allow-Origin` 造成跨域数据泄露
4. **就绪探针类型决定 Pod 是否被摘流量（本 case 关键环境事实）**：若 readiness 探针是 `tcpSocket`（本域常驻靶即此形态）或 `httpGet` 只校验状态码（200 即判就绪、不校验 header），则返回篡改 header 的 Pod 仍判 Ready、仍留在 Service Endpoints 里持续供错——k8s 平台层完全看不见这个应用层 header 错误（状态码是 200、body 也对）；只有当 readiness 是 `httpGet` 且其校验逻辑恰好命中被篡改的 header 时才可能摘流量（极罕见）。选靶与判读须先确认靶的就绪探针类型

## 资源准备
1. 确认目标 Deployment（承载 HTTP 服务）正常运行、有 Service 暴露、副本就绪，且未被上层 Operator/Helm/ArgoCD 强 reconcile（否则 command 修改会被覆盖回滚）
2. 确认容器镜像能起一个「接受连接后返回固定 HTTP 响应」的应用层响应器——Alpine 系镜像（如 `chaosblade-tool:1.8.0`，节点已缓存）`apk add --no-cache socat` 可装 socat，`printf '<HTTP 响应字节>' > /tmp/resp; socat TCP-LISTEN:<port>,fork,reuseaddr SYSTEM:'cat /tmp/resp'` **每连接 fork 一次、把 /tmp/resp 原样回给客户端** → 响应头完全由 printf 写入的 header 段决定（在状态行后多注入一个 header 行、保持 body 与 Content-Length 不变即得响应头篡改响应器）；镜像须能访问 apk 仓库（rollout 后新 Pod 每次重装 socat）——**探测 apk 可达性勿用裸 curl 默认 UA 下结论**：部分镜像站会对非 apk UA 施 WAF 过滤，`curl <repo>/APKINDEX.tar.gz` 默认 UA 返回 **403** 而带 `-A 'apk-tools/2.14.6'`（apk 自身 UA）返回 **200**、目录列表亦 200 ⇒ 403 ≠ 仓库不可达，`apk add` 自带 apk-tools UA 故实际可装；判定 apk 是否真不可达须用 apk UA 复测或看目录列表，勿据默认 UA 的 403 误判机制不可行而擅自降级响应器形态
3. 记录注入前基线（restorePatches 基线值来源）：`spec.template.spec.containers[0].command` 原值、Service 名与端口、目标 label selector、就绪探针类型（tcpSocket / httpGet）、**以及基线响应头 header 的确切清单**（本 case 判据核心——篡改前后 header 比对的参照，须逐个记录基线有哪些 header 及其值，如本域常驻靶基线仅 `Content-Length` + `Connection`、**无 `Content-Type`**）
4. 取判据锚点——用框架内置只读探针 `http_probe` 读该 Service 的基线（正常返回 `http_code=200`、`exit_code=0`、`time_connect`≈0、`time_starttransfer`<100ms、**基线 header 清单**）：
   ```
   http_probe(origin_pod=<独立非后端探测 Pod>, url=http://<service-name>.<namespace>.svc.cluster.local:<port>/, namespace=<namespace>, max_time=10, capture=headers)
   ```
   **`capture=headers` 是本 case 的关键参数、不可省**——`http_probe` 默认 `capture=metadata` 会 `-o /dev/null` 丢弃响应头、拿不到 header_json（本 case 判据核心即在此）；只有 `capture=headers` 才在 `-w` 里加 `headers=%{header_json}`，框架把 header_json **扁平成 `header.<小写名>` exact-key**（如 `header.content-length`）落观测时间线供 cross-check 精确查表。记下基线（**全部 `header.<名>` 键 + 元数据**；探测 Pod 须与 Service 同集群网络可达；**勿从后端 Pod 自身探测**——kube-proxy hairpin 会使后端 Pod 探测自身 Service ClusterIP 超时得到 exit_code=28/http_code=000 假象，探测点必须是独立非后端 Pod）。**本 case 探针与 1.5/1.6 的关键差异：`capture=headers` 读响应头**——1.5 只判状态码用默认 `capture=metadata`（丢 body 与 header）、1.6 判 body 用 `capture=body`（body 直出），本 case 判 header 篡改故用 `capture=headers`（丢 body、结构化 dump 响应头供判读）。**降级形态（仅当 `http_probe` 不可用）＝手写 plain curl** `kubectl exec <探测 Pod> -- curl -s -o /dev/null -w 'headers=%{header_json} http=%{http_code} exit=%{exitcode}\n' --max-time 10 http://...`（`%{header_json}` 需 curl ≥ 7.83，client curl 8.14.1 支持；**切勿用 per-header 变量 `%{header_<名>}`**——不存在，curl 报 `unknown --write-out variable`；**也不要用 `curl -D -`**——它被 verify 只读门 fail-closed 拒；详见注意事项第五条 verify 相位探针形态约束）

## 演练步骤
（主路径 = 步骤 1 基线快照（containers[0].command 原值 + Service/端口 + 就绪探针类型 + curl 基线 http=200/exit0 + **基线 header 清单**——restorePatches 基线值来源，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、patches=command→篡改响应头响应器、restorePatches=command→基线快响应器、duration_seconds=<duration>），注入+武装+readback 工具内同步完成。步骤 2 的手动 patch 序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 定位承载 HTTP 服务的目标 Deployment，完成基线快照：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].command}'
   kubectl get svc -n <namespace> -l <label-selector>
   ```
   Agent 读取并记录原始 command 数组（恢复值以它为参照）、Service/端口、就绪探针类型、**以及 curl 基线 header 清单**（判据参照）；多容器 Pod 请把 containers 索引对准承载 HTTP 服务的容器
2. **降级路径手动序列**（主路径由装配器承载，此处仅兜底）——把入口命令 patch 成篡改响应头响应器（状态行保持 200 OK、body 保持基线，只在 header 段多注入一个响应头行），rollout 自动重建 Pod：
   ```bash
   # 注入：command 换成篡改响应头响应器（与基线唯一差异 = printf 在状态行后多写入一个被篡改/注入的 header 行，body 与 Content-Length 均不变）
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sh","-c","apk add --no-cache socat >/dev/null 2>&1; printf '\''HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 3\r\nConnection: close\r\n\r\nok\n'\'' > /tmp/resp; exec socat TCP-LISTEN:<port>,fork,reuseaddr SYSTEM:'\''cat /tmp/resp'\''"]}]'
   # 恢复（逆 patch，幂等；command 用步骤 1 记录的基线数组）
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":<基线 command 数组>}]'
   ```
3. 等待 rollout 完成，观察对该 Service 的 HTTP 请求返回状态码正常（`http_code=200`、curl exit 0）、body 仍是基线数据，但 **响应头被篡改/注入了基线没有的头**（如 `Content-Type`）

## 注入验证
1. 确认 rollout 完成、新 Pod 承载篡改响应头响应器：**用 RS 视角判据，不要用 `kubectl rollout status`**——响应器 Pod 可能因就绪探针/启动耗时使 `rollout status` 等待超时报错，按其退出码会把已生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label-selector>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数且 READY 齐
2. **（主证据，必做）** 用 `http_probe(capture=headers)` 读该 Service 响应头，量化「状态码正常 + body 正常 + header 被篡改」并与基线对比：
   ```
   http_probe(origin_pod=<独立非后端探测 Pod>, url=http://<service-name>.<namespace>.svc.cluster.local:<port>/, namespace=<namespace>, max_time=10, capture=headers)
   ```
   注入生效判据：**`http_code=200`（状态码保持正常，未被篡改）、`exit_code=0`、`time_connect`/`time_starttransfer` 保持基线，而扁平出的 `header.<名>` 键里出现被篡改/注入的响应头（如 `header.content-type=application/json`，基线没有该键）**。**metric_claim 必须引这个 exact-key（`header.content-type=<值>`）而非派生表述**（如"响应头字段数 2→3""content-type present 0→1"）——框架只把 `header.<小写名>` 精确键落时间线，确定性 cross-check 对时间线做 byte-exact 查表；派生表述不在时间线上，会触发"LLM cited metric ... absent from the observation timeline"覆盖告警（虽不降级 verdict、absence 不算 contradiction，但丢了确定性交叉核对，退化成 LLM-only 直读）。为排除偶发，**把同一次 `http_probe(capture=headers)` 调用独立重复下发 2-3 次**确认稳定 http_code=200 / exit_code=0 / header=篡改值（socat 每连接 fork，逐连接一致；重复＝独立多次调用同一探针，**切勿改写成 `sh -c` 里的 `for` 循环**——见注意事项第五条）；探测点须是独立非后端 Pod（见资源准备第 4 条 hairpin 约束）。**若需同时坐实 body 未被牵连（与 1.6 分界），可另发一次 `http_probe(capture=body)` 确认 `body` 键仍 = 基线**——但主判据锚定 `header.<名>` 的篡改键，body 不变是本 case 与 1.6 的正交分界而非生效要件。**降级形态（仅当 `http_probe` 不可用）＝手写 plain curl** `-o /dev/null -w 'headers=%{header_json} http=%{http_code} exit=%{exitcode}\n'`（单条、勿套 `sh -c`、勿 `for` 循环、勿 `| grep` 筛 header、勿 `curl -D -`；详见注意事项第五条）
3. 读取 Deployment 模板确认 command 已替换为篡改响应头响应器（机制主证——模板层写入已落地，printf 的 header 段含被篡改/注入的头行、状态行仍是 `200 OK`、body 段仍是基线数据）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].command}'
   ```
4. **与相邻 HTTP 故障形态的分界复核（本 case 专属）**：确认 curl **exit 是 0（收到一个格式完好的响应）而非 52（Empty reply / 零响应，连接被重置域 1.4）或 7（connection refused）或 28（超时挂起）或 18（传输截断）**，且 **`http_code` 是 200（状态码正常，区别于状态码篡改域 1.5 的 503）**，且 **body 仍等于基线（区别于响应体篡改域 1.6 的 body≠基线）**，而 **`%{header_json}` 里出现被篡改/注入的响应头**——本 case 签名严格是「握手成功 + 即时返回一个状态码正常、body 正确、但响应头被篡改的响应」= exit 0 / http=200 / connect·starttransfer 正常 / body=基线 / **header≠基线**；若 http=200 且 header 也对而 body≠基线则命中的是响应体篡改域（1.6，打错案）；若 http=503 则命中的是状态码篡改域（1.5，打错案）；若 http=200 且 body/header 都对而 starttransfer↑ 则命中的是响应延迟域（1.3，打错案）

**持续性检查**——故障窗口内故障必须持续存活（配置型故障：篡改响应头响应器 command 在 Deployment 模板里，字段在即故障在，每个新 Pod 复制该模板起篡改响应头响应器）：
以「注入生效确认」为时点锚（注入验证第 1-3 条通过 = 生效：新 RS 齐 + curl http=200/exit0/body=基线 而 header_json 含篡改键 + 模板 command 含篡改 header 行）——生效确认只证明某一刻故障已落地，不证明它在整个窗口持续存活；持续性复测要把「稳态故障」与「传播收敛窗内的瞬时态」分开：待传播收敛后再次观测仍见故障态才算坐实持续。本故障传播特性：本案为字段型即时生效故障、无传播过程。隔多久复测、复测几次由你依该传播特性与故障形态自行判断（无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复后无法再采集。**时序硬约束**：持续性复测必须在载体 TTL `recovery_deadline`（fire 时刻）**之前留足余量**完成，否则复测会落在逆 patch 已应用之后、只观测到恢复态而非故障存活态。因此 `durationSeconds` 须覆盖「注入 + rollout 重建 + 生效确认 + 复测间隔 + 复测下发」全链并额外留出 fire 前安全余量（宁宽勿窄；窗口过短是持续性证据退化的直接根因）：
1. 白盒复查：篡改响应头响应器 command 仍在模板（`kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].command}'` 输出含篡改 header 行、状态行仍 `200 OK`，配置即状态）
2. 行为复查：`http_probe(capture=headers)` 读 Service 仍 http_code=200 / exit_code=0 / `header.<名>` 含篡改键（同注入验证第 2 条探针）

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原 command 基线（restorePatches 的快响应器 command 由载体内 timer 到点执行逆 patch，触发 rollout 用回快响应器；fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下 Agent 主动恢复为降级兜底形态）：
1. **降级路径 = Agent 主动执行逆 patch**（幂等，重复执行无副作用；把 command 还原为注入前基线快响应器，header 段随之回基线、不再含篡改头）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":<演练步骤 1 记录的基线 command 数组>}]'
   ```
2. 恢复是秒级——逆 patch 触发 rollout 逐副本用回快响应器重建 Pod，无需清理存量对象；json patch 按字段精确替换，天然规避 resourceVersion 乐观锁问题，也不像 apply 三方合并那样保留注入后新增字段

## 恢复验证
1. 执行 `kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].command}'`，确认 command 回到步骤 1 基线快响应器（printf 的 header 段回基线、不再含篡改头行）
2. 用 `http_probe(capture=headers)` 读该 Service，确认恢复返回 `http_code=200`、`exit_code=0`、**扁平出的 `header.<名>` 键回到基线 header 清单**（不再含被篡改/注入的头，如 `header.content-type` 消失；用演练步骤 4 的**同一探针 `http_probe(capture=headers)`** 与当时记录的基线 `header.<名>` 键直接对比）：
   ```
   http_probe(origin_pod=<独立非后端探测 Pod>, url=http://<service-name>.<namespace>.svc.cluster.local:<port>/, namespace=<namespace>, max_time=10, capture=headers)
   ```
   （降级形态＝手写 plain curl `-o /dev/null -w 'headers=%{header_json} ...'`，详见注意事项第五条）
3. 确认 rollout 完成、Pod 全部 Running（`kubectl get pods -n <namespace> -l <label-selector>`）；当前代 RS hash 回到注入前基线值（RS hash 是 pod template 全字段相等性的免费校验和——command 还原后 template 与基线逐字段相等 ⇒ hash 必回基线值；hash 不回 = 模板有残留字段，不必猜哪个字段）

## 基准事实
- **根因**：应用层返回一个**状态码正常（200 OK）、body 正确、协议完整，但某个响应头被篡改/注入**的 HTTP 响应——真实场景为应用 bug/误配置发出错误的 `Content-Type`、网关/反向代理重写响应头、注入 `Set-Cookie` 会话固定、改坏 `Cache-Control`/`Expires` 致陈旧缓存、重写 `Location` 重定向劫持、放宽 `Access-Control-Allow-Origin` 跨域泄露、篡改 `Content-Security-Policy` 削弱防护、或（本 case 手段）容器入口命令被改成返回篡改响应头的响应器；TCP 层、HTTP 状态行、body 数据全正常（握手成功、状态码 200、body 正确、curl exit 0），故障纯在「响应头语义」这一层——是五类 HTTP 故障里最隐蔽的一类（客户端凭状态码 + body 判成功，实则被一个错误的 header 误导了后续处理）
- **必现现象**：curl 该 Service 返回 `http_code=200`（状态码正常）、curl exit 0、body 仍等于基线，`time_connect`/`time_starttransfer` 保持基线，**而 `%{header_json}` 里出现被篡改/注入的响应头键值 ≠ 基线 header 清单**；Deployment 模板 command 呈篡改响应头响应器（printf header 段含篡改头行、状态行仍 `200 OK`、body 段仍基线）；tcpSocket（或只校验状态码的 httpGet）就绪探针下 Pod 仍 Ready 仍在 Endpoints 持续供错；上游拿到正确 body 却因错误 header 误处理；恢复后 curl header_json 回基线、command 回快响应器、RS hash 回基线
- **响应器形态（同域 1.3/1.4/1.5/1.6 骨架）**：socat `TCP-LISTEN,fork,reuseaddr` + `SYSTEM:'cat /tmp/resp'` 每连接 fork 一次、把 printf 预写入 /tmp/resp 的响应字节原样回给客户端 → 响应头由 printf 的 header 段唯一决定（在状态行后多注入一个 header 行、保持 body 与 Content-Length 不变即得响应头篡改响应器）；time_connect 正常（内核在监听 socket 上完成握手）、响应即时（无 sleep）、http_code = 状态行里的 200、header = printf 写入的头行、body = printf 写入的 body 段（本 case 保持基线）
- **判据探针形态（本 case 专属）**：client curl 8.14.1 支持 `-w '%{header_json}'`（把所有响应头 dump 成 JSON，如 `{"content-length":["3"],"connection":["close"]}`）；**per-header 变量 `%{header_<名>}` 不存在**（curl 报 `unknown --write-out variable`）；`curl -D -` 被 verify 只读门 fail-closed 拒；`curl -i` headers 直出 stdout 可作降级。故主判据锚定 `-o /dev/null -w 'headers=%{header_json} http=%{http_code} exit=%{exitcode}\n'`——plain 单条、结构化、只读门友好

## 注意事项
- **判据锚定「http_code=200 + curl exit 0 + connect·starttransfer 正常 + body=基线 + header ≠ 基线」——本 case 的核心独立价值**：响应头篡改是「服务在正常响应、状态码对、body 也对、但某个响应头在撒谎」，与同域另四案正交——① vs `Pod_HTTP故障_状态码被篡改`（1.5）：1.5 状态行错（http=503），本 case 状态行对（http=200）但 header 错；② vs `Pod_HTTP故障_响应体被篡改`（1.6）：1.6 body 被改（body≠基线、header 正常），本 case body 不变（body=基线）、唯 header 被改；③ vs `Pod_HTTP故障_响应延迟`（1.3）：1.3 慢但 header/body 都对（starttransfer↑），本 case 即时返回（starttransfer 正常）但 header 被篡改；④ vs `Pod_HTTP故障_连接被重置`（1.4）：1.4 零响应（exit 52 / http=000），本 case 返回一个格式完整、状态码正常、body 正确的响应（exit 0 / http=200）——报文层、状态层、数据层 body 无瑕疵、唯 header 层被改。注入验证第 4 条即以 exit 码 + http_code + body + header_json 四者形态作分界复核
- **本 case 判据须读 header（与 1.5/1.6 的探针形态差异，不可照抄）**：1.5 只判状态码用默认 `capture=metadata`（丢 body 与 header）；1.6 判 body 用 `capture=body`（body 直出 stdout）；本 case 判 header 篡改用 **`capture=headers`**（丢 body、结构化 dump 响应头）。切勿照抄 1.5（拿不到 header）或 1.6（body 直出但 header 混在 stdout 里不易结构化判读）——**首选形态是 `http_probe(capture=headers)`**（born-structured、扁平成 `header.<名>` exact-key 落时间线，见注意事项第五条），手写 plain curl `%{header_json}` 为降级形态。**降级用手写 curl 时 `%{header_json}` 需 curl ≥ 7.83**（选靶时确认探测 Pod 的 curl 版本；版本过旧则降级 `curl -i` 让 headers 直出 stdout 由 verifier 读原值）
- **注入 header 行的分帧合法性 + Content-Length 与 body 一致（本 case 专属技术红线，实测易错点）**：printf 写入的篡改 header 行必须以 `\r\n` 结尾、位于状态行与 body 分隔空行 `\r\n\r\n` 之间——漏 `\r\n` 或把头行写到分隔空行之后会破坏报文分帧，curl 报解析错误/`transfer closed`，把「header 被篡改」污染成「报文出错」假象。**`Content-Length` 仍必须精确等于 body 段字节数**（本 case body 保持基线不变，故 Content-Length 与基线同值）——注入 header 不改变 body 字节数，Content-Length 不随 header 变化，切勿因加了 header 就误改 Content-Length
- **SYSTEM 参数的严格立法（corpus 安全红线，不可违背，同 1.5/1.6）**：本 case 的 socat `SYSTEM:` 参数**必须**固定为非交互命令 `cat /tmp/resp`（响应字节由 printf 预先写入固定文件），**绝不可**是 `/bin/sh`、`bash` 或任何接受 stdin 的交互式 shell——后者是经典 bind-shell（`socat TCP-LISTEN:9100 EXEC:/bin/sh`），是 target_guard 在 **exec/host-command 路径**（`carriers.classify_host_operation`）显式封禁的攻击形态。本 case 之所以能把 socat SYSTEM 写进 Deployment command，是因为它是 **API 平面 patch 的 manifest 体**——守卫的 EXEC:/SYSTEM:/SHELL: 危险扫描只绑定 exec 侧宿主命令（chroot/nsenter 进入的 host_command），不扫 `kubectl patch` 的 JSON patch value（manifest 体不经该分类器，`recoverability` 亦明写「EXEC:/SYSTEM:/SHELL: danger is NOT re-checked here」）。**这是守卫的已知盲点，不等于该形态被判定为安全**：case 层以本条立法把响应器锁死为固定 `cat /tmp/resp`（响应头只由 printf header 段决定），防止此形态被泛化成往容器里塞交互 shell 的后门。任何把 SYSTEM/EXEC 参数改成 shell、或让它读取 socket 输入的做法都违反本 case 契约
- **就绪探针类型是判据前提（本 case 专属环境依赖）**：`tcpSocket` 就绪探针不校验 HTTP 内容 → 返回篡改 header 的 Pod 仍 Ready、仍在 Endpoints 供错，客户端稳定读到 http=200 + 篡改 header（本域常驻靶即此形态，判据成立）；`httpGet` 就绪探针通常只校验状态码（200 即就绪、不校验 header）→ 同样仍 Ready 供错；只有当 httpGet 探针逻辑恰好命中被篡改 header 时才可能判不就绪摘流量（极罕见，须据实际探针逻辑校准判据）。选靶时确认探针类型并据此校准判据
- **HTTP 边界（对标 Chaos Mesh HTTPChaos / Litmus 的能力边界）**：本形态作用于**明文 HTTP**；不支持 HTTPS/TLS（socat 明文响应器不终结 TLS，TLS 流量需证书/sidecar 面，另案）；注入经 rollout 生效，**存量长连接 / 已建立的 TCP 连接不命中**（只对此后新建连接生效）；与 Istio/Envoy sidecar 注入的服务网格冲突（sidecar 接管流量、可能自身重写响应头而非透传），网格环境须另议
- **verify 相位探针首选框架内置只读工具 `http_probe(capture=headers)`**：注入验证第 2 条 / 恢复验证第 2 条 / 资源准备第 4 条的 header+状态码观测，**首选** `http_probe`（参数 `origin_pod`＝独立非后端探测 Pod、`url`＝`http://<service-name>.<namespace>.svc.cluster.local:<port>/`、`namespace`、`max_time`、`capture=headers`）——它按名进只读分类器白名单（不会被只读门误拒），`capture=headers` 在 `-w` 里加 `headers=%{header_json}`（仍走 `-o /dev/null` 丢弃 body），框架解析出结构化 `http_code`/`time_connect`/`time_starttransfer`/`exit_code`/`size_download` **外加 `headers` 字段**；框架把 header_json **扁平成 `header.<小写名>` 键**进观测时间线（如 `header.content-type=application/json`），LLM 据此引用被篡改/注入的锚点 header 作 metric_claim，既有确定性 cross-check 精确查表命中（本 case 签名 http_code=200 / exit_code=0 / header=篡改键值被如实报为 DATA 而非工具错误）；「连测 2-3 次」＝把同一次 `http_probe` 调用独立重复下发。`http_probe` 已在 planning/verify/recover 相位可用，故资源准备第 4 条的注入前基线 header 也走同一 canonical 探针、born-structured 落时间线。**降级形态＝手写 `%{header_json}` plain curl（仅当 `http_probe` 不可用时）**：**必须**是「plain 单条命令」形态——`kubectl exec <探测 Pod> -- curl -s -o /dev/null -w 'headers=%{header_json}...' http://...`，用 curl 自身 `-w` 输出直接取 header_json 与 `%{http_code}`/`%{exitcode}`，**不要**套 `sh -c '...'` wrapper、**不要**用 `echo curl_exit=$?`、**不要**把「连测 2-3 次」写成 `for` 循环、**不要**用 `| grep` 管道筛 header（header 判读交给 verifier 读 `%{header_json}` 原值，不在 shell 里加工）、**不要**用 `curl -D -`（被只读门拒）。原因：verify 相位只读门对 `sh -c` wrapper + 循环/重定向/命令替换/管道无法静态证明只读 → fail-closed 拒绝（`readonly_phase_violation` / `unknown_syntax`），verifier 会反复重试后才收敛为 plain curl。plain 单条 curl（GET）本身被只读门放行、`%{header_json}` 免 shell 即得结构化响应头，是手写降级时 verify 相位友好的稳定形态；「连测 2-3 次」= 把同一条 plain 命令独立重复下发，而非在一条命令里循环
- 爆炸半径限于该 Deployment 自身（改的是它自己的入口命令），rollout 逐副本替换；**勿选承载真实写操作或真实数据消费的 HTTP 服务作靶**——响应头篡改比状态码篡改更隐蔽：上游凭 200 状态码 + 正确 body 判成功、不会触发重试/熔断的自我保护，却被错误 header 误导后续处理（错误 Content-Type 致解析失败、注入 Set-Cookie 致会话固定、改坏 Cache-Control 致缓存污染、重写 Location 致重定向劫持），部分 header 篡改（Set-Cookie/Location/CORS）还带安全语义、危害高于纯数据错误；务必用 expendable 自建演练靶，勿碰真实业务服务；rollout 期新 Pod 每次重装 socat 依赖 apk 仓库可达，仓库不可达时新 Pod 起不来（属环境依赖，须在选靶时确认）
