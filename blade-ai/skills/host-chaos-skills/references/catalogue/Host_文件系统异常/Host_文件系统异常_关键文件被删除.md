**用例名称** 关键文件被删除 导致 Host_文件系统异常

## 故障定位
持续型故障——目标文件被删除（移走）后在原路径持续缺失，贯穿整个故障窗口；窗口结束（`--timeout` 到期 / 武装的自恢复载体到期 `mv` 回）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是让目标文件在原路径消失、使读它的应用报 No such file or directory），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理）；否则手段2。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 关键文件（配置文件/数据文件/日志文件）被意外删除
2. 应用启动失败或运行异常
3. 数据丢失

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标文件路径：`ls -la <filepath>`
3. 确认该文件有备份或可重新生成

## 演练步骤
1. 记录目标文件信息：`ls -la <filepath>` 和 `md5sum <filepath>`
2. 注入文件删除 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create file delete --filepath <target-file> --timeout <duration>
```

参数说明：
- `--filepath`：目标文件路径（必填）
- `--force`：可选，强制删除（不可恢复，演练中不建议使用）
- `--timeout`：超时自动恢复（秒），非 force 模式下 destroy 可恢复文件

3. 观察应用对文件缺失的反应

## 注入验证
1. `ls <filepath>` 确认文件不存在
2. 观察应用日志中的 "No such file or directory" 错误
3. 确认应用行为变化（如无法启动、功能异常）

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
> 注意：非 --force 模式下，destroy 会恢复被删除的文件

**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的定时器——Tier-1 `systemctl stop` / Tier-2 `pkill` 括号法——再 `mv` 回原路径）

## 恢复验证
1. `ls -la <filepath>` 确认文件恢复
2. `md5sum <filepath>` 确认内容完整性
3. 确认应用恢复正常运行

## 基准事实
- **根因**：关键文件被意外删除（人为误操作、恶意攻击等），导致应用无法正常运行
- **必现现象**：文件不存在；应用报 No such file or directory；服务异常或无法启动

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（`mv` 移走目标文件使其在原路径消失）；主机未装 ChaosBlade 时选用本手段。

前提条件：需 `mv` 命令；自恢复载体需 `systemd-run`（Tier-1，主机有 systemd）或 `setsid`+`sh`+`sleep`（Tier-2，主机无 systemd），二者皆不可用则 fail-closed（见标准件文档 §七）。

注入命令（**先武装定时恢复，再注入**——武装载体按主机是否有 systemd 分层选型〔探测门 → Tier 选择 → 武装〕，构造细节/纪律/取证约束见 `references/carrier/recovery-carrier.md`〔host 域分层自恢复载体单一权威，本 case 只引用不内嵌〕；`<recovery-seconds>` 一律取 prompt 下发的 `recovery_timer_seconds`〔= duration + grace，见 SKILL.md 双数窗口契约〕，Tier-1 的 `--on-active` 与 Tier-2 的 `sleep` 同源取此值，禁自算缓冲）：
```bash
# 0) 只读探测 systemd 是否在场（任一信号为真即判在场，degraded 仍算在场，不因 degraded 降级）：
systemctl is-system-running          # 只读；bus 可达即 systemd 在场
# 或只读判据： [ -d /run/systemd/system ]
#   → 在场选 Tier-1（systemd-run transient timer）；不在场（两信号皆否，如最小容器 / 非 systemd init 裸机）
#     选 Tier-2（setsid detached-timer）；两 Tier 前置皆不可用（systemd 不在场 且 setsid/sh/sleep 任一缺失）
#     → fail-closed：显式报告「本环境无可用自治恢复载体」及缺失项，禁止注入无安全网的自定时故障（见标准件文档 §七）。
#   下面给出 Tier-1 / Tier-2 两种武装形态，按探测结果二选一；武装成功后（Tier-1 输出含 "Running timer as unit"、
#   Tier-2 即刻返回且 ps 见 PPID=1 detached 会话）再执行移走。各命令独立执行（执行通道不支持在工具调用层 && 串联）。
# 移走而非删除：原文件即备份，同文件系统内为原子操作，不存在「备份成功但删除失败」或「备份失败却已删除」的中间态。

# Tier-1（systemd 在场）：
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-filedel \
  mv <filepath>.orig <filepath>
# Tier-2（systemd 不在场；逆命令已在外层 sh -c 内，直接写、禁再嵌套 sh -c——payload 禁嵌套 interpreter）：
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && mv <filepath>.orig <filepath>'
# 武装成功后再移走文件：
mv <filepath> <filepath>.orig
```

恢复命令（timer 到期前可提前手动恢复，同时停掉已武装的定时器——按武装时选用的 Tier 停对应定时器）：
```bash
# 提前恢复：移回原路径，不留残留备份文件
#   Tier-1：systemctl stop blade-restore-filedel.timer 2>/dev/null
#   Tier-2：pkill -f 'sleep <recovery-seconds>' 用括号法防自匹配（如 pkill -f 'sleep 42[0]'，见标准件文档 §五）
mv <filepath>.orig <filepath>
```

注意事项：
- 操作前必须备份（`mv .orig` 移走本身即备份，原文件就是备份件），否则数据不可恢复
- 自恢复基于注入前武装的**分层自恢复载体**（Tier-1 主机有 systemd 时 systemd-run transient timer / Tier-2 主机无 systemd 时 setsid detached-timer；选型探测门、武装/重武装纪律、payload 文法〔Tier-2 禁嵌套 interpreter，逆命令直接写在外层 sh -c 内〕、fail-closed 边界、Tier-2 取证约束见 `references/carrier/recovery-carrier.md`），到期自动 `mv` 回原路径，补齐了 ChaosBlade `--timeout` 的自恢复能力；提前恢复仍用上方手动命令
- Tier-2 payload 守 discard-sink 不变量、**不写任何取证文件**，故无载体侧 fire 日志属预期取舍（见标准件文档 §八）；fire 是否发生由框架侧只读观测靶标状态差承担，不得据此误判武装失败
- 武装与移走分两次独立执行，须确认武装成功后再执行移走（等价于 `&&` 串联的失败短路保证）
- 演练环境不要对无法重建的唯一数据文件做此操作
- Tier-1 同名 transient timer 重复武装会报 `Unit blade-restore-filedel.service was already loaded`（上次武装失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-restore-filedel.service; systemctl reset-failed blade-restore-filedel.service`（武装命令成功执行过的 unit 无残留，可直接重武装）。Tier-2 重武装先 `pkill -f 'sleep <recovery-seconds>'`（括号法）终止旧会话再重新 setsid（见标准件文档 §五）
