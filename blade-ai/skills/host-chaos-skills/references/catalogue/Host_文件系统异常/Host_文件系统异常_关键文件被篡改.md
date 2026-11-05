**用例名称** 关键文件被篡改 导致 Host_文件系统异常

## 故障定位
持续型故障——目标文件被篡改（权限 / 内容 / 位置，三选一）后在整个故障窗内持续处于被篡改态，读它的应用持续报 Permission denied（权限变体）/ 解析错误（内容变体）/ No such file or directory（位置变体）；窗口结束（`--timeout` 到期或武装的 systemd timer 到期还原）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理）；否则手段2。**两手段各有三种对应的篡改变体**（权限：`chmod` / 内容：手段1 `append` 追加·手段2 `truncate` 清空 / 位置：`move`·`mv`），同一变体下两手段观测面等价；按演练目标选定一种变体，判据随变体而定（见「注入验证」）。**判据可达性关键 caveat**：权限变体 `chmod 000` 对 root 读者无效（root 绕过权限检查）——若注入通道以 root 运行、验证也以 root 读取，则「应用读取失败 Permission denied」判据**不可达**（`cat` 仍能读到内容），此时应选内容或位置变体，或改用非 root 应用视角核实。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 应用配置文件内容被修改或权限被篡改
2. 应用读取配置失败（Permission denied 或解析错误）
3. 服务行为异常或启动失败

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标文件路径及当前状态

## 演练步骤
1. 记录目标文件当前状态：`ls -la <filepath>` 和 `md5sum <filepath>`
2. 注入文件篡改 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）；下列三种变体按演练目标选定其一

方式一：权限篡改（应用无法读取）
```bash
blade create file chmod --filepath <target-file> --mark 000 --timeout <duration>
```

方式二：内容追加（配置文件被注入异常内容）
```bash
blade create file append --filepath <target-file> --content "<malicious-content>" --enable-backup=true --delete-file=true --timeout <duration>
```
> ⚠️ **`append` 变体的恢复正确性不得押在 ChaosBlade destroy 的内容还原语义上**——该语义**随 ChaosBlade 版本变化**，且工具自带 `--help` 都可能与实现矛盾（勿照抄 help 例子）。**版本无关的恢复保障三条**（任何版本都成立）：①`--enable-backup=true` **必定**在同目录创建原始快照 `<file>.chaos-blade-backup-<uid>`（后缀件，这是可依赖的稳定行为，与 destroy 是否自动还原无关）；②恢复侧**必须带外新鲜读靶**（`md5sum`/`ls -la`/`cat`）独立核实内容是否已还原，**绝不以「实验 Status=Destroyed / CLI overall=verified」为凭**（CLI 常在 `--timeout` fire 点前就 verified 退出）；③**若未自动还原，用该快照手动 `cp <file>.chaos-blade-backup-<uid> <file>`（+ chown/chmod 复位）+ `rm` 快照**（幂等兜底）。命令里的 `--delete-file=true` 只是「恰好能触发当前 vendored 版本自动还原」的便利，**不作为恢复依赖**；换 ChaosBlade 版本须重新实测 destroy 语义（1.8.2 实测细节见台账 `CASE_实测记录.md` Case #7）。

方式三：文件移动（配置文件消失）
```bash
blade create file move --filepath <target-file> --target /tmp --timeout <duration>
```

参数说明：
- `--filepath`：目标文件路径（必填）
- `--mark`：权限值如 000（chmod 方式）
- `--content`：追加内容（append 方式）
- `--enable-backup`：在同目录创建原始快照 `<file>.chaos-blade-backup-<uid>`（后缀件，非 `.<hash>` dotfile）。**这是恢复的可依赖基石——无论 destroy 是否自动还原，快照都在，可手动回写**
- `--delete-file`：destroy 时删除文件（默认 false）。**其对 append 还原的作用随版本而变（属版本相对事实，不入本文档正文）**：不得作为恢复正确性的唯一依赖，换版本须重新实测；恢复保障见方式二下 ⚠️ 注与「注入恢复」
- `--target`：移动目标目录（move 方式）
- `--timeout`：超时自动恢复（秒）

3. 观察应用对文件变化的反应

## 注入验证
> 判据随所选**变体**而定（手段1/手段2 同变体观测面等价）：权限变体看 `ls -la` 权限位、内容变体看 `cat` 内容、位置变体看 `ls` 是否消失。**权限变体 `chmod 000` 对 root 读者无效**——root 视角 `cat` 仍可读，「Permission denied」判据不可达，须以非 root 应用视角核实或改选内容/位置变体。**内容变体两手段方向不同**：手段1 `append` 追加异常内容（文件变大）、手段2 `truncate -s 0` 清空（文件变 0 字节），`cat` 均可观测到内容变化但形态不同。

