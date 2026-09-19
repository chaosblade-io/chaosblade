**用例名称** 缓存占用过大 导致 Host_内存使用率过高

**故障现象**：
1. 主机 Page Cache 持续增长
2. 可用内存（free）减少，但 buff/cache 指标异常偏高
3. 系统在内存回收时出现性能抖动

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测主机内存指标（如 `free -m`、`/proc/meminfo`）

**演练步骤**：
1. 记录当前内存基线：`free -m`（关注 buff/cache 列）
2. 使用 ChaosBlade 注入缓存内存压力

```bash
blade create mem load --mode cache --mem-percent <percent> --timeout <duration>
```

参数说明：
- `--mode cache`：通过页面缓存占用内存
- `--mem-percent`：内存使用率百分比（如 70、80）
- `--timeout`：超时自动恢复（秒）

3. 观察 buff/cache 增长及系统性能变化

**注入验证**：
1. `free -m` 确认 buff/cache 列显著增长
2. `cat /proc/meminfo | grep -i cache` 确认 Cached 值升高
3. 观察是否触发内存回收（`vmstat 1` 关注 si/so 列）

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `free -m` 确认内存恢复正常
2. 手动回收缓存：`echo 3 > /proc/sys/vm/drop_caches`（如有需要）

**基准事实**：
- **根因**：文件系统缓存异常增长，占用大量可用内存，导致应用可用内存不足
- **必现现象**：buff/cache 持续升高；free 内存显著减少；可能出现内存回收导致的性能抖动

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

前提条件：主机需具备 `stress-ng`（`dd` 方案见下方说明，受故障族限制）

注入命令：
```bash
# 0) 先测基线：cat /proc/meminfo 记 MemTotal/MemAvailable（单位 kB）
#    分配量 = MemTotal × 目标百分比 − (MemTotal − MemAvailable)，按增量算，勿用绝对值

# stress-ng 的 vm stressor 与本用例同属 mem 故障族，可直接执行。
# 注意：vm stressor 占用的是**匿名内存**——观测面是 free 的 used 列增长，
# **不会**进入 buff/cache（Cached 仅 +37MB 噪音级）。--vm-keep 只改写
# 策略（驻留不释放），不改变内存类型。
# 效果口径：从「可用内存减少」的应用视角等效；但上方注入验证的 buff/cache
# 判据在 stress-ng 路径下**不可达**——若监控告警钉在 buff/cache 指标上，
# 原生路径无法复现该形态（仅人工 dd 路径可填 Page Cache），判读时改看 used。
stress-ng --vm 1 --vm-bytes <算出的分配量换算的MB数>M --vm-keep --timeout <duration>s
```
> 分配量按**增量**计算：主机已有基础用量，直接按目标百分比的绝对值分配会超量触发 OOM Killer。

> **为什么不用 `dd` 填充文件**：本用例的批准故障族是 `mem`（主路径 `blade create mem load
> --mode cache`），而 `dd` 被判为 `disk` 族。target_guard 的故障类型锁定会以
> `fault_target drift: approved=mem effective=disk` 拒绝跨族命令 —— 这不是命令写法问题，
> 改参数也过不去。若确实要用 dd 走磁盘路径填充 Page Cache，需要把演练本身按 `disk` 族
> 立项，或由人工执行：
> `dd if=/dev/zero of=/tmp/cache_fill bs=1M count=<size_in_MB>`

恢复命令：
```bash
# 清空填充文件即释放其占用的 Page Cache 与磁盘空间
truncate -s 0 /tmp/cache_fill
```

> 如需彻底丢弃全部 Page Cache，需人工执行 `echo 3 > /proc/sys/vm/drop_caches`（写 procfs 需要 shell 重定向，Agent 不执行）。多数演练不需要这一步：清空文件后该部分缓存已可回收。

注意事项：
- 原生方式不如 ChaosBlade 精确控制百分比
- `--timeout` 到期 stress-ng 自行退出；`truncate` 只在用过 dd 人工方案时才需要
- 跨故障族的命令（如 dd）会被 target_guard 的类型锁定拒绝，与命令本身是否安全无关
