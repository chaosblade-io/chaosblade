**用例名称** 网络请求延迟 导致 Pod_网络延迟

## 故障定位
持续型故障——tc netem delay 规则是状态型故障，规则存活即故障存活，贯穿整个故障窗口；
窗口结束实验销毁/定时删除即自动恢复。手段1（ChaosBlade）与手段2（kubectl-native）是
**并列的注入手段**，底层效果完全等价（blade 底层也是下发同一条 netem delay 规则），
按环境能力选用：集群装有 ChaosBlade 且 `pod-network` 提供 delay action → 可用手段1；
否则用手段2。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. Pod 所有出方向网络请求增加固定延迟（如 1000ms）
2. 应用调用上下游服务超时或响应显著变慢
3. 健康检查可能因超时失败，导致 Pod 被重启
4. 模拟跨地域部署/弱网环境/网络拥塞场景

## 资源准备
1. 确认目标 Pod 正常运行，且有可用的 **iproute2** `tc`（见演练步骤 1 —— 精简镜像里常有同名的
   BusyBox applet，它不支持 netem；若无可用 tc，走演练步骤 2 的路径 B）
2. 确认目标 Pod 有对外网络调用（上下游服务、数据库等）
3. 确认目标 Pod 名称和命名空间
4. 确认目标节点内核支持 netem（**内核级依赖，路径 A/B 都绕不开**）：netem 由宿主机内核的 sch_netem 模块提供，容器与宿主共享内核，换 Pod / 换临时容器都改变不了。**权威判据是功能探测，不是 grep 模块列表**：
   - grep 只能作辅证：`kubectl exec <pod-name> -n <namespace> -- grep sch_netem /proc/modules`。**有输出 → 必可用；无输出 ≠ 不可用**（存在 sch_netem 未加载、/sys/module 无对应目录，但持 NET_ADMIN 的载体内建 netem qdisc 成功的形态——模块可能内建或首次使用时被按需加载，不得因 grep 无输出就放弃手段2）
   - 功能探测（在持有 NET_ADMIN 的共享 netns 载体内对 dummy 接口试建 netem，不碰业务网卡）——路径A 在目标容器内、路径B 用临时容器执行同一条命令：
     ```bash
     sh -c 'ip link add dtest0 type dummy && tc qdisc add dev dtest0 root netem delay 1ms \
            && echo NETEM_AVAILABLE || echo NETEM_UNAVAILABLE; \
            tc qdisc del dev dtest0 root 2>/dev/null; ip link del dtest0 2>/dev/null; true'
     ```
     输出 `NETEM_AVAILABLE` → 内核支持；`NETEM_UNAVAILABLE` 或注入时报
     `RTNETLINK answers: Operation not supported`、`RTNETLINK answers: No such file or directory` 或
     `Error: Specified qdisc kind is unknown.`（RC=2） → 内核不支持，立即停止并 replan。
     部分 ACK/ASI al8 内核（5.10.134-13.1.al8）即为此形态——同一节点 netem 全家（loss/delay/corrupt）全部不可行，而 sch_tbf 存在（带宽受限场景可用，见 `Pod_网络带宽不足_带宽受限`）。**不要**试图去宿主或基础设施容器里 modprobe 加载模块——那是目标范围外的变更，会被 target_guard 以 namespace 漂移拦截

## 演练步骤
1. 确认目标 Pod 运行状态，并判定容器内的 `tc` 是不是真的能用 —— **`which tc` / `command -v tc`
   会误判**，精简镜像里 `/bin/tc` 常与 `/bin/sh` 是同一个 BusyBox 二进制，名字在但不支持 netem：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -- tc -Version
   ```
   - 输出 `tc utility, iproute2-<版本>` → 真 tc，走路径 A（**还需容器持 NET_ADMIN：判据是该
     capability 位本身**——`CapEff` 十六进制值 bit12，该位为 0 时 `tc qdisc add` 报 EPERM；
     CapEff 非零也可能缺此位，**不要只看「全零」形态**。NET_ADMIN 属容器默认集**不含**的
     capability——spec 侧 `securityContext.capabilities` 无 add 声明即可推断运行时缺失，
     此方向 API 侧可代答；反向不成立，见注入验证第 2 条）
   - 输出 `BusyBox v<版本> ...` 或命令不存在 → 走路径 B
     （BusyBox 版执行 `tc qdisc add ... root netem` 会报 `invalid argument 'root' to 'command'`）
   同时取**延迟判据的对照锚点**（在目标 Pod 内对集群内稳定 HTTPS 服务测连接耗时基线；示例用
   集群 `kubernetes` Service，地址以当次实测为准）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- curl -s -o /dev/null \
     -w 'connect=%{time_connect} total=%{time_total}\n' --connect-timeout 3 -k https://<集群内稳定 HTTPS 服务地址>/
   ```
   记下基线值（集群内通常 <10ms）；注入后同一命令的 `time_connect` 应增加约注入的延迟值
