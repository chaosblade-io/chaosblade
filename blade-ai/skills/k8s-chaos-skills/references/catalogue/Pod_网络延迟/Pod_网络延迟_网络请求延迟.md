**用例名称** 网络请求延迟 导致 Pod_网络延迟

**故障现象**：
1. Pod 所有出方向网络请求增加固定延迟（如 1000ms）
2. 应用调用上下游服务超时或响应显著变慢
3. 健康检查可能因超时失败，导致 Pod 被重启
4. 模拟跨地域部署/弱网环境/网络拥塞场景

**资源准备**：
1. 确认目标 Pod 正常运行，且容器内已安装 `tc` 工具（iproute2 包）
2. 确认目标 Pod 有对外网络调用（上下游服务、数据库等）
3. 确认目标 Pod 名称和命名空间

**演练步骤**：
1. 确认目标 Pod 运行状态和容器内 tc 工具可用性：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -- which tc
   ```
2. 使用 kubectl exec 在目标 Pod 内注入网络延迟（本用例为 kubectl-native 方案，因 ChaosBlade v1.8.0 不支持 pod-network delay）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- \
     tc qdisc add dev eth0 root netem delay 1000ms
   ```
   参数说明：
   - `delay 1000ms`：固定延迟 1000 毫秒
   - 可附加抖动：`delay 1000ms 200ms`（1000±200ms）
   - `dev eth0`：通常为 Pod 主网卡，部分环境为 `eth0` 以外名称
3. 观察应用响应时间变化

**注入验证**：
1. 确认 tc 规则已生效：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   ```
   输出应包含 `netem delay 1000ms`
2. 在 Pod 内验证延迟效果：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ping -c 3 <目标服务IP>
   ```
   确认 RTT 增加约 1000ms
3. 验证 HTTP 请求延迟：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- \
     wget -qO- --timeout=10 -S <依赖服务地址> 2>&1 | head -5
   ```
4. 检查应用日志是否出现 timeout 或 slow response 相关错误

**注入恢复**：
1. 删除 tc netem 规则（注意：不使用 blade destroy，这是 kubectl-native 方案）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc del dev eth0 root
   ```
2. 如 Pod 因健康检查超时被重启，tc 规则自动消失（不持久化），无需额外操作

**恢复验证**：
1. 确认 tc 规则已清除：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   ```
   输出应不再包含 netem 规则
2. 在 Pod 内验证延迟恢复正常：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ping -c 3 <目标服务IP>
   ```
   确认 RTT 恢复到正常水平
3. 确认应用日志不再出现超时错误

**基准事实**：
- **根因**：通过 tc netem 在 Pod 网卡注入出方向固定延迟，模拟弱网/跨地域网络环境
- **必现现象**：Pod 内 ping RTT 增加约 1000ms；`tc qdisc show` 显示 netem delay 规则；应用对外请求响应时间显著增加
