**用例名称** 进程数过多 导致 Host_进程异常

## 故障定位
持续型故障——故障窗口内批量创建进程（类 fork bomb）占用进程槽位，逼近内核 `pid_max` / `ulimit -u` 上限，新进程创建变慢或失败、系统响应迟钝，故障贯穿整个窗口；窗口结束（手段1 `--timeout` 到期实验销毁 / 手段2 stress-ng `--timeout` 自停）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是批量创建进程耗尽进程槽位、窗口终点自动收敛），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 优先手段1（实验 UID 统一生命周期管理）；否则手段2（`stress-ng --fork`，其 `--timeout` 自带自恢复，无需额外武装定时载体）。**手段1 的 `success:true` 不等于故障已落地**——注入后必须在故障窗内 live 核实进程数 / `cat /proc/loadavg` 确实上升（见「注入验证」的免管道判据）；若核实无真实负载（某些宿主上该 action 的内部前置探测会静默失败、错误不冒泡，外层仍报 success），须 `blade destroy <uid>` 清残留后转手段2。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。**本故障属高危主机级操作**——进程槽位耗尽可能波及 SSH 会话自身与主机上其他进程，注入前须核实爆炸半径并确认 out-of-band 恢复通道（见文末注意事项）。

## 故障现象
1. 系统进程数接近或超过内核限制（pid_max）
2. 新进程创建失败（fork: Cannot allocate memory）
3. 系统响应变慢，服务无法启动新线程/进程

> **判据可达性（有界演练）**：现象 1「逼近 `pid_max`」与现象 2「fork 失败」在受控演练中**不可安全触达**——现代内核 `pid_max` 常达数百万，真逼近它需同等量级进程，`threads-max` 与可用内存会先行耗尽，且必然波及 SSH 会话与 out-of-band 恢复通道（见文末注意事项）。故本 case 的**主判据收敛为「有界进程数增量 + fork 速率」**（见「注入验证」），现象 1/2 仅作故障机理说明、不作演练验收判据。

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认当前进程数基线：`ps -e --no-headers`（**免管道**——`host_read`/`host_inject` 的只读诊断拒绝任何未加引号的管道符，`ps aux | wc -l` 会被直接拒；逐行数 `ps -e --no-headers` 的输出行数即进程数）
3. 确认系统进程上限：`cat /proc/sys/kernel/pid_max`

## 演练步骤
1. 记录当前进程数：`ps -e --no-headers`（免管道，同上）
2. 注入进程数飙升 —— **手段1（ChaosBlade）**（主机无 blade、或手段1 经「注入验证」live 核实无真实负载时，改用文末**手段2（原生命令）**）

```bash
blade create process load --count <count> --timeout <duration>
```

参数说明：
- `--count`：创建的进程数（**必须为正整数且有界**；严禁 `0` / 不设 / `unlimited`——那等价 fork bomb，会耗尽进程槽位并波及 SSH 与恢复通道。取值须远低于 `threads-max`，演练建议 ~百级量级）
- `--user`：可选，以指定用户身份创建进程
- `--timeout`：超时自动恢复（秒）

3. 观察系统进程数变化及服务可用性

## 注入验证
1. **主判据（免管道）**：`ps -e --no-headers` 逐行数进程数，对比注入前基线确认显著增加（有界注入 ~百级量级时进程数应成倍上升）；`cat /proc/loadavg` 第 4 字段（`running/total`，如 `5/358`）佐证运行队列上升
2. **fork 速率佐证**：`vmstat 1 2` 的 `cs`（上下文切换/秒）激增、`sy`（内核态 CPU）升高、`r`（运行队列）变长——有界演练下这比「逼近 `pid_max`」更稳定地表征「进程数过多」
3. **手段1 假阳性甄别**：若走手段1，注入后窗内 `ps -e --no-headers` 进程数**未**较基线上升（即使 `blade` 返回 `success:true`），即判手段1 未落地 → `blade destroy <uid>` 清残留后转手段2（见「故障定位」选型规则）
4. 现象「新命令变慢/失败」「`dmesg` fork 失败日志」在有界演练中**通常不可触达**（见「故障现象」判据可达性），不作验收判据；如需查 `dmesg` 用免管道形式 `dmesg -T`（`dmesg | tail` 会被只读诊断拒）

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（stress-ng `--timeout` 到期自停；需提前终止用 `pkill -x stress-ng`）

