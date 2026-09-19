**用例名称** Volume卸载失败 导致 Pod_Terminating

**故障现象**：
1. Pod 状态长时间停留在 Terminating
2. Pod Events 或 Node Events 中显示 `FailedMount`、`UnmountVolume failed`、`device is busy`
3. Volume 无法从节点上正常 unmount/detach，阻塞 Pod 终止流程

**资源准备**：
1. 确认应用 A 已正常运行，且挂载了 PVC（云盘类型）
2. 确认监控系统可观测 Pod 和 Volume 状态

**演练步骤**：
1. 定位应用 A 的 Pod 及其挂载的 Volume 路径
2. 进入目标节点，在 Volume 的挂载目录下创建一个持续占用文件句柄的进程，模拟 device busy
   （**必须用 `timeout` 自限时**，到期自动释放句柄=自动恢复；裸 `tail -f &` 会无限占用，
   遗忘后 volume 永久无法卸载）：
   ```bash
   # 在节点上执行（通过 nsenter 或 debug pod）
   # 找到 volume 挂载路径
   mount | grep <pv-name>
   # 创建限时占用的进程（<duration> 秒后自动结束）
   timeout <duration> tail -f <挂载路径>/some-file &
   ```
   或使用 chaosblade 对节点注入 IO 负载，锁住磁盘操作：
   ```bash
   blade create k8s node-disk burn \
     --names <节点名> \
     --path <volume挂载路径> \
     --read --write \
     --timeout <duration>

   ```
   倒计时从武装时刻起算：无论 timeout 限时占用还是 blade burn，武装后与后续的删 Pod 触发步骤必须是紧邻操作（≤60s）；武装后发生任何修复须先停旧定时（timeout 形态 kill 旧 tail 进程；blade 形态 `blade destroy <experiment_uid>`）再全额重武装，然后才触发删除（见 SKILL.md 安全红线「故障窗口完整」）
3. 删除应用 A 的 Pod，触发 Terminating 流程
4. 观察 Pod Terminating 状态持续时间

> ⚠️ **判据可达性警示（见手段2适用边界）**：diskplugin 类 CSI（如阿里云 ACK）环境中，Pod
> 对象删除与 volume unmount 异步分离——volume 清理不在 Pod 删除关键路径；且 EBUSY 是
> 引用语义（文件被打开占用）而非 IO 繁忙语义，IO 负载不产生 umount EBUSY。这两个
> 一般性结论同样约束本手段1（blade node-disk burn）：「Pod 卡 Terminating + Events
> device busy」判据在 CSI 卷 + 现代 kubelet 环境可达性存疑。判读前先小步验证——删
> Pod 后观察是否如期卡 Terminating；不可达时按手段2适用边界给出的替代观察点
> （节点侧 globalmount 滞留 + NodeUnstage 失败重试；该观察点需独立 globalmount 层，
> ACK 直挂形态下同样不可实例化）报告并调整判据，勿硬等不存在的现象。

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Terminating 且长时间未消失
2. 查看 Node Events 或 `kubectl describe pod`，确认有 UnmountVolume 失败或 device busy 相关错误
3. 在节点上确认 volume 挂载路径仍被占用

**注入恢复**：
1. 句柄占用进程（`timeout <duration> tail -f`）到期自动结束；如需提前恢复，终止占用进程
   （kill tail 进程或 `fuser -k <挂载路径>`）
2. 若使用 chaosblade：销毁 IO 实验 `blade destroy <UID>`
3. 等待 kubelet 自动重试 unmount 操作

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Terminating 的 Pod 已完成删除
2. 确认 volume 已成功 unmount 和 detach
3. 确认 PV 状态恢复为 Available 或被新 Pod 重新绑定

**基准事实**：
- **根因**：Volume unmount 时设备仍被占用（device busy）或 CSI 异常，导致 kubelet 无法完成卷卸载，Pod 终止流程被阻塞
- **必现现象**：Pod Terminating 持续；Events 显示 UnmountVolume failed/device busy；Volume 挂载路径未释放

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令模拟 Volume 占用导致卸载失败。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择已验证可拉取且含 `chroot`/`sh` 的镜像；宿主机变更必须 `--profile=sysadmin`；禁用 `-it`

