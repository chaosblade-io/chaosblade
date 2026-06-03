**用例名称** Volume挂载超时CSI异常 导致 Pod_ContainerCreating

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `FailedMount` 或 `FailedAttachVolume`，提示 CSI driver 超时或可用区不匹配
3. PV 对应的云盘与 Pod 调度的节点不在同一可用区

**资源准备**：
1. 确认应用 A 已正常运行，且使用了 CSI 驱动管理的存储卷
2. 确认集群跨多个可用区部署

**演练步骤**：
1. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
2. 使用 `kubectl(subcommand="apply", v_args="-f -", stdin_data="...")` 创建跨可用区的 PV 和 PVC（**必须使用 `stdin_data` 参数传入 YAML，不要用 exec heredoc 或其他方式**）：
   PV YAML:
   ```yaml
   apiVersion: v1
   kind: PersistentVolume
   metadata:
     name: chaos-az-mismatch-pv
   spec:
     capacity:
       storage: 20Gi
     accessModes: ["ReadWriteOnce"]
     csi:
       driver: diskplugin.csi.alibabacloud.com
       volumeHandle: <其他可用区的云盘ID>
     nodeAffinity:
       required:
         nodeSelectorTerms:
         - matchExpressions:
           - key: topology.kubernetes.io/zone
             operator: In
             values: ["<其他可用区>"]
   ```
   PVC YAML:
   ```yaml
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata:
     name: chaos-csi-mismatch-pvc
     namespace: <namespace>
   spec:
     accessModes: ["ReadWriteOnce"]
     resources:
       requests:
         storage: 20Gi
     volumeName: chaos-az-mismatch-pv
   ```
3. 修改应用 A 的 Deployment，添加引用该 PVC 的 volume，并确保 Pod 调度到不同可用区的节点
4. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
5. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）
6. 观察 Pod 的 ContainerCreating 状态

**注入验证**：
1. 执行 `kubectl rollout status deployment <deployment-name>`，确认滚动更新已完成（所有旧 Pod 已被替换）。如果滚动更新未完成（卡死），则故障未完全生效，不可判定为 verified
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 状态为 ContainerCreating（不是仅一个新 Pod，而是全部副本）
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 FailedMount 或 CSI attach 超时
4. 确认错误信息包含可用区不匹配或 CSI 异常相关描述

**注入恢复**：
1. 恢复应用 A 的 Deployment，移除注入时添加的 volumes 和 volumeMounts（两者都需移除，只移除其中一个会导致 Deployment 配置错误）
2. 等待 Pod 滚动更新完成，确认 Pod 恢复 Running
3. 清理测试 PVC：`kubectl delete pvc chaos-csi-mismatch-pvc -n <namespace>`
4. 清理测试 PV：`kubectl delete pv chaos-az-mismatch-pv`

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running
2. 确认测试资源已清理
3. 确认应用 A 存储功能正常

**基准事实**：
- **根因**：CSI 驱动异常或云盘与节点可用区不匹配，导致 Volume attach/mount 操作超时，Pod 无法完成存储卷挂载
- **必现现象**：Pod ContainerCreating；Events 显示 FailedMount/FailedAttachVolume/CSI 超时；可用区不匹配
