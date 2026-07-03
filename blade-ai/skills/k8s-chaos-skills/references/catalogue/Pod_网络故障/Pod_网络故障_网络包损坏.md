**⚠️ 注意：此场景为 kubectl-native 方案。blade v1.9.0 的 pod-network 仅支持 dns/drop/occupy，不支持 corrupt action，需通过 kubectl exec + tc netem 实现。**

**用例名称** 网络包损坏 导致 Pod_网络故障

**故障现象**：
1. 应用请求成功率下降但不完全中断，呈现间歇性失败
2. TCP 重传率显著上升，网络吞吐量下降
3. 部分 HTTP 请求返回错误或超时，服务质量劣化
4. 监控系统显示网络错误计数持续增长

**资源准备**：
1. 确认目标应用已正常运行，且有活跃的网络通信流量
2. 确认目标 Pod 的标签选择器和命名空间
3. 确认监控系统可观测网络重传率和请求成功率指标
4. 确认容器内有 `tc` 工具（iproute2 包），或通过 `kubectl debug --image=nicolaka/netshoot` 附加含网络工具的调试容器

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 确认容器内 tc 工具可用：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc -Version
   ```
   若不可用，使用 debug container：
   ```bash
   kubectl debug -it <pod-name> -n <namespace> --image=nicolaka/netshoot --target=<container-name> -- tc -Version
   ```
3. 通过 tc netem 对目标 Pod 注入网络包损坏故障：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc add dev eth0 root netem corrupt 30%
   ```
   - `corrupt 30%`：约 30% 的出站数据包 checksum 被随机修改，接收方校验失败后丢弃
   - `eth0`：网络接口名称，根据实际情况调整（可通过 `ip link show` 确认）
   - 原理：tc netem 的 corrupt 选项对数据包进行单比特翻转，导致校验和失败
4. 确认 tc 规则已生效：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   ```
   应显示 `qdisc netem ... corrupt 30%`

**注入验证**：
1. 在目标 Pod 内查看网络重传统计：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/snmp | grep -i retrans
   ```
   确认 RetransSegs 计数持续增长
2. 在目标 Pod 内验证请求间歇性失败：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c 'for i in $(seq 1 10); do wget -qO- --timeout=3 <目标服务地址> && echo OK || echo FAIL; done'
   ```
   确认部分请求成功、部分失败
3. 查看应用日志确认出现间歇性连接错误：
   ```bash
   kubectl logs <pod-name> -n <namespace> --tail=30
   ```
4. 确认网络监控指标中重传率和错误包计数上升

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
1. 在目标 Pod 内验证请求恢复正常：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c 'for i in $(seq 1 5); do wget -qO- --timeout=3 <目标服务地址> && echo OK || echo FAIL; done'
   ```
   确认所有请求均成功
2. 确认 RetransSegs 不再异常增长
3. 确认应用日志不再出现连接错误，服务质量恢复正常

**基准事实**：
- **根因**：Pod 网络接口上约 30% 的出站数据包 checksum 被 tc netem corrupt 修改，接收方校验失败后丢弃，触发 TCP 重传机制
- **必现现象**：RetransSegs 计数持续增长；请求成功率下降至约 70%（非完全中断）；应用日志出现间歇性 connection reset 或 timeout；网络吞吐量下降
- **方案说明**：此为 kubectl-native 方案（blade v1.9.0 pod-network 不支持 corrupt action）。恢复不使用 blade destroy，而是通过 `tc qdisc del dev eth0 root` 移除规则
**用例名称** 网络包损坏 导致 Pod_网络故障

**故障现象**：
1. 应用请求成功率下降但不完全中断，呈现间歇性失败
2. TCP 重传率显著上升，网络吞吐量下降
3. 部分 HTTP 请求返回错误或超时，服务质量劣化
4. 监控系统显示网络错误计数持续增长

**资源准备**：
1. 确认目标应用已正常运行，且有活跃的网络通信流量
2. 确认目标 Pod 的标签选择器和命名空间
3. 确认监控系统可观测网络重传率和请求成功率指标

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 使用 ChaosBlade 对目标 Pod 注入网络包损坏故障：
   ```bash
   blade create k8s pod-network corrupt \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --percent 30 \
     --interface eth0 \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
   - `--percent`：包损坏比例（0-100），30 表示约 30% 的包被损坏
   - `--interface`：网络接口，默认 eth0
   - 原理：对数据包 checksum 进行损坏，接收方校验失败后丢弃该包
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在目标 Pod 内查看网络重传统计：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/snmp | grep -i retrans
   ```
   确认 RetransSegs 计数持续增长
2. 在目标 Pod 内验证请求间歇性失败：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c 'for i in $(seq 1 10); do wget -qO- --timeout=3 <目标服务地址> && echo OK || echo FAIL; done'
   ```
   确认部分请求成功、部分失败
3. 查看应用日志确认出现间歇性连接错误：
   ```bash
   kubectl logs <pod-name> -n <namespace> --tail=30
   ```
4. 确认网络监控指标中重传率和错误包计数上升

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
2. 或等待 `--timeout` 600 秒到期后自动恢复

**恢复验证**：
1. 在目标 Pod 内验证请求恢复正常：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c 'for i in $(seq 1 5); do wget -qO- --timeout=3 <目标服务地址> && echo OK || echo FAIL; done'
   ```
   确认所有请求均成功
2. 确认 RetransSegs 不再异常增长
3. 确认应用日志不再出现连接错误，服务质量恢复正常

**基准事实**：
- **根因**：Pod 网络接口上约 30% 的出站数据包 checksum 被损坏，接收方校验失败后丢弃，触发 TCP 重传机制
- **必现现象**：RetransSegs 计数持续增长；请求成功率下降至约 70%（非完全中断）；应用日志出现间歇性 connection reset 或 timeout；网络吞吐量下降
