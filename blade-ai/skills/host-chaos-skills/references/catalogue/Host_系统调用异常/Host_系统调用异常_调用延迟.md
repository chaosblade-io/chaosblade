**用例名称** 调用延迟 导致 Host_系统调用异常

## 故障定位
持续型故障——目标进程的某个系统调用在故障窗口内被持续注入延迟，使其单次耗时显著增加、应用响应变慢但不报错；窗口结束（`--timeout` 到期 / 武装的自恢复载体到期）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，都让目标 syscall 变慢，按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（`blade create strace delay`，可精确指定延迟量与注入位置、实验 UID 统一生命周期管理）；否则手段2（借原生 strace attach 的跟踪开销制造延迟，非精确、约 10-100x 减速）。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 特定系统调用执行时间显著增加
2. 应用 IO 操作、内存分配等变慢
3. 应用响应延迟增大但不报错

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标进程 PID：`pidof <process>` 或 `ps aux | grep <process>`
3. 确认目标系统调用名（如 read、write、open、mmap 等）

## 演练步骤
1. 确认目标进程 PID 和关键系统调用：`strace -c -p <pid> -e trace=<syscall> &`（采样 5 秒后 Ctrl+C）
2. 注入系统调用延迟 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

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

## 注入验证
> 判据可达性（ptrace 排他）：手段1（`blade create strace delay`，底层派生 `strace -p <pid> -f -e inject=…`）与手段2（原生 `strace -p <pid> -e trace=<syscall> -T`）在故障窗内**独占目标进程的 ptrace slot**——Linux 一进程同时只能被一个 tracer attach，故窗内**第二次** `strace -T -p <pid>` 必然失败（`ptrace: Operation not permitted`）。因此 `strace -T -p <pid>` **不是窗内可用判据**，只在 ptrace 释放后（见「恢复验证」）可达。窗内主判据收敛为下述 ① 行为级 + ② 机制归因白盒证，二者均无需二次 ptrace、始终可达（详见 `references/criterion-reachability.md`）。

**① 行为级主证（演练方提供应用访问入口时首选）**
1. 用只读 HTTP/RPC 探针对应用入口测端到端响应时间（如 `curl -sS -o /dev/null -w '%{time_total}' <url>`），确认响应延迟从基线显著上升、升幅与注入的 `--time` 量级相符
2. 延迟应稳定贯穿故障窗且**不报错**（区别于「调用返回错误」：本 case 只慢不错）

**② 机制归因白盒证（无应用入口时主证；始终可达、无需二次 ptrace）**
3. `pgrep -a strace` 见注入器命令行，其规则文本含目标 syscall 与延迟量（手段1 形如 `-e inject=<syscall>:delay_enter=<delay>`；手段2 为 attach 到目标 pid 的 `strace -p <pid>`），证明延迟注入器确已 attach 目标进程
4. `cat /proc/<pid>/status` 读 `TracerPid` 行由 `0` 变为**非 0**（= 注入器 strace 的 pid）、`State` 行为被跟踪态（`t (tracing stop)`），证明目标进程确被 ptrace attach
5. （可选交叉）`ss -ltnp` 观察目标监听 socket 的 `Recv-Q` 由 0 转为积压（accept 被延迟 → 连接堆积），作为延迟生效的旁证

**③ 判定规则**
- 有应用入口：以 ① 端到端延迟上升为主证、② 白盒证为归因佐证
- 无应用入口：以 ② 白盒证（注入器 attach + `TracerPid` 非 0 + 规则文本含目标 syscall/延迟量）为主证即可判定延迟已注入
- **不得**以窗内 `strace -T -p <pid>`（第二次 attach）作判据——它在窗内结构性不可达（见上方判据可达性）

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```

**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的定时器 `systemctl stop blade-kill-strace.timer`，再 `pgrep -x strace` 取 strace 自身 PID 后 `kill` 终止，目标进程随即恢复原速）

## 恢复验证
> 判据可达性：注入器（手段1 blade 实验销毁 / 手段2 原生 strace 被 timer 或手动 kill）退出后**已释放 ptrace slot**，故此处 `strace -T -p <pid>` 第二次 attach **可达**（与故障窗内相反）——这是本 case 中唯一能直接测得 syscall 耗时的时机。
1. `strace -T -p <pid> -e trace=<syscall>` 确认该系统调用耗时恢复到基线（ptrace 已释放，可正常 attach）
2. 白盒脱离证：`pgrep -x strace` 输出为空（注入器 strace 已终止）+ `cat /proc/<pid>/status` 的 `TracerPid` 回到 `0`、`State` 回到 `S (sleeping)`（目标进程不再被跟踪）
3. （可选，有访问入口时）确认应用响应延迟恢复正常

## 基准事实
- **根因**：特定系统调用出现异常延迟（如磁盘慢、网络抖动导致的底层延迟）
- **必现现象**：目标系统调用耗时显著增加；应用响应延迟增大；整体吞吐下降

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，借原生 strace attach 的跟踪开销制造系统调用延迟（非精确、约 10-100x 减速）；主机未装 ChaosBlade 时选用本手段。

前提条件：需安装 `strace` 或具备 BPF/eBPF 工具

近似注入（借 strace 的跟踪开销制造延迟，非精确注入；**先武装定时终止，再注入**——timer 到期自动 kill 掉 strace、目标进程恢复原速，补齐自恢复能力）：
```bash
# 1) 先取目标 PID
pgrep -f <process-name>

# 2) 先武装定时终止（定时器由宿主机 systemd(PID 1) 管理，到期自动 kill 掉
#    strace）。各命令分次独立执行（执行通道不支持 && 串联）；武装成功
#    （输出含 Running timer as unit）后再执行 attach
# <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
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
