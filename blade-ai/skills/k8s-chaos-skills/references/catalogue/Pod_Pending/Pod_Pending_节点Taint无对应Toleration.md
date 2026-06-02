**用例名称** 节点Taint无对应Toleration 导致 Pod_Pending

**故障现象**：
1. Pod 状态为 Pending，无法被调度到任何节点
2. Pod Events 中显示 `N node(s) had untolerated taint {chaos-test: true}`
3. 目标 Pod 可调度的所有节点均带有 Pod 无法容忍的污点

**RCA症状**：
1. Pod 状态为 Pending，无法被调度到任何节点
2. Pod Events 中显示 `had untolerated taint`
（以上为kubectl直接可观测的现象，不包含诊断结论）

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群中有多个可调度节点
3. 记录应用 A 的 Pod 当前运行在哪些节点上（这些节点即为"目标节点"）

**演练步骤**：

> **爆炸半径控制**：本用例仅 taint 目标 Pod 所在的节点（而非全部集群节点），
> 通过 nodeSelector 约束目标 Pod 只能调度到这些节点，从而在保证故障复现的同时
> 避免影响集群中其他工作负载的调度。

1. 记录目标节点的当前 taint 信息，以及 Deployment 的当前 nodeSelector（用于恢复）
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. 给目标节点添加标签：`kubectl label node <node> chaos-target=<app-name>`（仅目标 Pod 所在节点）
4. 给应用 A 的 Deployment 添加 nodeSelector，约束 Pod 只能调度到目标节点：
   `kubectl patch deployment <name> -n <ns> -p '{"spec":{"template":{"spec":{"nodeSelector":{"chaos-target":"<app-name>"}}}}}'`
5. 等待 rollout 完成（Pod 仍在原节点上运行，因为只有目标节点有此标签）
6. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）
7. 给目标节点添加污点：`kubectl taint node <node> chaos-test=true:NoSchedule`（仅目标节点）
8. 删除应用 A 的一个 Pod，触发重建调度
9. 观察新 Pod 的调度状态

**注入验证**：
1. 执行 `kubectl get pods`，确认新 Pod 状态为 Pending
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 中显示 `had untolerated taint {chaos-test: true}`
3. 确认目标节点均有 chaos-test taint

**注入恢复**：
1. 移除目标节点上添加的污点：`kubectl taint node <node> chaos-test=true:NoSchedule-`
2. 移除 Deployment 的 nodeSelector 中添加的 chaos-target 键（若原 Deployment 无 nodeSelector，则移除整个 nodeSelector）
3. 移除目标节点上添加的标签：`kubectl label node <node> chaos-target-`
4. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态变为 Running
2. 确认目标节点 taint 已恢复到演练前状态
3. 确认 Deployment spec 已恢复到演练前状态

**基准事实**：
- **根因**：目标 Pod 可调度的所有节点被标记了 Taint，而 Pod 未配置对应的 Toleration，导致调度器无法找到合适节点
- **必现现象**：Pod Pending；Events 显示 untolerated taint；目标节点带有不可容忍的污点
