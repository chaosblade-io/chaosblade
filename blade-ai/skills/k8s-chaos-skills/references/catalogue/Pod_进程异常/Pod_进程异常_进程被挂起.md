**用例名称** 进程被挂起 导致 Pod_进程异常

**故障现象**：
1. 应用完全无响应但进程仍存在（与 kill 不同，进程不会退出）
2. Pod 状态仍为 Running（因为进程 PID 存在，容器未退出）
3. 所有入站请求超时，服务完全不可用
4. 若配置了 Liveness 探针，探针超时后 kubelet 会重启容器；若无 Liveness 探针则 Pod 持续 Running 但服务不可用（模拟应用死锁场景）

**资源准备**：
1. 确认目标应用已正常运行
2. 确认目标 Pod 所在 namespace 和 labels
3. 确认容器内实际进程名（不可凭服务名猜测）

**演练步骤**：
1. 记录应用当前 Pod 状态：
   ```bash
   kubectl get pods -l <labels> -n <namespace> -o wide
   ```
2. **验证实际进程名（必须）** — 进入容器确认目标进程的二进制名称：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ps aux
   ```
   注意：实际进程名可能与服务名不同，必须以 ps 输出为准
3. 使用 ChaosBlade 注入进程挂起故障：
   ```bash
   blade create k8s pod-process stop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --process <实际进程名> \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
   - `--process`：目标进程名，必须与 ps aux 输出一致
   - 原理：向目标进程发送 SIGSTOP 信号，进程被内核挂起，无法处理任何请求但不会退出
4. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在目标 Pod 内确认进程状态为 T（Stopped）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ps aux | grep <进程名>
   ```
   确认 STAT 列显示 T（表示进程被 SIGSTOP 挂起）
2. 验证应用端口无响应：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 localhost:<port>
   ```
   确认请求超时
3. 查看 Pod 事件确认 Liveness 探针状态：
   ```bash
   kubectl describe pod <pod-name> -n <namespace>
   ```
   若配置了 Liveness 探针，应观察到探针超时失败事件
4. 确认 Pod 状态仍为 Running（进程未退出，容器未重启——除非 Liveness 触发重启）

**注入恢复**：
1. 销毁 ChaosBlade 实验（会向进程发送 SIGCONT 恢复执行）：
   ```bash
   blade destroy <blade_uid>
   ```
2. 若 Liveness 探针已触发容器重启，等待新 Pod Ready 即可
3. 或等待 `--timeout` 600 秒到期后 ChaosBlade 自动发送 SIGCONT 恢复

**恢复验证**：
1. 确认进程恢复正常运行状态：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ps aux | grep <进程名>
   ```
   确认 STAT 列不再显示 T
2. 验证应用端口恢复响应：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 localhost:<port>
   ```
3. 确认服务正常处理请求，业务恢复可用

**基准事实**：
- **根因**：容器内应用主进程收到 SIGSTOP 信号被内核挂起，进程不退出但完全停止执行，无法处理任何请求
- **必现现象**：进程状态变为 T（Stopped）；应用端口请求全部超时；Pod 状态保持 Running（进程 PID 仍存在）；Liveness 探针超时失败（若已配置）

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效进程挂起注入。

前提条件：容器内需有 `kill` 和 `pgrep` 工具

注入命令：
```bash
# 挂起目标进程（发送 SIGSTOP）
kubectl exec <pod-name> -n <namespace> -- sh -c 'kill -STOP $(pgrep -f <process-name>)'
```

恢复命令：
```bash
# 恢复目标进程（发送 SIGCONT）
kubectl exec <pod-name> -n <namespace> -- sh -c 'kill -CONT $(pgrep -f <process-name>)'
```

注意事项：
- 必须确认实际进程名（通过 `ps aux` 确认），不可凭服务名猜测
- 无自动超时恢复机制，必须手动发送 SIGCONT 恢复
- 若 Liveness 探针已触发容器重启，进程会自动恢复（新容器中进程正常启动）
- 效果与 ChaosBlade 完全等价，ChaosBlade 内部也是发送 SIGSTOP/SIGCONT
