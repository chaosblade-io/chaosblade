# host 恢复载体标准件（recovery-carrier）

**定位**：当 host 域原生故障（非 ChaosBlade 路径）的恢复动作落在**主机本地**（删除填充文件、还原被改文件、解除进程冻结等），且需要「框架死亡仍能自治恢复」的安全网时，用一套**分层自恢复载体**承载定时恢复——不假设主机预装任何守护进程。这是 host 域分层自恢复载体的**单一权威**：Tier-1（主机有 systemd）用 systemd transient timer，Tier-2（主机无 systemd）用 setsid detached-timer。各 case 的降级/原生手段只引用本标准件，不再各自内嵌载体构造知识。本标准件同时是 host 域『自恢复如何达成』的**分支路由单一权威**，覆盖两条互补分支——**Branch A（持续型手段2）**：故障瞬时落地、恢复靠外挂分层载体（Tier-1/Tier-2）定时逆操作，即下文 §一~§八；**Branch B（自停型手段2）**：故障命令自带 `--timeout` 前台跑满窗口后自行退出即恢复、无需任何外挂载体，其发射语义见 §〇。**先在 §〇 判分支**，再进对应章节。

**Branch A 适用判据**（三条全中 → 走 §一~§八 分层载体；若「故障命令自带 `--timeout` 前台自停、无需外挂逆操作」→ 属 Branch B，走 §〇）：
1. 恢复动作是主机本地操作（文件/进程/网络配置的本机还原），不是 K8s API 对象写（那走 k8s 的 `references/carrier/recovery-carrier.md`），也不是 blade 实验回滚（那走 `blade destroy`）。
2. 故障是**持续型**、需要贯穿观察窗存活并在窗口终点自恢复（填充文件存活即故障存活）。
3. 需要「先武装定时恢复、再注入」的安全网——注入方（框架/会话）即便中途死亡，故障也能到点自治恢复。

---

## 〇、分支路由与 Branch B（自停型手段2 发射）

host 域原生手段2 按「恢复如何达成」分两条互补分支，**发射前先判分支**：

| 判据 | Branch A（持续型手段2） | Branch B（自停型手段2） |
|------|------------------------|------------------------|
| 故障命令形态 | 瞬时落地即返回（`kill -STOP`、`iptables` 下规则、写填充文件） | 前台持续运行到自停（`stress-ng --<stressor> --timeout` 等自带 timeout 的压测命令） |
| 恢复机制 | 外挂分层载体定时执行逆操作（§一~§八） | 故障命令自带的 `--timeout` 到期自行退出，无需任何逆操作 |
| 是否武装载体 | 是——先武装 Tier-1/Tier-2、再注入 | 否——直接前台发射故障命令 |
| 发射回执 | 载体武装调用秒回（`Running timer as unit` / setsid 秒回） | host_inject 本地等待超时、返回 outcome UNKNOWN，而故障命令在主机续跑 |

**Branch B 发射规则（自停型手段2 的权威发射语义）**：