2. 判定注入手段——手段1 的可用性是三个串联前置的与（按序短路，前面死了后面不用再查）：
   **前置1（operator 活性）**：ChaosBlade operator 未就绪（0 副本 / Init 失败 / 长期非 Running）
   → CRD 注入通道整体不可用，手段1 即判死——此状态下创建的实验 CR 无人调谐、静默失效
   （实测形态：operator 宕机集群中 15 个历史 CR 遗留 48–109 天未被 finalizer 清理），
   此时 `blade -h` 命令家族再全也不改变结论。
   **前置2（目标节点 tool pod）**：operator 就绪时才看——blade 的实际执行经由节点侧
   tool pod，目标节点无存活 tool pod 时实验无法落地。
   **前置3（命令家族）**：以**执行环境内** `blade create k8s pod-network -h` 的
   Available Commands 为准（列出 `delay` → 可用**手段1（ChaosBlade）**；仅有
   `dns/drop/occupy` → 该 blade 构建不提供 delay，用**手段2（kubectl-native）**）：

**手段1（ChaosBlade）**

3. 注入网络延迟：
   ```bash
   blade create k8s pod-network delay \
     --namespace <namespace> \
     --names <pod-name> \
     --interface eth0 \
     --time <delay-ms> \
     --timeout <duration>
   ```
   - `--time`：固定延迟毫秒数（如 1000）
   - `--timeout`：到期自动恢复（秒）
4. 记录返回的 experiment_uid，用于后续恢复

