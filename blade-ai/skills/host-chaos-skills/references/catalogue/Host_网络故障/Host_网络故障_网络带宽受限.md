**用例名称** 网络带宽受限 导致 Host_网络故障

## 故障定位
持续型故障——tc tbf（令牌桶过滤器）规则下到主机网卡后持续把出方向速率限到设定值，规则存活即故障存活，贯穿整个故障窗口；窗口结束（武装的自恢复载体到期 `tc qdisc del` 删除规则）即自动恢复。

**手段适用性诚实契约（本用例区别于本域其它 6 形态的关键）**：ChaosBlade `blade create network` **无带宽限速 action**（靶机 blade 1.8.2 的 9 个 action 为 `corrupt/delay/dns/dns_down/drop/duplicate/loss/occupy/reorder`，无 bandwidth/tbf；以 `blade create network --help` 探测为准，换版本须重测）。故本用例是**手段2-only**——只有原生 `tc tbf` 一条注入路径，**不存在手段1（ChaosBlade）**。**严禁据 blade 宣称注入或验证**：blade 无对应 model，任何「用 blade 注入带宽限制」的说法都是错的；注入与验证一律走 tc tbf。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

**tbf 只限出方向**：tbf 作用在网卡 egress（发送方向），入方向（下载）不受限——判据必须测**出向上传**，不能测下载。若业务是下载型流量，tbf 无法产生可观测现象，须换 ifb 重定向（多一层内核模块依赖，不在本用例范围）。

**判据特性（吞吐 ratio 经 host SSH 通道结构性不可达，见 `## 注入验证` 判据可达性 caveat）**：本用例最直观的行为级判据是「注入前 vs 注入后出向上传吞吐倍差」，但它是**跨主机流量测量**，依赖「另一台主机的接收端 sink ＋ 允许裸管道 `dd|nc` 的执行通道 ＋ 可计时」——这三条经 host SSH 传输通道结构性不成立（裸管道被 fail-closed 拒、nc/socat 常缺失、单主机通道无法武装跨主机 sink、自建本机 sink 走 lo 绕过目标网卡、host_read 不报 elapsed time）。故本用例经 host 通道的**主判据收敛为机制归因白盒** `tc -s qdisc show`（机制回执 ＋ 内核记账主动限速签名），吞吐 ratio 降为条件性佐证（仅 kubectl_exec 进 chroot-host 真 shell 等允许裸管道 ＋ 可预置跨主机 sink 的通道可达）。

**爆炸半径（主机网络 QoS 头号纪律）**：tbf 规则挂在主机**真实业务网卡的 root qdisc** 上，会限住该网卡**全部出向流量**——包括 SSH 控制与恢复通道自身（区别于容器内独立 netns 天然隔离的形态）。故注入必须遵守：①`rate` 不能设得过低，须保证 SSH 控制通道在限速下仍可操作（限速不等于阻断，SSH 变慢但不断）；②必须**先武装定时自删载体再注入**（到期自动 `tc qdisc del` 拆除 tbf，即使 SSH 变慢也能自治恢复）；③需按目标流量精确收窄时，须用 htb 分类层级 + u32 filter 把 tbf 挂到指定 class（比裸 root 复杂，属独立收窄形态，选用前须对该形态单独做功能探测坐实，本用例 prescribed 的是已坐实的裸 root tbf 形态并显式接受主机级 egress 全限速波及面）。

## 故障现象
1. 主机出方向网络吞吐被限到设定速率（如 1mbit），大数据量传输显著变慢
2. 小请求仍能正常返回（限速不影响单包时延），但批量上传 / 数据同步 / 日志外发耗时成倍增加
3. 应用出现「慢而不断」症状：健康检查通过、但数据同步 / 备份上传 / 日志上报堆积
4. 模拟跨地域专线拥塞、云厂商带宽包耗尽、共享宿主机带宽争抢场景

