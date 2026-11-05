---
# 受害者作用域参考（route A，非强制 hint）：本用例受害者是 TLS Secret 自身——故障
# 机制 patch 的是该 Secret 的 data.tls.crt/tls.key（受害者域内写，名字匹配放行），
# Ingress 只是引用方、全程不改。供 planner 填 finish_planning 的 fault_scope 时参照，
# 把它钉在 secret 而非降级到 service/ingress：降级会使 approved.scope ≠
# effective.scope=secret，被 drift 守卫结构性拒绝（openspec
# register-config-object-fault-scopes 已把 secret 注册进词表修复此链）。这是「参考」
# 而非「边界」——代码不校验、不覆盖（无 enforcement），模型仍须理解真实语义后自行判断。
fault_scope: secret
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 TLS Secret 的证书内容），
# 路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+
# 验权+武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/
# RBAC 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 载体由 faultdrill_assemble_carrier 程序化建栈、结构性自拆（fail-closed 路径 + 恢复句柄清理链 + 武装定时器自删 → apiserver ownerRef GC 级联）；降级路径不自建可自撤 RBAC 栈，正文无需传输载体自撤授权类知识。
carrier_teardown: assembler-managed
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch TLS Secret 的 data.tls.crt/tls.key 属受害者域内，名字匹配放行），
# 无跨对象写条目；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash>
# 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌检查（RBAC 从
# restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），不经 LLM
# kubectl 写面，无立法条目。
---

**用例名称** TLS证书过期 导致 Ingress_入口异常

## 故障定位
配置型故障——Ingress 引用的 TLS Secret 内容被替换为已过期的证书后，ingress
controller reload 新证书，客户端 TLS 握手因证书过期被拒（curl：`SSL certificate
problem: certificate has expired`；浏览器：`NET::ERR_CERT_DATE_INVALID`），
**过期证书在位即故障存活**，贯穿整个故障窗口；窗口到期载体 TTL 用活体捕获的
基线证书内容还原 Secret 即自动恢复。与同目录「后端服务不存在」的区别：路由层
健康（backend 正确、Endpoints 在位），故障只在 **TLS 终止层**——HTTP 明文路径
（若 Ingress 同时暴露 80）不受影响。手段1（ChaosBlade）不适用——ChaosBlade 无
证书类故障靶点，本用例为 kubectl-native 专属注入。`duration_seconds` 是必填的
故障窗口契约，未给定时先向用户确认。

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 TLS Secret 证书内容；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（不再作为恢复通道），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Secret
  name: <tls-secret-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch）：过期证书/私钥是计算型载荷，value 填哨兵串，装配器在载体内签发后回填真实 base64
- op: replace
  path: /data/tls.crt
  value: <faultdrill:expired-cert cn=<ingress-host> part=crt>
- op: replace
  path: /data/tls.key
  value: <faultdrill:expired-cert cn=<ingress-host> part=key>
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace
  path: /data/tls.crt
  value: null                             # 活体捕获语义——工具运行时自采注入前基线证书（byte-exact），证书内容不经 LLM 转录
