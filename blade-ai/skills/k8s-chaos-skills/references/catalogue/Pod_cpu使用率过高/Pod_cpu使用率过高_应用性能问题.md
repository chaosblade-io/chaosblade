**用例名称** 应用性能问题 导致 Pod_CPU使用率过高

**故障现象**：
1. Pod 的 CPU 使用率持续超过阈值
2. CPU 使用率过高影响其他服务
3. 应用响应变慢或发生超时

**资源准备**：
1. 确认应用 A/B 已正常运行
2. 确认监控系统（如 Prometheus）已配置，可观测 Pod CPU 指标

**演练步骤**：
1. 定位应用 A 的 Pod 作为故障注入目标
2. 使用 chaosblade 对目标 Pod 注入 CPU 压力（模拟死循环或高并发计算场景）
3. 观察 Pod CPU 使用率变化

**注入验证**：
1. 查看 Pod CPU 使用率监控，确认持续高于阈值
2. 进入容器查看 CPU 占用进程
3. 通过 APM 工具分析 CPU 占用情况，定位问题代码
4. 确认应用 A 对其他服务的调用出现延迟或超时

**注入恢复**：
1. 销毁 chaosblade CPU 压力实验
2. 如 Pod 仍异常，可删除 Pod 触发重建

**恢复验证**：
1. 查看 Pod CPU 使用率监控，确认恢复到正常水平
2. 确认应用 A 对其他服务的调用恢复正常

**基准事实**：
- **根因**：应用存在死循环、高并发计算或资源泄漏等性能问题，导致 CPU 使用率持续过高
- **必现现象**：Pod CPU 使用率持续超过阈值，应用响应变慢或超时

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效 CPU 压力注入。

前提条件：容器内有 `stress-ng` 时优先使用；否则用 shell 循环替代。多容器 Pod 请用 `-c <container>` 显式指定目标容器。

注入命令：
```bash
# 方式一：容器内有 stress-ng（后台+重定向让 exec 立即返回，--timeout 自带自动恢复）
kubectl exec <pod-name> -n <namespace> -c <container> -- \
  sh -c 'stress-ng --cpu 0 --cpu-load <percent> --timeout <duration>s >/dev/null 2>&1 &'

# 方式二：容器无 stress-ng，用 shell 循环。
# 关键点：① 先读 CPU 上限算循环数；② 每个循环重定向到 /dev/null（否则
# exec 会一直挂到 10s 超时）；③ PID 落盘 + 按文件定时 kill 实现可靠自动恢复。

# ① 读取 CPU 上限，计算循环数 N = ceil(limit核数 × percent/100)
#    例：limit=2、目标 90% → N=2；单核循环≈100%，无法做到不足单核的精细百分比
kubectl get pod <pod-name> -n <namespace> \
  -o jsonpath='{.spec.containers[0].resources.limits.cpu}'

# ② 注入：起 N 个循环，PID 落盘，<duration> 秒后按 PID 文件自动 kill
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c '
  : > /tmp/chaos_cpu.pids
  for i in $(seq 1 <N>); do
    ( while :; do :; done ) >/dev/null 2>&1 &
    echo $! >> /tmp/chaos_cpu.pids
  done
  ( sleep <duration>; kill $(cat /tmp/chaos_cpu.pids) 2>/dev/null; rm -f /tmp/chaos_cpu.pids ) >/dev/null 2>&1 &
  echo "started <N> loops, auto-stop after <duration>s"
'
```

恢复命令（从精确到兜底，任选其一）：
```bash
# stress-ng 方式的恢复
kubectl exec <pod-name> -n <namespace> -c <container> -- pkill -f stress-ng

# shell 循环方式的恢复
# 首选：按注入时落盘的 PID 精确 kill
kubectl exec <pod-name> -n <namespace> -c <container> -- \
  sh -c 'kill $(cat /tmp/chaos_cpu.pids) 2>/dev/null; rm -f /tmp/chaos_cpu.pids'
# 兜底1：按命令特征 kill（用 ps+kill，比 pkill 通用，精简镜像常无 pkill）
kubectl exec <pod-name> -n <namespace> -c <container> -- \
  sh -c "ps -o pid,args 2>/dev/null | grep '[w]hile :' | awk '{print \$1}' | xargs -r kill -9"
# 兜底2：删 Pod 触发重建（最稳，注意单副本会瞬时中断）
kubectl delete pod <pod-name> -n <namespace>
```

注意事项：
- shell 循环单个只能打满单核，需按 CPU 上限计算循环数 N 才能逼近目标百分比；不足单核的精细百分比无法通过循环实现（此时应优先 stress-ng）
- shell 循环命令必须重定向 `>/dev/null 2>&1`，否则会占住 exec 输出管道，导致 `kubectl exec` 挂起到 10s 超时（进程其实已在后台启动）
- 自动恢复基于 PID 文件（`/tmp/chaos_cpu.pids`）+ 定时 kill，可靠；切勿用 `$(jobs -p)` 定时自杀——后台循环是父 shell 的 job，exec 会话退出后脱离子 shell 取不到这些 PID，会导致定时器失效
- stress-ng 方式需容器镜像中预装该工具
- 精度不如 ChaosBlade 的 cgroup 级 CPU 控制
