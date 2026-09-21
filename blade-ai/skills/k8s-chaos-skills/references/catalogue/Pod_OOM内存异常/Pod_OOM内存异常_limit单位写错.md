---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 memory limits），路由进程序化
# 恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch Deployment 模板的 resources/maxUnavailable 属受害者域内，名字
# 匹配放行），无跨对象写条目；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名
# drill-rc-<hash> 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌检查
# （RBAC 从 restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），
# 不经 LLM kubectl 写面，无立法条目。
---

**用例名称** limit单位写错 导致 Pod_OOM内存异常

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 memory limits 与 maxUnavailable；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
# 基线有 resources → replace 整对象（limits/requests 一并携带注入值，正文步骤 4 B80
# 对称律二分）；基线无 resources → 改 add 整对象（键级 replace 对不存在的键报 path 错误）
- op: replace
  path: /spec/template/spec/containers/0/resources/limits/memory
  value: <错误单位的值（如把 Mi 写成 M 或 Ti）>
# maxUnavailable 100% 并入注入域（正文步骤 2 的防滚动死锁操作——注入期新 Pod 永不
# Ready，默认 MU 下 K8s 不会终止旧 Pod，滚动死锁）
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: "100%"
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
# 基线有 resources → replace 整对象回步骤 1 基线 JSON；基线无 → remove 整键
- op: replace
  path: /spec/template/spec/containers/0/resources/limits/memory
  value: <注入前记录的基线值>
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: <步骤2记录的基线值>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 多容器 Pod 调整 containers/N 索引至目标容器；滚动由 patch 自动触发。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 启动后立即异常退出，状态为 CrashLoopBackOff
2. Pod 的 lastState 显示 reason: OOMKilled，或新 Pod 卡在 ContainerCreating（极小 limit 在 cgroup v2 下的实际形态，见注入验证第 3 条）
3. 容器 memory limit 值极小（如 100m = 0.1 字节），应用启动即超限或无法启动

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认应用 A 的正常内存使用量（如 200Mi 以上）

