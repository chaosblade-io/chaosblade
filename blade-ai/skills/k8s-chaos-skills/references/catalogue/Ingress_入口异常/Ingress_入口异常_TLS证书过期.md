---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 TLS Secret 的证书内容），
# 路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+
# 验权+武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/
# RBAC 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
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
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 TLS Secret 证书内容；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Secret
  name: <tls-secret-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch；过期证书/私钥的 base64 由演练步骤 2 现场生成后填入）
- op: replace
  path: /data/tls.crt
  value: <过期证书base64>
- op: replace
  path: /data/tls.key
  value: <过期私钥base64>
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace
  path: /data/tls.crt
  value: null                             # 活体捕获语义——工具运行时自采注入前基线证书（byte-exact），证书内容不经 LLM 转录
- op: replace
  path: /data/tls.key
  value: null                             # 同上（私钥基线活体捕获，恢复不依赖任何人工记录）
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- **为什么直接 patch 既有 Secret 而非新建道具 Secret**：真实事故形态就是"在位证书过期未轮换"——替换既有 Secret 内容对 controller 是一次普通的 Secret 更新事件，reload 路径与真实轮换完全一致；新建道具 Secret + 改 Ingress 引用会引入第二处写入点且 reload 语义不同（引用切换 vs 内容更新）。活体捕获（`value: null`）保证恢复 byte-exact 还原原证书，无私钥泄漏面（基线值只在工具内存与台账恢复域中流转，不经 LLM 转录）。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。

## 故障现象
1. HTTPS 访问 Ingress 入口时 TLS 握手失败：curl 报 `SSL certificate problem: certificate has expired`；浏览器报 `NET::ERR_CERT_DATE_INVALID`；`openssl s_client` 显示 `Verify return code: 10 (certificate has expired)`
2. 服务端返回的证书 notAfter 时间在过去（白盒主证）
3. **HTTP 明文路径不受影响**（若 Ingress 同时暴露 80 端口且未强制跳转）——故障只在 TLS 终止层
4. 后端 Service/Pod 全程健康，Ingress 路由配置未变（与后端服务不存在形态的区别）
5. 强制 TLS 跳转（`ssl-redirect`）开启时，HTTP 请求被 308 跳转到 HTTPS 后同样失败——表现为入口完全不可用

## 资源准备
1. 确认目标 Ingress 配置了 TLS 且引用可定位的 Secret：
   ```bash
   kubectl get ingress <ingress-name> -n <namespace> -o jsonpath='{.spec.tls[0].secretName}{"\n"}{.spec.tls[0].hosts}'
   ```
2. 确认基线 HTTPS 访问正常且证书在有效期内（基线不可达时本 case 无法验证故障效果，先修基线或换靶）：
   ```bash
   curl -s -o /dev/null -w '%{http_code}' https://<ingress-host>/<path>
   echo | openssl s_client -connect <入口地址>:443 -servername <ingress-host> 2>/dev/null | openssl x509 -noout -enddate
   ```
   预期响应码正常、`notAfter` 在未来
3. 确认 Secret 类型为 `kubernetes.io/tls` 且含 `tls.crt`/`tls.key` 两键（cert-manager 管理的证书注意：cert-manager 会按 Certificate 资源自动轮换 Secret 内容——注入后可能被 cert-manager 在分钟级内还原，**演练窗口与 cert-manager 轮换周期存在竞态**。预检 `kubectl get certificate -n <namespace>` 确认靶 Secret 是否被 cert-manager 管理；被管理时如实告知用户该竞态并确认窗口，或换非 cert-manager 管理的靶）
4. 确认本地 openssl 版本（决定过期证书生成路径）：`openssl version`——3.2+ 支持 `-not_after` 直接生成已过期证书；老版本走 faketime 降级路径（演练步骤 2）

## 演练步骤
（主路径 = 基线捕获（步骤 1）→ 生成过期证书（步骤 2，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Secret、patches=证书内容替换、restorePatches=活体捕获还原、duration_seconds=<duration>），注入+武装+readback 工具内同步完成；以下手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：

> **爆炸半径分类（定案）**：`target-only`——写入集仅靶 TLS Secret 自身的 data 两键；consequence 影响面 = 该 Ingress host 的全部外部 HTTPS 入口流量，写入 `blast_radius_detail` 注明，勿因 consequence 抬档。

1. **基线捕获**：记录 Secret 当前证书指纹与有效期（restorePatches 活体捕获的对照锚点，两条路径共用；**不在命令行导出私钥明文**——活体捕获由装配器在工具内完成，人工基线只记录公证书指纹）：
   ```bash
   kubectl get secret <tls-secret-name> -n <namespace> -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -fingerprint -enddate
   ```
