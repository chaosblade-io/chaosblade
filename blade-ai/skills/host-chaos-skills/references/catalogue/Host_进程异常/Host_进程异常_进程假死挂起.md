**用例名称** 进程假死挂起 导致 Host_进程异常

## 故障定位
持续型故障——目标进程被 SIGSTOP 挂起后，进程仍存在但完全不处理任何请求，故障贯穿整个故障窗口；窗口结束（`--timeout` 到期实验销毁自动 SIGCONT / 武装的自恢复载体到期 `kill -CONT`）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是对目标进程发 SIGSTOP 将其挂起、窗口终点发 SIGCONT 恢复），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理）；否则手段2。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 目标进程存在但不响应任何请求
2. 服务端口监听但连接后无响应（超时）
3. 进程状态显示为 T（Stopped）

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标进程名或 PID：`ps aux | grep <process>`

## 演练步骤
1. 确认目标进程正在运行：`ps aux | grep <process>`
2. 注入进程假死（SIGSTOP）—— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create process stop --process <process-name> --timeout <duration>
```

参数说明：
- `--process`：进程名关键词
- `--process-cmd`：可选，按命令名匹配
- `--pid`：可选，直接指定 PID
- `--local-port`：可选，按监听端口匹配
- `--timeout`：超时自动恢复（秒），恢复时发送 SIGCONT

3. 观察进程状态及服务响应情况

## 注入验证
1. `ps aux | grep <process>` 确认进程状态为 T（Stopped）
2. `curl --connect-timeout 5 <service-url>` 确认连接后无响应
3. 健康检查（如 LB 心跳）是否触发报警（报警由 LB 多轮失败检测累积触发，存在传播
   延迟——首查未见报警不构成反证，挂起的直接证据是第 1 条的进程 T 状态）

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（destroy 自动发送 SIGCONT；`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的定时器——Tier-1 `systemctl stop` / Tier-2 `pkill` 括号法——再对目标 PID 发 `kill -CONT`）

## 恢复验证
1. `ps aux | grep <process>` 确认进程状态恢复为 S/R
2. 确认服务请求响应恢复正常

## 基准事实
- **根因**：进程被 SIGSTOP 挂起，虽然进程存在但完全不处理任何请求
- **必现现象**：进程存在但状态为 T（**恒成立**，挂起的直接权威证据）；**仅当目标是网络服务**（持有监听端口）时才表现为端口监听但连接无响应、健康检查/LB 心跳超时——**目标非网络服务（无监听端口）时后两项结构性不可观测，应判 not_applicable 而非 failed**，此时 T 状态即权威判据
- **T 状态归因（环境无关机制）**：权威证据是「进程仍存在 + 状态为 T」，可两层独立佐证——`ps -o stat= -p <pid>` 见 `T`、`/proc/<pid>/status` 见 `State: T (stopped)`。归因到本注入的 SIGSTOP（而非他因）的排除链：`TracerPid: 0` 排除 ptrace/调试器致停；目标为**无控制终端的会话首**（`TTY=?`、自持 `NSsid`）排除作业控制停（SIGTSTP/SIGTTIN/SIGTTOU）——二者成立则 T 必由显式 SIGSTOP 造成（SIGSTOP 不可被捕获/忽略，故 T 一经观测即稳定、非瞬态）

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（`kill -STOP` 挂起目标进程、窗口终点 `kill -CONT` 恢复）；主机未装 ChaosBlade 时选用本手段。

前提条件：自恢复载体需 `systemd-run`（Tier-1，主机有 systemd）或 `setsid`+`sh`+`sleep`（Tier-2，主机无 systemd），二者皆不可用则 fail-closed（见标准件文档 §七）。

