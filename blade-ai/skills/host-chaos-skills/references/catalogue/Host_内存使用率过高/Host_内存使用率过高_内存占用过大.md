**用例名称** 内存占用过大 导致 Host_内存使用率过高

## 故障定位
持续型故障——内存占用进程（blade `mem load` / stress-ng `--vm`）存活即故障存活，匿名内存被 pin 住贯穿整个故障窗口；窗口结束（`--timeout` 到期或实验销毁）即自动释放。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是分配并驻留匿名内存、抬高 `free -m` used），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理）；否则手段2。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 主机物理内存使用率持续超过目标百分比
2. （参数依赖，非必然）Swap 使用率可能升高——`ram` 模式将匿名内存 pin 在 RAM，仅在压力超出可回收余量时才触及 swap
3. 可能触发 OOM Killer 杀死进程

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测主机内存指标（如 `free -m`、Prometheus node_exporter）

## 演练步骤
1. 记录当前内存基线：`free -m`
2. 注入内存压力 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create mem load --mode ram --mem-percent <percent> --timeout <duration>
```

参数说明：
- `--mode ram`：直接占用物理内存
- `--mem-percent`：目标内存使用率百分比（如 80、90）——占用 N% × MemTotal 的匿名内存，默认不含 buffer/cache（`--include-buffer-cache` 才计入）；分配语义详见「注入验证」定案
- `--avoid-being-killed`：可选，防止被 OOM Killer 杀死
- `--rate`：可选，内存占用速率（MB/s）
- `--timeout`：超时自动恢复（秒）

3. 观察内存使用率及应用运行状态

## 注入验证
1. `free -m` 确认可用内存显著减少（`used` 逼近目标百分比 × MemTotal、MemAvailable 跌破余量）
2. `vmstat 1` / `free -m` 观察 swap：是否升高**取决于参数与内存余量，非必然**（见下方定案）
3. `dmesg | tail` 观察是否有 OOM 相关日志

> **定案（勿重推）——`--mem-percent` 分配语义与 swap 行为**：
> - `--mode ram --mem-percent N` 目标是占用 **N% × MemTotal 的匿名内存**，默认不含 buffer/cache；因此 `free -m` 的 `used` 会逼近 N% × MemTotal，判据「used 达目标水位」可满足。
> - 内核优先**回收 page cache**（buff/cache 下降）满足分配，而非必然换出。swap 只在压力超出可回收余量、且系统启用 swap 并达阈值时才升高；`--mem-percent` 留出余量时 swap 可全程不动——**swap 未升高不代表注入失败**。
> - 判死/判成应以 `free -m` used 达目标水位 + MemAvailable 跌破余量为权威判据，**不得依赖「swap 升高」「OOM 触发」这类参数依赖现象**。

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（stress-ng `--timeout` 自停 / 手动 kill）

## 恢复验证
1. `free -m` 确认内存使用率恢复正常
2. 确认应用进程未被 OOM Kill（`dmesg | grep -i oom`）

## 基准事实
- **根因**：异常进程大量占用物理内存，导致系统可用内存不足，可能触发 OOM Killer
- **必现现象**：物理内存使用率持续超过目标百分比（`free -m` used 达 N% × MemTotal、MemAvailable 跌破余量）
- **参数依赖现象（非必现）**：Swap 使用升高（取决于内存余量与是否启用 swap）；进程被 OOM Kill（取决于是否超出总内存及 `--avoid-being-killed`）

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（stress-ng `--vm` 分配并驻留匿名内存抬高 used）；主机未装 ChaosBlade 时选用本手段。本手段属 **Branch B（自停型手段2）**——stress-ng 自带 `--timeout` 前台跑满窗口后自行退出即恢复、无需外挂载体；发射经 host_inject 前台直发，本地等待超时返回 outcome UNKNOWN 即预期且完整的发射回执（命令在主机续跑），随后**一条只读探针对账**即为权威回执，**严禁** setsid/systemd-run/nohup/`&` 包裹故障命令追同步返回；发射语义详见 `references/carrier/recovery-carrier.md` §〇 Branch B。

前提条件：主机需安装 `stress-ng`

注入命令：
```bash
# 1) 先读总内存与可用内存（输出中 MemTotal/MemAvailable 单位是 kB）
cat /proc/meminfo

# 2) 按增量计算分配量：分配量 = MemTotal × 目标百分比 − (MemTotal − MemAvailable)
#    例：MemTotal=16000000k、MemAvailable=6000000k、目标 80%：
#    16000000×0.8 − (16000000−6000000) = 2800000k
stress-ng --vm 1 --vm-bytes <算出的分配量>k --timeout <recovery-seconds>s
```

`<recovery-seconds>`：安全网窗总时长（秒），取 prompt 下发的 `recovery_timer_seconds`
（= duration + grace，见 SKILL.md 双数窗口契约）——stress-ng 自停 `--timeout` 以它
武装，让框架在观察窗终点主动派发的恢复先于自治到期落地；上方 ChaosBlade 形态的
`--timeout` 由引擎在派发前按同一单源钉定，文档占位符保持 `<duration>` 不动

> **严禁直接按「MemTotal × 目标百分比」的绝对值分配**——主机已有基础用量，
> 绝对值分配会超出目标水位甚至超过总内存，触发 swap 抖动或 OOM Killer。

恢复命令：
```bash
# stress-ng 在 --timeout 到期后自动退出
# 如需手动终止：vm stressor 的 worker 随主进程一同退出，杀主进程即可。
# 必须用 -x 精确匹配进程名，不要用 -f（见下方注意事项）。
pkill -x stress-ng
```

注意事项：
- stress-ng 方式无法精确控制百分比，需按**增量**手动计算内存量（分配量 = MemTotal × 目标百分比 − 已用量）
- 无 `--avoid-being-killed` 等效保护，可能被 OOM Killer 杀死；若分配后进程消失且 `free -m` 无变化，先查 `dmesg | grep -i oom` 确认是否超量被杀，按增量重算后重试，不要直接换方法
- **不要用 `pgrep -f stress-ng` 取 PID**：`-f` 匹配完整命令行，会把携带该字符串的执行 shell（如 `sh -c 'pgrep -f stress-ng'` 自身）一并匹配进来，拿输出去 kill 会杀掉执行 shell 导致命令链中断；`-x` 按进程名精确匹配，只命中 stress-ng 主进程
