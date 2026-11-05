# host 判据可达性 authoring 契约（criterion-reachability）

**定位**：host 域每个 case 文档的 `## 注入验证` 判据，必须在**靶机运行时实际可达**——既能经通道执行、又能在故障窗内产出证据。判据不可达有两条正交成因轴：① **注入手段运行时封锁**（注入器占用的资源、运行的身份、有界演练的安全边界使主判据产不出证据，M2）；② **环境工具面缺失/语义错配**（判据命令依赖的工具在靶机可能不存在，或工具语义与故障观测面不匹配，M3）。这是 host 域「注入验证判据如何 authoring」的**单一权威**：各 case 只在 `## 注入验证` 节内引用本契约、按标准化结构写「判据可达性」caveat，不再各自发明格式。本契约与 `references/carrier/recovery-carrier.md`（自恢复载体单一权威）同构——都是把散落在各 case 的领域知识单源化。

**与通道准入（M1）的边界**：判据不可达有三种成因，后两种归本契约——

| | M1 通道准入形态 | M2 手段运行时封锁（本契约） | M3 工具面可达性（本契约） |
|---|---|---|---|
| 缺陷 | 判据命令形态被 `host_read`/`host_inject` 守卫拒（裸管道、写型探针 `dd`） | 判据命令形态合法、可经通道执行，但被注入器自身运行时条件语义性封锁 | 判据命令形态合法、注入器也不封锁，但依赖的工具在靶机可能不存在（dnsutils 的 `nslookup`/`dig`、抓包工具 `tcpdump` 非最小镜像默认装），或工具语义与故障观测面错配（`nslookup` 绕过 `/etc/hosts`、读不到 hosts 层劫持） |
| 可判定性 | 静态可判（命令形态） | 不可由命令形态判定（须懂手段的运行时资源占用/权限/有界性） | 不可由命令形态判定（须懂靶机工具面 + 工具解析语义） |
| 牙 | 主要由 `tests/test_skills/test_host_case_commands_pass_guard.py` 守（每条 case 命令喂权威分类器 `is_readonly_host_command`/`ToolGuard().check`）；但该牙有**声明边界**（只扫 fenced 代码块、`_HOST_HEADS` 不含 `ip`），故捕不到 inline-prose 里的管道吞吐探针（`dd … \| nc`）与 `ip link add` dummy 网卡 go/no-go——这两形态另由 `test_host_case_active_probe_channel_sentinel.py`（means-class 键）守 | 由本契约 + `test_host_case_criterion_reachability_sentinel.py` 结构 sentinel 守 | 由本契约 §五「工具面」类 + DNS 家族 `test_host_case_dns_probe_universality_sentinel.py` + netem 弱判据族 `test_host_case_tcpdump_tool_face_sentinel.py` 守 |

