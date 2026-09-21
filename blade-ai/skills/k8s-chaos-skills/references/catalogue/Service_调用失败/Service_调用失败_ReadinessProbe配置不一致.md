---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 readinessProbe 配置），路由进程序化
# 恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch readinessProbe/maxUnavailable 属受害者域内，名字匹配放行），无
# 跨对象写条目；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash>
# 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌检查（RBAC 从
# restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），不经
# LLM kubectl 写面，无立法条目。
---

**用例名称** ReadinessProbe配置不一致 导致 Service_调用失败

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 readinessProbe 配置；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace
  path: /spec/template/spec/containers/0/readinessProbe
  value: <与 Service 语义不一致的探针配置（如阈值/端口错配）>
# MU 100% 并入注入域（防滚动死锁：注入期新 Pod 不 Ready，默认策略下 K8s 不会
# 终止旧 Pod，滚动更新死锁——与 PVC/OOM case 既有模式一致；原本无探针时该
# op 改 add）
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: "100%"
restorePatches:                           # 恢复域：载体 TTL 到点执行；Agent 死亡后 recover 重放同源
- op: replace
  path: /spec/template/spec/containers/0/readinessProbe
  value: <注入前记录的基线探针配置（原本无探针时改 remove 该路径）>
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: <注入前记录的基线 maxUnavailable 值>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 多容器 Pod 调整 containers/N 索引至目标容器；滚动由 patch 自动触发。
- **MU 100% 的承载分工**：主路径下步骤 2 的手动置 100% 由配方注入域承载（一次 patch 面内原子并入，无需单独执行），其还原随 restorePatches 由载体 TTL 还原——窗口内维持 100%（故障态本身，无其他滚动触发源时无增量风险），不再泄漏到恢复期之后；降级路径下步骤 2/6 的手动序列照常执行。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 状态为 Running 但 READY 为 0/1
2. Service 的 Endpoints 列表为空或逐渐减少
3. Readiness Probe 持续失败，Pod 从 Service 后端移除

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service
2. 确认应用 A 实际监听的端口和健康检查路径

