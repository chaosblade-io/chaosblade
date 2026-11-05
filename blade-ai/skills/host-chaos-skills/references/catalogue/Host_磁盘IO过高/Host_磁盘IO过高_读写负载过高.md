**用例名称** 读写负载过高 导致 Host_磁盘IO过高

## 故障定位
持续型故障——磁盘 IO 压测（burn / fio）进程存活即高负载存活，贯穿整个故障窗口；窗口结束（`--timeout` / `--runtime` 到期进程退出 / 实验销毁）即自动恢复，无持久残留（不改文件内容/分区/配置，仅测试文件占空间需回收）。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是起密集读写占满磁盘 IO 带宽、抬高 %util 与 iowait），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理）；否则手段2（需 `fio`，仅有 `dd` 见文末替代写法）。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 磁盘 %util 持续接近 100%
2. IO Wait（wa）显著升高
3. 应用读写延迟增大，吞吐下降

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）；未装则走文末**手段2（原生命令）**（需 `fio`，仅有 `dd` 见替代写法）
2. 确认监控系统可观测磁盘 IO（如 `iostat -xd 1`、Prometheus node_exporter）
3. **爆炸半径约束**：`--path` 默认 `/` 会对根分区施加 IO 压力——在容器/共享宿主上会波及整块磁盘。演练须把 `--path`（及手段2 的 `<path>`）指向一个**专用有界分区**（如固定大小 loop 设备挂载点），把 IO 半径锁死在该分区内，严禁打根 overlay

## 演练步骤
1. 记录当前 IO 基线：`iostat -xd 1 3`（关注 %util、r/s、w/s）
2. 注入磁盘 IO 高负载 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create disk burn --read --write --path <target-path> --timeout <duration>
```

参数说明：
- `--read`：注入读 IO 负载
- `--write`：注入写 IO 负载
- `--path`：IO 操作目标目录（默认 /）
- `--size`：可选，块大小（MB），默认 10
- `--timeout`：超时自动恢复（秒）

3. 观察磁盘 IO 指标及应用性能变化

## 注入验证
1. `iostat -xd 1` 确认 %util 接近 100%
2. `top` 确认 wa（IO Wait）显著升高
3. （可选，仅当演练方提供了应用访问入口时）确认读写延迟增大；无入口时上述 %util 与 iowait 证据成立即可判定

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（fio `--runtime` 自停 / 回收测试文件）

## 恢复验证
1. `iostat -xd 1` 确认 %util 回落到正常水平
2. （可选，有访问入口时）确认应用读写延迟恢复正常

## 基准事实
- **根因**：磁盘被大量读写操作占满，导致 IO 队列堆积，正常应用的 IO 请求被延迟
- **必现现象**：%util 持续接近 100%；IO Wait 显著升高；应用读写延迟增大

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（`fio` 起密集读写占满磁盘 IO 带宽、抬高 %util 与 iowait）；主机未装 ChaosBlade 时选用本手段。

前提条件：主机需具备 `fio`（若只有 `dd`，见文末替代写法）

注入命令：
```bash
# 写 IO 负载：--time_based + --runtime 自带有界生命周期，到期自行退出，
# 不需要后台化、不需要记 PID、也不需要手动 kill。--direct=1 绕过缓存。
fio --name=iowrite --filename=<path>/io_burn_file --rw=write --bs=1M --size=1G \
    --direct=1 --time_based --runtime=<duration>

# 读 IO 负载
fio --name=ioread --filename=<path>/io_read_file --rw=read --bs=1M --size=1G \
    --direct=1 --time_based --runtime=<duration>

# 读写混合（一条命令同时施加两向压力）
fio --name=iomix --filename=<path>/io_mix_file --rw=readwrite --bs=1M --size=1G \
    --direct=1 --time_based --runtime=<duration>
```

恢复命令：
```bash
# --runtime 到期后 fio 自行退出，正常路径无需干预；只需回收测试文件占用的空间
truncate -s 0 <path>/io_burn_file <path>/io_read_file <path>/io_mix_file

# 如需提前终止：先取 PID 再杀（必须 -x 按进程名精确匹配；-f 会把携带 fio 字符串的
# 执行 shell 一并匹配，拿输出去 kill 会误杀执行 shell，同款陷阱见「文件句柄耗尽」case）
pgrep -x fio
kill <pid>
```

注意事项：
- `--direct=1` 绕过 Page Cache，确保压力落到真实磁盘
- 原生方式无法精确控制 IO 强度，效果取决于磁盘性能
- `--time_based --runtime` 即自动超时恢复；不要改成后台 `while` 循环 —— 那需要 shell 且必须手动记 PID 再 kill，中途失败会留下无人回收的压力进程
- 只有 `dd` 而无 `fio` 时：`dd` 单次执行是有界的，可按需重复执行
  `dd if=/dev/zero of=<path>/io_burn_file bs=1M count=500 oflag=direct` 逼近同等效果
