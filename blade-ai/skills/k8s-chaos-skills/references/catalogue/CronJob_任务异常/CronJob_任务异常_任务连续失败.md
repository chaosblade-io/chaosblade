---
# 受害者作用域参考（route A，非强制 hint）：本用例受害者是 CronJob 自身——故障机制
# patch 的就是该 CronJob 的 spec.jobTemplate（把任务体改成必然非零退出 + 收紧
# backoffLimit），受害者域内写、名字匹配放行；CronJob 控制器、调度器、节点全程健康，
# 底层 Job/Pod 是「按被改坏的模板忠实执行」而非平台故障。供 planner 填 finish_planning
# 的 fault_scope 时参照，钉在 cronjob 而非降级到 workload/pod（cronjob 已注册进
# k8s_chaosblade family scopes，见姊妹案 CronJob_任务异常_任务被挂起 frontmatter）。
# 这是「参考」而非「边界」——代码不校验、不覆盖，模型仍须理解真实语义后自行判断。
fault_scope: cronjob
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch：jobTemplate 的 command 还原为基线任务体
# + backoffLimit 还原为基线值），路由进程序化恢复载体装配器（faultdrill_assemble_carrier
# 工具一次调用：建栈+验权+武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/
# 节点不容纳/RBAC 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 载体由 faultdrill_assemble_carrier 程序化建栈、结构性自拆（fail-closed 路径 + 恢复句柄清理链 + 武装定时器自删 → apiserver ownerRef GC 级联）；降级路径不自建可自撤 RBAC 栈，正文无需传输载体自撤授权类知识。
carrier_teardown: assembler-managed
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者自身
# （patch CronJob 的 spec.jobTemplate.spec.backoffLimit 与
# spec.jobTemplate.spec.template.spec.containers/0/command 属受害者域内，名字匹配放行），
# 无跨对象写条目——失败 Job/Pod 由 CronJob 控制器按其自身调度点自动创建（不手动
# `kubectl create job --from=cronjob`，避免引入 Job 对象的机制写条目）；装配器载体栈
# （SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由工具内程序化构建——构造
# 保证 + fail-closed 内嵌检查（RBAC 从 restorePatches 同源推导禁通配、SA 真实 token
# 验权 403 中止+清理），不经 LLM kubectl 写面，无立法条目。
---

**用例名称** 任务连续失败 导致 CronJob_任务异常

## 载体配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 把 jobTemplate 的 command 与 backoffLimit 还原为基线；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:
  kind: CronJob
  name: <cronjob-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace                             # ① 收紧重试上限：让每个调度点的 Job 快速判死（缺省时用 add，见资源准备 4）
  path: /spec/jobTemplate/spec/backoffLimit
  value: 1                                # 基线通常 6；设 1 使 Job 在 2 次 Pod 尝试后即 Failed，缩短单周期收敛
- op: replace                             # ② 改坏任务体：命令必然非零退出（保留一行可辨识回显，便于 logs 取证）
  path: /spec/jobTemplate/spec/template/spec/containers/0/command
  value: ["sh", "-c", "echo drill-injected-fault; exit 1"]
restorePatches:                           # 恢复域：载体 TTL 到点执行；Agent 死亡后 recover 重放同源
- op: replace                             # 逆序还原：先还原任务体，再还原重试上限（顺序无强约束，均幂等）
  path: /spec/jobTemplate/spec/template/spec/containers/0/command
  value: <注入前记录的基线 command 数组>
- op: replace
  path: /spec/jobTemplate/spec/backoffLimit
  value: <注入前记录的原始 backoffLimit，缺省语义为 6>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（须覆盖 ≥2 个调度周期以观测「连续失败」而非单次偶发）
