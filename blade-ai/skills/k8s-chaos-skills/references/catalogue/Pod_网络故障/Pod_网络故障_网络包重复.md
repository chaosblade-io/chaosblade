**⚠️ 注意：此场景为 kubectl-native 方案，通过 tc netem 实现。tc 从哪来有两条路径，见演练步骤 3-4 —— 目标容器自带的 `tc` 在精简镜像里通常是不支持 netem 的 BusyBox applet，此时必须走临时容器路径。**
**选此方案的前提是 ChaosBlade 的 `pod-network` 没有 duplicate action —— 以 `blade create k8s pod-network --help` 的实际输出为准；若本地版本已提供，优先用 blade 方案并以 `blade destroy <UID>` 恢复。**

**用例名称** 网络包重复 导致 Pod_网络故障

**故障现象**：
1. 网络带宽消耗异常增加，重复包占用额外带宽
2. 应用层可能收到重复消息，考验业务幂等性设计
3. 网络延迟略有上升，TCP 层需额外 CPU 处理去重
4. 监控系统显示网络接收包数异常偏高

**资源准备**：
1. 确认目标应用已正常运行，且有活跃的网络通信流量
2. 确认目标 Pod 的标签选择器和命名空间
3. 确认监控系统可观测网络流量和包计数指标
4. 确认有可用的 **iproute2** `tc`（见演练步骤 3 —— 精简镜像里常有同名的 BusyBox applet，它不支持 netem）
5. 若需走临时容器路径：先确认当前集群**能拉取**一个含 iproute2 的镜像。不要假定公网镜像可用 —— 内网/离线集群常拉不到 Docker Hub，需换成集群已在使用的仓库地址

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 记录注入前的网络包统计基线：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```
3. 判定容器内的 `tc` 是不是真的能用 —— **只看命令是否存在会误判**：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc -Version
   ```
   - 输出 `tc utility, iproute2-<版本>` → 是真 tc，可走路径 A
   - 输出 `BusyBox v<版本> ...` → 是 BusyBox applet，**不支持 netem**（执行 `tc qdisc add ... root netem` 会报
     `tc: invalid argument 'root' to 'command'`）。精简镜像里 `/bin/tc` 常与 `/bin/sh` 是同一个 BusyBox
     二进制，`command -v tc` 一样返回成功，所以必须看 `-Version` 的输出内容，走路径 B
   - 命令不存在 → 走路径 B

4. 注入网络包重复故障，按上一步结论二选一。

   **路径 A —— 容器内确认是 iproute2 tc**（需容器有 NET_ADMIN；`CapEff` 全零的容器会报 EPERM）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- tc qdisc add dev eth0 root netem duplicate 30%
   ```

   **路径 B —— 容器内没有可用 tc（精简镜像的常态）**：用临时容器注入。临时容器与目标容器
   **共享同一个网络命名空间**，所以在它里面对 `eth0` 操作等价于对目标 Pod 的网卡操作；
   `tc` 来自调试镜像而非目标镜像，`--profile=netadmin` 提供 NET_ADMIN capability：
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
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc add dev eth0 root netem duplicate 30%
   ```
   - `<verified-cluster-image>`：必须是当前集群**已验证可拉取**且含 **iproute2**（非 BusyBox）的镜像。
     可靠的找法是看集群里已经在跑的镜像 —— 它们必然可拉取：
     `kubectl get pods -A -o jsonpath='{{..image}}'`。
     CNI / 网络组件（terway、calico、cilium 等）通常自带 iproute2，因为它们本身就要做流量整形；
     选定后用 `kubectl debug ... -- tc -Version` 确认输出是 `tc utility, iproute2-...`
   - `--quiet`：不进入交互附着；**不要加 `-it`**

   参数含义（两条路径相同）：
   - `duplicate 30%`：约 30% 的出站数据包会被复制一份重新发送
   - `eth0`：网络接口名称，根据实际情况调整（可通过 `ip link show` 确认）
   - 原理：tc netem 的 duplicate 选项对出站包进行复制，接收端会收到重复数据包

5. 确认 tc 规则已生效（**用注入时同一条路径查**，因为查询也需要真 tc）：
   ```bash
   # 路径 A
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   # 路径 B（复用注入时创建的临时容器）
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc show dev eth0
   ```
   应显示 `qdisc netem ... duplicate 30%`

**注入验证**：
1. 在目标 Pod 内查看网络接口统计，确认发送包数异常增高：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```
   与基线对比，TX packets 增长速率应明显高于正常水平
2. 在目标 Pod 内验证网络连通性（TCP 层自动去重，连接仍可用）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <目标服务地址>
   ```
   确认请求仍可成功，但响应时间可能略有增加
3. 查看应用日志确认是否出现重复消息处理记录：
   ```bash
   kubectl logs <pod-name> -n <namespace> --tail=30
   ```
4. 确认网络监控显示出站流量异常增加约 30%

**注入恢复**：
1. 移除 tc netem 规则 —— **必须用注入时那条路径**，因为 `tc qdisc del` 同样需要真 tc：
   ```bash
   # 路径 A（注入时用的是容器自带 iproute2 tc）
   kubectl exec <pod-name> -n <namespace> -- tc qdisc del dev eth0 root
   # 路径 B（注入时用的是临时容器）—— 复用同一个临时容器，不要新建
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc del dev eth0 root
   ```
   若临时容器名已丢失，用
   `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.ephemeralContainerStatuses[*].name}'`
   取回。
2. 确认规则已移除：
   ```bash
   # 与上一步同一条路径
   kubectl exec <pod-name> -n <namespace> [-c <debugger-name>] -- tc qdisc show dev eth0
   ```
   应恢复为默认 qdisc（如 `pfifo_fast` / `fq_codel` / `noqueue`），不再显示 netem
3. **临时容器本身无法从运行中的 Pod 移除**（Kubernetes 的既定行为），只能随 Pod 重建消失。
   `tc qdisc del` 成功即代表故障已恢复；残留的临时容器不影响业务容器，可留待 Pod 下次重建时清除。
   如需立即清理，须删除该 Pod 让上层控制器重建 —— 这是额外的变更动作，须经确认后再做。

**恢复验证**：
1. 在目标 Pod 内确认网络包统计恢复正常增长速率：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```
2. 确认出站流量恢复至基线水平
3. 确认应用响应时间和吞吐量恢复正常

**基准事实**：
- **根因**：Pod 网络接口上约 30% 的出站数据包被 tc netem duplicate 复制重发，导致接收端收到重复包，占用额外带宽和处理资源
- **必现现象**：出站 TX packets 增长率异常偏高（约 1.3 倍）；网络带宽占用增加；TCP 层自动去重但消耗额外 CPU；应用层若无幂等保护可能处理重复业务消息
- **方案说明**：此为 kubectl-native 方案（选用前提：`pod-network` 未提供 duplicate action，以 `--help` 实测为准）。恢复不使用 blade destroy，而是通过 `tc qdisc del dev eth0 root` 移除规则，且必须沿注入时那条路径执行
- **tc 来源决定成败**：netem 只有 iproute2 的 tc 支持。目标容器里同名的 BusyBox applet 会以 `invalid argument 'root' to 'command'` 失败，而 `command -v tc` 检测不出这个差别 —— 判据是 `tc -Version` 的输出内容。临时容器与目标容器共享网络命名空间，因此用调试镜像的 tc 操作 `eth0` 等价于操作目标 Pod 的网卡，这条路径不依赖目标镜像里有什么，也不依赖集群安装 ChaosBlade
