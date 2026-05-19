**用例名称** 存储类配置错误 导致 PVC_Pending_存储类配置错误

**故障现象**：
1. PVC 状态长时间停留在 Pending
2. PVC Event 显示 `provisioning failed`、`storageclass not found` 或 `waiting for a volume to be created`

**资源准备**：
1. 确认集群中存在可用的 StorageClass
2. 确认应用 A 的 Deployment/StatefulSet 依赖该 PVC

**演练步骤**：
1. 定位应用 A 关联的 PVC
2. 修改 PVC 的 `storageClassName` 为一个集群中不存在或不支持的存储类名称（如 `invalid-storage-class`）
3. 观察 PVC 的状态变化及 Event 信息

**注入验证**：
1. 查看 PVC 状态，确认停留在 Pending
2. 查看 PVC Event，确认包含存储类错误或供应失败相关信息，如 `storageclass.storage.k8s.io "invalid-storage-class" not found`

**注入恢复**：
1. 将 PVC 的 `storageClassName` 恢复为正确的存储类名称
2. 删除并重建异常的 PVC（注意：生产环境需谨慎操作，避免数据丢失）

**恢复验证**：
1. 查看 PVC 状态，确认为 Bound
2. 查看关联的 Pod 状态，确认能正常挂载存储并进入 Running

**基准事实**：
- **根因**：PVC 指定的 StorageClass 在集群中不存在，或对应的 Provisioner 无法正常工作
- **必现现象**：PVC 状态为 Pending，Event 提示找不到存储类或供应失败