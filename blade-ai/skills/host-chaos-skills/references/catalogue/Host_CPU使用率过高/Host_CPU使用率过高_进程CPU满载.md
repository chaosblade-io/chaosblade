**用例名称** 进程CPU满载 导致 Host_CPU使用率过高

## 故障定位
持续型故障——CPU 压测进程存活即满载存活，贯穿整个故障窗口；窗口结束（`--timeout` 到期进程退出 / 实验销毁）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是起 busy 进程占满 CPU、抬高使用率与 Load），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理）；否则手段2。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 主机 CPU 使用率持续超过 90%
2. 系统 Load Average 显著升高
3. 主机上运行的应用响应变慢，出现超时

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测主机 CPU 指标（如 Prometheus node_exporter、top、vmstat）

## 演练步骤
1. 记录当前 CPU 基线：`top -bn1 | head -5` 或 `vmstat 1 3`
2. 注入 CPU 满载 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create cpu fullload --cpu-percent <percent> --timeout <duration>
```

参数说明：
- `--cpu-percent`：CPU 使用率百分比（如 80、90）
- `--cpu-count`：可选，指定占用的核心数（如仅压 2 核）
- `--cpu-list`：可选，指定核心索引（如 0,1）
- `--timeout`：超时自动恢复（秒）

3. 观察 CPU 使用率及应用性能变化

## 注入验证
1. `top` 或 `mpstat -P ALL 1` 确认 CPU 使用率高于目标百分比——单次采样读数高于目标即满载已发生；「持续保持」由机制存活保证（fullload 压测进程在运行即持续满载由构造成立），无需反复采样验证持续
2. `uptime` 确认 Load Average 显著升高
3. （可选，仅当演练方提供了应用访问入口时）确认请求延迟增大；无入口时上述 CPU 与 Load 证据成立即可判定

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（stress-ng `--timeout` 自停 / 手动 kill PID）

## 恢复验证
1. `top` 确认 CPU 使用率恢复正常水平
2. （可选，有访问入口时）确认应用请求延迟恢复正常

## 基准事实
- **根因**：异常进程大量占用 CPU，导致主机 CPU 使用率过高，影响同主机上所有应用性能
- **必现现象**：CPU 使用率持续超过目标百分比；Load Average 显著升高；应用响应变慢

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（`stress-ng --cpu` 起 busy 进程占满 CPU）；主机未装 ChaosBlade 时选用本手段。本手段属 **Branch B（自停型手段2）**——stress-ng 自带 `--timeout` 前台跑满窗口后自行退出即恢复、无需外挂载体；发射经 host_inject 前台直发，本地等待超时返回 outcome UNKNOWN 即预期且完整的发射回执（命令在主机续跑），随后**一条只读探针对账**即为权威回执，**严禁** setsid/systemd-run/nohup/`&` 包裹故障命令追同步返回；发射语义详见 `references/carrier/recovery-carrier.md` §〇 Branch B。

前提条件：主机已安装 `stress-ng`。

先探测是否安装（优先用 shell 内建 `command -v`：不依赖额外装包，也不依赖安装路径）：
```bash
command -v stress-ng
```

判读：返回路径 = 已安装；返回 `(no output)` = **未安装**（探测本身是成功的，不要重复执行）。

若未安装或需确认具体路径，再按绝对路径确认。**不要只猜 `/usr/bin`**——一次列出所有候选路径，`ls` 只会列出真实存在的那个：
```bash
ls /usr/bin/stress-ng /usr/sbin/stress-ng /usr/local/bin/stress-ng
```

> 路径提示：`stress-ng`/`stress`/`dd` 等通常在 `/usr/bin`，而 `iptables`/`ip6tables`/`nft`/`tc` 等网络类注入二进制通常在 `/usr/sbin` 或 `/sbin`。只探测 `/usr/bin` 会把已安装的二进制误判为「未安装」。

注入命令（探测到 stress-ng 时）：
```bash
# 使用 stress-ng 注入 CPU 压力，--timeout 到期后自动退出
stress-ng --cpu 0 --cpu-load <percent> --timeout <recovery-seconds>s
```

`<recovery-seconds>`：安全网窗总时长（秒），取 prompt 下发的 `recovery_timer_seconds`
（= duration + grace，见 SKILL.md 双数窗口契约）——stress-ng 自停 `--timeout` 以它
武装，让框架在观察窗终点主动派发的恢复先于自治到期落地；上方 ChaosBlade 形态的
`--timeout` 由引擎在派发前按同一单源钉定，文档占位符保持 `<duration>` 不动

恢复命令：
```bash
# stress-ng 在 --timeout 到期后自动退出；如需提前恢复：
kill <stress-ng-pid>
```

注意事项：
- **若 `stress-ng` 未安装**：手段2（原生 CPU 满载）不可行（`host_inject` 的安全护栏放行**故障二进制 + 单条只读诊断**〔`top`/`mpstat`/`df`/`ps`/`ss`/`cat` 等，无管道/重定向〕，但 `sh`/`bash` 等 shell 解释器仍被拒，无法用 shell 死循环兜底）。此时应明确报告“手段2 原生 CPU 满载不可行”并交回 replan 重新规划（例如换用其它已安装工具或改用手段1 ChaosBlade）。
- 与 ChaosBlade 相比，原生方式缺少集中式实验记录，需自行记录 PID 以便恢复。