- op: replace
  path: /data/tls.key
  value: null                             # 同上（私钥基线活体捕获，恢复不依赖任何人工记录）
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- **为什么 patches 填哨兵而非现成 base64**：过期证书是计算型载荷——notAfter 落在过去需密码学签发，LLM 无法凭空算出，装配器的执行缝又只跑 kubectl 子命令（本机算不了）。载体 Pod 是集群内唯一受管制计算通道：`patches` 里需生成证书的 op 其 `value` 填哨兵串 `<faultdrill:expired-cert cn=<host> part=crt|key>`，装配器检测哨兵 → 在载体内用临时 mini-CA + `openssl ca -enddate <过去>` 签发过期证书与配对私钥 → 回填 `tls.crt`/`tls.key` 两个 op（crt/key 原子，任一缺失即 fail-closed 不注入）→ 再进入既有 arm→inject→landing readback（readback 比对的是回填后的真实 base64）。`cn` 取目标 host；无哨兵值的既有文本型配方行为完全恒等。生成回执（notAfter + 注入 crt base64）由装配器写入 receipt，作 machinery 白盒佐证；in-window 独立白盒以注入后对活体 Secret 内容的载体 exec 解码为准（见注入验证第 1 条），回执非唯一来源。
- **载体镜像须具备 openssl-ca 能力**：装配器在武装前探测载体镜像能否 `openssl ca -enddate`，缺失即 fail-closed（pre-arming 中止、清理已建栈、不注入）。镜像发现只覆盖 healthy 标准 DaemonSet 的镜像；集群若无可发现的 openssl-capable 载体镜像，需在计划中显式指定一个具备该能力的 `carrier_image`（节点缓存镜像免拉取）。
- **为什么直接 patch 既有 Secret 而非新建道具 Secret**：真实事故形态就是"在位证书过期未轮换"——替换既有 Secret 内容对 controller 是一次普通的 Secret 更新事件，reload 路径与真实轮换完全一致；新建道具 Secret + 改 Ingress 引用会引入第二处写入点且 reload 语义不同（引用切换 vs 内容更新）。活体捕获（`value: null`）保证恢复 byte-exact 还原原证书，无私钥泄漏面（基线值只在工具内存与台账恢复域中流转，不经 LLM 转录）。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。

## 故障现象
1. HTTPS 访问 Ingress 入口时 TLS 握手失败：curl 报 `SSL certificate problem: certificate has expired`；浏览器报 `NET::ERR_CERT_DATE_INVALID`；`openssl s_client … | openssl x509 -noout -checkend 0` **非零退出**（已过期）。注：`Verify return code` 在证书由不受信 mini-CA 签发且未 serve chain 时为 `21 (unable to verify the first certificate)`（建链阶段先短路）而非 `10 (certificate has expired)`，故判别锚 `-checkend` 退出码、勿锚 return code
2. 服务端返回的证书 notAfter 时间在过去（白盒主证）
3. **HTTP 明文路径不受影响**（若 Ingress 同时暴露 80 端口且未强制跳转）——故障只在 TLS 终止层
4. 后端 Service/Pod 全程健康，Ingress 路由配置未变（与后端服务不存在形态的区别）
5. 强制 TLS 跳转（`ssl-redirect`）开启时，HTTP 请求被 308 跳转到 HTTPS 后同样失败——表现为入口完全不可用

## 资源准备
1. 确认目标 Ingress 配置了 TLS 且引用可定位的 Secret：
   ```bash
   kubectl get ingress <ingress-name> -n <namespace> -o jsonpath='{.spec.tls[0].secretName}{"\n"}{.spec.tls[0].hosts}'
   ```
2. 确认基线 HTTPS 访问正常（基线不可达时本 case 无法验证故障效果，先修基线或换靶）：
   ```bash
   curl -s --max-time 10 -o /dev/null -w '%{http_code}' https://<ingress-host>/<path>
   ```
   预期响应码正常。**基线（注入前）证书有效期无法经只读通道独立 openssl 解码**——此相位载体 Pod 尚未建立、host 顶层通道禁管道（`base64 -d | openssl x509` 解码管道 host 侧不可达），故基线只能靠 curl 隐含核验：基线为 CA 可信证书时 curl 成功即隐含有效期正常；基线为自签证书时 curl 两态均校验失败，此时有效期以**注入后 in-window 白盒**（经载体 exec 解码活体 Secret 内容的 notAfter + base64 byte-identity 比对，见注入验证第 1-2 条）为准