```

- **恢复通道对称性（本 case 降级路径的红利）**：command 与 backoffLimit 都是声明式 API mutation——注入与恢复是同一组字段的镜像（改坏命令 ↔ 基线命令、`backoffLimit:1` ↔ 基线值），恢复通道与注入通道同源且幂等；恢复只是让下一个调度点起控制器用回好模板创建成功 Job，无需清理存量失败 Job（历史 Job 由 `failedJobsHistoryLimit` 自然回收），是秒级确定性幂等操作，Agent 主动执行即满足红线，**降级路径无需建任何定时器/载体**。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

## 故障现象
1. CronJob 每个调度点都创建 Job，但 Job 的 Pod 任务体必然非零退出，Job 在耗尽 `backoffLimit` 次重试后被控制器标记 `Failed`
2. Job `status.conditions[type=Failed].status=True` 且 `reason=BackoffLimitExceeded`；`status.failed` 达到 `backoffLimit+1`
3. 跨越 ≥2 个调度周期，**每个新 Job 都重复失败**（连续失败，区别于单次偶发失败）——最新 schedule-ID 的 Job 仍是 `COMPLETIONS 0/1` + Failed
4. 业务后果：周期性批处理任务（备份、清理、对账、报表、数据同步等）持续失败并堆积失败记录，告警噪声上升；与「任务被挂起」（静默不再发生）相反，本形态是「持续发生但持续失败」，有声但无效

## 资源准备
1. 确认目标 CronJob 存在且处于正常调度态（`spec.suspend=false`，与姊妹案「任务被挂起」互斥——本 case 要求调度器**照常创建** Job，只是 Job 必然失败），未被上层 Operator/Helm/ArgoCD 强 reconcile（否则 jobTemplate 修改会被覆盖回滚）
2. **选靶硬条件：调度周期足够密**——连续失败的可观测证据是「≥2 个调度点的 Job 都 Failed」，须在一个演练窗口内跨越 ≥2 个调度点才能证伪「只是恰好失败一次」。每分钟的 `schedule: "* * * * *"` 最理想；日/周级 CronJob 无法在短窗口内观测到连续失败，只能退化为单 Job 字段态判据（`BackoffLimitExceeded`）
3. 记录注入前基线（restorePatches 基线值来源）：`spec.jobTemplate.spec.template.spec.containers[0].command`（及 `args`——若任务体靠 args 而非 command 承载，注入/还原点相应改到 args）、`spec.jobTemplate.spec.backoffLimit` 原值（缺省语义为 6，对象里可能不落该字段）、`status.lastScheduleTime`、当前 Job 名单与其 `status.conditions`（恢复后对照「新 Job 重新成功」的参照系）
4. 确认 `spec.jobTemplate.spec.backoffLimit` 与 `containers/0/command` 字段在位：backoffLimit 缺省时对象里可能不落该字段——若 `kubectl get cronjob <name> -o jsonpath='{.spec.jobTemplate.spec.backoffLimit}'` 返回空，注入须用 `op: add` 而非 `op: replace`（json-patch replace 要求路径已存在）；command 同理，若基线任务体写在 `args` 而 `command` 缺省，则注入点改到 `/spec/jobTemplate/spec/template/spec/containers/0/args`
5. **确认基线是「真正在推进且成功」的活体调度态（关键前置）**——注入前必须实测 `status.lastScheduleTime` 逐周期前移、且 Job 的 Pod 能真正调度并 `COMPLETIONS=1/1` 成功（`kubectl get jobs -l <selector>` 出现递增的新 schedule-ID 且成功）。若基线 Job 本就失败或 Pod 不可调度（如 jobTemplate 缺集群污点所需的 nodeSelector/tolerations → Pod Forever Pending → Job 永久 active 而非 Failed），则「失败」判据在注入前即已成立、零区分力。故演练靶的 jobTemplate 须带与集群内其它 drill Pod 一致的调度约束（本演练集群全节点带 `sigma.ali/resource-pool` NoSchedule 污点：须 `nodeSelector: {sigma.ali/resource-pool: test}` + 对应 tolerations），任务体快进快出且**成功**（如 `sleep 3`，退出码 0），`backoffLimit` 基线保持默认，并用足够密的 schedule 让每个调度点都能观测到 Job 结局翻转（成功→失败）

## 演练步骤
（主路径 = 步骤 1 基线快照（command/args + backoffLimit 原值 + lastScheduleTime + Job 名单及其 conditions——restorePatches 基线值来源，两条路径共用）→ 调 faultdrill_assemble_carrier（参数取自载体配方：target_kind=CronJob、target_name=<cronjob-name>、target_namespace=<ns>、patches=…、restorePatches=…、duration_seconds=<duration>），注入+武装+readback 工具内同步完成。步骤 2 的手动 patch 序列仅当装配器 fail-closed 报告不可用时作降级兜底；本 case 对称性红利使降级路径无需建定时器——Agent 主动恢复即满足红线）：
1. 定位目标 CronJob，完成基线快照（containers[0].command/args、backoffLimit 原值、lastScheduleTime、当前 Job 名单与各自 conditions——确认基线全为成功 Job）
2. **恢复通道对称性定案（降级路径论证）**：command 与 backoffLimit 均为声明式 API mutation——注入与恢复是同一组字段的镜像，恢复通道与注入通道同源且幂等，且恢复是秒级（下一个调度点即用回好模板创建成功 Job，无需清理存量失败 Job），Agent 主动执行恢复即满足「30 秒内可恢复」红线，定时自恢复非必要；timer 自恢复的必要性仅当恢复通道可能比注入通道更难达（daemon 故障/载体失联/宿主网络断）——声明式 mutation 稳态无失控恶化风险（失败 Job 只是堆积历史记录，受 `failedJobsHistoryLimit` 上限约束、不消耗持续资源），不在其列。因此主路径下自治恢复由装配器载体 TTL 承载（载体配方），降级路径 = Agent 在演练结束时主动执行逆 patch、无需建任何载体标准件。降级路径手动序列：
   ```bash
   # 基线捕获：Agent 读取输出并记录原始 command 与 backoffLimit（恢复命令使用）
   kubectl get cronjob <name> -n <ns> -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].command}'
   kubectl get cronjob <name> -n <ns> -o jsonpath='{.spec.jobTemplate.spec.backoffLimit}'
   # 注入：① 收紧重试上限（缺省时把 replace 换成 add）② 改坏任务体命令为必然非零退出
   kubectl patch cronjob <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/jobTemplate/spec/backoffLimit","value":1}]'
   kubectl patch cronjob <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/jobTemplate/spec/template/spec/containers/0/command","value":["sh","-c","echo drill-injected-fault; exit 1"]}]'
   # 恢复（逆 patch，幂等；command 用基线数组、backoffLimit 用基线值）
   kubectl patch cronjob <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/jobTemplate/spec/template/spec/containers/0/command","value":<基线 command 数组>}]'
   kubectl patch cronjob <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/jobTemplate/spec/backoffLimit","value":<基线 backoffLimit>}]'
   ```
3. 观察连续失败：跨越 ≥2 个调度周期后，每个新创建的 Job 都在耗尽 backoffLimit 后转 `Failed`（`BackoffLimitExceeded`），对照基线（注入前 Job 均成功）出现结局翻转

## 注入验证
1. 执行 `kubectl get cronjob <name> -n <ns> -o jsonpath='suspend={.spec.suspend}'`，确认 suspend 仍为 `false`（**与姊妹案「任务被挂起」的分界判据**——本 case 调度器照常创建 Job，suspend 必须为 false；若为 true 则打错案）；再执行 `kubectl get cronjob <name> -n <ns> -o jsonpath='backoffLimit={.spec.jobTemplate.spec.backoffLimit}'`，确认 backoffLimit 等于注入值（**带标签 jsonpath**——确定性裁决层提取器按 `label={...}` 门控解析成 `Backoff limit` 指标键，裸 jsonpath 无标签则不产键、判据取不到数据）
2. **锚定注入后新产生的 Job（最大 schedule-ID）而非 Job 数量**——`failedJobsHistoryLimit`/`successfulJobsHistoryLimit` 会把 Job 总数钉死在上限值，`wc -l` 计数零区分力（沿用姊妹案纪律）。执行 `kubectl get jobs -l <selector> -n <ns> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.conditions[?(@.type=="Failed")].reason}{"\n"}{end}' | sort`，跨越 ≥1 个调度周期后确认**注入后新出现的最大 schedule-ID 的 Job 带 `BackoffLimitExceeded`**（基线期的成功 Job 无 Failed condition）
3. 执行 `kubectl get job <newest-job> -n <ns> -o jsonpath='failed={.status.failed} succeeded={.status.succeeded}'`，确认 `failed` 达到 `backoffLimit+1`（注入 backoffLimit=1 时为 2）、`succeeded=0`——这是 Job 层「重试耗尽判死」的字段态判据
4. 机制归因（区分「命令非零退出」与其它失败因）：执行 `kubectl get pods -n <ns> -l job-name=<newest-job>` 确认 Pod 处于 `Error`/`Completed`(退出码非 0) 而非 `Pending`/`ImagePullBackOff`（后者说明失败因是调度/镜像而非注入的命令，属选靶或环境缺陷）；执行 `kubectl logs -n <ns> <pod>` 确认输出含注入回显 `drill-injected-fault` 且容器以退出码 1 结束——坐实失败确由被改坏的任务体导致
5. **连续性确认（本 case 专属，区别于单次失败）**：等待 ≥2 个调度周期后复查步骤 2，确认**每个**新 schedule-ID 的 Job 都 `BackoffLimitExceeded`（而非仅第一个失败、后续恢复成功），排除「一次性偶发失败」误判——连续失败的稳态判据是失败随每个调度点重复出现

