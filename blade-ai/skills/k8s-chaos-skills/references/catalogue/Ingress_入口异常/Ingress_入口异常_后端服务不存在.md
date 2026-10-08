---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 Ingress backend service
# 名），路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：
# 建栈+验权+武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点
# 不容纳/RBAC 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch Ingress 的 backend service 名属受害者域内，名字匹配放行），无跨
# 对象写条目；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash>
# 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌检查（RBAC 从
# restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），不经 LLM
# kubectl 写面，无立法条目。
---

**用例名称** 后端服务不存在 导致 Ingress_入口异常

## 故障定位
配置型故障——Ingress 规则的 backend service 名被改为不存在的名字后，ingress
controller reload 配置，对该 host/path 的请求因找不到后端 Endpoints 而返回
503，**错误配置存活即故障存活**，贯穿整个故障窗口；窗口到期载体 TTL 还原
backend 名即自动恢复。与既有 Service 层形态（Service_调用失败/负载均衡异常）的
区别：故障点在**入口路由层**而非 Service/Endpoints 本身——Service 与后端 Pod
全程健康，仅 Ingress 的转发目标被指错。手段1（ChaosBlade）不适用——ChaosBlade
无 Ingress 配置类故障靶点，本用例为 kubectl-native 专属注入。`duration_seconds`
是必填的故障窗口契约，未给定时先向用户确认。

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 backend service 名；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Ingress
  name: <ingress-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace
  path: /spec/rules/0/http/paths/0/backend/service/name
  value: drill-nonexistent-svc-<hash>
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace
  path: /spec/rules/0/http/paths/0/backend/service/name
  value: null                             # 活体捕获语义——工具运行时自采注入前基线值（byte-exact），优于 LLM 转录
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 多 rule/多 path 的 Ingress 调整 `rules/N`、`paths/M` 索引至目标条目；只改一条 path 的 backend，其余 path 不受影响（爆炸半径收敛的最小注入面）。
- 若靶 Ingress 使用 `defaultBackend`（无 rules 结构），注入路径改为 `/spec/defaultBackend/service/name`。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。

## 故障现象
1. 通过 Ingress 入口访问目标 host/path 返回 **503 Service Unavailable**（ingress-nginx 对无 Endpoints 后端的默认响应；其他 controller 实现可能返回 502/404，以实测为准）
2. ingress controller 日志出现 backend 解析失败记录（ingress-nginx：`service drill-nonexistent-svc-<hash> not found` 或 endpoints 为空告警）
3. **Service 与后端 Pod 全程健康**——`kubectl get svc,endpoints,pods` 均正常，故障只在 Ingress 转发层（与 Service 层故障的关键区别）
4. 直接访问 Service ClusterIP 或 Pod IP 仍可达（绕过 Ingress 的路径不受影响）

## 资源准备
1. 确认目标 Ingress 存在且当前路由正常（基线可达）：
   ```bash
   kubectl get ingress <ingress-name> -n <namespace> -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}'
   ```
2. 确认有可访问的入口地址（外部 LB 地址 / 节点端口 + Host 头）并记录基线响应：
   ```bash
   curl -s -o /dev/null -w '%{http_code}' -H 'Host: <ingress-host>' http://<入口地址>/<path>
   ```
   预期 200（或业务正常码）——基线不可达时本 case 无法验证故障效果，先修基线或换靶
3. 确认 ingress controller 在位且健康：
   ```bash
   kubectl get pods -A -l app.kubernetes.io/name=ingress-nginx
   ```
4. 确认目标 Ingress 的 rules/paths 结构（决定 json-patch 索引与 defaultBackend 分支）：
   ```bash
   kubectl get ingress <ingress-name> -n <namespace> -o jsonpath='{.spec.rules[*].http.paths[*].backend.service.name}'
   ```

## 演练步骤
（主路径 = 基线捕获（步骤 1）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Ingress、patches=backend 名改错、restorePatches=活体捕获还原、duration_seconds=<duration>），注入+武装+readback 工具内同步完成；以下手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：

> **爆炸半径分类（定案）**：`target-only`——写入集仅靶 Ingress 自身的一个 backend 字段（新 backend 名是字符串值，不创建任何新对象）；consequence 影响面 = 该 host/path 的全部外部入口流量，写入 `blast_radius_detail` 注明，勿因 consequence 抬档。

1. **基线捕获**：记录 backend service 名基线与入口响应码基线（restorePatches 活体捕获的对照锚点，两条路径共用）：
   ```bash
   kubectl get ingress <ingress-name> -n <namespace> -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}'
   curl -s -o /dev/null -w '%{http_code}' -H 'Host: <ingress-host>' http://<入口地址>/<path>
   ```
2. **先武装定时自恢复，再注入**（主路径 = 装配器一次调用内完成武装+注入；降级路径 = 按 recovery-carrier.md 标准件自建载体栈，Role 动词集 `get,patch ingresses`，武装命令形态参照 ConfigMap 篡改 case 资源准备第 3 条——紧凑变量形态 merge-patch curl。恢复命令幂等：定时器到期还原为主，Agent 演练结束时主动执行同一条 patch 兜底，重复执行无副作用）
3. **注入**（降级兜底形态——主路径下由装配器注入域承载）：
   ```bash
   kubectl patch ingress <ingress-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/rules/0/http/paths/0/backend/service/name","value":"drill-nonexistent-svc-<hash>"}]'
   ```
   注入后 ingress controller 自动 reload（ingress-nginx watch Ingress 变更，reload 秒级；无需手动重启 controller）