## 恢复验证
1. `ps -e --no-headers`（免管道）确认进程数回落到基线水平
2. 确认新命令可正常执行

## 基准事实
- **根因**：大量进程被创建（类似 fork bomb），耗尽系统进程资源
- **必现现象**：进程数显著升高；新进程创建变慢或失败；系统响应迟钝

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（批量创建进程耗尽进程槽位、窗口终点自动收敛）；主机未装 ChaosBlade、或手段1 经「注入验证」live 核实无真实负载（`success:true` 假阳性）时选用本手段。stress-ng `--fork` stressor 自带 `--timeout` 自恢复，无需额外武装定时载体（区别于文件/网络/进程冻结类手段2 需 Tier-1/Tier-2 分层载体）。本手段属 **Branch B（自停型手段2）**——stress-ng 自带 `--timeout` 前台跑满窗口后自行退出即恢复、无需外挂载体；发射经 host_inject 前台直发，本地等待超时返回 outcome UNKNOWN 即预期且完整的发射回执（命令在主机续跑），随后**一条只读探针对账**即为权威回执，**严禁** setsid/systemd-run/nohup/`&` 包裹故障命令追同步返回；发射语义详见 `references/carrier/recovery-carrier.md` §〇 Branch B。

前提条件：建议先设置 ulimit 保护，避免影响恢复能力

注入命令：
```bash
# 用 stress-ng 的 fork stressor 批量占用进程槽位，自带超时不需人工清理。
# --fork 是否可用以 `stress-ng --help` 的实际输出为准。
# <count> 必须有界（正整数、远低于 threads-max，建议 ~百级量级）；
# 严禁无界/超大值——那等价 fork bomb，会波及 SSH 与恢复通道（见文末注意事项）。
stress-ng --fork <count> --timeout <recovery-seconds>s
```

`<recovery-seconds>`：安全网窗总时长（秒），取 prompt 下发的 `recovery_timer_seconds`
（= duration + grace，见 SKILL.md 双数窗口契约）——stress-ng 自停 `--timeout` 以它
武装，让框架在观察窗终点主动派发的恢复先于自治到期落地；上方 ChaosBlade 形态的
`--timeout` 由引擎在派发前按同一单源钉定，文档占位符保持 `<duration>` 不动

恢复命令：
```bash
# --timeout 到期后 stress-ng 自行退出，正常路径无需干预。
# 如需提前终止：fork stressor 的 worker 随主进程一同退出（kill 主 PID
# 后 worker 全清、fork 活动停止），杀主进程即可。
# 必须用 -x 精确匹配进程名，不要用 -f（见下方注意事项）。
pkill -x stress-ng
```

注意事项：
- 创建过多进程可能导致当前 SSH 会话无法创建新进程，建议提前设置 ulimit -u
- 原生方式无法精确控制，过量可能导致系统完全不可用
- 演练前建议确认可通过 out-of-band 方式（如 IPMI/iDRAC）恢复主机
- **「进程数显著增加」判据的命中强度依赖 worker 规模**：fork stressor 的每个 worker 持续 fork 短命子进程，瞬时进程数增量 ≈ N+1——N=4 时仅 +7（约 3%，不显著），N=64 时 +72（约 30%，显著）。判读时结合 fork 速率类证据更稳：`vmstat 1 2` 的 cs 列激增（如 28822/s→基线 5313/s）、sy CPU 升高（如 31%）、r 运行队列变长（如 r=4）
- **不要用 `pgrep -f stress-ng` 取 PID**：`-f` 匹配完整命令行，会把携带该字符串的执行 shell 自身一并匹配，拿输出去 kill 会杀掉执行 shell；`-x` 按进程名精确匹配，只命中 stress-ng 主进程
