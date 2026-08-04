**用例名称** 网络请求延迟 导致 Pod_网络延迟

**故障现象**：
1. Pod 所有出方向网络请求增加固定延迟（如 1000ms）
2. 应用调用上下游服务超时或响应显著变慢
3. 健康检查可能因超时失败，导致 Pod 被重启
4. 模拟跨地域部署/弱网环境/网络拥塞场景

**资源准备**：
1. 确认目标 Pod 正常运行，且有可用的 **iproute2** `tc`（见演练步骤 1 —— 精简镜像里常有同名的
   BusyBox applet，它不支持 netem；若无可用 tc，走演练步骤 2 的路径 B）
2. 确认目标 Pod 有对外网络调用（上下游服务、数据库等）
3. 确认目标 Pod 名称和命名空间
4. 确认目标节点内核支持 netem（**内核级依赖，路径 A/B 都绕不开**）：netem 由宿主机内核的 sch_netem 模块提供，容器与宿主共享内核，换 Pod / 换临时容器都改变不了。只读探查：`kubectl exec <pod-name> -n <namespace> -- grep sch_netem /proc/modules`（临时容器载体同样可用）——有输出说明已加载；无输出**不能**判定不可行（注入时内核可能自动加载模块），记为待 Phase 2 验证的假设。**判据以注入输出为准**：注入报 `RTNETLINK answers: Operation not supported` 即为内核不支持 netem 的确证，见演练步骤 2

**演练步骤**：
1. 确认目标 Pod 运行状态，并判定容器内的 `tc` 是不是真的能用 —— **`which tc` / `command -v tc`
   会误判**，精简镜像里 `/bin/tc` 常与 `/bin/sh` 是同一个 BusyBox 二进制，名字在但不支持 netem：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -- tc -Version
   ```
   - 输出 `tc utility, iproute2-<版本>` → 真 tc，走路径 A
   - 输出 `BusyBox v<版本> ...` 或命令不存在 → 走路径 B
     （BusyBox 版执行 `tc qdisc add ... root netem` 会报 `invalid argument 'root' to 'command'`）
2. 注入网络延迟，按上一步结论二选一。本用例为 kubectl-native 方案，选用前提是 ChaosBlade 的
   `pod-network` 没有 delay action；以 `blade create k8s pod-network --help` 实测为准，
   若本地版本已提供则优先用 blade 方案。

   **路径 A —— 容器内确认是 iproute2 tc**（需 NET_ADMIN；`CapEff` 全零的容器会报 EPERM）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- \
     tc qdisc add dev eth0 root netem delay 1000ms
   ```

   **路径 B —— 容器内没有可用 tc（精简镜像的常态）**：用临时容器注入。临时容器与目标容器
   **共享同一个网络命名空间**，对 `eth0` 操作等价于操作目标 Pod 的网卡；`tc` 来自调试镜像，
   `--profile=netadmin` 提供 NET_ADMIN capability：
   ```bash
   # 1) 先建一个【长驻】临时容器作为执行载体。必须用 `sleep` 保活 ——
   #    若直接把 tc 命令交给 kubectl debug，命令跑完容器立即终止，
   #    后续 `kubectl exec -c <debugger>` 会报 `container not found`，故障就没法恢复了。
   # 0) 前置安全检查：确认目标 Pod 不是 hostNetwork。hostNetwork=true 的 Pod
   #    其网络命名空间【就是宿主机】，临时容器里的 tc 会打穿整个节点，
   #    爆炸半径从单 Pod 扩大到整台机器。为 true 时禁止此路径，改用 node 级用例。
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.hostNetwork}'
   # 期望输出为空或 false；输出 true 则停止。

   kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
     --target=<container-name> --profile=netadmin --quiet -- sleep <duration>

   # 2) 取载体名（等它进入 running 再继续）
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}{"="}{.state}{"\n"}{end}'

   # 3) 经载体注入。载体与目标容器共享同一个网络命名空间，操作 eth0 即操作目标 Pod 的网卡；
   #    tc 来自调试镜像，--profile=netadmin 提供 NET_ADMIN capability
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc add dev eth0 root netem delay 1000ms
   ```
   - `<verified-cluster-image>`：必须是当前集群**已验证可拉取**且含 **iproute2**（非 BusyBox）的镜像。
     可靠的找法是看集群里已经在跑的镜像 —— 它们必然可拉取：
     `kubectl get pods -A -o jsonpath='{{..image}}'`。
     CNI / 网络组件（terway、calico、cilium 等）通常自带 iproute2，因为它们本身就要做流量整形；
     选定后用 `kubectl debug ... -- tc -Version` 确认输出是 `tc utility, iproute2-...`
   - `--quiet`：不进入交互附着；**不要加 `-it`**

   参数说明（两条路径相同）：
   - `delay 1000ms`：固定延迟 1000 毫秒
   - 可附加抖动：`delay 1000ms 200ms`（1000±200ms）
   - `dev eth0`：通常为 Pod 主网卡，部分环境为 `eth0` 以外名称
   - **内核级依赖（两条路径相同）**：netem 需要宿主机内核支持 sch_netem。若注入报
     `RTNETLINK answers: Operation not supported`，即内核不支持 netem 的确证 —— 立即停止，
     **不要重试、不要换 Pod 或重建临时容器**（内核是同一个，重试只是空转），发起 replan
     并附上该报错证据，由 Phase 1 改选其他可行方案或判定不可行
