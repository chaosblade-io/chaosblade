**用例名称** StartupProbe配置不足 导致 Pod_CrashLoopBackOff

**故障现象**：
1. Pod 反复重启，状态为 CrashLoopBackOff
2. Pod Events 中显示 `Startup probe failed` 后容器被杀
3. 慢启动应用尚未完成初始化就被 StartupProbe 判定为失败

**资源准备**：
1. 确认应用 A 已正常运行（应用启动时间较长，如 Java 应用）
2. 确认应用 A 启动过程中健康检查接口不可用

**演练步骤**：
1. 记录应用 A 当前的探针配置
2. 修改应用 A 的 Deployment，添加或修改 startupProbe 使其窗口不足以覆盖应用启动时间：
   ```yaml
   startupProbe:
     httpGet:
       path: /healthz
       port: 8080
     failureThreshold: 3
     periodSeconds: 5
   ```
   （总等待时间 = failureThreshold × periodSeconds = 15 秒，远小于应用实际启动时间）
3. 同时确保 livenessProbe 存在，使得 startupProbe 失败后触发容器重启
4. 触发 Pod 重建（如 rollout restart）
5. 观察 Pod 启动行为

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod RESTARTS 持续增长，状态为 CrashLoopBackOff
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 `Startup probe failed`
3. 查看容器日志，确认应用正在启动但未完成初始化就被杀

**注入恢复**：
1. 恢复应用 A 的 Deployment，增大 startupProbe 的窗口时间：
   - 增大 failureThreshold（如 30）
   - 或增大 periodSeconds（如 10）
   - 确保 failureThreshold × periodSeconds > 应用实际启动时间
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Running 且不再重启
2. 确认 StartupProbe 和 LivenessProbe 检查均正常通过
3. 确认应用 A 完成初始化并正常服务

**基准事实**：
- **根因**：StartupProbe 未配置或 failureThreshold × periodSeconds 总窗口不足以覆盖应用启动时间，慢启动应用在初始化完成前被判定为启动失败，反复被杀重启
- **必现现象**：Pod CrashLoopBackOff；Events 显示 Startup probe failed；容器日志显示应用启动中被中断
