**用例名称** 报文重复 导致 Host_网络故障

## 故障定位
持续型故障——tc netem duplicate 规则下到主机网卡后持续按比例重复发送出向报文，规则存活即故障存活，贯穿整个故障窗口；窗口结束（`--timeout` 到期实验销毁 / 武装的自恢复载体到期 `tc qdisc del` 删除规则）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（blade 底层也是下发同一条 netem duplicate 规则），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）且 `blade create network duplicate --help` 存在 duplicate action → 手段1（实验 UID 统一生命周期管理）；否则手段2。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

**判据特性（本用例属弱行为判据族，见 `## 注入验证` 判据可达性 caveat）**：报文重复的行为级证据被协议栈**条件性吸收**——TCP 接收端按序列号去重，重复段被丢弃或合并，应用层通常**看不到重复数据**；只有 UDP / 无去重逻辑 / 应用层幂等性缺失的路径才暴露可见的重复数据（重复日志、重复交易、计数翻倍）。故本用例主判据收敛为**机制归因白盒**（`tc qdisc show` 见 duplicate 规则文本，稳定可达），行为级证据（线上重复包捕获 / UDP 应用重复）为条件性补充。

**爆炸半径（主机网络 QoS 头号纪律）**：netem duplicate 作用在主机**真实业务网卡**上，无差别施加会连 SSH 控制与恢复通道自身一起重复报文，导致观测失真甚至恢复通道拥塞（区别于容器内独立 netns 天然隔离的形态）。故注入必须收窄到目标流量：手段1 用 blade matcher（`--destination-ip <target-ip>` 只作用到目标的流量，或 `--exclude-port 22` 排除 SSH 控制通道）；手段2 优先用 tc filter 把 netem 收窄到目标（见文末「手段2」收窄形态），无法收窄时须显式接受「主机级 egress 全重复」波及面并武装定时自删作恢复安全网。

## 故障现象
1. 主机出方向到匹配目标的报文按比例被重复发送
2. TCP 场景：接收端按 seq 去重，应用层通常看不到重复数据，但重复包占用带宽、可能触发 ACK 风暴 / 吞吐抖动
3. UDP / 无去重场景：应用收到重复数据报，出现重复日志 / 重复交易 / 计数翻倍 / 幂等性缺陷暴露
4. 模拟链路层重传风暴 / 中间设备重复转发 / 应用幂等性缺失场景

## 资源准备
1. 确认目标主机网卡名（**不硬编码 eth0**）：`ip route get <target-ip>` 看 `dev` 字段，或 `ip -o link` 列网卡
2. 确认目标主机 `tc` 可用且为 iproute2（非 BusyBox）：`tc -Version` 输出 `tc utility, iproute2-<版本>`
3. **内核级 netem 依赖 go/no-go（功能探测，不是 grep 模块列表）**：netem 由宿主机内核 sch_netem 提供，可用性随内核异质；grep `/proc/modules`、`/lib/modules`、`modinfo` 均不可靠（模块可能内建或按需加载，**grep 无输出 ≠ 不可用**）。权威判据是对 dummy 网卡真跑一次 netem duplicate（**零触碰业务网卡、探测产物用后即删**）：
   ```bash
   ip link add dtest0 type dummy && tc qdisc add dev dtest0 root netem duplicate 1% \
     && echo NETEM_AVAILABLE || echo NETEM_UNAVAILABLE
   tc qdisc del dev dtest0 root 2>/dev/null; ip link del dtest0 2>/dev/null; true
   ```
   输出 `NETEM_AVAILABLE` → go；`NETEM_UNAVAILABLE`，或注入时报 `RTNETLINK answers: Operation not supported`、`RTNETLINK answers: No such file or directory`、`Error: Specified qdisc kind is unknown.`（RC=2）→ 内核不支持，**立即停止并 replan**（换目标 / 换网卡都无效，内核是同一个），不得据 grep 无输出就放弃手段2
4. 记录重复判据的对照锚点（见演练步骤 1）

## 演练步骤
1. 记录当前基线（对目标 IP 的连通性基线，正常应无重复包）：
   ```bash
   ping -c 20 <target-ip>
   ```
   记下基线 rtt 与丢包（正常应 0%、无 duplicates）
