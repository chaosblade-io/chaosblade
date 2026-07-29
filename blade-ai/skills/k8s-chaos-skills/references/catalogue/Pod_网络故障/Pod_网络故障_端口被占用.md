**用例名称** 端口被占用 导致 Pod_网络故障

**故障现象**：
1. Pod 内服务端口被强制占用，应用无法监听预期端口
2. 应用启动失败或现有连接中断，日志报 `Address already in use`
3. 健康检查可能失败（如探针使用被占端口），导致 Pod 被重启
4. Service 端点无法正常接收流量

**资源准备**：
1. 确认目标应用已正常运行，且在特定端口提供服务
2. 确认目标 Pod 的标签选择器和命名空间
3. 确认需要占用的端口号（应用监听的端口）

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 确认目标端口当前正在被应用正常使用：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- netstat -tlnp
   ```
3. 使用 ChaosBlade 对目标 Pod 注入端口占用：
   ```bash
   blade create k8s pod-network occupy \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --port <port> \
     --force \
     --timeout 300 \
     --kubeconfig <kubeconfig-path>
   ```
   - `--port`：要占用的端口（必填）
   - `--force`：强制杀死当前使用该端口的进程后占用（不加则端口被占时注入失败）
4. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 确认端口已被 ChaosBlade 进程占用：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- netstat -tlnp
   ```
   确认监听进程已变更为 ChaosBlade 注入的进程
2. 确认原应用进程已停止监听该端口（使用 `--force` 时原进程被杀）
3. 验证服务不可达：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 http://localhost:<port>
   ```
   确认连接失败或返回非预期响应
4. 查看应用日志确认进程被杀/端口冲突相关错误

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
2. 被杀的应用进程通常由容器 init 系统或 K8s 探针重启机制自动恢复
3. 若应用未自动恢复，可重启 Pod：`kubectl delete pod <pod-name> -n <namespace>`

**恢复验证**：
1. 确认应用重新监听目标端口：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- netstat -tlnp
   ```
2. 确认服务恢复可达：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 http://localhost:<port>
   ```
3. 确认 Pod 状态为 Running 且 Ready
4. 确认 Service Endpoints 包含该 Pod

**基准事实**：
- **根因**：Pod 内目标端口被 ChaosBlade 强制占用，原应用进程被杀死（`--force`），模拟端口冲突或进程异常退出场景
- **必现现象**：应用进程停止监听目标端口；服务请求失败；健康检查可能失败触发 Pod 重启

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效 Pod 内端口占用。

前提条件：容器内需有 `nc`（netcat）或 `socat` 工具

注入命令：
```bash
# 通过 kubectl exec 在 Pod 内占用端口（先杀原进程；后台进程必须重定向，否则 exec 会挂到超时）
kubectl exec <pod-name> -n <namespace> -- sh -c '
  kill $(fuser <port>/tcp 2>/dev/null) 2>/dev/null
  ( nc -l -p <port> -k ) >/dev/null 2>&1 &
  echo $! > /tmp/chaos_port.pid
  ( sleep <duration>; kill $(cat /tmp/chaos_port.pid) 2>/dev/null; rm -f /tmp/chaos_port.pid ) >/dev/null 2>&1 &
'
# 如容器无 nc，可用 socat（同样重定向 + PID 落盘 + 定时自恢复）：
kubectl exec <pod-name> -n <namespace> -- sh -c '
  kill $(fuser <port>/tcp 2>/dev/null) 2>/dev/null
  ( socat TCP-LISTEN:<port>,fork,reuseaddr /dev/null ) >/dev/null 2>&1 &
  echo $! > /tmp/chaos_port.pid
  ( sleep <duration>; kill $(cat /tmp/chaos_port.pid) 2>/dev/null; rm -f /tmp/chaos_port.pid ) >/dev/null 2>&1 &
'
```

恢复命令：
```bash
# 首选：按 PID 文件精确终止占用进程
kubectl exec <pod-name> -n <namespace> -- sh -c 'kill $(cat /tmp/chaos_port.pid) 2>/dev/null; rm -f /tmp/chaos_port.pid'
# 兜底：按命令特征 kill（用 ps+kill，比 fuser/pkill 通用，精简镜像常无 fuser）
kubectl exec <pod-name> -n <namespace> -- sh -c \
  "ps -o pid,args 2>/dev/null | grep -E '[n]c -l|[s]ocat TCP-LISTEN' | awk '{print \$1}' | xargs -r kill -9"
# 应用进程通常由容器 init 系统或 K8s 探针重启机制自动恢复；若未恢复，重启 Pod：
kubectl delete pod <pod-name> -n <namespace>
```

注意事项：
- 大多数精简容器镜像不包含 nc/socat，可能需使用 shell 内置的重定向：`( sh -c 'exec 3<>/dev/tcp/0.0.0.0/<port>; while true; do read line <&3; done' ) >/dev/null 2>&1 &`（同样必须重定向后台化）
- 后台占用进程必须 `>/dev/null 2>&1 &`，否则继承 exec 管道会导致 `kubectl exec` 挂起到超时（进程其实已启动）
- 自动恢复基于 PID 文件（`/tmp/chaos_port.pid`）+ 定时 kill 补齐了 ChaosBlade `--timeout` 的自恢复能力；恢复兜底优先 `ps+kill` 而非 `fuser -k`（精简镜像常无 fuser）
- `--force` 效果通过先 `kill $(fuser <port>/tcp)` 实现，会杀死当前监听该端口的进程
