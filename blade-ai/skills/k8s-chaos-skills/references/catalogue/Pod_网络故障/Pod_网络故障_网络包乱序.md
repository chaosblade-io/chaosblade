**用例名称** 网络包乱序 导致 Pod_网络故障

**故障定位**：持续型故障——tc netem reorder 规则是状态型故障，规则存活即故障存活，
贯穿整个故障窗口；窗口结束实验销毁/定时删除即自动恢复。手段1（ChaosBlade）与
手段2（kubectl-native）是**并列的注入手段**，底层效果完全等价（blade 内部也是下发
同一条 netem reorder 规则），按环境能力选用：集群装有 ChaosBlade 且 `pod-network`
提供 reorder action → 可用手段1（实验 UID 统一生命周期管理）；否则用手段2。
`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

**故障现象**：
1. TCP 连接出现乱序（out-of-order）事件，重排序消耗额外 CPU 并可能触发伪重传
2. 依赖报文顺序的应用协议（HTTP/2、gRPC 流、部分消息中间件客户端）出现超时、重试或帧错序错误
3. 网络延迟抖动增大（乱序必然伴随 delay 基底，被乱序的包会延迟 `--time` 毫秒后才发出）
4. 监控系统可见 TCP `TCPOFOQueue`/`OutOfOrder` 类计数增长，应用 P99 延迟抬升

**资源准备**：
1. 确认目标应用已正常运行，且有活跃的网络通信流量
2. 确认目标 Pod 的标签选择器、命名空间，以及**实际容器名**（临时容器 `--target` 必须填容器名，
   填 Pod 名/服务名会被 API server 拒绝：`targetContainerName: Not found`）
3. **能力探测（决定手段选择）**——确认 ChaosBlade 的 `pod-network` 是否提供 reorder action，
   **以 `-h` 实际输出为准**：
   ```bash
   blade create k8s pod-network -h
   ```
   - Available Commands 列出 `reorder` → 手段1 可用
   - 仅有 `dns/drop/occupy` → 该 blade 构建不提供 reorder，用手段2
4. 手段2 额外前提：
   - 节点内核支持 netem——**权威判据是功能探测，不是 grep 模块列表**：
     - grep 只能作辅证：`kubectl exec <pod-name> -n <namespace> -- grep sch_netem /proc/modules`。
       **有输出 → 必可用；无输出 ≠ 不可用**（存在 sch_netem 未加载、/sys/module 无对应目录，
       但持 NET_ADMIN 的载体内建 netem qdisc 成功的形态——模块可能内建或首次使用时被按需加载，
       不得因 grep 无输出就放弃手段2）
     - 功能探测（在持有 NET_ADMIN 的共享 netns 载体内对 dummy 接口试建 netem，不碰业务网卡）：
       路径A 在目标容器内、路径B 用临时容器执行同一条命令：
       ```bash
       sh -c 'ip link add dtest0 type dummy && tc qdisc add dev dtest0 root netem delay 1ms \
              && echo NETEM_AVAILABLE || echo NETEM_UNAVAILABLE; \
              tc qdisc del dev dtest0 root 2>/dev/null; ip link del dtest0 2>/dev/null; true'
       ```
       输出 `NETEM_AVAILABLE` → 内核支持；`NETEM_UNAVAILABLE` 或注入时报
       `RTNETLINK answers: Operation not supported` / `No such file or directory` /
       `Error: Specified qdisc kind is unknown.` → 内核不支持，立即停止并 replan
   - 若走临时容器路径：确认集群**能拉取**一个含 iproute2 的镜像（CNI 镜像如 terway/calico/cilium 通常自带；
     用 `kubectl get pods -A -o jsonpath='{{..image}}'` 找集群已在用的）

**演练步骤**：
1. 记录注入前基线：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -- cat /proc/net/dev
   ```

**手段1（ChaosBlade）**

2. 注入网络包乱序：
   ```bash
   blade create k8s pod-network reorder \
     --namespace <namespace> \
     --names <pod-name> \
     --interface eth0 \
     --percent 50 \
     --correlation 50 \
     --gap 2 \
     --time 20 \
     --timeout <duration>
   ```
   参数含义（`--interface`、`--percent`、`--correlation` 必填）：
   - `--percent`：被乱序的报文百分比（如 50）
   - `--correlation`：乱序概率的相关性百分比（如 50）
   - `--gap`：每间隔多少个包触发一次乱序（如 2）
   - `--time`：延迟基底毫秒数——乱序包会被延迟该时长后发出（如 20，对应 `delay 20ms`）
   - `--timeout`：到期自动恢复（秒）
