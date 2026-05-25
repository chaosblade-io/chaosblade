**用例名称** 容器运行时异常 导致 Pod_ContainerCreating

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `container runtime is not ready` 或 `rpc error` 相关错误
3. 节点上的 containerd/docker 进程异常或 hang，无法响应容器创建请求

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认目标节点上有多个应用副本（避免单点影响）
3. 确认监控系统可观测节点和容器运行时状态

**演练步骤**：
1. 定位应用 A 所在的目标节点
2. 使用 chaosblade 挂起（stop）目标节点上的 containerd 进程，模拟容器运行时 hang：
   ```bash
   blade create k8s node-process stop \
     --names <节点名> \
     --process containerd \
     --timeout 120 \
     --kubeconfig <路径>
   ```
3. 删除应用 A 在目标节点上的 Pod，触发重建
4. 观察新 Pod 的 ContainerCreating 状态

**注入验证**：
1. 执行 `kubectl get pods`，确认新 Pod 状态为 ContainerCreating
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示容器运行时相关错误
3. 确认目标节点状态可能变为 NotReady（RuntimeNotReady condition）

**注入恢复**：
1. 等待 chaosblade 实验自动超时恢复（120 秒内），containerd 进程自动恢复
2. 如超时后仍未恢复，通过 `blade destroy <UID>` 强制恢复
3. 等待容器运行时恢复正常，Pod 自动完成创建

**恢复验证**：
1. 确认 containerd 进程恢复正常运行
2. 执行 `kubectl get nodes`，确认目标节点恢复 Ready
3. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running

**基准事实**：
- **根因**：容器运行时（containerd/docker）进程异常、hang 或状态不一致，无法响应 kubelet 的容器创建请求
- **必现现象**：Pod ContainerCreating；Events 显示 runtime 相关 rpc error；节点可能 NotReady（RuntimeNotReady）
