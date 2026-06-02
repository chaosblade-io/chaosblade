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
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. 修改应用 A 的 Deployment，将 readinessProbe 路径或端口设置为与实际不一致：
   ```yaml
   readinessProbe:
     httpGet:
       path: /non-existent-health-path
       port: 9999    # 应用实际不监听此端口
     periodSeconds: 5
     failureThreshold: 3
   ```
4. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
5. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）
6. 观察 Pod Ready 状态和 Service Endpoints 变化

**注入验证**：
1. 执行 `kubectl rollout status deployment <deployment-name>`，确认滚动更新已完成（所有旧 Pod 已被替换）。如果滚动更新未完成（卡死），则故障未完全生效，不可判定为 verified
2. 执行 `kubectl get pods`，确认**所有** Pod 状态为 Running 但 READY 列显示 0/1（不是仅一个新 Pod 0/1，而是全部副本都 0/1）
3. 执行 `kubectl get endpoints <service-name>`，确认 Endpoints 列表**为空**（无子集），而非仅新 Pod 不在列表中
4. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 `Readiness probe failed`
5. 向 Service 发送请求，确认返回 connection refused 或超时。注意：connection reset by peer 可能是应用自身行为而非故障效果，不可作为故障生效的充分证据

**注入恢复**：
1. 恢复应用 A 的 Deployment，将 readinessProbe 的路径和端口修正为正确值（若原本无 readinessProbe，则移除）
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod READY 为 1/1
2. 执行 `kubectl get endpoints <service-name>`，确认 Pod 重新加入 Endpoints
3. 向 Service 发送请求，确认服务恢复正常

**基准事实**：
- **根因**：Readiness Probe 的路径或端口与应用实际监听不一致，Probe 持续失败导致 Pod 被标记为 Not Ready，从 Service Endpoints 中移除
- **必现现象**：Pod Running 但 Not Ready（0/1）；Endpoints 为空；Events 显示 Readiness probe failed
