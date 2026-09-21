---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原镜像地址），路由进程序化
# 恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch Deployment 模板的 image/maxUnavailable 属受害者域内，名字匹配
# 放行），无跨对象写条目；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名
# drill-rc-<hash> 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌检查
# （RBAC 从 restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），
# 不经 LLM kubectl 写面，无立法条目。
---

**用例名称** 镜像不存在或标签错误 导致 Pod_镜像拉取失败

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原镜像地址与 maxUnavailable；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace
  path: /spec/template/spec/containers/0/image
  value: <不存在的镜像名称或标签>
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
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 多容器 Pod 调整 containers/N 索引至目标容器；滚动由 patch 自动触发。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 状态停留在 ImagePullBackOff 或 ErrImagePull
2. Pod Event 显示镜像拉取失败，错误信息包含镜像不存在类错误：docker 通道为
   `manifest for xxx not found`；containerd 通道（k8s ≥1.24 主流形态）为
   `failed to resolve reference ... [404 Not Found]`（resolve 阶段直接 404，无 manifest 字样，containerd 集群形态）

**资源准备**：
1. 确认应用 A/B 所在节点正常运行
2. 确认节点可正常访问镜像仓库

**演练步骤**（主路径 = 基线捕获（步骤 1-2 的读取部分，restorePatches 的基线值来源，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、patches=镜像地址改错+maxUnavailable 100%、restorePatches=基线还原、duration_seconds=<duration>），注入+武装+readback 工具内同步完成——步骤 2 的手动置 100% 在主路径下由配方注入域承载，无需单独执行；步骤 2-4 的手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 定位应用 A 的 Deployment，并记录镜像基线（基线捕获：Agent 读取输出并记录原始镜像地址，
   恢复时使用；多容器 Pod 请调整 containers 索引至目标容器）：
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
   `<duration>` 需覆盖滚动更新与观察窗口）：
   ```bash
   # 武装定时自恢复（"注入恢复"第 1 步命令按第七节四档表选定落盘形态；下行为旧契约历史形态示例，勿套用）
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-badimage.sh; ( sleep <duration>; sh /tmp/blade-restore-badimage.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-badimag[e]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
4. 修改应用 A 的镜像地址为不存在的镜像名称或标签
5. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
6. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段。主路径下此项随载体配方 restorePatches 由载体 TTL 还原，无需手动执行）
7. 观察 Pod 的镜像拉取状态

**注入验证**：
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 永不 Ready（镜像不存在，故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 处于 ImagePullBackOff）
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 状态停留在 ImagePullBackOff 或 ErrImagePull——两者是同一故障的先后渲染（首拉失败即 ErrImagePull，进入退避后转为 ImagePullBackOff），出现任一即判（不是仅一个新 Pod，而是全部副本）
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 包含镜像不存在相关信息：docker 通道为
   `manifest for xxx not found`；containerd 通道为 `failed to resolve reference ... [404 Not Found]`
   （判读时不要按 manifest 字样硬匹配，两种通道措辞不同，containerd 集群报 404）

**注入恢复**（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原镜像地址与 maxUnavailable 基线（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动将镜像地址还原为步骤 1 基线；演练提前结束时由 Agent
   主动执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用。json patch 按字段精确替换，
   天然规避 resourceVersion 乐观锁问题，也不会像 apply 三方合并那样保留注入后新增的字段）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"<步骤1基线镜像地址>"}]'
   ```
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 查看 Pod 状态，确认恢复为 Running
2. 查看 Pod Event，确认镜像拉取成功

**基准事实**：
- **根因**：Pod 配置的镜像名称拼写错误或标签在镜像仓库中不存在
- **必现现象**：Pod 状态为 ImagePullBackOff 或 ErrImagePull；拉取错误按运行时通道二分：docker 通道 `manifest for xxx not found`，containerd 通道 `failed to resolve reference ... [404 Not Found]`