注入命令（**先武装定时恢复，再注入**——武装载体按主机是否有 systemd 分层选型〔探测门 → Tier 选择 → 武装〕，构造细节/纪律/取证约束见 `references/carrier/recovery-carrier.md`〔host 域分层自恢复载体单一权威，本 case 只引用不内嵌〕；`<recovery-seconds>` 一律取 prompt 下发的 `recovery_timer_seconds`〔= duration + grace，见 SKILL.md 双数窗口契约〕，Tier-1 的 `--on-active` 与 Tier-2 的 `sleep` 同源取此值，禁自算缓冲）：
```bash
# 0) 只读探测 systemd 是否在场（任一信号为真即判在场，degraded 仍算在场，不因 degraded 降级）：
systemctl is-system-running          # 只读；bus 可达即 systemd 在场
# 或只读判据： [ -d /run/systemd/system ]
#   → 在场选 Tier-1（systemd-run transient timer）；不在场（两信号皆否，如最小容器 / 非 systemd init 裸机）
#     选 Tier-2（setsid detached-timer）；两 Tier 前置皆不可用（systemd 不在场 且 setsid/sh/sleep 任一缺失）
#     → fail-closed：显式报告「本环境无可用自治恢复载体」及缺失项，禁止注入无安全网的自定时故障（见标准件文档 §七）。
#   下面给出 Tier-1 / Tier-2 两种武装形态，按探测结果二选一；武装成功后（Tier-1 输出含 "Running timer as unit"、
#   Tier-2 即刻返回且 ps 见 PPID=1 detached 会话）再执行挂起。各命令独立执行（执行通道不支持在工具调用层 && 串联）。
#   逆命令 kill -CONT 的目标必须与注入 kill -STOP 的进程一致；到期重新 pgrep 取 PID（SIGSTOP 挂起的进程对 pgrep 仍可见）。

# 先取目标 PID（输出可能多行，逐个处理）：
pgrep -f <process-name>

# Tier-1（systemd 在场）：到期由宿主机 systemd(PID 1) 重新 pgrep 取 PID 发 SIGCONT
systemd-run --on-active=<recovery-seconds>s --unit=blade-cont-<process-name> \
  sh -c 'kill -CONT $(pgrep -f <process-name>)'
# Tier-2（systemd 不在场；逆命令已在外层 sh -c 内，直接写、禁再嵌套 sh -c——payload 禁嵌套 interpreter；
#   $( … ) 体内仅限 PID 探针 pgrep/pidof，供 kill -CONT 类进程恢复，见标准件文档 §三）：
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && kill -CONT $(pgrep -f <process-name>)'
# 武装成功后再挂起目标进程（对上面 pgrep 记录的同一 PID）：
kill -STOP <pid>
```

恢复命令（timer 到期前可提前手动恢复，同时停掉已武装的定时器——按武装时选用的 Tier 停对应定时器；SIGCONT 幂等，武装的定时器后续再触发也无副作用）：
```bash
# 提前恢复：对注入时记录的同一 PID 发送 SIGCONT
#   Tier-1：systemctl stop blade-cont-<process-name>.timer 2>/dev/null
#   Tier-2：pkill -f 'sleep <recovery-seconds>' 用括号法防自匹配（如 pkill -f 'sleep 42[0]'，见标准件文档 §五）
kill -CONT <pid>
```

注意事项：
- SIGSTOP 信号无法被进程捕获或忽略，进程必定被挂起
- 与 kill 不同，stop 后进程仍存在，资源未释放
- 自恢复基于注入前武装的**分层自恢复载体**（Tier-1 主机有 systemd 时 systemd-run transient timer / Tier-2 主机无 systemd 时 setsid detached-timer；选型探测门、武装/重武装纪律、payload 文法〔Tier-2 禁嵌套 interpreter，逆命令直接写在外层 sh -c 内、`$( … )` 体内仅限 PID 探针〕、fail-closed 边界、Tier-2 取证约束见 `references/carrier/recovery-carrier.md`），到期自动 `kill -CONT` 解除挂起，补齐了 ChaosBlade `--timeout` 的自恢复能力；提前恢复仍用上方手动命令
- SIGCONT 幂等——对已恢复（或已不存在）的进程再发 SIGCONT 无副作用；定时器自治 fire 与框架提前兜底双执行时第二次 CONT 属预期，不得据此判恢复失败（见标准件文档 §六）
- Tier-2 payload 守 discard-sink 不变量、**不写任何取证文件**，故无载体侧 fire 日志属预期取舍（见标准件文档 §八）；fire 是否发生由框架侧只读观测靶标状态差承担（如 `ps -o stat= -p <pid>` 由 T 恢复为 S/R），不得据此误判武装失败
- 武装与挂起分两次独立执行，须确认武装成功后再执行 `kill -STOP`（等价于 `&&` 串联的失败短路保证）
- Tier-1 同名 transient timer 重复武装会报 `Unit blade-cont-<process-name>.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-cont-<process-name>.service; systemctl reset-failed blade-cont-<process-name>.service`（武装命令成功执行过的 unit 无残留，可直接重武装）。Tier-2 重武装先 `pkill -f 'sleep <recovery-seconds>'`（括号法）终止旧会话再重新 setsid（见标准件文档 §五）
