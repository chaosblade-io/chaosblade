**⚠️ 注意：此场景为 kubectl-native 方案，dmsetup 步骤默认人工执行。选用前提是 ChaosBlade 没有 pod-IO target（以 `blade create k8s --help` 探测为准；若本地版本提供 `pod-IO errno`，优先用它），需通过 kubectl exec + dmsetup（device-mapper）在特权容器中实现。**
**执行边界**：dmsetup 改写的是**块设备映射表**（目标设备指定区段的数据可用性受直接影响），且命令位于 kubectl exec 载荷内、工具守卫不拦截——为防误操作，dmsetup 相关命令（创建/移除 error 映射）**由演练组织者人工执行或经人工确认后执行**；Agent 仅承担只读观察步骤（lsblk / blockdev --getsz / ls -ld）与注入前后的验证。

**用例名称** 文件系统IO返回错误 导致 Pod_磁盘IO异常

**故障现象**：
1. 应用写入数据失败，日志出现 I/O error 或 errno 5（EIO）
2. 数据库/缓存持久化操作异常，数据丢失风险
3. 文件系统读写操作间歇性返回错误码
4. 应用功能降级或部分请求失败

**资源准备**：
1. 确认应用 A 已正常运行，且有活跃的磁盘读写操作
2. 确认目标 Pod 内的文件路径存在且有读写活动
3. 确认监控系统可观测应用错误率和日志
4. 确认目标 Pod 以特权模式运行（`securityContext.privileged: true`），或通过 debug container 获得特权访问
5. 确认容器内有 `dmsetup` 工具（device-mapper 包），或通过 `kubectl debug` 附加含该工具的调试容器

**演练步骤**：
1. 定位应用 A 的 Pod，确认目标文件路径：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ls -ld <目录>
   ```
2. 获取目标目录所在块设备信息：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- df <目录>
   kubectl exec <pod-name> -n <namespace> -- lsblk
   ```
3. **先武装定时恢复，再注入**（到期自动移除 error 映射；恢复命令幂等：定时器到期自动恢复
   为主，Agent 在演练结束时主动执行同一条命令兜底，迟到重复执行无副作用（映射已移除时报
   not found）。dmsetup 是节点本地操作，恢复定时器用**宿主机 systemd-run 登记**——timer
   由宿主机 PID 1 管理，不依赖 debug Pod 存活（debug Pod 到期退出后，timer 仍按期
   自动移除了 dm 设备）；登记命令须经 `kubectl exec` 载体派发，顶层裸 `sh -c` 不被
   工具守卫放行）：
   ```bash
   # 武装定时自恢复（在节点 debug Pod 内执行；<dm-name> 必须与实际创建的 dm 设备名一致——
   # 曾因中途改设备名，首个 timer 到期时报移除目标不存在，需二次武装正确名字）
   kubectl exec <node-debug-pod> -n <debug-namespace> -c debugger -- chroot /host sh -c \
     'systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-dmerr \
        sh -c "dmsetup remove <dm-name>"'
   ```
4. 通过 dmsetup 创建 error 映射表，注入 IO 错误（需特权；**人工执行**，见文件头执行边界；
   dmsetup 操作节点块设备，载体为节点 debug Pod，chroot /host 内执行）：
   ```bash
   # 获取设备大小（sectors）
   kubectl exec <node-debug-pod> -n <debug-namespace> -c debugger -- chroot /host blockdev --getsz /dev/<device>
   # 创建 error 映射（前半段正常，后半段返回 IO error）
   kubectl exec <node-debug-pod> -n <debug-namespace> -c debugger -- chroot /host sh -c \
     'SECTORS=$(blockdev --getsz /dev/<device>) && \
      HALF=$((SECTORS / 2)) && \
      echo "0 $HALF linear /dev/<device> 0
   $HALF $HALF error" | dmsetup create error-device'
   ```
   - 原理：device-mapper 的 `error` target 会对所有落入该区段的 IO 请求返回 EIO
   - 注意：此操作会影响块设备上半区数据可用性，仅适用于演练环境

   > ⚠️ **linear 段源设备占用前置核查（限阿里云 ECS 云盘环境）**：对**挂载中的云盘**
   > 创建含 linear 段的映射一律失败——应用 PVC 云盘（挂载中）与容器盘
   > /var/lib/containerd 均报 `device-mapper: reload ioctl on <name> (252:0) failed:
   > Device or resource busy`（挂载文件系统对源设备持有独占打开，与 linear target 的设备
   > 获取冲突），「对已挂载文件系统所在分区直接建映射」的路径在本环境**不可达**。可行
   > 形态为**纯 error 设备**（无源设备依赖，创建即成功）：
   > ```bash
   > # 创建纯 error 设备（示例 512KB = 1024 sectors）
   > kubectl exec <node-debug-pod> -n <debug-namespace> -c debugger -- chroot /host sh -c \
   >   'echo "0 1024 error" | dmsetup create <dm-name>'
   > ```
   > 纯 error 设备独立于 debug Pod 存活（载体退出后设备仍在），且不影响宿主机既有挂载
   > （原云盘读写全程正常——爆炸半径仅为 dm 设备本身）。

