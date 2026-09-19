**用例名称** Sidecar网络中断 导致 Container_网络丢包

**故障现象**：
1. Sidecar 容器绑定端口的网络流量被丢弃，该端口服务完全不可达
2. 如果是代理类 Sidecar（如 istio-proxy），所有经过代理的业务流量中断
3. 主容器直接对外的端口流量可能不受影响（取决于网络拓扑和 Sidecar 角色）
4. Sidecar 日志中出现大量连接超时或 reset 错误

**资源准备**：
1. 确认目标 Pod 包含多个容器，明确 Sidecar 容器名称
2. 确认 Sidecar 容器监听的端口（如 istio-proxy 的 15001/15006）
3. 确认目标 Pod 所在 namespace 和 labels
4. **多容器靶接线（靶为单容器 Pod 时）**：业务靶常见单容器形态——用 JSON patch 给靶 Deployment 模板追加一个 sidecar 容器承载"受害端口服务"（演练资产，结束拆线归还）。**双端口设计**（受害端口 + 对照端口是本用例验证判据的根基——共享网络栈下"只断该端口、其他端口正常"必须由同 Pod 双端口对照证明）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"add","path":"/spec/template/spec/containers/-","value":{"name":"drill-sidecar","image":"<节点缓存镜像>","command":["sh","-c","<双端口监听命令>"]}}]'
   kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=120s
   ```
   - **镜像选型**：须选**节点缓存或 VPC registry 可达镜像**——VPC 受限网络下 docker.io 公网镜像（如 busybox:1.36）会 `ImagePullBackOff`（接线即挂，需 fallback 回基线重接线补救）。节点缓存镜像（如靶容器自身的 CNI 镜像）零拉取风险；选定后在候选容器内 `command -v socat` / `command -v httpd` 探测监听工具再定双端口命令形态（socat 双监听：`socat TCP-LISTEN:8080,fork,reuseaddr EXEC:/bin/cat & socat TCP-LISTEN:8081,fork,reuseaddr EXEC:/bin/cat & sleep 7200`；探针客户端亦用该镜像工具——socat 客户端形态 `echo PING | socat -T 3 - TCP:127.0.0.1:8080`，超时无回显即受害、回显即对照）
   - 8080 = 受害端口、8081 = 对照端口；拆线用 `kubectl patch --type='json' -p='[{"op":"remove","path":"/spec/template/spec/containers/<sidecar索引>"}]'` 后 rollout status 等收敛
   - **拆线归恢复生命周期**：接线资产拆除不进执行计划（SKILL.md 安全红线「拆线不进执行计划」）——执行器下发完「接线+武装+注入」即止，等待恢复/拆线由 recover 生命周期接管
5. **注入宿主预判（iptables 执行位置）**：iptables 规则作用于 Pod 网络栈（共享 netns），但执行进程所在容器必须同时具备 **iptables 二进制 + NET_ADMIN**。busybox sidecar 有 wget/httpd 无 iptables；业务主容器常见有 iptables 无 NET_ADMIN（默认 Pod 不给）——**两端都不齐时唯一宿主是 kubectl debug 临时容器**（`--profile=netadmin` 自动补 NET_ADMIN，镜像选集群已验证可拉取且含 iptables 的镜像，CNI 镜像如 terway 必带）。容器工具集 + capability 一轮探明：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container-name> -- sh -c 'command -v iptables; iptables --version; grep CapEff /proc/self/status'
   ```
6. **手段前置探测**：ChaosBlade operator 就绪性以当次探测为准（`kubectl get pods -n chaosblade`）——未就绪则 blade 手段判死、转手段2；sch_netem 同样当次探测（路径 B/C 的 tc netem 判死依据）；工具集漂移是常态（同一镜像不同批次工具可能不同），历史探测结论不作数
7. 若走路径 C（tc netem）：确认目标节点内核支持 netem（**内核级依赖**）。netem 由宿主机内核的 sch_netem 模块提供，容器与宿主共享内核，换容器 / 换临时容器都改变不了。只读探查：`kubectl exec <pod-name> -n <namespace> -- grep sch_netem /proc/modules`——有输出说明已加载；无输出时可用更强的前置确证：经 node debug 载体执行 `chroot /host modprobe sch_netem` 试载，报 `FATAL: Module sch_netem not found` 即模块文件本身缺失（内核自动加载不可能成功），**注入前即可定案不可行**；部分 ACK/ASI al8 内核（5.10.134-13.1.al8）即为此形态——同一节点 netem 全家（loss/delay/corrupt）全部不可行，而 sch_tbf 存在（带宽受限场景可用，见 `Pod_网络带宽不足_带宽受限`）。**判据以注入输出为准**：注入报 `RTNETLINK answers: Operation not supported`、`RTNETLINK answers: No such file or directory`（后者为节点上 sch_netem 模块文件本身缺失、内核自动加载失败）或 `Error: Specified qdisc kind is unknown.`（RC=2，另一种报错形态）即为内核不支持 netem 的确证

