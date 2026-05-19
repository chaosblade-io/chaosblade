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