**手段2（kubectl-native）**

   **路径 A —— 容器内确认是 iproute2 tc**（需 NET_ADMIN——**判据是 `CapEff` 十六进制值的
   bit12 位**，该位为 0 时 `tc qdisc add` 报 EPERM；CapEff 非零也可能缺此位，不要只看「全零」形态）。
   **先武装定时自删，再注入规则**（后台进程与目标容器同 netns，到期自动移除规则，
   补齐自恢复能力；必须重定向后台化，否则 exec 挂住）。
   两条命令分两次独立执行——不能用 && 串联：第二段 kubectl 会沦为第一条 exec
   载荷（sh -c）的死参数，注入静默丢失：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c \
     '( sleep <recovery-seconds>; tc qdisc del dev eth0 root ) >/dev/null 2>&1 &'
   kubectl exec <pod-name> -n <namespace> -- \
     tc qdisc add dev eth0 root netem delay <delay>
   ```
   倒计时从武装时刻起算：武装与注入两条命令必须紧邻连续下发（≤60s）；武装后发生任何修复须先 `kubectl exec <pod-name> -n <namespace> -- sh -c 'pkill -f "qdisc de[l]"; true'` 停旧定时器再全额重武装；精简镜像无 pkill 时旧定时器无法停止，到期会提前恢复侵蚀故障窗口——须中止演练改人工恢复或如实上报缩短的窗口（见 SKILL.md 安全红线「故障窗口完整」）

   `<recovery-seconds>`：安全网窗总时长（秒），取 prompt 下发的 `recovery_timer_seconds`
   （= duration + grace，见 SKILL.md 双数窗口契约）——路径A 的 sleep 定时器与路径B 的
   链内 sleep 自删链以它武装，让框架在观察窗终点主动派发的恢复先于自治到期落地；
   手段1 的 `--timeout` 由引擎在派发前按同一单源钉定，文档占位符保持 `<duration>` 不动

   **路径 B —— 容器内没有可用 tc（精简镜像的常态）：临时容器载体**

   临时容器与目标容器**共享同一个网络命名空间**，在其内对 `eth0` 操作等价于操作目标 Pod 的网卡；
   `tc` 来自调试镜像而非目标镜像，`--profile=netadmin` 提供 NET_ADMIN capability。

   ```bash
   # 0) 前置安全检查：确认目标 Pod 不是 hostNetwork。hostNetwork=true 的 Pod
   #    其网络命名空间【就是宿主机】，临时容器里的 tc 会打穿整个节点，
   #    爆炸半径从单 Pod 扩大到整台机器。为 true 时禁止此路径，改用 node 级用例。
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.hostNetwork}'
   # 期望输出为空或 false；输出 true 则停止。

   # 1) 注入 + 内置定时自删（一条命令完成：注入成功后 sleep <recovery-seconds> 到期自动删除规则）。
   #    --target 必须填实际容器名；--quiet 不进入交互附着，不要加 -it。
   kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
     --target=<container-name> --profile=netadmin --quiet -- sh -c \
     'tc qdisc add dev eth0 root netem delay <delay> \
      && tc qdisc show dev eth0 && echo INJECTED && sleep <recovery-seconds> \
      && tc qdisc del dev eth0 root && tc qdisc show dev eth0 && echo RECOVERED'
   ```
   链条是**自证的**：`add` 后、`INJECTED` 前的 `tc qdisc show` 把生效规则原文写入容器日志，
   `del` 后、`RECOVERED` 前的 `tc qdisc show` 把回落后的默认 qdisc 写入同一日志——白盒主证
   在注入时刻即被捕获，验证阶段读日志即可取证，**不再受故障窗口是否已关闭的时序约束**
   （窗口短于验证启动延迟时，窗口内探针结构性不可达；链内快照消除此竞态）。两处 show 必须保留，
   不得为缩短命令而省略。
   倒计时从注入时刻起算（注入成功与倒计时起点在同一条命令链内严格串行，无侵蚀间隙）；
   注入后发生任何修复需重武装时，先按下方「注入恢复」的提前恢复命令删掉旧规则
   （旧链到期后的重复删除是幂等空触发、无害），再重跑注入命令重新武装+注入（见 SKILL.md
   安全红线「故障窗口完整」）
   - 该命令整体阻塞 `<recovery-seconds>` 秒（命令自身就是保活载体，自恢复随命令完成而闭环）；
     如需后台执行，将整条 kubectl debug 置于后台并轮询其输出/临时容器日志
   - 输出未直接回流时，从临时容器日志读取（`INJECTED`/`RECOVERED` 标记即注入/自恢复的确证；
     标记之间的 `tc qdisc show` 输出即白盒主证原文）：
     ```bash
     kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.ephemeralContainers[-1].name}'
     kubectl logs <pod-name> -n <namespace> -c <ephemeral-container-name>
     ```
   - `<verified-cluster-image>`：必须是当前集群**已验证可拉取**且含 **iproute2**（非 BusyBox）的镜像。
     可靠的找法是看集群里已经在跑的镜像 —— 它们必然可拉取：
     `kubectl get pods -A -o jsonpath='{{..image}}'`。
     CNI / 网络组件（terway、calico、cilium 等）通常自带 iproute2，因为它们本身就要做流量整形；
     选定后用 `kubectl debug ... -- tc -Version` 确认输出是 `tc utility, iproute2-...`
   - `--quiet`：不进入交互附着；**不要加 `-it`**

   参数说明（两条路径相同）：
   - `delay <delay>`：固定延迟，值须带单位（如 `500ms`、`2s`），按演练目标确定
   - 可附加抖动：`delay <delay> <jitter>`（如 `1000ms 200ms` 表示 1000±200ms）
   - `dev eth0`：通常为 Pod 主网卡，部分环境为 `eth0` 以外名称
   - **内核级依赖（两条路径相同）**：netem 需要宿主机内核支持 sch_netem。若注入报
     `RTNETLINK answers: Operation not supported`、`RTNETLINK answers: No such file or directory`
     （模块文件缺失）或 `Error: Specified qdisc kind is unknown.`（RC=2，另一实际形态），
     即内核不支持 netem 的确证 —— 立即停止，
     **不要重试、不要换 Pod 或重建临时容器**（内核是同一个，重试只是空转），发起 replan
     并附上该报错证据，由 Phase 1 改选其他可行方案或判定不可行
3. 观察应用响应时间变化

## 注入验证
1. 白盒确认 netem 规则已生效。**路径 B 的首选证据是注入容器日志**——链内 `tc qdisc show`
   快照在注入时刻已捕获，直接读日志取证（无窗口时序约束，验证阶段晚于窗口关闭也同样有效）：
   ```bash
   kubectl logs <pod-name> -n <namespace> -c <注入 ephemeral 容器名>
   ```
   `INJECTED` 之前应显示 `qdisc netem ... delay <delay>`。路径 A 或需要**当前**规则状态时，
   用注入时同一条路径查（`tc qdisc show` 同样需要真 tc）——规则快照为机制代言不为结果代言，
   用于定位失败层（规则未挂载 vs 挂载未生效）：
   ```bash
   # 路径 A
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   # 路径 B（复用注入时创建的临时容器）
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc show dev eth0
   ```
   输出应包含 `netem delay <delay>`（即注入时配置的延迟规则）
2. **（主证据，必做）** 在 Pod 内验证延迟效果。**不要用 `ping`** —— 它需要 `CAP_NET_RAW`，
   未持该位的容器（**`CapEff` 全零只是最常见形态，非零也可能缺这一位**）执行会返回
   `ping: permission denied (are you root?)`；更糟的是若接了管道（如 `| tail`），退出码来自
   管道末端，会**看起来成功**。NET_RAW 属容器默认集（runtime/default）**含**的
   capability——spec 侧 `securityContext.capabilities` 为空不代表运行时缺失（默认集
   注入不体现在该字段），运行时是否持有只有 `CapEff` 能回答（实测形态：spec 侧空但
   CapEff bit13=1，ping 本可用——把 API 侧空误读为运行时缺位是实测发生过的误判）。改用
   TCP 层的探测：
   ```bash
   # 把探测超时设在「基线耗时」与「基线耗时+注入延迟」之间，用「成功→超时」的翻转作为判据。
   # 例：基线 <100ms、注入 1000ms 时，--timeout=1 的探测会由成功转为超时：
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=<探测超时秒数> --tries=1 <依赖服务地址>
   # 无 wget 的镜像（含 iproute2 tc 的 CNI 镜像常无 wget 但有 curl）用 curl 形态，
   # 同样把连接超时设在「基线」与「基线+注入延迟」之间：
   kubectl exec <pod-name> -n <namespace> -- curl -s -o /dev/null --connect-timeout <探测超时秒数> <依赖服务地址>
   # 目标容器内没有探测工具时，可在共享 netns 的调试容器内执行（-c <debugger-name>）
   ```
   注入生效的判据：该命令由「正常返回」变为「超时失败」。把超时放宽到远大于注入延迟值应仍能成功，
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

## 注入恢复

手段1（ChaosBlade）：
1. 提前恢复：销毁实验（移除 netem 规则）`blade destroy <experiment_uid>`
2. 或等待 `--timeout`（`<duration>`）到期后 ChaosBlade 自动恢复

手段2（kubectl-native）：
1. 主保险：路径A 武装的定时器 / 路径B 注入命令内置的 `sleep <recovery-seconds>` 自删链到期自动移除规则；
   如需提前恢复，手动删除 tc netem 规则（不使用 blade destroy，这是 kubectl-native 方案）——
   **必须用注入时那条路径**，`tc qdisc del` 同样需要真 tc：
   ```bash
   # 路径 A
   kubectl exec <pod-name> -n <namespace> -- tc qdisc del dev eth0 root
   # 路径 B —— 复用同一个临时容器，不要新建
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc del dev eth0 root
   ```
   临时容器名遗失时用
   `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.ephemeralContainerStatuses[*].name}'`
   取回。`tc qdisc del` 对已删除的规则报 `RTNETLINK answers: No such file or directory`，无害但需预期
