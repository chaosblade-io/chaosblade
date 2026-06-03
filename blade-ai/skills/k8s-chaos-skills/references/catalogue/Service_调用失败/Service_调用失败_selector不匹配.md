**用例名称** selector不匹配 导致 Service_调用失败

**故障现象**：
1. Service 的 Endpoints 列表为空
2. 通过 Service 访问应用返回 connection refused 或无响应
3. Pod 正常运行但未被 Service 选中

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service
2. 确认监控系统可观测 Service 请求指标和 Endpoints 状态

**演练步骤**：
1. 记录应用 A 的 Service 当前 selector 配置
2. 使用 kubectl patch 修改 Service 的 selector，使其不匹配任何 Pod：
   ```bash
   kubectl patch svc <service-name> -n <namespace> \
     -p '{"spec":{"selector":{"app":"non-existent-app"}}}'
   ```
3. 观察 Endpoints 变化和服务可用性

**注入验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认 Endpoints 列表为空（无子集）
2. 向 Service 发送请求，确认返回 connection refused 或超时
3. 执行 `kubectl get pods -l app=<原标签>`，确认 Pod 实际正常运行
4. 对比 Service selector 与 Pod labels，确认不匹配

**注入恢复**：
1. 使用 kubectl patch 将 Service selector 恢复为原始值：
   ```bash
   kubectl patch svc <service-name> -n <namespace> \
     -p '{"spec":{"selector":{"app":"<原始标签>"}}}'
   ```
2. 等待 Endpoints 自动更新

**恢复验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认 Endpoints 列表恢复，包含后端 Pod IP
2. 向 Service 发送请求，确认恢复正常
3. 确认服务可用性恢复

**基准事实**：
- **根因**：Service 的 selector 与后端 Pod 的 label 不匹配，导致 Endpoints 控制器无法关联任何 Pod，Service 无后端可转发
- **必现现象**：Endpoints 为空；Service 请求失败（connection refused/超时）；Pod 正常但未被选中
