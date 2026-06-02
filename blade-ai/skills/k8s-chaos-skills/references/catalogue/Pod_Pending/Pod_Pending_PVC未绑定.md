**用例名称** PVC未绑定 导致 Pod_Pending

**故障现象**：
1. Pod 状态为 Pending，无法启动
2. Pod Events 中显示 `pod has unbound immediate PersistentVolumeClaims`
3. PVC 状态为 Pending，无法绑定到 PV

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群中 StorageClass 和 CSI 插件正常工作

**演练步骤**（注意：本用例需要临时修改 Deployment 添加 volume 引用，这是故障注入的必要操作，不违反安全红线。目标应用无需预先配置 PVC——注入的目的就是添加一个无法绑定的 PVC 依赖。恢复步骤会还原所有修改）：
1. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
2. 使用 `kubectl apply -f` 创建一个引用不存在的 StorageClass 的 PVC（通过 `stdin_data` 传入 YAML）：
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
3. 使用 `kubectl patch` 修改应用 A 的 Deployment，添加引用该 PVC 的 volume 和 volumeMount
4. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
5. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）
6. 观察新 Pod 的状态

**注入验证**：
1. 执行 `kubectl rollout status deployment <deployment-name>`，确认滚动更新已完成（所有旧 Pod 已被替换）。如果滚动更新未完成（卡死），则故障未完全生效，不可判定为 verified
2. 执行 `kubectl get pvc`，确认 PVC 状态为 Pending
3. 执行 `kubectl get pods`，确认**所有**目标 Pod 状态为 Pending（不是仅一个新 Pod，而是全部副本）
4. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 unbound PVC 相关信息
5. 执行 `kubectl describe pvc chaos-test-pvc`，确认 StorageClass 不存在或 Provisioner 异常

**注入恢复**：
1. 恢复应用 A 的 Deployment 定义，移除引用 chaos-test-pvc 的 volume
2. 删除测试用 PVC：`kubectl delete pvc chaos-test-pvc`
3. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running
2. 确认测试 PVC 已被清理

**基准事实**：
- **根因**：Pod 引用的 PVC 无法绑定，原因为 StorageClass 不存在或 Provisioner 异常，导致 Pod 无法挂载所需存储卷而 Pending
- **必现现象**：Pod Pending；PVC Pending；Events 显示 unbound PersistentVolumeClaims
