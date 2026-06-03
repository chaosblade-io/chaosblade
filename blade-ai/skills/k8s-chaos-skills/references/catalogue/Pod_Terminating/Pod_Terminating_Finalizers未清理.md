**用例名称** Finalizers未清理 导致 Pod_Terminating

**故障现象**：
1. Pod 无法完成删除，`metadata.deletionTimestamp` 已设置但 Pod 对象仍存在于 API 中
2. Pod 的 metadata 中存在 finalizers 字段，等待外部控制器清理
3. kubectl 显示状态可能是 `Terminating`（容器仍在运行时）或 `Error`/`Failed`（容器已退出时）

> **注意**："Terminating" 是 kubectl 的显示状态，仅当 `deletionTimestamp` 已设置
> 且容器仍在运行时才显示。如果容器在 delete 信号后已退出，kubectl 会按容器状态
> 显示（如 Error/Completed），但 Pod 对象仍因 finalizer 滞留在 API 中。
> 判定依据是 `deletionTimestamp` + `finalizers` 同时存在，而非 kubectl 显示状态。

**RCA症状**：
1. `kubectl get pod` 显示 Pod 状态为 Terminating 或 Error/Failed，但 Pod 对象持续存在
2. `kubectl get pod -o jsonpath='{.metadata.deletionTimestamp}'` 返回非空时间戳
3. `kubectl get pod -o jsonpath='{.metadata.finalizers}'` 返回非空列表
（以上为kubectl直接可观测的现象，不包含诊断结论）

**资源准备**：
1. 确认目标应用已正常运行
2. 确认目标 Pod 当前没有 finalizers（`kubectl get pod <name> -o jsonpath='{.metadata.finalizers}'` 返回空）

**演练步骤**：
1. 定位目标 Pod
2. 使用 kubectl patch 给 Pod 添加自定义 finalizer：
   `kubectl patch pod <pod-name> -n <namespace> -p '{"metadata":{"finalizers":["example.com/block-deletion"]}}'`
3. 使用 `--wait=false` 删除 Pod，触发终止流程：
   `kubectl delete pod <pod-name> -n <namespace> --wait=false`
   > 不加 `--wait=false` 会导致 kubectl 等待删除完成，因 finalizer 阻塞而超时。
4. 观察 Pod 状态变化

**注入验证**：
1. 执行 `kubectl get pod <pod-name>`，确认 Pod 对象仍存在（显示 Terminating 或 Error）
2. 执行 `kubectl get pod <pod-name> -o jsonpath='{.metadata.deletionTimestamp}'`，确认已设置
3. 执行 `kubectl get pod <pod-name> -o jsonpath='{.metadata.finalizers}'`，确认包含注入的 finalizer
4. 确认没有控制器在处理该 finalizer（`example.com/block-deletion` 是注入的假标识）

**注入恢复**：
1. 移除 Pod 上的 finalizer：
   `kubectl patch pod <pod-name> -n <namespace> --type=json -p '[{"op":"remove","path":"/metadata/finalizers"}]'`
2. Pod 将被 Kubernetes GC 自动清除

**恢复验证**：
1. 执行 `kubectl get pod <pod-name>`，确认 Pod 已从集群中删除（返回 NotFound）
2. 确认 ReplicaSet 已创建替代 Pod 且状态为 Running

**基准事实**：
- **根因**：Pod 的 metadata 中存在 finalizers，Kubernetes 在删除资源时会设置 deletionTimestamp 但不会从 etcd 中移除资源对象，直到所有 finalizer 被外部控制器清除
- **必现现象**：Pod 对象持续存在于 API 中；deletionTimestamp 已设置；metadata.finalizers 非空