## 资源准备
1. 确认目标主机网卡名（**不硬编码 eth0**）：`ip route get <target-ip>` 看 `dev` 字段，或 `ip -o link` 列网卡
2. 确认目标主机 `tc` 可用且为 iproute2（非 BusyBox）：`tc -Version` 输出 `tc utility, iproute2-<版本>`（BusyBox 版不支持 tbf，执行会报错）
3. **内核级 tbf 依赖 go/no-go（功能探测，不是 grep 模块列表）**：tbf 由宿主机内核 sch_tbf 提供，可用性随内核异质；grep `/proc/modules`、`/lib/modules`、`modinfo` 均不可靠（模块可能内建或按需加载，**grep 无输出 ≠ 不可用**）。权威判据是在一块**既有 inert 接口**上真跑一次 tbf 再删（qdisc-kind 能否解析走内核全局能力、与接口是否承载业务无关，故零爆炸半径、用后即删）。**不用 `ip link add` 建 dummy 网卡**——mutating `ip` 不在 host 注入白名单（`ToolGuard` 拒 `Command not allowed: ip`），且执行通道不支持 `&&`/`||` 串联，故 `ip link add dtest0 type dummy && tc …` 形态在 host SSH 通道**结构性不可达**（详见 `references/criterion-reachability.md` M1 的关键陷阱）。改法：先只读发现一块既有非业务接口（优先 `tunl0`〔ipip 默认隧道、通常 inert〕，无则 `lo`；用只读 `ip -o link show` 或 `tc qdisc show` 列接口），再在它上面**两条命令各自独立执行**（无 `&&` 串联）：
   ```bash
   # 探测（burst 取 ≥ 该接口 MTU，见演练步骤 2 参数说明；探测用大 rate、只碰 inert 接口不碰业务网卡）：
   tc qdisc add dev <inert-if> root tbf rate 100mbit burst 65536 latency 400ms
   # 上一条 RC=0 → TBF_AVAILABLE；报 RTNETLINK answers: Operation not supported / Error: Specified qdisc kind is unknown.（RC=2）→ TBF_UNAVAILABLE
   # 探测后立即拆除（对已删规则报 No such file 属幂等容忍）：
   tc qdisc del dev <inert-if> root
   ```
   `TBF_AVAILABLE` → go；`TBF_UNAVAILABLE` → 内核不支持，**立即停止并 replan**（换目标 / 换网卡都无效，内核是同一个），不得据 grep 无输出就放弃
4. 确认目标主机是否有持续的**出方向**数据传输（对象存储上传、日志外发、数据同步等）：有则限速自然产生可观测现象；**无（如 sshd-only 空闲靶机）则不阻断演练**——注入验证 step 1 会在故障窗内主动发一段有界不可压缩 egress 负载（`dd if=/dev/urandom …`，见 `references/criterion-reachability.md` M1）把整形器 exercise 出来，使 enforcement 计数器可观测（详见 `## 注入验证` step 1「窗内主动 exercise」）
5. **（条件性·仅吞吐 ratio 判据可达通道需要）预置吞吐探测接收端 sink**：仅当执行通道允许裸管道 `dd|nc` **且**存在另一台可达主机时（如 kubectl_exec 进 chroot-host 真 shell），才需在注入前于该另一台主机起 `nc -l <port> >/dev/null`（或精简环境用 `socat TCP-LISTEN:<port> ...`）作为上传落点——listen 属「模式开关即写」，只能在资源准备阶段人工预置，**不得在 verify 阶段临时起**。**经 host SSH 传输通道本条不适用**：单主机通道无法武装跨主机 sink、且裸管道被 fail-closed 拒，此时吞吐 ratio 判据结构性不可达（见注入验证判据可达性 caveat），主判据走白盒 `tc -s qdisc show`，既不必也无法预置 sink
6. **（条件性·同第 5 条，仅吞吐 ratio 判据可达通道需要）** 记录限速前的出向吞吐基线（测法见注入验证步骤 2），否则无法判断限速倍差；经 host SSH 通道吞吐 ratio 不可达时，白盒主判据改以注入前的 `tc -s qdisc show dev <if>` 逐字节零基线快照（`Sent 0 bytes 0 pkt (dropped 0, overlimits 0 …)`）为对照，无需吞吐基线

