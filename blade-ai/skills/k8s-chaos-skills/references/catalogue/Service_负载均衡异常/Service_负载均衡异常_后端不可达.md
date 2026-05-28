**用例名称** 后端不可达 导致 Service_负载均衡异常

**故障现象**：
1. Service 的 Endpoints 列表为空或部分后端不可用
2. 请求到 Service 出现 5xx 错误或连接超时
3. Ingress 后端健康检查失败

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service 和 Ingress
2. 确认监控系统可观测 Service 请求指标和 Endpoints 状态

**演练步骤**：
1. 定位应用 A 的 Service 和后端 Pod
2. 使用 chaosblade 对应用 A 的 Pod 注入故障，模拟后端不可达

**注入方式选择（重要）**：
- **推荐优先**：使用 `pod-process kill` 杀死后端 Pod 的主进程。优点：直接导致 Pod 重启或进入 CrashLoopBackOff，Endpoints 控制器会自动将其从 Endpoints 列表移除，故障效果明确且可观测
- **备选方式**：使用 `pod-network drop --percent 100` 断开 Pod 网络。注意：如果主机 blade 二进制与集群不兼容，会退化为 kubectl exec 方式注入，导致恢复阶段必须通过 kubectl exec 执行 blade destroy，增加恢复复杂度
  - 如果目标是单个服务端口（如 MySQL 3306），推荐使用端口过滤以缩小爆炸半径：`pod-network drop --percent 100 --local-port 3306 --namespace <ns> --labels "app=<app>"`
  - 如果需要完全断开 Pod 网络：`pod-network drop --percent 100 --interface eth0 --namespace <ns> --labels "app=<app>"`，注意此方式会影响所有端口，包括 DNS 和监控
- 选择原则：如果集群中已部署 ChaosBlade Operator 且主机 blade 可直接使用，两种方式均可；如果需要通过 kubectl exec 注入，优先选择 pod-process kill

**Readiness Probe 兼容性**（选择注入方式前必须评估）：
- 通过 `kubectl describe pod <pod>` 获取目标 Pod 的 Readiness Probe 类型
- `exec` 类型 Probe：在容器内通过 localhost 执行，**不受** pod-network drop 的 tc 规则影响 → Pod 保持 Ready → Endpoints 不会移除
- `httpGet/tcpSocket` 类型 Probe（端口在 Service 端口范围内）：**可能受** pod-network drop 影响 → 延迟后 Pod 变为 NotReady → Endpoints 会被移除
- 选择原则：如果目标是"Endpoints 移除"，exec 类型 Probe 的 Pod 应使用 pod-process kill 而非 pod-network drop

**注入验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认部分后端被移除或全不可用
2. 向 Service 发送请求，确认出现 5xx 错误或连接超时
3. 查看 Ingress 状态，确认后端健康检查失败
4. 确认请求流量被调度到剩余可用后端（部分后端不可用时）

**注入恢复**：
1. 销毁 chaosblade 实验

**恢复验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认所有后端恢复可用
2. 向 Service 发送请求，确认恢复正常
3. 查看 Ingress 状态，确认后端健康检查通过

**基准事实**：
- **根因**：Service 后端 Pod 异常或网络不通，导致负载均衡无法将请求转发到健康的后端，服务可用性下降
- **必现现象**：Endpoints 列表部分为空或全部为空；请求出现 5xx 或超时；Ingress 后端健康检查失败