3. 确认 Secret 类型为 `kubernetes.io/tls` 且含 `tls.crt`/`tls.key` 两键（cert-manager 管理的证书注意：cert-manager 会按 Certificate 资源自动轮换 Secret 内容——注入后可能被 cert-manager 在分钟级内还原，**演练窗口与 cert-manager 轮换周期存在竞态**。预检 `kubectl get certificate -n <namespace>` 确认靶 Secret 是否被 cert-manager 管理；被管理时如实告知用户该竞态并确认窗口，或换非 cert-manager 管理的靶）
4. **载体镜像 openssl-ca 能力由装配器自探保证，规划期无需手动探测**：过期证书在载体 Pod 内签发（本机 openssl 版本与生成路径无关）。装配器在武装前于**已建载体内**跑能力探针（`command -v openssl` + `openssl ca -enddate`），缺能力即 pre-arming fail-closed（中止、清理已建栈、不注入，回执明写 `retryable with a corrected carrier_image`）。因此**不要在规划期主动搜寻 DaemonSet 镜像或拉起探针 Pod 去"确认存在"**——那与装配器自探完全重复，且会在只读规划期遗留探针残留。唯一动作是反应式的：仅当装配器 fail-closed 报告"无可发现的 openssl-capable 载体镜像"时，才在重试计划中显式指定一个具备 `openssl ca -enddate` 能力的 `carrier_image`（节点缓存镜像免拉取，详见载体配方）；**若集群无任何预置 openssl-ca 的 allowlist 镜像，见演练步骤 2 的"运行时安装"有界兜底**

## 演练步骤
（主路径 = 基线锚点捕获（步骤 1）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Secret、patches=证书内容替换且 value 填哨兵串、restorePatches=活体捕获还原、duration_seconds=<duration>），装配器在载体内签发过期证书并回填 patches，注入+武装+readback 工具内同步完成——无独立的证书生成步骤；以下手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：

> **爆炸半径分类（定案，defer recovery-carrier §9）**：声明 `blast_radius_scope=namespace-wide`——本 case 走 apiserver-write 四件套 carrier（Pod/SA/Role/RoleBinding），新增 namespaced RBAC 授权面，按 §9①「四件套 → namespace-wide」定案免推导，勿在 target-only↔namespace-wide 间重复仲裁。故障本体写入集仅靶 TLS Secret 自身的 data 两键（target-only 语义）；consequence 影响面 = 该 Ingress host 的全部外部 HTTPS 入口流量，一并写入 `blast_radius_detail` 注明，勿因 consequence 抬到 cluster-wide（无跨 ns 持久资源写入）。

1. **基线锚点捕获**：记录 Secret 当前 `tls.crt` 的 base64 串（恢复 byte-identity 对照锚；主路径下装配器活体捕获基线并在工具内 byte-exact 回填，此锚仅供恢复验证白盒比对；**不导出私钥明文**——基线捕获在注入前、载体尚未建立，host 顶层禁管道，故此阶段 `openssl` 指纹/有效期解码管道不可达，基线锚只取 `tls.crt` 的 base64 原文；注入后 in-window 则可经载体 exec 独立解码活体证书，见注入验证第 1 条）：
   ```bash
   kubectl get secret <tls-secret-name> -n <namespace> -o jsonpath='{.data.tls\.crt}'
   ```