1. **无载体**：不武装 systemd-run / setsid，不构造任何定时逆命令——故障命令自带的 `--timeout <recovery-seconds>s` 就是恢复机制（`<recovery-seconds>` 取值见 §四）。
2. **前台直发**：经 host_inject **直接前台执行**故障命令，本地等待 timeout 用默认短值即可；**不要**为「拿到同步返回」把本地 timeout 加大到覆盖整个故障窗——那只是把阻塞从工具层挪到调用层，不改变结果。
3. **UNKNOWN 即预期且完整的发射回执**：故障命令前台运行时长（= `recovery_timer_seconds`，通常远超 host_inject 默认本地等待上限）→ host_inject **必然**返回 timed-out / outcome UNKNOWN，而故障命令在主机上继续跑到 `--timeout` 自停。**这个 UNKNOWN 不是丢失回执、不是失败、不是「不理想」**——它是本家族结构上唯一可能的发射结果。
4. **对账探针才是权威回执（至多一条）**：发射后紧接着用 host_inject 跑**一条只读探针**（如 `ps -C stress-ng -o pid,comm` 确认进程在跑、`cat /proc/loadavg` 确认负载上升）核实故障命令确已在主机运行——这条只读探针的返回才是发射成功的权威回执（与 host_inject 工具描述的「UNKNOWN 后先对账、再只补真正缺失的」语义同源）。这是「痕迹确认」而非「效果观测」：只确认变异已落位，效果是否生效归 verify 阶段，**不得重复采样**。
5. **严禁 detach 追同步返回**：**不得**用 `setsid` / `systemd-run` / `nohup` / `&` 包裹故障命令去追一个「干净的同步回执」。detach 只服务 **Branch A 的恢复载体**（让定时逆命令脱离会话、到点自治 fire，见 §三），**绝不用于故障命令本体**。用 setsid 包裹故障命令并不能把 UNKNOWN 变成同步成功——`setsid --fork` 的子进程同样跑满 `--timeout` 才退出、host_inject 本地等待同样超时返回 UNKNOWN；detach 改变的是进程树归属，不是命令的运行时长。

**Branch A ↔ Branch B 划界**：Branch A 的故障是瞬时的、恢复靠外挂载体定时逆操作，故必须先武装载体（§一~§八 全适用）；Branch B 的故障命令本身持续前台运行到自停、恢复内建于 `--timeout`，故 §一~§八 的载体构造**一律不适用**，只需按上述发射规则前台直发 + 一条对账探针。判错的代价是对称的：给 Branch B 强加载体 = 武装一个永不会 fire 的逆操作（故障已自停）+ 把前台命令移出可控范围；给 Branch A 省掉载体 = 故障无自治恢复安全网（违反 §七 fail-closed）。

---

## 一、分层选型与 systemd 探测门

武装前**必须先只读探测** systemd 是否在场，据此选 Tier。探测与 Tier 选择必须紧邻武装步骤（探测本身只读、不改靶标）。

```bash
# 探测信号（任一即可判定 systemd 在场）：
systemctl is-system-running     # 只读；bus 可达即 systemd 在场（degraded 仍算在场）
[ -d /run/systemd/system ]      # 只读；目录存在即 systemd 在场
```

**Tier 选择判据**：
- systemd **在场** → **Tier-1**（systemd-run transient timer，见第二节）。
- systemd **不在场**（`systemctl is-system-running` 失败且 `/run/systemd/system` 不存在，如最小容器、非 systemd init 的裸机）→ **Tier-2**（setsid detached-timer，见第三节）。
- 两 Tier 前置皆不可用（systemd 不在场**且** `setsid`/`sh`/`sleep` 任一缺失）→ **fail-closed**（见第七节），禁止静默注入无安全网的自定时故障。

> `systemctl is-system-running` 在 systemd 存在但 degraded 时返回非零——degraded 仍有 systemd timer 能力，**不因 degraded 降级到 Tier-2**；仅以「systemd bus 不可达 / `/run/systemd/system` 不存在」为 Tier-2 判据。

---

## 二、Tier-1：systemd-run transient timer（主机有 systemd）

延迟由 systemd timer 提供，payload 是**幂等逆命令**（不含 sleep 链）：

```bash
# <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（见第四节），禁自算缓冲。
# 武装成功输出含 "Running timer as unit ..."；武装成功后再执行注入。
systemd-run --on-active=<recovery-seconds>s --unit=<unit-name> \
  <幂等逆命令>
# 例（磁盘填充）：systemd-run --on-active=<recovery-seconds>s --unit=blade-rmfill-host \
#   rm -f <path>/app-archive.dat
```