3. 记录返回的 experiment_uid，用于后续恢复

**手段2（kubectl-native）** —— 按容器内是否有可用的 iproute2 `tc` 二选一

**路径 A —— 容器内有 iproute2 tc**

先判定容器内的 `tc` 是不是真的能用——**只看命令是否存在会误判**：
```bash
kubectl exec <pod-name> -n <namespace> -- tc -Version
```
- 输出 `tc utility, iproute2-<版本>` → 是真 tc，可走本路径（还需容器有 NET_ADMIN，`CapEff` 全零会报 EPERM）
- 输出 `BusyBox v<版本> ...` 或命令不存在 → 走路径 B（精简镜像的常态：`/bin/tc` 与 `/bin/sh` 同为 BusyBox
  二进制，`command -v tc` 一样返回成功，执行 netem 时报 `invalid argument ... to 'command'`）

注入命令（**先武装定时自删，再注入规则**；两条命令分两次独立执行——不能用 && 串联：
第二段 kubectl 会沦为第一条 exec 载荷（sh -c）的死参数，注入静默丢失）：
```bash
kubectl exec <pod-name> -n <namespace> -- sh -c \
  '( sleep <duration>; tc qdisc del dev eth0 root ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -n <namespace> -- tc qdisc add dev eth0 root netem delay <time>ms reorder <percent>% <correlation>% gap <gap>
```

**路径 B —— 容器内没有可用 tc（精简镜像的常态）：临时容器载体**

临时容器与目标容器**共享同一个网络命名空间**，在其内对 `eth0` 操作等价于操作目标 Pod 的网卡；
`tc` 来自调试镜像而非目标镜像，`--profile=netadmin` 提供 NET_ADMIN capability。

```bash
# 0) 前置安全检查：确认目标 Pod 不是 hostNetwork。hostNetwork=true 的 Pod
#    其网络命名空间【就是宿主机】，临时容器里的 tc 会打穿整个节点。
#    为 true 时禁止此路径，改用 node 级用例。
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.hostNetwork}'
# 期望输出为空或 false；输出 true 则停止。

# 1) 注入 + 内置定时自删（一条命令完成：注入成功后 sleep <duration> 到期自动删除规则）。
#    --target 必须填实际容器名；--quiet 不进入交互附着，不要加 -it。
kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
  --target=<container-name> --profile=netadmin --quiet -- sh -c \
  'tc qdisc add dev eth0 root netem delay <time>ms reorder <percent>% <correlation>% gap <gap> \
   && tc qdisc show dev eth0 && echo INJECTED && sleep <duration> \
   && tc qdisc del dev eth0 root && tc qdisc show dev eth0 && echo RECOVERED'
```
链条是**自证的**：`add` 后、`INJECTED` 前的 `tc qdisc show` 把生效规则原文写入容器日志，
`del` 后、`RECOVERED` 前的 `tc qdisc show` 把回落后的默认 qdisc 写入同一日志——白盒主证
在注入时刻即被捕获，验证阶段读日志即可取证，**不再受故障窗口是否已关闭的时序约束**
（窗口短于验证启动延迟时，窗口内探针结构性不可达；链内快照消除此竞态）。两处 show 必须保留，
不得为缩短命令而省略（注意 wiz 等通道 sh -c 载荷有 1024 字节上限，本链含占位符约 350 字节，安全）。
倒计时从武装时刻起算：注入成功（INJECTED 回显）与倒计时起点在同一条命令链内严格串行，无侵蚀间隙；武装后发生任何修复需重武装时，先用下方提前恢复命令另起临时容器删规则（旧链到期后的重复删除是幂等空触发、无害），再重跑注入命令重新武装+注入（见 SKILL.md 安全红线「故障窗口完整」）
- 该命令整体阻塞 `<duration>` 秒（命令自身就是保活载体，自恢复随命令完成而闭环）；
  如需后台执行，将整条 kubectl debug 置于后台并轮询其输出/临时容器日志
