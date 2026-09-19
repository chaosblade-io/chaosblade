**用例名称** 后端不可达 导致 Service_负载均衡异常

**故障定位**：持续型故障——故障窗口内后端 Pod 网络持续中断，Service 负载均衡
持续无法转发请求。**本用例不提供一次性注入**：单次杀进程/单次删 Pod 随 kubelet/
控制器重建一次即自愈（15-90s），不构成有效演练窗口；一次性 kill/delete 形态已被
废除（上游取证：ChaosBlade kill 类 action 仅在实验创建时发送一次信号）。
`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

**故障现象**：
1. Service 的 Endpoints 列表为空或部分后端不可用
2. 请求到 Service 出现 5xx 错误或连接超时
3. Ingress 后端健康检查失败

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service 和 Ingress
2. 确认监控系统可观测 Service 请求指标和 Endpoints 状态
3. 确认 `duration_seconds`（故障窗口）已明确
4. **Readiness Probe 兼容性评估（必做）**：通过 `kubectl describe pod <pod>` 获取
   目标 Pod 的 Readiness Probe 类型——`exec` 类型 Probe 在容器内通过 localhost 执行，
   **不受**网络 drop 的 tc 规则影响 → Pod 保持 Ready → Endpoints 不会移除；
   `httpGet/tcpSocket` 类型 Probe（端口在 Service 端口范围内）会受影响 → 延迟后
   Pod 变为 NotReady → Endpoints 被移除。目标是「Endpoints 移除」而 Probe 为 exec
   类型时，本用例的网络形态判据不可达，改走 Pod_进程被杀死 用例（节点侧持续停容器
   循环）制造后端重启风暴，如实报告并请用户确认

**演练步骤**：
1. 定位应用 A 的 Service 和后端 Pod
2. 注入 —— `pod-network drop` 断开 Pod 网络（**唯一形态**：tc/netem 规则是状态型
   故障，规则本身贯穿整个故障窗口，`--timeout` 到期 destroy 即恢复）：
   ```bash
   blade create k8s pod-network drop \
     --namespace <namespace> \
     --labels "app=<app>" \
     --timeout <duration>
   ```
   - 目标是单个服务端口（如 MySQL 3306）时用端口过滤缩小爆炸半径：
     追加 `--source-port 3306`
   - 不加端口过滤则完全断开 Pod 网络——影响所有端口，包括 DNS 和监控，
     爆炸半径评估时按此计
   - 记录返回的 experiment_uid，用于恢复
3. 观察 Service 访问与 Endpoints 变化

**注入验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认部分后端被移除或全不可用
   （exec 型 Probe 的 Pod 不会被移除，见资源准备第 4 条的判据约束）
2. 向 Service 发送请求，确认出现 5xx 错误或连接超时
3. （仅当服务经 Ingress 暴露时）查看 Ingress 状态，确认后端健康检查失败
4. 确认请求流量被调度到剩余可用后端（部分后端不可用时）
5. **持续性检查（必做）**——网络 drop 是状态型故障，tc 规则存活即故障存活。本步证明的是持续性，不是效果存在——效果已由第 1-4 步证明；对持续性命题，机制状态就是直接证据：
   **白盒主证（即时，单独充分）**：实验仍在（未 destroy 且未到 `--timeout`）——机制存活即"持续 drop"由构造成立，本步即完成，无需佐证窗口；**有界佐证（仅当实验状态不可查时的回退）**：静观短窗口后向 Service 发请求仍超时/5xx。若窗口内提前恢复，说明故障窗口契约未达成，必须如实报告实际持续时长

**注入恢复**：
1. 等待 `--timeout` 到期实验自动销毁（主保险）
2. 提前恢复：销毁 chaosblade 实验 `blade destroy <experiment_uid>`

**恢复验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认所有后端恢复可用
2. 向 Service 发送请求，确认恢复正常
3. （仅当服务经 Ingress 暴露时）查看 Ingress 状态，确认后端健康检查通过

**基准事实**：
- **根因**：Service 后端 Pod 网络在窗口内持续中断，负载均衡无法将请求转发到健康的后端，服务可用性下降
- **必现现象**：请求出现 5xx 或超时贯穿窗口；httpGet/tcpSocket Probe 的后端从 Endpoints 移除；窗口结束 destroy 后恢复

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效后端不可达。
> 一次性形态（容器内 `kill 1`、`kubectl delete pod`）已被本用例废除：只换来一次
> 重启/重建，随 kubelet/控制器自愈即消失，不构成持续故障窗口。持续形态只有网络
> 规则类（状态贯穿窗口）。

前提条件：容器内有 `iptables` 且有 NET_ADMIN，或集群可拉取含 iproute2 的调试镜像。
精简镜像（BusyBox/distroless）通常两者都不满足，注入前先验证。

注入命令：
```bash
# 方式B：注入网络丢包（需容器内真有 iptables 且有 NET_ADMIN，先验证：
#         kubectl exec <pod-name> -n <namespace> -- sh -c 'command -v iptables || echo NO_IPTABLES'）
#         先武装定时 -D 再 -A，到期自动恢复。两条命令分两次独立执行——不能用 && 串联：
#         第二段 kubectl 会沦为第一条 exec 载荷（sh -c）的死参数，注入静默丢失
kubectl exec <pod-name> -n <namespace> -- sh -c \
  '( sleep <duration>; iptables -D OUTPUT -j DROP ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -n <namespace> -- iptables -A OUTPUT -j DROP
