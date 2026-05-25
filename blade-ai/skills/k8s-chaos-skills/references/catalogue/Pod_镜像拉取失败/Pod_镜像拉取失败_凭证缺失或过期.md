**用例名称** 凭证缺失或过期 导致 Pod_镜像拉取失败

**故障现象**：
1. Pod 状态为 ImagePullBackOff 或 ErrImagePull
2. Pod Events 中显示 `unauthorized` 或 `authentication required`
3. 镜像仓库返回 401/403 认证错误

**资源准备**：
1. 确认应用 A 已正常运行，且使用私有镜像仓库
2. 确认应用 A 的 Pod 配置了 imagePullSecrets

**演练步骤**：
1. 记录应用 A 当前使用的 imagePullSecrets 名称
2. 创建一个包含无效凭证的 Secret 来替换原有的有效凭证：
   ```bash
   kubectl create secret docker-registry chaos-invalid-secret \
     --docker-server=<registry-server> \
     --docker-username=invalid-user \
     --docker-password=invalid-password \
     --namespace <namespace>
   ```
3. 修改应用 A 的 Deployment，将 imagePullSecrets 指向无效 Secret（或直接移除 imagePullSecrets）
4. 删除应用 A 的一个 Pod，触发重建和镜像拉取
5. 观察 Pod 状态变化

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 ImagePullBackOff 或 ErrImagePull
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 中显示认证失败相关错误
3. 确认错误信息包含 `unauthorized` 或 `authentication required`

**注入恢复**：
1. 恢复应用 A 的 Deployment，将 imagePullSecrets 指回原有的有效 Secret
2. 删除测试用的无效 Secret：`kubectl delete secret chaos-invalid-secret`
3. 等待 Pod 自动重试镜像拉取

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running
2. 确认镜像拉取成功，无认证错误

**基准事实**：
- **根因**：imagePullSecrets 缺失或 Secret 中的凭证已过期/无效，导致向私有镜像仓库拉取镜像时认证失败
- **必现现象**：Pod ImagePullBackOff；Events 显示 unauthorized/authentication required；镜像仓库返回 401/403