- 输出未直接回流时，从临时容器日志读取（`INJECTED`/`RECOVERED` 标记即注入/自恢复的确证；
  标记之间的 `tc qdisc show` 输出即白盒主证原文）：
  ```bash
  kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.ephemeralContainers[-1].name}'
  kubectl logs <pod-name> -n <namespace> -c <ephemeral-container-name>
  ```

手段2 参数含义（两条路径相同）：
- `delay <time>ms`：乱序的延迟基底（与手段1 `--time` 对应）
- `reorder <percent>% <correlation>%`：乱序比例与相关性（与手段1 `--percent`/`--correlation` 对应）
- `gap <gap>`：乱序间隔（与手段1 `--gap` 对应）
- `eth0`：网络接口名，按 `ip link show` 实际输出调整
- **内核级依赖**：netem 需要宿主机内核的 sch_netem 模块（容器与宿主共享内核，换载体改变不了）。
  可用性以资源准备第 4 条的 dummy 功能探测为权威判据（grep /proc/modules 无输出不构成否定证据）。
  注入报 `RTNETLINK answers: Operation not supported`、`RTNETLINK answers: No such file or directory`
  或 `Error: Specified qdisc kind is unknown.`（RC=2）即内核不支持的确证——立即停止，
  **不要重试、不要换 Pod 或重建临时容器**，发起 replan 并附报错证据；
  也**不要**试图去基础设施容器（如 CNI policy 容器）里 modprobe 加载模块——那是目标范围外的
  变更，会被 target_guard 以 namespace 漂移拦截
- **注入报 `File exists`**：网卡上已有残留 qdisc（典型成因：上一次演练的自删链未走完/被中断）。
  处置：先 `tc qdisc show dev eth0` 取证残留规则，若正是同类 netem 规则且处于
  有效故障窗口，改用 `tc qdisc replace dev eth0 root netem delay <time>ms reorder <percent>% <correlation>% gap <gap>`
  （replace 幂等，替换而非叠加）；若是无关/过期残留，先 `tc qdisc del dev eth0 root` 清掉再
  重新武装注入。**不要盲目重试 `add`**，它只会反复报同样的错

**注入验证**（两种手段共用——底层是同一条 netem 规则）：
1. 白盒确认 netem 规则已生效。**手段2 路径B 的首选证据是注入容器日志**——链内 `tc qdisc show`
   快照在注入时刻已捕获，直接读日志取证（无窗口时序约束，验证阶段晚于窗口关闭也同样有效）：
   ```bash
   kubectl logs <pod-name> -n <namespace> -c <注入 ephemeral 容器名>
   ```
   `INJECTED` 之前应显示 `qdisc netem ... delay <time>ms reorder <percent>% <correlation>% gap <gap>`，
   `RECOVERED` 之前应显示回落后的默认 qdisc。路径A 或需要**当前**规则状态时，在与目标容器
   **共享网络命名空间**的载体内执行 `tc qdisc show`。
   目标容器自带的 `tc` 常是 BusyBox applet（`tc -Version` 输出 BusyBox 即不可用，报
   `invalid argument ... to 'command'`），推荐用临时容器载体（镜像含 iproute2，`--profile=netadmin`
   提供查询所需的能力；`--target` 必须填实际容器名）：
   ```bash
   kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
     --target=<container-name> --profile=netadmin --quiet -- tc qdisc show dev eth0
   ```
   若输出未直接回流，从临时容器日志读取：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.ephemeralContainers[-1].name}'
   kubectl logs <pod-name> -n <namespace> -c <ephemeral-container-name>
   ```
   应显示 `qdisc netem ... delay <time>ms reorder <percent>% <correlation>% gap <gap>`
2. 业务侧验证——在目标 Pod 内发起对下游的请求，确认可达但延迟/重试异常：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <目标服务地址>
   ```
3. 查看应用日志确认出现超时/重试类记录：
   ```bash
   kubectl logs <pod-name> -n <namespace> --tail=30
   ```
4. 确认 Pod 状态仍为 Running、无 RESTARTS（netem 不打断连接，只劣化传输）
5. **持续性检查（必做）**——netem 是状态型故障，规则存活即故障存活：白盒主证为
   `tc qdisc show dev eth0` 仍显示 `netem ... reorder` 规则（手段1 实验未 destroy 且未到
   `--timeout`；手段2 路径A 武装定时器未走完）；路径B 以日志为准：`INJECTED` 已现而
   `RECOVERED` 未现即窗口仍开（链条严格串行，两标记之间的规则快照即生效主证）。有界佐证
   为静观短窗口后请求仍现延迟抖动/重试。若窗口内提前恢复，说明故障窗口契约未达成，
   必须如实报告实际持续时长