**持续性检查**——故障窗口内故障必须持续存活（配置型故障：被改坏的 command 与 backoffLimit 在 CronJob spec.jobTemplate 里，字段在即故障在，每个调度点复制该模板创建必败 Job）：
以「注入生效确认」为时点锚（注入验证第 1-4 条通过 = 生效：suspend 仍 false + 新 Job `BackoffLimitExceeded` + Pod 退出码非 0 + logs 见注入回显）——生效确认只证明某一刻故障已落地，不证明它在整个窗口持续存活；持续性复测要把「稳态故障」与「传播收敛窗内的瞬时态」分开：待传播收敛后再次观测仍见故障态才算坐实持续。本故障传播特性：本案为字段型即时生效故障，无传播过程。隔多久复测、复测几次由你依该传播特性与故障形态自行判断（无固定间隔、无固定探针数），到点**下发**复测探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：被改坏的 command 与 backoffLimit 仍在 spec.jobTemplate（`kubectl get cronjob <name> -n <ns> -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].command}'` 输出含 `exit 1`，配置即状态）
2. 行为复查：最新调度点的 Job 仍 Failed（`kubectl get jobs -l <selector> -n <ns>` 中最大 schedule-ID 的 Job `COMPLETIONS 0/1` 未变成功）
3. 事件复查：FailedScheduling 无关（本 case Pod 可调度、只是执行失败）——改看 Job 失败事件 `kubectl get events -n <ns> --field-selector reason=BackoffLimitExceeded` 的 LAST SEEN 相比注入时有新增（控制器仍在为每个新 Job 判死）

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原（restorePatches 的 command 还原 + backoffLimit 还原由载体内 timer 到点执行，fire 证据落载体 /tmp/restore.log + 任务台账 recovery_handle）；演练提前结束时 blade-ai recover 从台账重放同源配方提前收敛，与载体幂等双执行。以下 Agent 主动恢复为降级兜底形态——本 case 对称性红利使降级路径无需载体，直接执行即满足红线）：
1. **降级路径 = Agent 主动执行逆 patch**（幂等，重复执行无副作用；把 command 与 backoffLimit 还原为注入前基线值）：
   ```bash
   kubectl patch cronjob <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/jobTemplate/spec/template/spec/containers/0/command","value":<基线 command 数组>}]'
   kubectl patch cronjob <name> -n <ns> --type=json -p '[{"op":"replace","path":"/spec/jobTemplate/spec/backoffLimit","value":<基线 backoffLimit>}]'
   ```
