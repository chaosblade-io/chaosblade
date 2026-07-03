**用例名称** 服务依赖中断 导致 Pod_网络丢包

**故障现象**：
1. Pod 对外部服务或上下游依赖的网络请求超时或无响应
2. 应用健康检查可能失败（如依赖外部探活）
3. 服务间调用链路出现断裂，影响业务可用性

**资源准备**：
1. 确认目标应用已正常运行，且有对外网络调用（数据库、缓存、上下游服务等）
2. 确认监控系统可观测网络请求成功率和延迟指标
3. 确认目标 Pod 的标签选择器和命名空间

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 使用 ChaosBlade 对目标 Pod 注入网络丢包故障：
   ```bash
   blade create k8s pod-network drop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --source-port <port> \
     --kubeconfig <kubeconfig-path>
   ```
   - `--source-port`：限定丢包端口（如 3306 丢弃 MySQL 流量、53 丢弃 DNS 流量）
   - 不指定端口时为全量丢包（慎用，影响所有流量包括监控和健康检查）
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在目标 Pod 内验证网络连通性丧失：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <依赖服务地址>
   ```
   确认请求超时或无响应
2. 查看应用日志确认出现连接超时错误：
   ```bash
   kubectl logs <pod-name> -n <namespace> --tail=20
   ```
3. 检查服务调用链路指标，确认目标端口流量中断

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
2. 如 Pod 因丢包导致健康检查失败被重启，等待新 Pod Ready

**恢复验证**：
1. 在目标 Pod 内重新验证网络连通性恢复：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <依赖服务地址>
   ```
2. 确认应用日志不再出现连接超时错误
3. 确认服务调用链路指标恢复正常

**基准事实**：
- **根因**：Pod 出方向网络流量被 iptables DROP 规则丢弃，导致对指定端口/地址的所有请求无响应
- **必现现象**：目标端口的 TCP/UDP 请求超时；应用日志出现 connection timed out；依赖该连接的业务功能不可用

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效网络丢包注入。

前提条件：容器需有 NET_ADMIN capability 或以 root 运行，容器内需有 `iptables` 工具

注入命令：
```bash
# 全量丢包（所有出站流量）：
kubectl exec <pod-name> -n <namespace> -- iptables -A OUTPUT -j DROP
# 或基于端口的精确丢包：
kubectl exec <pod-name> -n <namespace> -- iptables -A OUTPUT -p tcp --dport <port> -j DROP
```

恢复命令：
```bash
# 精确删除注入的规则（与注入命令对应）：
kubectl exec <pod-name> -n <namespace> -- iptables -D OUTPUT -j DROP
# 如果注入的是端口级丢包：
kubectl exec <pod-name> -n <namespace> -- iptables -D OUTPUT -p tcp --dport <port> -j DROP
```

注意事项：
- 全量丢包会影响所有流量包括监控和健康检查，建议使用端口级丢包
- 需要容器具备 NET_ADMIN capability，否则 iptables 命令会报权限错误
- 无自动超时恢复机制，必须手动执行 `iptables -D` 删除规则
- 恢复时使用 `iptables -D` 精确删除注入的规则，避免 `iptables -F` 清除容器原有的其他规则