2. **过期证书 = 声明式哨兵载荷，无独立生成步骤**：`patches` 中 `tls.crt`/`tls.key` 两个 op 的 `value` 填哨兵串 `<faultdrill:expired-cert cn=<ingress-host> part=crt|key>`（见载体配方）。装配器检测哨兵后在载体 Pod 内用临时 mini-CA + `openssl ca -enddate <过去>` 签发过期证书与配对私钥，原子回填两个 op 再进入 arm→inject→readback——过期证书是计算型载荷，notAfter 落在过去需密码学签发，LLM 与本机 shell 均无法凭空算出，载体 Pod 是集群内唯一受管制计算通道。**证书生成/签发在本机三通道皆不可达**（签发是 `openssl ca`/`req -x509`/`genrsa` 类**写/签面**，只读守卫在各通道一律拒；host 顶层通道另禁管道；`faketime` 亦拦），故不设本机生成步骤。注意区分**解码面**（`openssl x509 -noout` 打印字段、`s_client` 读服务端证书，经载体 exec 只读可达，见注入验证）与**签发面**：解码面无法签发过期证书，生成仍须依托载体内 `openssl ca` 能力，与本机可达性结论无关。降级路径下（装配器不可用）证书生成仍须依托一个具备 openssl-ca 能力的环境，无纯本机可达兜底。装配器 fail-closed 报"无 openssl-capable 镜像"时，按序两条兜底：① 换用具备 `openssl ca -enddate` 能力的 `carrier_image` 重试（节点缓存镜像免拉取）；② **若集群无任何预置 openssl-ca 的 allowlist 镜像**，但某 allowlist 镜像基于包管理器（Alpine/apk 等）且其包源经预检可达，则在已建载体内运行时安装 openssl（`apk add --no-cache openssl`）后再签发。**有界条件（缺一即回落如实失败、不反复重试）**：(a) 仅当装配器已 fail-closed 确认无预置镜像；(b) 只用镜像自带包管理器、不下 arbitrary 二进制；(c) 包源可达性判定**必须以真实 `apk add --no-cache openssl` 尝试为准，严禁用 curl/wget 探 HTTP 码或目录列表代替**——包镜像普遍对通用 HTTP 客户端做 bot 拦截（curl/wget 常得 403 + 合成 honeypot 目录含伪造包名），却对包管理器自身 UA 放行；故 curl 403/伪造列表**不构成「不可达」证据**，据此推断「apk 必然失败」是假阴性。唯一合法的条件 c 证伪 = 实际 `apk add` 命令自身非零退出/装不上，装不上才 fail-closed 收尾、不忙等；(d) 不放宽载体 SHAPE 白名单、不授任何运行时特权——安装只是载体内一条 exec；(e) 该安装是有意的 mutating setup 命令（会盖 issue-time 窗口锚，属预期）；(f) 装的包随载体 teardown 自清、无宿主残留。**本机顶层生成仍三通道皆不可达**，此兜底只在载体内、不改本机可达性结论。定位为 best-effort 兜底、非保证路径（依赖包源可达性）
3. **先武装定时自恢复，再注入**（主路径 = 装配器一次调用内完成武装+注入；降级路径 = 按 recovery-carrier.md 标准件自建载体栈，Role 动词集 `get,patch secrets`，武装命令形态参照 ConfigMap 篡改 case——紧凑变量形态 merge-patch curl，data 两键同载荷还原。恢复命令幂等：定时器到期还原为主，Agent 演练结束时主动执行同一条 patch 兜底，重复执行无副作用）
4. **注入**（降级兜底形态——主路径下由装配器注入域承载）：
   ```bash
   kubectl patch secret <tls-secret-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/data/tls.crt","value":"<过期证书base64>"},{"op":"replace","path":"/data/tls.key","value":"<过期私钥base64>"}]'
   ```
   （降级路径的 `<过期证书base64>`/`<过期私钥base64>` 须来自一个具备 openssl-ca 能力的签发环境；主路径下这两个值由装配器在载体内签发后自动回填，不经 LLM 转录）
   注入后 ingress controller watch Secret 变更自动 reload（ingress-nginx 秒级；**已知证书缓存坑**——部分版本 controller 对 Secret 更新 reload 滞后或需 worker 进程轮换才生效，见注意事项）

