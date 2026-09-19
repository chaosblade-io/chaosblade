**用例名称** 进程数过多 导致 Host_进程异常

**故障现象**：
1. 系统进程数接近或超过内核限制（pid_max）
2. 新进程创建失败（fork: Cannot allocate memory）
3. 系统响应变慢，服务无法启动新线程/进程

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认当前进程数基线：`ps aux | wc -l`
3. 确认系统进程上限：`cat /proc/sys/kernel/pid_max`

**演练步骤**：
1. 记录当前进程数：`ps aux | wc -l`
2. 使用 ChaosBlade 注入进程数飙升

```bash
blade create process load --count <count> --timeout <duration>
```

参数说明：
- `--count`：创建的进程数（正整数，0 或不设为无限）
- `--user`：可选，以指定用户身份创建进程
- `--timeout`：超时自动恢复（秒）

3. 观察系统进程数变化及服务可用性

**注入验证**：
1. `ps aux | wc -l` 确认进程数显著增加
2. 尝试执行新命令（如 `ls`）观察是否变慢或失败
3. `dmesg | tail` 观察是否有 fork 失败日志

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `ps aux | wc -l` 确认进程数回落到正常水平
2. 确认新命令可正常执行

**基准事实**：
- **根因**：大量进程被创建（类似 fork bomb），耗尽系统进程资源
- **必现现象**：进程数显著升高；新进程创建变慢或失败；系统响应迟钝

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

前提条件：建议先设置 ulimit 保护，避免影响恢复能力

注入命令：
```bash
# 用 stress-ng 的 fork stressor 批量占用进程槽位，自带超时不需人工清理。
# --fork 是否可用以 `stress-ng --help` 的实际输出为准。
stress-ng --fork <count> --timeout <duration>s
```

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
