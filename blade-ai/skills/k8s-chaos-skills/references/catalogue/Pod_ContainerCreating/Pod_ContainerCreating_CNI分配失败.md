**用例名称** CNI分配失败 导致 Pod_ContainerCreating

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `failed to allocate for ENI` 或 `no available IP in subnet` 或 CNI 相关错误
3. 节点上的 IP 资源池耗尽或 ENI 数量达到上限

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群使用 ENI 或 vSwitch 分配 Pod IP 的 CNI 插件（如 Terway）

**演练步骤**：
1. 查看目标节点的 ENI 和 IP 分配情况
2. 在目标节点上批量创建大量 Pod（使用 DaemonSet 或指定 nodeName），耗尽该节点的可用 IP/ENI 资源：
   ```bash
   kubectl create deployment chaos-ip-exhaust \
     --image=busybox \
     --replicas=<大量副本数> \
     -- sleep 3600
   kubectl patch deployment chaos-ip-exhaust -p '{"spec":{"template":{"spec":{"nodeName":"<目标节点>"}}}}'
   ```
3. 删除应用 A 在目标节点上的 Pod，触发重建
4. 观察新 Pod 的 ContainerCreating 状态

**注入验证**：
1. 执行 `kubectl get pods`，确认应用 A 新 Pod 状态为 ContainerCreating
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 CNI/IP 分配失败相关错误
3. 查看节点 ENI/IP 使用情况，确认资源已耗尽

**注入恢复**：
1. 删除批量创建的 Pod/Deployment：`kubectl delete deployment chaos-ip-exhaust`
2. 等待 IP/ENI 资源释放
3. 等待应用 A 的 Pod 自动完成网络分配

**恢复验证**：
1. 执行 `kubectl get pods`，确认应用 A 的 Pod 状态恢复为 Running
2. 确认节点 IP/ENI 资源恢复可用
3. 确认应用 A 网络连通正常

**基准事实**：
- **根因**：节点可用 IP 池耗尽或 ENI 数量达到上限或 vSwitch IP 不足，CNI 插件无法为新 Pod 分配网络资源
- **必现现象**：Pod ContainerCreating；Events 显示 CNI/IP/ENI 分配失败；节点网络资源耗尽
