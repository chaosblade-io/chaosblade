**用例名称** 云盘挂载超时或冲突 导致 Pod_ContainerCreating

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `Multi-Attach error` 或 `AttachVolume.Attach failed`
3. 云盘被其他节点占用，无法 attach 到当前节点

**资源准备**：
1. 确认应用 A 已正常运行，且使用了云盘类型的 PVC（accessMode 为 ReadWriteOnce）
2. 确认集群中有多个节点

**演练步骤**：
1. 定位应用 A 使用的 PVC 及其对应的 PV
2. 在另一个节点上创建一个临时 Pod，强制挂载同一个 PVC（通过直接指定 PV 的 volumeHandle 创建新 PV/PVC 对），模拟 Multi-Attach 冲突：
   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: chaos-volume-holder
     namespace: <namespace>
   spec:
     nodeName: <另一节点>
     containers:
     - name: holder
       image: busybox
       command: ["sleep", "3600"]
       volumeMounts:
       - name: data
         mountPath: /data
     volumes:
     - name: data
       persistentVolumeClaim:
         claimName: <应用A的PVC名称>
   ```
3. 删除应用 A 原来的 Pod，触发在其他节点重建
4. 观察新 Pod 的 ContainerCreating 状态

**注入验证**：
1. 执行 `kubectl get pods`，确认应用 A 的新 Pod 状态为 ContainerCreating
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 Multi-Attach error 或 volume attach 失败
3. 确认 PV 仍 attach 在占用它的节点上

**注入恢复**：
1. 删除临时 Pod：`kubectl delete pod chaos-volume-holder --force --grace-period=0`
2. 等待云盘从原节点 detach
3. 等待应用 A 的 Pod 自动完成 volume attach

**恢复验证**：
1. 执行 `kubectl get pods`，确认应用 A 的 Pod 状态恢复为 Running
2. 确认 volume 成功 attach 并 mount
3. 确认应用 A 数据读写正常

**基准事实**：
- **根因**：云盘（ReadWriteOnce）被其他节点/Pod 占用，新 Pod 无法 attach 该云盘，导致 volume mount 阶段阻塞
- **必现现象**：Pod ContainerCreating；Events 显示 Multi-Attach error 或 AttachVolume 失败；PV 被其他节点占用
