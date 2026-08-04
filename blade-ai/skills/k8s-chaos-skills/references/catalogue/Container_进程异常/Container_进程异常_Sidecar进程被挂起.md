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
> 两条路径按 sidecar 容器内是否有 `kill`/`pgrep` 二选一。

---

**路径 A —— sidecar 容器内有 `kill` 和 `pgrep`**

前提条件：目标容器内需有 `kill` 命令和 `pgrep` 工具可用。先确认：
```bash
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> \
  -- sh -c 'command -v kill; command -v pgrep'
```
两者都有输出才走本路径。sidecar 常是 distroless 的极简镜像（istio-proxy、各类
driver-registrar 等），缺失时走路径 B。

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

---

**路径 B —— sidecar 容器内没有 `kill`：节点侧 cgroup freezer**

从节点侧冻结**该 sidecar 容器**的 cgroup，不需要容器内有任何二进制。同 Pod 内的
其它容器（含主容器）**不受影响** —— 每个容器有独立的 `.scope` cgroup，这正是
本用例「只挂起 sidecar」语义所要求的。

> **仅适用 cgroup v1。** 先判定版本，v2 的路径与写法不同（`cgroup.freeze`，写 `1`/`0`），
> 本用例未覆盖 v2，判定为 v2 时应停止并报告不支持：
> ```bash
> kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
>   -- chroot /host stat -fc %T /sys/fs/cgroup
> ```
> 输出 `tmpfs` → v1，继续；输出 `cgroup2fs` → v2，**停止**。

1. 定位目标节点，并取 sidecar 容器的 containerID。**多容器 Pod 必须按容器名精确取**
   （`containerStatuses[0]` 是不确定的那一个，取错会冻错容器）：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.nodeName}
   # 列出全部容器名与 ID，从中挑 sidecar 那一条
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath={range.status.containerStatuses[*]}{.name}{.containerID}{end}
   ```

2. 取该容器的 freezer cgroup 路径。**推荐用 crictl 按容器名反查，不要手工拼路径**
   —— 路径含 QoS 层（`kubepods-burstable.slice` / `kubepods-besteffort.slice`，
   Guaranteed 则无此层）和 Pod UID 的下划线化，手工拼极易错：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'CID=$(crictl ps -q --name <sidecar-container-name> | head -1); \
        find /sys/fs/cgroup/freezer -type d -name "*$CID*"'
   ```
   输出形如（实测样例，Burstable QoS）：
   ```
   /sys/fs/cgroup/freezer/kubepods.slice/kubepods-burstable.slice/\
   kubepods-burstable-pod<UID_下划线>.slice/cri-containerd-<containerID>.scope
   ```
   **同名容器跨 Pod 时 `--name` 会命中多个** —— 用 `crictl ps --pod <podSandboxId>
   --name <容器名> -q` 限定到目标 Pod，或核对上一步拿到的 containerID 前缀。

3. 注入 —— **必须先武装定时解冻，再冻结**：
   ```bash
   # ⚠️ 顺序不可颠倒：冻结后无法 exec 进该容器，且 debug pod 可能先于解冻被删除。
   #    定时器由宿主机 systemd(PID 1) 管理，不受 debug pod 生命周期影响。
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c '
       P=<步骤2得到的路径>/freezer.state
       systemd-run --on-active=<recovery-seconds>s --unit=blade-thaw-<containerID前12位> \
         sh -c "echo THAWED > $P" &&
       echo FROZEN > $P
     '
   ```

验证：
```bash
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host cat <步骤2得到的路径>/freezer.state
```
应输出 `FROZEN`。再确认**主容器未受影响**（这是本用例与「整个 Pod 挂起」的分界）：
```bash
kubectl exec <pod-name> -c <主容器名> -n <namespace> -- echo alive
```
应正常返回 `alive`。sidecar 侧的业务影响按其职责验证（如 istio-proxy 被冻则出入流量中断、
driver-registrar 被冻则插件注册失效）。

恢复：
```bash
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host sh -c 'echo THAWED > <步骤2得到的路径>/freezer.state'
```

注意事项：
- **冻结的是该容器的全部进程**，不是路径 A 的单个进程；差异要在演练报告里说明
- **sidecar 的 liveness 探针会失败** → 该容器被 kubelet 单独重启 → cgroup 目录消失、
  故障自动结束。此时记录为「故障导致容器重启」，且**不要再写 THAWED**（路径已不存在）
- 冻结期间 `kubectl exec -c <sidecar>` 会挂住；主容器仍可正常 exec
- `freezer.state` 权限实测为 `-rw-r--r-- root root`，`chroot /host` 后可写
- 路径依赖 **systemd cgroup driver**（`.slice`/`.scope` 命名）；cgroupfs driver 的路径形如
  `/sys/fs/cgroup/freezer/kubepods/burstable/pod<UID>/<containerID>/`，
  本用例未覆盖 —— 用步骤 2 的 `find` 实测确认后再操作

