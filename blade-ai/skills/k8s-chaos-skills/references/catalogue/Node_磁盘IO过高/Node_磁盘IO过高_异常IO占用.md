**用例名称** 异常IO占用 导致 Node_磁盘IO过高

**故障现象**：
1. 节点磁盘 IO 使用率持续过高（iostat 显示 %util 接近 100%）
2. 节点上 Pod 的磁盘读写延迟增大，应用响应变慢
3. iowait 占比升高

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认监控系统可观测节点磁盘 IO 指标

**演练步骤**：
1. 定位运行应用 A 的节点
2. **路径校验（必须）**：注入前验证目标路径存在且可写：
   - 通过 `kubectl exec <tool-pod> -n chaosblade -- ls -ld <磁盘路径>` 确认路径存在
   - 如路径不存在，通过 `df -h` 查看可用挂载点，选择已存在的可写目录
   - 常见可用路径：`/var/lib/containerd`、`/var/lib/docker`、`/tmp`、`/var/log`
   - **禁止使用未校验的路径** — ChaosBlade 接受任意路径但底层 dd 进程会静默失败
   - 不同路径可能对应不同的物理磁盘分区，注入前可通过 `df -h <路径>` 确认路径所在的设备
3. 使用 chaosblade 对该节点注入磁盘 IO 负载：`blade create k8s node-disk burn --read --write --names <节点名> --path <已校验的磁盘路径> --timeout <秒> --kubeconfig <路径>`
4. 观察节点磁盘 IO 指标及 Pod 性能变化

**注入验证**：
1. 查看节点磁盘 IO 负载指标：
   - 优先：`iostat -xd 1 3`（关注 %util 接近 100%）
   - BusyBox 备选：`iostat -d -k 1 3`（关注 tps 和 kB_wrtn/s/kB_read/s 异常升高）+ `iostat -c 1 3`（关注 %iowait 显著升高）
   - 进程确认：`ps | grep dd` 确认 ChaosBlade 的 dd 压测进程在运行（若 dd 不存在，说明路径无效导致注入静默失败，需检查路径是否存在）
   - **多磁盘注意**：`iostat` 显示节点上所有物理磁盘的 IO 指标。如果节点有多个磁盘（如 vda 用于 nodefs，vdb 用于 imagefs），需根据 `--path` 参数判断目标路径对应的物理磁盘，只关注该磁盘的指标变化
2. 查看 iowait 占比，确认显著升高（`iostat -c` 或节点监控）
3. 确认应用 A 的磁盘读写延迟增大（见下方 Pod 级验证方法）

**Pod 级磁盘 IO 验证方法**：
- 方法 1（推荐）：在目标节点上选一个 Running Pod，`kubectl exec <pod> -- dd if=/dev/zero of=/tmp/disk-io-test bs=1M count=100`，对比注入前后写入耗时
- 方法 2：`kubectl exec <pod> -- df -h` 确认 Pod 所在容器的磁盘使用率（注意：显示的是 overlay 文件系统，非宿主机），`kubectl describe pod <pod>` 检查 Events 中是否有磁盘相关告警
- 方法 3（容器无 dd 时）：`kubectl get events -n <ns> --field-selector involvedObject.name=<pod>` 观察是否有 IO 相关事件

**注入恢复**：
1. 销毁 chaosblade 实验

**恢复验证**：
1. 查看节点磁盘 IO 监控，确认 %util 恢复正常：
   - 优先：`iostat -xd 1 3`
   - BusyBox 备选：`iostat -d -k 1 3`（确认 tps/吞吐量回落）+ `iostat -c 1 3`（确认 %iowait 恢复基线）
2. 确认 iowait 占比恢复正常
3. 确认应用 A 的磁盘读写延迟恢复正常（使用 Pod 级验证方法中的 dd 写入测试，确认耗时恢复）

**基准事实**：
- **根因**：节点上存在异常进程大量占用磁盘 IO，导致磁盘 IO 使用率过高，影响同节点上所有 Pod 的磁盘读写性能
- **必现现象**：节点磁盘 IO %util 接近 100%；iowait 占比升高；同节点 Pod 磁盘读写延迟增大

**CRD 模式 overlay 文件系统关键说明**：
- ChaosBlade K8s CRD 模式下，`node-disk burn --path /tmp` 的 dd 进程运行在 DaemonSet tool pod 内，
  写入的是**容器 overlay 文件系统**（通常由 imagefs/vdb 支持），**而非宿主机 /tmp**（通常由 nodefs/vda3 支持）
- **验证 burn 时不应该检查 /host/tmp/ 下的文件**：burn 产生的临时文件在 overlay 中，不会出现在宿主机路径
- **df -h 对 burn 验证无效**：burn 产生的是 I/O 压力（临时文件会被自动清理），不会造成磁盘使用量持续增长。
  用 df -h 验证 burn 可能导致误判为"注入失败"
- **正确的 burn 验证方法**：通过 `/proc/diskstats` 两次采样（间隔 3-5 秒），计算写入吞吐量增量：
  - 读取方法：`cat /proc/diskstats`，格式为 `major minor name reads reads_merged sectors_read ... sectors_written writes_merged sectors_written ...`
  - 计算方式：delta(sectors_written) × 512 / 间隔秒数 / 1048576 = MB/s
  - 判断标准：任何分区持续写入 >10MB/s 即表示 burn 在生效
  - 注意跳过分区条目（如 vda1、vda3），只关注整盘设备（如 vda、vdb）
- **fill 与 burn 的验证方法差异**：
  - fill（空间填充）：df -h 是主要验证手段（填充产生持久数据，磁盘使用量增加）
  - burn（IO 压力）：/proc/diskstats 是主要验证手段（burn 产生 IO 压力，不产生持久数据）