## 演练步骤
1. **（条件性·仅吞吐 ratio 判据可达通道需要）** 记录当前出向吞吐基线（对预置 sink 发有界流量测上传速率，见注入验证步骤 2 的测法）；经 host SSH 通道吞吐 ratio 不可达时，改为记录注入前 `tc -s qdisc show dev <if>` 的逐字节零基线快照作白盒主判据对照
2. 注入带宽限制 —— **手段2（原生命令，tc tbf）**。本用例无手段1（blade 无 bandwidth action，见故障定位诚实契约）。**先武装定时自删载体，再注入 tbf 规则**（载体按主机是否有 systemd 分层选型〔探测门 → Tier 选择 → 武装〕，构造细节 / 纪律 / 取证约束见 `references/carrier/recovery-carrier.md`〔host 域分层自恢复载体单一权威，本 case 只引用不内嵌〕；`<recovery-seconds>` 一律取 prompt 下发的 `recovery_timer_seconds`〔= duration + grace，见 SKILL.md 双数窗口契约〕，Tier-1 的 `--on-active` 与 Tier-2 的 `sleep` 同源取此值，禁自算缓冲）：
   ```bash
   # 0) 只读探测 systemd 是否在场（任一信号为真即判在场，degraded 仍算在场，不因 degraded 降级）：
   systemctl is-system-running          # 只读；bus 可达即 systemd 在场
   # 或只读判据： [ -d /run/systemd/system ]
   #   → 在场选 Tier-1（systemd-run transient timer）；不在场（两信号皆否）选 Tier-2（setsid detached-timer）；
   #     两 Tier 前置皆不可用（systemd 不在场 且 setsid/sh/sleep 任一缺失）→ fail-closed：显式报告
   #     「本环境无可用自治恢复载体」及缺失项，禁止注入无安全网的自定时故障（见标准件文档 §七）。
   #   武装成功后（Tier-1 输出含 "Running timer as unit"、Tier-2 即刻返回且 ps 见 PPID=1 detached 会话）再执行注入。
   #   各命令独立执行（执行通道不支持在工具调用层 && 串联）。逆命令 `tc qdisc del dev <if> root` 一条即拆掉 tbf。

   # Tier-1（systemd 在场）：
   systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-tbf \
     tc qdisc del dev <if> root
   # Tier-2（systemd 不在场；逆命令直接写在外层 sh -c 内，禁再嵌套 sh -c——payload 禁嵌套 interpreter）：
   setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && tc qdisc del dev <if> root'

   # 武装成功后再注入 tbf 限速规则（裸 root 形态，限住整块网卡 egress，须已在故障定位接受波及面）：
   tc qdisc add dev <if> root tbf rate <rate> burst <burst> latency <latency>
   ```
   参数说明：
   - `rate <rate>`：目标速率，单位是 **bit/s** 而非 byte/s（如 `1mbit` ≈ 125 KB/s 上限）；**须保证 SSH 控制通道在此速率下仍可操作**（爆炸半径纪律）
   - `burst <burst>`：令牌桶容量，允许的瞬时突发量。**过小会导致达不到 rate**（包还没攒够令牌就被丢），经验值取 `rate/8` 上下（如 `rate 1mbit` 配 `burst 32kbit`）。**须 ≥ 目标设备 MTU**：`rate/8` 经验值在真实 NIC（MTU 1500）下得清洁令牌桶整形（延迟路径，`overlimits` 上升、`backlog` 非零）；但容器 veth 常见大 MTU（如 65535）＋内核 GSO 超级包下，`burst 32kbit`=4096B « 单包，会退化为入队拒绝（丢包路径，`dropped` 上升而 `overlimits` 停在 0）——仍限住了出向（属 tbf 生效），只是 enforcement 形态不同。如需清洁延迟整形，`burst` 须取 ≥ 设备 MTU（大 MTU 环境相应放大）；判据须认这两条 enforcement 路径（见注入验证步骤 1），**不得因 `overlimits` 未动就判限速未生效**
   - `latency <latency>`：包在队列里最长等待时间，超时即丢弃。**过小会退化成丢包而非限速**（如 `400ms`）
   - `dev <if>`：目标网卡（取资源准备第 1 条探测结果，不硬编码 eth0）
   - 只限**出方向**；入方向限速需 ifb 重定向，不在本用例范围