2. 注入报文重复 —— **手段1（ChaosBlade）**（主机无 blade 或 blade 无 duplicate action 时改用文末**手段2（原生命令）**）
   ```bash
   blade create network duplicate --percent <N> --interface <if> --destination-ip <target-ip> --timeout <duration>
   ```
   参数说明：
   - `--percent`：重复率百分比（**必填**，正整数，如 10）
   - `--interface`：目标网卡名（**必填**，取资源准备第 1 条探测结果，不硬编码 eth0）
   - `--destination-ip`：目的 IP 收窄（**爆炸半径纪律**，只作用到目标的流量）
   - `--exclude-port`：排除本地端口（如 `--exclude-port 22` 保护 SSH 控制通道；**与 `--local-port`/`--remote-port` 互斥**）
   - `--local-port` / `--remote-port`：按本端 / 对端端口收窄
   - `--timeout`：到期自动恢复（秒）
   记录返回的 experiment_uid，用于后续恢复
3. 观察报文重复及应用状态

## 注入验证
1. （诊断，仅当下述证据未出现时执行）确认实验已生效：
   ```bash
   blade status --uid <experiment-uid>
   ```
   状态为 Success/Running 即表示 netem duplicate 规则已下到主机网卡——这是机制回执，为机制代言不为结果代言，用于定位失败层（规则未下发 vs 下发未生效），不构成效果证据
2. **（主证据·机制归因白盒，必做）** 确认 netem duplicate 规则文本已挂载，并在同一快照内读到该 netem 子 band 的包计数器（本弱行为判据族的**稳定可达主判据**）：
   ```bash
   tc -s qdisc show dev <if>
   ```
   输出应包含 `qdisc netem ... duplicate <N>%`；收窄形态下 netem 挂在 prio 根的**子 band**（`qdisc netem <handle>: parent 1:<band> ... duplicate <N>%`）而非 root，语义签名 = netem 在 prio 子 band ＋ `duplicate` 参数==注入值 ＋（若 u32 收窄）filter flowid 指向该子 band；band 号/handle 由手段与 iproute2 版本渲染决定（手段2 原生 `prio` 默认 3 bands 渲染 `parent 1:3`/`handle 30:`；手段1 blade 1.8.2 形态 `bands 4` prio 根渲染 `parent 1:4`/`handle 40:`），属渲染差异而非机制偏离——规则文本在位即重复机制已挂载，不依赖被协议栈吸收的行为级证据。取 `-s`（统计视图）而非裸 `tc qdisc show` 的意义：`-s` 在同一份输出里额外渲染每个 qdisc/子 band 的 `Sent <bytes> <pkt> (dropped …)` 计数器，netem 子 band 的 `pkt > 0` 是「匹配流量确实被分类进了这条 netem band」的白盒实证，闭合「规则文本在位但无流量进入该 band」的归因缺口——对本弱行为判据族（应用层重复数据被 TCP 去重条件性吸收、行为级证据常不可达）而言，这条包计数器是窗内最硬的稳定可达实证。该子 band 随注入自恢复到期被拆除、计数器一并消失，故窗内第一批快照的 `tc -s` 是能读到它的时机；恢复之后再读只会得到回落后的默认 qdisc（`noqueue`，`Sent 0 bytes 0 pkt`），那属于 unobserved 而非 absent，不能反读为「netem 未承载流量」
3. **（行为级证据·条件性）** 线上重复包 / 应用层重复——仅在未被 TCP 去重吸收的路径上稳定可见：
   ```bash
   tcpdump -ni <if> -c 200 host <target-ip>
   ```
   可见同一 seq / payload 的报文重复出现（TCP 场景接收端会去重，但线上仍能捕获到重复发送的包）；UDP / 无去重场景可见应用层重复数据（重复日志 / 计数翻倍）。**若应用全程无可见重复、仅线上捕获到重复包，属 TCP 去重吸收的预期，不是注入失败**（见下方判据可达性 caveat）。**`tcpdump` 属工具面非普适探针**（最小镜像默认不装，缺失即 `which` 空 / `exit 127`），而 `ping` 普适在场——ICMP echo 无接收端 seq 去重（不同于 TCP），收窄目标为可 ping 地址时 `ping` 直接报 `+N duplicates` / `(DUP!)`，是普适在场的行为级重复签名；它是条件性佐证而非主判据，tcpdump 缺失时以 `ping` 的 duplicates 计数作等价佐证即可，不得反复重试或据此判注入失败，主判据白盒 `tc -s qdisc show`（普适在场）已充分。
