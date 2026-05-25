**用例名称** LivenessProbe配置不合理 导致 Pod_CrashLoopBackOff

**故障现象**：
1. Pod 反复重启，状态为 CrashLoopBackOff
2. Pod Events 中显示 `Liveness probe failed` 后容器被杀
3. 容器本身运行正常，但 Liveness Probe 配置参数不合理导致误判

**RCA症状**：
1. Pod 反复重启，状态为 CrashLoopBackOff
2. Pod Events 中显示 `Liveness probe failed`，容器被反复杀死
3. 容器日志无应用异常退出记录
（以上为kubectl直接可观测的现象，不包含诊断结论）

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认应用 A 有健康检查接口

**演练步骤**：
1. 记录应用 A 当前的 livenessProbe 配置
2. 修改应用 A 的 Deployment，设置不合理的 livenessProbe 参数：
   ```yaml
   livenessProbe:
     httpGet:
       path: /healthz
       port: 8080
     initialDelaySeconds: 1
     timeoutSeconds: 1
     periodSeconds: 2
     failureThreshold: 1
   ```
   （initialDelaySeconds 过短，应用未完成初始化；timeoutSeconds 过短，正常响应来不及返回；failureThreshold 为 1，无容错空间）
3. 等待 Pod 滚动更新
4. 观察 Pod 重启行为

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod RESTARTS 数持续增长，状态为 CrashLoopBackOff
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 `Liveness probe failed` 和 `Container will be killed`
3. 查看容器日志，确认应用本身无异常退出（非应用 bug）

**注入恢复**：
1. 恢复应用 A 的 Deployment，将 livenessProbe 参数恢复为原始合理值
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Running 且不再重启
2. 确认 Liveness Probe 检查正常通过
3. 确认应用 A 服务正常

**基准事实**：
- **根因**：Liveness Probe 的 initialDelaySeconds/timeoutSeconds/failureThreshold 配置不合理，导致健康检查在应用正常运行时误判为失败，kubelet 反复杀死并重启容器
- **必现现象**：Pod CrashLoopBackOff；RESTARTS 持续增长；Events 显示 Liveness probe failed；容器日志无异常
