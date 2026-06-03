**用例名称** CoreDNS异常 导致 DNS_解析失败

**故障现象**：
1. Pod 内 DNS 解析失败，应用报 `Name or service not known` 或 `NXDOMAIN` 错误
2. CoreDNS Pod 异常（CrashLoopBackOff/不可用）
3. 集群内服务间调用因 DNS 解析失败而中断

**资源准备**：
1. 确认应用 A 已正常运行，且依赖集群 DNS 进行服务发现
2. 确认 CoreDNS Deployment 正常运行
3. 确认监控系统可观测 DNS 请求指标

**演练步骤**：
1. 记录 CoreDNS Deployment 当前副本数和配置，并获取其 Pod 选择器标签：
   ```bash
   kubectl get deployment coredns -n kube-system -o jsonpath='{.spec.selector.matchLabels}'
   ```
   记录返回的标签（如 `component=coredns` 或 `k8s-app=kube-dns`），后续步骤中用 `<coredns-label>` 表示该标签
2. 将 CoreDNS Deployment 的副本数缩为 0，模拟 CoreDNS 完全不可用：
   ```bash
   kubectl scale deployment coredns -n kube-system --replicas=0
   ```
3. 在应用 A 的 Pod 内尝试进行 DNS 解析
4. 观察应用 A 的服务间调用行为

**注入验证**：
1. 执行 `kubectl get pods -n kube-system -l <coredns-label>`（使用演练步骤 1 中获取的实际标签），确认无 CoreDNS Pod 运行
2. 在应用 A 的 Pod 内执行 DNS 解析测试：
   ```bash
   kubectl exec <pod-name> -- nslookup kubernetes.default.svc.cluster.local
   ```
   确认解析失败
3. 确认应用 A 依赖 DNS 的服务调用出现错误

**注入恢复**：
1. 恢复 CoreDNS 副本数：
   ```bash
   kubectl scale deployment coredns -n kube-system --replicas=<原始副本数>
   ```
2. 等待 CoreDNS Pod 启动并就绪

**恢复验证**：
1. 执行 `kubectl get pods -n kube-system -l <coredns-label>`（使用演练步骤 1 中获取的实际标签），确认 CoreDNS Pod 全部 Running 且 Ready
2. 在应用 A 的 Pod 内重新执行 DNS 解析，确认恢复正常
3. 确认应用 A 的服务间调用恢复

**基准事实**：
- **根因**：CoreDNS Pod 异常或不可用，导致集群内 DNS 解析服务中断，依赖 DNS 的服务发现和调用全部失败
- **必现现象**：Pod 内 DNS 解析超时或返回 NXDOMAIN；CoreDNS Pod 不可用；服务间调用失败
