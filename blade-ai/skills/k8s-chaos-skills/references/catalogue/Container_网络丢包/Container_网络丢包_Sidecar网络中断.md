**用例名称** Sidecar网络中断 导致 Container_网络丢包

**故障现象**：
1. Sidecar 容器绑定端口的网络流量被丢弃，该端口服务完全不可达
2. 如果是代理类 Sidecar（如 istio-proxy），所有经过代理的业务流量中断
3. 主容器直接对外的端口流量可能不受影响（取决于网络拓扑和 Sidecar 角色）
4. Sidecar 日志中出现大量连接超时或 reset 错误

**资源准备**：
1. 确认目标 Pod 包含多个容器，明确 Sidecar 容器名称
2. 确认 Sidecar 容器监听的端口（如 istio-proxy 的 15001/15006）
3. 确认目标 Pod 所在 namespace 和 labels

**演练步骤**：
1. 确认 Pod 内容器列表，获取 Sidecar 容器名称：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].name}' --kubeconfig <kubeconfig-path>
   ```
2. 确认 Sidecar 容器监听的端口：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- netstat -tlnp --kubeconfig <kubeconfig-path>
   ```
3. 记录注入前通过 Sidecar 端口的连通性：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- curl -s -o /dev/null -w '%{http_code}' http://localhost:<port>/health --kubeconfig <kubeconfig-path>
   ```
4. 使用 ChaosBlade 对 Sidecar 容器注入网络丢包故障：
   ```bash
   blade create k8s container-network drop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --container-names <sidecar-container-name> \
     --source-port <port> \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
   说明：Container 共享 Pod 网络 namespace，drop 通过 iptables 针对容器进程绑定端口的流量规则实现
5. 观察通过 Sidecar 的流量是否完全中断

**注入验证**：
1. 通过 Sidecar 代理端口的访问完全失败（连接超时或被拒绝）
2. 主容器直接暴露的端口仍可正常访问（如果存在直连路径）
3. 执行 `kubectl logs <pod-name> -c <sidecar-container-name> -n <namespace>`，确认日志中出现连接错误
4. 检查依赖该 Sidecar 代理的上下游服务是否出现超时或 5xx 错误

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动清除 iptables 规则

**恢复验证**：
1. 通过 Sidecar 代理端口的访问恢复正常
2. 执行 `kubectl logs <pod-name> -c <sidecar-container-name> -n <namespace> --tail=10`，确认无新增连接错误
3. 确认依赖该 Sidecar 的业务服务恢复正常响应

**基准事实**：
- **根因**：Sidecar 容器绑定端口的网络流量被 iptables 规则丢弃，模拟 Sidecar 网络隔离场景
- **必现现象**：通过 Sidecar 端口的流量 100% 丢弃；Sidecar 日志出现连接超时/reset 错误；主容器直接端口不受影响（在非代理拓扑下）；依赖 Sidecar 代理的服务调用失败

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，用以下 kubectl 原生命令实现等效故障注入。

前提条件：需要 `iptables` 或 **iproute2** `tc` 之一。**两者在精简镜像里通常都没有** ——
先判定，再选路径：

```bash
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c \
  'command -v iptables || echo NO_IPTABLES; tc -Version'
```
- 有 `iptables` 且容器有 `NET_ADMIN` → 路径 A
- `tc -Version` 输出 `tc utility, iproute2-<版本>` 且有 `NET_ADMIN` → 路径 B
- 两者都没有，或 `tc -Version` 输出 `BusyBox v...`（BusyBox applet 不支持 netem），
  或容器 `CapEff` 全零 → 路径 C

注入命令：
```bash
# ── 路径 A：容器内有真 iptables + NET_ADMIN
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -A OUTPUT -j DROP
# 仅丢弃特定端口流量：
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -A OUTPUT -p tcp --sport <port> -j DROP

# ── 路径 B：容器内有真 tc + NET_ADMIN（100% 丢包等效于网络中断）
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- tc qdisc add dev eth0 root netem loss 100%

# ── 路径 C：容器内两者都不可用（精简镜像的常态）。临时容器与 Pod 内各容器共享同一个网络
#    命名空间，所以在临时容器里操作 eth0 就等于操作这个 Pod 的网络栈；工具来自调试镜像
# ── 路径 C：容器内无可用工具（精简镜像的常态）。
#    先建【长驻】临时容器作为载体 —— 必须 sleep 保活；若把 tc 直接交给 kubectl debug，
#    命令跑完容器即终止，后续 `kubectl exec -c <debugger>` 会报 container not found，
#    故障将无法恢复（已实测）。
kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
  --target=<container-name> --profile=netadmin --quiet -- sleep <duration>

# 取载体名，等它进入 running
kubectl get pod <pod-name> -n <namespace> \
  -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}{"="}{.state}{"\n"}{end}'

# 经载体注入：载体与目标容器共享网络命名空间，操作 eth0 即操作目标 Pod 的网卡
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc add dev eth0 root netem loss 100%
```
- `<verified-cluster-image>`：必须是当前集群**已验证可拉取**且含 **iproute2**（非 BusyBox）的镜像。
  可靠找法：看集群里已经在跑的镜像，它们必然可拉取 ——
  `kubectl get pods -A -o jsonpath='{..image}'`。CNI / 网络组件（terway、calico、cilium 等）
  通常自带 iproute2，因为它们本身就要做流量整形。选定后用
  `kubectl debug ... -- tc -Version` 确认输出是 `tc utility, iproute2-...` 而非 `BusyBox v...`；
  拉不动时 Pod 事件会出现 `ErrImagePull` / `ImagePullBackOff`
- `--quiet`：不进入交互附着；**不要加 `-it`**
- 载体名形如 `debugger-xxxxx`，注入/验证/恢复三步都要用同一个

恢复命令：
```bash
# ── 路径 A：与注入命令逐字对应删除
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -D OUTPUT -j DROP
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -D OUTPUT -p tcp --sport <port> -j DROP

# ── 路径 B
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- tc qdisc del dev eth0 root

# ── 路径 C：复用注入时那个临时容器，不要新建
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc del dev eth0 root
```
临时容器名遗失时用
`kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.ephemeralContainerStatuses[*].name}'`
取回。

注意事项：
- **Pod 内所有容器共享同一个网络 namespace**，所以无论从哪个容器（含临时容器）下手，
  iptables/tc 规则都作用于整个 Pod 的网络栈。要限定影响范围只能靠 `--sport`/`--dport`
  或 netem 的作用方向，而不是靠 `-c` 选容器
- 全量中断会切断监控和健康检查，可能触发 Pod 重启
- 无自动超时恢复，必须手动删除规则；Pod 重启会让规则自动消失（不持久化）
- 恢复 iptables 用 `-D` 逐字对应删除，不要用 `iptables -F`——那会清掉 Pod 原有的其他规则
- 走过路径 C 的话：**临时容器无法从运行中的 Pod 移除**（Kubernetes 既定行为），只能随 Pod 重建消失。
  `tc qdisc del` 成功即代表故障已恢复；如需立即清理须删除该 Pod 让上层控制器重建 ——
  这是额外的变更动作，须经确认后再做
