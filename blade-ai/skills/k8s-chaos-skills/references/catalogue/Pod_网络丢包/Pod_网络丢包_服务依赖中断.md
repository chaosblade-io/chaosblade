**用例名称** 服务依赖中断 导致 Pod_网络丢包

**故障现象**：
1. Pod 对外部服务或上下游依赖的网络请求超时或无响应
2. 应用健康检查可能失败（如依赖外部探活）
3. 服务间调用链路出现断裂，影响业务可用性

**资源准备**：
1. 确认目标应用已正常运行，且有对外网络调用（数据库、缓存、上下游服务等）
2. 确认监控系统可观测网络请求成功率和延迟指标
3. 确认目标 Pod 的标签选择器和命名空间
4. 若走 手段2（kubectl-native）的 tc netem 路径：确认目标节点内核支持 netem（**内核级依赖，路径 A/B 都绕不开**）。netem 由宿主机内核的 sch_netem 模块提供，容器与宿主共享内核，换 Pod / 换临时容器都改变不了。只读探查：`kubectl exec <pod-name> -n <namespace> -- grep sch_netem /proc/modules`——有输出说明已加载；无输出时可用更强的前置确证：经 node debug 载体执行 `chroot /host modprobe sch_netem` 试载，报 `FATAL: Module sch_netem not found` 即模块文件本身缺失（内核自动加载不可能成功），**注入前即可定案不可行**；部分 ACK/ASI al8 内核（5.10.134-13.1.al8）即为此形态——同一节点 netem 全家（loss/delay/corrupt）全部不可行，而 sch_tbf 存在（带宽受限场景可用，见 `Pod_网络带宽不足_带宽受限`）。**判据以注入输出为准**：注入报 `RTNETLINK answers: Operation not supported`、`RTNETLINK answers: No such file or directory`（后者为节点上 sch_netem 模块文件本身缺失、内核自动加载失败）或 `Error: Specified qdisc kind is unknown.`（RC=2，另一种报错形态）即为内核不支持 netem 的确证

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 使用 ChaosBlade 对目标 Pod 注入网络丢包故障（**必须带 `--timeout`**，到期实验自动结束并
   移除丢包规则，补齐自恢复能力；漏掉该参数实验将无限持续，只能靠 blade destroy 手动恢复）：
   ```bash
   blade create k8s pod-network drop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --source-port <port> \
     --timeout <duration>

   ```
   - `--source-port`：限定丢包端口（如 3306 丢弃 MySQL 流量、53 丢弃 DNS 流量）
   - 不指定端口时为全量丢包（慎用，影响所有流量包括监控和健康检查）
3. 记录返回的 experiment_uid，用于后续恢复

**注入验证**：
1. 在目标 Pod 内验证网络连通性丧失：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <依赖服务地址>
   ```
   确认请求超时或无响应
2. 查看应用日志确认出现连接超时错误：
   ```bash
   kubectl logs <pod-name> -n <namespace> --tail=20
   ```
3. （可选，仅当有监控平台访问能力时）确认目标端口流量中断；无平台时上述连通性探针与日志证据成立即可判定

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <experiment_uid>
   ```
2. 如 Pod 因丢包导致健康检查失败被重启，等待新 Pod Ready

**恢复验证**：
1. 在目标 Pod 内重新验证网络连通性恢复：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <依赖服务地址>
   ```
2. 确认应用日志不再出现连接超时错误
3. （可选，有监控平台访问能力时）确认调用链路指标恢复正常

**基准事实**：
- **根因**：Pod 出方向网络流量被 iptables DROP 规则丢弃，导致对指定端口/地址的所有请求无响应
- **必现现象**：目标端口的 TCP/UDP 请求超时；应用日志出现 connection timed out；依赖该连接的业务功能不可用

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，用以下 kubectl 原生命令实现等效网络丢包注入。
> **工具选择很关键**：`tc netem loss <percent>%` 是唯一能做出「按百分比丢包」的手段；
> `iptables -j DROP` 只能全丢或按端口全丢（二元），做不出 30% 这类比例。所以主路径用 tc。

前提条件：需要一份可用的 **iproute2** `tc`。它可能来自目标容器，也可能来自临时容器 —— 先判定：

```bash
kubectl exec <pod-name> -n <namespace> -- tc -Version
```
- 输出 `tc utility, iproute2-<版本>` → 真 tc，可走路径 A
- 输出 `BusyBox v<版本> ...` 或命令不存在 → 走路径 B。**注意 `which tc` / `command -v tc` 会误判**：
  精简镜像里 `/bin/tc` 常与 `/bin/sh` 是同一个 BusyBox 二进制，名字在但不支持 netem，
  执行时报 `invalid argument 'root' to 'command'`

注入命令（**先武装定时自删，再注入规则**——补齐 ChaosBlade `--timeout` 的自恢复能力）：

```bash
# ── 路径 A：容器内确认是 iproute2 tc，且有 NET_ADMIN（CapEff 全零的容器会报 EPERM）
# 先武装定时自删（后台进程与目标容器同 netns，到期自动移除规则；必须重定向后台化）。
# 两条命令分两次独立执行——不能用 && 串联：第二段 kubectl 会沦为第一条 exec
# 载荷（sh -c）的死参数，注入静默丢失
kubectl exec <pod-name> -n <namespace> -- sh -c \
  '( sleep <duration>; tc qdisc del dev eth0 root ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -n <namespace> -- tc qdisc add dev eth0 root netem loss <percent>%

