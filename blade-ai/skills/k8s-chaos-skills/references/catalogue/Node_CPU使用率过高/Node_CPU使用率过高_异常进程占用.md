**用例名称** 异常进程占用 导致 Node_CPU使用率过高

**故障现象**：
1. 节点 CPU 使用率持续超过 90%
2. 节点上 Pod 响应变慢，出现超时
3. Load Average 显著升高

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认监控系统（如 Prometheus）已配置，可观测节点 CPU 指标

**演练步骤**：
1. 定位运行应用 A 的节点
2. 使用 chaosblade 对该节点注入 CPU 满载，模拟异常进程占用节点 CPU
3. 观察节点 CPU 使用率及 Pod 性能变化

**注入验证**：
1. 查看节点 CPU 使用率监控，确认持续超过 90%
2. 查看 Load Average，确认显著升高
3. 确认应用 A 的请求延迟增大

**注入恢复**：
1. 销毁 chaosblade 实验

**恢复验证**：
1. 查看节点 CPU 使用率监控，确认恢复到正常水平
2. 确认应用 A 的请求延迟恢复正常

**基准事实**：
- **根因**：节点上存在异常进程大量占用 CPU，导致节点 CPU 使用率过高，影响同节点上所有 Pod 的性能
- **必现现象**：节点 CPU 使用率持续超过 90%；Load Average 显著升高；同节点 Pod 响应变慢

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；debug 镜像需包含 `stress-ng` 或有基本 shell

注入命令：
```bash
# 使用 kubectl debug node 注入 CPU 压力
kubectl debug node/<node-name> -it --image=alpine -- sh -c 'stress-ng --cpu 0 --cpu-load <percent> --timeout <duration>s'
# 如果无 stress-ng 镜像，使用 busybox 模拟：
kubectl debug node/<node-name> -it --image=busybox -- sh -c 'while true; do :; done &'
```

恢复命令：
```bash
# 退出 debug Pod（Ctrl+D），或直接删除 debug Pod：
kubectl delete pod <debug-pod-name> --force --grace-period=0
# stress-ng 会在 --timeout 到期后自动退出
```

注意事项：
- debug Pod 运行在宿主机 PID namespace 中，CPU 压力会直接影响节点
- busybox 循环方式只能实现单核满载，多核场景需启动多个后台进程
- 与 ChaosBlade 相比，kubectl debug node 方式需手动管理 debug Pod 生命周期