**演练步骤**（主路径 = 步骤 1 基线捕获（readinessProbe 配置 JSON——restorePatches 基线值来源，两条路径共用）→ 调 faultdrill_assemble_carrier（参数取自载体配方：target_kind=deployment、target_name=<deployment-name>、target_namespace=<namespace>、patches=…（含 MU 100% 注入域）、restorePatches=…、duration_seconds=<duration>），注入+武装+readback 工具内同步完成——步骤 2 的手动置 MU 100% 由配方注入域承载，无需单独执行 → 步骤 5 等待滚动更新完成（两路径共用）→ 步骤 6 的 MU 还原随 restorePatches 由载体 TTL 承载，无需手动执行。步骤 3 的手动定时器序列与步骤 4 的 strategic patch 仅当装配器 fail-closed 报告不可用时作降级兜底——降级路径下步骤 2/4/6 手动序列照常）：
1. 记录应用 A 当前的 readinessProbe 配置（基线捕获：Agent 读取输出并记录 JSON，恢复时使用；
   原本无 readinessProbe 时输出为空）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].readinessProbe}'
   ```
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）——**降级路径形态；主路径下此项由载体配方注入域承载（restorePatches 基线值来源 = 本步的读取部分，两条路径共用），无需单独执行**：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. **武装定时自恢复**（**降级兜底形态——主路径下由装配器载体 TTL 承载，无需手动武装**；恢复命令幂等：定时器到期自动还原为主，Agent 在演练结束时主动执行
   同一条命令兜底，定时器迟到重复执行无副作用。定时器 shell 逻辑必须作为 `kubectl exec` 载体
   载荷派发——直接以 `sh -c '…'` 作为顶层命令派发会被命令守卫拦截（unknown_binary: sh），
   载体内 `sh -c` 同时解决 exec-form 通道不解释裸 `( sleep … ) &` 语法的问题；载体 Pod 为
   多副本时无法可靠终止定时器，故不设 pidfile。载体 Pod 选集群内带 kubectl 且有足够
   RBAC 权限的常驻 Pod（如演练工具 Pod）。恢复脚本落盘形态按 recovery-carrier.md 第七节
   「四档定案表」按明文字节数查表选定（Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符）；
   `<duration>` 需覆盖滚动更新与观察窗口）：
   ```bash
   # 武装定时自恢复（"注入恢复"第 1 步命令按基线选定 replace/remove 后按第七节四档表选定落盘形态；
   # 下行为旧契约历史形态示例，勿套用）
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-readiness.sh; ( sleep <duration>; sh /tmp/blade-restore-readiness.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-readines[s]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
4. 修改应用 A 的 Deployment，将 readinessProbe 路径或端口设置为与实际不一致：
   ```yaml
   readinessProbe:
     httpGet:
       path: <应用不提供的路径>   # 占位符：必须与应用实际健康检查路径不同；/non-existent-health-path 仅为示例写法
       port: <应用不监听的端口>   # 占位符：必须先探测应用实际监听端口后选一个未监听端口；9999 仅为示例写法
     periodSeconds: 5
     failureThreshold: 3
   ```
   （本用例的故障机制就是探针指向错误端点：注入前必须先探测目标应用实际监听端口与健康检查路径，再选一个确定未监听/不存在的值；不得照抄示例值，若示例端口恰被应用监听则故障不生效）
5. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
6. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段；跳过此步会导致 100% 泄漏到故障恢复之后，始终要在注入验证完成后第一时间还原）——**降级路径形态；主路径下此项随载体配方 restorePatches 由载体 TTL 还原，无需手动执行**
7. 观察 Pod Ready 状态和 Service Endpoints 变化

**注入验证**：
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`** ——
   注入期新 Pod 永不 Ready（故障本身），`rollout status` 等待 available 副本必然超时报错
   （复现），按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是
   `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=1（或旧 Pod
   名消失、新 Pod Running 0/1）。对照：恢复路径（探针还原后）`rollout status` 正常返回
   `successfully rolled out`，可作恢复完成判据
2. 执行 `kubectl get pods`，确认**所有** Pod 状态为 Running 但 READY 列显示 0/1（不是仅一个新 Pod 0/1，而是全部副本都 0/1）
3. 执行 `kubectl get endpoints <service-name>`，确认 Endpoints 列表**为空**（无子集），而非仅新 Pod 不在列表中
4. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 `Readiness probe failed`
5. 向 Service 发送请求，确认返回 connection refused 或超时。注意：connection reset by peer 可能是应用自身行为而非故障效果，不可作为故障生效的充分证据；ipvs 模式无后端时为 Connection refused（kube-proxy 对无 Endpoints 的 ClusterIP 直接 reject）

**注入恢复**（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原（restorePatches 的探针 replace + MU 基线 replace 由载体内 timer 到点执行，恢复自动触发回滚滚动；fire 证据落载体 /tmp/restore.log + 任务台账 recovery_handle）；演练提前结束时 blade-ai recover 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动将 readinessProbe 还原为步骤 1 基线；演练提前结束时由
   Agent 主动执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用——基线非空时 json patch
   replace 回原值 JSON，原本无探针时 remove。json patch 按字段精确替换，天然规避
   resourceVersion 乐观锁问题，也不会像 apply 三方合并那样保留注入后新增的字段）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe","value":<步骤1基线JSON>}]'
   # 原本无 readinessProbe 时改用 remove：
   # kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
   #   -p='[{"op":"remove","path":"/spec/template/spec/containers/0/readinessProbe"}]'
   ```
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod READY 为 1/1
2. 执行 `kubectl get endpoints <service-name>`，确认 Pod 重新加入 Endpoints
3. 向 Service 发送请求，确认服务恢复正常

**基准事实**：
- **根因**：Readiness Probe 的路径或端口与应用实际监听不一致，Probe 持续失败导致 Pod 被标记为 Not Ready，从 Service Endpoints 中移除
- **必现现象**：Pod Running 但 Not Ready（0/1）；Endpoints 为空；Events 显示 Readiness probe failed
