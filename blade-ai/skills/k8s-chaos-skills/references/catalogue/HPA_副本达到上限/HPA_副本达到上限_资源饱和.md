**用例名称** 资源饱和 导致 HPA_副本达到上限

**故障现象**：
1. HPA 的当前副本数达到 maxReplicas 上限，无法继续扩容
2. 应用 CPU 或内存使用率仍持续高于 HPA 目标阈值
3. 应用响应延迟增大，出现超时

**资源准备**：
1. 确认应用 A 已正常运行，且已配置 HPA（设置合适的 maxReplicas 以便快速触发上限）
2. 确认监控系统可观测 HPA 状态和 Pod CPU/内存指标

**演练步骤**：
1. 定位应用 A 的 HPA，记录 maxReplicas 配置
2. 使用 chaosblade 对应用 A 的所有 Pod 注入 CPU 压力，持续超过 HPA 扩容阈值
3. 观察 HPA 扩容行为，等待副本数达到 maxReplicas 上限

**注入验证**：
1. 执行 `kubectl get hpa`，确认 REPLICAS 已达到 MAXPODS 上限
2. 查看 HPA Event，确认出现 `FailedGetScale` 或 `DesiredReplicas` 超过 maxReplicas 的告警
3. 查看 Pod CPU 使用率，确认仍持续高于目标阈值
4. 确认应用 A 的请求延迟增大，出现超时

**注入恢复**：
1. 销毁 chaosblade CPU 压力实验
2. 等待 HPA 自动缩容

**恢复验证**：
1. 执行 `kubectl get hpa`，确认 REPLICAS 回落至正常水平
2. 查看 Pod CPU 使用率，确认恢复到 HPA 目标阈值以下
3. 确认应用 A 的请求延迟恢复正常

**基准事实**：
- **根因**：应用负载超过 HPA 的 maxReplicas 能覆盖的处理能力，HPA 达到扩容上限后无法继续扩容，导致服务资源饱和
- **必现现象**：HPA REPLICAS 达到 MAXPODS；HPA Event 有超限告警；CPU 使用率持续超过目标阈值；应用性能下降

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效 CPU 压力触发 HPA 扩容。

前提条件：容器内需包含 `stress-ng` 或支持 `dd`/shell 循环

注入命令：
```bash
# 0) 先列出目标 Pod —— 下面每条注入命令对【每一个】Pod 各执行一次。
#    不要写 shell for 循环：执行层按 argv 下发，没有 shell 来展开它。
kubectl get pods -n <namespace> -l <label-selector> -o name

# 方式一：容器内有 stress-ng（后台+重定向让 exec 立即返回，--timeout 自带自动恢复）
kubectl exec <pod-name> -n <namespace> -- sh -c 'stress-ng --cpu 0 --cpu-load 80 --timeout 600s >/dev/null 2>&1 &'

# 方式二：容器无 stress-ng，用 shell 循环。
# 关键点：① 每个循环重定向到 /dev/null（否则 exec 会挂到 10s 超时）；
# ② PID 落盘 + 按文件定时 kill 实现可靠自动恢复；③ 单核循环，多核需起多个（N）。
# 同样对每个 Pod 各执行一次。sh -c 的载荷整体是一个参数，内部的 for/while/&
# 由容器内的 sh 解释，不需要外层 shell。
kubectl exec <pod-name> -n <namespace> -- sh -c ': > /tmp/chaos_cpu.pids; for i in $(seq 1 <N>); do ( while :; do :; done ) >/dev/null 2>&1 & echo $! >> /tmp/chaos_cpu.pids; done; ( sleep <duration>; kill $(cat /tmp/chaos_cpu.pids) 2>/dev/null; rm -f /tmp/chaos_cpu.pids ) >/dev/null 2>&1 &'
```

恢复命令（从精确到兜底）：
```bash
# 对每个 Pod 各执行一次。
# 首选：kill stress-ng + 按 PID 文件 kill shell 循环（只杀 stress-ng 会漏掉循环）
kubectl exec <pod-name> -n <namespace> -- sh -c 'pkill stress-ng 2>/dev/null; kill $(cat /tmp/chaos_cpu.pids) 2>/dev/null; rm -f /tmp/chaos_cpu.pids'

# 兜底：按命令特征 kill（ps+kill 比 pkill 通用，精简镜像常无 pkill）
kubectl exec <pod-name> -n <namespace> -- sh -c "ps -o pid,args 2>/dev/null | grep -E '[w]hile :|[s]tress-ng' | awk '{print \$1}' | xargs -r kill -9"

# 全部 Pod 处理完后，等待 HPA 自动缩容（cooldown period 后）
```

注意事项：
- 需对所有目标 Pod 逐个注入 CPU 压力，否则 HPA 可能不会触发扩容
- `stress-ng --timeout` 提供自动超时，配合后台+重定向让 exec 立即返回，建议始终指定
- shell 循环命令必须重定向 `>/dev/null 2>&1`，否则会占住 exec 输出管道导致 `kubectl exec` 挂起到 10s 超时
- shell 循环单核，需按 CPU 上限起 N 个循环逼近目标；自动恢复基于 PID 文件 + 定时 kill，切勿用 `$(jobs -p)`（脱离子 shell 取不到 PID 会失效）
