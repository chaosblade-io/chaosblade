**用例名称** 磁盘被填满 导致 Host_磁盘空间不足

## 故障定位
持续型故障——填充文件占用目标分区空间，文件存活即故障存活，贯穿整个故障窗口；窗口结束（`--timeout` 到期删除 / 武装的分层自恢复载体（Tier-1 systemd timer / Tier-2 setsid detached-timer）到期 `rm`）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是创建大文件占满目标分区、抬高 `df` Use%），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理）；否则手段2。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 目标分区磁盘使用率超过 90%
2. 应用写入失败（No space left on device）
3. 日志写入中断，可能导致服务异常

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测磁盘空间（如 `df -h`、Prometheus node_exporter）
3. 确认目标目录所在分区非根分区或已设置安全 reserve

## 演练步骤
1. 记录当前磁盘基线：`df -h <path>`
2. 注入磁盘填充 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create disk fill --path <target-path> --percent <percent> --timeout <duration>
```

参数说明：
- `--path`：填充目标目录（必须为已存在的目录）
- `--percent`：目标分区使用率百分比（如 90、95）
- `--size`：可选，直接指定填充大小（MB），与 percent 互斥时 percent 优先
- `--reserve`：可选，保留空间大小（MB）
- `--retain-handle`：可选，保留文件句柄（rm 后仍占空间）
- `--timeout`：超时自动恢复（秒）

3. 观察磁盘使用率及应用写入情况

## 注入验证
1. `df -h <path>` 确认磁盘使用率达到目标百分比 —— **这是目标使用率的权威判据**（对比注入前基线的 Use%）。
2. 尝试在该分区写入文件确认失败：`dd if=/dev/zero of=<path>/test bs=1M count=1`
   - **定案（勿重推）**：写探针只在分区剩余空间小于探针写入量时才因 `No space left on device` 失败。`--percent` 小于 100 时分区仍留有剩余空间，小幅写入会成功、不返回 ENOSPC，故写探针对"是否达到目标使用率"不具判别力；是否达到目标使用率一律以上条 `df` 的 Use% 为准，勿据写探针成功反推故障未生效。
   - 验证阶段以只读方式取证：`dd of=<真实路径>` 属写操作、会被只读门拒绝，故验证相不依赖写探针，判据以只读的 `df Use%` 为准。
3. 观察应用日志是否出现 "No space left on device" 错误（同样只在剩余空间耗尽时出现，非目标使用率的判别信号）

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的 timer 再清理填充文件）

## 恢复验证
1. `df -h <path>` 确认磁盘空间恢复
2. 确认应用写入恢复正常

## 基准事实
- **根因**：磁盘空间被大量占用（日志堆积、临时文件未清理等），导致分区剩余空间不足
- **必现现象**：磁盘使用率持续超过目标百分比；写入操作失败；应用出现磁盘相关错误

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（`fallocate`/`dd` 创建大文件占满目标分区）；主机未装 ChaosBlade 时选用本手段。

前提条件：主机需具备 `dd` 或 `fallocate` 命令；自恢复载体需 `systemd-run`（Tier-1，主机有 systemd）或 `setsid`+`sh`+`sleep`（Tier-2，主机无 systemd），二者皆不可用则 fail-closed（见标准件文档 §七）

注入命令（填充量必须按**增量**计算，先测基线）：
```bash
# 0) 先测目标分区基线：总容量、已用量、可用量
df -h <path>

# 1) 填充量 = 分区总容量 × 目标使用率 − 当前已用量
#    例：分区 100G、已用 40G、目标 90% → 100×0.9 − 40 = 50G

# 2) 先武装定时清理（到期自动删除填充文件，补齐自恢复能力），再填充。
#    武装与填充分两次独立执行（执行通道不支持在工具调用层 && 串联）；武装成功后再执行填充。
#    武装载体按主机是否有 systemd 分层选型（探测门 → Tier 选择 → 武装），构造细节/纪律/取证约束
#    见 references/carrier/recovery-carrier.md（host 域分层自恢复载体单一权威，本 case 只引用不内嵌）。
#    <recovery-seconds> 一律取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md
#    双数窗口契约）；Tier-1 的 --on-active 与 Tier-2 的 sleep 同源取此值，禁自算缓冲。