> ⚠️ **适用边界（卷卸载走 lazy umount 或等价语义的 CSI，如阿里云 ACK diskplugin CSI）**：
> 本方案的机制链是「文件句柄占用 → umount EBUSY → Pod 卡 Terminating」，但此类 CSI
> （如 diskplugin.csi.alibabacloud.com）的卷卸载走 lazy umount（或等价语义）路径，
> **句柄占用无法阻塞卸载**——句柄持续占用期间，kubelet 日志仍显示
> `UnmountVolume.TearDown succeeded` + `UnmountDevice succeeded`（零失败零重试），
> Pod 数十秒内完成删除重建，三判据（Pod 卡 Terminating / Events 显示 device busy /
> 挂载路径仍被占用）全部不可达。内核 EBUSY 语义本身正常（fd 占用时 umount 返回 32，
> lazy umount 返回 0）——归因在 CSI 侧卸载语义。另注：Pod 对象删除与 volume unmount 本就是异步分离的
> （volume 清理不在 Pod 删除关键路径）——「volume 卸载失败卡 Pod Terminating」的
> 机制链在 CSI 卷 + 现代 kubelet 上不成立；如需验证卷清理异常，观察点是节点侧
> globalmount 路径滞留 + NodeUnstage 失败重试，而非 Pod Terminating。本用例的
> Pod_Terminating 判据在 CSI 环境下对手段1（blade node-disk burn，IO 负载形态）同样存疑
> ——异步分离不因注入方式而异，IO 繁忙也不产生引用语义的 EBUSY；主路径注入验证前已加
> 判据可达性警示，判读前先小步验证卡 Terminating 是否可达。
>
> 除 lazy umount 外的另一独立机制限制：`kubectl debug node` 注入 pod 的 `/host`
> 可能为 private rbind（容器内 mountinfo 中该挂载无 shared/master 传播标记，而宿主机
> 同挂载为 shared:N）。此时 chroot /host 后占用进程持有的 fd 落在容器私有 vfsmount
> 副本上，不钉宿主机引用计数——方式一（chroot 持句柄）在该形态下机制性无效，与 CSI
> 卸载语义无关、为独立根因。判定方法：注入前比对容器与宿主机 mountinfo 中 /host
> 挂载的传播标记。上文替代观察点（globalmount 滞留 + NodeUnstage 失败重试）亦仅
> 适用于有独立 globalmount 层的 CSI 形态；直挂形态（见下方注意事项）下结构性不可
> 实例化。lazy umount 与 private rbind 两重限制叠加时，本 case 三判据均不可达，
> 应改用同域其他手段（如 [Finalizers 未清理](Pod_Terminating_Finalizers未清理.md)）。

注入命令：
```bash
# 方式一：占用文件句柄（tail 作为 debug Pod 主进程；用 timeout 到点自动释放句柄=自动恢复）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- \
  chroot /host timeout <duration> tail -f <volume-mount-path>/some-file
# 方式二：持续读写占用磁盘 IO（用 timeout 让循环到点自停）：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'timeout <duration> sh -c "while true; do dd if=/dev/urandom of=<volume-mount-path>/.mountlock.dat bs=1M count=10; done"'
```

恢复命令：
```bash
# 占用进程会在 <duration> 到期后自动结束；如需立即释放：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'fuser -k <volume-mount-path>; rm -f <volume-mount-path>/.mountlock.dat'
# 清理 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
# 等待 kubelet 自动重试 unmount
```

注意事项：
- 需先通过 `mount | grep <pv-name>` 确认 volume 实际挂载路径（ACK 直挂形态：
  `/dev/vdc on /var/lib/kubelet/pods/<PodUID>/volumes/kubernetes.io~csi/<pv-name>/mount`，
  无独立 globalmount 层）
- 与 ChaosBlade node-disk burn 不同，此方式通过文件句柄占用而非 IO 负载来阻塞 unmount
  ——但在 lazy umount 语义的 CSI 上两者都无法阻塞卸载（见上方适用边界）
- 用 `timeout <duration>` 让占用进程到点自动释放句柄（自动恢复，机制正常）；
  debug 命令客户端会阻塞到 <duration> 或断连，但服务端持续运行（kubectl debug 无
  TTY 时立即返回不阻塞）
- 占用文件写在卷的文件系统内部，Pod 重建后仍残留于 PVC——收尾时记得清理 lock 文件
  （挂载点目录会随 Pod 删除被 kubelet 回收，但文件系统内容不会）
