---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch：partition 归还原值 + 撤销触发
# 标记），路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：
# 建栈+验权+武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点
# 不容纳/RBAC 不可授/验权 403）时降级正文 SOP 路径。
recovery_channel: apiserver-write
# 载体由 faultdrill_assemble_carrier 程序化建栈、结构性自拆（fail-closed 路径 + 恢复句柄清理链 + 武装定时器自删 → apiserver ownerRef GC 级联）；降级路径不自建可自撤 RBAC 栈，正文无需传输载体自撤授权类知识。
carrier_teardown: assembler-managed
# 确定性判据绑定声明（openspec mechanism-invariant-trust-boundary，决策 C）：
# 本 case 验证判据是确定性字段态（partition 停在声明值 N + revision 分裂），
# 绑定到冻结规则键 (statefulset, partition-stall)，不随 LLM 运行时三元组
# (fault_target=rollingupdate / fault_action=partition-stuck，会漂且未注册)
# 漂移。expected 的键须与提取器 humanize 输出精确匹配（partition→Partition）；
# N 与真实注入 partition 同值（3 副本取 2：ordinal 0/1 留旧、ordinal 2 更新）。
deterministic_verdict:
  target: statefulset
  action: partition-stall
  expected:
    Partition: 2
# 机制写入集立法（write-set approval contract）：本 case 故障机制只写受害者
# StatefulSet 自身（patch spec.updateStrategy.rollingUpdate.partition 与
# spec.template 属受害者域内，名字匹配放行），无跨对象写条目；装配器载体栈
# （SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由工具内程序化构建
# ——构造保证 + fail-closed 内嵌检查（RBAC 从 restorePatches 同源推导禁通配、
# SA 真实 token 验权 403 中止+清理），不经 LLM kubectl 写面，无立法条目。
---

**用例名称** 分区更新卡住 导致 StatefulSet_滚动异常

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 把 partition 归还原值并撤销触发标记；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:
  kind: StatefulSet
  name: <sts-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: add                                 # ① 制造 pending 更新：给 pod template 打一个无害标记
  path: /spec/template/metadata/annotations/<drill-key>   # 触发新 controller revision；不用 image 变更（避免拉镜像副作用）
  value: "<tick>"
- op: replace                             # ② 设分区挡住低 ordinal：ordinal < partition 的 Pod 不更新
  path: /spec/updateStrategy/rollingUpdate/partition
  value: <N>                              # 0 < N <= replicas-1（如 replicas=2 取 N=1）
restorePatches:                           # 恢复域：载体 TTL 到点执行；Agent 死亡后 recover 重放同源
- op: replace
  path: /spec/updateStrategy/rollingUpdate/partition
  value: <注入前记录的原始 partition，通常为 0>
- op: remove                              # 撤销触发标记，使 template 回到注入前形态
  path: /spec/template/metadata/annotations/<drill-key>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄，须覆盖两轮滚动收敛）