# 方式B'：容器内无 iptables 时，用临时容器 + tc。
#    临时容器与目标容器共享同一个网络命名空间，工具来自调试镜像，
#    --profile=netadmin 提供 NET_ADMIN。先建【长驻】临时容器作为载体 ——
#    必须 sleep 保活；若把 tc 直接交给 kubectl debug，命令跑完容器即终止，
#    后续 `kubectl exec -c <debugger>` 会报 container not found，故障将无法恢复。
# 0) 前置安全检查：确认目标 Pod 不是 hostNetwork。hostNetwork=true 的 Pod
#    其网络命名空间【就是宿主机】，临时容器里的 tc 会打穿整个节点，
#    爆炸半径从单 Pod 扩大到整台机器。为 true 时禁止此路径，改用 node 级用例。
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.hostNetwork}'
# 期望输出为空或 false；输出 true 则停止。

kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
  --target=<container-name> --profile=netadmin --quiet -- sleep <duration>

# 取载体名，等它进入 running
kubectl get pod <pod-name> -n <namespace> \
  -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}{"="}{.state}{"\n"}{end}'

# 经载体注入：载体与目标容器共享网络命名空间，操作 eth0 即操作目标 Pod 的网卡。
# 同样先武装定时自删（在载体内后台运行；载体保活 sleep 必须 ≥ <duration>）。
# 两条命令分两次独立执行——不能用 && 串联（第二段会沦为第一条 exec 载荷的死参数）
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- sh -c \
  '( sleep <duration>; tc qdisc del dev eth0 root ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc add dev eth0 root netem loss 100%
```
方式B/B' 倒计时均从武装时刻起算：武装与注入两条命令必须紧邻连续下发（≤60s）；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec <pod-name> -n <namespace> [-c <debugger-name>] -- sh -c 'pkill -f "iptables -[D]|qdisc de[l]"; true'`（exec 目标必须与武装时同一容器）；精简镜像无 pkill 时旧定时器无法停止，到期会提前恢复侵蚀故障窗口——须中止演练改人工恢复或如实上报缩短的窗口（见 SKILL.md 安全红线「故障窗口完整」）

恢复命令：
```bash
# 方式B：删除 iptables 规则（与注入逐字对应，不要用 iptables -F）
kubectl exec <pod-name> -n <namespace> -- iptables -D OUTPUT -j DROP
# 方式B'：复用注入时那个临时容器，不要新建
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc del dev eth0 root
```

注意事项：
- 方式B（iptables）与方式B'（tc）均为状态型故障，规则存活期间故障持续
- 方式B 依赖容器内有 `iptables`，精简镜像（BusyBox/distroless）通常没有
  （BusyBox 报 NO_IPTABLES）；此时用方式B'，但要先确认集群能拉取含
  iproute2 的镜像。**临时容器无法从运行中的 Pod 移除**，只能随 Pod 重建消失；
  `tc qdisc del` 成功即代表故障已恢复
- exec 类型 Readiness Probe 的 Pod 网络 drop 后仍保持 Ready，Endpoints 不移除——
  该组合下本用例的「Endpoints 移除」判据不可达（流量超时判据仍成立），需
  Endpoints 移除时改走 Pod_进程被杀死 用例的持续形态