**演练步骤**（主路径 = 基线捕获（步骤 1-2 的读取部分，restorePatches 的基线值来源，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、patches=resources 错误单位注入+maxUnavailable 100%、restorePatches=基线还原、duration_seconds=<duration>），注入+武装+readback 工具内同步完成——步骤 2 的手动置 100% 在主路径下由配方注入域承载，无需单独执行；步骤 2-4 的手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 记录应用 A 当前的 resources 配置（基线捕获：Agent 读取输出并记录 JSON，恢复时使用）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].resources}'
   ```
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. **武装定时自恢复**（恢复命令幂等：定时器到期自动还原为主，Agent 在演练结束时主动执行
   同一条命令兜底，定时器迟到重复执行无副作用。定时器 shell 逻辑必须作为 `kubectl exec` 载体载荷派发——直接以
   `sh -c '…'` 作为顶层命令派发会被命令守卫拦截（unknown_binary: sh）；载体 Pod 为多副本
   时无法可靠终止定时器，故不设 pidfile。恢复脚本落盘形态按 recovery-carrier.md 第七节「四档定案表」
   按明文字节数查表选定（Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符或手算 b64 长度）；
   `<duration>` 需覆盖滚动更新与观察窗口）。
   载体 Pod 选集群内带 kubectl 且有足够 RBAC 权限的常驻 Pod（如演练工具 Pod）；集群无常驻带
   kubectl 的 Pod 时按**恢复载体标准件**自建四件套（Role 按标准件第二节推导表取
   `--verb=get,patch --resource=deployments`——verb 清单按钦定恢复形态推导，换形态时以恢复脚本
   实际载荷动词为准重建，见第二节形态无关总则与第三节写动词 SSAR 对账；武装用第四节紧凑变量 REST 形态，恢复
   PATCH 的 Content-Type 用 application/json-patch+json——resources 位于 containers 数组
   元素内部（merge-patch 对数组是整组替换语义会抹掉容器），json patch 精确路径 replace 整对象
   与注入同型满足铁律 1 对称律；resources+MU 双 op 可并入单条 json-patch 数组一条 curl 完成
   （#45 实证））：
   ```bash
   # 武装定时自恢复（"注入恢复"第 1 步命令按第七节四档表选定落盘形态；标准件 REST 路径下恢复明文
   # （紧凑变量形态一条 json-patch 双 op PATCH，含 token/CA 变量赋值）~450-570B ⇒ 档②；#45 实测
   # 形态=双文件分离——脚本 ~250B + patch.json 206B 各自 quoted heredoc 落盘、curl `-d @/tmp/patch.json`
   # 引用 payload（零反斜杠转义、payload 文件内容零单引号字符——载荷引号保真律免疫；下行为旧契约历史形态示例，勿套用）
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-oom.sh; ( sleep <duration>; sh /tmp/blade-restore-oom.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-oo[m]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
4. 修改应用 A 的 Deployment，将 memory limit 单位写错（注入命令形态按基线二分，与恢复分支
   键级对称（B80 对称律，见 recovery-carrier.md 第七节铁律 1）：基线**有** resources → json patch
   **replace 整对象**（limits/requests 一并携带注入值）；基线**无** resources → json patch
   **add 整对象**（replace 对不存在的键报 path 错误；add 与恢复 remove 配对是键级对称）：
   ```yaml
   resources:
     limits:
       memory: "100m"    # 错误！100m = 0.1 字节（milli），应为 100Mi
     requests:
       memory: "100m"
   ```
   注入值选型判型：进程实际内存超限才会触发形态 A（OOMKilled）——轻量骨架靶（sleep 类，实际
   内存 ~1MB）取 10Mi 不会 OOM，必须取 **100m 走形态 B**（CRI 取整 0 字节 → cgroup
   memory.max=0 → 容器创建失败，故障在 cgroup 配置层与进程内存无关，注入验证第 3 条形态 B）；
   有真实内存负载的应用取 10Mi 可走形态 A（lastState OOMKilled 判据更直）；形态预判以注入后实测为准
   ——容器内 `/sys/fs/cgroup` 视图不可靠（#45：容器内探测显示 v1 布局，节点实际行为是 v2 语义
   memory.max=0 → 形态 B 兑现；bind-mount 视角与节点运行时 cgroup 语义可不一致），勿据容器内探测改判型
   注意：在 Kubernetes 中，`m` 表示 milli（千分之一），`100m` = 0.1 字节；正确应为 `Mi`（Mebibyte）。patch 提交时 API server 在响应头携带 `Warning: fractional byte value "100m" is invalid, must be an integer`（不影响 patch 生效）——但 **kubectl CLI 不渲染 Warning 响应头**（#45 实证：Warning 未在输出中出现），勿以「未见 Warning」判失败；单位错误的权威确认信号是 **spec 持久化值**（Deployment 模板与各 Pod spec 中 `100m` 原样落地、无 LimitRange/ResourceQuota 改写即注入生效）
5. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
6. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段；置 100% 的数学必要性：2 副本 × 25% ⇒ floor(0.5)=0 个不可用，永不 Ready 的新 Pod 下滚动死锁——注入期新 Pod 本就永不 Ready，默认 MU 下 K8s 不会终止旧 Pod。主路径下此项随载体配方 restorePatches 由载体 TTL 还原，无需手动执行）
7. 观察 Pod 启动行为

**注入验证**：
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod CrashLoop 或卡 ContainerCreating（故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 处于故障形态）
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 的故障形态到位（不是仅一个新 Pod，而是全部副本）：形态 A（OOMKilled）为 CrashLoopBackOff 且 RESTARTS 已高于注入前读数（单调递增计数器，高于基线即重启已发生，无需等待持续增长——状态标签是重启的渲染）；形态 B（ContainerCreating 卡死）容器未创建、**无 RESTARTS 增长**（重启计数无从累加，判据见第 3 条形态 B——勿把「无重启」误判为注入失败）
3. 按 limit 取整结果分形态验证（两种形态均已复现）：
   - limit 为可用级小值（如 10Mi，容器可创建但启动即超限）：执行 `kubectl get pod <pod-name> -o jsonpath='{.status.containerStatuses[0].lastState}'`，确认 reason 为 OOMKilled
   - limit 极小（如 100m，CRI 取整为 0 字节、cgroup v2 memory.max=0）：容器无法创建，新 Pod 卡在 ContainerCreating，Events 显示 `FailedMount ... no space left on device`。**该报错是误导信号**：projected volume 以 tmpfs 承载、写入计入 Pod memcg，memory.max=0 时 tmpfs 页分配被拒而返回 ENOSPC，与节点磁盘空间无关（节点磁盘仅用 14% 仍稳定复现；此时 lastState 为空，查 OOMKilled 必然查不到，不是注入失败）
4. 执行 `kubectl describe pod <pod-name>`，确认 limits.memory 为极小值
5. 确认容器启动后立即异常退出（运行时间极短或无法启动）

**注入恢复**（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原 resources 基线与 maxUnavailable（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动将 resources 还原为步骤 1 基线；演练提前结束时由
   Agent 主动执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用——用基线 JSON 整体
   replace resources 对象，limits/requests 一并还原。json patch 按字段精确替换，天然规避
   resourceVersion 乐观锁问题，也不会像 apply 三方合并那样保留注入引入的错误值）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources","value":<步骤1基线JSON>}]'
   # 原本无 resources 时改用 remove
   ```
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Running 且不再重启（恢复代新 Pod RESTARTS 冻结即为「不再重启」；形态 B 案注入期本无重启，判据退化为「新 Pod 创建成功转 Running」——ContainerCreating 解除本身就是恢复生效的最强证据）
2. 确认容器正常运行，内存使用率在合理范围（`kubectl top pod <pod-name> -n <namespace>` 单行判据，截断免疫）
3. 确认应用 A 服务正常
4. **恢复判据锄基线（勿从恢复命令的字段域派生）**：jsonpath 读回**整个 resources 子树**
   （`-o jsonpath='{.spec.template.spec.containers[0].resources}'`）与步骤 1 基线逐字段比对——
   **replace 分支**逐字段全比（勿只查恢复命令写过的字段——判据域跟着恢复命令窄化是 B80 验证层
   失效机制）；**remove 分支**（基线本无 resources）判据是读回**空输出**（jsonpath 对不存在的键
   返回空串——空即键已移除，任何非空输出都是残留）；另读 `kubectl get rs -n <namespace> -l <label>`
   确认**当前代 RS hash 回到注入前基线值**（RS hash 是 pod template 全字段相等性的免费校验和——
   resources 键还原/移除且 MU 还原后 template 与基线逐字段相等 ⇒ hash 必回基线值，hash 不回
   即有残留不必猜哪个字段）。verify 窗口先于 timer fire 结束属常态（效果证据在窗口存活期采集），
   本条判据由带外终验兑现（timer fire 后读子树 + RS hash；Agent 收尾报告须注明恢复正确性待带外终验确认）

**基准事实**：
- **根因**：memory limit 单位写错（如 `100m` 而非 `100Mi`），导致 limit 值极小，容器启动后内存使用立即超过 limit 被 OOMKill，或在 cgroup v2 环境下因内存配置过小无法启动
- **必现现象**：Pod 异常退出（OOMKilled）或无法创建（ContainerCreating 卡死 + FailedMount ENOSPC，极小 limit 在 cgroup v2 下的形态）；limits.memory 值不合理（如 100m）；容器运行时间极短或无法启动
