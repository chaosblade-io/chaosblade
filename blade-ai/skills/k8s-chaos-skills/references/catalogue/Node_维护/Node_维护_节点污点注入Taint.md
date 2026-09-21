---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2/ND9）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 摘除注入的污点），但靶为 Node
# （cluster-scoped）——装配器 M1 四对象栈仅支持 namespaced 靶（Role/RoleBinding
# 无法授权 cluster-scoped 资源，工具对 Node 靶 fail-closed 拒绝），本 case 无
# 装配器主路径；且本 case 亦无 timer 自恢复通道（宿主 kubelet.conf 受
# NodeRestriction 不能修改 taints）——恢复完全依赖 Agent（或人工）在演练结束
# 时主动摘除，属 recovery-carrier spec「降级路径」回落 agent-online 恢复的
# 结构性形态，恢复闭环不缺失（如实向用户声明该边界后确认 duration）。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（污点写靶 Node 的 spec.taints 属靶域内，名字匹配放行），无跨对象写条
# 目；恢复路径不建 k8s 对象，无立法条目。
---

**用例名称** 节点污点注入Taint 导致 Node_维护

**恢复动作配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：摘除注入的污点。**靶为 Node（cluster-scoped），装配器 M1 不支持**（四对象栈 Role/RoleBinding 无法授权 cluster-scoped 资源，工具对 Node 靶 fail-closed 拒绝）——本配方不经装配器；本 case 亦无 timer 自恢复通道（宿主 kubelet.conf 受 NodeRestriction 不能修改 taints，timer 到期触发但返回 Forbidden）——恢复完全依赖 Agent（或人工）在演练结束时按本配方主动摘除（幂等，见注入恢复段）：

```yaml
targetRef:                                # 靶（cluster-scoped，无 namespace）
  kind: Node
  name: <node-name>
patches:                                  # 注入域（json-patch 语义；实际经 kubectl taint 命令执行）
- op: add
  path: /spec/taints/-
  value: {key: <key>, value: <value>, effect: <effect>}
restorePatches:                           # 恢复域：Agent 主动兜底动作清单（无任何自治通道）
# 基线空 → remove 注入污点（按 key 精确摘除）；基线非空 → replace <基线完整数组>（严禁整组清空——误删集群固有污点）
- op: remove
  path: /spec/taints
durationSeconds: <duration>               # 演练窗口声明（无自治 timer；摘除时效由 Agent 演练结束动作决定）
```

- cluster-scoped 目标（Node）：targetRef 无 namespace 字段；恢复不经装配器（见配方标题）。
- **自治恢复边界（CR 通道退役后的如实声明）**：原 CR 通道把恢复意图住进集群曾是此域最大价值（调和器 TTL 自治摘污点）；CR 退役且装配器不支持 cluster-scoped 靶后，本 case 无任何自治恢复通道——恢复完全依赖 Agent（或人工）主动摘除，NoExecute 形态下摘除时效直接决定驱逐持续面。如实向用户声明该边界后确认 duration。
- 恢复由 Agent 演练结束时主动执行承载（restorePatches 动作清单）——属 recovery-carrier spec「降级路径」回落 agent-online 恢复的结构性形态；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. 节点被添加 Taint，被标记为维护/隔离状态，模拟平台或运维标记节点的场景
2. 新 Pod 无法调度到该节点（除非声明了对应 Toleration）
3. 已有 Pod 是否被驱逐，取决于注入的 `<effect>`：
   - `NoExecute`：**驱逐**不容忍该 Taint 的已有 Pod，被驱逐 Pod 在其他节点重建
   - `NoSchedule`：**不驱逐**已有 Pod，仅阻止新 Pod 调度
   - `PreferNoSchedule`：软约束，调度器尽量避开该节点，但不保证

**资源准备**：
1. 确认目标节点名称（通过 `kubectl get nodes` 获取）
2. 确认目标节点上有业务 Pod 运行，且 Pod 未声明通配 Toleration
3. 确认目标节点当前无同名污点（同 key 污点若已存在，`kubectl taint` 会报 already has taint；taints 列表可甄别）
4. 仅当注入 `NoExecute` 时：确认集群中其他节点有足够资源接纳被驱逐的 Pod
5. 靶节点可行性以字段查询核验排除维度即可（控制面 / 既有同名污点 / 容量可接纳驱逐 / 反亲和极点，见 SKILL.md 安全红线「选靶论证字段化」），勿做全集群逐节点算术展开

