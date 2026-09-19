**用例名称** DiskPressure 导致 Pod_被驱逐重建

**故障现象**：
1. Pod 被 kubelet 驱逐（Evicted），状态为 Failed，reason 为 Evicted
2. 节点 Conditions 中 DiskPressure 为 True
3. 容器运行时目录（如 /var/lib/docker 或 /var/lib/containerd）磁盘使用超过 kubelet 驱逐阈值

**资源准备**：
1. 确认应用 A 已正常运行，有多个副本分布在不同节点
2. 确认目标节点的容器运行时磁盘当前使用率距离驱逐阈值有一定空间
3. 确认监控系统可观测节点磁盘使用率和 Pod 驱逐事件

**演练步骤**：
1. 前置确证（两项，直接决定填充量计算与本场景可行性）：
   ```bash
   # a) 分区拓扑：容器运行时目录与根分区是否同一分区（决定适用阈值）
   kubectl debug node/<节点名> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host df -h / /var/lib/containerd
   # b) kubelet 驱逐配置与 feature-gates（探测驱逐链路是否被集群定制禁用）
   kubectl debug node/<节点名> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c 'tr "\0" "\n" < /proc/$(pgrep -x kubelet | head -1)/cmdline | grep -E "eviction-hard|feature-gates"'
   ```
   - 阈值随分区拓扑变化：容器运行时目录与根分区**同分区**时，默认阈值为 `nodefs.available < 10%`（已用 >90%）；独立 imagefs 分区时才是 `imagefs.available < 15%`。显式 `--eviction-hard` 存在时以其为准
   - feature-gates 中若含 `DisablePodEviction=true`（ACK 等托管集群定制 gate），kubelet 驱逐链路被整体禁用：将根分区 available 压至 4% 以下并持续观察 90 秒，节点 DiskPressure condition 恒为 False、无任何 Pod 被驱逐。命中此 gate 时「Pod Evicted / DiskPressure=True」现象在该集群**不可达**——磁盘填充本身仍可注入且可观测（df 可证，节点定制检测组件会上报 RootDiskPressure 类事件），演练按「填充可验证 + 驱逐不可达」定案或更换集群，不得在报告中断言驱逐现象
2. 使用 chaosblade 对目标节点的容器运行时目录注入磁盘填充，使其超过驱逐阈值：
   ```bash
   blade create k8s node-disk fill \
     --names <节点名> \
     --path <容器运行时数据目录> \
     --percent <percent> \
     --timeout <duration>

   ```
   倒计时从武装时刻起算：--timeout 与注入命令一同下发原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `blade destroy <experiment_uid>` 旧实验，再重跑上方注入命令重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）
   （`--path` 必须按目标节点实际运行时探测填写：containerd 为 `/var/lib/containerd`，docker 为 `/var/lib/docker`，不得照抄；`--percent` 须超过驱逐阈值，按目标节点实际基线确定）
3. 等待 kubelet 检测到 DiskPressure 并触发 Pod 驱逐
4. 观察应用 A 的 Pod 驱逐和重建行为

**注入验证**：
1. **（即时主证据，必做）** 经 debug pod 确认填充已越过驱逐阈值：`kubectl debug node/<节点名> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host df -h <容器运行时数据目录>`，使用率超过演练步骤 1 探测的阈值（同分区 nodefs>90% 或独立 imagefs>85%）——填充是同步操作，df 即时可见，用于区分「填充未达阈值」与「驱逐传播延迟」两个失败层
2. 执行 `kubectl describe node <节点名>`，确认 Conditions 中 DiskPressure 为 True——该条件由 kubelet 周期检测同步，存在滞后，首查仍为 False 不构成反证（前置确证命中 `DisablePodEviction=true` 时此项不可达，按演练步骤第 1 步判读改验磁盘使用率与节点定制事件）
3. 执行 `kubectl get pods --field-selector=status.phase=Failed`，确认有 Pod 被 Evicted——驱逐由 kubelet 周期决策，同样存在滞后；第 1 条的 df 阈值确认成立而驱逐未出现时，属传播延迟而非注入失败，无需反复轮询
4. 查看被驱逐 Pod 的详情，确认 reason 为 `The node was low on resource: ephemeral-storage`
5. 确认应用 A 在其他节点重建 Pod

