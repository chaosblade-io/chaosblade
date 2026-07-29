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
# 如需提前终止：先取 PID 再杀。
pgrep -f stress-ng
kill <pid>
```

> 不要用 `python3 -c '...'` 之类的解释器写法：执行层按 argv 下发且不提供任何解释器，该形态无法执行。

注意事项：
- 单进程受 `ulimit -n` 限制，全局受 `file-max` 限制
- 建议在演练前临时提高 ulimit 以达到预期效果：`ulimit -n 1000000`
- 原生方式无自动超时恢复