M1 的关键陷阱：命令形态被守卫拒不止「裸管道 / 写型 `dd`」这类可静态判定的写法——**同一条 active-probe 判据在不同注入通道上可达性相反**。host SSH 通道（`SSHChannel.wrap_command` 对每个 token 逐一 `shlex.quote`，管道/重定向作为字面量送达远程 shell 会静默失效）结构性拒绝一切裸管道/替换/子 shell，`ToolGuard` 二进制白名单不含 mutating `ip`，最小靶机常无 `nc`/`socat`；而 kubectl_exec 通道（`kubectl exec`/`debug`+`chroot /host` 在物理节点）可跑管道、有真 `nc`/`socat`、可 `ip link add`。故从 k8s 域改编到 host 域的 case，凡把「发有界流量到外部 sink 测吞吐比」（`dd … | nc <sink> <port>`）或「建 dummy 网卡跑一次 qdisc 做 go/no-go」（`ip link add dtest0 type dummy && …`）列为判据，在 host 通道皆**结构性不可达**——非抽样不足、非靶机临时缺件，而是通道准入形态使然；且严禁声称这类管道探针「已被只读守卫放行」（有界发流量探测的豁免是 kubectl_exec 通道专属，host 通道按设计拒一切裸管道）。host 通道的可达替代：① egress 吞吐类判据改走**内核计数器差分**——`tc -s qdisc show dev <if>` off 网卡默认 `noqueue` 的**字节精确零基线**（`Sent 0 bytes 0 pkt`，因 `IFF_NOQUEUE` 绕过计数），整形挂载后 `Sent`/`overlimits`/`dropped` 变非零即内核级白盒证据，发流量用**有界不可压缩 `dd` 到 stdout**（`dd if=/dev/urandom bs=1M count=<N> status=none`，无 `of=` 即写 stdout、流经 SSH 通道自身 egress 被 tbf 整形，`count=` 有界即守卫放行、无需外部 sink/管道）。**两条硬约束（复测活体坐实）**：其一 **源 MUST 用 `/dev/urandom` 不可压缩，不得用 `/dev/zero`**——SSH 若协商启用压缩，全零载荷会被压到近乎零字节、根本不上线也就 exercise 不到整形器，urandom 不可压缩故无论压缩开关都产生真实 egress 字节；其二 **这段负载生成 MUST 落在故障窗内**（tbf 挂载后、自恢复载体 fire 前）——enforcement 计数器 `dropped`/`overlimits` 只在 offered load 逼近/超过 `rate` 时才移动，空闲靶机（sshd-only、无业务出向）被动观测时 offered load 远低于 cap、令牌桶永不排空、两计数器恒 0，tbf 虽 IN FORCE 却未被 exercise；故须主动窗内发一段 `bs×count` ≫ `burst` 桶深、且「按 cap 排空耗时 ≈ 总字节×8 / rate」落在剩余窗内的有界负载把整形器 exercise 出来，发完在**同一窗内**重读 `tc -s` 才见计数器移动；载体 fire 拆掉 qdisc 之后再发只跑在已恢复网卡上、计数器纹丝不动，是白耗一轮；② qdisc 可用性 go/no-go 改在**既有 inert 接口**（`tunl0`/`lo`，只读发现）上 `tc qdisc add`/`del` 探测（qdisc-kind 解析走内核全局、零爆炸半径、用后即删），不建 dummy 网卡。

M2 的关键陷阱：命令**能跑通通道**不代表**能在故障窗内产出证据**。典型如 `strace -T -p <pid>` 是合法只读 attach 形态、被 M1 分类器放行，但注入器已独占 ptrace 时它二次 attach 必然失败——M1 守卫证不了这一点。

M3 的关键陷阱：命令**能在故障窗内跑**不代表**靶机装了它、且它读得到故障观测面**。典型如 `nslookup <domain>` 是合法只读形态、注入器也不封锁，但 dnsutils 非最小靶机默认装（缺失即 exit 127），且即便装了它也只查 DNS 服务器、绕过 `/etc/hosts`——对 hosts 层劫持读不到效果。承重判据 MUST 走靶机普适在场、且与应用同观测面的探针（DNS 解析类＝glibc NSS 路径 `getent hosts`/`ping`，遵 nsswitch `files→dns`、与应用同口径），`nslookup`/`dig` 只作 DNS 层可选诊断并带 caveat。M3 有两种子形态，修法不同——① **承重探针非普适**（如 DNS 类的 `nslookup`）：MUST 换成普适在场、同观测面的探针（`getent`/`ping`）；② **佐证探针非普适**（如 netem 弱判据族〔报文乱序/重复/损坏〕的线上抓包 `tcpdump`——最小镜像默认不装、缺失即 `which` 空 / exit 127）：该探针本就不是主判据，MUST 披露其工具面缺失可能、并明确其不可达**不否定普适在场的白盒主判据**（`tc -s qdisc show` 子 band `pkt>0`），不得因佐证探针缺失反复重试或判注入失败。**披露只是防守的一半——另一半是进攻：MUST 处理 ICMP-ping 普适行为面，不得沉默跳过。** `ping` 普适在场（不同于 `tcpdump`），且 ICMP echo 无接收端 seq 去重（不同于 TCP），故当收窄目标为可 ping 地址时 `ping` 直接产出行为级签名（重复＝`+N duplicates` / `(DUP!)`；损坏＝重传 / 时延上升，配 `ss -ti`）——tcpdump 缺失时 MUST 以 `ping` 作等价行为佐证，把整层行为级证据抓回来，而非退化成「只剩白盒单层」。唯 `ping` 面本身**结构性不可达**的形态（如 reorder：必然伴随的 delay 令后发包在默认间隔下算术上无法超车前包，而能令超车可见的快间隔形态 `ping -i <0.1` 被只读观测通道判为流量放大 / flood 拒绝）MUST 显式声明其不可达 ＋ rationale，而非沉默跳过——即「处理 ping 面」有两种合法落法：**可达则开为等价佐证，不可达则声明并给出机理**，二者必居其一。