- 校验武装：`systemctl show <unit-name>.timer -p ActiveState,SubState,NextElapseUSecMonotonic`（应见 `ActiveState=active`、`NextElapseUSecMonotonic` 非空）。**瞬态 timer（`systemd-run` 创建）在过滤形 `systemctl list-timers <unit>` 下会假阴性返回 `0 timers listed`**，须 `systemctl list-timers --all` 或 `systemctl show` 才可见——勿据裸过滤查询判定武装失败。
- **payload 文法 = §三「payload 文法（Tier-1/Tier-2 单一源）」，零放宽、不在此重述**。Tier-1 与 Tier-2 共用同一套白名单文法（§三 明示「链式删除硬地板对 Tier-1/Tier-2 对称生效」即此意）：单条逆命令（`rm -f <单文件>`、`truncate -s 0 <file>`、`iptables -D …`、`kill -CONT <pid>`）是最简形态（argv form）；多命令逆操作链（如进程被杀死 case 形态A 的 `unmask; reset-failed; start`）用**恰好一层** `sh -c '<链>'` 包装承载（script form）——守卫的 payload-region readmission（guard.py `_admit_command_region` → `_admit_script`）对 interpreter 形态只准入 `sh -c '<script>'` 三 token 形（裸 `sh <文件>`、第四 token 一律拒），再用 bashfacts 把脚本分解、内层按 §三 文法**逐段准入**。§三「禁嵌套 interpreter」指 payload 脚本**内部**不得再嵌 `sh`/`bash`，不是禁这层载体包装。`rm` 仅限 `rm -f <单文件>`（递归 `rm -rf` 无演练边界，一律拒）。

---

## 三、Tier-2：setsid detached-timer（主机无 systemd）

setsid 无内建定时器，延迟由 payload 内的 `sleep` 提供；`--fork` 令子进程脱离会话，会话死亡时不接收 SIGHUP，到点自治 fire。**但脱离进程树不等于脱离传输通道**——fork 出的子进程仍继承 SSH/exec 通道的 fd1/fd2，通道服务端（sshd）等不到输出管道 EOF，会阻塞整个 `sleep` 窗（或撞工具超时），arm 步骤迟迟不返回。故 payload 首句必须是 `exec >/dev/null 2>&1` 断开这对继承的 fd（stdio 侧的 detach，与 `--fork` 的进程树 detach 成对）：

```bash
# canonical 形态——四个构成性约束缺一不可：
#   (1) --fork：setsid 默认不 fork，缺之则同步阻塞调用方整个恢复窗（arm-then-inject
#       结构性失败）；--fork 令父进程即刻退出、子进程入新会话（新 SID、PPID=1）。
#   (2) exec >/dev/null 2>&1（payload 首句）：断开子进程从传输通道继承的 fd1/fd2，
#       令通道服务端立即见 EOF、arm 秒回（实测 69ms），而 detached 子进程照常存活到点 fire；
#       缺之则 arm 阻塞整个 sleep 窗（实测 8112ms）。MUST 写在 sh -c 脚本内——传输层对
#       每个 argv token 做 shlex.quote，写在 setsid argv 层（… sh -c '…' >/dev/null 2>&1）
#       会被引号化成死参数、到不了远端 shell（实测 8074ms 仍阻塞）。
#   (3) && 分隔：`; rm` 会命中「链式删除」硬地板（该地板对 Tier-1/Tier-2 对称生效），
#       规范统一用 && 分隔；sleep <N> 恒返回 0，&& 与 ; 对定时恢复语义等价。
#   (4) <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（见第四节），禁自算。
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && <幂等逆命令>'
# 例（磁盘填充）：setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && rm -f <path>/app-archive.dat'
```