3. 观察应用响应时间变化

**注入验证**：
1. 确认 tc 规则已生效（**用注入时同一条路径查**，`tc qdisc show` 同样需要真 tc）：
   ```bash
   # 路径 A
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   # 路径 B（复用注入时创建的临时容器）
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc show dev eth0
   ```
   输出应包含 `netem delay 1000ms`
2. 在 Pod 内验证延迟效果。**不要用 `ping`** —— 它需要 `CAP_NET_RAW`，非 root 容器
   （`CapEff` 全零）执行会返回 `ping: permission denied (are you root?)`；更糟的是若接了管道
   （如 `| tail`），退出码来自管道末端，会**看起来成功**。改用 TCP 层的探测：
   ```bash
   # 把 --timeout 设在注入延迟的两侧，用「成功→超时」的翻转作为判据。
   # 注入 1000ms 延迟后，原本 <100ms 的请求会超过 1s：
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=1 --tries=1 <依赖服务地址>
   ```
   注入生效的判据：该命令由「正常返回」变为「超时失败」。放宽到 `--timeout=5` 应仍能成功，
   以此区分「延迟」和「完全不通」。
3. 量化延迟毫秒数（比步骤 2 的超时翻转更精确）：
   ```bash
   # 输出丢进 /dev/null 只取耗时；两条命令按容器内可用的那个选
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- \
     curl -s -o /dev/null -w '%{time_total}' --max-time 10 <依赖服务地址>
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- \
     wget -O /dev/null --timeout=10 <依赖服务地址>
   ```
   `time_total` 应比基线高出约注入的延迟值。**不要接管道或重定向**
   （`| head`、`2>&1`）——只读探针会拒绝 shell 操作符，且 exec-form 下它们会被
   当成字面参数传给命令。响应头用 `wget -S` 直接看即可，无需 `2>&1`。
4. 检查应用日志是否出现 timeout 或 slow response 相关错误

**注入恢复**：
1. 删除 tc netem 规则（不使用 blade destroy，这是 kubectl-native 方案）——
   **必须用注入时那条路径**，`tc qdisc del` 同样需要真 tc：
   ```bash
   # 路径 A
   kubectl exec <pod-name> -n <namespace> -- tc qdisc del dev eth0 root
   # 路径 B —— 复用同一个临时容器，不要新建
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc del dev eth0 root
   ```
   临时容器名遗失时用
   `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.ephemeralContainerStatuses[*].name}'`
   取回。
2. 如 Pod 因健康检查超时被重启，tc 规则自动消失（不持久化），无需额外操作
3. 走过路径 B 的话：**临时容器无法从运行中的 Pod 移除**（Kubernetes 既定行为），只能随 Pod 重建消失。
   `tc qdisc del` 成功即代表故障已恢复，残留的临时容器不影响业务容器。如需立即清理须删除该 Pod
   让上层控制器重建 —— 这是额外的变更动作，须经确认后再做。

**恢复验证**：
1. 确认 tc 规则已清除（**与注入/恢复同一条路径**）：
   ```bash
   # 路径 A
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   # 路径 B（复用注入时那个临时容器）
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc show dev eth0
   ```
   输出应不再包含 netem 规则
2. 在 Pod 内验证延迟恢复正常：
   ```bash
   # 同样避开 ping（需 CAP_NET_RAW）——用 TCP 探测
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=1 --tries=1 <依赖服务地址>
   ```
   确认 RTT 恢复到正常水平
3. 确认应用日志不再出现超时错误

**基准事实**：
- **根因**：通过 tc netem 在 Pod 网卡注入出方向固定延迟，模拟弱网/跨地域网络环境
- **必现现象**：Pod 内对外 TCP 请求耗时增加约 1000ms（`--timeout=1` 的 wget 由成功转为超时）；`tc qdisc show` 显示 netem delay 规则；应用对外请求响应时间显著增加