**注入恢复**：

手段1（ChaosBlade）：
1. 提前恢复：销毁实验（移除 netem 规则）`blade destroy <experiment_uid>`
2. 或等待 `--timeout`（`<duration>`）到期后 ChaosBlade 自动恢复。
   注意：到期后 `blade status <uid>` 可能仍显示 `Success` 不翻状态，**不要以 status 判断规则是否还在**，
   以白盒 `tc qdisc show` 为准

手段2（kubectl-native）：
1. 主保险：路径A 武装的定时器 / 路径B 内置自删链到期自动移除规则
2. 提前恢复：
   - 路径A：`kubectl exec <pod-name> -n <namespace> -- tc qdisc del dev eth0 root`
     （`tc qdisc del` 对已删除的规则报 `RTNETLINK answers: No such file or directory`，无害但需预期）
   - 路径B：另起一个临时容器执行删除，与注入容器无关——netem 规则挂在共享 netns 的网卡上，
     任何共享该 netns 的载体都能删：
     ```bash
     kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
       --target=<container-name> --profile=netadmin --quiet -- sh -c \
       'tc qdisc del dev eth0 root && echo EARLY_RECOVERED && tc qdisc show dev eth0'
     ```
     随后注入容器里的定时器到期再删一次，规则已不存在，删除静默失败无害

**恢复验证**（两种手段共用）：
1. 用注入验证第 1 条的同一路径确认规则已移除：
   ```bash
   kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
     --target=<container-name> --profile=netadmin --quiet -- tc qdisc show dev eth0
   ```
   应恢复为默认 qdisc（形态如 `qdisc noqueue 0: root refcnt 2`，也可能是 `pfifo_fast`/`fq_codel`），
   不再显示 netem
2. 确认 Pod 无 RESTARTS、应用响应时间恢复基线

**基准事实**：
- **根因**：Pod 网络接口上的 tc netem reorder 规则按注入概率延迟重排出站报文，接收端 TCP 栈观察到乱序并触发重排序/伪重传
- **必现现象**：`tc qdisc show` 出现 `netem ... reorder <percent>%` 规则；延迟抖动增大（基底为 `--time` 毫秒）；TCP 乱序计数增长；应用层超时/重试增多但连接不断；窗口结束规则移除后恢复
- **blade 可用性因构建而异**：`pod-network reorder` 并非所有 blade 发行版都提供（官方 v1.8.0 构建含 netem 全家桶 reorder/corrupt/duplicate/delay/loss，某定制发行 v1.8.5 仅 dns/drop/occupy）——以 `blade create k8s pod-network -h` 的 Available Commands 为准，没有 reorder 就用手段2
- **netem 可用性以功能探测为准**：节点 `/proc/modules` 与 `/sys/module` 均无 sch_netem 痕迹时，持 NET_ADMIN 的临时容器内 dummy 接口功能探测仍可返回 `NETEM_AVAILABLE`，注入成功（白盒 `qdisc netem ... reorder 25% 25% gap 2` 生效、窗口到期自删回落 `noqueue`）——grep 无输出只说明模块未显式加载，不得作为放弃手段2 的依据

**手段2 注意事项**：
- **`--` 之后是裸 argv 直通，无 shell 解释**：多个命令、`;`、`&&` 不得直接拼在 `--` 后
  （首 token 会被当成含 `;` 的可执行文件名，容器立即 exit 255）——整条链必须包进单个
  `sh -c '<完整链>'` 作为 `--` 的单一参数
- **临时容器本身无法从运行中的 Pod 移除**（Kubernetes 既定行为），只能随 Pod 重建消失；
  `tc qdisc del` 成功即代表故障已恢复，残留临时容器不影响业务容器
- 自恢复基于武装的定时删除（路径A）或注入命令内置的 `sleep <duration>` 自删链（路径B）；
  提前恢复用上方手动删除命令
- 效果与手段1 完全等价——blade 底层就是下发同一条 netem reorder 规则