4. 检查应用日志是否出现重复处理 / 幂等性相关错误

> **判据可达性（duplicate 效果被协议栈条件性吸收）**：
> - **① 陷阱与不可达原因**：报文重复的**应用层重复数据**判据在 TCP 路径上**条件性不可达**——TCP 接收端按序列号去重，重复段被丢弃 / 合并，应用层收到的是去重后的数据流，看不到「重复的数据」；只有 UDP / 无去重逻辑 / 应用层幂等性缺失的路径才暴露可见重复。故不得把「应用收到重复数据」列为无条件主判据。
> - **② 可达的替代主判据**：**机制归因白盒** `tc -s qdisc show dev <if>` 见 `netem ... duplicate <N>%` 规则文本＋该子 band `Sent … pkt > 0`（步骤 2，稳定可达、不依赖协议栈是否吸收）为主判据；行为级以 **ICMP 路径 `ping` 的 duplicates 计数**（ICMP echo 无接收端 seq 去重，收窄目标为可 ping 地址时普适在场、直接报 `+N duplicates` / `(DUP!)`）或**线上重复包捕获**（`tcpdump` 见同 seq/payload 重复出现，未被接收端去重前）为条件性佐证，UDP / 无去重场景另可直接观测应用重复。验证落在**匹配收窄条件的目标**上，用其它地址 / 端口测试必然无重复，**不要换目标反复重试**。

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的定时器——Tier-1 `systemctl stop` / Tier-2 `pkill` 括号法——再 `tc qdisc del dev <if> root` 删除注入的 netem 规则）

## 恢复验证
1. 确认 netem 规则已清除：
   ```bash
   tc qdisc show dev <if>
   ```
   输出应不再包含 netem 规则（回落默认 qdisc / `noqueue`；收窄形态下 prio 根与 netem 子类随 `tc qdisc del dev <if> root` 一并拆除）
2. 对目标测连通性（与演练步骤 1 **同一探针、同口径**）：
   ```bash
   ping -c 20 <target-ip>
   ```
   确认恢复回**演练步骤第 1 步记录的基线水平**（无重复包 / rtt 回落）
3. 确认应用日志不再出现重复处理错误

## 基准事实
- **根因**：通过 tc netem 在主机网卡注入出方向按比例报文重复，模拟链路重传风暴 / 中间设备重复转发 / 应用幂等性缺失环境
- **必现现象**：`tc qdisc show dev <if>` 显示 netem duplicate 规则（稳定可达主判据）；线上可捕获重复包；UDP / 无去重场景应用重复数据 / 计数翻倍
- **判据特性**：属弱行为判据族——应用层重复数据被 TCP 去重条件性吸收，主判据收敛为机制归因白盒（`tc qdisc show` 见 duplicate 规则）+ 线上重复包条件性佐证，携带判据可达性 caveat
- **作用域边界**：仅影响匹配收窄条件（`--destination-ip` / 端口 / tc filter）的流量；`--exclude-port 22` 时 SSH 控制通道不受影响
- **爆炸半径**：主机 netem 作用在真实业务网卡，无差别施加会波及 SSH 控制 / 恢复通道自身，必须收窄到目标流量；无法收窄时须以武装的定时自删作恢复安全网
- **netem 可用性以功能探测为准**：grep `/proc/modules`、`modinfo` 查不到 sch_netem 痕迹不等于内核不支持，必须用 dummy 网卡功能探测定论（见资源准备第 3 条）；探测报不支持即收工，换目标 / 换网卡都改变不了（内核是同一个）

