**用例名称** 带宽受限 导致 Pod_网络带宽不足

**故障现象**：
1. Pod 出方向网络吞吐被限制到设定速率（如 1mbit），大数据量传输显著变慢
2. 小请求仍能正常返回（限速不影响单包时延），但批量拉取/上传耗时成倍增加
3. 应用出现「慢而不断」的症状：健康检查通过、但数据同步/镜像拉取/日志上报堆积
4. 模拟跨地域专线拥塞、云厂商带宽包耗尽、共享宿主机带宽争抢场景

> **与网络延迟用例的区别**：`netem delay` 增加每个包的时延，`tbf` 限制单位时间字节数。
> 前者让「一次请求」变慢，后者让「传大文件」变慢 —— 二者不可互相替代。
> ChaosBlade 的 `pod-network` 无带宽限速 action（以 `blade create k8s pod-network --help`
> 实测为准），本用例只有 kubectl-native 一条路径。

**资源准备**：
1. 确认目标 Pod 正常运行，且有可用的 **iproute2** `tc`（见演练步骤 1 —— 精简镜像里常有同名的
   BusyBox applet，它不支持 tbf；若无可用 tc，走演练步骤 2 的路径 B）
2. 确认目标 Pod 有持续的出方向数据传输（对象存储上传、日志外发、数据同步等），
   否则限速不产生可观测现象
3. 记录限速前的吞吐基线（见注入验证步骤 2 的测速方法），否则无法判断限速是否生效
4. 确认目标 Pod 名称和命名空间

**演练步骤**：
1. 确认目标 Pod 运行状态，并判定容器内的 `tc` 是不是真的能用 —— **`which tc` / `command -v tc`
   会误判**，精简镜像里 `/bin/tc` 常与 `/bin/sh` 是同一个 BusyBox 二进制，名字在但不支持 tbf：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -- tc -Version
   ```
   - 输出 `tc utility, iproute2-<版本>` → 真 tc，走路径 A
   - 输出 `BusyBox v<版本> ...` 或命令不存在 → 走路径 B

2. 注入带宽限制，按上一步结论二选一。

   **路径 A —— 容器内确认是 iproute2 tc**（需 NET_ADMIN；`CapEff` 全零的容器会报 EPERM）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- \
     tc qdisc add dev eth0 root tbf rate 1mbit burst 32kbit latency 400ms
   ```

   **路径 B —— 容器内没有可用 tc（精简镜像的常态）**：用临时容器注入。临时容器与目标容器
   **共享同一个网络命名空间**，对 `eth0` 操作等价于操作目标 Pod 的网卡；`tc` 来自调试镜像，
   `--profile=netadmin` 提供 NET_ADMIN capability：
   ```bash
   # 0) 前置安全检查：确认目标 Pod 不是 hostNetwork。hostNetwork=true 的 Pod
   #    其网络命名空间【就是宿主机】，临时容器里的 tc 会限住整个节点的带宽，
   #    爆炸半径从单 Pod 扩大到整台机器。为 true 时禁止此路径。
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.hostNetwork}'
   # 期望输出为空或 false；输出 true 则停止。

   # 1) 先建一个【长驻】临时容器作为执行载体。必须用 `sleep` 保活 ——
   #    若直接把 tc 命令交给 kubectl debug，命令跑完容器立即终止，
   #    后续 `kubectl exec -c <debugger>` 会报 `container not found`，故障就没法恢复了。
   kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
     --target=<container-name> --profile=netadmin --quiet -- sleep <duration>

   # 2) 取载体名（等它进入 running 再继续）
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}{"="}{.state}{"\n"}{end}'

   # 3) 经载体注入
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- \
     tc qdisc add dev eth0 root tbf rate 1mbit burst 32kbit latency 400ms
   ```
   - `<verified-cluster-image>`：必须是当前集群**已验证可拉取**且含 **iproute2**（非 BusyBox）的镜像。
     可靠的找法是看集群里已经在跑的镜像 —— 它们必然可拉取：
     `kubectl get pods -A -o jsonpath='{{..image}}'`。
     CNI / 网络组件（terway、calico、cilium 等）通常自带 iproute2；
     选定后用 `kubectl debug ... -- tc -Version` 确认输出是 `tc utility, iproute2-...`
   - `--quiet`：不进入交互附着；**不要加 `-it`**

   参数说明（两条路径相同）：
   - `rate 1mbit`：目标速率。注意单位是 **bit/s** 而非 byte/s —— `1mbit` ≈ 125 KB/s
   - `burst 32kbit`：令牌桶容量，允许的瞬时突发量。**过小会导致达不到 rate**
     （包还没攒够令牌就被丢），经验值取 `rate/8` 上下；`rate 1mbit` 配 `burst 32kbit` 是安全组合
   - `latency 400ms`：包在队列里最长等待时间，超时即丢弃。**过小会变成丢包而非限速**
   - `dev eth0`：通常为 Pod 主网卡，部分环境为 `eth0` 以外名称
   - 只限**出方向**。入方向限速需 `ifb` 重定向（多一层内核模块依赖），不在本用例范围