3. 观察应用的数据传输耗时与吞吐变化

## 注入验证
1. **（主证据·机制归因白盒，必做）** 确认 tbf 规则已挂载，并在同一快照内读到内核记账的主动限速签名（本用例经 host 观测通道的**稳定可达主判据**，见下方判据可达性 caveat）：
   ```bash
   tc -s qdisc show dev <if>
   ```
   语义签名 = ①**机制回执**：输出含 `qdisc tbf ... rate <注入速率> burst ... lat ...`（rate/burst/lat==注入值；handle/refcnt 的具体数值由 iproute2 版本渲染决定，属渲染差异非机制偏离）；②**主动限速实证**：取 `-s`（统计视图）而非裸 `tc qdisc show` 的意义在于它额外渲染 `Sent <bytes> <pkt> (dropped …, overlimits … requeues …)` 计数器——该计数器**离开注入前的逐字节零基线**（`Sent` 字节/包数上升）且 `dropped` **或** `overlimits` 至少一个移动，即「注入的 tbf 确实处理并约束了真实出向流量」的白盒实证（挂载但惰性的规则、或不存在的规则都无法自增自己的计数器）。**窗内主动 exercise（关键·空闲靶机否则计数器被动永不移动，复测活体坐实的头号缺口）**：`dropped`/`overlimits` 只在 offered load 逼近/超过 `rate` 时才移动——sshd-only 等无业务出向的靶机被动观测时 offered load 远低于 cap（实测 ~806 bit/s ≈ 1Mbit 的 1/1250）、令牌桶永不排空、两计数器恒 0，此时 tbf 虽 IN FORCE 却**未被 exercise**，白盒读到「计数器不动」属负载可用性缺口而非注入失败，但也不能据此判 verified。**修法**：读到机制回执（规则已挂载、rate/burst/lat==注入值）后，**在故障窗内（tbf 挂载后、自恢复载体 fire 前）主动发一段有界不可压缩 egress 负载**把整形器 exercise 出来，再在**同一窗内**重读 `tc -s qdisc show dev <if>` 即见 `Sent` 大幅上升 + `dropped` 或 `overlimits` 移动。负载生成命令（无 `of=` 即写 stdout、流经 SSH 通道自身 egress 被 tbf 整形；`count=` 有界即守卫放行〔`bs×count ≤ 256MiB`〕、无需外部 sink/管道，见 `references/criterion-reachability.md` M1）：`dd if=/dev/urandom bs=1M count=<N> status=none`。三条约束：**其一·源必须 `/dev/urandom` 不可压缩、禁用 `/dev/zero`**——SSH 若协商启用压缩，全零载荷被压到近乎零字节、根本不上线也就 exercise 不到整形器，urandom 无论压缩开关都产生真实 egress 字节；**其二·`<N>` 取值**——只需总字节 ≫ `burst` 桶深（如 ≥ 8×）即可：enforcement 计数器在 offered load **首个越过 `burst` 的 chunk** 即触发（`overlimits`/`dropped` 随之移动），无须 dd 跑完全部字节。**复测实测校正——勿按「总字节×8 / rate」估墙钟排空时间来选 `count`（该估算假设干净排空，自整形下不成立）**：dd 的 stdout 经**同一条被 tbf 整形的 SSH 通道**回传，与自身整形形成拥塞反馈，传输常在只送达约一个 `burst` 桶量（实测 rate 1mbit、`count=2`〔2MB〕仅 ~74KB 上线）后即停滞、命令可能撞工具 timeout 而 exit status UNKNOWN；**这是预期且充分的**——dd 的 exit/timeout 非效果证据，一律以 `tc -s` 计数器离基线移动为准，不要为「dd 未跑完 / 超时」补轮重试、也不必专门 kill 残留 dd（本地 ssh 一断，远端 dd 下次 write 即 SIGPIPE 自清，实测靶机零残留）。故 `count` 取小值（如 1mbit 档 `count=2`）既 ≫ burst 稳触发 enforcement、又缩短停滞对窗内后续观测的占用；**其三·时序纪律（头号陷阱）**——负载生成与计数器重读 MUST 在自恢复载体 fire 前完成，载体到期 `tc qdisc del` 拆掉整形后再发流量只跑在已恢复网卡上、计数器纹丝不动、白耗一轮（复测中 agent 曾把 dd 发在 carrier fire 后 ~96s、跑在 unshaped eth0 上零 enforcement 证据），故本步应紧随注入确认、在窗内第一批观测里完成，勿拖到后续 verify 迭代。**须认两条 enforcement 路径的稳定语义签名，勿硬编单一计数器**：`burst` ≥ 设备 MTU 时是清洁令牌桶整形（延迟路径，`overlimits` 上升、`backlog` 非零）；`burst` < 设备 MTU（尤其容器 veth 大 MTU + 内核 GSO 超级包，如 `burst 32kbit`=4096B « MTU 65535）时退化为入队拒绝（丢包路径，`dropped` 上升而 `overlimits` 停在 0）——**两者都是 tbf 主动约束出向的实证**，不得因「`overlimits` 未动 / 只 `dropped` 动」就否定限速已生效（这是 burst/MTU 环境特异的 enforcement 形态差异，非机制偏离；如需清洁延迟整形，见注意事项的 burst ≥ 设备 MTU 选型）。该 tbf 规则随自恢复载体到期被拆除、计数器一并消失，故窗内第一批快照的 `tc -s` 是能读到主动限速签名的时机；恢复之后再读只会得到回落后的默认 qdisc（`noqueue`，`Sent 0 bytes 0 pkt`），那属 unobserved 而非 absent，不能反读为「tbf 未承载流量」