2. **生成已过期证书**（两条路径共用；主路径 OpenSSL 3.2+，降级 faketime 包装）：
   ```bash
   # 主路径（OpenSSL 3.2+）：-not_before/-not_after 直接指定有效期窗口（落在过去）
   openssl req -x509 -newkey rsa:2048 -nodes -keyout /tmp/drill-expired.key -out /tmp/drill-expired.crt \
     -subj "/CN=<ingress-host>" -not_before 20200101000000Z -not_after 20200102000000Z
   # 降级（老版本 OpenSSL 且本地有 faketime）：时间旅行包装，-days 1 即生成"2020 年签发、1 天后过期"的证书
   faketime '2020-01-01 00:00:00' openssl req -x509 -newkey rsa:2048 -nodes \
     -keyout /tmp/drill-expired.key -out /tmp/drill-expired.crt -subj "/CN=<ingress-host>" -days 1
   # 生成后立即自证过期（两条路径共用）：notAfter 必须在过去
   openssl x509 -in /tmp/drill-expired.crt -noout -enddate
   ```
   base64 编码供 patches 填入：`base64 -i /tmp/drill-expired.crt`（macOS）/ `base64 -w0 /tmp/drill-expired.crt`（Linux）
3. **先武装定时自恢复，再注入**（主路径 = 装配器一次调用内完成武装+注入；降级路径 = 按 recovery-carrier.md 标准件自建载体栈，Role 动词集 `get,patch secrets`，武装命令形态参照 ConfigMap 篡改 case——紧凑变量形态 merge-patch curl，data 两键同载荷还原。恢复命令幂等：定时器到期还原为主，Agent 演练结束时主动执行同一条 patch 兜底，重复执行无副作用）
4. **注入**（降级兜底形态——主路径下由装配器注入域承载）：
   ```bash
   kubectl patch secret <tls-secret-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/data/tls.crt","value":"<过期证书base64>"},{"op":"replace","path":"/data/tls.key","value":"<过期私钥base64>"}]'
   ```
   注入后 ingress controller watch Secret 变更自动 reload（ingress-nginx 秒级；**已知证书缓存坑**——部分版本 controller 对 Secret 更新 reload 滞后或需 worker 进程轮换才生效，见注意事项）

## 注入验证
1. **（主证据，必做）** 白盒确认 Secret 内证书已过期：
   ```bash
   kubectl get secret <tls-secret-name> -n <namespace> -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -enddate
   ```
   预期 `notAfter` 在过去（2020 年窗口）
2. 行为确认——TLS 握手报证书过期（与基线对照）：
   ```bash
   curl -sv https://<ingress-host>/<path> 2>&1 | grep -i 'expire'
   echo | openssl s_client -connect <入口地址>:443 -servername <ingress-host> 2>/dev/null | openssl x509 -noout -enddate
   ```
   预期 curl 报 `certificate has expired`、s_client 返回的证书 notAfter 为过期值。**controller reload 有秒级传播窗**——注入后立即探测可能仍返回旧证书，等 15s 再采；15s 后仍旧证书则核对 controller 日志 reload 记录（缓存坑判读见注意事项）
3. 反证确认——HTTP 明文路径与后端健康（故障只在 TLS 层的关键判据）：
   ```bash
   curl -s -o /dev/null -w '%{http_code}' -H 'Host: <ingress-host>' http://<入口地址>/<path>
   kubectl get endpoints <backend-svc> -n <namespace> -o jsonpath='{.subsets[0].addresses[0].ip}'
   ```
   预期 HTTP 路径响应码正常（若开启强制跳转则 308，属预期）、Endpoints 有 IP

> ⚠️ 验证纪律：
> - `curl -k`（跳过校验）能正常访问是**预期**（证书内容过期但 TLS 握手本身可完成），不是注入失败——本 case 判据是「校验开启时握手被拒」，严禁用 `-k` 的结果反推注入无效。
> - 同一事实（证书 notAfter）确认一次即可，不要重复查询。

**持续性检查（必做）**——故障窗口内故障必须持续存活（配置型故障：过期证书在 Secret 即在位）：
以「注入生效确认」为时点锚（注入验证第 1-2 条通过 = 生效），生效后一次 `time_wait 30`（间隔 = 2 × 传播上限：controller reload 秒级 → 30s 最小复测窗，按 SKILL.md「持续性采样间隔 per-case 推导」；cert-manager 管理的靶按资源准备第 3 条竞态告知缩短复测间隔），到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入/cert-manager 轮换介入后如实报告：
1. 白盒复查：Secret 内证书 notAfter 仍为过期值（同注入验证第 1 条）
2. 行为复查：TLS 握手仍报证书过期（同注入验证第 2 条）
3. 稳定性复查：后端 Pod RESTARTS 无增长、Endpoints 仍有 IP（排除故障向路由层蔓延的误判）

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原 Secret 证书内容（活体捕获基线 byte-exact 回填；fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动将 Secret 证书内容还原为基线；演练提前结束时由 Agent 主动执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用。**降级路径人工还原的前提**：演练步骤 1 已记录基线证书内容——人工路径需要注入前导出的原证书 base64（`kubectl get secret ... -o jsonpath='{.data.tls\.crt}'` 注入前留存），主路径活体捕获无此人工依赖）：
   ```bash
   kubectl patch secret <tls-secret-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/data/tls.crt","value":"<基线证书base64>"},{"op":"replace","path":"/data/tls.key","value":"<基线私钥base64>"}]'
   ```