## 注入验证
1. **（主证据，必做）白盒 notAfter——活体解码**：注入后窗口内经载体 Pod exec 对**活体 Secret 内容**独立解码取 notAfter——把第 2 条 jsonpath 取回的 `tls.crt` base64 塞进载体解码（exec 通道放行解码管道；host 顶层禁管道、签发面 `openssl ca` 仍拒，故只在载体内解码；用字面量、勿用 `VAR=` 变量赋值形，后者被只读守卫拒）：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'echo <tls.crt-base64> | base64 -d | openssl x509 -noout -enddate -subject -issuer'
   ```
   notAfter 由工具从活体字节解出、落在过去即证过期证书在位——此为 receipt-independent 的活体白盒。装配器回执 `cert_payloads[].not_after`（签发时写入）作 machinery 佐证，两者一致即互证；回执是上一相位 bookkeeping，活体解码才是独立观测，故不以回执为唯一来源。已有确定性工具解码通道即以其为准——对 base64 肉眼解 UTCTime 昂贵且易错，不再是本判据的取证形态
2. **（主证据，必做）白盒在位比对**：取回 Secret 内 `tls.crt` 的 base64 原文，与回执 `cert_payloads[].crt_b64` 字符串逐字节比对——byte-identity 即证明签发的过期证书仍在位：
   ```bash
   kubectl get secret <tls-secret-name> -n <namespace> -o jsonpath='{.data.tls\.crt}'
   ```
3. 行为佐证——HTTPS 入口 TLS 校验失败（两个只读信号均经载体 exec 可达）。**served-cert 校验层信号**（最忠实）——读 controller 实际加载并对外服务的证书，`checkend 0` 非零退出=已过期；受 reload 传播窗约束，等 15s 后采、用字面量 hostname（勿用 `VAR=` 变量赋值形，会被只读守卫拒后重发挤过窗口）：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'echo | openssl s_client -connect <controller-svc>.<ingress-ns>.svc.cluster.local:443 -servername <ingress-host> 2>/dev/null | openssl x509 -noout -enddate -checkend 0'
   ```
   **客户视角佐证**——curl HTTPS 校验失败：
   ```bash
   curl -s --max-time 10 -o /dev/null -w '%{http_code}' https://<ingress-host>/<path>
   ```
   **区分度取决于基线信任**：基线为 CA 可信证书时，注入后 curl 由基线成功（200）转为校验失败（自造 mini-CA 签发的证书 issuer 不受信 + 已过期，curl 校验层先失败），区分度成立；基线为自签证书时，curl 在基线态与注入态均校验失败，HTTP 码无区分度——此时行为面不作裁决依据，以第 1-2 条白盒为准。`curl -k`（跳校验）恒可完成握手（故障纯在校验层），亦不作判据。**controller reload 有秒级传播窗**——注入后立即探测可能仍返回旧证书，等 15s 再采；15s 后仍旧证书则核对 controller 日志 reload 记录（缓存坑判读见注意事项）
4. 反证确认——HTTP 明文路径与后端健康（故障只在 TLS 层的关键判据）：
   ```bash
   curl -s --max-time 10 -o /dev/null -w '%{http_code}' -H 'Host: <ingress-host>' http://<入口地址>/<path>
   kubectl get endpoints <backend-svc> -n <namespace> -o jsonpath='{.subsets[0].addresses[0].ip}'
   ```
   预期 HTTP 路径响应码正常（若开启强制跳转则 308，属预期）、Endpoints 有 IP

> ⚠️ 验证纪律：
> - `curl -k`（跳过校验）能正常访问是**预期**（证书内容过期但 TLS 握手本身可完成），不是注入失败——本 case 判据是「校验开启时握手被拒」，严禁用 `-k` 的结果反推注入无效。
> - 同一事实（证书 notAfter）确认一次即可，不要重复查询。

**持续性检查**——故障窗口内故障必须持续存活（配置型故障：过期证书在 Secret 即在位）：
以「注入生效确认」为时点锚（注入验证第 1-2 条通过 = 生效）——生效确认只证明某一刻故障已落地，不证明它在整个窗口持续存活；持续性复测要把「稳态故障」与「传播收敛窗内的瞬时态」分开：待传播收敛后再次观测仍见故障态才算坐实持续。本故障传播特性：controller reload 秒级；cert-manager 管理的靶存在证书轮换竞态（见资源准备第 3 条），复测须早于轮换介入。隔多久复测、复测几次由你依该传播特性与故障形态自行判断（无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入/cert-manager 轮换介入后如实报告：
1. 白盒复查：`kubectl get secret -o jsonpath='{.data.tls\.crt}'` 取回的 base64 仍与回执 `crt_b64` byte-identity（过期证书仍在位，同注入验证第 2 条）
2. 行为复查：HTTPS 入口 curl 仍校验失败（区分度受基线信任约束，同注入验证第 3 条；基线自签时以白盒为准）
3. 稳定性复查：后端 Pod RESTARTS 无增长、Endpoints 仍有 IP（排除故障向路由层蔓延的误判）

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原 Secret 证书内容（活体捕获基线 byte-exact 回填；fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动将 Secret 证书内容还原为基线；演练提前结束时由 Agent 主动执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用。**降级路径人工还原的前提**：演练步骤 1 已记录基线证书内容——人工路径需要注入前导出的原证书 base64（`kubectl get secret ... -o jsonpath='{.data.tls\.crt}'` 注入前留存），主路径活体捕获无此人工依赖）：
   ```bash
   kubectl patch secret <tls-secret-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/data/tls.crt","value":"<基线证书base64>"},{"op":"replace","path":"/data/tls.key","value":"<基线私钥base64>"}]'
   ```