---

## 一、不变量

**凡某判据在故障窗内、以本文档手段所产生的运行时条件、在靶机实际工具面上结构性或条件性无法产出证据，该判据 MUST NOT 列为无条件主判据；MUST 降级并携带标准化「判据可达性」caveat，指明可达的替代主判据。**

「不可达」的机理分两轴（见 §五分类表）——

- **手段运行时封锁（M2）四类**：资源占用（注入器独占观测资源，如 ptrace 排他）、权限（注入/验证同身份使故障对该身份不可见，如 root 绕过 `chmod 000`）、有界演练安全性（真触达阈值会波及控制/恢复通道，如逼近 `pid_max`）、局部 vs 全局触顶（故障局部于 holder、不触顶全局资源，如 fd 局部不触顶 `file-max`）。
- **环境工具面（M3）一类**：工具可用性/语义匹配——承重**或佐证**探针依赖的工具在靶机可能缺失（dnsutils `nslookup`/`dig`、抓包 `tcpdump` 非最小镜像默认装），或工具语义读不到故障观测面（`nslookup` 绕过 `/etc/hosts`、验不出 hosts 层劫持）。承重探针非普适 MUST 改用靶机普适在场、与应用同观测面的探针（DNS 解析类＝glibc NSS 路径 `getent hosts`/`ping`，遵 nsswitch `files→dns`、与应用同口径），非普适工具降为可选诊断并带 caveat；佐证探针非普适（如 netem 弱判据族 `tcpdump` 线上抓包）MUST 披露其缺失可能并明确不否定普适白盒主判据（`tc -s qdisc show` 子 band `pkt>0`），不因佐证不可达反复重试或判失败；且 MUST 处理 ICMP-ping 普适行为面——`ping` 普适在场、ICMP echo 无接收端 seq 去重，可达则开为等价行为佐证（重复 `+N duplicates`／损坏重传时延），结构性不可达（如 reorder）则声明不可达＋机理，二者必居其一，不得沉默跳过。

## 二、可达性排序规则

`## 注入验证` 的判据 MUST 按可达性排序：

1. **可达者列为主判据、在前**——在故障窗内、以本文档手段所产生的运行时条件，稳定能产出证据的判据。
2. **被封锁者降级、在后**，且 MUST 带 caveat 说明 ① 为何窗内不可达 ② 可达的替代证据。
3. **优先级不得倒置**：严禁把窗内不可达的判据列为无条件主判据#1、反把可达判据标「（可选）」。（这正是 2 个 strace 文档的缺陷形态。）

## 三、标准化 caveat 结构

「判据可达性」caveat MUST 含两要素，缺一不可：

- **① 陷阱与不可达原因**：点明哪条判据、被本文档哪个手段的什么运行时条件封锁、窗内为何产不出证据（并说明它在何时才可达，如「ptrace 释放后的恢复验证阶段」）。
- **② 可达的替代判据**：给出窗内稳定可达的主证（行为级 app 端到端证据 / 机制归因白盒证据），使验证不依赖被封锁的判据也能定性。