## 注意事项
- **弱行为判据族**：报文重复的应用层重复数据被 TCP 去重吸收，验证不得据「应用没收到重复数据」判失败；主判据是白盒 `tc qdisc show` 见 duplicate 规则 + 线上重复包条件性佐证（见 `## 注入验证` 判据可达性 caveat）
- 重复为概率性效果，行为级观测样本量须足够（`tcpdump -c 200` 而非几个包）；小样本下看不到重复包可能是抽样噪声。**注意 `tcpdump` 工具面非普适**（最小镜像默认不装，缺失即 `which` 空 / `exit 127`）：缺失时改用普适在场的 `ping`（ICMP echo 无 seq 去重，直接报 duplicates 计数）作行为佐证，不否定白盒主判据 `tc -s qdisc show`，不得反复重试
- 与 `网络丢包`(loss) / `报文损坏`(corrupt) 分界：loss 是整包丢弃、corrupt 是包内比特损坏、duplicate 是整包重复发送；三者机制不同、判据不同，不得互相套用
- 报文重复常用于验证应用**幂等性**——UDP / 无去重路径下的重复数据是本用例最能暴露业务缺陷的场景

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（`tc qdisc add dev <if> root netem duplicate` 下发同一条 netem duplicate 规则）；主机未装 ChaosBlade 或 blade 构建不提供 duplicate action 时选用本手段。

前提条件：具备 root 权限执行 `tc`（iproute2，非 BusyBox，见资源准备第 2 条）；内核支持 netem（见资源准备第 3 条 dummy 网卡功能探测 go/no-go）；自恢复载体需 `systemd-run`（Tier-1，主机有 systemd）或 `setsid`+`sh`+`sleep`（Tier-2，主机无 systemd），二者皆不可用则 fail-closed（见标准件文档 §七）。

注入命令（**先武装定时恢复，再注入**——武装载体按主机是否有 systemd 分层选型〔探测门 → Tier 选择 → 武装〕，构造细节 / 纪律 / 取证约束见 `references/carrier/recovery-carrier.md`〔host 域分层自恢复载体单一权威，本 case 只引用不内嵌〕；`<recovery-seconds>` 一律取 prompt 下发的 `recovery_timer_seconds`〔= duration + grace，见 SKILL.md 双数窗口契约〕，Tier-1 的 `--on-active` 与 Tier-2 的 `sleep` 同源取此值，禁自算缓冲）：
```bash
# 0) 只读探测 systemd 是否在场（任一信号为真即判在场，degraded 仍算在场，不因 degraded 降级）：
systemctl is-system-running          # 只读；bus 可达即 systemd 在场
# 或只读判据： [ -d /run/systemd/system ]
#   → 在场选 Tier-1（systemd-run transient timer）；不在场（两信号皆否，如最小容器 / 非 systemd init 裸机）
#     选 Tier-2（setsid detached-timer）；两 Tier 前置皆不可用（systemd 不在场 且 setsid/sh/sleep 任一缺失）
#     → fail-closed：显式报告「本环境无可用自治恢复载体」及缺失项，禁止注入无安全网的自定时故障（见标准件文档 §七）。
#   下面给出 Tier-1 / Tier-2 两种武装形态，按探测结果二选一；武装成功后（Tier-1 输出含 "Running timer as unit"、
#   Tier-2 即刻返回且 ps 见 PPID=1 detached 会话）再执行注入。各命令独立执行（执行通道不支持在工具调用层 && 串联）。
#   逆命令 `tc qdisc del dev <if> root` 对下方两种注入形态（收窄 / 裸 root）同源有效——一条即拆掉整棵 qdisc 树。

# Tier-1（systemd 在场）：
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-netem-duplicate \
  tc qdisc del dev <if> root
# Tier-2（systemd 不在场；逆命令已在外层 sh -c 内，直接写、禁再嵌套 sh -c——payload 禁嵌套 interpreter）：
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && tc qdisc del dev <if> root'

# 武装成功后再注入重复规则，二选一：
# 【优先·收窄形态】用 prio 根 + netem 子类 + u32 dst filter，把重复限定到目的 <target-ip> 的流量，
#   其余流量（含 SSH:22）走默认 band 不被重复——满足爆炸半径收窄纪律：
tc qdisc add dev <if> root handle 1: prio
tc qdisc add dev <if> parent 1:3 handle 30: netem duplicate <N>%
tc filter add dev <if> protocol ip parent 1:0 prio 3 u32 match ip dst <target-ip> flowid 1:3
# 【备选·裸 root 形态】无收窄需求或靶机 tc 不支持 filter 时：作用整块网卡 egress（会波及 SSH 控制通道），
#   须确保上方定时自删安全网已武装，并在故障定位显式标注「主机级 egress 全重复」波及面：
# tc qdisc add dev <if> root netem duplicate <N>%
```