# ── 路径 B：容器内无可用 tc（精简镜像的常态）。临时容器与目标容器共享网络命名空间，
#    对 eth0 操作等价于操作目标 Pod 的网卡；tc 来自调试镜像，--profile=netadmin 给 NET_ADMIN
# ── 路径 B：容器内无可用工具（精简镜像的常态）。
#    先建【长驻】临时容器作为载体 —— 必须 sleep 保活；若把 tc 直接交给 kubectl debug，
#    命令跑完容器即终止，后续 `kubectl exec -c <debugger>` 会报 container not found，
#    故障将无法恢复。
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
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc add dev eth0 root netem loss <percent>%
```
各路径倒计时均从武装时刻起算：武装与注入两条命令必须紧邻连续下发（≤60s）；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec <pod-name> -n <namespace> [-c <debugger-name>] -- sh -c 'pkill -f "qdisc de[l]"; true'`（exec 目标必须与武装时同一容器）；精简镜像无 pkill 时旧定时器无法停止，到期会提前恢复侵蚀故障窗口——须中止演练改人工恢复或如实上报缩短的窗口（见 SKILL.md 安全红线「故障窗口完整」）
- `<verified-cluster-image>`：当前集群**已验证可拉取**且含 iproute2 的镜像。先看集群在用哪些仓库
  （`kubectl get pods -A -o jsonpath='{..image}'`）并从同仓库取；拉不动时 Pod 事件里会出现
  `ErrImagePull` / `ImagePullBackOff`
- `--quiet`：不进入交互附着；**不要加 `-it`**
- 载体名形如 `debugger-xxxxx`，注入/验证/恢复三步都要用同一个
- **内核级依赖（两条路径相同）**：netem 需要宿主机内核支持 sch_netem。若注入报
  `RTNETLINK answers: Operation not supported`、`RTNETLINK answers: No such file or directory`
  （模块文件缺失）或 `Error: Specified qdisc kind is unknown.`（RC=2，另一实际形态），
  即内核不支持 netem 的确证 —— 立即停止，
  **不要重试、不要换 Pod 或重建临时容器**（内核是同一个，重试只是空转），发起 replan
  并附上该报错证据，改选其他可行方案（如 iptables 全丢）或判定不可行

只需要「完全断开某个依赖」而非按比例丢包时，可用 iptables（需容器内真有 `iptables`，
精简镜像通常没有；节点上一般有，但那要走 node 级用例）——同样先武装定时 `-D` 再 `-A`：
```bash
# 两条命令分两次独立执行（&& 串联会使第二段沦为第一条 exec 载荷的死参数）：
kubectl exec <pod-name> -n <namespace> -- sh -c \
  '( sleep <duration>; iptables -D OUTPUT -p tcp --dport <port> -j DROP ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -n <namespace> -- iptables -A OUTPUT -p tcp --dport <port> -j DROP
```
倒计时从武装时刻起算：武装与注入两条命令必须紧邻连续下发（≤60s）；武装后发生任何修复须先 `kubectl exec <pod-name> -n <namespace> -- sh -c 'pkill -f "iptables -[D]"; true'` 停旧定时器再全额重武装；容器无 pkill 时旧定时器无法停止，到期会提前恢复侵蚀故障窗口——须中止演练改人工恢复或如实上报缩短的窗口（见 SKILL.md 安全红线「故障窗口完整」）

恢复命令：

```bash
# 与注入同一条路径 —— tc qdisc del 同样需要真 tc
# 路径 A
kubectl exec <pod-name> -n <namespace> -- tc qdisc del dev eth0 root
# 路径 B（复用注入时那个临时容器，不要新建）
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc del dev eth0 root

# 若注入用的是 iptables，按注入命令逐字对应删除
kubectl exec <pod-name> -n <namespace> -- iptables -D OUTPUT -p tcp --dport <port> -j DROP
```
临时容器名遗失时用
`kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.ephemeralContainerStatuses[*].name}'`
取回。

注意事项：
- 按百分比丢包只有 `tc netem loss` 能做，`iptables -j DROP` 是二元的，两者不可互相替代
- 全量丢包（`iptables -A OUTPUT -j DROP` 或 `netem loss 100%`）会切断监控和健康检查，
  可能触发 Pod 重启，建议用端口级或较低百分比
- 自恢复基于注入前武装的后台定时器（`sleep <duration>` + 逆操作），到期自动删除规则；
  提前恢复仍用下方手动命令。Pod 重启也会让 tc 规则自动消失（不持久化）
- 恢复 iptables 用 `-D` 逐字对应删除，不要用 `iptables -F`——那会清掉容器原有的其他规则
- 走过路径 B 的话：**临时容器无法从运行中的 Pod 移除**（Kubernetes 既定行为），只能随 Pod 重建消失。
  `tc qdisc del` 成功即代表故障已恢复，残留容器不影响业务容器；如需立即清理须删除该 Pod
  让上层控制器重建 —— 这是额外的变更动作，须经确认后再做
