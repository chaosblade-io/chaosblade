**用例名称** 云盘挂载超时或冲突 导致 Pod_ContainerCreating

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `Multi-Attach error` 或 `AttachVolume.Attach failed`
3. 云盘被其他节点占用，无法 attach 到当前节点

**资源准备**：
1. 确认应用 A 已正常运行，且使用了云盘类型的 PVC（accessMode 为 ReadWriteOnce）
2. 确认集群中有多个节点，且存在与应用 A 当前节点**同可用区的另一节点**（云盘只能 attach 到同 zone 节点，记为 `<占用节点>`）
3. **删除类注解风险核查（必做）**：检查 PVC、PV 及其所属工作负载是否携带删除/清理类注解（如 `kubeone.ali/enable-cascading-deletion: "true"`，命令：`kubectl get pvc <pvc> -n <ns> -o yaml | grep -i -e deletion -e cleanup -e retention`，PV 与工作负载同理）。命中任何一项时，删除目标 Pod 会触发 PVC 被级联删除，冲突机制不可达且会造成数据盘丢失——**此路径不可用**，必须换用不删除 Pod 的替代方案或终止演练，禁止继续执行步骤 3
4. 确认占用者 Pod 使用的镜像在集群内可拉取（内网环境须使用内网镜像仓库地址），避免占用者 Pod 陷入 ImagePullBackOff 导致云盘一直无法 attach

**演练步骤**：
1. 前置核查：定位应用 A 使用的 PVC（`kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.volumes[*].persistentVolumeClaim.claimName}'`）及其对应的 PV；确认应用 A 当前所在节点与 `<占用节点>` 位于同一可用区
2. 创建一个占用者 Pod，直接声明应用 A 的同一个 PVC（claimName），并用 nodeName 钉到 `<占用节点>`，使云盘 attach 到该节点形成 Multi-Attach 冲突。**此 apply 受目标守卫校验，manifest 必须满足占用者契约**：仅单个 `kind: Pod` 文档；容器 command 只能是 `sleep`（不得有 args/initContainers）；不带 hostNetwork/hostPID/hostIPC/privileged/capabilities；volumes 只允许 persistentVolumeClaim 且必须引用批准目标的 PVC；必须带 `activeDeadlineSeconds`（≤ 3600）：
   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: vol-attach-checker
     namespace: <namespace>
   spec:
     activeDeadlineSeconds: 1800
     nodeName: <占用节点>
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
   **不要等待占用者 Running 再继续**：应用 A 的 Pod 仍在使用该 PVC，RWO 卷按 Pod 使用者
   排他，占用者此刻必然卡在 ContainerCreating，Events 显示 `Multi-Attach error`（等待卷
   从原使用者释放）——这是预期时序，不是注入失败。
3. 删除应用 A 原来的 Pod，触发在其他节点重建。卷从原节点 detach 后，占用者与新 Pod 对该 RWO
   云盘形成 Multi-Attach 竞争，**竞争结局是概率性的**（AD controller 对 Multi-Attach 失败按
   退避重试，双方谁的 attach 重试先成功谁赢，无法预先指定）：
   - **结局 A**：占用者抢先 attach 到 `<占用节点>` 转 Running，新 Pod 调度到其他节点后卡
     ContainerCreating 并反复报 Multi-Attach error
   - **结局 B（实测出现过镜像反转）**：新 Pod 抢先 attach 转 Running，占用者反卡
     ContainerCreating（其 attach 重试已进入长退避）
   两种结局均视为注入生效——**判定标志是“恰好一方 Running、另一方 ContainerCreating 且报
   Multi-Attach error”，不绑定哪一方胜出**
4. 观察竞争结局（卡住一方的 ContainerCreating 状态及其 Multi-Attach error Events）

**机制反证条件（命中即停）**：
删除目标 Pod 后，若观察到以下任一现象，说明挂载冲突机制不可达（资源被删除或发生其他无关故障，而非卡住），**立即停止一切尝试，上报偏离并转入恢复，禁止继续重试或加压**：
1. PVC 或 PV 进入 Terminating 状态或被删除（说明存在级联删除/清理策略，继续操作会扩大破坏）
2. 工作负载报 FailedCreate / CrashLoopBackOff 而非 Pod 停留 ContainerCreating
3. 新 Pod 长时间 Pending 且原因是 PVC 不存在（而非 volume attach 冲突）
4. 竞争双方均长期停留 ContainerCreating（双方 Events 均报 Multi-Attach error 且超过合理收敛
   时长仍未分出胜者），说明竞争未收敛，冲突机制未按预期成立

**注入验证**：
1. 执行 `kubectl get pods`，确认占用者与新 Pod 恰好一方 Running、另一方停留在
   ContainerCreating（无论哪一方胜出，见步骤 3 的两种结局）
2. 执行 `kubectl describe pod <卡住一方的Pod名>`，确认 Events 显示 Multi-Attach error 或
   volume attach 失败
3. 确认 PV 仍 attach 在胜出方的节点上

**注入恢复**：
1. 删除占用者 Pod：`kubectl delete pod vol-attach-checker -n <namespace> --force --grace-period=0`（该 Pod 已由框架登记为演练载具，此删除会被守卫豁免）
2. 若为结局 A（占用者赢得 attach），等待云盘从 `<占用节点>` detach；若为结局 B（新 Pod 已
   赢得 attach），云盘已随新 Pod 正常挂载，此步自然跳过
3. 等待应用 A 的 Pod 自动完成 volume attach

**恢复验证**：
1. 执行 `kubectl get pods`，确认应用 A 的 Pod 状态恢复为 Running
2. 确认 volume 成功 attach 并 mount
3. 确认应用 A 数据读写正常

**基准事实**：
- **根因**：云盘（ReadWriteOnce）被其他节点/Pod 占用，新 Pod 无法 attach 该云盘，导致 volume mount 阶段阻塞
- **必现现象**：Pod ContainerCreating；Events 显示 Multi-Attach error 或 AttachVolume 失败；PV 被其他节点占用