# 2a) 只读探测 systemd 是否在场（任一信号为真即判在场，degraded 仍算在场，不因 degraded 降级）：
systemctl is-system-running          # 只读；bus 可达即 systemd 在场
# 或只读判据： [ -d /run/systemd/system ]

# 2b-Tier-1) systemd 在场 → systemd-run transient timer（延迟由 timer 提供，payload 为单条逆命令）。
#    武装成功输出含 "Running timer as unit ..."；成功后再执行第 3 步填充。
systemd-run --on-active=<recovery-seconds>s --unit=blade-rmfill-host \
  rm -f <path>/app-archive.dat

# 2b-Tier-2) systemd 不在场（2a 两信号皆否，如最小容器 / 非 systemd init 裸机）→ setsid detached-timer。
#    四个构成性约束：--fork（缺之则同步阻塞调用方整个恢复窗，arm-then-inject 结构性失败）；
#    payload 首句 exec >/dev/null 2>&1（断开子进程从 SSH/exec 通道继承的 fd1/fd2，令 arm 秒回而非阻塞
#    整个 sleep 窗；MUST 写在 sh -c 脚本内——写在 setsid argv 层会被传输 shlex.quote 引号化成死参数、到不了远端 shell）；
#    && 分隔（payload 内 sleep 提供延迟；`; rm` 会命中链式删除硬地板，对两 Tier 对称拒）；
#    <recovery-seconds> 同上取 recovery_timer_seconds。武装即刻返回、子进程脱离会话（PPID=1），到点自治 fire。
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && rm -f <path>/app-archive.dat'

# 2c) 两 Tier 前置皆不可用（systemd 不在场 且 setsid/sh/sleep 任一缺失）→ fail-closed：
#    显式报告「本环境无可用自治恢复载体」及缺失项，禁止注入无安全网的自定时故障（见标准件文档 §七）。

# 3) 填充（与 Tier 选择无关，武装成功后执行）——使用 fallocate 快速创建大文件（推荐，速度快）
fallocate -l <算出的填充量>G <path>/app-archive.dat

# 若主机无 fallocate：用 dd 替代上面的 fallocate（武装保持不变、同样须先武装成功再填充，
# 禁止单独执行 dd 而漏掉武装）。Tier-1 武装形如：
# systemd-run --on-active=<recovery-seconds>s --unit=blade-rmfill-host \
#   rm -f <path>/app-archive.dat
# Tier-2 武装形如： setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && rm -f <path>/app-archive.dat'
# dd if=/dev/zero of=<path>/app-archive.dat bs=1M count=<填充量换算的MB数>
```
> 量太小达不到目标使用率；量太大把分区填到 100% 会影响验证观察与恢复阶段写入，
> 甚至触发非预期的系统级故障（日志写入中断、服务崩溃）。

恢复命令（提前恢复；先停武装的定时器再清理填充文件——按武装时选用的 Tier 停对应定时器）：
```bash
# Tier-1（systemd-run 武装）：停 transient timer
systemctl stop blade-rmfill-host.timer 2>/dev/null
# Tier-2（setsid 武装）：终止 detached sleep 会话（见标准件文档 §五）
pkill -f 'sleep <recovery-seconds>' 2>/dev/null
# 两 Tier 通用：幂等清理填充文件（rm -f / truncate -s 0 重复执行无副作用）
truncate -s 0 <path>/app-archive.dat
```

注意事项：
- 原生方式无法按百分比精确控制，需按**增量**手动计算填充大小（填充量 = 分区总容量 × 目标使用率 − 当前已用量）
- 自恢复基于注入前武装的**分层自恢复载体**（Tier-1 主机有 systemd 时 systemd-run transient timer / Tier-2 主机无 systemd 时 setsid detached-timer；选型探测门、武装/重武装纪律、payload 文法、fail-closed 边界、Tier-2 取证约束见 `references/carrier/recovery-carrier.md`），到期自动删除填充文件；提前恢复仍用上方手动命令
- 注意不要对根分区执行填满操作
- Tier-1 重武装：同名 transient timer 重复武装会报 `Unit blade-rmfill-host.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-rmfill-host.service; systemctl reset-failed blade-rmfill-host.service`（武装命令成功执行过的 unit 无残留，可直接重武装）。Tier-2 重武装：先终止旧 setsid 会话（`pkill -f 'sleep <recovery-seconds>'`）再以修正后逆命令重新武装，纪律见标准件文档 §五
