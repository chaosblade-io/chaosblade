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