2. **（行为级证据·条件性）** 出向上传吞吐速率差——用「注入前 vs 注入后」的**倍差**作判据（tbf 只限出向，测上传不测下载）。**此判据仅在具备跨主机 sink + 允许裸管道的执行通道（如 kubectl_exec 进 chroot-host 真 shell）时可达；经 host SSH 传输通道结构性不可达**（host_read/host_inject 对一切裸管道 fail-closed 拒绝、nc/socat 常缺失、单主机通道无法武装跨主机 sink、自建本机 sink 走 lo 绕过 `<if>` root qdisc 必然假阴性、host_read 不报 elapsed time——四条前提的逐条不可达原因见下方判据可达性 caveat ①③）。可达时测法：从目标主机往**资源准备阶段预置的** sink 发**有界流量**并计时——`dd if=/dev/zero bs=1M count=50 | nc <sink-host> <port>`（无 nc 时 `dd if=/dev/zero bs=1M count=50 | socat - TCP:<sink-host>:<port>`），比较注入前后耗时。**有界约束**：必须显式带 `count=`（无 count 的 `dd if=/dev/zero` 是无界流按 flood 拒），且 `bs×count ≤ 256MiB`（`bs=1M` 时 `count ≤ 256`；低速率档下大 count 会跑很久，在能测出倍差前提下取小值如 `count=50`）。**不要用小请求测**——限速不影响单个小包时延，几 KB 请求在低 rate 下仍毫秒级返回，看起来「没生效」。判据：出向上传速率相对基线**显著下降、逼近注入的 `rate`**（判据的实际决定项是相对基线的倍差〔robust〕，绝对速率是信息项，不得凭空捏造 MB/s 数字）；若与基线相比无明显下降，检查 `burst` 是否过大（相当于没限速）或测试流量是否为入向（入向不受限）
3. 确认**小请求仍然正常**——这是区分「限速」与「网络不通」的关键（旁证，`curl` 普适在场）：
   ```bash
   curl -sS -m 5 -o /dev/null -w '%{http_code}' <依赖服务健康检查地址>
   ```
   应正常返回。若连小请求也失败，说明 `latency` 设得过小、退化成丢包了
