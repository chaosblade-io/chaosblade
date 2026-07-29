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
2. 进入目标节点，在 Volume 的挂载目录下创建一个持续占用文件句柄的进程，模拟 device busy：
   ```bash
   # 在节点上执行（通过 nsenter 或 debug pod）
   # 找到 volume 挂载路径
   mount | grep <pv-name>
   # 创建持续占用的进程
   tail -f <挂载路径>/some-file &
   ```
   或使用 chaosblade 对节点注入 IO 负载，锁住磁盘操作：
   ```bash
   blade create k8s node-disk burn \
     --names <节点名> \
     --path <volume挂载路径> \
     --read --write \
     --timeout 180 \
     --kubeconfig <路径>
   ```
3. 删除应用 A 的 Pod，触发 Terminating 流程
4. 观察 Pod Terminating 状态持续时间

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Terminating 且长时间未消失
2. 查看 Node Events 或 `kubectl describe pod`，确认有 UnmountVolume 失败或 device busy 相关错误
3. 在节点上确认 volume 挂载路径仍被占用

**注入恢复**：
1. 终止节点上占用 volume 的进程（kill tail 进程）
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

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令模拟 Volume 占用导致卸载失败。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择已验证可拉取且含 `chroot`/`sh` 的镜像；宿主机变更必须 `--profile=sysadmin`；禁用 `-it`

注入命令：
```bash
# 方式一：占用文件句柄（tail 作为 debug Pod 主进程；用 timeout 到点自动释放句柄=自动恢复）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- \
  chroot /host timeout <duration> tail -f <volume-mount-path>/some-file
# 方式二：持续读写占用磁盘 IO（用 timeout 让循环到点自停）：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'timeout <duration> sh -c "while true; do dd if=/dev/urandom of=<volume-mount-path>/chaos_busy bs=1M count=10; done"'
```

恢复命令：
```bash
# 占用进程会在 <duration> 到期后自动结束；如需立即释放：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'fuser -k <volume-mount-path>; rm -f <volume-mount-path>/chaos_busy'
# 清理 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
# 等待 kubelet 自动重试 unmount
```

注意事项：
- 需先通过 `mount | grep <pv-name>` 确认 volume 实际挂载路径
- 与 ChaosBlade node-disk burn 不同，此方式通过文件句柄占用而非 IO 负载来阻塞 unmount
- 用 `timeout <duration>` 让占用进程到点自动释放句柄（自动恢复）；debug 命令客户端会阻塞到 <duration> 或断连，但服务端持续运行