## 四、落位形态

- caveat MUST 落在 `## 注入验证` 节内的 `>` blockquote（或紧随 `## 故障现象` 的 `>` blockquote，如「进程数过多」先例），**MUST NOT 新增 `##` 章节名**——章节名词表由 `test_skill_case_section` / `test_all_md_headings_in_wordlist` 钉死，新增 `## 判据可达性` 会撞词表边界。
- 统一以 `> 判据可达性：` 或 `> **判据可达性（<限定>）**：` 起头，便于 sentinel 与人读一致识别。

## 五、机理分类表（M2 四类 + M3 工具面类）

| 机理类 | 封锁成因 | 可达替代主判据 | 先例文档 |
|---|---|---|---|
| 资源占用（ptrace 排他，M2） | 手段1 `blade create strace` / 手段2 `strace -p <pid>` 已独占 ptrace，判据 `strace -T -p <pid>` 窗内二次 attach 必失败 | 行为级：app 端到端延迟/错误；机制归因白盒：`pgrep -a strace` 见注入规则文本 + `/proc/<pid>/status` `TracerPid≠0`/`State=t` | **反例（待修）**：`Host_系统调用异常_调用延迟`、`Host_系统调用异常_调用返回错误` |
| 权限（root 绕过，M2） | 注入/验证同为 root，`chmod 000` 对 root 读者无效，「Permission denied」判据不可达 | 改内容/位置变体，或以非 root 应用视角核实 | `Host_文件系统异常_关键文件被篡改` |
| 有界演练安全性（M2） | 真逼近 `pid_max` 需同等量级进程，`threads-max`/内存先耗尽，且波及 SSH 与 out-of-band 恢复通道 | 主判据收敛为「有界进程数增量 + fork 速率」，现象1/2 仅作机理说明 | `Host_进程异常_进程数过多` |
| 局部 vs 全局触顶（M2） | fd 消耗局部于 holder 进程，`file-max` 极大的主机上不触顶全局，独立应用 `Too many open files` 不可达 | `file-nr` 首列飙升 + holder 进程 fd 数（`/proc/<pid>/limits` + 高位 fd `readlink` 归因） | `Host_文件系统异常_文件句柄耗尽` |
| 工具面（探针普适性，M3） | 承重**或佐证**探针依赖靶机可能缺失的工具（dnsutils `nslookup`/`dig`、抓包 `tcpdump` 非最小镜像默认装，缺失即 exit 127 / `which` 空），或工具语义读不到故障观测面（`nslookup` 绕过 `/etc/hosts`、验不出 hosts 层劫持） | ① 承重探针非普适→改用靶机普适在场、与应用同观测面的探针：DNS 解析类＝glibc NSS 路径 `getent hosts <domain>`（遵 nsswitch `files→dns`、与应用同口径；getent 亦缺时 `ping <domain>` 同 honors resolver），`nslookup`/`dig` 降为 DNS 层可选诊断带 caveat；② 佐证探针非普适（netem 弱判据族 `tcpdump` 线上抓包）→披露其缺失可能并明确不否定普适白盒主判据（`tc -s qdisc show` 子 band `pkt>0`），不因佐证不可达反复重试或判失败，且 MUST 处理 ICMP-ping 普适行为面（`ping` 普适在场、ICMP echo 无 seq 去重：可达＝开为等价行为佐证 `+N duplicates`／重传时延，结构性不可达如 reorder＝声明不可达＋机理，二者必居其一，不得沉默跳过） | `Host_网络故障_DNS不可用`、`Host_网络故障_DNS劫持`（承重探针走 glibc NSS 路径 `getent`/`ping`）；`Host_网络故障_报文乱序`、`Host_网络故障_报文重复`、`Host_网络故障_报文损坏`（佐证探针 `tcpdump` 非普适，白盒 `tc -s` 为主判据）；范式先例：k8s `Pod_网络故障_域名不存在NXDOMAIN`「工具面→判据形态」映射 |
