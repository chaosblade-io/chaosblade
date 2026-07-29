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
2. 观察应用响应延迟是否增大
3. 确认延迟是否符合注入的时间值

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `strace -T -p <pid> -e trace=<syscall>` 确认系统调用耗时恢复正常
2. 确认应用响应延迟恢复正常

**基准事实**：
- **根因**：特定系统调用出现异常延迟（如磁盘慢、网络抖动导致的底层延迟）
- **必现现象**：目标系统调用耗时显著增加；应用响应延迟增大；整体吞吐下降

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，难以直接实现系统调用级别的精确延迟注入。以下为近似方案。

前提条件：需安装 `strace` 或具备 BPF/eBPF 工具

近似注入（借 strace 的跟踪开销制造延迟，非精确注入）：
```bash
# 1) 先取目标 PID
pgrep -f <process-name>

# 2) attach 到该 PID。只允许 attach 形态：不带 -p 时 strace 会
#    直接【启动】其参数，那是任意命令执行而非跟踪，会被拒绝。
strace -p <pid> -e trace=<syscall> -T
```

恢复命令：
```bash
# 取 strace 自身的 PID 后终止，目标进程随即恢复原速
pgrep -f strace
kill <strace-pid>
```

注意事项：
- 原生 strace 附加本身会对进程产生显著性能开销（约 10-100x 减速），但无法精确控制延迟量
- 精确的 syscall 延迟注入是 ChaosBlade 的独特能力，原生命令难以完全等效替代
- 如需更精确的替代方案，可考虑使用 BCC/bpftrace 工具
