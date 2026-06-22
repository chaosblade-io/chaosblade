**用例名称** 应用主进程异常 导致 Pod_进程被杀死

**故障现象**：
1. 容器内应用主进程被杀死，容器因主进程退出而重启
2. Pod RestartCount 持续增长
3. 若持续杀进程超过退避阈值，Pod 状态可能进入 CrashLoopBackOff
4. Pod Events 中显示 `Back-off restarting failed container`

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认目标 Pod 所在 namespace 和 labels
3. 确认容器内实际进程名（不可凭服务名猜测）

**演练步骤**：
1. 记录应用 A 当前 Pod 状态和 RestartCount：
   ```bash
   kubectl get pods -l <labels> -n <namespace> -o wide
   ```
2. **验证实际进程名（必须）** — 进入容器确认目标进程的二进制名称：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ps aux
   ```
   注意：实际进程名可能与服务名不同（如 prometheus 服务的实际二进制为 `prometheus-agent-linux`），必须以 ps 输出为准
3. 使用 ChaosBlade 注入进程杀死故障：
   ```bash
   blade create k8s pod-process kill \
     --process <实际进程名> \
     --signal 15 \
     --namespace <namespace> \
     --labels <labels> \
     --timeout <秒> \
     --kubeconfig <路径>
   ```
   说明：`--signal 15` 发送 SIGTERM，如需强制杀死可用 `--signal 9`（SIGKILL）
4. 观察 Pod 重启行为

**注入验证**：
1. 执行 `kubectl get pods -l <labels> -n <namespace>`，确认 RESTARTS 数相比注入前增加
2. 执行 `kubectl exec <pod-name> -n <namespace> -- ps aux`，确认主进程 PID 已变化（容器重启后 PID 重新分配）
3. 执行 `kubectl describe pod <pod-name> -n <namespace>`，确认 Events 中有 `Back-off restarting failed container` 或 Last State 显示 terminated 且 reason 为 Error/Signal
4. 若 timeout 期间持续杀进程，确认 Pod 状态是否进入 CrashLoopBackOff

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动停止杀进程
3. 说明：进程被杀后容器 entrypoint 会自动拉起主进程，ChaosBlade 的 timeout 控制的是"持续杀进程"的时长，超时后不再杀进程，容器自行恢复

**恢复验证**：
1. 执行 `kubectl get pods -l <labels> -n <namespace>`，确认 Pod 状态为 Running 且 RESTARTS 不再增长
2. 执行 `kubectl exec <pod-name> -n <namespace> -- ps aux`，确认主进程稳定运行（PID 不再变化）
3. 确认应用 A 服务正常响应

**基准事实**：
- **根因**：容器内应用主进程被外部信号（SIGTERM/SIGKILL）杀死，导致容器退出并被 kubelet 重启
- **必现现象**：Pod RestartCount 增长；容器 Last State 为 terminated（Exit Code 非 0）；进程 PID 在重启后变化；Events 显示容器重启记录
