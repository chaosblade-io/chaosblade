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

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现容器运行时挂起。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；恢复需 SSH 访问权限

注入命令：
```bash
# 通过 kubectl debug node 挂起 containerd 进程（SIGSTOP），并先武装定时自恢复
# ⚠️ 关键顺序：必须先用 systemd-run 武装 kill -CONT 定时恢复，再执行 kill -STOP。
#    STOP containerd 会冻结依赖 CRI 的 exec 会话本身，若恢复排在 STOP 之后，
#    恢复进程可能来不及登记 → containerd 永久挂起、节点无法自恢复。
#    systemd-run 的 transient timer 由宿主机 systemd(PID 1) 管理，debug Pod 删除也不影响。
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c '
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-containerd sh -c "kill -CONT $(pidof containerd)" &&
  kill -STOP $(pidof containerd)
'
```

恢复命令：

主恢复路径是注入时登记的 systemd 定时器（`--on-active=<recovery-seconds>s`），到期自动 `kill -CONT`，Agent 无需干预。

**提前恢复必须人工带外执行 —— Agent 不执行下面的命令。** containerd 已停止，`kubectl debug node` 需要新建容器，此刻物理上无法完成；SSH 是唯一通道：

```text
ssh root@<node-ip> 'kill -CONT $(pidof containerd)'
```

注意事项：
- 挂起 containerd 后节点上所有容器操作均失效（包括 kubectl debug/exec/logs），恢复只能通过 SSH 或等待超时自恢复
- 建议超时设置 30-120 秒
- 若节点使用 docker 而非 containerd，将 `pidof containerd` 替换为 `pidof dockerd`
