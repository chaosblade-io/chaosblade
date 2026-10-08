**用例名称** 磁盘被填满 导致 Host_磁盘空间不足

## 故障现象
1. 目标分区磁盘使用率超过 90%
2. 应用写入失败（No space left on device）
3. 日志写入中断，可能导致服务异常

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测磁盘空间（如 `df -h`、Prometheus node_exporter）
3. 确认目标目录所在分区非根分区或已设置安全 reserve

## 演练步骤
1. 记录当前磁盘基线：`df -h <path>`
2. 使用 ChaosBlade 注入磁盘填充

```bash
blade create disk fill --path <target-path> --percent <percent> --timeout <duration>
```

参数说明：
- `--path`：填充目标目录（必须为已存在的目录）
- `--percent`：目标分区使用率百分比（如 90、95）
- `--size`：可选，直接指定填充大小（MB），与 percent 互斥时 percent 优先
- `--reserve`：可选，保留空间大小（MB）
- `--retain-handle`：可选，保留文件句柄（rm 后仍占空间）
- `--timeout`：超时自动恢复（秒）

3. 观察磁盘使用率及应用写入情况

## 注入验证
1. `df -h <path>` 确认磁盘使用率达到目标百分比
2. 尝试在该分区写入文件确认失败：`dd if=/dev/zero of=<path>/test bs=1M count=1`
3. 观察应用日志是否出现 "No space left on device" 错误

## 注入恢复
```bash
blade destroy <experiment-uid>
```

## 恢复验证
1. `df -h <path>` 确认磁盘空间恢复
2. 确认应用写入恢复正常

## 基准事实
- **根因**：磁盘空间被大量占用（日志堆积、临时文件未清理等），导致分区剩余空间不足
- **必现现象**：磁盘使用率持续超过目标百分比；写入操作失败；应用出现磁盘相关错误

---

## 降级方案（原生命令）

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

前提条件：主机需具备 `dd` 或 `fallocate` 命令

注入命令（填充量必须按**增量**计算，先测基线）：
```bash
# 0) 先测目标分区基线：总容量、已用量、可用量
df -h <path>

# 1) 填充量 = 分区总容量 × 目标使用率 − 当前已用量
#    例：分区 100G、已用 40G、目标 90% → 100×0.9 − 40 = 50G

# 2) 先武装定时清理（到期自动删除填充文件，补齐自恢复能力），再填充。
#    两条命令分两次独立执行（执行通道不支持 && 串联）；武装成功（输出含 Running timer as unit）后再执行填充
# <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
systemd-run --on-active=<recovery-seconds>s --unit=blade-rmfill-host \
  rm -f <path>/app-archive.dat
# 使用 fallocate 快速创建大文件（推荐，速度快）
fallocate -l <算出的填充量>G <path>/app-archive.dat

# 若主机无 fallocate：用 dd 替代上面的 fallocate 部分（武装保持不变，同样
# 须先武装成功再填充，禁止单独执行 dd 而漏掉武装）：
# systemd-run --on-active=<recovery-seconds>s --unit=blade-rmfill-host \
#   rm -f <path>/app-archive.dat
# dd if=/dev/zero of=<path>/app-archive.dat bs=1M count=<填充量换算的MB数>
```
> 量太小达不到目标使用率；量太大把分区填到 100% 会影响验证观察与恢复阶段写入，
> 甚至触发非预期的系统级故障（日志写入中断、服务崩溃）。

恢复命令（提前恢复；先停武装的定时器再清理）：
```bash
systemctl stop blade-rmfill-host.timer 2>/dev/null
truncate -s 0 <path>/app-archive.dat
```

注意事项：
- 原生方式无法按百分比精确控制，需按**增量**手动计算填充大小（填充量 = 分区总容量 × 目标使用率 − 当前已用量）
- 自恢复基于注入前武装的 systemd-run transient timer（到期自动删除填充文件）；提前恢复仍用上方手动命令
- 注意不要对根分区执行填满操作
- 同名 transient timer 重复武装会报 `Unit blade-rmfill-host.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-rmfill-host.service; systemctl reset-failed blade-rmfill-host.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