**payload 文法（Tier-1/Tier-2 单一源，§二 引用此处、不重述）**——`<script>` 逐段准入，MUST 满足：
- 纯 `&&`/`;`/`||` 顺序的**白名单二进制**命令（非白名单二进制如 `python` 一律拒）；
- **禁** `&` 后台化、**禁** subshell `( … )`、**禁**嵌套 interpreter（`sh`/`bash`）或嵌套 carrier（`systemd-run`）；
- 重定向**仅限 discard sinks**（`/dev/null`、`/dev/stdout`、`/dev/stderr`、`/dev/fd/`）——**payload 不写任何真实文件**；fd 复制（`2>&1`）不写文件、天然属 sink；
- **裸 `exec`（无命令参数）+ 仅 discard-sink 重定向**是唯一的 `exec` 豁免形态——即上面的 stdio sever；`exec <程序>`（带任何参数）会用任意程序替换 shell，一律硬地板拒绝；
- `$( … )` 体内**仅限 PID 探针**（`pidof`/`pgrep`），供 `kill -CONT $(pgrep -f x)` 类进程恢复。

**被拒形态（fail-closed，勿尝试）**：裸 `setsid <cmd>`（立即执行任意命令）、缺 `--fork` 的 `setsid sh -c '…'`（阻塞调用方）、`setsid --wait/-w`（等待子进程）、`setsid --fork sh <脚本文件>`（内容不可见）、`exec <程序>`（任意程序替换 shell）、把 fd-sever 重定向写在 setsid argv 层（被 shlex.quote 引号化、到不了远端 shell，等于没断 fd）。合规形态唯一：`setsid --fork sh -c 'exec >/dev/null 2>&1; <script>'`。

**payload 忠实性由构造保证（Tier-1/Tier-2 同理，勿探测、勿因「探测不了」改形）**：host/SSH transport 对每个 argv token 单独 `shlex.quote` 后拼成远程命令串，故 `systemd-run … sh -c '<script>'`（及 Tier-2 `setsid … sh -c '<script>'`）里的 `<script>` 作为**单个** token 被引号包裹、完整送达远端再由远端 `sh` 二次解析——payload 由构造忠实，不会被 naive 按空格拆分。`sh` 不在顶层注入白名单，故无需（也无法）另起一条 `host_inject` 去探测 argv 是否被拆。武装回执与 payload 忠实性证明的是**两件不同的事**，勿互相顶替：`Running timer as unit …` 证载体已武装（这才是它的判据），但它对 payload 完整性不作证；`systemctl show <unit>.service -p ExecStart` 回读把 `argv[]` 各元素**空格拼接且不带引号**渲染，故「脚本完整送达」与「脚本被拆成多个元素」两种情况的回读字节完全相同——它既不能确认也不能否认该边界，回读里的歧义是渲染属性、不是拆分证据，勿据此改形或重装。忠实性的正面证据来自施加引号化的那一方：`host_inject` 的成功回执在含 interpreter payload 时直接给出该元素的序号、字节数与 sha256，无需另起探测。**严禁因「无法探测」把单条 `sh -c '<链>'` 拆成多个裸 argv 单元**：裸 argv 无法用 `|| true` / `2>/dev/null` 容忍合法非零退出（如 `chronyc makestep` 在 chronyd 刚起时 no-op 退 1），非零退出令 transient service unit 落 `failed` 态，而 systemd **不自动 GC failed unit** → 残留；单条 `sh -c` 形整体 `exit 0`、unit 干净 GC、零残留，严格更优。

---

## 四、双数窗口契约取值（`<N>` = recovery_timer_seconds）

Tier-1 与 Tier-2 的定时值**同源**，均取执行 prompt 下发的 `recovery_timer_seconds=<N>s` 整数值（`structured_params_hint` 已渲染），**禁止自算缓冲**：

- 观察窗 `D` = `duration_seconds`（框架在场义务，严禁侵蚀或擅延）；
- 安全网窗 `D+G` = `recovery_timer_seconds`（故障自治恢复点的武装值，G 为程序立法的恢复宽限）；
- Tier-1 的 `--on-active=<N>s` 与 Tier-2 的 `sleep <N>` 取**相同** `<N>` = D+G，保证框架在观察窗终点（onset+D）主动派发的恢复**先于**故障自治恢复（onset+D+G）落地。

---