**演练步骤**：

> **爆炸半径分类（定案）**：`target-only`——iptables 规则以 `--sport <受害端口>` 锚定单端口，只丢该端口的响应流量；规则写入 Pod netns（共享网络栈），变更面 = 靶 Pod 网络栈 + 靶 Deployment 模板（接线/拆线）+ 靶 Pod 内临时容器，不触及任何非靶资源。勿用全量 `-j DROP`（不带端口限定会断整个 Pod 出流量——含健康检查与监控，属 Pod 级而非端口级爆炸半径，且违反本用例"sidecar 中断、主容器正常"的故障语义）。

1. 确认 Pod 内容器列表，获取 Sidecar 容器名称：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].name}'
   ```
2. 确认 Sidecar 容器监听的端口：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- netstat -tlnp
   ```
3. 记录注入前通过 Sidecar 端口的连通性：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- curl -s -o /dev/null -w '%{http_code}' http://localhost:<port>/health
   ```
4. 使用 ChaosBlade 对 Sidecar 容器注入网络丢包故障：
   ```bash
   blade create k8s container-network drop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --container-names <sidecar-container-name> \
     --source-port <port> \
     --timeout <duration>

   ```
   说明：Container 共享 Pod 网络 namespace，drop 通过 iptables 针对容器进程绑定端口的流量规则实现
5. 观察通过 Sidecar 的流量是否完全中断

**注入验证**：
> **verify 阶段行为探针不可用（定案）**：verify 只读门禁会把 socat/wget 网络探针判为 load-generating 拒绝（基线采集阶段不受限——executor 阶段可正常跑 socat 探针）。被拒时**勿重试同类命令**，按规则型判据裁决（规则在即故障在，iptables DROP 是内核同步执法非代理指标）+ deviation 文档化（记录被拒命令与原因），行为对照由恢复后带外核实补强（规则移除后受害端口恢复回显 = 规则作用过的反向证明）

1. 通过 Sidecar 代理端口的访问完全失败（连接超时或被拒绝）
2. 主容器直接暴露的端口仍可正常访问（如果存在直连路径）
3. 执行 `kubectl logs <pod-name> -c <sidecar-container-name> -n <namespace>`，确认日志中出现连接错误
4. 检查依赖该 Sidecar 代理的上下游服务是否出现超时或 5xx 错误

**持续性检查（必做）**——故障窗口内故障必须持续存活（规则型故障：iptables 规则在即故障在）：
以「注入生效确认」为时点锚（受害端口超时 + 对照端口正常 + `iptables -S` 规则在位三者齐 = 生效），生效后一次 `time_wait 60`，到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：`iptables -S OUTPUT`（或 `iptables -L OUTPUT -n`）规则仍在（规则在即故障在）
2. 行为复查：受害端口探针仍超时（`wget -T 3 -q -O /dev/null http://127.0.0.1:<受害端口>/` 失败）
3. 对照复查：对照端口探针仍正常（`wget -T 3 -q -O /dev/null http://127.0.0.1:<对照端口>/` 返回内容）——对照持续正常同时证明规则未越界扩散

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动清除 iptables 规则

**恢复验证**：
1. 通过 Sidecar 代理端口的访问恢复正常
2. 执行 `kubectl logs <pod-name> -c <sidecar-container-name> -n <namespace> --tail=10`，确认无新增连接错误
3. 确认依赖该 Sidecar 的业务服务恢复正常响应
4. **白盒终态判据**：`iptables -S OUTPUT`（在原注入容器执行）不再含 sport 受害端口规则——规则删除是逐字对应 `-D`，非零残留不适用（与计数器型判据不同，规则型故障的恢复判据就是规则不在）
5. **拆线归还（接线靶）**：patch remove sidecar 容器 + `kubectl rollout status` 等收敛——拆线触发的 Pod 重建**同时消除 debug 临时容器**（ephemeralContainer 随 Pod 生命周期消失，无法从运行中 Pod 移除是 K8s 既定行为），靶 Deployment 回到单容器基线形态（同 RS 归还为模板精确回滚的数学证据）

**基准事实**：
- **根因**：Sidecar 容器绑定端口的网络流量被 iptables 规则丢弃，模拟 Sidecar 网络隔离场景
- **必现现象**：通过 Sidecar 端口的流量 100% 丢弃；Sidecar 日志出现连接超时/reset 错误；主容器直接端口不受影响（在非代理拓扑下）；依赖 Sidecar 代理的服务调用失败

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，用以下 kubectl 原生命令实现等效故障注入。

