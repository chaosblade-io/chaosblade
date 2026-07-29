**用例名称** 后端不可达 导致 Service_负载均衡异常

**故障现象**：
1. Service 的 Endpoints 列表为空或部分后端不可用
2. 请求到 Service 出现 5xx 错误或连接超时
3. Ingress 后端健康检查失败

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service 和 Ingress
2. 确认监控系统可观测 Service 请求指标和 Endpoints 状态

**演练步骤**：
1. 定位应用 A 的 Service 和后端 Pod
2. 使用 chaosblade 对应用 A 的 Pod 注入故障，模拟后端不可达

**注入方式选择（重要）**：
- **推荐优先**：使用 `pod-process kill` 杀死后端 Pod 的主进程。优点：直接导致 Pod 重启或进入 CrashLoopBackOff，Endpoints 控制器会自动将其从 Endpoints 列表移除，故障效果明确且可观测
- **备选方式**：使用 `pod-network drop` 断开 Pod 网络。注意：如果主机 blade 二进制与集群不兼容，会退化为 kubectl exec 方式注入，导致恢复阶段必须通过 kubectl exec 执行 blade destroy，增加恢复复杂度
  - 如果目标是单个服务端口（如 MySQL 3306），推荐使用端口过滤以缩小爆炸半径：`pod-network drop --source-port 3306 --namespace <ns> --labels "app=<app>"`
  - 如果需要完全断开 Pod 网络：`pod-network drop --namespace <ns> --labels "app=<app>"`，注意此方式会影响所有端口，包括 DNS 和监控
- 选择原则：如果集群中已部署 ChaosBlade Operator 且主机 blade 可直接使用，两种方式均可；如果需要通过 kubectl exec 注入，优先选择 pod-process kill

**Readiness Probe 兼容性**（选择注入方式前必须评估）：
- 通过 `kubectl describe pod <pod>` 获取目标 Pod 的 Readiness Probe 类型
- `exec` 类型 Probe：在容器内通过 localhost 执行，**不受** pod-network drop 的 tc 规则影响 → Pod 保持 Ready → Endpoints 不会移除
- `httpGet/tcpSocket` 类型 Probe（端口在 Service 端口范围内）：**可能受** pod-network drop 影响 → 延迟后 Pod 变为 NotReady → Endpoints 会被移除
- 选择原则：如果目标是"Endpoints 移除"，exec 类型 Probe 的 Pod 应使用 pod-process kill 而非 pod-network drop

**注入验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认部分后端被移除或全不可用
2. 向 Service 发送请求，确认出现 5xx 错误或连接超时
3. 查看 Ingress 状态，确认后端健康检查失败
4. 确认请求流量被调度到剩余可用后端（部分后端不可用时）

**注入恢复**：
1. 销毁 chaosblade 实验

**恢复验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认所有后端恢复可用
2. 向 Service 发送请求，确认恢复正常
3. 查看 Ingress 状态，确认后端健康检查通过

**基准事实**：
- **根因**：Service 后端 Pod 异常或网络不通，导致负载均衡无法将请求转发到健康的后端，服务可用性下降
- **必现现象**：Endpoints 列表部分为空或全部为空；请求出现 5xx 或超时；Ingress 后端健康检查失败

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效后端不可达。

前提条件：方式A 只需容器内有 `kill`（BusyBox 也提供，基本总是可用）；方式C 只用控制面 kubectl，
不依赖容器内任何东西 —— **这两条是最可靠的**。方式B 额外要求容器内真有 `iptables` 且有
NET_ADMIN，精简镜像通常两者都不满足，选它之前先验证。

注入命令：
```bash
# 方式A：杀死后端 Pod 主进程（推荐，导致 Pod 重启/CrashLoopBackOff）
kubectl exec <pod-name> -n <namespace> -- sh -c 'kill 1'
# 方式B：注入网络丢包（需容器内真有 iptables 且有 NET_ADMIN，先验证：
#         kubectl exec <pod-name> -n <namespace> -- sh -c 'command -v iptables || echo NO_IPTABLES'）
kubectl exec <pod-name> -n <namespace> -- iptables -A OUTPUT -j DROP
# 方式B'：容器内无 iptables 时，用临时容器 + tc（临时容器与目标容器共享网络命名空间，
#         工具来自调试镜像，--profile=netadmin 提供 NET_ADMIN）
# ── 路径 B'：容器内无可用工具（精简镜像的常态）。
#    先建【长驻】临时容器作为载体 —— 必须 sleep 保活；若把 tc 直接交给 kubectl debug，
#    命令跑完容器即终止，后续 `kubectl exec -c <debugger>` 会报 container not found，
#    故障将无法恢复（已实测）。
# 0) 前置安全检查：确认目标 Pod 不是 hostNetwork。hostNetwork=true 的 Pod
#    其网络命名空间【就是宿主机】，临时容器里的 tc 会打穿整个节点，
#    爆炸半径从单 Pod 扩大到整台机器。为 true 时禁止此路径，改用 node 级用例。
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.hostNetwork}'
# 期望输出为空或 false；输出 true 则停止。

kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
  --target=<container-name> --profile=netadmin --quiet -- sleep <duration>

# 取载体名，等它进入 running
kubectl get pod <pod-name> -n <namespace> \
  -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}{"="}{.state}{"\n"}{end}'

# 经载体注入：载体与目标容器共享网络命名空间，操作 eth0 即操作目标 Pod 的网卡
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc add dev eth0 root netem loss 100%
# 方式C：直接删除后端 Pod
kubectl delete pod <pod-name> -n <namespace>
```

恢复命令：
```bash
# 方式A/C：Pod 由 Deployment 控制器自动重建，无需手动操作
# 方式B：删除 iptables 规则（与注入逐字对应，不要用 iptables -F）
kubectl exec <pod-name> -n <namespace> -- iptables -D OUTPUT -j DROP
# 方式B'：复用注入时那个临时容器，不要新建
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc del dev eth0 root
```

注意事项：
- 方式A（kill 1）效果最直接，但应用会立即重启，故障窗口可能较短
- 方式B（iptables）持续效果更好，但需容器有 NET_ADMIN 权限
- 与 ChaosBlade 不同，方式A/C 无法精确控制故障持续时间
- 方式B 依赖容器内有 `iptables`，精简镜像（BusyBox/distroless）通常没有；此时用方式B'，
  但要先确认集群能拉取含 iproute2 的镜像。**临时容器无法从运行中的 Pod 移除**，
  只能随 Pod 重建消失；`tc qdisc del` 成功即代表故障已恢复
