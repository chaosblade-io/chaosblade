**用例名称** Sidecar进程被杀死 导致 Container_进程异常

**故障现象**：
1. Sidecar 容器内主进程被杀死，该容器因主进程退出而重启
2. Pod 内其他容器（主容器）不受影响，Pod 本身不重启
3. Sidecar 提供的能力暂时中断（如日志停止采集、代理不可用、监控数据丢失）
4. Pod Events 中显示特定容器重启记录，Pod 整体状态仍为 Running

**资源准备**：
1. 确认目标 Pod 包含多个容器，明确 Sidecar 容器名称
2. 确认 Sidecar 容器内实际进程名（必须通过 ps 确认，不可猜测）
3. 确认目标 Pod 所在 namespace 和 labels

**演练步骤**：
1. 确认 Pod 内容器列表，获取 Sidecar 容器名称：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].name}' --kubeconfig <kubeconfig-path>
   ```
2. 进入 Sidecar 容器确认实际进程名：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- ps aux --kubeconfig <kubeconfig-path>
   ```
3. 记录注入前 Pod 状态和各容器 RestartCount：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[*].restartCount}' --kubeconfig <kubeconfig-path>
   ```
4. 使用 ChaosBlade 对 Sidecar 容器注入进程杀死故障：
   ```bash
   blade create k8s container-process kill \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --container-names <sidecar-container-name> \
     --process <进程名> \
     --signal 15 \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
5. 观察 Sidecar 容器重启行为及主容器是否受影响

**注入验证**：
1. 执行 `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses}'`，确认目标 Sidecar 容器 restartCount 增加
2. 确认主容器仍正常运行，restartCount 未变化
3. 执行 `kubectl describe pod <pod-name> -n <namespace>`，确认 Events 中有目标容器重启记录
4. 验证 Sidecar 提供的服务在重启期间中断（如代理端口不可达、日志缺失）

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动停止杀进程
3. 说明：进程被杀后容器 entrypoint 会自动拉起进程，ChaosBlade timeout 控制"持续杀进程"的时长

**恢复验证**：
1. 执行 `kubectl get pod <pod-name> -n <namespace>`，确认 Pod 状态为 Running 且 Sidecar 容器 restartCount 不再增长
2. 确认 Sidecar 容器内进程稳定运行（PID 不再变化）
3. 确认 Sidecar 提供的服务恢复正常

**基准事实**：
- **根因**：Sidecar 容器内主进程被外部信号（SIGTERM）杀死，容器退出后被 kubelet 重启，但 Pod 内其他容器不受影响
- **必现现象**：目标 Sidecar 容器 restartCount 增长；容器 Last State 为 terminated（Exit Code 非 0）；主容器 restartCount 不变且持续运行；Pod 整体状态保持 Running

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。

前提条件：目标容器内需有 `kill` 命令和 `pgrep` 工具可用

注入命令：
```bash
# 对指定容器内的目标进程发送信号杀死
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c 'kill -15 $(pgrep -f <process-name>)'
# 常用信号：15 (SIGTERM, 优雅终止) 或 9 (SIGKILL, 强制杀死)
# 分步执行：
# 1. 获取进程 PID
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- pgrep -f <process-name>
# 2. 发送信号
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- kill -15 <pid>
```

恢复命令：
```bash
# 容器编排自动重启：进程被杀后，容器 entrypoint 会自动重启进程
# 无需手动恢复，但如需停止“持续杀进程”的行为，停止执行 kill 命令即可
```

注意事项：
- 必须通过 `ps aux` 或 `pgrep` 确认实际进程名，不可猜测
- 单次 kill 后容器会自动重启；若需模拟“持续杀进程”效果（类似 ChaosBlade 的 timeout 行为），需配合 `watch` 或循环脚本
- 与 ChaosBlade 相比，kubectl exec 方式缺少自动超时停止机制
