---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原镜像地址与 maxUnavailable），
# 路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+
# 验权+武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/
# RBAC 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 载体由 faultdrill_assemble_carrier 程序化建栈、结构性自拆（fail-closed 路径 + 恢复句柄清理链 + 武装定时器自删 → apiserver ownerRef GC 级联）；降级路径不自建可自撤 RBAC 栈，正文无需传输载体自撤授权类知识。
carrier_teardown: assembler-managed
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch Deployment 模板的 image/maxUnavailable 属受害者域内，名字匹配
# 放行），无跨对象写条目；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名
# drill-rc-<hash> 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌检查
# （RBAC 从 restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），
# 不经 LLM kubectl 写面，无立法条目。
---

**用例名称** 拉取超时 导致 Pod_镜像拉取失败

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原镜像地址与 maxUnavailable；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（不再作为恢复通道），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace
  path: /spec/template/spec/containers/0/image
  value: <黑洞 registry 地址，如 192.0.2.1:5000/drill/pull-timeout:probe>   # RFC 5737 文档保留段，公网不路由、TCP connect 挂起→超时
# maxUnavailable 100% 并入注入域（正文步骤 2 的防滚动死锁操作——注入期新 Pod 永不
# Ready，默认 MU 下 K8s 不会终止旧 Pod，滚动死锁）
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: "100%"
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace
  path: /spec/template/spec/containers/0/image
  value: <演练步骤 1 记录的基线镜像地址>
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: <步骤2记录的基线值>
durationSeconds: <duration>               # TTL 从武装时刻起算；须覆盖 connect 超时×重试+退避+观察窗（比即时失败形态宽，见注意事项 duration 契约）
```

- 多容器 Pod 调整 containers/N 索引至目标容器；滚动由 patch 自动触发。
- **黑洞地址选型（本形态与「镜像不存在」的唯一注入差异）**：镜像地址须指向一个**不可达且被静默丢弃**的 registry 端点——TCP SYN 无响应（既非 RST 拒绝、也非 ICMP 不可达），connect 挂起至超时。RFC 5737 文档保留段（TEST-NET-1 `192.0.2.0/24`）是标准黑洞：公网不路由、无主机应答。用**字面 IP** 而非域名——域名若解析失败（NXDOMAIN）会在 resolve 阶段即时返回 `no such host`，退化为「镜像不存在」签名而非超时。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

## 故障现象
1. Pod 状态停留在 ImagePullBackOff 或 ErrImagePull
2. Pod Event 显示镜像拉取**超时**（区别于既有形态的即时错误响应）：containerd 通道（k8s ≥1.24 主流形态）为 `failed to resolve reference "192.0.2.1:5000/...": dial tcp 192.0.2.1:5000: i/o timeout`（或 `context deadline exceeded`）；docker 通道为 `Get "https://192.0.2.1:5000/v2/": dial tcp ...: i/o timeout`（或 `net/http: request canceled while waiting for connection (Client.Timeout exceeded while awaiting headers)`）
3. 拉取尝试长时间无响应（TCP connect 挂起），失败出现在超时窗口之后而非注入后即刻——这是与凭证/标签/篡改三形态（registry 可达且即时返回错误响应）的根本区别

## 资源准备
1. 确认应用 A/B 所在节点正常运行，且节点当前可正常访问其镜像仓库（基线健康，排除既有网络问题干扰超时判读）
2. **黑洞地址环境适配（硬前置）**：预校验选定的黑洞地址在目标集群节点上确实表现为**超时**而非**立即拒绝**——若在节点上探测该地址立即返回 `connection refused`（RST）或 `no route to host`（ICMP 不可达）而非挂起到超时，说明环境对该地址即时拒绝、签名会退化，须换一个确定被静默丢弃的地址
3. 若集群配置了全局 registry mirror（containerd `hosts.toml` / dockerd `registry-mirrors`），须确认黑洞地址不会被 mirror 重写——否则拉取经 mirror 即时返回 404，退化为「镜像不存在」签名而非超时

## 演练步骤
（主路径 = 基线捕获（步骤 1-2 的读取部分，restorePatches 的基线值来源，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、patches=镜像地址改黑洞+maxUnavailable 100%、restorePatches=基线还原、duration_seconds=<duration>），注入+武装+readback 工具内同步完成——步骤 2 的手动置 100% 在主路径下由配方注入域承载，无需单独执行；步骤 2-4 的手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 定位应用 A 的 Deployment，并记录镜像基线（基线捕获：Agent 读取输出并记录原始镜像地址，恢复时使用；多容器 Pod 请调整 containers 索引至目标容器）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].image}'
   ```
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. **武装定时自恢复**（恢复命令幂等：定时器到期自动还原为主，Agent 在演练结束时主动执行
   同一条命令兜底，定时器迟到重复执行无副作用。定时器 shell 逻辑必须作为 `kubectl exec` 载体
   载荷派发——直接以 `sh -c '…'` 作为顶层命令派发会被命令守卫拦截（unknown_binary: sh），
   载体内 `sh -c` 同时解决 exec-form 通道不解释裸 `( sleep … ) &` 语法的问题；载体 Pod 为
   多副本时无法可靠终止定时器，故不设 pidfile。载体 Pod 选集群内带 kubectl 且有足够
   RBAC 权限的常驻 Pod（如演练工具 Pod）。恢复脚本落盘形态按 recovery-carrier.md 第七节
   「四档定案表」按明文字节数查表选定（Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符或手算 b64 长度）；
   `<duration>` 需覆盖 connect 超时×重试+滚动更新+观察窗口，比即时失败形态更宽）：
   ```bash
   # 武装定时自恢复（"注入恢复"第 1 步命令按第七节四档表选定落盘形态；下行为旧契约历史形态示例，勿套用）
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-pulltimeout.sh; ( sleep <duration>; sh /tmp/blade-restore-pulltimeout.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-pulltimeou[t]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
4. 修改应用 A 的镜像地址为黑洞 registry 地址（如 `192.0.2.1:5000/drill/pull-timeout:probe`）
5. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
6. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段。主路径下此项随载体配方 restorePatches 由载体 TTL 还原，无需手动执行）
7. 观察 Pod 的镜像拉取状态——**须等待 connect 超时窗口过后**再判读（超时签名不会即刻出现，见注入验证第 2 条采集时机）

## 注入验证
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 永不 Ready（镜像拉取挂起超时，故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 处于 ImagePullBackOff）
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 状态停留在 ImagePullBackOff 或 ErrImagePull——两者是同一故障的先后渲染（首拉失败即 ErrImagePull，进入退避后转为 ImagePullBackOff），出现任一即判（不是仅一个新 Pod，而是全部副本）。**采集时机（本形态专属）**：超时签名出现在 connect 超时窗口之后（containerd/docker 默认 connect 超时约数十秒，叠加拉取退避），注入后即刻查询可能只见 `ContainerCreating`（拉取挂起中、尚未失败）——须等待至少一个 connect 超时周期后再判读，不可因“尚未见 ImagePullBackOff”而误判注入失败
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 包含**超时/挂起**语义的拉取错误：containerd 通道 `dial tcp 192.0.2.1:5000: i/o timeout` / `context deadline exceeded`；docker 通道 `Client.Timeout exceeded while awaiting headers` / `i/o timeout`。**判读锚点**：错误须为超时语义（timeout / deadline / canceled），而非即时响应类（`manifest not found`/404=镜像不存在、`unauthorized`/401/403=凭证、digest mismatch=篡改）——这是本形态区别于既有三形态的核心判据；若见 `connection refused` / `no route to host`（立即拒绝而非挂起），说明黑洞地址被环境即时拒绝、签名退化，须换地址重注入

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原镜像地址与 maxUnavailable 基线（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动将镜像地址还原为步骤 1 基线；演练提前结束时由 Agent
   主动执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用。json patch 按字段精确替换，
   天然规避 resourceVersion 乐观锁问题，也不会像 apply 三方合并那样保留注入后新增的字段）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"<步骤1基线镜像地址>"}]'
   ```
