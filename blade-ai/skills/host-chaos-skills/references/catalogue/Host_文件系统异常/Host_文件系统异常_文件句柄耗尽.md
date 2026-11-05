**用例名称** 文件句柄耗尽 导致 Host_文件系统异常

## 故障定位
持续型故障——文件描述符（fd）被大量占用后，目标进程在整个故障窗内持续无法打开新文件 / 建立新连接（socket 亦消耗 fd），应用报 `Too many open files`、日志写入失败、连接池耗尽；窗口结束（**手段1** blade `--timeout` 到期销毁实验释放 fd / **手段2** stress-ng `--timeout` 到期自行退出释放 fd）即自动恢复——**两手段各有内建 `--timeout` 自恢复，无需外挂武装载体**。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（`file load --force`，实验 UID 统一生命周期管理）；否则手段2（`stress-ng --open`，需主机具备 stress-ng open stressor）。**判据可达性 / 爆炸半径 caveat（以当次探测为准）**：真正演示「句柄耗尽」的注入会把 fd 开到**进程级上限 `nr_open`**（`cat /proc/sys/fs/nr_open`；手段1 `--force`、手段2 stress-ng worker 各抬到自身上限），使**全局已分配 `file-nr` 首列飙升到近 `nr_open`**（百万级）——这是主判据。注入前必须先 `cat /proc/sys/fs/file-max` + `cat /proc/sys/fs/nr_open` 探测二者关系：**`file-max` 远大于 `nr_open` 时故障局部在 holder 进程自身 fd 表、一般不波及整机 / SSH 控制通道**；若某主机 `file-max` 低于 `nr_open`，耗尽会**触顶全局 fd 表、连 sshd 都无法分配 fd → 切断控制通道**，此类主机禁用手段1 `--force`（见「演练步骤」step1 前置核实）。**独立受害应用的 `Too many open files` 判据仅在 fd 表全局触顶时可达**，`file-max` 极大的主机上不可达（fd 消耗局部在 holder 进程），此时以 `file-nr` 飙升 + holder 进程 fd 数为主判据。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 应用打开文件失败（Too many open files）
2. 新连接无法建立（socket 也消耗文件描述符）
3. 日志写入失败，数据库连接池耗尽

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认当前文件描述符使用情况：`cat /proc/sys/fs/file-nr`
3. 确认系统限制：`ulimit -n` 和 `cat /proc/sys/fs/file-max`

