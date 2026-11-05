**用例名称** 系统时钟漂移 导致 Host_时间偏移

## 故障定位
持续型故障——系统时钟被整体偏移，与真实时间在整个故障窗口内显著不一致；窗口结束（手段1 `--timeout` 到期 / 手段2 武装的自恢复载体到期重启时间同步服务并校时）即自动校回真实时间。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是把系统时钟偏移一个量，使依赖系统时间的应用出现证书校验异常 / 定时任务错乱 / 日志时间戳失序），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理、`--timeout` 自带恢复）；否则手段2（原生 `date -s` + 先武装定时恢复载体再偏移）。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 系统时间与真实时间不一致
2. 证书验证失败（SSL/TLS 证书过期判断异常）
3. 日志时间戳混乱，分布式系统因果序关系错乱
4. 定时任务触发异常（cron 提前或延迟执行）

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测时间指标
3. 确认目标主机上全部会校回系统时钟(CLOCK_REALTIME)的时间同步源：NTP 服务（ntpd / chronyd / systemd-timesyncd），以及虚拟化环境下 hypervisor 侧的 guest-agent 时间同步（见文末手段2「注意事项」）

## 演练步骤
1. 记录基线（三项都取，恢复验证要拿它作对照）：
   - `date` 与 `timedatectl status`：系统时间、RTC 时间、`System clock synchronized`、`NTP service` 状态
   - `systemd-detect-virt`：靶机是否处于虚拟化之下（输出 `none` = 裸机）。**虚拟化靶机必须先取这一项**——guest 时钟是否被 hypervisor 侧校正决定了偏移能否 hold、以及恢复期哪个校时源可信；`--vm` / `--container` 可进一步缩小范围。**但 `none` 不构成「无 guest-agent 校时」的否证**：部分 hypervisor 隐藏 systemd 依赖的 DMI/SMBIOS 标记，真 VM 也可能返回 `none`；guest-agent 是否校时是运行时行为，须按文末手段2 步骤1 读其 journal 确认，不得据本项或单元 Description/ExecStart 就判定偏移能 hold
   - 装有 chrony 时取 `chronyc tracking`：`System time` 偏移量、`Stratum`、`Leap status`。这是时钟是否真正锁定的权威读数，`timedatectl` 的 `synchronized` 标志只是它的下游产物
2. 注入时间偏移 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create time travel --offset <offset> --timeout <duration>
```

参数说明：
- `--offset`：时间偏移量（必填），支持格式如 5m30s、-2h30m、1h
- `--disableNtp`：可选，是否禁用 NTP（默认 true；如系统不支持 NTP 设为 false）。**注意**：它只禁用 ntpd / chronyd 等 NTP 服务，不停虚拟化环境 hypervisor 侧的 guest-agent 时间同步；在虚拟化靶机上偏移仍可能被 guest-agent 从宿主机硬校回，此时改用文末**手段2（原生命令）**（可显式停 guest-agent）。**该降级触发条件不能用静态探测否证**——`systemd-detect-virt=none`、无 qemu-guest-agent、单元 Description 只显示传输职责，都排除不了某个在跑的 guest-agent 周期校时；须按文末手段2 步骤1 读候选 journal 确认后才能断定手段1 能 hold 偏移。blade 返回 `Status: Success` 只证明命令执行、不证明偏移 hold，注入后须在一个疑似校时周期内复核偏移是否仍在
- `--timeout`：超时自动恢复（秒）

3. 观察依赖时间的服务和组件的反应

## 注入验证
1. `date` 确认系统时间已偏移——须与一个**靶机外**的权威真实时间对照（虚拟化靶机用宿主机时间，否则用另一台未注入机器或可信时间源），只读靶机自身的 `date` 无法区分「已偏移」与「本来就是这个时间」。`timedatectl status` 的 `Universal time` 与 `RTC time` 之差可作靶机内的独立佐证（偏移只动 CLOCK_REALTIME、不动 RTC）
2. 尝试建立 HTTPS 连接，观察是否出现证书相关错误
3. 观察定时任务是否异常触发（`systemctl list-timers --all` 的各 timer `elapsed-since-LAST` 会随墙钟一起膨胀；带 unit 参数的过滤形查询对 `systemd-run` 创建的 transient timer 假阴性返回 `0 timers listed`，须带 `--all` 或改用 `systemctl show <unit>.timer -p ActiveState,SubState,NextElapseUSecMonotonic`）
4. 检查分布式系统日志时间戳一致性

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```

**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的定时器，再启回注入时停掉的时间同步服务并强制校时）