2. 等待 controller reload（秒级传播窗 + 可能的证书缓存滞后，见注意事项），TLS 握手自动恢复——**无需重启任何 Pod**
3. 清理本地过期证书临时文件（演练资产，两条路径共用）：`rm -f /tmp/drill-expired.crt /tmp/drill-expired.key`

## 恢复验证
1. 白盒确认 Secret 内证书已还原基线（指纹与演练步骤 1 基线一致）：
   ```bash
   kubectl get secret <tls-secret-name> -n <namespace> -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -fingerprint -enddate
   ```
   预期指纹与基线一致、`notAfter` 回到未来
2. 行为确认——TLS 握手恢复正常（reload 传播窗内立即探测可能仍返回过期证书，等 15s 再采）：
   ```bash
   curl -s -o /dev/null -w '%{http_code}' https://<ingress-host>/<path>
   echo | openssl s_client -connect <入口地址>:443 -servername <ingress-host> 2>/dev/null | openssl x509 -noout -enddate
   ```
   预期 curl 无证书错误、响应码回基线、s_client 证书为基线证书
3. 确认载体 restore.log 取证（`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`——定时器 fire 的直接证据，PATCH 响应回显）

## 基准事实
- **根因**：Ingress 引用的 TLS Secret 证书内容过期未轮换（真实场景：cert-manager 故障/人工轮换遗漏/自动化流水线失效），controller 加载过期证书后客户端 TLS 校验失败
- **必现现象**：Secret 内证书 notAfter 在过去；HTTPS 握手报 `certificate has expired`（curl）/ `Verify return code: 10`（s_client）；HTTP 明文路径与后端全程健康；`curl -k` 跳过校验仍可访问（证书过期不阻断握手完成，只阻断校验）
- **传播窗事实**：ingress controller watch Secret 变更后 reload 为秒级，但存在证书缓存滞后形态（见注意事项）——注入与恢复的行为判据都要计入 ~15s 窗，窗内结果不作裁决依据
- **作用域边界**：仅引用该 Secret 的 Ingress host 受影响；同集群其他 TLS 入口、HTTP 明文路径、Service 层直访路径均不受影响

## 注意事项
- chaosblade 无证书类故障靶点，本用例为 kubectl-native 专属注入
- **ingress-nginx 证书缓存坑（实测定案前如实标注）**：部分 ingress-nginx 版本对 Secret 更新的证书热加载存在滞后（GitHub ingress-nginx#9451 同族问题——worker 进程持有旧证书直至轮换）。判读纪律：注入验证 15s 窗后仍返回旧证书时，先核对 controller 日志的 reload 记录（`kubectl logs -n <ingress-ns> <controller-pod> | grep -i reload`），确认 reload 已发生而证书仍旧 → 属缓存滞后形态，等待 worker 轮换（分钟级）或如实记录该环境事实；**严禁**为此重启 controller Pod（超出演练授权面，且重启会波及该 controller 服务的全部入口）
- **cert-manager 竞态**：靶 Secret 被 cert-manager 管理时，其控制器可能在分钟级内把 Secret 轮换还原——注入后故障窗口可能被 cert-manager 提前终止（非载体 timer 所为）。持续性检查发现证书被还原时，取证 cert-manager 事件（`kubectl get events -n <namespace> --field-selector involvedObject.name=<tls-secret-name>`）如实归因，不得误判为 timer 提前触发
- 过期私钥与证书是演练现场生成的临时资产（/tmp 下），演练结束必须清理（恢复段第 3 步）——虽是自签废证书，私钥文件残留仍是不良卫生
- **基线私钥不留人工副本**：主路径活体捕获在工具内完成基线回填，人工降级路径才需要注入前导出原证书内容留存——导出物含私钥，留存介质与清理义务按演练方安全规范执行，case 不鼓励人工导出（优先主路径）
- 若 Ingress 使用云厂商托管证书（ALB/GCE 的证书资源不在 Secret 里），本 case 对该靶结构性不可达——预检发现 `spec.tls` 无 secretName 或引用云证书资源时如实报告并停止
- 恢复验证的指纹对照是「还原 byte-exact」的强判据——活体捕获路径下指纹必与基线一致；不一致即还原异常，取证 restore.log 与 cert-manager 事件后如实报告