5. 将应用的写入路径指向 error-device（通过 hostPath 或块设备卷将 /dev/mapper/<dm-name> 挂给目标 Pod；纯 error 设备不挂载则应用无感知）

**替代方案（更安全，推荐用于非特权环境）**：
使用 `pod-disk burn` 制造高 IO 负载，间接导致 IO 超时和错误：
```bash
blade create k8s pod-disk burn \
  --read --write \
  --path / \
  --size <size> \
  --namespace <namespace> \
  --labels "<label-key>=<label-value>" \
  --timeout <duration>

```
倒计时从武装时刻起算：--timeout 与注入命令一同下发原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `blade destroy <experiment_uid>` 旧实验，再重跑上方注入命令重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）
- `--path`：必须使用 `/`（容器根文件系统）。不要使用 EmptyDir、hostPath 等子目录挂载路径，这些路径在 ChaosBlade nsexec 模式下校验会失败
注意：`pod-disk burn` 制造的是 IO 高负载（吞吐饱和），而非精确的 errno 返回，效果为 IO 延迟剧增而非确定性错误码。

**注入验证**：
1. 对 error 映射设备发起写入，确认返回 IO error（在节点 debug Pod 内执行）：
   ```bash
   kubectl exec <node-debug-pod> -n <debug-namespace> -c debugger -- chroot /host sh -c \
     'dd if=/dev/zero of=/dev/mapper/<dm-name> bs=64k count=1 oflag=direct'
   ```
   - ⚠️ **判据必须带 `oflag=direct`**：默认（buffered）写入落页缓存即返回成功（RC=0），
     EIO 被推迟到后台 writeback 才发生，形成「注入未生效」误判；oflag=direct 直写立即
     报 `Input/output error`（读 iflag=direct 同样 EIO）
   - ⚠️ 写入量必须小于 dm 设备大小，否则先报 `No space left on device`（ENOSPC，是设备
     大小限制而非 error target 生效）
2. 查看应用日志，确认出现 `Input/output error` 或 errno 5 相关错误
3. 确认应用数据写入请求失败率上升
4. 查看 Pod Events：`kubectl get events -n <namespace> --field-selector involvedObject.name=<pod-name>`

**注入恢复**：
1. 等待 `<recovery-seconds>` 到期后 systemd 定时器自动移除 error 映射（journalctl 可见
   `<unit> Started … dmsetup remove` + `Succeeded` 记录）；演练提前结束时由 Agent 主动执行
   同一条恢复命令（幂等——映射移除后再执行报 not found 无副作用，定时器迟到重复执行无
   副作用；systemd timer 到期前无法可靠撤销，不设 pidfile、不做 kill）：
   ```bash
   kubectl exec <node-debug-pod> -n <debug-namespace> -c debugger -- chroot /host dmsetup remove <dm-name>
   ```
2. 若使用替代方案（pod-disk burn），销毁 blade 实验：`blade destroy <experiment_uid>`
3. 若应用未自动恢复，可重启 Pod 清除残留影响

**恢复验证**：
1. 确认 error 映射已移除：在节点 debug Pod 内执行 `dmsetup ls`，应返回 `No devices found`
   （或列表不含 <dm-name>）
2. 在 Pod 内重新写入文件，确认成功无报错（注入期间原挂载读写不受影响——纯 error 设备
   爆炸半径仅限 dm 设备本身；此步验证恢复后基线仍正常）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=<目录>/test bs=1M count=1
   ```
2. 查看应用日志，确认 IO error 不再出现
3. 确认应用数据写入功能恢复正常，错误率回落基线

**基准事实**：
- **根因**：文件系统 IO 操作返回错误码（EIO），模拟磁盘硬件故障或文件系统损坏场景，导致应用读写操作失败
- **必现现象**：Pod 内文件写入返回 Input/output error；应用日志出现 errno 5 相关错误；数据写入请求失败率上升
- **方案说明**：此为 kubectl-native 方案（选用前提：无 pod-IO target，以 `--help` 探测为准）。精确 IO 错误注入需要特权容器 + dmsetup；非特权环境可使用 `blade create k8s pod-disk burn` 作为近似替代（效果为 IO 饱和而非精确 errno）。（阿里云 ECS）：对挂载中云盘创建含 linear 段的映射一律 EBUSY，可行形态为纯 error 设备——应用需另行挂载该设备才有感知，否则故障仅表现为对 /dev/mapper/<dm-name> 的 direct IO 报 EIO