2. 等待 controller reload（秒级传播窗 + 可能的证书缓存滞后，见注意事项），TLS 握手自动恢复——**无需重启任何 Pod**
3. 主路径无本机临时文件需清理——过期证书在载体 Pod 内的临时工作目录签发，脚本收尾即 `rm -rf` 自清；载体栈四件套由恢复流程带外核查零残留（见注意事项）

## 恢复验证
1. **白盒 byte-exact 还原**：取回 Secret 内 `tls.crt` 的 base64 原文，与演练步骤 1 的基线锚逐字节比对——byte-identity 即证明活体捕获的基线证书已 byte-exact 还原（主路径下装配器活体捕获保证还原必然 byte-exact；不一致即还原异常，取证 restore.log 与 cert-manager 事件后如实报告）：
   ```bash
   kubectl get secret <tls-secret-name> -n <namespace> -o jsonpath='{.data.tls\.crt}'
   ```
2. 行为确认——HTTPS 入口 TLS 校验恢复（reload 传播窗内立即探测可能仍返回过期证书，等 15s 再采；区分度受基线信任约束——基线 CA 可信时 curl 由校验失败恢复为成功，基线自签时两态均失败、以第 1 条白盒为准）：
   ```bash
   curl -s --max-time 10 -o /dev/null -w '%{http_code}' https://<ingress-host>/<path>
   ```
3. 确认载体 restore.log 取证（`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`——定时器 fire 的直接证据，PATCH 响应回显；`cat` 经只读通道可达）

## 基准事实
- **根因**：Ingress 引用的 TLS Secret 证书内容过期未轮换（真实场景：cert-manager 故障/人工轮换遗漏/自动化流水线失效），controller 加载过期证书后客户端 TLS 校验失败
- **必现现象**：Secret 内证书 notAfter 在过去（白盒主证 = 注入后 in-window 经载体 exec 解码活体 Secret 内容的 notAfter `base64 -d | openssl x509 -noout -enddate` + Secret base64 与回执 byte-identity；回执 notAfter 作 machinery 佐证）；HTTPS 入口 TLS 校验失败——校验层忠实信号是 `echo | openssl s_client … | openssl x509 -noout -checkend 0` 的**非零退出**（trust-independent、最鲁棒），经载体 exec 只读可达；`Verify return code` 不必是 `10 (certificate has expired)`——注入证书由临时 mini-CA 签发、issuer 不受信且未 serve chain 时，openssl 建链阶段短路返回 `21 (unable to verify the first certificate)` 先于日期判定，故勿以 return code 10 为锚，curl 作客户视角佐证且区分度受基线信任约束（基线自签时 curl 两态无区分度）；HTTP 明文路径与后端全程健康；`curl -k` 跳过校验仍可访问（证书过期不阻断握手完成，只阻断校验）
- **传播窗事实**：ingress controller watch Secret 变更后 reload 为秒级，但存在证书缓存滞后形态（见注意事项）——注入与恢复的行为判据都要计入 ~15s 窗，窗内结果不作裁决依据
- **作用域边界**：仅引用该 Secret 的 Ingress host 受影响；同集群其他 TLS 入口、HTTP 明文路径、Service 层直访路径均不受影响