**演练步骤**：
1. 确认目标节点当前 Taints 和运行的 Pod：
   ```bash
   kubectl get node <node-name> -o jsonpath='{.spec.taints}'
   kubectl get pods --field-selector spec.nodeName=<node-name> -A -o wide
   ```
2. **注入污点（taint 无 timer 自恢复路径，恢复完全依赖 Agent 主动执行——两条定时器路径均
   不可行：宿主机 systemd timer 的 kubectl 载荷以宿主机 kubelet.conf 为凭证，受
   NodeRestriction 限制**不能修改 taints**（`Forbidden: node "X" is not allowed to
   modify taints`，timer 到期触发但污点不会被摘除）；载体内后台定时形态不在 agent
   守卫的载荷放行形态内。摘除承载于注入验证完成之后的恢复段（`blade-ai recover` 或
   Agent 演练结束时主动执行），**严禁编入 execute 计划的收尾步骤**——提前摘除会使
   污点消失、主证据失效（见 SKILL.md 安全红线「拆线不进执行计划」）。`<effect>` 按
   演练目标选择）：
   ```bash
   # 注入污点
   kubectl taint nodes <node-name> <key>=<value>:<effect>
   ```
   说明：
   - `NoExecute`：驱逐所有不容忍该 Taint 的已有 Pod（激进模式，影响面最大）；由 taint-manager **持续**生效，后续新建的不容忍 Pod 也会被驱逐；可配合 Pod 的 `tolerationSeconds` 做延迟驱逐演练
   - `NoSchedule`：仅阻止新 Pod 调度，不驱逐已有 Pod（温和模式）
   - `PreferNoSchedule`：尽量不调度，软约束
   - `<key>=<value>` 可自由指定。若演练目标是模拟平台自身打的维护污点（使故障不易被一眼看出是人为注入），可选用与平台一致的键，例如 `node.alibabacloud.com/instance-charged-type=PostPaid`；恢复时须用完全相同的 `<key>=<value>:<effect>` 摘除
3. 观察节点调度状态与已有 Pod 的行为（是否被驱逐取决于所选 `<effect>`）

**注入验证**：
1. **（主证据，必做）** 执行 `kubectl get node <node-name> -o jsonpath='{.spec.taints}'`（单行输出，截断免疫），确认返回包含注入的 `<key>=<value>:<effect>`。**此条成立即已证明注入生效**——污点的存在本身就是故障效果。（`describe node | grep -A 5 Taints` 亦可，但多行输出形态头部信息有截断风险，jsonpath 优先）
2. **（只做与本次 `<effect>` 匹配的分支）** 其余分支的现象在本次注入下**不可能出现**，直接标记为 `expected` 并跳过：
   - **`NoSchedule`**：执行 `kubectl get pods -A --field-selector spec.nodeName=<node-name>`，确认已有 Pod **仍全部 Running**——这是预期行为，**不是失败**。
   - **`NoExecute`**：a) 同上命令确认不容忍该 Taint 的 Pod 已被驱逐；b) `kubectl get pods -A -o wide` 确认被驱逐 Pod 已在其他节点重建（**被驱逐 Pod 的重建节点不可硬编码预测**——调度器自由选择，只断言「新节点 ≠ 被 taint 节点」；若靶声明了 required podAntiAffinity，还需断言「新节点 ≠ 反亲和极点所在节点」）；c) `kubectl get events -A --field-selector type=Warning --sort-by='.lastTimestamp' | grep TaintManagerEviction` 确认驱逐事件。**事件工件要点**：NoExecute taint 驱逐由 taint-manager 执行，事件 reason 是 `TaintManagerEviction`（Warning 级，"Marking for deletion" 语义）——**不是** Pod 上的 `Evicted` reason；挂载对象（k8s v1.31 实测形态）是**被驱逐的 Pod 对象**而非节点对象——`--field-selector involvedObject.name=<node-name>` 查节点域会得到假阴性空集，**按 reason 查询勿假设挂载对象**（不同 k8s 版本挂载点可能不同）。
   - **`PreferNoSchedule`**：仅第 1 条即可——调度偏好无确定性可观测证据，不做额外验证。

> ⚠️ 验证纪律：
> - **严禁为不适用的分支反复更换查询方式去找证据**。例如注入 `NoSchedule` 时不存在驱逐、Pod 重建、eviction 事件，查不到是**必然**而非失败。
> - 查事件的过滤维度必须与事件挂载对象对齐（见 SKILL.md 安全红线「判据形态三原则」①）——本 case 驱逐事件挂在**被驱逐 Pod 对象**上（见注入验证 c），节点域 field-selector 必得空集；此场景用 `-A` + `type=Warning` + grep reason 收敛形态，**严禁的是无过滤全量拉取**。
> - 同一事实（如污点是否存在）确认一次即可，不要重复查询。

