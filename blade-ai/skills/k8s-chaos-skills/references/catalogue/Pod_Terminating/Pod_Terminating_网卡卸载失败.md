**用例名称** 网卡卸载失败 导致 Pod_Terminating

**故障现象**：
1. Pod 状态长时间停留在 Terminating
2. 容器已停止，但 Pod sandbox 清理失败
3. Events 或 kubelet 日志中显示 CNI DEL 调用失败或网络资源释放异常

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群使用 ENI/Terway 等需要显式清理网络资源的 CNI 插件
3. 确认监控系统可观测 Pod 状态和 CNI 插件日志

**演练步骤**：
1. 定位应用 A 的 Pod 所在节点
2. 使用 chaosblade 挂起节点上的 CNI 插件进程（如 terway-daemon），模拟 CNI 响应异常：
   ```bash
   blade create k8s node-process stop \
     --names <节点名> \
     --process terway \
     --timeout 120 \
     --kubeconfig <路径>
   ```
   或删除 CNI 插件 DaemonSet 中该节点的 Pod（先 cordon 节点防止重建）
3. 删除应用 A 的 Pod，触发 Terminating 流程
4. 观察 Pod Terminating 状态

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Terminating 且长时间未消失
2. 查看 kubelet 日志（`journalctl -u kubelet`），确认有 CNI DEL 调用超时或失败的记录
3. 确认 CNI 插件进程处于 stopped 状态或不可用

**注入恢复**：
1. 恢复 CNI 插件进程：等待 chaosblade 超时或执行 `blade destroy <UID>`
2. 若删除了 CNI Pod：uncordon 节点，等待 CNI DaemonSet Pod 重建
3. kubelet 将自动重试 sandbox 清理

**恢复验证**：
1. 确认 CNI 插件进程恢复正常
2. 执行 `kubectl get pods`，确认 Terminating 的 Pod 已被完全清理
3. 确认节点网络资源（ENI/IP）已释放
4. 确认新 Pod 可以正常创建和分配网络

**基准事实**：
- **根因**：CNI 插件异常或不可用，导致 Pod 删除时网卡/ENI 资源无法正常释放，sandbox 清理失败，Pod 卡在 Terminating
- **必现现象**：Pod Terminating 持续；kubelet 日志显示 CNI DEL 失败；网络资源未释放
