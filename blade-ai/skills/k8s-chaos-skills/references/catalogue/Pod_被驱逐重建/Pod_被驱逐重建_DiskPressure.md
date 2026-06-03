**用例名称** DiskPressure 导致 Pod_被驱逐重建

**故障现象**：
1. Pod 被 kubelet 驱逐（Evicted），状态为 Failed，reason 为 Evicted
2. 节点 Conditions 中 DiskPressure 为 True
3. 容器运行时目录（如 /var/lib/docker 或 /var/lib/containerd）磁盘使用超过 kubelet 驱逐阈值

**资源准备**：
1. 确认应用 A 已正常运行，有多个副本分布在不同节点
2. 确认目标节点的容器运行时磁盘当前使用率距离驱逐阈值有一定空间
3. 确认监控系统可观测节点磁盘使用率和 Pod 驱逐事件

**演练步骤**：
1. 查看目标节点 kubelet 配置的驱逐阈值（默认 `imagefs.available < 15%`）
2. 使用 chaosblade 对目标节点的容器运行时目录注入磁盘填充，使其超过驱逐阈值：
   ```bash
   blade create k8s node-disk fill \
     --names <节点名> \
     --path /var/lib/containerd \
     --percent 90 \
     --timeout 300 \
     --kubeconfig <路径>
   ```
3. 等待 kubelet 检测到 DiskPressure 并触发 Pod 驱逐
4. 观察应用 A 的 Pod 驱逐和重建行为

**注入验证**：
1. 执行 `kubectl describe node <节点名>`，确认 Conditions 中 DiskPressure 为 True
2. 执行 `kubectl get pods --field-selector=status.phase=Failed`，确认有 Pod 被 Evicted
3. 查看被驱逐 Pod 的详情，确认 reason 为 `The node was low on resource: ephemeral-storage`
4. 确认应用 A 在其他节点重建 Pod

**注入恢复**：
1. 等待 chaosblade 实验自动超时恢复（300 秒内），或执行 `blade destroy <UID>`
2. 等待节点磁盘空间释放
3. 清理 Evicted Pod：`kubectl delete pods --field-selector=status.phase=Failed`

**恢复验证**：
1. 执行 `kubectl describe node <节点名>`，确认 DiskPressure 恢复为 False
2. 确认应用 A 的 Pod 在正常节点上运行
3. 确认节点磁盘使用率恢复到安全水位

**基准事实**：
- **根因**：容器运行时目录磁盘使用超过 kubelet 驱逐阈值（DiskPressure），kubelet 按优先级驱逐 Pod 以释放磁盘空间
- **必现现象**：节点 DiskPressure=True；Pod 被 Evicted；reason 为 ephemeral-storage 不足