3. 观察应用的数据传输耗时与吞吐变化

**注入验证**：
1. 确认 tbf 规则已生效（**用注入时同一条路径查**）：
   ```bash
   # 路径 A
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   # 路径 B（复用注入时创建的临时容器）
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc show dev eth0
   ```
   输出应包含 `qdisc tbf ... rate 1Mbit burst ... lat ...`

2. 测吞吐，用「注入前 vs 注入后」的速率差作为判据。**不要用小请求测** ——
   限速不影响单个小包的时延，几 KB 的请求在 1mbit 下仍是毫秒级返回，看起来「没生效」：
   ```bash
   # 拉一个足够大的对象（>= 1MB），只看速率不要正文
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- \
     curl -s -o /dev/null -w '%{speed_download} bytes/s in %{time_total}s' --max-time 60 <大文件地址>
   ```
   判据：`speed_download` 应落在 `rate` 附近（`1mbit` ≈ 125000 bytes/s，允许 ±30% 偏差）。
   若与基线相比无明显下降，检查 `burst` 是否过大（相当于没限速）或测试对象太小。

3. 确认**小请求仍然正常** —— 这是区分「限速」与「网络不通」的关键：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <依赖服务健康检查地址>
   ```
   应正常返回。若连小请求也失败，说明 `latency` 设得过小、退化成丢包了。

4. 检查应用日志是否出现传输超时、上传失败、同步滞后等慢速症状

**注入恢复**：
1. 删除 tc tbf 规则（不使用 blade destroy，这是 kubectl-native 方案）——
   **必须用注入时那条路径**：
   ```bash
   # 路径 A
   kubectl exec <pod-name> -n <namespace> -- tc qdisc del dev eth0 root
   # 路径 B —— 复用同一个临时容器，不要新建
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc del dev eth0 root
   ```
   临时容器名遗失时用
   `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.ephemeralContainerStatuses[*].name}'`
   取回。
2. 如 Pod 被重启，tc 规则自动消失（不持久化），无需额外操作
3. 走过路径 B 的话：**临时容器无法从运行中的 Pod 移除**（Kubernetes 既定行为），只能随 Pod 重建消失。
   `tc qdisc del` 成功即代表故障已恢复，残留的临时容器不影响业务容器。如需立即清理须删除该 Pod
   让上层控制器重建 —— 这是额外的变更动作，须经确认后再做。

**恢复验证**：
1. 确认 tbf 规则已清除（**与注入/恢复同一条路径**）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc show dev eth0
   ```
   输出应回到默认 qdisc（如 `pfifo_fast` / `noqueue` / `mq`），不再包含 tbf
2. 重测吞吐，确认速率恢复到基线水平：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- \
     curl -s -o /dev/null -w '%{speed_download} bytes/s' --max-time 60 <大文件地址>
   ```
3. 确认应用日志不再出现传输慢/超时错误

**基准事实**：
- **根因**：通过 tc tbf（令牌桶过滤器）在 Pod 网卡限制出方向速率，模拟带宽受限/专线拥塞环境
- **必现现象**：大对象下载速率被压到 `rate` 附近（1mbit ≈ 125 KB/s）；`tc qdisc show` 显示 tbf 规则；
  小请求仍正常返回（区别于网络不通）；应用出现数据同步滞后、上传超时等慢速症状
