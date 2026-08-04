**用例名称** Pod故障 导致 Pod_被删除

**故障现象**：
1. 目标 Pod 被直接删除，短暂出现 Terminating 状态后消失
2. 如 Pod 由 Deployment/ReplicaSet 管理，控制器自动创建新 Pod 替代（Pod 名称变化，AGE 重置）
3. Service Endpoints 短暂减少（旧 Pod 摘除到新 Pod 就绪的窗口期内流量中断）
4. Pod Events 中出现 Killing 事件，随后有新 Pod 的 Scheduled/Pulling/Created/Started 事件

**资源准备**：
1. 确认目标应用已正常运行，有明确的 namespace 和 label selector
2. 确认目标 Pod 由 Deployment/ReplicaSet 管理（确保删除后能自动重建）
3. 确认监控系统可观测 Pod 生命周期事件和 Service Endpoints 变化

**演练步骤**：
1. 确认目标 Pod 当前状态为 Running 且 Ready，记录当前 Pod 名称：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 使用 ChaosBlade 对目标 Pod 注入删除故障：
   ```bash
   blade create k8s pod-pod delete \
     --labels <label-selector> \
     --namespace <namespace> \
     --timeout <seconds> \
     --kubeconfig <路径>
   ```
   - `--timeout`：控制 ChaosBlade 持续删除的时间窗口，在此时间内 Pod 每次被控制器重建后都会再次被删除
   - 故障机制：ChaosBlade 直接执行 kubectl delete pod，目标 Pod 被立即终止
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 执行 `kubectl get pods -n <namespace> -l <label-selector>`，确认旧 Pod 名称已不存在，新 Pod 已被创建（名称不同、AGE 很短）
2. 执行 `kubectl get events -n <namespace> --sort-by='.lastTimestamp'`，确认存在 Killing 事件（旧 Pod 被删除）以及 Scheduled/Created/Started 事件（新 Pod 被重建）
3. 执行 `kubectl get endpoints <service-name> -n <namespace>`，观察 Endpoints 是否短暂减少（取决于新 Pod 就绪速度）
4. **持续性检查（"持续删除"意图必须做）**：单次删除与持续删除是两种故障形态——
   单次删除后控制器重建一次即稳定。若注入的是持续删除，注入后**停止一切操作、静观 1-2 分钟**，
   确认 Pod 仍在被反复 Killing、AGE 持续极短；若已稳定，只能报"单次删除"，
   不得报"持续删除已达成"

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
   注：由于 Pod 删除后控制器会自动重建，destroy 主要是清理 ChaosBlade 实验记录并停止持续删除行为，Pod 状态已由控制器自动恢复
2. 等待新 Pod 完全就绪（Running + Ready）

**恢复验证**：
1. 执行 `kubectl get pods -n <namespace> -l <label-selector>`，确认 Pod 状态为 Running 且 Ready（READY 列为 x/x）
2. 执行 `kubectl get endpoints <service-name> -n <namespace>`，确认 Service Endpoints 数量恢复正常
3. 确认 `blade status` 中该实验已被清理（无残留实验记录）

**基准事实**：
- **根因**：ChaosBlade `pod-pod delete` 直接删除目标 Pod（等同于 kubectl delete pod），Pod 被立即终止；在 timeout 时间窗口内，控制器每次重建的 Pod 都会被再次删除
- **必现现象**：旧 Pod 名称消失，新 Pod 被创建（名称不同、AGE 极短）；Events 中有 Killing 事件；timeout 窗口内 Pod 反复重建-删除；Service Endpoints 短暂波动

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效 Pod 删除注入。

前提条件：无特殊要求，仅需 kubectl 可访问集群

注入命令：
```bash
# 单次删除 Pod：
kubectl delete pod <pod-name> -n <namespace>

# 持续删除（模拟 ChaosBlade timeout 窗口内反复删除）——有界删除循环：
sh -c 'end=$(( $(date +%s) + <duration> )); i=0;
while [ "$(date +%s)" -lt "$end" ] && [ $i -lt <rounds> ]; do
  kubectl delete pod -l <label-selector> -n <namespace> --wait=false
  i=$((i+1)); sleep <interval>
done'
```
参数说明：
- `<duration>`：故障窗口总时长（秒），墙钟到期循环自动终止——这是主保险
  （用 `date +%s` 计时而非 `SECONDS`：后者是 bash 特性，`sh -c` 下不会自增）
- `<rounds>`：轮数上限，是第二重保险；应满足 `<rounds> × <interval>` ≥ `<duration>`
- `<interval>`：两轮删除间隔（秒），建议 5-10，让控制器有时间重建再删，
  才能观察到完整的"重建-删除"循环
- 与节点侧 crictl 类用例不同：kubectl 的 kubeconfig 不在宿主机上，无法用
  `systemd-run` 在节点武装自停 timer，因此持续删除用**墙钟时限的有界循环**实现
  自停兜底。严禁使用无时限的手动反复执行

恢复命令：
```bash
# 单次删除无需恢复，控制器自动重建 Pod
# 持续删除：有界循环到 <duration> 自动终止；如需提前终止，中断该循环命令即可，
# 之后控制器会自动重建 Pod 并稳定
```

注意事项：
- 单次 `kubectl delete pod` 与 ChaosBlade 单次删除效果完全等价
- 单次删除与持续删除是**两种故障形态**：单次删除的故障随控制器重建一次即消失；
  用户意图为"持续删除/反复重建-删除"时必须用有界删除循环，且演练报告必须写明
  实际使用的形态
- 确保目标 Pod 由 Deployment/ReplicaSet 管理，否则删除后不会自动重建
- 持续删除期间 Pod 名不断变化，验证与恢复都要用 label selector，不要锁定旧 Pod 名
