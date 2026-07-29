**用例名称** 节点端口占用 导致 Node_网络故障

**故障现象**：
1. 节点上的关键端口被占用（如 kubelet 10250、NodePort 范围 30000-32767、应用 HostPort）
2. 使用该端口的系统组件或应用无法正常工作
3. NodePort 类型 Service 无法在该节点接收流量
4. 模拟节点端口资源冲突或恶意进程占用场景

**资源准备**：
1. 确认目标节点名称
2. 确认需要占用的端口号（NodePort、HostPort 或系统组件端口）
3. 确认 ChaosBlade Operator 已部署（DaemonSet 通道）或具备节点 SSH 访问权限（SSH 通道）

**演练步骤**：
1. 确认目标节点和端口使用情况：
   ```bash
   kubectl get nodes <node-name>
   kubectl get svc --all-namespaces -o jsonpath='{range .items[?(@.spec.type=="NodePort")]}{.metadata.name}{"\t"}{.spec.ports[*].nodePort}{"\n"}{end}'
   ```
2. 选择执行通道并注入节点端口占用：

   **方式一：DaemonSet 通道**
   ```bash
   blade create k8s node-network occupy \
     --names <node-name> \
     --port <port> \
     --force \
     --timeout 300 \
     --kubeconfig <kubeconfig-path>
   ```

   **方式二：SSH 通道**
   ```bash
   blade create k8s node-network occupy \
     --port <port> \
     --force \
     --channel ssh \
     --ssh-host <node-ip> \
     --ssh-user root \
     --timeout 300
   ```
   - `--port`：要占用的端口（必填）
   - `--force`：强制杀死当前使用该端口的进程后占用
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 确认端口已被占用（通过 SSH 或 debug Pod 检查节点）：
   ```bash
   kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host ss -tlnp | grep <port>
   ```
2. 若占用的是 NodePort，验证该节点上 Service 不可达：
   ```bash
   curl --connect-timeout 5 http://<node-ip>:<nodeport>
   ```
   确认连接失败
3. 若占用的是 kubelet 端口（10250），检查节点健康状态：
   ```bash
   kubectl get nodes <node-name>
   ```
   观察是否变为 NotReady
4. 检查相关事件和系统组件日志

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
2. 或等待 `--timeout` 到期自动恢复
3. 被杀的系统组件（如 kubelet）通常由 systemd 自动重启

**恢复验证**：
1. 确认端口恢复正常使用：
   ```bash
   kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host ss -tlnp | grep <port>
   ```
   确认原进程重新监听
2. 确认节点状态恢复为 Ready
3. 确认 NodePort Service 恢复可达
4. 确认系统组件运行正常

**基准事实**：
- **根因**：节点宿主机上的指定端口被 ChaosBlade 强制占用，原使用该端口的进程被杀死（`--force`），模拟端口资源冲突或关键组件端口被抢占场景
- **必现现象**：目标端口被占用；原监听进程中断；依赖该端口的服务/组件不可用

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效节点端口占用。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；宿主机需包含 `nc`（netcat）或 `socat`（因 `chroot /host` 后工具从宿主机解析）；宿主机变更必须 `--profile=sysadmin`；禁用 `-it`

注入命令：
```bash
# 通过 kubectl debug node 在宿主机网络空间占用端口（nc 作为 debug Pod 主进程常驻，端口持续被占用；恢复=删除该 debug Pod）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host nc -l -p <port> -k
# 如需强制占用（先杀原进程再监听，exec 让 nc 取代 shell 成为主进程）：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'fuser -k <port>/tcp; exec nc -l -p <port> -k'
```

恢复命令：
```bash
# 终止占用端口的 nc 进程
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'fuser -k <port>/tcp'
# 删除 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
# 被杀的系统组件（如 kubelet）通常由 systemd 自动重启
```

注意事项：
- `nc -l -p` 在宿主机网络命名空间监听，效果与节点端口被占用等价
- 如需占用 UDP 端口，使用 `nc -l -u -p <port>`
- 与 ChaosBlade 不同，此方式无自动超时恢复，必须手动终止 nc 进程
