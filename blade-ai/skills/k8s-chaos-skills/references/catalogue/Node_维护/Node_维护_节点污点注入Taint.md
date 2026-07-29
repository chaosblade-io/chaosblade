**用例名称** 节点污点注入Taint 导致 Node_维护

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
3. 确认目标节点当前无同名污点（避免与既有污点混淆）
4. 仅当注入 `NoExecute` 时：确认集群中其他节点有足够资源接纳被驱逐的 Pod

**演练步骤**：
1. 确认目标节点当前 Taints 和运行的 Pod：
   ```bash
   kubectl describe node <node-name> | grep -A 5 Taints
   kubectl get pods --field-selector spec.nodeName=<node-name> -A -o wide
   ```
2. 为节点添加污点（`kubectl taint`），`<effect>` 按演练目标选择：
   ```bash
   kubectl taint nodes <node-name> <key>=<value>:<effect>
   ```
   说明：
   - `NoExecute`：驱逐所有不容忍该 Taint 的已有 Pod（激进模式，影响面最大）；由 taint-manager **持续**生效，后续新建的不容忍 Pod 也会被驱逐；可配合 Pod 的 `tolerationSeconds` 做延迟驱逐演练
   - `NoSchedule`：仅阻止新 Pod 调度，不驱逐已有 Pod（温和模式）
   - `PreferNoSchedule`：尽量不调度，软约束
   - `<key>=<value>` 可自由指定。若演练目标是模拟平台自身打的维护污点（使故障不易被一眼看出是人为注入），可选用与平台一致的键，例如 `node.alibabacloud.com/instance-charged-type=PostPaid`；恢复时须用完全相同的 `<key>=<value>:<effect>` 摘除
3. 观察节点调度状态与已有 Pod 的行为（是否被驱逐取决于所选 `<effect>`）

**注入验证**：
1. **（主证据，必做）** 执行 `kubectl describe node <node-name> | grep -A 5 Taints`，确认 Taints 字段包含注入的 `<key>=<value>:<effect>`。**此条成立即已证明注入生效**——污点的存在本身就是故障效果。
2. **（只做与本次 `<effect>` 匹配的分支）** 其余分支的现象在本次注入下**不可能出现**，直接标记为 `expected` 并跳过：
   - **`NoSchedule`**：执行 `kubectl get pods -A --field-selector spec.nodeName=<node-name>`，确认已有 Pod **仍全部 Running**——这是预期行为，**不是失败**。
   - **`NoExecute`**：a) 同上命令确认不容忍该 Taint 的 Pod 已被驱逐；b) `kubectl get pods -A -o wide` 确认被驱逐 Pod 已在其他节点重建；c) `kubectl get events -A --field-selector involvedObject.name=<node-name> --sort-by='.lastTimestamp'` 确认出现 taint-based eviction 事件。
   - **`PreferNoSchedule`**：仅第 1 条即可——调度偏好无确定性可观测证据，不做额外验证。

> ⚠️ 验证纪律：
> - **严禁为不适用的分支反复更换查询方式去找证据**。例如注入 `NoSchedule` 时不存在驱逐、Pod 重建、eviction 事件，查不到是**必然**而非失败。
> - 查事件必须带 `--field-selector involvedObject.name=<node-name>` 限定到目标节点，**严禁**全集群 `kubectl get events -A --sort-by=...`（量大、噪音高且与本次故障无关）。
> - 同一事实（如污点是否存在）确认一次即可，不要重复查询。

**注入恢复**：
1. 移除添加的污点（注意末尾的 `-` 表示删除，`<key>=<value>:<effect>` 须与注入时完全一致）：
   ```bash
   kubectl taint nodes <node-name> <key>=<value>:<effect>-
   ```
2. 仅当注入的是 `NoExecute` 时：等待调度器重新平衡工作负载

> ⚠️ `kubectl taint` **没有自动恢复机制**（不同于 ChaosBlade 的 `--timeout`）。即使注入请求里带了 `timeout` 参数，污点也会一直留在节点上，必须显式执行上述恢复命令。

**恢复验证**：
1. 执行 `kubectl describe node <node-name> | grep -A 5 Taints`，确认注入的 `<key>=<value>:<effect>` 已移除
2. 执行 `kubectl get nodes`，确认节点状态正常（Ready，无异常 Condition）
3. 确认新 Pod 可正常调度到该节点；若注入的是 `NoExecute`，还需确认被驱逐 Pod 已恢复 Running 且 Ready

**基准事实**：
- **根因**：节点被添加 Taint，调度器据此拒绝不容忍该污点的新 Pod；`NoExecute` 还会由 taint-manager 驱逐不容忍该污点的已有 Pod，模拟节点被标记维护/隔离的场景
- **必现现象（与 effect 无关）**：节点 Taints 字段含注入的 `<key>=<value>:<effect>`；新 Pod 无法调度到该节点
- **随 effect 变化的现象**：
  - `NoExecute`：不容忍该 Taint 的 Pod 被驱逐并在其他节点重建；Events 显示 taint-based eviction
  - `NoSchedule`：已有 Pod 不受影响，保持 Running
  - `PreferNoSchedule`：无确定性可观测现象（仅调度偏好）
