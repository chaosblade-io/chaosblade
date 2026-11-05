---
# 受害者作用域参考（route A，非强制 hint）：本用例受害者是 CronJob 自身——故障机制
# patch 的就是该 CronJob 的 spec.suspend（受害者域内写，名字匹配放行），底层 Job/Pod
# 与调度器全程健康。供 planner 填 finish_planning 的 fault_scope 时参照，把它钉在
# cronjob 而非降级到 workload/pod：cronjob 不在旧 scope 词表时 approved 会被迫降级，
# 使 approved.scope ≠ effective.scope=cronjob，被 drift 守卫结构性拒绝（openspec
# expand-k8s-case-coverage 4.2 已把 cronjob 注册进 k8s_chaosblade family scopes + 补齐
# faultdrill assembler 的 batch/v1 REST 映射修复此链）。这是「参考」而非「边界」——代码
# 不校验、不覆盖（无 enforcement），模型仍须理解真实语义后自行判断、不盲从本字段。
fault_scope: cronjob
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch：spec.suspend 还原为 false），路由进
# 程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC 不可授/验权
# 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 载体由 faultdrill_assemble_carrier 程序化建栈、结构性自拆（fail-closed 路径 + 恢复句柄清理链 + 武装定时器自删 → apiserver ownerRef GC 级联）；降级路径不自建可自撤 RBAC 栈，正文无需传输载体自撤授权类知识。
carrier_teardown: assembler-managed
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者自身
# （patch CronJob 的 spec.suspend 属受害者域内，名字匹配放行），无跨对象写条目；装配器
# 载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由工具内程序化构建
# ——构造保证 + fail-closed 内嵌检查（RBAC 从 restorePatches 同源推导禁通配、SA 真实
# token 验权 403 中止+清理），不经 LLM kubectl 写面，无立法条目。
---

**用例名称** 任务被挂起 导致 CronJob_任务异常

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 把 spec.suspend 还原为 false；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:
  kind: CronJob
  name: <cronjob-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace                             # 挂起任务：控制器停止在调度点创建新 Job
  path: /spec/suspend
  value: true                             # spec.suspend 缺省时改用 op: add（见资源准备 4）
