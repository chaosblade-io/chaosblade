**用例名称** Sidecar容器被移除 导致 Container_被删除

**故障现象**：
1. Pod 内指定 Sidecar 容器被强制删除（不是进程杀死，而是整个容器被移除）
2. kubelet 检测到容器缺失后按 Pod spec 定义重建该容器
3. 存在短暂的服务中断窗口（从容器被删除到新容器启动就绪）
4. 容器 ID 发生变化（新容器实例），所有临时状态丢失

**资源准备**：
1. 确认目标 Pod 包含多个容器，明确 Sidecar 容器名称
2. 确认目标 Pod 所在 namespace 和 labels
3. 记录注入前 Sidecar 容器的 Container ID 和 restartCount

**演练步骤**：
1. 确认 Pod 内容器列表，获取 Sidecar 容器名称：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].name}' --kubeconfig <kubeconfig-path>
   ```
2. 记录注入前 Sidecar 容器的 Container ID：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[?(@.name=="<sidecar-container-name>")].containerID}' --kubeconfig <kubeconfig-path>
   ```
3. 记录注入前各容器 RestartCount：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[*].restartCount}' --kubeconfig <kubeconfig-path>
   ```
4. 使用 ChaosBlade 对 Sidecar 容器注入删除故障：
   ```bash
   blade create k8s container-container remove \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --container-names <sidecar-container-name> \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
5. 观察容器被删除后 kubelet 重建行为及服务中断时长

**注入验证**：
1. 执行 `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[?(@.name=="<sidecar-container-name>")].restartCount}'`，确认 restartCount 增加
2. 执行 `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[?(@.name=="<sidecar-container-name>")].containerID}'`，确认容器 ID 已变化（新容器实例）
3. 执行 `kubectl describe pod <pod-name> -n <namespace>`，确认 Events 中显示容器被 killed 并重建
4. 主容器 restartCount 不变，Pod 整体状态保持 Running

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动停止删除行为
3. 说明：容器被删除后 kubelet 会根据 Pod spec 自动重建，ChaosBlade timeout 控制"持续删除容器"的时长

**恢复验证**：
1. 执行 `kubectl get pod <pod-name> -n <namespace>`，确认 Pod 状态为 Running 且 Sidecar 容器 restartCount 不再增长
2. 确认 Sidecar 容器进程正常运行且服务恢复可用
3. 确认 Sidecar 容器 ID 稳定不再变化

**基准事实**：
- **根因**：Sidecar 容器被 ChaosBlade 强制删除（底层调用 CRI 接口移除容器），kubelet 检测后重建，与 process kill 不同的是整个容器实例被销毁重建
- **必现现象**：Sidecar 容器 restartCount 增长；容器 ID 发生变化（新实例）；容器内临时状态（文件、内存缓存）丢失；Events 显示容器 killed 并重建；主容器不受影响

---

**降级方案（kubectl-native）**

> ✖ 此场景无可行的 kubectl-native 降级方案。

原因：容器移除需要 CRI（Container Runtime Interface）层面的直接操作，kubectl exec 无法实现容器的自我删除。ChaosBlade 通过 Operator 调用节点上的 CRI 接口完成此操作。

替代建议：如需模拟容器丢失效果，可考虑使用 `kubectl delete pod <pod-name> -n <namespace>` 删除整个 Pod 触发重建，但这会影响 Pod 内所有容器（而非仅目标 Sidecar）。
