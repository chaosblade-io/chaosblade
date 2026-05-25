**用例名称** kube-proxy异常 导致 Service_调用失败

**故障现象**：
1. 通过 ClusterIP/NodePort 访问 Service 失败，连接超时
2. 节点上 iptables/ipvs 规则未更新或被清空
3. kube-proxy Pod 异常，无法维护 Service 转发规则

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service
2. 确认 kube-proxy DaemonSet 正常运行
3. 确认监控系统可观测 Service 请求指标

**演练步骤**：
1. 记录 kube-proxy DaemonSet 当前状态
2. 选择目标节点，删除该节点上的 kube-proxy Pod 并临时阻止重建（通过 cordon 节点或修改 DaemonSet nodeSelector）：
   ```bash
   # 方式A：给目标节点添加标签排除 kube-proxy 调度
   kubectl label node <目标节点> chaos-no-proxy=true
   kubectl patch ds kube-proxy -n kube-system \
     -p '{"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"chaos-no-proxy","operator":"DoesNotExist"}]}]}}}}}}}'
   ```
   或者直接使用 chaosblade 杀掉 kube-proxy 进程：
   ```bash
   blade create k8s node-process kill \
     --names <目标节点> \
     --process kube-proxy \
     --timeout 120 \
     --kubeconfig <路径>
   ```
3. 在目标节点上的 Pod 内通过 ClusterIP 访问 Service
4. 观察 Service 访问结果

**注入验证**：
1. 确认目标节点上 kube-proxy 进程不存在或 Pod 处于异常状态
2. 在目标节点的 Pod 内访问 Service ClusterIP，确认连接超时或失败
3. 检查节点 iptables/ipvs 规则，确认 Service 相关转发规则缺失或过期

**注入恢复**：
1. 若使用标签方式：移除节点标签并恢复 DaemonSet 配置：
   ```bash
   kubectl label node <目标节点> chaos-no-proxy-
   kubectl patch ds kube-proxy -n kube-system --type=json \
     -p='[{"op":"remove","path":"/spec/template/spec/affinity"}]'
   ```
2. 若使用 chaosblade：等待实验超时或执行 `blade destroy <UID>`
3. 等待 kube-proxy Pod 在目标节点重建

**恢复验证**：
1. 确认目标节点上 kube-proxy Pod 恢复 Running 且 Ready
2. 在目标节点的 Pod 内重新访问 Service，确认恢复正常
3. 检查 iptables/ipvs 规则已重新同步

**基准事实**：
- **根因**：kube-proxy Pod 异常或进程被杀，无法维护节点上的 iptables/ipvs 转发规则，导致 Service ClusterIP 流量无法被正确转发
- **必现现象**：Service ClusterIP 访问超时；kube-proxy 不可用；节点 iptables/ipvs 规则缺失
