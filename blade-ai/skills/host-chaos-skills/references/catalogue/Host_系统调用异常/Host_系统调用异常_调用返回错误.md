**用例名称** 调用返回错误 导致 Host_系统调用异常

**故障现象**：
1. 特定系统调用返回非预期错误码
2. 应用出现文件打开失败、内存分配失败等异常
3. 应用可能崩溃或进入错误处理分支

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标进程 PID：`pidof <process>` 或 `ps aux | grep <process>`
3. 确认目标系统调用名及期望注入的错误返回值

**演练步骤**：
1. 确认目标进程使用的关键系统调用：`strace -c -p <pid>` 采样
2. 使用 ChaosBlade 注入系统调用返回值篡改

```bash
blade create strace error --pid <pid> --syscall-name <syscall> --return-value <value> --timeout <duration>
```

参数说明：
- `--pid`：目标进程 PID（必填）
- `--syscall-name`：目标系统调用名（必填，如 open、read、write、mmap、connect）
- `--return-value`：篡改后的返回值（必填，如 -1 表示失败，-12 表示 ENOMEM）
- `--first`：可选，仅对前 N 次调用注入
- `--end`：可选，仅对最后 N 次调用注入
- `--step`：可选，间隔 N 次注入一次
- `--timeout`：超时自动恢复（秒）

3. 观察应用错误处理行为

**注入验证**：
1. `strace -e trace=<syscall> -p <pid>` 确认系统调用返回错误值
2. 观察应用日志中的错误信息
3. 确认应用是否正确处理了该错误（优雅降级 vs 崩溃）

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `strace -e trace=<syscall> -p <pid>` 确认系统调用恢复正常返回值
2. 确认应用恢复正常功能

**基准事实**：
- **根因**：系统调用返回异常错误（如磁盘故障导致 read 返回 -EIO，内存不足导致 mmap 返回 -ENOMEM）
- **必现现象**：系统调用返回错误码；应用进入错误处理路径；可能触发重试/降级/崩溃

**常见错误码参考**：
| 返回值 | errno | 含义 |
|--------|-------|------|
| -1 | EPERM | 操作不允许 |
| -2 | ENOENT | 文件不存在 |
| -5 | EIO | IO 错误 |
| -12 | ENOMEM | 内存不足 |
| -13 | EACCES | 权限拒绝 |
| -28 | ENOSPC | 磁盘空间不足 |

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，难以直接实现系统调用返回值篡改。以下为近似方案。

前提条件：精确的 syscall 返回值篡改没有可由 Agent 执行的降级方案（见下）

**此场景无 Agent 可执行的降级路径。** 精确 errno 注入依赖 ChaosBlade 的 strace 执行器：
```bash
blade create strace error --pid <pid> --syscall-name <syscall> --return-value -1 --timeout <duration>
```

ChaosBlade 不可用时，只有两条路，都不由 Agent 执行：

1. **人工用 bpftrace**（内核需编译时启用 `CONFIG_BPF_KPROBE_OVERRIDE`，多数生产内核未启用）：
   ```text
   bpftrace -e 'tracepoint:syscalls:sys_exit_open /pid == <pid>/ { override(args->ret, -1); }'
   ```
   `bpftrace -e` 接受任意 BPF 程序，`override()` 可改写内核态返回值，程序里还能调 `system()`。
   它的风险在程序文本里，不在命令形态上 —— 无法像 `nc` 只允许监听、`strace` 只允许 attach
   那样用参数检查框住，所以未纳入执行白名单。

2. **退化为观测型近似**：用已放行的 `strace` attach 到目标 PID，制造调用开销而非篡改返回值。
   这不是同一个故障（延迟 ≠ 错误码），只在验证「调用路径异常时应用如何降级」时可替代：
   ```bash
   pgrep -f <process-name>
   strace -p <pid> -e trace=<syscall> -T
   ```

恢复命令：
```bash
# 若用了上面第 2 条：取 strace 自身 PID 后终止
pgrep -f strace
kill <pid>
```

注意事项：
- 精确的 syscall 返回值篡改是 ChaosBlade 的独特能力
- bpftrace override 功能需要内核编译时启用 CONFIG_BPF_KPROBE_OVERRIDE
- 大多数生产内核未启用该功能，原生方案可用性有限
- 如需替代方案，可考虑 LD_PRELOAD 注入自定义 so 库拦截 libc 函数
