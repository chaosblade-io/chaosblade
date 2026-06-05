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
