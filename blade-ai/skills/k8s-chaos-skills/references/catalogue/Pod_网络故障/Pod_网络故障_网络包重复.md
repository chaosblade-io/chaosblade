**⚠️ 注意：此场景为 kubectl-native 方案。blade v1.9.0 的 pod-network 仅支持 dns/drop/occupy，不支持 duplicate action，需通过 kubectl exec + tc netem 实现。**

**用例名称** 网络包重复 导致 Pod_网络故障

**故障现象**：
1. 网络带宽消耗异常增加，重复包占用额外带宽
2. 应用层可能收到重复消息，考验业务幂等性设计
3. 网络延迟略有上升，TCP 层需额外 CPU 处理去重
4. 监控系统显示网络接收包数异常偏高

**资源准备**：
1. 确认目标应用已正常运行，且有活跃的网络通信流量
2. 确认目标 Pod 的标签选择器和命名空间
3. 确认监控系统可观测网络流量和包计数指标
4. 确认容器内有 `tc` 工具（iproute2 包），或通过 `kubectl debug --image=nicolaka/netshoot` 附加含网络工具的调试容器

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 记录注入前的网络包统计基线：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```
3. 确认容器内 tc 工具可用：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc -Version
   ```
   若不可用，使用 debug container：
   ```bash
   kubectl debug -it <pod-name> -n <namespace> --image=nicolaka/netshoot --target=<container-name> -- tc -Version
   ```
4. 通过 tc netem 对目标 Pod 注入网络包重复故障：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc add dev eth0 root netem duplicate 30%
   ```
   - `duplicate 30%`：约 30% 的出站数据包会被复制一份重新发送
   - `eth0`：网络接口名称，根据实际情况调整（可通过 `ip link show` 确认）
   - 原理：tc netem 的 duplicate 选项对出站包进行复制，接收端会收到重复数据包
5. 确认 tc 规则已生效：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   ```
   应显示 `qdisc netem ... duplicate 30%`

**注入验证**：
1. 在目标 Pod 内查看网络接口统计，确认发送包数异常增高：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```
   与基线对比，TX packets 增长速率应明显高于正常水平
2. 在目标 Pod 内验证网络连通性（TCP 层自动去重，连接仍可用）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <目标服务地址>
   ```
   确认请求仍可成功，但响应时间可能略有增加
3. 查看应用日志确认是否出现重复消息处理记录：
   ```bash
   kubectl logs <pod-name> -n <namespace> --tail=30
   ```
4. 确认网络监控显示出站流量异常增加约 30%

**注入恢复**：
1. 移除 tc netem 规则：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc del dev eth0 root
   ```
2. 确认规则已移除：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   ```
   应恢复为默认 qdisc（如 `pfifo_fast` 或 `fq_codel`），不再显示 netem

**恢复验证**：
1. 在目标 Pod 内确认网络包统计恢复正常增长速率：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```
2. 确认出站流量恢复至基线水平
3. 确认应用响应时间和吞吐量恢复正常

**基准事实**：
- **根因**：Pod 网络接口上约 30% 的出站数据包被 tc netem duplicate 复制重发，导致接收端收到重复包，占用额外带宽和处理资源
- **必现现象**：出站 TX packets 增长率异常偏高（约 1.3 倍）；网络带宽占用增加；TCP 层自动去重但消耗额外 CPU；应用层若无幂等保护可能处理重复业务消息
- **方案说明**：此为 kubectl-native 方案（blade v1.9.0 pod-network 不支持 duplicate action）。恢复不使用 blade destroy，而是通过 `tc qdisc del dev eth0 root` 移除规则
**用例名称** 网络包重复 导致 Pod_网络故障

**故障现象**：
1. 网络带宽消耗异常增加，重复包占用额外带宽
2. 应用层可能收到重复消息，考验业务幂等性设计
3. 网络延迟略有上升，TCP 层需额外 CPU 处理去重
4. 监控系统显示网络接收包数异常偏高

**资源准备**：
1. 确认目标应用已正常运行，且有活跃的网络通信流量
2. 确认目标 Pod 的标签选择器和命名空间
3. 确认监控系统可观测网络流量和包计数指标

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 记录注入前的网络包统计基线：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```
3. 使用 ChaosBlade 对目标 Pod 注入网络包重复故障：
   ```bash
   blade create k8s pod-network duplicate \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --percent 30 \
     --interface eth0 \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
   - `--percent`：包重复比例（0-100），30 表示约 30% 的包会被复制一份重新发送
   - `--interface`：网络接口，默认 eth0
   - 原理：利用 tc netem 对出站包进行复制，接收端会收到重复数据包
4. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在目标 Pod 内查看网络接口统计，确认发送包数异常增高：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```
   与基线对比，TX packets 增长速率应明显高于正常水平
2. 在目标 Pod 内验证网络连通性（TCP 层自动去重，连接仍可用）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <目标服务地址>
   ```
   确认请求仍可成功，但响应时间可能略有增加
3. 查看应用日志确认是否出现重复消息处理记录：
   ```bash
   kubectl logs <pod-name> -n <namespace> --tail=30
   ```
4. 确认网络监控显示出站流量异常增加约 30%

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
2. 或等待 `--timeout` 600 秒到期后自动恢复

**恢复验证**：
1. 在目标 Pod 内确认网络包统计恢复正常增长速率：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```
2. 确认出站流量恢复至基线水平
3. 确认应用响应时间和吞吐量恢复正常

**基准事实**：
- **根因**：Pod 网络接口上约 30% 的出站数据包被 tc netem 复制重发，导致接收端收到重复包，占用额外带宽和处理资源
- **必现现象**：出站 TX packets 增长率异常偏高（约 1.3 倍）；网络带宽占用增加；TCP 层自动去重但消耗额外 CPU；应用层若无幂等保护可能处理重复业务消息