1. `ls -la <filepath>` 确认权限变化（chmod 方式）
2. `cat <filepath>` 确认内容变化（append 方式）
3. `ls <filepath>` 确认文件不存在（move 方式）
4. 观察应用日志中的错误信息

## 注入恢复
**手段1（ChaosBlade）**——`blade destroy <uid>` / `--timeout` 到期销毁实验。**`append` 内容变体的恢复正确性不得依赖 destroy 的内容还原语义（随版本变化、`--help` 亦可能与实现矛盾）**：注入带 `--enable-backup=true`（必建原始快照 `<file>.chaos-blade-backup-<uid>`，可依赖）；恢复侧必须带外核实靶内容是否已还原，**未还原则用该快照手动 `cp <file>.chaos-blade-backup-<uid> <file>`（+ chown/chmod 复位）+ `rm` 快照**（幂等兜底，版本无关）。`--delete-file=true` 仅为当前 vendored 版本的自动还原便利，换版本须重测。详见「恢复验证」：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的 timer，再执行对应变体的逆操作：`chmod` 还原权限 / `cp` 备份回写内容 / `mv` 移回位置）

## 恢复验证
1. `ls -la <filepath>` 确认权限/内容/位置恢复
2. 确认应用读取配置恢复正常
3. **`append` 内容变体额外核实（版本无关，必做）**：destroy/`--timeout` fire 后，带外新鲜 `md5sum <filepath>`/`ls -la` 确认内容逐字节复原（size 回基线）、属主/mode 未漂移；**若仍处篡改态（destroy 未自动还原，属版本相关行为），立即用快照手动 `cp <file>.chaos-blade-backup-<uid> <file>` + chown/chmod 复位 + `rm` 快照**，再复验。**勿以「实验 Status=Destroyed / CLI overall=verified」当作已还原的证据**——CLI 常在 `--timeout` fire 点前就 verified 退出，恢复侧一律带外独立核实。

## 基准事实
- **根因**：关键文件被篡改（权限、内容或位置），导致应用无法正常读取
- **必现现象**：文件状态变化；应用报 Permission denied / 解析错误 / 文件不存在

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（三种变体各有对应原生命令：`chmod` 改权限 / `truncate` 清内容 / `mv` 移位置）；主机未装 ChaosBlade 时选用本手段。

前提条件：三种变体分别需 `chmod` / `truncate` / `mv` 命令；自恢复载体需 `systemd-run`（Tier-1，主机有 systemd）或 `setsid`+`sh`+`sleep`（Tier-2，主机无 systemd），二者皆不可用则 fail-closed（见标准件文档 §七）。