4. 检查应用日志是否出现传输超时 / 上传失败 / 同步滞后等慢速症状

> **判据可达性（吞吐 ratio 经 host SSH 通道结构性不可达，主判据收敛机制归因白盒）**：
> - **① 陷阱与不可达原因**：本用例的「注入前 vs 注入后出向上传吞吐倍差」是**跨主机流量测量**，天然依赖「另一台主机的接收端 sink ＋ 允许裸管道 `dd|nc` 的执行通道 ＋ 可计时」。经 host SSH 传输通道这些前提**结构性不成立**：(a) `host_read`/`host_inject` 对一切未加引号裸管道 fail-closed 拒绝（传输层逐 token 引号化，`|` 变字面参数、远程 shell 不再当操作符解析，物理不可执行——「有界发流量探测」的只读豁免**只在 kubectl_exec 容器面路径存在，host SSH 通道无此豁免**）；(b) `nc`/`socat`/`ncat`/`iperf3` 在最小宿主镜像常缺失；(c) 单主机 SSH 通道无法在另一台可达主机武装 `nc -l` sink（listen 属「模式开关即写」，且无第二条可达主机的通道）；(d) 退而在本机自建 sink 亦不可替代——发往本机自有 IP 的流量走 local 路由经 lo，根本不经过 `<if>` root qdisc，必然假阴性；(e) host_read 不报 elapsed time，无法在通道内计时。故吞吐 ratio 不得列为经 host 通道的无条件主判据。
> - **② 可达的替代主判据**：**机制归因白盒** `tc -s qdisc show dev <if>`（步骤 1）——机制回执（tbf 规则文本 ＋ rate/burst/lat==注入值）＋内核记账主动限速签名（`Sent` 离逐字节零基线 ＋ `dropped` **或** `overlimits` 移动，认延迟 / 丢包两条 enforcement 路径的稳定语义签名）为主判据，稳定可达、不依赖跨主机 sink 或裸管道；小请求仍通（步骤 3，`curl` http_code）为「限速≠阻断」的旁证。验证落在**注入网卡自身**上，勿换网卡 / 换目标反复重试；同一事实（如规则是否挂载）确认一次即可。
> - **③ 吞吐 ratio 经 host 观测通道结构性不可达（诚实声明）**：`dd|nc` / `dd|socat` 吞吐测量在 kubectl_exec 进 chroot-host 真 shell 路径可达（管道可用、可预置跨主机 sink），但经 host SSH 传输通道结构性不可达（见 ①）——这是**通道不可达，非抽样不足、非注入失败**。故经 host 通道时：不要为拿到吞吐 ratio 反复重试不可达的裸管道形态、不要改测下载（tbf 只限出向）、不要自建绕过 `<if>` 的本机 sink；主判据白盒 `tc -s qdisc show`（见 ②）已充分证明限速生效。**不得凭空捏造 MB/s 数字**——绝对速率是信息项，判据的实际决定项是白盒计数器离基线的移动方向与量级。

