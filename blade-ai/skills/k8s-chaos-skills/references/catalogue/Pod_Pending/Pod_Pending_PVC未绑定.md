**用例名称** PVC未绑定 导致 Pod_Pending

**故障现象**：
1. Pod 状态为 Pending，无法启动
2. Pod Events 中显示 `pod has unbound immediate PersistentVolumeClaims`
3. PVC 状态为 Pending，无法绑定到 PV

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群中 StorageClass 和 CSI 插件正常工作

**演练步骤**：
1. 创建一个引用不存在的 StorageClass 的 PVC：
   ```yaml
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata:
     name: chaos-test-pvc
     namespace: <namespace>
   spec:
     accessModes: ["ReadWriteOnce"]
     storageClassName: "non-existent-sc"
     resources:
       requests:
         storage: 10Gi
   ```
2. 修改应用 A 的 Deployment，添加引用该 PVC 的 volume
3. 删除应用 A 的一个 Pod，触发使用新 PVC 的 Pod 重建
4. 观察新 Pod 的状态

**注入验证**：
1. 执行 `kubectl get pvc`，确认 PVC 状态为 Pending
2. 执行 `kubectl get pods`，确认 Pod 状态为 Pending
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 unbound PVC 相关信息
4. 执行 `kubectl describe pvc chaos-test-pvc`，确认 StorageClass 不存在或 Provisioner 异常

**注入恢复**：
1. 恢复应用 A 的 Deployment 定义，移除引用 chaos-test-pvc 的 volume
2. 删除测试用 PVC：`kubectl delete pvc chaos-test-pvc`
3. 等待 Pod 自动重建

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running
2. 确认测试 PVC 已被清理

**基准事实**：
- **根因**：Pod 引用的 PVC 无法绑定，原因为 StorageClass 不存在或 Provisioner 异常，导致 Pod 无法挂载所需存储卷而 Pending
- **必现现象**：Pod Pending；PVC Pending；Events 显示 unbound PersistentVolumeClaims
