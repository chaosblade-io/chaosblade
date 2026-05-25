**用例名称** ReadinessProbe配置不一致 导致 Service_调用失败

**故障现象**：
1. Pod 状态为 Running 但 READY 为 0/1
2. Service 的 Endpoints 列表为空或逐渐减少
3. Readiness Probe 持续失败，Pod 从 Service 后端移除

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service
2. 确认应用 A 实际监听的端口和健康检查路径

**演练步骤**：
1. 记录应用 A 当前的 readinessProbe 配置
2. 修改应用 A 的 Deployment，将 readinessProbe 路径或端口设置为与实际不一致：
   ```yaml
   readinessProbe:
     httpGet:
       path: /non-existent-health-path
       port: 9999    # 应用实际不监听此端口
     periodSeconds: 5
     failureThreshold: 3
   ```
3. 等待 Pod 滚动更新
4. 观察 Pod Ready 状态和 Service Endpoints 变化

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Running 但 READY 列显示 0/1
2. 执行 `kubectl get endpoints <service-name>`，确认 Endpoints 中无该 Pod
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 `Readiness probe failed`
4. 向 Service 发送请求，确认服务不可用或负载下降

**注入恢复**：
1. 恢复应用 A 的 Deployment，将 readinessProbe 的路径和端口修正为正确值
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod READY 为 1/1
2. 执行 `kubectl get endpoints <service-name>`，确认 Pod 重新加入 Endpoints
3. 向 Service 发送请求，确认服务恢复正常

**基准事实**：
- **根因**：Readiness Probe 的路径或端口与应用实际监听不一致，Probe 持续失败导致 Pod 被标记为 Not Ready，从 Service Endpoints 中移除
- **必现现象**：Pod Running 但 Not Ready（0/1）；Endpoints 为空；Events 显示 Readiness probe failed
