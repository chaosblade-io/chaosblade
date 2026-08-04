**用例名称** 内存压力过大 导致 Pod_OOM内存异常

**故障现象**：
1. Pod 内存使用率接近 Limit 上限
2. 应用响应变慢，出现延迟
3. 存在被 OOMKill 的风险

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认应用 A 的 Pod 已配置 resources.limits.memory

**演练步骤**：
1. 定位应用 A 的 Pod
2. 使用 chaosblade 对应用 A 的 Pod 注入内存压力，模拟内存占用增长接近 Limit 的场景
3. 观察 Pod 内存使用率变化

**注入命令**：
```bash
blade create k8s pod-mem load --mode ram --mem-percent 80 --names <Pod名> --namespace <命名空间> --kubeconfig <path> --timeout 300
```
> **必须使用 `--mode ram`**。默认的 cache 模式在 cgroup v2 环境下不会增加 Pod 的 RSS 内存占用，kubectl top 观测不到变化。`--mode ram` 直接分配匿名内存，确保 Pod 内存使用率真实上升。

**注入验证**：
1. 查看监控指标，确认 Pod 内存使用率接近 Limit 上限
2. 查看 Pod Event，确认存在内存相关告警
3. 确认应用 A 的响应延迟增大

**注入恢复**：
1. 销毁 chaosblade 内存压力注入实验

**恢复验证**：
1. 查看监控指标，确认 Pod 内存使用率恢复正常
2. 确认应用性能恢复正常

**基准事实**：
- **根因**：应用内存使用增长或注入内存压力，导致 Pod 内存使用率接近 Limit，存在被 OOMKill 的风险
- **必现现象**：Pod 内存使用率接近 Limit；应用响应变慢；存在 OOMKill 风险

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效内存压力注入。

前提条件：容器内需有 `stress-ng` 工具，或可使用 `dd` 作为替代

注入命令：
```bash
# 方案1：指定绝对大小（推荐，精确控制；后台+重定向让 exec 立即返回，--timeout 自带自动恢复）
kubectl exec <pod-name> -n <namespace> -- \
  sh -c 'stress-ng --vm 1 --vm-bytes <size，如 512M 或 2G> --timeout <duration>s >/dev/null 2>&1 &'
# 方案2：按系统总内存百分比（注意：是节点总内存，非 Pod limit）
kubectl exec <pod-name> -n <namespace> -- \
  sh -c 'stress-ng --vm 1 --vm-bytes 80% --timeout <duration>s >/dev/null 2>&1 &'
# 如果无 stress-ng，用 dd 分配内存（PID 落盘 + 定时释放）：
kubectl exec <pod-name> -n <namespace> -- sh -c '
  ( dd if=/dev/zero bs=1M count=<MB> 2>/dev/null | tail ) >/dev/null 2>&1 &
  echo $! > /tmp/memcache-warmup.pid
  ( sleep <duration>; kill $(cat /tmp/memcache-warmup.pid) 2>/dev/null; rm -f /tmp/memcache-warmup.pid ) >/dev/null 2>&1 &
'
```

恢复命令（从精确到兜底）：
```bash
# stress-ng：kill 进程
kubectl exec <pod-name> -n <namespace> -- sh -c 'pkill -f stress-ng 2>/dev/null'
# dd 首选：按落盘 PID kill
kubectl exec <pod-name> -n <namespace> -- \
  sh -c 'kill $(cat /tmp/memcache-warmup.pid) 2>/dev/null; rm -f /tmp/memcache-warmup.pid'
# 兜底：ps+kill（比 pkill 通用）
kubectl exec <pod-name> -n <namespace> -- \
  sh -c "ps -o pid,args 2>/dev/null | grep -E '[s]tress-ng|[d]d if=/dev/zero' | awk '{print \$1}' | xargs -r kill -9"
```

注意事项：
- stress-ng `--vm-bytes` 按系统内存百分比计算，非 Pod cgroup 百分比，需手动转算绝对值
- dd 方式为一次性分配，无法持续加压，且精度不如 ChaosBlade 的 `--mode ram`
- 无 cgroup 感知能力，可能直接触发 OOMKill 而非停留在“接近 Limit”状态