## 注入验证
1. **（主证据，必做）** 白盒确认 backend 已指向不存在的 service：
   ```bash
   kubectl get ingress <ingress-name> -n <namespace> -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}'
   ```
   预期输出 `drill-nonexistent-svc-<hash>`
2. 行为确认——入口访问返回 503（与基线响应码对照）：
   ```bash
   curl -s -o /dev/null -w '%{http_code}' -H 'Host: <ingress-host>' http://<入口地址>/<path>
   ```
   预期 503。**controller reload 有秒级传播窗**——注入后立即 curl 可能仍 200（旧配置未卸载），等 10s 再采；10s 后仍 200 则核对 controller 日志是否 reload 失败
3. 反证确认——Service 与后端 Pod 健康（故障只在入口层的关键判据）：
   ```bash
   kubectl get endpoints <backend-svc> -n <namespace> -o jsonpath='{.subsets[0].addresses[0].ip}'
   kubectl get pods -n <namespace> -l <backend-label> -o jsonpath='{.items[0].status.phase}'
   ```
   预期 Endpoints 有 IP、Pod Running——若 Endpoints 为空，故障根因不是本 case（backend 本来就异常），如实报告并 replan

> ⚠️ 验证纪律：
> - 直接访问 Service ClusterIP/Pod IP 仍可达是**预期**（绕过 Ingress），不是注入失败。
> - 同一事实（backend 字段值）确认一次即可，不要重复查询。

**持续性检查（必做）**——故障窗口内故障必须持续存活（配置型故障：错误 backend 名在即故障在）：
以「注入生效确认」为时点锚（注入验证第 1-2 条通过 = 生效），生效后一次 `time_wait 30`（间隔 = 2 × 传播上限：controller reload 秒级 → 30s 最小复测窗，按 SKILL.md「持续性采样间隔 per-case 推导」），到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：Ingress backend 字段仍为错误值（同注入验证第 1 条）
2. 行为复查：入口访问仍 503（同注入验证第 2 条）
3. 稳定性复查：后端 Pod RESTARTS 无增长、Endpoints 仍有 IP（排除故障向 Service 层蔓延的误判）

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原 backend 名（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动将 backend 名还原为基线；演练提前结束时由 Agent 主动执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用。json patch 按字段精确替换，天然规避 resourceVersion 乐观锁问题）：
   ```bash
   kubectl patch ingress <ingress-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/rules/0/http/paths/0/backend/service/name","value":"<步骤1基线service名>"}]'
   ```
2. 等待 controller reload（秒级传播窗），入口流量自动恢复——**无需重启任何 Pod 或 Service**

## 恢复验证
1. 白盒确认 backend 已还原基线：
   ```bash
   kubectl get ingress <ingress-name> -n <namespace> -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}'
   ```
   预期与演练步骤 1 基线一致
2. 行为确认——入口访问恢复基线响应码（reload 传播窗内立即 curl 可能仍 503，等 10s 再采）：
   ```bash
   curl -s -o /dev/null -w '%{http_code}' -H 'Host: <ingress-host>' http://<入口地址>/<path>
   ```
   预期与基线一致（200 或业务正常码）
3. 确认载体 restore.log 取证（`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`——定时器 fire 的直接证据，PATCH 响应回显）

## 基准事实
- **根因**：Ingress 规则的 backend service 名被改为不存在的名字（人为误操作/GitOps 流水线错误/服务重命名遗漏同步），ingress controller 找不到后端 Endpoints
- **必现现象**：入口访问该 host/path 返回 503（ingress-nginx 默认码）；Ingress backend 字段为错误值；Service/Endpoints/后端 Pod 全程健康；绕过 Ingress 直访 Service/Pod 仍可达
- **传播窗事实**：ingress controller watch Ingress 变更后 reload 为秒级——注入与恢复的行为判据都要计入 ~10s reload 窗，窗内结果不作裁决依据
- **作用域边界**：仅被改的那条 host/path 路由受影响；同 Ingress 其他 path、其他 Ingress、Service 层直访路径均不受影响

## 注意事项
- chaosblade 无 Ingress/入口配置类故障靶点，本用例为 kubectl-native 专属注入
- **非 ingress-nginx controller 的响应码差异**：traefik/haproxy-ingress/云厂商 ALB Ingress 对空后端可能返回 502/404 而非 503——判据以「响应码 ≠ 基线且为 5xx/4xx 错误族」为准，不硬编码 503
- 云厂商托管 Ingress（ALB/GCE）的 controller reload 可能慢至分钟级——传播窗按实测放大，持续性采样间隔同步放大（2 × 实测窗）
- 注入的假 service 名带 `drill-` 前缀（任务侧资产归属登记依据）——本 case 不创建任何新对象，假名只是字符串值，无残留清理义务
- 若 Ingress 带 TLS，curl 验证用 `https://` + `-k`（证书校验非本 case 判据；TLS 证书形态见同目录 TLS证书过期 case）
- 多 rule/多 path Ingress 只改一条 path 即构成有效故障（最小注入面纪律）——严禁为"效果明显"改全部 path（爆炸半径无谓扩大）