2. 如 Pod 因健康检查超时被驱逐重建（**Pod 级重建伴随 sandbox/netns 重建**），tc 规则随旧 netns
   消失（不持久化），无需额外操作；**仅目标容器级重启不重建 netns**（规则仍挂在原 netns 的
   网卡上），仍需按第 1 条删除
3. 走过路径 B 的话：**临时容器无法从运行中的 Pod 移除**（Kubernetes 既定行为），只能随 Pod 重建消失。
   `tc qdisc del` 成功即代表故障已恢复，残留的临时容器不影响业务容器。如需立即清理须删除该 Pod
   让上层控制器重建 —— 这是额外的变更动作，须经确认后再做。

## 恢复验证
1. 确认 tc 规则已清除（**与注入/恢复同一条路径**）：
   ```bash
   # 路径 A
   kubectl exec <pod-name> -n <namespace> -- tc qdisc show dev eth0
   # 路径 B（复用注入时那个临时容器）
   kubectl exec <pod-name> -n <namespace> -c <debugger-name> -- tc qdisc show dev eth0
   ```
   输出应不再包含 netem 规则。**路径 B 亦可直接读注入容器日志取证**——`RECOVERED` 之前的
   链内快照即回落后的默认 qdisc，无窗口时序约束：
   ```bash
   kubectl logs <pod-name> -n <namespace> -c <注入 ephemeral 容器名>
   ```
