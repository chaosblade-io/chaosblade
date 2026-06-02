**用例名称** CNI分配失败 导致 Pod_ContainerCreating

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `failed to allocate for ENI` 或 `no available IP in subnet` 或 CNI 相关错误
3. 节点上的 IP 资源池耗尽或 ENI 数量达到上限

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群使用 ENI 或 vSwitch 分配 Pod IP 的 CNI 插件（如 Terway）

**演练步骤**：
1. 查看目标节点的 ENI 和 IP 分配情况，确认目标应用 Pod 所在节点（记为 `<目标节点>`）
2. 给目标节点添加标签，并给应用 A 的 Deployment 添加 nodeSelector，确保新 Pod 只能调度到目标节点（防止调度器规避耗尽节点）：
   ```bash
   kubectl label node <目标节点> chaos-cni-target=true
   kubectl patch deployment <deployment-name> -n <namespace> --type='merge' \
     -p='{"spec":{"template":{"spec":{"nodeSelector":{"chaos-cni-target":"true"}}}}}'
   ```
   等待 rollout 完成（Pod 仍在原节点运行，因为目标节点已有此标签）。
   记录原始 nodeSelector 值，恢复时还原。
3. 使用 `execute_skill_script` 在目标节点批量创建 Pod 耗尽 IP/ENI 资源（**`kubectl create/apply` 不可用，必须使用脚本**）：
   ```
   execute_skill_script(
     skill_name="k8s-chaos-skills",
     script_name="inject_cni_exhaust.py",
     params="--namespace <namespace> --node <目标节点> --kubeconfig <kubeconfig路径>"
   )
   ```
   脚本会创建 `chaos-ip-exhaust` Deployment 并绑定到目标节点。
4. 删除应用 A 在目标节点上的 Pod，触发重建。由于 nodeSelector 约束，新 Pod 只能调度到已耗尽的目标节点，将进入 ContainerCreating 状态
5. 观察新 Pod 的 ContainerCreating 状态

**注入验证**：
1. 执行 `kubectl get pods`，确认应用 A 新 Pod 状态为 ContainerCreating
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 CNI/IP 分配失败相关错误
3. 查看节点 ENI/IP 使用情况，确认资源已耗尽

**注入恢复**：
1. 删除批量创建的 Deployment：`kubectl delete deployment chaos-ip-exhaust -n <namespace>`
2. 移除应用 A 的 Deployment 上添加的 nodeSelector（还原为原始值，若原本无 nodeSelector 则移除整个 nodeSelector）
3. 移除目标节点上添加的标签：`kubectl label node <目标节点> chaos-cni-target-`
4. 等待 IP/ENI 资源释放和 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认应用 A 的 Pod 状态恢复为 Running
2. 确认节点 IP/ENI 资源恢复可用
3. 确认应用 A 网络连通正常

**基准事实**：
- **根因**：节点可用 IP 池耗尽或 ENI 数量达到上限或 vSwitch IP 不足，CNI 插件无法为新 Pod 分配网络资源
- **必现现象**：Pod ContainerCreating；Events 显示 CNI/IP/ENI 分配失败；节点网络资源耗尽
