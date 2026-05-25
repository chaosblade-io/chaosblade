**用例名称** Volume挂载超时CSI异常 导致 Pod_ContainerCreating

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `FailedMount` 或 `FailedAttachVolume`，提示 CSI driver 超时或可用区不匹配
3. PV 对应的云盘与 Pod 调度的节点不在同一可用区

**资源准备**：
1. 确认应用 A 已正常运行，且使用了 CSI 驱动管理的存储卷
2. 确认集群跨多个可用区部署

**演练步骤**：
1. 创建一个指定可用区的 PVC/PV（选择与应用 A 所在节点不同的可用区）：
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
2. 修改应用 A 的 Deployment，添加引用该 PV 对应 PVC 的 volume，并确保 Pod 调度到不同可用区的节点
3. 触发 Pod 重建
4. 观察 Pod 的 ContainerCreating 状态

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 ContainerCreating
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 FailedMount 或 CSI attach 超时
3. 确认错误信息包含可用区不匹配或 CSI 异常相关描述

**注入恢复**：
1. 恢复应用 A 的 Deployment，移除引用测试 PVC 的 volume
2. 清理测试 PV/PVC：`kubectl delete pv chaos-az-mismatch-pv`
3. 等待 Pod 自动重建

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running
2. 确认测试资源已清理
3. 确认应用 A 存储功能正常

**基准事实**：
- **根因**：CSI 驱动异常或云盘与节点可用区不匹配，导致 Volume attach/mount 操作超时，Pod 无法完成存储卷挂载
- **必现现象**：Pod ContainerCreating；Events 显示 FailedMount/FailedAttachVolume/CSI 超时；可用区不匹配
