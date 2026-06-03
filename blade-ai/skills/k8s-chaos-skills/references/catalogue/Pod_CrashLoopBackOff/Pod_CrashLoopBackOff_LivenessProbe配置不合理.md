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
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. 修改应用 A 的 Deployment，设置不合理的 livenessProbe 参数：
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
4. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
5. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）
6. 观察 Pod 重启行为

**注入验证**：
1. 执行 `kubectl rollout status deployment <deployment-name>`，确认滚动更新已完成（所有旧 Pod 已被替换）。如果滚动更新未完成（卡死），则故障未完全生效，不可判定为 verified
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 的 RESTARTS 数持续增长，状态为 CrashLoopBackOff（不是仅一个新 Pod，而是全部副本）
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 `Liveness probe failed` 和 `Container will be killed`
4. 查看容器日志，确认应用本身无异常退出（非应用 bug）

**注入恢复**：
1. 恢复应用 A 的 Deployment，将 livenessProbe 参数恢复为原始合理值（若原本无 livenessProbe，则移除）
3. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Running 且不再重启
2. 确认 Liveness Probe 检查正常通过
3. 确认应用 A 服务正常

**基准事实**：
- **根因**：Liveness Probe 的 initialDelaySeconds/timeoutSeconds/failureThreshold 配置不合理，导致健康检查在应用正常运行时误判为失败，kubelet 反复杀死并重启容器
- **必现现象**：Pod CrashLoopBackOff；RESTARTS 持续增长；Events 显示 Liveness probe failed；容器日志无异常