```

- **恢复通道对称性（本 case 降级路径的红利）**：partition 与 template 标记都是声明式 API mutation——注入与恢复是同一组 patch 的镜像（partition `<N>` ↔ `<原值>`、annotation `add` ↔ `remove`），恢复通道与注入通道同源且幂等；与单纯缩容不同，本 case 恢复会再触发一轮滚动（partition 归零后低 ordinal 收敛、撤销标记后全体回到原 revision），恢复耗时 = 逐 Pod 重建窗口而非秒级，但仍是确定性幂等收敛，Agent 主动执行即满足红线，**降级路径无需建任何定时器/载体**。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

## 故障现象
1. StatefulSet 滚动更新停在半途不收敛：ordinal ≥ partition 的 Pod 已重建到新 revision，ordinal < partition 的 Pod 长期停在旧 revision（新旧版本共存）
2. `status.updateRevision` 与 `status.currentRevision` 不一致（滚动未完成的稳态，非过渡暂态）
3. `spec.updateStrategy.rollingUpdate.partition` 停在大于 0 的值，控制器不再推进低 ordinal 的更新
4. 版本分裂的业务后果：新旧 Pod 行为/协议/数据结构不一致，依赖一致版本的服务出现兼容性问题

## 资源准备
1. 确认目标 StatefulSet 正常运行、副本数 > 1（单副本无分区意义），且未被上层 Operator/Helm 强 reconcile（否则 partition 修改会被覆盖回滚）
2. **选靶硬条件：`updateStrategy.type=RollingUpdate`**——partition 仅在 RollingUpdate 策略下生效；OnDelete 策略下更新只在手动删 Pod 时发生、partition 无推进语义，须改选 RollingUpdate 的 StatefulSet：`kubectl get sts <name> -n <ns> -o jsonpath='{.spec.updateStrategy.type}'`
3. 记录注入前基线：partition 原值（默认 0，可能字段缺省需以 add 而非 replace 落值）、`currentRevision`/`updateRevision`、各 Pod 的名字/UID/`controller-revision-hash`（恢复后按 ordinal 分区对照的前后参照：注入期 ordinal ≥ partition 者会重建换 UID、ordinal < partition 者保留基线 UID）
4. 确认 `spec.template.metadata.annotations` 存在（json-patch `add` 单个 annotation key 要求父 map 已在位；缺省时先初始化空 annotations）

## 演练步骤
（主路径 = 步骤 1 基线快照（partition 原值 + revision + 各 Pod hash/UID——restorePatches 基线值来源，两条路径共用）→ 调 faultdrill_assemble_carrier（参数取自载体配方：target_kind=StatefulSet、target_name=<sts-name>、target_namespace=<ns>、patches=…、restorePatches=…、duration_seconds=<duration>），注入+武装+readback 工具内同步完成。步骤 2 的手动 patch 序列仅当装配器 fail-closed 报告不可用时作降级兜底；本 case 对称性红利使降级路径无需建定时器——Agent 主动恢复即满足红线）：
1. 定位目标 StatefulSet，完成基线快照（partition 原值、currentRevision/updateRevision、每个 Pod 的名字/UID/controller-revision-hash）
2. **恢复通道对称性定案（降级路径论证）**：partition 与 template 标记均为声明式 API mutation——注入与恢复是同一组 patch 的镜像，恢复通道与注入通道同源且幂等，Agent 主动执行恢复即满足「30 秒内可恢复」红线，定时自恢复非必要；timer 自恢复的必要性仅当恢复通道可能比注入通道更难达（daemon 故障/载体失联/宿主网络断）——声明式 mutation 稳态无失控恶化风险，不在其列。因此主路径下自治恢复由装配器载体 TTL 承载（载体配方），降级路径 = Agent 在演练结束时主动执行逆 patch、无需建任何载体标准件。降级路径手动序列：
   ```bash
   # 基线捕获：Agent 读取输出并记录原始 partition（恢复命令使用）
   kubectl get sts <name> -n <ns> -o jsonpath='{.spec.updateStrategy.rollingUpdate.partition}'
   # ① 制造 pending 更新：给 pod template 打无害标记（触发新 controller revision，不拉镜像）
   kubectl patch sts <name> -n <ns> --type=merge -p '{"spec":{"template":{"metadata":{"annotations":{"<drill-key>":"<tick>"}}}}}'
   # ② 设分区挡住低 ordinal（ordinal < N 卡旧 revision，ordinal >= N 更新到新 revision）
   kubectl patch sts <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/updateStrategy/rollingUpdate/partition","value":<N>}]'
   # 恢复（逆 patch，幂等；先归零 partition 再撤销标记，触发全体收敛回原 revision）
   kubectl patch sts <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/updateStrategy/rollingUpdate/partition","value":<基线捕获的原始 partition>}]'
   kubectl patch sts <name> -n <ns> --type=json -p '[{"op":"remove","path":"/spec/template/metadata/annotations/<drill-key>"}]'
   ```
3. 观察滚动停在 partition 边界：高 ordinal Pod 重建到新 revision 后，低 ordinal Pod 保持旧 revision 不再推进

## 注入验证
1. 执行 `kubectl get sts <name> -n <ns> -o jsonpath='partition={.spec.updateStrategy.rollingUpdate.partition}'`，确认 partition 等于注入值 N 且大于 0（**带标签 jsonpath**——确定性裁决层提取器按 `label={...}` 门控解析成 `Partition` 指标键，裸 jsonpath 无标签则不产键、判据取不到数据）
2. 执行 `kubectl get sts <name> -n <ns> -o jsonpath='updateRevision={.status.updateRevision} currentRevision={.status.currentRevision}'`，确认 updateRevision 与 currentRevision 不一致（滚动未收敛的稳态判据；标签 humanize 为 `Update revision` / `Current revision`，供确定性判据读版本分裂）
3. 执行 `kubectl get pods -l <selector> -n <ns> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.controller-revision-hash}{"\n"}{end}'`，确认版本分裂：ordinal ≥ N 的 Pod 带新 revision hash、ordinal < N 的 Pod 带旧 revision hash
4. 稳态确认：等待一个以上滚动周期后复查步骤 2/3，低 ordinal Pod 仍停在旧 revision（控制器未推进），排除「滚动进行中的过渡暂态」误判

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原（restorePatches 的 partition replace + annotation remove 由载体内 timer 到点执行，fire 证据落载体 /tmp/restore.log + 任务台账 recovery_handle）；演练提前结束时 blade-ai recover 从台账重放同源配方提前收敛，与载体幂等双执行。以下 Agent 主动恢复为降级兜底形态——本 case 对称性红利使降级路径无需载体，直接执行即满足红线）：
1. **降级路径 = Agent 主动执行逆 patch**（幂等，重复执行无副作用；先归零 partition 让全体收敛，再撤销 template 标记回到注入前 revision）：
   ```bash
   kubectl patch sts <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/updateStrategy/rollingUpdate/partition","value":<基线捕获的原始 partition>}]'
   kubectl patch sts <name> -n <ns> --type=json -p '[{"op":"remove","path":"/spec/template/metadata/annotations/<drill-key>"}]'
   ```
