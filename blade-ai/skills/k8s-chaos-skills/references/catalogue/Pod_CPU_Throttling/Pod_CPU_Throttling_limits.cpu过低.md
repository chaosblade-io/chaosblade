**用例名称** limits.cpu过低 导致 Pod_CPU_Throttling

**故障现象**：
1. Pod 内进程频繁被内核 CPU 节流（throttle），`cpu.stat` 中 `nr_throttled` 持续增长
2. 应用请求延迟显著增大，P99 延迟飙升
3. `kubectl top pod` 显示 CPU 使用率接近 limits 但实际未满载

**资源准备**：
1. 确认应用 A 已正常运行，且 Pod 配置了 resources.limits.cpu
2. 确认监控系统可观测 Pod CPU 使用率及 throttle 指标

**演练步骤**：
1. 定位应用 A 的 Deployment
2. 使用 kubectl patch 将应用 A 的 CPU limits 调低为极小值（如 50m），模拟 limits.cpu 配置过低的场景
3. 使用 chaosblade 对应用 A 的 Pod 注入 CPU 负载，确保实际 CPU 需求超过 limits，触发内核 throttle
4. 观察 Pod CPU throttle 指标变化及应用响应延迟

**注入验证**：
1. 进入容器查看 `/sys/fs/cgroup/cpu/cpu.stat`，确认 `nr_throttled` 和 `throttled_time` 持续增长
2. 查看监控指标，确认 Pod CPU 使用率接近 limits
3. 确认应用 A 的请求延迟显著增大

**注入恢复**：
1. 销毁 chaosblade CPU 负载实验
2. 使用 kubectl patch 将应用 A 的 CPU limits 恢复为原始合理值

**恢复验证**：
1. 查看 `cpu.stat`，确认 `nr_throttled` 停止增长
2. 确认应用 A 的请求延迟恢复正常

**基准事实**：
- **根因**：容器 limits.cpu 设置过低，实际 CPU 需求超过 limit，内核对容器 CPU 时间片进行 throttle，导致应用性能下降
- **必现现象**：cpu.stat 中 nr_throttled 持续增长；应用延迟飙升；CPU 使用率接近 limits 上限
