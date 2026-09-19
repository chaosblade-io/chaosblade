**用例名称** 调用延迟 导致 Host_系统调用异常

**故障现象**：
1. 特定系统调用执行时间显著增加
2. 应用 IO 操作、内存分配等变慢
3. 应用响应延迟增大但不报错

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标进程 PID：`pidof <process>` 或 `ps aux | grep <process>`
3. 确认目标系统调用名（如 read、write、open、mmap 等）

**演练步骤**：
1. 确认目标进程 PID 和关键系统调用：`strace -c -p <pid> -e trace=<syscall> &`（采样 5 秒后 Ctrl+C）
2. 使用 ChaosBlade 注入系统调用延迟

```bash
blade create strace delay --pid <pid> --syscall-name <syscall> --time <delay> --delay-loc enter --timeout <duration>
```

参数说明：
- `--pid`：目标进程 PID（必填）
- `--syscall-name`：目标系统调用名（必填，如 read、write、open、mmap、futex）
- `--time`：延迟时间（必填，支持单位 s/ms/us/ns，如 100ms、1s）
- `--delay-loc`：延迟注入位置（必填，enter=调用前注入 / exit=调用后注入）
- `--first`：可选，仅对前 N 次调用注入
- `--step`：可选，间隔 N 次注入一次
- `--timeout`：超时自动恢复（秒）

3. 观察应用性能变化

**注入验证**：
1. `strace -T -p <pid> -e trace=<syscall>` 确认该系统调用耗时增加
2. （可选，仅当演练方提供了应用访问入口时）确认响应延迟增大；无入口时上述 strace 耗时证据成立即可判定
3. 确认延迟是否符合注入的时间值

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `strace -T -p <pid> -e trace=<syscall>` 确认系统调用耗时恢复正常
2. （可选，有访问入口时）确认应用响应延迟恢复正常

**基准事实**：
- **根因**：特定系统调用出现异常延迟（如磁盘慢、网络抖动导致的底层延迟）
- **必现现象**：目标系统调用耗时显著增加；应用响应延迟增大；整体吞吐下降

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，难以直接实现系统调用级别的精确延迟注入。以下为近似方案。

前提条件：需安装 `strace` 或具备 BPF/eBPF 工具

近似注入（借 strace 的跟踪开销制造延迟，非精确注入；**先武装定时终止，再注入**——timer 到期自动 kill 掉 strace、目标进程恢复原速，补齐自恢复能力）：
```bash
# 1) 先取目标 PID
pgrep -f <process-name>

# 2) 先武装定时终止（定时器由宿主机 systemd(PID 1) 管理，到期自动 kill 掉
#    strace）。各命令分次独立执行（执行通道不支持 && 串联）；武装成功
#    （输出含 Running timer as unit）后再执行 attach
systemd-run --on-active=<recovery-seconds>s --unit=blade-kill-strace \
  sh -c 'kill $(pgrep -x strace)'

# 3) attach 到该 PID。只允许 attach 形态：不带 -p 时 strace 会
#    直接【启动】其参数，那是任意命令执行而非跟踪，会被拒绝。
strace -p <pid> -e trace=<syscall> -T
```

恢复命令（提前恢复；先停武装的定时器，再手动终止 strace）：
```bash
systemctl stop blade-kill-strace.timer 2>/dev/null
# 取 strace 自身的 PID 后终止，目标进程随即恢复原速
# （-x 按进程名精确匹配；-f 会把携带 strace 字符串的执行 shell 一并匹配，
#   拿输出去 kill 会误杀执行 shell）
pgrep -x strace
kill <strace-pid>
```

注意事项：
- 原生 strace 附加本身会对进程产生显著性能开销（约 10-100x 减速），但无法精确控制延迟量
- 精确的 syscall 延迟注入是 ChaosBlade 的独特能力，原生命令难以完全等效替代
- 如需更精确的替代方案，可考虑使用 BCC/bpftrace 工具
- 自恢复基于注入前武装的 systemd-run transient timer（宿主机 PID 1 管理，到期自动终止 strace）；提前恢复仍用上方手动命令
- 同名 transient timer 重复武装会报 `Unit blade-kill-strace.service was already loaded`（unit 以 failed 状态残留所致——武装命令执行失败，或手动恢复未先停 timer、到期 `kill $(pgrep -x strace)` 因无匹配 exit 1）；重武装前先清理残留：`systemctl stop blade-kill-strace.service; systemctl reset-failed blade-kill-strace.service`