## 注入恢复
本用例无 blade 实验（手段2-only），恢复即删除 tbf 规则：等待 `<recovery-seconds>` 到期后注入前武装的定时器自动 `tc qdisc del`；如需提前恢复，手动停掉已武装的定时器（按武装时选用的 Tier——Tier-1 `systemctl stop blade-restore-tbf.timer` / Tier-2 `pkill -f 'sleep <recovery-seconds>'` 括号法，均见文末注意事项与标准件文档 §五）再删除规则：
```bash
tc qdisc del dev <if> root
```

## 恢复验证
1. 确认 tbf 规则已清除：
   ```bash
   tc qdisc show dev <if>
   ```
   输出应回到默认 qdisc（如 `pfifo_fast` / `noqueue` / `mq`），不再包含 tbf
2. 重测出向上传吞吐，确认恢复到基线水平（测法同注入验证步骤 2，须 sink 仍在）。**若 sink 已随演练收尾拆除**（listen 属写入集组件、恢复阶段一并拆除，而 verify/recover 是只读阶段重新起 listen 会被守卫拒绝），则字面的 `dd|nc` 重测不可执行——此时**认可「机制缺失等价论证」为吞吐类恢复验证的合法降级形态**：无需重测绝对 MB/s，三条腿成立即可判恢复到基线——(a) 机制不存在：`tc qdisc show dev <if>` 回默认 qdisc / `noqueue`（网卡上没挂任何限速装置 → 出向物理上不可能再被整形）；(b) 同资源基线逐字节匹配（qdisc 状态 = 注入前基线）；(c) 旁证计数干净（网卡 TX errors/dropped 无异常增长、小请求延迟回基线）。裁决如实标注「通过（方法偏差：未重测绝对速率）」，**不得捏造 MB/s 数字**
3. 确认应用日志不再出现传输慢 / 超时错误

## 基准事实
- **根因**：通过 tc tbf（令牌桶过滤器）在主机网卡限制出方向速率，模拟带宽受限 / 专线拥塞 / 带宽包耗尽环境
- **手段适用性**：ChaosBlade `blade create network` 无 bandwidth/tbf action（9 action 探测为准），本用例是**手段2-only**（仅 tc tbf），**严禁据 blade 宣称注入或验证**
- **必现现象**：`tc -s qdisc show dev <if>` 显示 tbf 规则（rate/burst/lat==注入值）——机制回执恒可达；内核记账计数器离开逐字节零基线（`Sent` 上升 ＋ `dropped` 或 `overlimits` 移动）为**稳定可达主判据，但须窗内主动 exercise**——enforcement 计数器只在 offered load 逼近 cap 时才动，空闲靶机被动观测恒 0，故须在故障窗内（载体 fire 前）主动发有界不可压缩负载 `dd if=/dev/urandom bs=1M count=<N> status=none` 把整形器 exercise 出来再重读（见 `## 注入验证` step 1）；出方向大流量传输速率相对基线显著下降、逼近注入 `rate`（入向不受限；条件性佐证，仅吞吐 ratio 可达通道）；小请求仍正常返回（区别于网络不通）；应用出现数据同步滞后 / 上传超时等慢速症状
- **判据特性**：吞吐 ratio（跨主机流量测量）经 host SSH 通道**结构性不可达**（裸管道 fail-closed、nc/socat 常缺失、无跨主机 sink、自建本机 sink 绕过 `<if>`、host_read 不报时），主判据收敛为**机制归因白盒** `tc -s qdisc show`（机制回执 ＋ 内核记账主动限速签名，须认延迟〔`overlimits`〕/ 丢包〔`dropped`〕两条 enforcement 路径的稳定语义签名，勿硬编单一计数器）；携带判据可达性 caveat（见 `## 注入验证`）
- **作用域边界**：裸 root tbf 限住整块网卡**全部出向流量**（含 SSH 控制通道，限速不等于阻断）；tbf 只限出方向，入方向需 ifb 重定向（不在本用例范围）
- **爆炸半径**：`rate` 须保证 SSH 控制通道仍可操作，且必须先武装定时自删载体作恢复安全网；需按目标流量精确收窄时用 htb 分类层级 + filter（独立收窄形态，选用前须单独功能探测坐实）
- **tbf 可用性以功能探测为准**：grep `/proc/modules`、`modinfo` 查不到 sch_tbf 痕迹不等于内核不支持，必须用 dummy 网卡功能探测定论（见资源准备第 3 条）；探测报不支持即收工，换目标 / 换网卡都改变不了（内核是同一个）

