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

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。

> ⚠️ 前提条件：目标容器需具有 `NET_ADMIN` capability（SecurityContext.capabilities.add: ["NET_ADMIN"]），否则 iptables 命令将因权限不足失败。

注入命令：
```bash
# 对指定容器丢弃所有出站网络流量
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -A OUTPUT -j DROP
# 如需仅丢弃特定端口流量：
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -A OUTPUT -p tcp --sport <port> -j DROP
```

恢复命令：
```bash
# 精确删除注入的规则（与注入命令对应）：
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -D OUTPUT -j DROP
# 如果注入的是端口级丢包：
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -D OUTPUT -p tcp --sport <port> -j DROP
```

注意事项：
- Container 共享 Pod 网络 namespace，iptables 规则影响整个 Pod 的网络栈，需通过 `--sport`/`--dport` 精确限定影响范围
- 如容器无 `NET_ADMIN` capability，此降级方案不可用，需考虑重建 Pod 并添加权限
- 与 ChaosBlade 相比，kubectl exec + iptables 无法自动超时恢复，需手动执行 `iptables -D` 精确删除注入的规则
