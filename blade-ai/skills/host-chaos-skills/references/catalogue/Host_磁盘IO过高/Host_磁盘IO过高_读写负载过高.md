**用例名称** 读写负载过高 导致 Host_磁盘IO过高

**故障现象**：
1. 磁盘 %util 持续接近 100%
2. IO Wait（wa）显著升高
3. 应用读写延迟增大，吞吐下降

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测磁盘 IO（如 `iostat -xd 1`、Prometheus node_exporter）

**演练步骤**：
1. 记录当前 IO 基线：`iostat -xd 1 3`（关注 %util、r/s、w/s）
2. 使用 ChaosBlade 注入磁盘 IO 高负载

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

**注入验证**：
1. `iostat -xd 1` 确认 %util 接近 100%
2. `top` 确认 wa（IO Wait）显著升高
3. 观察应用读写延迟是否增大

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `iostat -xd 1` 确认 %util 回落到正常水平
2. 确认应用读写延迟恢复正常

**基准事实**：
- **根因**：磁盘被大量读写操作占满，导致 IO 队列堆积，正常应用的 IO 请求被延迟
- **必现现象**：%util 持续接近 100%；IO Wait 显著升高；应用读写延迟增大

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

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

# 如需提前终止：先取 PID 再杀
pgrep -f fio
kill <pid>
```

注意事项：
- `--direct=1` 绕过 Page Cache，确保压力落到真实磁盘
- 原生方式无法精确控制 IO 强度，效果取决于磁盘性能
- `--time_based --runtime` 即自动超时恢复；不要改成后台 `while` 循环 —— 那需要 shell 且必须手动记 PID 再 kill，中途失败会留下无人回收的压力进程
- 只有 `dd` 而无 `fio` 时：`dd` 单次执行是有界的，可按需重复执行
  `dd if=/dev/zero of=<path>/io_burn_file bs=1M count=500 oflag=direct` 逼近同等效果
