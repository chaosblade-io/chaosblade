**用例名称** 异常进程占用 导致 Node_内存使用率过高

**故障现象**：
1. 节点内存使用率持续超过 90%
2. 节点上 Pod 出现 OOMKilled 或被驱逐
3. 节点 Status 出现 MemoryPressure 条件为 True

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认监控系统（如 Prometheus）已配置，可观测节点内存指标

**演练步骤**：
1. 定位运行应用 A 的节点
2. 使用 chaosblade 对该节点注入内存压力，模拟异常进程占用节点内存
3. 观察节点内存使用率及 Pod 状态变化

**注入命令**：
```bash
blade create k8s node-mem load --mode ram --mem-percent 90 --names <节点名> --kubeconfig <path> --timeout 600
```
> **必须使用 `--mode ram`**。默认的 cache 模式在 cgroup v2 节点上不会增加实际物理内存占用（仅填充页缓存），kubectl top 观测不到变化。`--mode ram` 通过分配匿名内存直接占用物理 RAM。

**注入验证**：
1. 查看节点内存使用率监控，确认持续超过 90%
2. 执行 `kubectl describe node <节点名>`，确认 MemoryPressure 条件为 True
3. 确认应用 A 的 Pod 出现 OOMKilled 或被驱逐

**注入恢复**：
1. 销毁 chaosblade 实验

**恢复验证**：
1. 查看节点内存使用率监控，确认恢复到正常水平
2. 执行 `kubectl describe node <节点名>`，确认 MemoryPressure 条件为 False
3. 确认应用 A 的 Pod 恢复正常运行

**基准事实**：
- **根因**：节点上存在异常进程大量占用内存，导致节点内存使用率过高，触发 MemoryPressure，Pod 被 OOMKilled 或驱逐
- **必现现象**：节点内存使用率持续超过 90%；MemoryPressure 条件为 True；Pod 出现 OOMKilled 或被驱逐
