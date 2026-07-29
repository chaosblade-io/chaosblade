**用例名称** Sidecar容器CPU资源争抢 导致 Container_CPU满载

**故障现象**：
1. Pod 内特定 Sidecar 容器（如 istio-proxy、filebeat）CPU 使用率飙升至 100%
2. 主容器可能因 CPU cgroup 共享而出现响应变慢（取决于 limits/requests 配置）
3. Sidecar 提供的辅助功能（流量代理、日志采集等）性能显著下降或超时
4. Pod 整体 CPU 使用率上升，可能触发 HPA 扩容

**资源准备**：
1. 确认目标 Pod 包含多个容器（至少有一个 Sidecar 容器）
2. 确认目标 Pod 所在 namespace、labels 和 Sidecar 容器名称
3. 记录注入前各容器的 CPU 使用基线

**演练步骤**：
1. 确认 Pod 内容器列表，获取 Sidecar 容器名称：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].name}' --kubeconfig <kubeconfig-path>
   ```
2. 记录注入前各容器 CPU 使用情况：
   ```bash
   kubectl top pod <pod-name> -n <namespace> --containers --kubeconfig <kubeconfig-path>
   ```
3. 使用 ChaosBlade 对 Sidecar 容器注入 CPU 满载故障：
   ```bash
   blade create k8s container-cpu fullload \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --container-names <sidecar-container-name> \
     --cpu-percent 100 \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
4. 观察 Sidecar 容器 CPU 飙升后对主容器和整体服务的影响

**注入验证**：
1. 执行 `kubectl top pod <pod-name> -n <namespace> --containers`，确认目标 Sidecar 容器 CPU 接近 100%
2. 确认主容器 CPU 和响应时间是否受到影响（取决于是否配置了 CPU limits）
3. 验证 Sidecar 提供的服务是否降级（如代理转发延迟增加、日志采集中断）
4. 执行 `kubectl describe pod <pod-name> -n <namespace>`，检查是否有 CPU throttling 相关 Events

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动停止 CPU 负载注入

**恢复验证**：
1. 执行 `kubectl top pod <pod-name> -n <namespace> --containers`，确认 Sidecar 容器 CPU 回落至正常水平
2. 确认 Sidecar 提供的辅助服务恢复正常（代理可用、日志恢复采集）
3. 确认主容器服务响应时间恢复基线

**基准事实**：
- **根因**：Sidecar 容器内 CPU 被 ChaosBlade 注入满载，消耗该容器 CPU cgroup 配额
- **必现现象**：目标 Sidecar 容器 CPU 使用率 100%；Sidecar 提供的功能（代理/采集/监控）延迟增加或超时；Pod 整体 CPU 使用率上升；主容器在无独立 CPU limits 时可能受到资源争抢影响

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。

前提条件：目标容器内需有 `stress-ng` 工具，或至少有 `sh` shell 可用

注入命令：
```bash
# 方式一：容器内有 stress-ng（后台+重定向让 exec 立即返回，--timeout 自带自动恢复）
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c 'stress-ng --cpu 0 --cpu-load <percent> --timeout <duration>s >/dev/null 2>&1 &'
# 方式二：容器无 stress-ng，用 shell 循环（重定向避免 exec 挂起；PID 落盘定时自动 kill）：
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c '
  : > /tmp/chaos_cpu.pids
  for i in $(seq 1 <N>); do
    ( while :; do :; done ) >/dev/null 2>&1 &
    echo $! >> /tmp/chaos_cpu.pids
  done
  ( sleep <duration>; kill $(cat /tmp/chaos_cpu.pids) 2>/dev/null; rm -f /tmp/chaos_cpu.pids ) >/dev/null 2>&1 &
'
```

恢复命令（从精确到兜底）：
```bash
# stress-ng：kill 进程
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- pkill -f stress-ng
# shell 循环 首选：按注入落盘的 PID 精确 kill
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c 'kill $(cat /tmp/chaos_cpu.pids) 2>/dev/null; rm -f /tmp/chaos_cpu.pids'
# 兜底：ps+kill（比 pkill 通用，精简镜像常无 pkill）
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c "ps -o pid,args 2>/dev/null | grep '[w]hile :' | awk '{print \$1}' | xargs -r kill -9"
```

注意事项：
- shell 循环单个只能打满单核，需按容器 CPU 上限起 N 个循环逼近目标百分比；精细百分比应优先 stress-ng
- shell 循环命令必须重定向 `>/dev/null 2>&1`，否则占住 exec 输出管道导致 `kubectl exec` 挂起到 10s 超时
- 自动恢复基于 PID 文件（`/tmp/chaos_cpu.pids`）+ 定时 kill，可靠；切勿用 `$(jobs -p)` 定时自杀（脱离子 shell 取不到 PID）
- stress-ng 方式支持 `--cpu-load` 精确控制负载百分比，但需容器镜像包含该工具
