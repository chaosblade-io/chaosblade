**用例名称** 内存占用过大 导致 Host_内存使用率过高

**故障现象**：
1. 主机物理内存使用率持续超过 90%
2. 系统 Swap 使用率升高
3. 可能触发 OOM Killer 杀死进程

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测主机内存指标（如 `free -m`、Prometheus node_exporter）

**演练步骤**：
1. 记录当前内存基线：`free -m`
2. 使用 ChaosBlade 注入内存压力

```bash
blade create mem load --mode ram --mem-percent <percent> --timeout <duration>
```

参数说明：
- `--mode ram`：直接占用物理内存
- `--mem-percent`：内存使用率百分比（如 80、90）
- `--avoid-being-killed`：可选，防止被 OOM Killer 杀死
- `--rate`：可选，内存占用速率（MB/s）
- `--timeout`：超时自动恢复（秒）

3. 观察内存使用率及应用运行状态

**注入验证**：
1. `free -m` 确认可用内存显著减少
2. `vmstat 1` 确认 swap 使用变化
3. `dmesg | tail` 观察是否有 OOM 相关日志

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `free -m` 确认内存使用率恢复正常
2. 确认应用进程未被 OOM Kill（`dmesg | grep -i oom`）

**基准事实**：
- **根因**：异常进程大量占用物理内存，导致系统可用内存不足，可能触发 OOM Killer
- **必现现象**：物理内存使用率持续超过目标百分比；Swap 使用升高；可能出现进程被 OOM Kill

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

前提条件：主机需安装 `stress-ng`

注入命令：
```bash
# 1) 先读总内存（输出中的 MemTotal 单位是 kB）
cat /proc/meminfo

# 2) 取 MemTotal 的 80% 作为 --vm-bytes 的值填入（单位 k）
stress-ng --vm 1 --vm-bytes <MemTotal的80%>k --timeout <duration>s
```

恢复命令：
```bash
# stress-ng 在 --timeout 到期后自动退出
# 如需手动终止：
pgrep -f stress-ng
kill <pid>
```

注意事项：
- stress-ng 方式无法精确控制百分比，需手动计算内存量
- 无 `--avoid-being-killed` 等效保护，可能被 OOM Killer 杀死