## 注意事项
- chaosblade 无证书类故障靶点，本用例为 kubectl-native 专属注入
- **探针形态与同目录「后端服务不存在」(#70) 的关键差异——`http_probe` 不是本 case 首选**：#70 裁决锚是入口 `http_code`（503 vs 基线），故首选 `http_probe(resolve=...)` 把状态码 born-structured 落 cross-check 时间线（W-70-5 根治）。本 case 裁决锚是**白盒**——注入验证第 1-2 条（载体 exec `openssl x509 -noout -enddate` 解码活体 notAfter + Secret `tls.crt` base64 与回执 byte-identity）与第 3 条 served-cert 校验层信号（`openssl s_client … -checkend 0` 非零退出，trust-independent 最鲁棒），均非 HTTP 状态码判据，`http_probe` 结构上够不到（只产 `http_code`/`exit_code` 等 L7 HTTP 元数据，做不了 openssl 证书解码/TLS 校验层探测）。第 3 条客户视角 `curl https://`（不加 `-k`）是**显式弱佐证**（基线自签时两态皆校验失败、无区分度，见第 3 条「行为面不作裁决依据」），故不改为 `http_probe`（改了也不进裁决、反增正文/note 张力）。**带 TLS 的 HTTPS 探针一律不用 `http_probe`**——它无 `-k`/insecure flag，且本 case 恰恰要观测「校验开启时握手被拒」，HTTPS 探针仍走手写 `curl https://`（不加 `-k`）+ `openssl s_client`。唯一与 #70 同形的是第 4 条反证的 HTTP 明文探针（`curl -H 'Host:' http://`，vhost 路由、预期正常码），如需与 #70 一致落时间线可选 `http_probe(resolve=<ingress-host>:80:<controller-ClusterIP>)`，但它是反证、非裁决要件，保留手写形态亦可
- **ingress-nginx 证书缓存坑**：部分 ingress-nginx 版本对 Secret 更新的证书热加载存在滞后（GitHub ingress-nginx#9451 同族问题——worker 进程持有旧证书直至轮换）。判读纪律：注入验证 15s 窗后仍返回旧证书时，先核对 controller 日志的 reload 记录（`kubectl logs -n <ingress-ns> <controller-pod> | grep -i reload`），确认 reload 已发生而证书仍旧 → 属缓存滞后形态，等待 worker 轮换（分钟级）或如实记录该环境事实；**严禁**为此重启 controller Pod（超出演练授权面，且重启会波及该 controller 服务的全部入口）
- **cert-manager 竞态**：靶 Secret 被 cert-manager 管理时，其控制器可能在分钟级内把 Secret 轮换还原——注入后故障窗口可能被 cert-manager 提前终止（非载体 timer 所为）。持续性检查发现证书被还原时，取证 cert-manager 事件（`kubectl get events -n <namespace> --field-selector involvedObject.name=<tls-secret-name>`）如实归因，不得误判为 timer 提前触发
- 过期私钥与证书在载体 Pod 内的临时工作目录签发，签发脚本收尾即 `rm -rf` 自清，无本机临时资产残留；载体栈四件套（SA/Role/RoleBinding/裸 Pod）由恢复流程带外核查零残留
- **基线私钥不留人工副本**：主路径活体捕获在工具内完成基线回填，人工降级路径才需要注入前导出原证书内容留存——导出物含私钥，留存介质与清理义务按演练方安全规范执行，case 不鼓励人工导出（优先主路径）
- 若 Ingress 使用云厂商托管证书（ALB/GCE 的证书资源不在 Secret 里），本 case 对该靶结构性不可达——预检发现 `spec.tls` 无 secretName 或引用云证书资源时如实报告并停止
- 恢复验证的 base64 byte-identity 对照是「还原 byte-exact」的强判据——活体捕获路径下 Secret 内 `tls.crt` 的 base64 必与演练步骤 1 基线锚逐字节一致；不一致即还原异常，取证 restore.log 与 cert-manager 事件后如实报告