## 恢复验证
1. **权威判据（外部对照）**：靶机 `date` 与步骤 1 记录的同一个靶机外权威真实时间之差回落至约 0。这是唯一不可绕过的判据——只读靶机自身的任何时钟读数都无法自证已校回
2. **锁定判据（chrony 环境）**：`chronyc tracking` 的 `System time` 偏移量回落至基线量级（正常锁定常在毫秒级）、`Stratum` 为有限值、`Leap status` 为 `Normal`
3. `timedatectl status` 作**佐证而非判据**：多个校时源共同 discipline 时钟时（chrony 与 hypervisor guest-agent 并存是常见形态），`System clock synchronized` 会在 `no`↔`yes` 间 flap——这是标志的下游产物属性，不代表真实失步，勿据此判恢复失败，以第 1/2 项为准
4. **虚拟化靶机的实际校时者归因**：硬校回可能由 hypervisor guest-agent 的 SyncTime 完成而非 chrony 的 step——chronyd 会记录 `System clock was stepped by 0.000000 seconds`（首次量测前 `chronyc makestep` 是 no-op，见文末注意事项）。**不要把这个 0 秒 step 误读为「恢复未发生」**；需要归因时读 `journalctl -u <校时服务>`（校回瞬间会有 `Backward time jump detected` 类时间戳断点），但恢复与否仍以第 1 项裁决
5. `hwclock --show` 对照系统时间，确认 RTC 与系统时钟一致（`--show`/`-r`/`--get` 是只读形态；`-s`/`--hctosys`/`-w`/`--systohc`/`--adjust` 会写时钟，不属验证动作）
6. 确认应用时间相关功能恢复正常
7. 确认注入期停掉的每一个校时服务都已启回、且自恢复载体已 fire 并干净 GC（`systemctl list-units --all` / `list-unit-files` / `/run/systemd/transient/` 三处零残留；`failed` 态 transient unit 不被 systemd 自动 GC，须 `systemctl reset-failed` 清理）

## 基准事实
- **根因**：系统时钟发生漂移（NTP 故障、硬件时钟异常等），导致时间敏感的功能异常
- **必现现象**：系统时间与实际时间不符；TLS 证书可能验证失败；定时任务异常；日志时间戳混乱

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（`date -s` 偏移系统时钟）；主机未装 ChaosBlade 时选用本手段。

前提条件：具备 root 权限

注入命令（**先武装定时恢复，再注入**；到期自动重启时间同步服务并校时，补齐自恢复能力；
武装→停服务→改时间逐条独立执行，武装未确认成功（输出含 Running timer as unit）时不得执行偏移）：
```bash
# 1) 枚举本机全部会校回系统时钟(CLOCK_REALTIME)的时间源，不要盲试、也不要只查 NTP 服务
systemctl is-active ntpd
systemctl is-active chronyd
systemctl is-active systemd-timesyncd
# 虚拟化靶机（KVM/QEMU、云厂商 VM 等）除上述 NTP 服务外，hypervisor 侧的 guest-agent
# 时间同步（如 qemu-guest-agent 的 time-sync、ptp_kvm、厂商 VM guest agent 服务）也会
# 绕过 ntpd/chronyd 直接从宿主机硬校回 CLOCK_REALTIME；枚举 running 服务只列出「候选」：
systemctl list-units --type=service --state=running
# 关键：枚举只给出候选，判不了它是否真校时——校时是运行时行为、不是静态属性。单元
# Description / ExecStart 只反映传输职责（挂载 / 端口转发 / vsock 等），看不到内部的周期
# 校时循环；systemd-detect-virt 也可能在真 VM 上返回 none（hypervisor 隐藏 DMI/SMBIOS 标记）。
# 唯一可靠确认 = 读候选的近期运行时 journal，在输出里找固定周期的「与宿主机校时 /
# step CLOCK_REALTIME / drift」类日志（措辞因 agent 而异）：
journalctl -u <候选-guest-agent-unit> --no-pager -n 50
# 注意：执行通道不接受管道 / 重定向，直接读全量输出自行筛查，勿加 `| grep`（会被守卫拒）。
# 判读：出现周期校时日志 ⇒ 它会硬校回 CLOCK_REALTIME，手段1（仅 --disableNtp）hold 不住
# 偏移，必须走手段2 并把它纳入停服集（见下）。journal 无定论但候选在跑 ⇒ 按「疑似校时源」
# 对待（静态探测查不到证据 ≠ 不校时），在爆炸半径允许下纳入停服集，勿默认无害而选手段1。

# 2) 武装定时恢复（定时器由宿主机 systemd(PID 1) 管理）→ 停服务 → 改时间。
#    三条命令分次独立执行（执行通道不支持 && 串联）；前一条成功返回（武装以输出含
#    Running timer as unit 为准）后再执行下一条，武装失败时不得继续
# <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
# 非虚拟化靶机 / 无 hypervisor guest-agent 时间同步：payload 与停服务都只针对 chronyd（或实际那个 NTP 服务）
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-ntp \
  sh -c 'systemctl start chronyd; chronyc makestep 2>/dev/null || true'
systemctl stop chronyd
# 虚拟化靶机额外一步：把步骤1 探得的 hypervisor guest-agent 时间同步服务一并纳入——
# 上方载体 payload 里追加 `systemctl start <hypervisor-guest-agent-unit>`（供到期自恢复启回），
# 停服务追加下面这行；否则 guest-agent 会在数十秒内从宿主机硬校回 CLOCK_REALTIME、偏移无法持续。
# 无此服务时省略下一行（并删去 payload 里对应追加的 start）：
systemctl stop <hypervisor-guest-agent-unit>
date -s "<offset>"   # 偏移量按演练目标确定，如 "+2 hours"（向前）、"-30 minutes"（向后）

# 若两者都没有，改用 timedatectl 关闭同步（武装对应还原，同样逐条独立执行）：
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-ntp \
  timedatectl set-ntp true
timedatectl set-ntp false
date -s "<offset>"
```