前提条件：需要 `iptables` 或 **iproute2** `tc` 之一。**两者在精简镜像里通常都没有** ——
先判定，再选路径：

```bash
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c \
  'command -v iptables || echo NO_IPTABLES; tc -Version'
```
- 有 `iptables` 且容器有 `NET_ADMIN` → 路径 A
- `tc -Version` 输出 `tc utility, iproute2-<版本>` 且有 `NET_ADMIN` → 路径 B
- 两者都没有，或 `tc -Version` 输出 `BusyBox v...`（BusyBox applet 不支持 netem），
  或容器 `CapEff` 全零 → 路径 C

注入命令（**先武装定时自删，再注入规则**——后台定时器到期自动执行逆操作，
补齐 ChaosBlade `--timeout` 的自恢复能力；必须重定向后台化，否则 exec 挂住）：
```bash
# ── 路径 A：容器内有真 iptables + NET_ADMIN
# 两条命令分两次独立执行——不能用 && 串联：第二段 kubectl 会沦为第一条 exec
# 载荷（sh -c）的死参数，注入静默丢失
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c \
  '( sleep <duration>; iptables -D OUTPUT -j DROP ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -A OUTPUT -j DROP
# 仅丢弃特定端口流量（同样两次独立执行，不能 && 串联）：
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c \
  '( sleep <duration>; iptables -D OUTPUT -p tcp --sport <port> -j DROP ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -A OUTPUT -p tcp --sport <port> -j DROP

# ── 路径 B：容器内有真 tc + NET_ADMIN（100% 丢包等效于网络中断；同样两次独立执行）
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c \
  '( sleep <duration>; tc qdisc del dev eth0 root ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- tc qdisc add dev eth0 root netem loss 100%

# ── 路径 C：容器内两者都不可用（精简镜像的常态）。临时容器与 Pod 内各容器共享同一个网络
#    命名空间，所以在临时容器里操作 eth0 就等于操作这个 Pod 的网络栈；工具来自调试镜像。
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
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc add dev eth0 root netem loss 100%
```

# ── 路径 C'：iptables-via-debug（tc netem 判死而 iptables 可用时的主路径）
#    适用形态：容器内无 iptables 或无 NET_ADMIN（busybox sidecar / 默认业务容器常态），
#    且节点内核无 sch_netem（netem 路径 B/C 全判死）。iptables 二进制 + NET_ADMIN 都由
#    debug 临时容器补齐（--profile=netadmin），规则作用于 Pod 共享网络栈。
#    镜像选含 iptables 的集群已验证镜像（CNI 类如 terway 必带，探测见资源准备第 5 条）。
kubectl debug <pod-name> -n <namespace> --image=<cni-image-with-iptables> \
  --profile=netadmin --quiet -- sleep 7200
# 载体保活 sleep ≥ <duration>；取载体名（形如 debugger-xxxxx）：
kubectl get pod <pod-name> -n <namespace> \
  -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}{"="}{.state}{"\n"}{end}'
# 注入前基线（受害/对照双端口连通，在 sidecar 容器内探测——探针形态随 sidecar 镜像工具集，
# wget 形态（busybox 系镜像）或 socat 客户端形态（CNI 系镜像））：
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c 'wget -T 3 -q -O - http://127.0.0.1:<受害端口>/ || echo PING | socat -T 3 - TCP:127.0.0.1:<受害端口>'
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c 'wget -T 3 -q -O - http://127.0.0.1:<对照端口>/ || echo PING | socat -T 3 - TCP:127.0.0.1:<对照端口>'
# 先武装定时自删（在 debug 载体内），再注入规则——两条独立执行，紧邻 ≤60s：
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- sh -c \
  '( sleep <duration>; iptables -D OUTPUT -p tcp --sport <受害端口> -j DROP ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- \
  iptables -A OUTPUT -p tcp --sport <受害端口> -j DROP
