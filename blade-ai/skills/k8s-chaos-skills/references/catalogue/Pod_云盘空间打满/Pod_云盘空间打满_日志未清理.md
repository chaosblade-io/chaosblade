**用例名称** 日志未清理 导致 Pod_云盘空间打满

**故障现象**：
1. Pod 挂载的云盘空间使用率达到 100%
2. 应用写入操作失败，日志报 `No space left on device`
3. 应用功能异常，数据无法持久化

**资源准备**：
1. 确认应用 A 已正常运行，且挂载了 PVC 用于数据存储
2. 确认 PVC 对应的云盘容量已知
3. 确认监控系统可观测 PVC 磁盘使用率

**演练步骤**：
1. 定位应用 A 的 Pod 及其 PVC 挂载路径
2. 使用 chaosblade 对应用 A 的 Pod 注入磁盘填充，模拟日志堆积打满云盘：
   ```bash
   blade create k8s pod-disk fill \
     --namespace <namespace> \
     --labels "app=<app>" \
     --path <PVC挂载路径> \
     --percent 99 \
     --timeout 300 \
     --kubeconfig <路径>
   ```
3. 观察应用 A 的写入行为和错误日志

**注入验证**：
1. 进入 Pod 查看挂载路径磁盘使用率：`df -h <挂载路径>`，确认使用率接近 100%
2. 在 Pod 内尝试写入文件，确认报 `No space left on device` 错误
3. 查看应用 A 日志，确认有写入失败相关错误
4. 确认应用 A 的业务功能受影响（如数据写入失败）

**注入恢复**：
1. 销毁 chaosblade 磁盘填充实验：`blade destroy <UID>`
2. 确认填充的临时文件被清理
3. 若空间未释放，手动清理注入的文件

**恢复验证**：
1. 进入 Pod 查看挂载路径磁盘使用率，确认恢复到正常水平
2. 在 Pod 内尝试写入文件，确认成功
3. 确认应用 A 的写入操作恢复正常
4. 确认应用 A 业务功能恢复

**基准事实**：
- **根因**：云盘上的日志或数据文件持续增长且未配置清理策略，最终占满整个磁盘空间，导致新的写入操作失败
- **必现现象**：磁盘使用率 100%；写入报 No space left on device；应用功能异常

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效磁盘填充。
> 两条路径按容器内是否有 `fallocate`/`dd` 二选一。

---

**路径 A —— 容器内有 `fallocate` 或 `dd`**

前提条件：容器内需有 `fallocate` 或 `dd` 工具。先确认：
```bash
kubectl exec <pod-name> -n <namespace> -- sh -c 'command -v fallocate; command -v dd'
```
任一存在即可走本路径；都没有（distroless / scratch 极简镜像的常态）走路径 B。

注入命令：
```bash
# 使用 fallocate 快速填充磁盘
kubectl exec <pod-name> -n <namespace> -- fallocate -l <size>G <PVC挂载路径>/fill_file
# 或使用 dd：
kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=<PVC挂载路径>/fill_file bs=1M count=<MB>
```

恢复命令：
```bash
kubectl exec <pod-name> -n <namespace> -- rm -f <PVC挂载路径>/fill_file
```

注意事项：
- `fallocate` 分配速度快（仅分配元数据），`dd` 实际写入数据速度较慢但更真实
- 无自动超时恢复机制，必须手动删除填充文件
- 需计算填充大小以确保磁盘使用率达到预期值（先用 `df -h` 查看剩余空间）

---

**路径 B —— 容器内没有 `fallocate`/`dd`：从节点侧写该 PVC**

PVC 在容器里是一个挂载点，它的真实存储在宿主机的 kubelet 目录下。从节点侧直接写，
工具来自 debug 镜像，不需要业务容器内有任何二进制。

> 本用例目标是 **PVC / 云盘**，实测这类卷挂在**独立块设备**上（如 `/dev/vdb`、`/dev/vde`，
> 与节点根盘 `/dev/vda3` 分离），填满只影响该 Pod —— 这正是本路径的安全适用场景。
> 但**必须在步骤 2 实测确认设备独立**后再写：若目标路径其实落在节点根盘
> （emptyDir 或容器 rootfs 的普通目录），填满会触发节点 `DiskPressure` 并驱逐其它 Pod，
> 此时应改用 `Node_磁盘空间不足` 用例并按节点级爆炸半径评估。

1. 取节点名与 Pod UID（宿主机路径以 UID 为目录名，**此处保持原样带 `-`**）：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.nodeName}
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.metadata.uid}
   ```

2. 在节点上定位 PVC 的宿主机路径并**确认设备独立**。实测路径规律：
   ```
   /var/lib/kubelet/pods/<PodUID>/volumes/kubernetes.io~csi/<volumeHandle>/mount
   ```
   `<volumeHandle>` 目录名（如 `d-hn3g3bxq9181lyh1roo4`）**无法从 Pod spec 推导**，
   只能实地列出：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'for d in /var/lib/kubelet/pods/<PodUID>/volumes/*/*/; do echo "$d"; df -h "$d" | tail -1; done'
   ```
   **判据**：目标卷那一行的 `Filesystem` **不是**节点根设备（不是挂在 `/` 的那个）才继续。

3. 注入 —— 往确认过的路径写填充文件：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c '
       systemd-run --on-active=<recovery-seconds>s --unit=blade-rmfill-<PodUID前8位> \
         rm -f <步骤2确认的路径>/fill_file &&
       fallocate -l <size>G <步骤2确认的路径>/fill_file
     '
   ```
   - **先武装定时删除再填充** —— debug pod 可能先于清理被删；定时器由宿主机 systemd(PID 1)
     管理，不受 debug pod 生命周期影响
   - `fallocate` 不可用时改 `dd if=/dev/zero of=<路径>/fill_file bs=1M count=<MB>`

验证：
```bash
# 容器内视角才是业务判据
kubectl exec <pod-name> -n <namespace> -- df -h <PVC挂载路径>
# 节点侧确认文件已生成
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host ls -lh <步骤2确认的路径>/fill_file
```
容器内 `df` 的 `Use%` 应显著上升。**节点侧看到文件存在只说明写成功，不代表业务容器
感知到空间不足** —— 以容器内 `df` 为准。

恢复：
```bash
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host rm -f <步骤2确认的路径>/fill_file
```

注意事项：
- **写宿主机路径等于写进容器** —— 同一份存储的两个视角，容器内立刻可见
- Pod UID 在此处**带 `-` 原样使用**，与 cgroup 路径要下划线化的规则相反，不要混用
- Pod 重建后 `<PodUID>` 目录更换，填充文件随旧目录被 kubelet 回收；若注入后 Pod 已重建，
  **不要再执行 rm**（路径已不存在）
- PVC 若被多个 Pod 共享（ReadWriteMany），填满会影响所有挂载方 —— 注入前用
  `kubectl get pvc <name> -n <namespace> -o jsonpath={.spec.accessModes}` 确认
- 无 `--timeout` 自动恢复，靠上面的 `systemd-run` 定时删除兜底