恢复命令（timer 到期前可提前手动恢复，同时停掉已武装的定时器——按武装时选用的 Tier 停对应定时器）：
```bash
# 提前恢复：删除本次注入的 netem 规则（对收窄 / 裸 root 两形态同源有效，一条拆整棵树）
#   Tier-1：systemctl stop blade-restore-netem-duplicate.timer 2>/dev/null
#   Tier-2：pkill -f 'sleep <recovery-seconds>' 用括号法防自匹配（如 pkill -f 'sleep 42[0]'，见标准件文档 §五）
tc qdisc del dev <if> root
```

注意事项：
- netem duplicate 规则立即生效，已建立的 TCP 连接的后续包即按比例重复发送
- **爆炸半径**：裸 `tc qdisc add dev <if> root netem duplicate` 会重复整块网卡 egress（含 SSH:22）；优先用上方 prio+u32 filter 收窄形态把重复限定到目标流量，无法收窄时须确保定时自删安全网在位并在故障定位标注波及面
- 自恢复基于注入前武装的**分层自恢复载体**（Tier-1 主机有 systemd 时 systemd-run transient timer / Tier-2 主机无 systemd 时 setsid detached-timer；选型探测门、武装 / 重武装纪律、payload 文法〔Tier-2 禁嵌套 interpreter，逆命令直接写在外层 sh -c 内〕、fail-closed 边界、Tier-2 取证约束见 `references/carrier/recovery-carrier.md`），到期自动 `tc qdisc del dev <if> root` 删除 netem 规则，补齐了 ChaosBlade `--timeout` 的自恢复能力；提前恢复仍用上方手动命令
- `tc qdisc del` 对已删除的规则报 `RTNETLINK answers: No such file or directory`，无害但需预期（幂等容忍，见标准件文档 §六）——定时器自治 fire 与框架提前兜底双执行时，第二次 `del` 报此错属预期，不得据此判恢复失败
- Tier-2 payload 守 discard-sink 不变量、**不写任何取证文件**，故无载体侧 fire 日志属预期取舍（见标准件文档 §八）；fire 是否发生由框架侧只读观测靶标状态差承担（如 `tc qdisc show dev <if>` 由 netem 回落默认 qdisc），不得据此误判武装失败
- 武装与注入分两次独立执行，须确认武装成功后再执行注入（等价于 `&&` 串联的失败短路保证）
- 载体 unit 名取非故障家族词根（`blade-restore-netem-duplicate`）以符合 host 段分层放行；Tier-1 同名 transient timer 重复武装会报 `Unit blade-restore-netem-duplicate.service was already loaded`（上次武装失败时 unit 以 failed 状态残留所致），重武装前先清理残留：`systemctl stop blade-restore-netem-duplicate.service; systemctl reset-failed blade-restore-netem-duplicate.service`；Tier-2 重武装先 `pkill -f 'sleep <recovery-seconds>'`（括号法）终止旧会话再重新 setsid（见标准件文档 §五）
- 效果与手段1 完全等价——blade 底层同样下发「prio 根 + netem 子类 + u32 dst filter」把重复收窄到目标；差异仅在 prio 根 band 数的渲染（本手段用 iproute2 默认 3 bands，netem 挂 `parent 1:3`；blade 1.8.2 形态 `bands 4`，netem 挂 `parent 1:4`），故注入验证的机制判据认「netem 在子 band ＋ 效果参数==注入值」这一语义签名，band/handle 的具体数值属渲染差异
