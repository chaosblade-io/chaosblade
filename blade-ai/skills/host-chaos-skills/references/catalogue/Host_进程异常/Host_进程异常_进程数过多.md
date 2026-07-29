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
# 如需提前终止：先取 PID 再逐个杀。
pgrep -f stress-ng
kill <pid>
```

注意事项：
- 创建过多进程可能导致当前 SSH 会话无法创建新进程，建议提前设置 ulimit -u
- 原生方式无法精确控制，过量可能导致系统完全不可用
- 演练前建议确认可通过 out-of-band 方式（如 IPMI/iDRAC）恢复主机