恢复命令（提前恢复；先停武装的定时器再手动还原）：
```bash
# 0) 终止武装的定时器
systemctl stop blade-restore-ntp.timer 2>/dev/null

# 1) 启回注入时停掉的那个服务（与注入步骤对应，不要盲试）
systemctl start chronyd
# 虚拟化靶机：若注入时停过 hypervisor guest-agent 时间同步，这里一并启回，使靶机回归演练前状态
systemctl start <hypervisor-guest-agent-unit>

# 若注入时用的是 timedatectl
timedatectl set-ntp true

# 2) 强制拉一次时间（按实际可用工具选一条）
chronyc makestep
ntpdate pool.ntp.org
```

注意事项：
- 时间偏移会影响所有依赖系统时钟的应用（日志、证书、定时器、分布式一致性）
- **虚拟化靶机的隐藏时间源**：KVM/QEMU、云厂商 VM 等虚拟化环境里，hypervisor 侧的 guest-agent 时间同步（如 qemu-guest-agent 的 time-sync、ptp_kvm、厂商 VM guest agent 服务）会独立于 ntpd/chronyd 周期性地把客户机 CLOCK_REALTIME 硬校回宿主机时间。仅停 NTP 服务而不停该 guest-agent 时，`date -s` 的偏移会在数十秒内被静默校回、持续型故障无法成立；故注入前须枚举全部时间源（NTP 服务 + hypervisor guest-agent），**并用运行时 journal 确认候选 guest-agent 是否真的周期校时**——校时是运行时行为、非静态属性，`systemd-detect-virt`（真 VM 也可能返回 none）、单元 Description / ExecStart（只反映传输职责）均不可靠，**静态探测查不到校时证据 ≠ 该 guest-agent 不校时**，无法排除时按疑似校时源处理、勿据「枚举没看出校时」就断定手段1 能 hold（确认方法见手段2 步骤1）；在爆炸半径允许下一并临时停掉（guest-agent 时间同步的停用一般限于该 VM 与其宿主机之间的校时、可逆），恢复侧对应启回。裸金属 / 无此类 guest-agent 的靶机不涉及本条。
- **RTC 是观测面不是恢复面**：偏移只动 CLOCK_REALTIME，`hwclock --show` 读到的 RTC 仍走真实时间，故「系统时间 vs RTC」之差能在靶机内独立佐证偏移量级；但 `hwclock -s`/`--hctosys` 不是可靠的校时手段——它依赖虚拟 RTC 与真实时间的关系，而该关系在虚拟化环境下由 hypervisor 的 RTC 模型决定、不保证等于真实时间，且它是写系统时钟的动作，勿写进验证或恢复判据
- **只读观测形态**：`chronyc` 的监控动词（`tracking`/`sources`/`sourcestats`/`ntpdata`/`activity` 等）、`hwclock --show`/`-r`/`--get`、`hostnamectl status`、`systemd-detect-virt` 与 `timedatectl status` 同为只读诊断形态，可用于基线与验证段；而 `chronyc makestep`/`manual on|off|reset`/`waitsync`、`hwclock -s`/`-w`/`--systohc`/`--adjust`、`hostnamectl set-*` 是写动作，只属注入或恢复段（其中 `hwclock -s` 按上条不作为校时手段），一律不得写进验证步骤
- 自恢复基于注入前武装的 systemd-run transient timer（到期自动启回时间同步服务并校时）；提前恢复仍用上方手动命令
- 同名 transient timer 重复武装会报 `Unit blade-restore-ntp.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-restore-ntp.service; systemctl reset-failed blade-restore-ntp.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
- 恢复后时间是否立即校回取决于偏移量与 chrony.conf `makestep` 阈值（如 `makestep 10 3` 表示仅启动后前 3 次更新中偏差 >10 秒才自动步进）：偏移量 ≤ 阈值时 chronyd 走纯 slew 缓慢修正（如 +10 秒偏移约 2.5 分钟收敛），时间敏感的恢复验证需预留等待窗口或手动 `date -s` 精确校回；`chronyc makestep` 在 chronyd 刚启动、尚未完成首次测量时执行是 no-op