**注入恢复**：
1. **由 Agent 主动执行摘除命令**（幂等——taint- 对已删除的污点报 not found 无害。
   注意末尾的 `-` 表示删除，`<key>=<value>:<effect>` 须与注入时完全一致）：
   ```bash
   kubectl taint nodes <node-name> <key>=<value>:<effect>-
   ```
2. 仅当注入的是 `NoExecute` 时：确认被驱逐 Pod 在异地后继节点保持 Running——**驱逐不回迁**：污点摘除仅恢复可调度性，已重建 Pod 留在驱逐后落位的节点（新起 Pod 方可回流本节点）

> ⚠️ `kubectl taint` 本身**没有自动恢复机制**（不同于 ChaosBlade 的 `--timeout`），且
> **无可用 timer 自恢复路径**：宿主机 systemd-run timer 的 kubectl 载荷以宿主机
> kubelet.conf 为凭证，该凭证受 NodeRestriction 限制**不能修改 taints**（timer 到期
> 触发但返回 Forbidden，污点残留）；载体内后台定时形态不在 agent 守卫放行形态内。
> **污点类演练必须在演练结束时由 Agent（或人工）主动摘除污点**——尤其 `NoExecute` 污点
> 残留期间 taint-manager 会持续驱逐不容忍的 Pod。**注入验证完成前不得摘除——提前摘除
> 会使主证据（taints 字段在位）失效，裁决被迫降级到间接驱逐工件**（见 SKILL.md 安全
> 红线「拆线不进执行计划」）。

**恢复验证**：
1. 执行 `kubectl get node <node-name> -o jsonpath='{.spec.taints}'`（单行截断免疫，与注入验证主证据同形态），确认注入的 `<key>=<value>:<effect>` 已移除（自建污点移除后 taints 为空或仅剩基线污点；若节点存在其他基线污点，以「注入键不在列表」为判据）
2. 执行 `kubectl get nodes`，确认节点状态正常（Ready，无异常 Condition）
3. 若注入的是 `NoExecute`，确认被驱逐 Pod 已恢复 Running 且 Ready（注意：若被驱逐的是 Deployment 管理的 Pod，恢复路径是 Deployment controller 在污点摘除后重建副本（新 Pod 新 UID——重建而非复活，断言照旧态 UID 消失 + 新副本 Running）；裸 Pod 则不重建，恢复验证只看污点摘除本身）——调度可达性由污点摘除判据蕴含（污点不在位即调度阻碍移除），无需单独断言

**基准事实**：
- **根因**：节点被添加 Taint，调度器据此拒绝不容忍该污点的新 Pod；`NoExecute` 还会由 taint-manager 驱逐不容忍该污点的已有 Pod，模拟节点被标记维护/隔离的场景
- **必现现象（与 effect 无关）**：节点 Taints 字段含注入的 `<key>=<value>:<effect>`；新 Pod 无法调度到该节点
- **驱逐对象甄别不变式（预检义务，planning 期必须完成）**：凡目标节点名册里的 Pod，逐个核对其 tolerations，按容忍三分法归类（全文见 SKILL.md 安全红线「驱逐波及面甄别」）：① `operator: Exists` 无 key 无 effect 的通配容忍 → **全 effect 驱逐免疫**（系统 DS 几乎都带）；② `operator: Exists` 无 key 但带 `effect:` 限定 → **仅该 effect 免疫**——`effect: NoSchedule` 型对 NoExecute 驱逐**不免**（部分组件 Pod 是此形态，勿按 workload 类型猜测）；③ 仅容忍标准污点键（node.kubernetes.io/*）或指定 key → 对自定义 `<key>` 不容忍、**驱逐必中**。判读以逐 Pod jsonpath 读 tolerations 字段为准。该核对结论直接决定「已有 Pod 驱逐波及面」的爆炸半径声明与 NoExecute 分支的驱逐对象名册（**仅必驱逐组会出现在驱逐判据里，两类免疫组必须显式归入 expected-不驱逐组**，防止误判「驱逐失败」）
- **随 effect 变化的现象**：
  - `NoExecute`：不容忍该 Taint 的 Pod 被驱逐并在其他节点重建；Events 显示 `TaintManagerEviction`（挂载在被驱逐 Pod 对象上，非 Pod `Evicted` reason——见注入验证 c）
  - `NoSchedule`：已有 Pod 不受影响，保持 Running
  - `PreferNoSchedule`：无确定性可观测现象（仅调度偏好）
