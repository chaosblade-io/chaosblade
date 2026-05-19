**用例名称** 镜像不存在或标签错误 导致 Pod_镜像拉取失败

**故障现象**：
1. Pod 状态停留在 ImagePullBackOff 或 ErrImagePull
2. Pod Event 显示镜像拉取失败，错误信息包含 `manifest for xxx not found`

**资源准备**：
1. 确认应用 A/B 所在节点正常运行
2. 确认节点可正常访问镜像仓库

**演练步骤**：
1. 定位应用 A 的 Deployment
2. 修改应用 A 的镜像地址为不存在的镜像名称或标签
3. 观察 Pod 的镜像拉取状态

**注入验证**：
1. 查看 Pod 状态，确认停留在 ImagePullBackOff 或 ErrImagePull
2. 查看 Pod Event 或 kubelet 日志，确认包含镜像不存在相关信息，如 `manifest for xxx not found`

**注入恢复**：
1. 恢复应用 A 的镜像地址为正确的镜像名称和标签
2. 删除异常 Pod，触发重新拉取镜像

**恢复验证**：
1. 查看 Pod 状态，确认恢复为 Running
2. 查看 Pod Event，确认镜像拉取成功

**基准事实**：
- **根因**：Pod 配置的镜像名称拼写错误或标签在镜像仓库中不存在
- **必现现象**：Pod 状态为 ImagePullBackOff 或 ErrImagePull，日志显示 `manifest for xxx not found`