2. 等待 Pod 滚动更新完成

## 恢复验证
1. 查看 Pod 状态，确认恢复为 Running
2. 查看 Pod Event，确认基线镜像地址拉取成功（不再有超时错误）

## 基准事实
- **根因**：Pod 配置的镜像地址指向一个不可达（黑洞）的 registry 端点——TCP connect 无响应而挂起，拉取在超时窗口后失败（区别于 registry 可达但即时返回错误响应的形态：镜像不存在=404/`manifest not found`、凭证=401/403/`unauthorized`、篡改=digest mismatch）
- **必现现象**：Pod 状态为 ImagePullBackOff 或 ErrImagePull；拉取错误为超时语义，按运行时通道二分：containerd 通道 `dial tcp <黑洞地址>: i/o timeout` / `context deadline exceeded`，docker 通道 `Client.Timeout exceeded while awaiting headers`；失败出现在 connect 超时窗口之后而非注入即刻

## 注意事项
- 黑洞地址必须被**静默丢弃**（SYN 无响应）才产生超时签名；被即时拒绝（RST→`connection refused` / ICMP→`no route to host`）会退化为即时失败，非本形态——注入前按资源准备第 2 条预校验
- 用**字面 IP**（RFC 5737 TEST-NET）而非域名作黑洞：域名解析失败会在 resolve 阶段即时返回 `no such host`，退化为「镜像不存在」签名
- 全局 registry mirror（containerd `hosts.toml` / dockerd `registry-mirrors`）会重写拉取目标使黑洞失效——注入前确认地址不被 mirror 代理
- **与「镜像不存在或标签错误」的区别**：后者 registry 可达、resolve 阶段即时返回 404/`manifest not found`；本形态 registry 不可达、connect 挂起至超时。两者注入机制同为 patch Deployment image + maxUnavailable 100%，但故障签名（即时 404 vs 延迟 timeout）与判据采集时机（即刻可见 vs 须等 connect 超时窗口）不同
- **备选注入手段（不作首选）**：节点侧 tc netem 网络延迟使**真实** registry 拉取变慢至超时——爆炸半径覆盖整节点所有流量，且是否触发超时取决于镜像体积/延迟量/拉取超时三方博弈（非确定性）；本 case 主用 API 平面黑洞地址（确定性、零节点侧爆炸半径、逆 patch 幂等恢复），故不采用节点侧延迟
- **duration 契约**：TTL 须覆盖 connect 超时 × 重试次数 + 退避间隔 + 滚动更新 + 观察窗，比即时失败形态（镜像不存在）的窗口更宽——窗口过窄会在超时签名显现前即恢复，导致判据采集不到