restorePatches:                           # 恢复域：载体 TTL 到点执行；Agent 死亡后 recover 重放同源
- op: replace
  path: /spec/suspend
  value: false                            # 注入前记录的原始 suspend（正常调度态为 false）
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（须覆盖 ≥2 个调度周期以观测「新 Job 不再产生」）
```

- **恢复通道对称性（本 case 降级路径的红利）**：spec.suspend 是声明式 API mutation——注入与恢复是同一字段的镜像（`true` ↔ `false`），恢复通道与注入通道同源且幂等；与滚动/重建类不同，恢复只是让控制器在下一个调度点重新开始创建 Job，无需等待任何 Pod 收敛窗口，是秒级确定性幂等操作，Agent 主动执行即满足红线，**降级路径无需建任何定时器/载体**。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

## 故障现象
1. CronJob 被挂起：`spec.suspend=true`，控制器在所有后续调度点都不再创建新 Job
2. `status.lastScheduleTime` 冻结不再前进（停在挂起生效前的最后一次调度时刻）
3. 调度周期到点但无新 Job 对象产生（最新 Job 的 **schedule-ID**（Job 名末尾数字）在 ≥2 个调度周期内不再前进；⚠️ Job **数量**受 `successfulJobsHistoryLimit` 钉死在上限值，不是可靠判据）
4. 业务后果：周期性任务（备份、清理、报表、对账、心跳巡检等）静默停摆——不报错、不告警，只是「不再发生」，是隐蔽性极强的一类故障

## 资源准备
1. 确认目标 CronJob 存在且处于正常调度态（`spec.suspend=false`），未被上层 Operator/Helm 强 reconcile（否则 suspend 修改会被覆盖回滚）
2. **选靶硬条件：调度周期足够密**——挂起的可观测证据是「新 Job 不再产生」，须在一个演练窗口内跨越 ≥2 个调度点才能证伪「只是还没到点」。每分钟的 `schedule: "* * * * *"` 最理想；日/周级 CronJob 无法在短窗口内观测到调度差异，只能退化为纯字段态判据（suspend=true）
3. 记录注入前基线：`spec.suspend` 原值（正常为 false）、`status.lastScheduleTime`、当前 Job 名单与数量（恢复后对照「新 Job 重新产生」的参照系）
4. 确认 `spec.suspend` 字段在位：CronJob 的 suspend 缺省语义为 false，但对象里可能不落该字段——若 `kubectl get cronjob <name> -o jsonpath='{.spec.suspend}'` 返回空，注入须用 `op: add` 而非 `op: replace`（json-patch replace 要求路径已存在）
5. **确认基线是「真正在推进」的活体调度态（关键前置）**——注入前必须实测 `status.lastScheduleTime` 逐分钟前移、且 Job 的 Pod 能真正调度并完成（`kubectl get jobs -l <selector>` 出现递增的新 schedule-ID 且 `COMPLETIONS=1/1`）。两个常见「假基线」会让行为级判据在注入前即已成立、零区分力：① jobTemplate 的 Pod 缺集群污点所需的 nodeSelector/tolerations → Pod Forever Pending（`describe pod` 事件 `0/N nodes are available: ... untolerated taint`）→ Job 永久 active；② 叠加 `concurrencyPolicy: Forbid` 时，这个永不完成的 Job 会独自锁死后续所有调度点（controller 持续发 `JobAlreadyActive` 事件、`lastScheduleTime` 提前冻结）。故演练靶的 jobTemplate 须带与集群内其它 drill Pod 一致的调度约束（本演练集群全 25 节点带 `sigma.ali/resource-pool` NoSchedule 污点：须 `nodeSelector: {sigma.ali/resource-pool: test}` + 对应 tolerations），优先 `concurrencyPolicy: Allow`，任务体快进快出（如 `sleep 3`），并用足够密的 schedule 让每个调度点都能观测到新 Job

## 演练步骤
（主路径 = 步骤 1 基线快照（suspend 原值 + lastScheduleTime + Job 名单——restorePatches 基线值来源，两条路径共用）→ 调 faultdrill_assemble_carrier（参数取自载体配方：target_kind=CronJob、target_name=<cronjob-name>、target_namespace=<ns>、patches=…、restorePatches=…、duration_seconds=<duration>），注入+武装+readback 工具内同步完成。步骤 2 的手动 patch 序列仅当装配器 fail-closed 报告不可用时作降级兜底；本 case 对称性红利使降级路径无需建定时器——Agent 主动恢复即满足红线）：
1. 定位目标 CronJob，完成基线快照（suspend 原值、lastScheduleTime、当前 Job 名单与数量）
2. **恢复通道对称性定案（降级路径论证）**：spec.suspend 是声明式 API mutation——注入与恢复是同一字段的镜像，恢复通道与注入通道同源且幂等，且恢复是秒级（下一个调度点即重新创建 Job，无 Pod 收敛窗口），Agent 主动执行恢复即满足「30 秒内可恢复」红线，定时自恢复非必要；timer 自恢复的必要性仅当恢复通道可能比注入通道更难达（daemon 故障/载体失联/宿主网络断）——声明式 mutation 稳态无失控恶化风险，不在其列。因此主路径下自治恢复由装配器载体 TTL 承载（载体配方），降级路径 = Agent 在演练结束时主动执行逆 patch、无需建任何载体标准件。降级路径手动序列：
   ```bash
   # 基线捕获：Agent 读取输出并记录原始 suspend（恢复命令使用）
   kubectl get cronjob <name> -n <ns> -o jsonpath='{.spec.suspend}'
   # 注入：挂起任务（spec.suspend 缺省时把 replace 换成 add）
   kubectl patch cronjob <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/suspend","value":true}]'
   # 恢复（逆 patch，幂等）
   kubectl patch cronjob <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/suspend","value":false}]'
   ```
3. 观察调度停摆：跨越 ≥2 个调度周期后，`status.lastScheduleTime` 冻结在挂起前时刻、无新 Job 产生（对照基线 Job 名单未增长）

## 注入验证
1. 执行 `kubectl get cronjob <name> -n <ns> -o jsonpath='suspend={.spec.suspend}'`，确认 suspend 等于 `true`（**带标签 jsonpath**——确定性裁决层提取器按 `label={...}` 门控解析成 `Suspend` 指标键，裸 jsonpath 无标签则不产键、判据取不到数据）
2. 执行 `kubectl get cronjob <name> -n <ns> -o jsonpath='lastScheduleTime={.status.lastScheduleTime}'`，确认 lastScheduleTime 冻结在挂起生效前的最后调度时刻（标签 humanize 为 `Last schedule time`）；间隔 ≥1 个调度周期后复查，该值不再前进即为挂起生效的正向证据
3. **锚定最新 Job 的 schedule-ID / 名字集合，而非 Job 数量**——`successfulJobsHistoryLimit` 会把 Job 总数钉死在上限值（如 5），使 `wc -l` 计数在挂起前后恒等、对「有无新 Job」零区分力。正确判据：执行 `kubectl get jobs -l <selector> -n <ns> -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort`，跨越 ≥2 个调度周期后确认**最大 schedule-ID（Job 名末尾数字，= 自 epoch 起的分钟数）不再前进、无更大的新 ID 出现**——这是「控制器停止调度」的行为级判据，比单看 suspend 字段更强
4. 稳态确认：等待 ≥2 个调度周期后复查步骤 2/3，lastScheduleTime 仍冻结、最大 schedule-ID 仍未前进，排除「恰好还没到调度点」的过渡暂态误判
5. **基线被污染时的等价行为判据（降级观测形态）**：若靶标基线非活体调度态（如 jobTemplate Pod 不可调度 + `Forbid` 锁死，`lastScheduleTime` 注入前即冻结），则 #2/#3 的「冻结 / 无新 Job」在注入前已成立、零区分力。此时改用 **controller 事件流差分**：cronjob-controller 在 `sync()` 中**先判 `spec.suspend`、后判 `concurrencyPolicy`**，故挂起前控制器每个调度点持续发 `JobAlreadyActive` 事件（Forbid 拒绝新 Job），`suspend=true` 后在 suspend 分支短路、事件**停止累积**。判据：`kubectl get events -n <ns> --field-selector involvedObject.name=<name>,reason=JobAlreadyActive` 的 `count`/`lastTimestamp` 在注入后冻结不再前进 = 挂起在 controller 层生效的正证；恢复后事件重新累积 = 调度路径恢复。⚠️ 用此判据时须先复核 `suspend` 字段：suspend==true 即注入成立，不得因事件流仍在累积（恢复中）反判失败

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原（restorePatches 的 suspend replace=false 由载体内 timer 到点执行，fire 证据落载体 /tmp/restore.log + 任务台账 recovery_handle）；演练提前结束时 blade-ai recover 从台账重放同源配方提前收敛，与载体幂等双执行。以下 Agent 主动恢复为降级兜底形态——本 case 对称性红利使降级路径无需载体，直接执行即满足红线）：
1. **降级路径 = Agent 主动执行逆 patch**（幂等，重复执行无副作用；把 suspend 还原为注入前原值 false）：
   ```bash
   kubectl patch cronjob <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/suspend","value":false}]'
   ```
2. 恢复是秒级——无需等待任何 Pod 收敛；下一个调度点控制器即重新创建 Job

## 恢复验证
1. 执行 `kubectl get cronjob <name> -n <ns> -o jsonpath='suspend={.spec.suspend}'`，确认 suspend 回到注入前原值（正常调度态为 `false`）
2. 执行 `kubectl get cronjob <name> -n <ns> -o jsonpath='{.status.lastScheduleTime}'`，跨越下一个调度点后确认 lastScheduleTime 恢复前进（不再是冻结值）
3. 执行 `kubectl get jobs -l <selector> -n <ns> -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort`，确认恢复后**出现比挂起期冻结的最大 schedule-ID 更大的新 ID**（新 Job 被创建）——这是「调度恢复」的行为级正证；同样以 schedule-ID / 名字集合为准，勿用 Job 数量（受 `successfulJobsHistoryLimit` 钉死，恢复后可能仍等于上限值而假阴性）
4. 语义澄清（挂起类专属判据，区别于缩容/删除类）：恢复只让控制器从下一个调度点起重新开始创建 Job，**不会补建挂起期间错过的历史 Job**（除非 CronJob 配了足够大的 `startingDeadlineSeconds`）——「错过窗口的 Job 不再出现」是 suspend 语义的正确表现，不得判为恢复失败。判据锚定：①suspend==false；②lastScheduleTime 在恢复后跨过调度点重新前进；③恢复后至少产生 1 个新 Job

## 基准事实
- **根因**：CronJob 的 `spec.suspend` 被置为 `true`（人为误操作、发布脚本遗留、自动化编排副作用、或运维为临时止血而挂起后忘记恢复），控制器因此在所有后续调度点停止创建新 Job，周期性任务静默停摆
- **必现现象**：`spec.suspend=true`；`status.lastScheduleTime` 冻结不再前进；跨越 ≥2 个调度周期无新 Job 产生（**最大 schedule-ID / Job 名字集合不再前进**——Job 数量受 `successfulJobsHistoryLimit` 钉死在上限值，不可作判据）；已在挂起前创建的 Job 不受影响、继续跑完（suspend 只拦未来调度，不杀存量）

## 注意事项
- `spec.suspend` 只阻止**未来**的 Job 创建，不会终止挂起时刻已在运行的 Job/Pod（区别于 delete cronjob 或删 Job）——存量任务照常跑完
- 恢复（suspend=false）**不补建**挂起期间错过的调度点（除非配了 `startingDeadlineSeconds` 且错过时长在窗口内），只从下一个调度点恢复；对「必须补齐错过批次」的业务须另行手工触发
- 调度周期过疏（日/周级）的 CronJob 无法在短演练窗口内观测到「新 Job 不再产生」的行为差异，验证退化为纯字段态判据（suspend=true）；欲做行为级验证须选每分级密集调度的靶或临时缩短 schedule
- 若 CronJob 由 Helm/Operator/ArgoCD 管理，suspend 修改可能被上层 reconciliation 覆盖，故障窗口非预期收敛——须选无强 reconcile 的靶或在注入期暂停上层控制器
