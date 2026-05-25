**用例名称** limit单位写错 导致 Pod_OOM内存异常

**故障现象**：
1. Pod 启动后立即被 OOMKilled，状态为 CrashLoopBackOff
2. Pod 的 lastState 显示 reason: OOMKilled
3. 容器 memory limit 值极小（如 100m = 0.1 字节），应用启动即超限

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认应用 A 的正常内存使用量（如 200Mi 以上）

**演练步骤**：
1. 记录应用 A 当前的 resources.limits.memory 配置
2. 修改应用 A 的 Deployment，将 memory limit 单位写错：
   ```yaml
   resources:
     limits:
       memory: "100m"    # 错误！100m = 0.1 字节（milli），应为 100Mi
     requests:
       memory: "100m"
   ```
   注意：在 Kubernetes 中，`m` 表示 milli（千分之一），`100m` = 0.1 字节；正确应为 `Mi`（Mebibyte）
3. 等待 Pod 滚动更新
4. 观察 Pod 启动行为

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 CrashLoopBackOff，RESTARTS 持续增长
2. 执行 `kubectl get pod <pod-name> -o jsonpath='{.status.containerStatuses[0].lastState}'`，确认 reason 为 OOMKilled
3. 执行 `kubectl describe pod <pod-name>`，确认 limits.memory 为极小值
4. 确认容器启动后立即被杀（运行时间极短）

**注入恢复**：
1. 恢复应用 A 的 Deployment，将 memory limit 修正为正确单位（如 `256Mi`）
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Running 且不再重启
2. 确认容器正常运行，内存使用率在合理范围
3. 确认应用 A 服务正常

**基准事实**：
- **根因**：memory limit 单位写错（如 `100m` 而非 `100Mi`），导致 limit 值极小，容器启动后内存使用立即超过 limit 被 OOMKill
- **必现现象**：Pod 启动即 OOMKilled；CrashLoopBackOff；limits.memory 值不合理（如 100m）；容器运行时间极短