## 注意事项
- **本用例无手段1**：blade 无 bandwidth/tbf action，只有 tc tbf 手段2；不要尝试用 blade 注入带宽限制、也不要据 blade status 宣称验证（那是为不存在的机制代言）
- **tbf 只限出向**：验证测上传不测下载；下载型业务须换 ifb 重定向（不在本用例范围）
- **爆炸半径**：裸 `tc qdisc add dev <if> root tbf` 限住整块网卡 egress（含 SSH:22），`rate` 过低会连恢复通道一起拖慢到不可操作——须取保证 SSH 可用的速率并先武装定时自删安全网；需精确收窄到目标流量时用 htb + u32 filter 分类层级（比裸 root 复杂，选用前须单独功能探测）
- 自恢复基于注入前武装的**分层自恢复载体**（Tier-1 主机有 systemd 时 systemd-run transient timer / Tier-2 主机无 systemd 时 setsid detached-timer；选型探测门、武装 / 重武装纪律、payload 文法〔Tier-2 禁嵌套 interpreter，逆命令直接写在外层 sh -c 内〕、fail-closed 边界、Tier-2 取证约束见 `references/carrier/recovery-carrier.md`），到期自动 `tc qdisc del dev <if> root` 删除 tbf 规则；提前恢复仍用上方手动命令
- `tc qdisc del` 对已删除的规则报 `RTNETLINK answers: No such file or directory`，无害但需预期（幂等容忍，见标准件文档 §六）——定时器自治 fire 与框架提前兜底双执行时，第二次 `del` 报此错属预期，不得据此判恢复失败
- Tier-2 payload 守 discard-sink 不变量、**不写任何取证文件**，故无载体侧 fire 日志属预期取舍（见标准件文档 §八）；fire 是否发生由框架侧只读观测靶标状态差承担（如 `tc qdisc show dev <if>` 由 tbf 回落默认 qdisc），不得据此误判武装失败
- 武装与注入分两次独立执行，须确认武装成功后再执行注入（等价于 `&&` 串联的失败短路保证）
- 载体 unit 名取非故障家族词根（`blade-restore-tbf`）以符合 host 段分层放行；Tier-1 同名 transient timer 重复武装会报 `Unit blade-restore-tbf.service was already loaded`（上次武装失败时 unit 以 failed 状态残留所致），重武装前先清理残留：`systemctl stop blade-restore-tbf.service; systemctl reset-failed blade-restore-tbf.service`；Tier-2 重武装先 `pkill -f 'sleep <recovery-seconds>'`（括号法）终止旧会话再重新 setsid（见标准件文档 §五）
- **吞吐探测经 host SSH 通道结构性不可达（勿反复重试裸管道）**：`dd|nc` / `dd|socat` 裸管道被 `host_read`/`host_inject` fail-closed 拒（「有界发流量探测」的只读豁免**只在 kubectl_exec 容器面路径存在，host SSH 通道无此豁免**——传输层逐 token 引号化使 `|` 变字面参数物理不可执行）；经 host 通道不要反复重试裸管道吞吐形态、不要改测下载（tbf 只限出向）、不要自建绕过 `<if>` 的本机 sink，主判据走白盒 `tc -s qdisc show`（见 `## 注入验证` 判据可达性 caveat）。**仅当走 kubectl_exec 等允许裸管道 ＋ 可预置跨主机 sink 的通道时**，吞吐探测的 `count=` 才受守卫有界预算约束（`bs×count ≤ 256MiB`、无 count 按 flood 拒）、接收端 sink 须注入前预置不在 verify 阶段临时起 listen
