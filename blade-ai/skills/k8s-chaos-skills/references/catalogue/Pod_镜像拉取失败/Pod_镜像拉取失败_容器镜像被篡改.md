---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原镜像引用与 maxUnavailable），
# 路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+
# 验权+武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/
# RBAC 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch Deployment 模板的 image/maxUnavailable 属受害者域内，名字匹配
# 放行），无跨对象写条目——篡改为纯引用值替换，无道具对象创建；装配器载体栈
# （SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由工具内程序化构建
# ——构造保证 + fail-closed 内嵌检查（RBAC 从 restorePatches 同源推导禁通配、
# SA 真实 token 验权 403 中止+清理），不经 LLM kubectl 写面，无立法条目。
---

**用例名称** 容器镜像被篡改 导致 Pod_镜像拉取失败

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原镜像引用与 maxUnavailable；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
# 篡改形态 = 「替换 tag」：保留仓库地址与镜像名，只把 tag 换成仓库中不存在的篡改值
# （如基线 tag 加 `-tampered` 后缀构成的新 tag，仍是合法单冒号引用）；禁止在完整基线
# 引用后拼接后缀（拼出双冒号非法引用 → InvalidImageName 误判，见下方第一条要点）
- op: replace
  path: /spec/template/spec/containers/0/image
  value: <registry>/<repo>:<篡改 tag（仓库中不存在）>
# maxUnavailable 100% 并入注入域（正文步骤 2 的防滚动死锁操作——注入期新 Pod 永不
# Ready，默认 MU 下 K8s 不会终止旧 Pod，滚动死锁）
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: "100%"
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace
  path: /spec/template/spec/containers/0/image
  value: <演练步骤 1 记录的基线镜像完整引用>
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: <步骤2记录的基线值>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- **篡改引用必须用「替换 tag」形态**（`<registry>/<repo>:<不存在 tag>`）：旧形态在原始镜像后追加后缀（`<原始镜像>-fault-injection` 类）——基线镜像已含 tag（生产常态）时拼出双冒号引用（如 `busybox:1.33:non-existent-tag`），K8s 直接拒绝为非法镜像名，新 Pod 呈 **InvalidImageName**（Event 报 `invalid reference format`）而非镜像拉取失败，按判据会误判（复现）；恢复值同坑——用基线捕获的完整引用，不要拼 `<原始镜像>:<原始标签>`。
- 多容器 Pod 调整 containers/N 索引至目标容器；滚动由 patch 自动触发。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

## 故障现象
1. Pod 状态停留在 ImagePullBackOff 或 ErrImagePull
2. Pod Event 显示镜像拉取失败，错误信息包含镜像不可拉取类错误：docker 通道为
   `manifest for xxx not found`；containerd 通道（k8s ≥1.24 主流形态）为
   `failed to resolve reference ... [404 Not Found]`（resolve 阶段直接 404，无 manifest 字样，containerd 集群形态）
3. Deployment 模板中的容器镜像引用呈被篡改的值（保留仓库地址与镜像名，tag 被替换为仓库中不存在的篡改值）

## 资源准备
1. 确认应用 A 所在节点正常运行
2. 确认节点可正常访问镜像仓库