2. 恢复是秒级——无需清理存量失败 Job（历史失败 Job 由 `failedJobsHistoryLimit` 自然回收，或按需 `kubectl delete job --ignore-not-found` 手动清理注入期产生的失败 Job，属可选收尾、非恢复必需）；下一个调度点控制器即用回好模板创建成功 Job

## 恢复验证
1. 执行 `kubectl get cronjob <name> -n <ns> -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].command}'`，确认 command 回到注入前基线任务体（不再含 `exit 1`）；`kubectl get cronjob <name> -n <ns> -o jsonpath='backoffLimit={.spec.jobTemplate.spec.backoffLimit}'` 确认回到基线值（缺省语义为 6）
2. 执行 `kubectl get jobs -l <selector> -n <ns> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.succeeded}{"\n"}{end}' | sort`，跨越下一个调度点后确认**出现比失败期最大 schedule-ID 更大的新 ID 且其 `succeeded=1`**（新 Job 重新成功）——这是「任务体已修复」的行为级正证；同样以 schedule-ID 为准，勿用 Job 数量（受 historyLimit 钉死）
3. 语义澄清（连续失败类专属判据，区别于挂起类）：恢复只让控制器从下一个调度点起用回好模板创建成功 Job，**不会把注入期已 Failed 的历史 Job 变成成功**——存量失败 Job 保持 Failed 是正确表现，不得判为恢复失败。判据锚定：①command/backoffLimit 回基线；②恢复后跨过调度点至少产生 1 个 `succeeded=1` 的新 Job；③恢复后新 Job 不再出现 `BackoffLimitExceeded`
4. 确认失败事件不再新增——事件列表 append-only，判据是 `reason=BackoffLimitExceeded` 事件的 LAST SEEN 时戳停在恢复时刻之前不再前进：`kubectl get events -n <ns> --field-selector reason=BackoffLimitExceeded`