2. 等待 StatefulSet 逐 Pod 重建收敛（RollingUpdate 按 ordinal 从高到低推进，恢复耗时 = 全体重建窗口，非秒级）

## 恢复验证
1. 执行 `kubectl get sts <name> -n <ns> -o jsonpath='{.spec.updateStrategy.rollingUpdate.partition}'`，确认 partition 回到注入前原值（通常 0）
2. 执行 `kubectl get sts <name> -n <ns> -o jsonpath='{.status.updateRevision} {.status.currentRevision}'`，确认 updateRevision == currentRevision（滚动收敛）
3. 执行 `kubectl get pods -l <selector> -n <ns> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.controller-revision-hash}{"\n"}{end}'`，确认全体 Pod 带同一 revision hash、且 template 标记已撤销（`kubectl get sts <name> -o jsonpath='{.spec.template.metadata.annotations}'` 不再含 drill-key）
4. UID 对照（partition 语义专属判据，区别于缩容类「全体重建」——不得照搬「基线 UID 不得再现」）：恢复只重建注入窗口内被更新过的 Pod（ordinal ≥ 注入 partition 者，本 case 即最高 ordinal），这些 Pod 恢复后为新 UID；而 ordinal < partition 的 Pod 全程从未离开基线 revision hash，恢复后仍保留注入前基线 UID——**这是 partition 曾生效的正向反证，不是恢复失败**。判据锚定：①全体 Pod 的 `controller-revision-hash` 收敛回基线 hash（步骤 3 已验，且以 hash 而非 ControllerRevision NUMBER 为准——NUMBER 恢复后通常前进到 > 基线，属正常 K8s 语义）；②UID 变化仅出现在注入期被更新的 ordinal ≥ partition 的 Pod 上；③ordinal < partition 的 Pod UID == 基线（正确保留，不得判为失败）；④副本数 == 基线

## 基准事实
- **根因**：StatefulSet 滚动更新设置了 `partition`（金丝雀/分批发布），但 partition 未继续下调至 0，或触发更新后运维中断了发布流程，导致 ordinal < partition 的 Pod 长期停在旧 revision，滚动更新卡住不收敛、新旧版本共存
- **必现现象**：`spec.updateStrategy.rollingUpdate.partition > 0`；`status.updateRevision != status.currentRevision`；Pod 群体 `controller-revision-hash` 分裂为新旧两值（ordinal ≥ partition 新、ordinal < partition 旧）；控制器稳态下不再推进低 ordinal 更新（区别于滚动进行中的过渡暂态）

## 注意事项
- partition 仅在 `updateStrategy.type=RollingUpdate` 下生效；OnDelete 策略的 StatefulSet 不适用本手段（须换靶）
- template 变更（含无害标记）会触发 ordinal ≥ partition 的 Pod 重建；对有状态负载须评估 PDB、数据一致性与重建期间的可用性
- 若 StatefulSet 由 Helm/Operator 管理，partition 与 template 修改可能被上层 reconciliation 覆盖，故障窗口非预期收敛——须选无强 reconcile 的靶或在注入期暂停上层控制器
- 恢复是「还原到注入前 revision」而非「让滚动前进到新版本」：restorePatches 同时归零 partition 并撤销触发标记，全体 Pod 收敛回原 revision