**注入恢复**：
1. 等待 chaosblade 实验自动超时恢复（`<duration>` 内），或执行 `blade destroy <UID>`
2. 等待节点磁盘空间释放
3. 清理 Evicted Pod：`kubectl delete pods --field-selector=status.phase=Failed`

**恢复验证**：
1. 执行 `kubectl describe node <节点名>`，确认 DiskPressure 恢复为 False
2. 确认应用 A 的 Pod 在正常节点上运行
3. 确认节点磁盘使用率恢复到安全水位

**基准事实**：
- **根因**：容器运行时目录磁盘使用超过 kubelet 驱逐阈值（DiskPressure），kubelet 按优先级驱逐 Pod 以释放磁盘空间
- **必现现象**：节点 DiskPressure=True；Pod 被 Evicted；reason 为 ephemeral-storage 不足

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效磁盘填充触发驱逐。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择已验证可拉取且含 `chroot`/`sh` 的镜像；宿主机变更必须 `--profile=sysadmin`；禁用 `-it`

注入命令（填充量必须按**增量**计算，先经 debug pod 测目标分区基线）：
```bash
# 0) 先测容器运行时目录所在分区的基线
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host df -h /var/lib/containerd

# 1) 填充量 = 分区总容量 × 目标使用率（对齐 blade --percent 注入值） − 当前已用量

# 2) 通过 kubectl debug node 在容器运行时目录填充数据。
#    **先武装定时清理，再填充**：timer 由宿主机 systemd(PID 1) 管理，到期自动删除填充文件；
#    `&&` 串联保证武装失败时不会执行填充
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-diskfill rm -f /var/lib/containerd/app-archive.log &&
   dd if=/dev/zero of=/var/lib/containerd/app-archive.log bs=1M count=<算出的填充量换算的MB数>'
# 或使用 fallocate（更快）：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-diskfill rm -f /var/lib/containerd/app-archive.log &&
   fallocate -l <算出的填充量>G /var/lib/containerd/app-archive.log'
```

恢复命令（timer 到期前可提前手动恢复）：
```bash
# 提前恢复：删除填充文件（同时停掉已武装的 timer）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemctl stop blade-restore-diskfill.timer 2>/dev/null; rm -f /var/lib/containerd/app-archive.log'
# 删除 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
# 清理 Evicted Pod
kubectl delete pods --field-selector=status.phase=Failed
```

注意事项：
- 填充大小需按**增量**计算（填充量 = 分区总容量 × 目标使用率 − 当前已用量），确保超过驱逐阈值——阈值随分区拓扑变化：容器运行时目录与根分区同分区时默认 `nodefs.available < 10%`，独立 imagefs 分区时默认 `imagefs.available < 15%`（显式 --eviction-hard 优先）；量太小不越阈值不触发驱逐，量太大把分区填满会影响恢复阶段写入
- 演练前先按手段1（演练步骤）第 1 步做前置确证：feature-gates 含 `DisablePodEviction=true` 的集群驱逐链路被禁用，填充可注入可自恢复但不会出现 DiskPressure=True 与 Pod Evicted，须按「填充可验证 + 驱逐不可达」定案
- 与 ChaosBlade `--percent` 不同，此方式需手动计算填充字节数
- 自恢复基于 systemd-run transient timer 到期自动删除填充文件，补齐了 ChaosBlade `--timeout` 的自恢复能力；被驱逐的 Pod 由上层控制器自动重建，Evicted 残留记录需手动清理
- 同名 transient timer 重复武装会报 `Unit blade-restore-diskfill.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先按本文件注入命令的同等 chroot /host 通道形态清理残留：`systemctl stop blade-restore-diskfill.service; systemctl reset-failed blade-restore-diskfill.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
