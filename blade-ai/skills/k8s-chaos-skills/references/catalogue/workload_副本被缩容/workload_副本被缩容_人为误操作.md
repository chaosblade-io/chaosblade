---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 扩回原始副本数），路由进程序化
# 恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch spec.replicas 属受害者域内，名字匹配放行），无跨对象写条目；
# 装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由
# 工具内程序化构建——构造保证 + fail-closed 内嵌检查（RBAC 从 restorePatches
# 同源推导禁通配、SA 真实 token 验权 403 中止+清理），不经 LLM kubectl 写面，
# 无立法条目。
---

**用例名称** 人为误操作 导致 workload_副本被缩容

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 扩回原始副本数；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:
  kind: <workload-kind>
  name: <workload-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace
  path: /spec/replicas
  value: <缩容后的低副本数（如 0）>
restorePatches:                           # 恢复域：载体 TTL 到点执行；Agent 死亡后 recover 重放同源
- op: replace
  path: /spec/replicas
  value: <注入前记录的原始副本数>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- **恢复通道对称性（本 case 降级路径的红利）**：kubectl scale 是声明式 API mutation——注入与恢复是同一条命令的镜像（`--replicas=<较小值>` ↔ `--replicas=<基线值>`），恢复通道与注入通道同源同复杂度且幂等（<5s）——装配不可用时 Agent 主动执行恢复即满足「30 秒内可恢复」红线，**降级路径无需建任何定时器/载体**（步骤 2 三通道判死论证仍在位）。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Deployment/StatefulSet 的 `spec.replicas` 被改小（注意稳态语义：kubectl scale 后 READY==DESIRED==缩后新值，正确的判读是「spec.replicas 与 Pod 总数均小于注入前基线」，而非「READY < DESIRED」——后者是缩容未完成的暂态，不是本故障的稳态现象）
2. 应用可用实例减少，部分请求无法处理
3. 服务响应延迟增大或出现超时

**资源准备**：
1. 确认应用 A 的 Deployment/StatefulSet 已正常运行，副本数大于 1
2. 确认监控系统可观测 Pod 副本数和请求指标
3. **HPA 管控排除预检（选靶硬条件）**：`kubectl get hpa -n <namespace>` 确认目标 workload 不被任何 HPA 引用——HPA 管控的 workload 被缩容后控制器会立即扩回（scale 至 0 时扩回 minReplicas，缩至中间值时按指标扩回），故障窗口秒级消失，演练不可成立；须改选无 HPA 管控的同域 workload

**演练步骤**（主路径 = 步骤 1 基线快照（含副本数与 Pod UID——restorePatches 基线值来源，两条路径共用）→ 调 faultdrill_assemble_carrier（参数取自载体配方：target_kind=<workload-kind>、target_name=<workload-name>、target_namespace=<namespace>、patches=…、restorePatches=…、duration_seconds=<duration>），注入+武装+readback 工具内同步完成。步骤 2 的手动 scale 序列仅当装配器 fail-closed 报告不可用时作降级兜底；本 case 对称性红利使降级路径无需建定时器——Agent 主动恢复即满足 30 秒红线）：
1. 定位应用 A 的 Deployment/StatefulSet，记录当前副本数，并完成基线快照（每个 Pod 的名字/UID/nodeName——UID 对照是恢复后「重建而非复活」判据的前后参照）
2. **恢复通道对称性定案（本 case 与 daemon 类故障的本质分野——降级路径论证）**：kubectl scale 是声明式 API mutation——注入与恢复是同一条命令的镜像（`--replicas=<较小值>` ↔ `--replicas=<基线值>`），恢复通道与注入通道同源同复杂度且幂等（<5s），**Agent 主动执行恢复即满足「30 秒内可恢复」红线，定时自恢复非必要**；timer 自恢复的必要性仅当恢复通道可能比注入通道更难达（daemon 故障/载体失联/宿主网络断）——声明式 mutation 稳态无失控恶化风险，不在其列。因此主路径下自治恢复由装配器载体 TTL 承载（载体配方），降级路径 = Agent 在演练结束时主动执行恢复、**无需建任何载体标准件**（对称性免 timer）。通用模板（载体 Pod 内 kubectl 定时器）在本集群三通道判死（降级路径如需 timer 时的既定事实）：①演练载体镜像（terway 等 CNI 组件镜像）无 kubectl 二进制；②宿主 systemd timer + `--kubeconfig=/etc/kubernetes/kubelet.conf` 仅放行节点自操作（uncordon 族），scale deployment 属跨资源写，Node authorizer 语义外必拒；③集群内无可复用的带 kubectl 常驻 Pod（第三方组件 Pod 不得征用）。降级路径手动序列：
   ```bash
   # 基线捕获：Agent 读取输出并记录原始副本数（恢复命令使用）
   kubectl get <workload-kind> <name> -n <namespace> -o jsonpath='{.spec.replicas}'
   # 缩容注入（误操作经典形态：脚本 bug 或打错数字直接归零）
   kubectl scale <workload-kind> <name> -n <namespace> --replicas=<较小值>
   # 恢复（幂等，迟到重复执行无副作用）
   kubectl scale <workload-kind> <name> -n <namespace> --replicas=<基线捕获的原始副本数>
   ```
   若演练环境存在带 kubectl 且有 scale 权限的常驻工具 Pod，可按通用模板加注定时自恢复（`kubectl exec <载体Pod> -n <载体ns> -- sh -c '( sleep <duration>; kubectl scale ... --replicas=<基线> ) >/tmp/restore.log 2>&1 & echo armed'`）作为兜底——但须先按上述三通道逐一实证可达，不可默认在位；兜底 timer 在场时，武装后发生任何修复须先 `kubectl exec <同一载体Pod> -- sh -c 'pkill -f --replica[s]=; true'` 停旧定时器再全额重武装（SKILL.md 安全红线「故障窗口完整」）
