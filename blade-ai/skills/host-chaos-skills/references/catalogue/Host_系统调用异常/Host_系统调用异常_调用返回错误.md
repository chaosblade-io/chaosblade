**用例名称** 调用返回错误 导致 Host_系统调用异常

## 故障定位
持续型故障——目标进程的指定系统调用在故障窗口内被持续注入错误返回值（如 `openat` 返回 `-ENOENT`、`mmap` 返回 `-ENOMEM`），使应用走进「文件打开失败 / 内存分配失败」等错误处理路径（优雅降级或崩溃）；窗口结束（`--timeout` 到期 / `blade destroy <uid>`）即自动恢复原返回值。

**本 case 是「手段1-only」**：手段1（ChaosBlade `blade create strace error`）是精确 errno 注入的**唯一 Agent 可执行路径**。精确篡改 syscall 返回值**没有同故障类型的原生命令替身**——原生 `strace` attach 只能制造**延迟**（延迟 ≠ 错误码，属不同故障类型），`bpftrace override()` 需内核 `CONFIG_BPF_KPROBE_OVERRIDE`（多数生产内核未启用）且未纳入执行白名单。故文末「降级方案」块的 strace-delay 近似**不是手段1 的等价替身**：严禁在无 blade 时据它注入并宣称验证了「调用返回错误」（那测的是延迟、结论 false-pass），它仅在验证「调用路径异常时应用如何降级」时作观测替代。