2. 在 Pod 内验证延迟恢复正常（用演练步骤 1 的**同一探针**，与当时记录的基线直接对比）：
   ```bash
   # 同样避开 ping（需 CAP_NET_RAW）——用 TCP 探测，超时取接近基线耗时的短值
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=<探测超时秒数> --tries=1 <依赖服务地址>
   # 无 wget 的镜像用 curl 形态（与演练步骤 1 的基线探针同口径，直接对比 time_connect）
   kubectl exec <pod-name> -n <namespace> -- curl -s -o /dev/null \
     -w 'connect=%{time_connect} total=%{time_total}\n' --connect-timeout 3 -k https://<集群内稳定 HTTPS 服务地址>/
   ```
   确认耗时/`time_connect` 恢复回**演练步骤第 1 步记录的基线水平**（不再是「基线 + 注入延迟」的量级）
3. 确认应用日志不再出现超时错误

## 基准事实
- **根因**：通过 tc netem 在 Pod 网卡注入出方向固定延迟，模拟弱网/跨地域网络环境
- **必现现象**：Pod 内对外 TCP 请求耗时增加约注入的延迟值（短超时探测由成功转为超时）；`tc qdisc show` 显示 netem delay 规则；应用对外请求响应时间显著增加
- **blade 可用性因构建而异**：`pod-network delay` 并非所有 blade 发行版都提供（官方发行含
  delay/loss/corrupt/reorder 等 netem 全家桶；存在仅提供 dns/drop/occupy 的定制发行）——
  以**执行环境内** `blade create k8s pod-network -h` 的 Available Commands 为准，没有 delay 就用手段2
- **集群 Pod 遗迹是环境形态的历史证据**：`kubectl get pods -A`（或 `-o jsonpath='{{..image}}'`）
  一次扫描同时回答多个环境问题——哪些调试镜像已在集群内验证可拉取、历史演练留下了哪些
  载体/CR 遗迹、同类工具 pod 的存活比例。存活比例须按计数陈述（实测形态：36/41 失效
  + 5 Running）；「全部失效」式的全称判断与部分存活的事实结论不同，直接影响载体可用性判定
- **netem 可用性以功能探测为准**：`/proc/modules` 与 `/sys/module` 查不到 sch_netem 痕迹，
  不等于内核不支持——模块可能内建或首次使用时按需加载，必须用 dummy 接口功能探测定论
  （见资源准备第 4 条）；反之探测报不支持（或注入报 `Operation not supported` /
  `No such file or directory` / `Specified qdisc kind is unknown.`）即收工，换 Pod、换临时容器、
  换路径都改变不了（容器与宿主共享内核）

**手段2 注意事项**：
- **`--` 之后是裸 argv 直通，无 shell 解释**：多个命令、`;`、`&&` 不得直接拼在 `--` 后
  （首 token 会被当成含 `;` 的可执行文件名，容器立即 exit 255）——整条链必须包进单个
  `sh -c '<完整链>'` 作为 `--` 的单一参数
- **临时容器本身无法从运行中的 Pod 移除**（Kubernetes 既定行为），只能随 Pod 重建消失；
  `tc qdisc del` 成功即代表故障已恢复，残留临时容器不影响业务容器
- 自恢复基于武装的定时删除（路径A）或注入命令内置的 `sleep <recovery-seconds>` 自删链（路径B）；
  提前恢复用上方手动删除命令
- 效果与手段1 完全等价——blade 底层就是下发同一条 netem delay 规则