## 演练步骤
（主路径 = 基线捕获（步骤 1-2 的读取部分，restorePatches 的基线值来源，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、patches=镜像引用替换为篡改 tag+maxUnavailable 100%、restorePatches=基线还原、duration_seconds=<duration>），注入+武装+readback 工具内同步完成——步骤 2 的手动置 100% 在主路径下由配方注入域承载，无需单独执行；步骤 2-4 的手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 定位应用 A 的 Deployment，并记录镜像基线（基线捕获：Agent 读取输出并记录原始镜像完整引用——恢复值与篡改值的构造均以它为参照；多容器 Pod 请调整 containers 索引至目标容器）：
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
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-tampered.sh; ( sleep <duration>; sh /tmp/blade-restore-tampered.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-tamper[e]d; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
4. 修改应用 A 的镜像引用为篡改后的版本（**替换 tag 形态**——保留仓库地址与镜像名，仅把 tag 替换为仓库中不存在的篡改值；禁止在完整基线引用后拼接后缀——双冒号非法引用会呈现为 InvalidImageName 误判）
5. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
6. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段。主路径下此项随载体配方 restorePatches 由载体 TTL 还原，无需手动执行）
7. 观察 Pod 的镜像拉取状态

## 注入验证
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 永不 Ready（篡改引用不可拉取，故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 处于 ImagePullBackOff）
2. 执行 `kubectl get pods -n <namespace> -l <label>`，确认**所有**目标 Pod 状态停留在 ImagePullBackOff 或 ErrImagePull——两者是同一故障的先后渲染（首拉失败即 ErrImagePull，进入退避后转为 ImagePullBackOff），出现任一即判（不是仅一个新 Pod，而是全部副本）。**判据必须限定在目标 label 内**——命名空间可能存在既有的、与本故障无关的 ImagePullBackOff Pod（常驻工具/工作负载），不计入本判据
3. 读取 Deployment 模板确认镜像引用已替换为篡改值（机制主证——模板层写入已落地；与步骤 1 基线对比仅 tag 被替换）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].image}'
   ```
4. 执行 `kubectl describe pod <pod-name>`，确认 Events 包含镜像不可拉取相关信息：docker 通道为
   `manifest for xxx not found`；containerd 通道为 `failed to resolve reference ... [404 Not Found]`
   （判读时不要按 manifest 字样硬匹配，两种通道措辞不同，containerd 集群报 404）。**若 Events 呈 `invalid reference format` 且 Pod 状态为 InvalidImageName，说明注入了非法引用形态（双冒号拼接），不是本 case 现象**——修正注入形态后重试

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原镜像引用与 maxUnavailable 基线（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动将镜像引用还原为步骤 1 基线（值用基线完整引用，不要拼接
   `<原始镜像>:<原始标签>`——双冒号非法引用同坑）；演练提前结束时由 Agent 主动执行同一条恢复
   命令（幂等，定时器迟到再执行一次无副作用。json patch 按字段精确替换，天然规避 resourceVersion
   乐观锁问题，也不会像 apply 三方合并那样保留注入后新增的字段）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"<步骤1记录的基线镜像完整引用>"}]'
   ```
2. 等待 Pod 滚动更新完成

## 恢复验证
1. 执行 `kubectl get pods -n <namespace> -l <label>`，确认目标 Pod 状态全部恢复为 Running
2. 确认 Deployment 模板镜像引用回到步骤 1 的基线完整引用：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].image}'
   ```
3. 确认当前代 RS hash 回到注入前基线值（RS hash 是 pod template 全字段相等性的免费校验和——镜像引用还原后 template 与基线逐字段相等 ⇒ hash 必回基线值；hash 不回 = 模板有残留字段，不必猜哪个字段。注意 hash 只覆盖 pod template，maxUnavailable 等 strategy 字段不在其中，由第 4 条独立确认）
4. 确认 maxUnavailable 回到步骤 2 记录的基线值（strategy 字段不在 RS hash 覆盖内，必须独立确认；值未回 = 恢复不完整）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   ```
5. 查看 Pod Event，确认镜像拉取成功

## 基准事实
- **根因**：容器镜像引用被篡改为仓库中不存在的版本（供应链投毒/误操作/镜像退役等），滚动替换后新 Pod 无法拉取镜像启动
- **必现现象**：所有 Pod 停留在 ErrImagePull/ImagePullBackOff；Deployment 模板镜像引用呈篡改值；拉取错误按运行时通道二分：docker 通道 `manifest for xxx not found`，containerd 通道 `failed to resolve reference ... [404 Not Found]`

## 注意事项
- **Pod spec 层镜像篡改不可达本 case 现象（手段判死，v1.24.6 实测）**：ChaosBlade `pod-pod fail`（Pod spec 层直改镜像）后，kubelet 反复拉取失败（Events 404 BackOff 累积）但**不杀旧容器**——运行中容器保持旧镜像、Pod 持续 Running、服务不中断、Endpoints 不摘除；「全部副本 ErrImagePull/ImagePullBackOff + 服务中断」判据不可达。镜像拉取失败现象必须经 **Deployment 模板层篡改 + maxUnavailable 100% 强制滚动替换**达成（本案路径）。