`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 特定系统调用返回非预期错误码
2. 应用出现文件打开失败、内存分配失败等异常
3. 应用可能崩溃或进入错误处理分支

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标进程 PID：`pidof <process>` 或 `ps aux | grep <process>`
3. 确认目标系统调用名及期望注入的错误返回值

## 演练步骤
1. 确认目标进程使用的关键系统调用：`strace -c -p <pid>` 采样
2. 使用 ChaosBlade 注入系统调用返回值篡改

```bash
blade create strace error --pid <pid> --syscall-name <syscall> --return-value <value> --timeout <duration>
```

参数说明：
- `--pid`：目标进程 PID（必填）
- `--syscall-name`：目标系统调用名（必填，如 open、read、write、mmap、connect）
- `--return-value`：注入的 errno（必填，**传正 errno 数字**，如 `2`=ENOENT、`5`=EIO、`12`=ENOMEM；见下方「错误码参考」表）。blade 把该值**逐字**拼进 strace 的 `error=<值>` 字段，而 strace 的 `error=` 只接受正 errno——**传负值（如 `-2`）会令 strace 判 `invalid inject argument` 启动即退，blade 却仍返回 `success:true`（静默哑弹）**。
- `--first` / `--end` / `--step`：⚠️ 在 blade 1.8.2（捆绑 strace 6.6）上**请勿使用**——三者任一非空都会让 blade 拼出 legacy `when=<first>..<end>+<step>` 子句，被 strace 6.6 判 `invalid inject argument` 而静默哑弹。持续型窗口故障本就要「每次匹配 syscall 都注入」，省略三者即为该默认行为。
- `--timeout`：超时自动恢复（秒）

3. 观察应用错误处理行为

> ⚠️ **success:true ≠ 故障已落地**：blade 对 strace error 的哑弹（非法 inject spec 致 strace 启动即退）仍返回 `success:true` + `Status:Success` + 空 `Error`。故注入后**必须**以「注入验证 ①」的应用层行为证据（如 victim 日志翻为 `errno=2 (ENOENT)`）确认故障真已生效，不得凭 success 回执采信。诊断哑弹时给 `blade create` 加全局 `--debug`，可把真实 strace argv 与其否则被丢弃的 stderr 记进 `chaosblade.log`。

## 注入验证
> 判据可达性（ptrace 排他）：本 case 手段1-only——`blade create strace error` 底层派生 `strace -p <pid> -f -e inject=<syscall>:error=<errno>`，在故障窗内**独占目标进程的 ptrace slot**。Linux 一进程同时只能被一个 tracer attach，故窗内**第二次** `strace -e trace=<syscall> -p <pid>` 必然失败（`ptrace: Operation not permitted`）——它**不是窗内可用判据**，只在 ptrace 释放后（见「恢复验证」）可达。窗内主判据收敛为下述 ① 行为级 + ② 机制归因白盒证，二者均无需二次 ptrace、始终可达（详见 `references/criterion-reachability.md`）。

**① 行为级主证（始终可达、无需二次 ptrace）**
1. 观察应用日志/stderr 中的错误信息（如 `open` 被注入 `-ENOENT` → 应用报「文件不存在」；`mmap` 被注入 `-ENOMEM` → 报「内存分配失败」），确认错误类型与注入的 `--return-value`/errno 相符
2. 确认应用走进预期的错误处理路径（优雅降级 vs 崩溃 vs 重试）——这是「调用返回错误」故障的核心可观测效果

**② 机制归因白盒证（归因佐证；始终可达）**
3. `pgrep -a strace` 见注入器命令行，其规则文本含目标 syscall 与注入 errno（手段1 形如 `-e inject=<syscall>:error=<errno>`），证明 errno 注入器确已 attach 目标进程
4. `cat /proc/<pid>/status` 读 `TracerPid` 行由 `0` 变为**非 0**（= 注入器 strace 的 pid），证明目标进程确被 ptrace attach。⚠️ `TracerPid` 非 0 才是权威 attach 指标，`State` **不可靠**——error 注入用 `PTRACE_SEIZE` 且瞬时返回、不像 delay 那样把 tracee 停在 `t (tracing stop)`，故窗内 `State` 通常仍为 `S (sleeping)`。

**③ 判定规则**
- 以 ① 应用错误行为（日志错误类型与注入 errno 相符 + 走进错误处理路径）为主证、② 白盒证为归因佐证
- **不得**以窗内 `strace -e trace=<syscall> -p <pid>`（第二次 attach）作判据——它在窗内结构性不可达（见上方判据可达性）

## 注入恢复
```bash
blade destroy <experiment-uid>
```

## 恢复验证
> 判据可达性：`blade destroy <uid>` / `--timeout` 到期销毁实验后，注入器 strace **已释放 ptrace slot**，故此处 `strace -e trace=<syscall> -p <pid>` 第二次 attach **可达**（与故障窗内相反）——这是本 case 中唯一能直接测得 syscall 返回值的时机。
1. `strace -e trace=<syscall> -p <pid>` 确认该系统调用恢复正常返回值（ptrace 已释放，可正常 attach）
2. 白盒脱离证：`pgrep -x strace` 输出为空（注入器 strace 已终止）+ `cat /proc/<pid>/status` 的 `TracerPid` 回到 `0`、`State` 回到 `S (sleeping)`（目标进程不再被跟踪）
3. 确认应用恢复正常功能（不再报注入的错误、退出错误处理路径）

## 基准事实
- **根因**：系统调用返回异常错误（如磁盘故障导致 read 返回 -EIO，内存不足导致 mmap 返回 -ENOMEM）
- **必现现象**：系统调用返回错误码；应用进入错误处理路径；可能触发重试/降级/崩溃

**常见错误码参考**（`--return-value` 取下表 errno 数字列，**正值**）：
| --return-value | errno | 含义 |
|--------|-------|------|
| 1 | EPERM | 操作不允许 |
| 2 | ENOENT | 文件不存在 |
| 5 | EIO | IO 错误 |
| 12 | ENOMEM | 内存不足 |
| 13 | EACCES | 权限拒绝 |
| 28 | ENOSPC | 磁盘空间不足 |

> 内核层失败 syscall 返回的是该 errno 的负值（如 openat 失败返回 `-2`），但 blade `--return-value` 喂给 strace `error=` 的是**正 errno**（`2`）——注入后应用侧观测到的即是 `errno=2 (ENOENT)`。

---

## 降级方案（原生命令）

> 当 ChaosBlade 不可用时，难以直接实现系统调用返回值篡改。以下为近似方案。

前提条件：精确的 syscall 返回值篡改没有可由 Agent 执行的降级方案（见下）

**此场景无 Agent 可执行的降级路径。** 精确 errno 注入依赖 ChaosBlade 的 strace 执行器：
```bash
blade create strace error --pid <pid> --syscall-name <syscall> --return-value 1 --timeout <duration>
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
   # 先武装定时终止（timer 由宿主机 systemd(PID 1) 管理，到期自动 kill 掉 strace；
   # 详见「调用延迟」case），再 attach。武装成功（输出含 Running timer as unit）后再执行
   # <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
   systemd-run --on-active=<recovery-seconds>s --unit=blade-kill-strace \
     sh -c 'kill $(pgrep -x strace)'
   strace -p <pid> -e trace=<syscall> -T
   ```

恢复命令：
```bash
# 若用了上面第 2 条：先停定时器，再取 strace 自身 PID 后终止
# （-x 按进程名精确匹配；-f 会把携带 strace 字符串的执行 shell 一并匹配，
#   拿输出去 kill 会误杀执行 shell）
systemctl stop blade-kill-strace.timer 2>/dev/null
pgrep -x strace
kill <pid>
```

注意事项：
- 精确的 syscall 返回值篡改是 ChaosBlade 的独特能力
- bpftrace override 功能需要内核编译时启用 CONFIG_BPF_KPROBE_OVERRIDE
- 大多数生产内核未启用该功能，原生方案可用性有限
- 如需替代方案，可考虑 LD_PRELOAD 注入自定义 so 库拦截 libc 函数
- 第 2 条近似路径的定时终止、残留清理与「调用延迟」case 同款（blade-kill-strace timer；重复武装报 already loaded 时按该 case 注意事项清理残留）
