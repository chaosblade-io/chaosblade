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

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令模拟 CNI 插件异常。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+），或可操作 CNI DaemonSet

注入命令：
```bash
# 方式A：通过 kubectl debug node 挂起 CNI 插件进程
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'kill -STOP $(pidof terway-daemon || pidof cilium-agent || pidof calico-node)'
# 方式B：删除节点上的 CNI Pod（先 cordon 防止重建）
kubectl cordon <node-name>
kubectl delete pod -n kube-system -l app=terway --field-selector spec.nodeName=<node-name>
```

恢复命令：
```bash
# 方式A：恢复 CNI 插件进程
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'kill -CONT $(pidof terway-daemon || pidof cilium-agent || pidof calico-node)'
# 方式B：uncordon 节点，等待 DaemonSet 重建 CNI Pod
kubectl uncordon <node-name>
# 删除 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
```

注意事项：
- CNI 插件名称因集群而异：Terway（阿里云）、Cilium、Calico 等，需根据实际环境确认进程名
- 方式B 删除 CNI Pod 后，如未 cordon 节点，DaemonSet 会立即重建，故障窗口极短
- 与 ChaosBlade 不同，此方式无自动超时恢复，必须手动恢复进程或 uncordon 节点