注入命令（**先武装定时恢复，再注入**——武装载体按主机是否有 systemd 分层选型〔探测门 → Tier 选择 → 武装〕，构造细节/纪律/取证约束见 `references/carrier/recovery-carrier.md`〔host 域分层自恢复载体单一权威，本 case 只引用不内嵌〕；`<recovery-seconds>` 一律取 prompt 下发的 `recovery_timer_seconds`〔= duration + grace，见 SKILL.md 双数窗口契约〕，Tier-1 的 `--on-active` 与 Tier-2 的 `sleep` 同源取此值，禁自算缓冲）：
```bash
# 0) 只读探测 systemd 是否在场（任一信号为真即判在场，degraded 仍算在场，不因 degraded 降级）：
systemctl is-system-running          # 只读；bus 可达即 systemd 在场
# 或只读判据： [ -d /run/systemd/system ]
#   → 在场选 Tier-1（systemd-run transient timer）；不在场（两信号皆否，如最小容器 / 非 systemd init 裸机）
#     选 Tier-2（setsid detached-timer）；两 Tier 前置皆不可用（systemd 不在场 且 setsid/sh/sleep 任一缺失）
#     → fail-closed：显式报告「本环境无可用自治恢复载体」及缺失项，禁止注入无安全网的自定时故障（见标准件文档 §七）。
#   下面每变体给出 Tier-1 / Tier-2 两种武装形态，按探测结果二选一；武装成功后（Tier-1 输出含 "Running timer as unit"、
#   Tier-2 即刻返回且 ps 见 PPID=1 detached 会话）再执行注入。各命令独立执行（执行通道不支持在工具调用层 && 串联）。

# ── 方式一：权限篡改（应用无法读取）──
# 先用只读 stat 取原权限数字固化进 timer（不可事后猜测），再武装、再 chmod 000。
#   注：chmod 000 对 root 读者无效——注入通道与验证均以 root 运行时「Permission denied」判据不可达，
#       须以非 root 应用视角核实或改选方式二/三（见「注入验证」）。
stat -c %a <filepath>                                    # 只读，经 host_read 通道
# Tier-1（systemd 在场）：
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-filemode \
  chmod <stat取到的原权限数字> <filepath>
# Tier-2（systemd 不在场）：
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && chmod <stat取到的原权限数字> <filepath>'
chmod 000 <filepath>

# ── 方式二：内容清空（truncate -s 0；备份→武装→清空逐条独立执行）──
# 备份失败时武装与清空都不执行，避免「无备份却已清空」的不可恢复破坏。
cp <filepath> <filepath>.bak
# Tier-1（systemd 在场；引号内的 sh -c 属 timer 载荷，到期由目标机 shell 执行，保留原样）：
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-filecontent \
  sh -c 'cp <filepath>.bak <filepath> && rm -f <filepath>.bak'
# Tier-2（systemd 不在场；逆命令已在外层 sh -c 内，直接写、禁再嵌套 sh -c——payload 禁嵌套 interpreter）：
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && cp <filepath>.bak <filepath> && rm -f <filepath>.bak'
truncate -s 0 <filepath>

# ── 方式三：文件移走（同目录，避免跨文件系统）──
# 两条命令分两次独立执行；武装成功后再执行移走。
# Tier-1（systemd 在场）：
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-filemove \
  mv <filepath>.orig <filepath>
# Tier-2（systemd 不在场）：
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && mv <filepath>.orig <filepath>'
mv <filepath> <filepath>.orig
```

恢复命令（timer 到期前可提前手动恢复，同时停掉已武装的定时器——按武装时选用的 Tier 停对应定时器）：
```bash
# 方式一恢复：
#   Tier-1：systemctl stop blade-restore-filemode.timer 2>/dev/null
#   Tier-2：pkill -f 'sleep <recovery-seconds>' 用括号法防自匹配（如 pkill -f 'sleep 42[0]'，见标准件文档 §五）
chmod <original-mode> <filepath>

# 方式二恢复（两条命令独立执行；rm -f 收尾清理演练自建的备份文件）：
#   Tier-1：systemctl stop blade-restore-filecontent.timer 2>/dev/null
#   Tier-2：pkill -f 'sleep <recovery-seconds>'（括号法）
cp <filepath>.bak <filepath>
rm -f <filepath>.bak

# 方式三恢复：
#   Tier-1：systemctl stop blade-restore-filemove.timer 2>/dev/null
#   Tier-2：pkill -f 'sleep <recovery-seconds>'（括号法）
mv <filepath>.orig <filepath>
```

注意事项：
- 操作前必须备份原文件（方式二 `cp .bak` / 方式三 `mv .orig` 本身即备份），否则无法恢复
- 自恢复基于注入前武装的**分层自恢复载体**（Tier-1 主机有 systemd 时 systemd-run transient timer / Tier-2 主机无 systemd 时 setsid detached-timer；选型探测门、武装/重武装纪律、payload 文法〔Tier-2 禁嵌套 interpreter，逆命令直接写在外层 sh -c 内〕、fail-closed 边界、Tier-2 取证约束见 `references/carrier/recovery-carrier.md`），到期自动执行对应变体逆操作；提前恢复仍用上方手动命令
- Tier-2 payload 守 discard-sink 不变量、**不写任何取证文件**，故无载体侧 fire 日志属预期取舍（见标准件文档 §八）；fire 是否发生由框架侧只读观测靶标状态差承担，不得据此误判武装失败
- 武装与注入分次独立执行，须确认武装成功后再执行篡改操作（等价于 `&&` 串联的失败短路保证）；方式一的原权限必须在武装前用 `stat -c %a` 取真实值固化进 timer，不可事后猜测
- Tier-1 同名 transient timer 重复武装会报 `Unit blade-restore-*.service was already loaded`（上次武装失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop <unit>.service; systemctl reset-failed <unit>.service`。Tier-2 重武装先 `pkill -f 'sleep <recovery-seconds>'`（括号法）终止旧会话再重新 setsid（见标准件文档 §五）
- chmod 000 对 root 用户无效（root 可绕过权限检查）