3. 观察 Pod 缩容过程和应用状态变化

**标签选择器提示**：
- Kubernetes 推荐标签格式为 `app.kubernetes.io/name=<name>`，而非简单的 `app=<name>`
- 建议先不带 `-l` 过滤器查询 `kubectl get deployment <name> -n <ns>`，再从返回结果中提取实际标签
- 若需用标签过滤，优先使用 `-l app.kubernetes.io/name=<name>` 或 `-l app.kubernetes.io/component=<name>`

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 总数减少，部分 Pod 被终止（`--replicas=0` 形态下目标 selector 的 Pod 列表为空集）
2. 执行 `kubectl get deployment/statefulset <name> -o jsonpath='{.spec.replicas}'`，确认 spec.replicas 等于注入值且小于缩容前的基线值；RS 的 desired/ready 同步收敛（`kubectl get rs -l <selector>`）
3. 缩容事件佐证：Deployment/RS 事件流出现 `ScalingReplicaSet` 缩容记录（Scaled down replica set ...），事件时间落在注入窗口内
4. （可选，仅当演练方提供了应用访问入口时）确认请求延迟增大/超时或可用性下降；无入口时上述副本数证据成立即可判定

**注入恢复**（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原（restorePatches 的 replicas replace 由载体内 timer 到点执行，fire 证据落载体 /tmp/restore.log + 任务台账 recovery_handle）；演练提前结束时 blade-ai recover 从台账重放同源配方提前收敛，与载体幂等双执行。以下 Agent 主动恢复为降级兜底形态——本 case 对称性红利使降级路径无需载体，直接执行即满足 30 秒红线）：
1. **降级路径 = Agent 主动执行恢复命令**（幂等，重复执行无副作用；若降级路径已按通用模板加注兜底 timer，定时器到期自动恢复与主动恢复互为冗余，迟到重放无副作用）：
   ```bash
   kubectl scale <workload-kind> <name> -n <namespace> --replicas=<基线捕获的原始副本数>
   ```
2. 等待 Pod 自动扩容

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 总数恢复到缩容前的值
2. 执行 `kubectl get deployment/statefulset <name>`，确认 READY==DESIRED==基线副本数（注意 UID 对照：恢复后的 Pod 是新 UID 新 Pod 名——「重建而非复活」，旧 Pod UID 不得再现）
3. （可选，有访问入口时）确认请求延迟与可用性恢复正常

**基准事实**：
- **根因**：人为误操作（如 kubectl scale、修改 YAML 等）导致 Deployment/StatefulSet 的副本数被意外缩小，可用实例不足
- **必现现象**：`spec.replicas` 小于注入前基线值，且稳态 READY==DESIRED==缩后新值（「READY < DESIRED」只是缩容过渡暂态，非稳态现象——勿作为注入判据）；Pod 被终止（总数较基线减少）；服务可用性下降

**注意事项**：
- 若目标 Deployment/StatefulSet 由 Helm 管理（label `app.kubernetes.io/managed-by: Helm`），kubectl scale 修改会被 Helm reconciliation 覆盖
- 注入期间应避免触发 Helm upgrade/rollback 操作，否则故障会被意外恢复
- 反之，若需快速恢复，可通过 `helm rollback` 或 `helm upgrade` 强制还原
