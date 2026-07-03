**用例名称** Sidecar进程被挂起 导致 Container_进程异常

**故障现象**：
1. Sidecar 容器内主进程被 SIGSTOP 信号挂起，进程不退出但完全停止处理请求
2. 如果 Sidecar 容器配置了独立 Liveness 探针，探针超时后可能触发容器重启
3. 如果无 Liveness 探针，Sidecar 持续不可用但 Pod 状态仍显示 Running
4. 模拟 Sidecar 死锁/卡死场景，主容器依赖 Sidecar 的功能不可用

**资源准备**：
1. 确认目标 Pod 包含多个容器，明确 Sidecar 容器名称
2. 确认 Sidecar 容器内实际进程名（必须通过 ps 确认）
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
3. 记录注入前 Sidecar 服务状态（如探针配置、端口响应情况）：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[?(@.name=="<sidecar-container-name>")].livenessProbe}' --kubeconfig <kubeconfig-path>
   ```
4. 使用 ChaosBlade 对 Sidecar 容器注入进程挂起故障：
   ```bash
   blade create k8s container-process stop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --container-names <sidecar-container-name> \
     --process <进程名> \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
5. 观察 Sidecar 容器进程挂起后的行为（是否触发探针重启、主容器是否受影响）

**注入验证**：
1. 进入 Sidecar 容器执行 `ps aux`，确认目标进程状态为 T（Stopped）
2. 验证 Sidecar 提供的服务完全无响应（如代理端口连接超时、日志停止写入）
3. 确认主容器仍正常运行但依赖 Sidecar 的功能不可用
4. 若配置了 Liveness 探针，执行 `kubectl describe pod` 确认是否触发探针失败事件

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动发送 SIGCONT 恢复进程
3. 说明：ChaosBlade stop 动作在超时后会自动恢复进程，进程从挂起状态继续执行

**恢复验证**：
1. 进入 Sidecar 容器执行 `ps aux`，确认目标进程状态恢复为 S/R（非 T）
2. 确认 Sidecar 提供的服务恢复正常响应
3. 确认主容器依赖 Sidecar 的功能恢复可用

**基准事实**：
- **根因**：Sidecar 容器内主进程被 SIGSTOP 信号挂起，进程不退出但停止调度执行，模拟死锁/卡死场景
- **必现现象**：目标进程状态变为 T（Stopped）；Sidecar 服务完全无响应；Pod 状态仍为 Running（除非探针触发重启）；主容器本身正常但通过 Sidecar 的功能链路断裂

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。

前提条件：目标容器内需有 `kill` 命令和 `pgrep` 工具可用

注入命令：
```bash
# 对指定容器内的目标进程发送 SIGSTOP 信号挂起
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c 'kill -STOP $(pgrep -f <process-name>)'
# 或分步执行：
# 1. 获取进程 PID
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- pgrep -f <process-name>
# 2. 发送 SIGSTOP
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- kill -STOP <pid>
```

恢复命令：
```bash
# 对目标进程发送 SIGCONT 信号恢复执行
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c 'kill -CONT $(pgrep -f <process-name>)'
```

注意事项：
- 必须通过 `ps aux` 或 `pgrep` 确认实际进程名，不可猜测
- 与 ChaosBlade 相比，kubectl exec 方式缺少自动超时恢复机制，需手动发送 SIGCONT
- 如容器内无 `pgrep`，可用 `ps aux | grep <process-name>` 替代获取 PID