## 基准事实
- **根因**：CronJob 的 `spec.jobTemplate` 任务体被改成必然非零退出（发布/改配置误操作把命令写错、依赖的下游服务或数据缺失致脚本 `exit 1`、镜像内脚本 bug、或重试上限 backoffLimit 被误设过小），控制器每个调度点忠实按坏模板创建 Job，Job 耗尽 backoffLimit 次重试后判 `Failed`，周期性批处理任务持续失败并堆积
- **必现现象**：`spec.suspend=false`（调度器照常创建 Job，区别于挂起案）；注入后每个新 schedule-ID 的 Job `status.conditions[type=Failed].reason=BackoffLimitExceeded`、`status.failed=backoffLimit+1`、`succeeded=0`；Job Pod 退出码非 0、logs 见注入回显；跨越 ≥2 个调度周期失败重复出现（连续而非偶发）；存量成功 Job 不受影响（注入只改坏此后创建的 Job 模板）

## 注意事项
- 与姊妹案 `CronJob_任务异常_任务被挂起` 的分界：挂起案 `suspend=true`、**无新 Job 产生**、lastScheduleTime 冻结（静默停摆）；本案 `suspend=false`、**Job 照常产生但反复失败**（`BackoffLimitExceeded`，有声无效）——两案注入机制/故障现象/判据均不同，不得互相套用判据
- 与 `Pod_CrashLoopBackOff` 域的分界：Job 的 Pod `restartPolicy` 为 `OnFailure`/`Never`，失败后由 Job 控制器**新建 Pod 重试**（受 backoffLimit 约束）而非原地重启，故终态是 Job 层 `Failed/BackoffLimitExceeded` 而非 Pod 层 CrashLoopBackOff；且受害者是周期性批处理 CronJob、非长驻服务 Deployment，业务语义不同
- 注入用「命令非零退出」而非「坏镜像」——坏镜像会落入 `Pod_镜像拉取失败` 域（Pod 卡 `ImagePullBackOff`、Job 因 Pod 起不来而 active 非 Failed），既是形态重叠又使判据从 `BackoffLimitExceeded` 漂成 Pod 调度/拉取失败，须严格用退出码路径
- backoffLimit 设小（如 1）是为缩短单周期收敛、让失败在演练窗口内可观测；若靶 CronJob 的 `schedule` 过疏（日/周级），窗口内跨不过 2 个调度点，连续失败退化为单 Job 字段态判据（`BackoffLimitExceeded`），须如实降级标注、不得据单次失败强判「连续」
- 若 CronJob 由 Helm/Operator/ArgoCD 管理，jobTemplate 修改可能被上层 reconciliation 覆盖，故障窗口非预期收敛——须选无强 reconcile 的靶或在注入期暂停上层控制器
- 失败 Job/Pod 会堆积到 `failedJobsHistoryLimit` 上限后由控制器回收，爆炸半径限于该 CronJob 自身的 Job 对象（快进快出的 `exit 1` 任务体几乎不占资源）；勿选承载真实业务数据的 CronJob 作靶，避免失败任务体产生副作用（本 case 任务体是纯 `exit 1`、无写操作）