# 验证（sidecar 容器内）：受害端口超时 + 对照端口正常 + 白盒规则在位——
# 行为判据（受害超时/对照回显）与白盒判据（iptables -S）缺一不可，行为未采集即未生效确认
# （SKILL.md 安全红线「效果证据在窗口内采齐」）：
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c 'wget -T 3 -q -O /dev/null http://127.0.0.1:<受害端口>/; echo VICTIM_EXIT=$?'
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c 'wget -T 3 -q -O /dev/null http://127.0.0.1:<对照端口>/; echo CTRL_EXIT=$?'
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- iptables -S OUTPUT
# 期望：VICTIM_EXIT 非 0（超时）+ CTRL_EXIT=0（正常）+ OUTPUT 链含 sport 规则——
# 请求方向（dport=受害端口）畅通而响应（sport=受害端口）被丢，连接建立后无响应即超时；
# 对照端口流量不匹配规则全程正常，同 Pod 双端口对照证明"只断该端口"。
各路径倒计时均从武装时刻起算：武装与注入两条命令必须紧邻连续下发（≤60s）；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec <pod-name> -n <namespace> [-c <debugger-name>] -- sh -c 'pkill -f "iptables -[D]|qdisc de[l]"; true'`（exec 目标必须与武装时同一容器）；精简镜像无 pkill 时旧定时器无法停止，到期会提前恢复侵蚀故障窗口——须中止演练改人工恢复或如实上报缩短的窗口（见 SKILL.md 安全红线「故障窗口完整」）
- `<verified-cluster-image>`：必须是当前集群**已验证可拉取**且含 **iproute2**（非 BusyBox）的镜像。
  可靠找法：看集群里已经在跑的镜像，它们必然可拉取 ——
  `kubectl get pods -A -o jsonpath='{..image}'`。CNI / 网络组件（terway、calico、cilium 等）
  通常自带 iproute2，因为它们本身就要做流量整形。选定后用
  `kubectl debug ... -- tc -Version` 确认输出是 `tc utility, iproute2-...` 而非 `BusyBox v...`；
  拉不动时 Pod 事件会出现 `ErrImagePull` / `ImagePullBackOff`
- `--quiet`：不进入交互附着；**不要加 `-it`**
- 载体名形如 `debugger-xxxxx`，注入/验证/恢复三步都要用同一个
- **内核级依赖（仅路径 C）**：netem 需要宿主机内核支持 sch_netem。若注入报
  `RTNETLINK answers: Operation not supported`、`RTNETLINK answers: No such file or directory`
  （模块文件缺失）或 `Error: Specified qdisc kind is unknown.`（RC=2，另一种报错形态），
  即内核不支持 netem 的确证 —— 立即停止，
  **不要重试、不要换容器或重建临时容器**（内核是同一个，重试只是空转），发起 replan
  并附上该报错证据，改选 iptables 路径或判定不可行

恢复命令：
```bash
# ── 路径 A：与注入命令逐字对应删除
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -D OUTPUT -j DROP
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- iptables -D OUTPUT -p tcp --sport <port> -j DROP

# ── 路径 B
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- tc qdisc del dev eth0 root

# ── 路径 C
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc del dev eth0 root

# ── 路径 C'：与注入命令逐字对应删除（debug 载体内执行）
kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- \
  iptables -D OUTPUT -p tcp --sport <受害端口> -j DROP
```
临时容器名遗失时用
`kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.ephemeralContainerStatuses[*].name}'`
取回。

注意事项：
- **Pod 内所有容器共享同一个网络 namespace**，所以无论从哪个容器（含临时容器）下手，
  iptables/tc 规则都作用于整个 Pod 的网络栈。要限定影响范围只能靠 `--sport`/`--dport`
  或 netem 的作用方向，而不是靠 `-c` 选容器
- 全量中断会切断监控和健康检查，可能触发 Pod 重启
- 自恢复基于注入前武装的后台定时器（`sleep <duration>` + 逆操作），到期自动删除规则；
  提前恢复仍用下方手动命令。Pod 重启也会让规则自动消失（不持久化）
- 恢复 iptables 用 `-D` 逐字对应删除，不要用 `iptables -F`——那会清掉 Pod 原有的其他规则
- 本用例手段2（路径 A/C'）的恢复是**容器内进程操作**（定时器 kill/iptables -D 作用于 Pod netns），不是 API 对象写——恢复载体标准件判据一不满足，勿建四件套；定时器由注入容器内 sleep 自带（进程型自恢复），与恢复载体（API 型）是两类机制（见 `Container_CPU满载_Sidecar资源争抢` 同型立法）
- nf_tables 后端的 iptables（如 `iptables v1.8.8 (nf_tables)`）在临时容器内需 root + NET_ADMIN（netadmin profile 提供）——若报 `Permission denied` 或 `Could not fetch rule set generation id`，核对 capability 是否真的生效（`grep CapEff /proc/self/status`，NET_ADMIN 为 bit 12）
- 走过路径 C 的话：**临时容器无法从运行中的 Pod 移除**（Kubernetes 既定行为），只能随 Pod 重建消失。
  `tc qdisc del` 成功即代表故障已恢复；如需立即清理须删除该 Pod 让上层控制器重建 ——
  这是额外的变更动作，须经确认后再做