## 五、武装纪律与重武装

- 倒计时从**武装时刻**起算，武装与注入必须**紧邻**（间隔 ≤ 60 秒）；先武装成功、再注入。
- 武装后发生任何**逆命令修正**，必须先终止旧定时器再全额重武装（窗口契约不被侵蚀也不被擅延）：
  - **Tier-1**：`systemctl stop <unit-name>.service; systemctl reset-failed <unit-name>.service`（同名 transient unit 重武装前清残留；武装成功过的 unit 无残留可直接重武装）。
  - **Tier-2**：终止对应 setsid 会话进程——`pkill -f 'sleep <recovery-seconds>'` 用括号法防自匹配（如 `pkill -f 'sleep 60[0]'`），再以修正后逆命令重新 `setsid --fork …` 武装。

---

## 六、恢复幂等

逆命令**必须幂等**——定时器到期自治执行与框架主动兜底执行**双执行无副作用**：
- `rm -f <file>`：文件已删则 no-op（`-f` 保证不因缺失而失败）；
- `truncate -s 0 <file>`：重复置零幂等；
- `kill -CONT <pid>` / `iptables -D <rule>`：重复解除/删除已无副作用（删除不存在的规则须容忍失败）。

提前恢复（框架主动）与自治恢复（定时器到期）走**同一逆命令**，二者对结果无差异。

---

## 七、fail-closed 边界

systemd 不在场**且** Tier-2 前置二进制（`setsid`/`sh`/`sleep`）任一缺失时：
- **必须显式报告**「本环境无可用自治恢复载体」及缺失原因（缺哪个二进制）；
- **禁止**注入需要自恢复安全网的自定时故障；
- **禁止**静默放弃恢复能力而不报告——不得出现「已注入但无安全网」的静默状态。

---

## 八、取证约束（Tier-2 弱于 Tier-1，事实性警示）

- **Tier-1**：可用 `journalctl -u <unit-name>` 查证 timer 是否 fire、逆命令是否执行。
- **Tier-2**：payload **不写任何取证文件**（守 discard-sink 不变量，见第三节），故无载体侧 fire 日志。fire 是否发生的可观测性由**框架侧**承担：注入前后只读观测靶标状态差（如 `df -h <path>` 磁盘占用回落、`pgrep` 进程状态翻转）+ 任务台账 recovery_handle 记录。
- **Tier-2 fire 后载体可能残留为 zombie（机制不变量，非缺陷，勿据此判恢复失败）**：`setsid --fork` 的子进程脱离会话后 reparent 到 PID 1；当 PID 1 是**非 reaper**（如最小容器的 `sleep infinity` init、任何不调用 `wait()` 收割子进程的裸机 init）时，载体 `sh` 在 fire（逆命令执行完毕）退出后会残留为 `[sh] <defunct>` 僵尸且**永不被收割**。僵尸是**已终止**进程，只占一个 PID 槽、不持任何资源、逆命令已执行——**属预期残留，不是活体载体**。据此，Tier-2 的**恢复权威判据是「无 LIVE `sleep <recovery-seconds>`/setsid 载体进程」+ 靶标状态已回落**（如冻结进程 `ps -o stat= -p <pid>` 由 T 复原为 S/R、填充文件消失、被删规则消失），**而非**「进程表零载体条目 / 僵尸消失」；验证与恢复相位**不得把 `[sh] <defunct>` 误判为活体残留**（否则会把已成功恢复假阴性降级）。僵尸不可 kill、无需清理，随宿主重启或 reaper 出现自然消解。（环境里预存多少个僵尸是集群相对事实，只入台账，不在本标准件钉数字。）
- 这是 discard-sink 不变量与取证丰富度的**明确取舍**：Tier-2 换取「不假设任何预装守护进程、fire 精度约 0s、天然抗会话断开」，代价是载体侧取证弱于 Tier-1。需要强取证时优先确认 systemd 是否可用（Tier-1）。
