**用例名称** 人为误操作 导致 workload_副本被缩容

**故障现象**：
1. Deployment/StatefulSet 的当前副本数小于期望副本数
2. 应用可用实例减少，部分请求无法处理
3. 服务响应延迟增大或出现超时

**资源准备**：
1. 确认应用 A 的 Deployment/StatefulSet 已正常运行，副本数大于 1
2. 确认监控系统可观测 Pod 副本数和请求指标

**演练步骤**：
1. 定位应用 A 的 Deployment/StatefulSet
2. 使用 kubectl 将 replicas 修改为较小的值，模拟人为误操作导致的意外缩容
3. 观察 Pod 缩容过程和应用状态变化

**标签选择器提示**：
- Kubernetes 推荐标签格式为 `app.kubernetes.io/name=<name>`，而非简单的 `app=<name>`
- 建议先不带 `-l` 过滤器查询 `kubectl get deployment <name> -n <ns>`，再从返回结果中提取实际标签
- 若需用标签过滤，优先使用 `-l app.kubernetes.io/name=<name>` 或 `-l app.kubernetes.io/component=<name>`

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 总数减少，部分 Pod 被终止
2. 执行 `kubectl get deployment/statefulset <name>`，确认当前副本数小于缩容前的值
3. 确认应用 A 的请求延迟增大或出现超时
4. 确认服务可用性下降

**注入恢复**：
1. 使用 kubectl 将 replicas 恢复为原来的合理值
2. 等待 Pod 自动扩容

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 总数恢复到缩容前的值
2. 执行 `kubectl get deployment/statefulset <name>`，确认 READY 副本数等于 DESIRED
3. 确认应用 A 的请求延迟恢复正常
4. 确认服务可用性恢复

**基准事实**：
- **根因**：人为误操作（如 kubectl scale、修改 YAML 等）导致 Deployment/StatefulSet 的副本数被意外缩小，可用实例不足
- **必现现象**：READY 副本数小于 DESIRED；Pod 被终止；服务可用性下降

**注意事项**：
- 若目标 Deployment/StatefulSet 由 Helm 管理（label `app.kubernetes.io/managed-by: Helm`），kubectl scale 修改会被 Helm reconciliation 覆盖
- 注入期间应避免触发 Helm upgrade/rollback 操作，否则故障会被意外恢复
- 反之，若需快速恢复，可通过 `helm rollback` 或 `helm upgrade` 强制还原
