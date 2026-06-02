**用例名称** 镜像不存在或标签错误 导致 Pod_镜像拉取失败

**故障现象**：
1. Pod 状态停留在 ImagePullBackOff 或 ErrImagePull
2. Pod Event 显示镜像拉取失败，错误信息包含 `manifest for xxx not found`

**资源准备**：
1. 确认应用 A/B 所在节点正常运行
2. 确认节点可正常访问镜像仓库

**演练步骤**：
1. 定位应用 A 的 Deployment
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. 修改应用 A 的镜像地址为不存在的镜像名称或标签
4. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
5. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）
6. 观察 Pod 的镜像拉取状态

**注入验证**：
1. 执行 `kubectl rollout status deployment <deployment-name>`，确认滚动更新已完成（所有旧 Pod 已被替换）。如果滚动更新未完成（卡死），则故障未完全生效，不可判定为 verified
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 状态停留在 ImagePullBackOff 或 ErrImagePull（不是仅一个新 Pod，而是全部副本）
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 包含镜像不存在相关信息，如 `manifest for xxx not found`

**注入恢复**：
1. 恢复应用 A 的镜像地址为正确的镜像名称和标签
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 查看 Pod 状态，确认恢复为 Running
2. 查看 Pod Event，确认镜像拉取成功

**基准事实**：
- **根因**：Pod 配置的镜像名称拼写错误或标签在镜像仓库中不存在
- **必现现象**：Pod 状态为 ImagePullBackOff 或 ErrImagePull，日志显示 `manifest for xxx not found`
