**用例名称** 文件句柄耗尽 导致 Host_文件系统异常

**故障现象**：
1. 应用打开文件失败（Too many open files）
2. 新连接无法建立（socket 也消耗文件描述符）
3. 日志写入失败，数据库连接池耗尽

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认当前文件描述符使用情况：`cat /proc/sys/fs/file-nr`
3. 确认系统限制：`ulimit -n` 和 `cat /proc/sys/fs/file-max`

**演练步骤**：
1. 记录当前 fd 基线：`cat /proc/sys/fs/file-nr`（已分配 / 未使用 / 最大值）
2. 使用 ChaosBlade 注入文件句柄耗尽

```bash
blade create file load --filepath <target-file> --count <count> --timeout <duration>
```

参数说明：
- `--filepath`：目标文件路径（必填，建议使用日志文件或临时文件）
- `--count`：打开次数（正整数，0 或不设为无限直到达到系统限制）
- `--force`：可选，强制达到文件句柄上限（注意：使用此标志无法自动恢复）
- `--timeout`：超时自动恢复（秒）

3. 观察文件描述符消耗及应用状态

**注入验证**：
1. `cat /proc/sys/fs/file-nr` 确认已分配 fd 数显著增加
2. `ls /proc/<app-pid>/fd | wc -l` 查看目标应用进程的 fd 使用量
3. 观察应用日志是否出现 "Too many open files" 错误

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `cat /proc/sys/fs/file-nr` 确认 fd 使用量回落
2. 确认应用文件/连接操作恢复正常

**基准事实**：
- **根因**：文件描述符被大量占用，导致系统或进程无法打开新文件/建立新连接
- **必现现象**：fd 使用量逼近或达到上限；应用报 Too many open files；新连接建立失败

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

前提条件：主机需具备 `stress-ng`

注入命令：
```bash
# 用 stress-ng 的 open stressor 反复打开文件描述符，--timeout 到期自行退出。
# open stressor 是否可用以 `stress-ng --help` 的实际输出为准。
stress-ng --open <count> --timeout <duration>s
```

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
- 自恢复基于 stress-ng 自带的 `--timeout <duration>s`，到期进程自行退出、fd 释放；提前恢复用上方 kill 命令
- **open worker 孤儿化**：kill 主进程后 worker 被收养（PPID 归 1 号进程族）继续打开 fd，必须用 `pkill -9 -x stress-ng-open` 按名清理；与 vm/fork stressor（worker 随主进程退出）行为不同
- **timeout 自恢复有迟滞**：到期后 worker 逐个退出，完全释放滞后约 1–2 分钟（如 9 个 worker 约 75s 清零）——勿在到期则立即判「未恢复」
- **不要用 `pgrep -f stress-ng` 取 PID**：`-f` 匹配完整命令行，会把携带该字符串的执行 shell 自身一并匹配，拿输出去 kill 会杀掉执行 shell；`-x` 按进程名精确匹配
