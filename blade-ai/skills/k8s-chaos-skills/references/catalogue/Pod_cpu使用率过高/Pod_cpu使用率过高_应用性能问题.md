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

前提条件：容器内需有 `stress-ng` 工具，或可使用 shell 循环作为替代

注入命令：
```bash
# 使用 stress-ng 注入 CPU 压力
kubectl exec <pod-name> -n <namespace> -- stress-ng --cpu 0 --cpu-load <percent> --timeout <duration>s
# 如果容器无 stress-ng，使用 shell 循环：
kubectl exec <pod-name> -n <namespace> -- sh -c 'while true; do :; done &'
```

恢复命令：
```bash
kubectl exec <pod-name> -n <namespace> -- pkill -f stress-ng
# 或 kill shell 循环：
kubectl exec <pod-name> -n <namespace> -- pkill -f 'while true'
```

注意事项：
- shell 循环方式只能打满单核，无法精确控制 CPU 使用百分比
- stress-ng 方式需容器镜像中预装该工具
- 无自动超时恢复机制，需手动 kill 进程
- 精度不如 ChaosBlade 的 cgroup 级 CPU 控制