## 演练步骤
1. 记录当前 fd 基线与上限：`cat /proc/sys/fs/file-nr`（已分配 / 未使用 / 最大值）+ `cat /proc/sys/fs/nr_open`（进程 fd 上限，`--force` 会顶到此值）+ `cat /proc/sys/fs/file-max`（全局 fd 上限）。**爆炸半径前置核实：仅当 `file-max` 远大于 `nr_open` 才可走手段1 `--force`；否则 `--force` 会全局耗尽 fd 表危及 SSH 控制通道，禁用之（见「故障定位」caveat）**
2. 注入文件句柄耗尽 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create file load --filepath <target-file> --force --timeout <duration>
```

> ⚠️ **演示 fd 耗尽必须用 `--force`**：`--count <N>`（不带 `--force`）对靶文件只是反复 open/close 的 churn，**不持有并发 fd、`file-nr` 基本不变**，演示不出「句柄耗尽」；只有 `--force` 会把 fd 开到进程上限 `nr_open` 真正耗尽句柄。
> ⚠️ **`--force` 爆炸半径前置核实（必做）**：`--force` 开到 `nr_open` 个 fd，注入前必须先核实 **`file-max` 远大于 `nr_open`**（见「演练步骤」step1）——若主机 `file-max` 低于 `nr_open`，`--force` 会全局耗尽 fd 表、连 sshd 都无法分配 fd → 切断控制通道，此类主机**禁用 `--force`**（改走文末手段2 或放弃本 case）。

参数说明：
- `--filepath`：目标文件路径（必填，建议使用日志文件或临时文件）
- `--force`：把 fd 开到进程上限 `nr_open` 真正耗尽句柄，**是演示本故障的必需标志**。blade `--help` 称「`--force` cannot automatically recover（无法自动恢复）」，但当前 vendored ChaosBlade 版本 `--force` + `--timeout` 到点会自动销毁实验、fd 全部释放、`file-nr` 回基线——**`--help` 与实现矛盾，勿照抄 help**；该自恢复行为版本相对，换 ChaosBlade 版本须重新核实，且恢复侧一律带外核实 `file-nr` 回落、不以实验 Status=Destroyed 为凭。
- `--count`：打开次数（正整数）。**单独使用（无 `--force`）只是 open/close churn，不持有 fd、不产生耗尽**，不用于本故障主演示。
- `--timeout`：超时自动恢复（秒）

3. 观察文件描述符消耗及应用状态

## 注入验证
> 判据可达性：**「已分配 fd 显著增加」仅在真正把 fd 开到 `nr_open` 时成立**（手段1 `--force` / 手段2 stress-ng）——`file-nr` 首列从基线飙升到近 `nr_open`（百万级）；手段1 `--count` 单独注入时 `file-nr` 基本不变（churn），据此判「未演示出故障」。**独立受害应用的 "Too many open files" 判据仅在 fd 表全局触顶时可达**（`file-max` 极大的主机上不可达，见「故障定位」caveat），此时以 `file-nr` 飙升 + holder 进程 fd 数为主判据。
1. `cat /proc/sys/fs/file-nr` 首列确认已分配 fd 数显著增加（`--force` 下→近 `nr_open`）
2. 定位 holder 进程并核实其 fd 逼近上限：**手段1 真实 holder 是 `chaos_os` 进程（非 blade spec 声称的 `chaos_fileload`）**——用 `ps -C chaos_os -o pid,comm`（按名免管道取 pid）。**核实 fd 数不要用 `ls /proc/<pid>/fd | wc -l`**：`--force` 下 holder 持有近 `nr_open`（百万级）个 fd，只读探测通道拒绝未加引号的 shell 管道、且百万条目会超输出安全阀，该写法在本故障不可执行。改用三件等价的免管道证据：① `cat /proc/sys/fs/file-nr` 首列减注入前基线 ≈ holder 持有的 fd 数（主机进程表空闲时全局增量可唯一归因于该 holder）；② `cat /proc/<pid>/limits` 读 `Max open files` 行，确认 soft/hard 已抬到 `nr_open`（证明 fd 被真实持有到上限，而非停在继承的 1024）；③ 边界归因 `readlink /proc/<pid>/fd/<接近 soft limit 的高位索引>` → 应解析到靶文件，证明这些 fd 确指向靶文件。**切勿用 `pgrep -f`/`pkill -f`（`-f` 匹配完整命令行会自匹配携带该串的执行 shell；用 `-C`/`-x` 按名精确匹配）**
3. fd 持有进程自身触顶时会报 "Too many open files"（holder 视角可观测；独立应用视角见上方判据可达性）

## 注入恢复
**手段1（ChaosBlade）**——`blade destroy <uid>` / `--timeout` 到期销毁实验、释放其持有的 fd：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（stress-ng `--timeout` 到期自行退出释放 fd；提前恢复须先 `pkill -x stress-ng` 再 `pkill -9 -x stress-ng-open` 清孤儿 worker）

## 恢复验证
1. `cat /proc/sys/fs/file-nr` 确认 fd 使用量回落
2. 确认应用文件/连接操作恢复正常

## 基准事实
- **根因**：文件描述符被大量占用，导致系统或进程无法打开新文件/建立新连接
- **必现现象**：fd 使用量逼近或达到上限；应用报 Too many open files；新连接建立失败

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（stress-ng open stressor 反复打开 fd 逼近上限）；主机未装 ChaosBlade 时选用本手段。本手段属 **Branch B（自停型手段2）**——stress-ng 自带 `--timeout` 前台跑满窗口后自行退出即恢复、无需外挂载体；发射经 host_inject 前台直发，本地等待超时返回 outcome UNKNOWN 即预期且完整的发射回执（命令在主机续跑），随后**一条只读探针对账**即为权威回执，**严禁** setsid/systemd-run/nohup/`&` 包裹故障命令追同步返回；发射语义详见 `references/carrier/recovery-carrier.md` §〇 Branch B。

前提条件：主机需具备 `stress-ng`（open stressor，是否可用以 `stress-ng --help` 实际输出为准）；自恢复靠 stress-ng 自带 `--timeout` 到期进程自行退出、fd 释放，**无需外挂武装载体**（与手段1 blade `--timeout` 同属内建自恢复）

注入命令：
```bash
# 用 stress-ng 的 open stressor 反复打开文件描述符，--timeout 到期自行退出。
# open stressor 是否可用以 `stress-ng --help` 的实际输出为准。
stress-ng --open <count> --timeout <recovery-seconds>s
```

`<recovery-seconds>`：安全网窗总时长（秒），取 prompt 下发的 `recovery_timer_seconds`
（= duration + grace，见 SKILL.md 双数窗口契约）——stress-ng 自停 `--timeout` 以它
武装，让框架在观察窗终点主动派发的恢复先于自治到期落地；上方 ChaosBlade 形态的
`--timeout` 由引擎在派发前按同一单源钉定，文档占位符保持 `<duration>` 不动

恢复命令：
```bash
# --timeout 到期后 stress-ng 自行退出，正常路径无需干预。
# 如需提前终止，必须两步：open stressor 的 worker 不随主进程退出（孤儿化，
# fd 持续增长），只杀主进程会残留 worker 且 fd 继续上涨。先杀主进程再按名清 worker（-x 精确匹配进程名）：
pkill -x stress-ng
pkill -9 -x stress-ng-open
# 清理后用 cat /proc/sys/fs/file-nr 复核已分配 fd 数回落，未回落重复执行一次
```

> 不要用 `python3 -c '...'` 之类的解释器写法：执行层按 argv 下发且不提供任何解释器，该形态无法执行。

注意事项：
- 单进程受 `ulimit -n` 限制，全局受 `file-max` 限制
- 建议在演练前临时提高 ulimit 以达到预期效果：`ulimit -n 1000000`
- 自恢复基于 stress-ng 自带的 `--timeout <recovery-seconds>s`，到期进程自行退出、fd 释放；提前恢复用上方 kill 命令
- **open worker 孤儿化**：kill 主进程后 worker 被收养（PPID 归 1 号进程族）继续打开 fd，必须用 `pkill -9 -x stress-ng-open` 按名清理；与 vm/fork stressor（worker 随主进程退出）行为不同
- **timeout 自恢复有迟滞**：到期后 worker 逐个退出，完全释放滞后约 1–2 分钟（如 9 个 worker 约 75s 清零）——勿在到期则立即判「未恢复」
- **不要用 `pgrep -f stress-ng` 取 PID**：`-f` 匹配完整命令行，会把携带该字符串的执行 shell 自身一并匹配，拿输出去 kill 会杀掉执行 shell；`-x` 按进程名精确匹配
