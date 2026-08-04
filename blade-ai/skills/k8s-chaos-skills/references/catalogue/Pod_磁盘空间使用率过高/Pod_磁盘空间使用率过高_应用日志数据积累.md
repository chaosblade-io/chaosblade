**用例名称** 应用日志数据积累 导致 Pod_磁盘空间使用率过高

**故障现象**：
1. Pod 使用的存储空间接近上限，磁盘使用率告警
2. 应用可能出现写入失败、响应变慢等问题
3. 容器内日志目录（如 /var/log）文件过大

**资源准备**：
1. 确认应用 A/B 已正常运行
2. 确认应用配置了日志输出（如输出到 /tmp 或挂载目录）

**演练步骤**：
1. 定位应用 A 的 Pod，确认根文件系统可写：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -- df -h /
   ```
2. 使用 ChaosBlade 向应用 A 的 Pod 注入磁盘填充故障：
   ```bash
   blade create k8s pod-disk fill \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --path / \
     --percent 90 \
     --timeout 300 \
     --kubeconfig <kubeconfig-path>
   ```
   - `--path`：填充目标路径，使用 `/` 填充容器根文件系统
   - `--percent`：磁盘使用率目标百分比（优先级高于 `--size`）
   - `--size`：填充大小（MB），与 `--percent` 二选一
   - `--retain-handle`：是否保留文件句柄（保留时 rm 文件后空间不释放，更真实模拟日志占用）
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 进入应用 A 的 Pod，确认磁盘使用率已达目标：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- df -h /
   ```
   确认使用率达到注入的百分比
2. 在 Pod 内尝试写入文件，确认空间不足：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c 'echo test > /tmp/write_test'
   ```
3. 查看 Pod 事件，确认是否有磁盘相关异常：
   ```bash
   kubectl get events -n <namespace> --field-selector involvedObject.name=<pod-name>
   ```
4. 查看应用日志确认出现写入失败错误

**注入恢复**：
1. 销毁 ChaosBlade 磁盘填充实验：
   ```bash
   blade destroy <blade_uid>
   ```
2. 填充文件会随实验销毁自动清理
3. 若使用 `--retain-handle` 且空间未释放，可重启 Pod

**恢复验证**：
1. 查看 Pod 磁盘使用率，确认恢复到正常水平：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- df -h /
   ```
2. 在 Pod 内验证写入操作恢复正常
3. 确认 Pod 状态为 Running，无磁盘相关异常事件

**基准事实**：
- **根因**：应用日志未清理或数据写入过多，导致 Pod 存储空间被占满
- **必现现象**：Pod 磁盘使用率超过设定阈值，应用出现写入失败

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
kubectl exec <pod-name> -n <namespace> -- fallocate -l <size>G <目标目录>/fill_file
# 或使用 dd：
kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=<目标目录>/fill_file bs=1M count=<MB>
```

恢复命令：
```bash
kubectl exec <pod-name> -n <namespace> -- rm -f <目标目录>/fill_file
```

注意事项：
- `fallocate` 分配速度快（仅分配元数据），`dd` 实际写入数据速度较慢但更真实
- 无自动超时恢复机制，必须手动删除填充文件
- 需计算填充大小以确保磁盘使用率达到预期值（先用 `df -h` 查看剩余空间）

---

**路径 B —— 容器内没有 `fallocate`/`dd`：从节点侧写目标卷**

目标目录在容器里，但它的**真实存储在宿主机上**。从节点侧直接写那个路径，
工具来自 debug 镜像，不需要业务容器内有任何二进制。

> ⚠️ **必须先判定卷类型，这决定爆炸半径**：
> - **PVC / 云盘（`kubernetes.io~csi`）**：实测挂在**独立块设备**上（如 `/dev/vdb`、`/dev/vde`），
>   填满只影响该 Pod —— **这是本路径的安全适用场景**
> - **emptyDir（`kubernetes.io~empty-dir`）**：实测落在**节点根盘**（`/dev/vda3`，与
>   `/var/lib/kubelet`、容器运行时同盘）。填满会触发节点 `DiskPressure`，**驱逐该节点上
>   其它 Pod** —— 爆炸半径从单 Pod 扩到整节点。若目标是 emptyDir，**改用节点级用例**
>   （`Node_磁盘空间不足`）并按节点级爆炸半径评估，不要在这里做
> - **容器 rootfs 内的普通目录**（无卷挂载）：同样落在节点根盘，风险等同 emptyDir

1. 判定目标目录的卷类型与宿主机路径：
   ```bash
   # a) 看 Pod 声明的卷类型
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.volumes}
   # b) 看目标目录对应哪个卷
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath={.spec.containers[*].volumeMounts}
   # c) 取 Pod UID（宿主机路径以它为目录名，注意此处【不做】下划线化）
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.metadata.uid}
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.nodeName}
   ```

2. 在节点上定位并确认设备归属。**宿主机路径规律（实测）**：
   ```
   PVC/云盘 : /var/lib/kubelet/pods/<PodUID>/volumes/kubernetes.io~csi/<volumeHandle>/mount
   emptyDir : /var/lib/kubelet/pods/<PodUID>/volumes/kubernetes.io~empty-dir/<卷名>
   ```
   ```bash
   # 列出该 Pod 的全部卷，并确认各自所在设备 —— 只有独立设备才继续
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'for d in /var/lib/kubelet/pods/<PodUID>/volumes/*/*/; do echo "$d"; df -h "$d" | tail -1; done'
   ```
   **判据**：目标卷的 `Filesystem` 列**不是**节点根设备（不是 `/` 那一行）才继续；
   若与 `/` 同设备，回到上面的爆炸半径说明，改走节点级用例。
   也可用 `crictl inspect <containerID>` 读 `hostPath`↔`containerPath` 映射交叉核对。

3. 注入 —— 往确认过的宿主机路径写填充文件：
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
   - 文件名沿用 `fill_file`，与路径 A 一致，便于统一清理

验证（从容器内看使用率上升 —— 这才是业务视角的效果判据）：
```bash
# 容器内视角：目标目录所在文件系统使用率
kubectl exec <pod-name> -n <namespace> -- df -h <目标目录>
# 节点侧确认文件已生成
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host ls -lh <步骤2确认的路径>/fill_file
```
容器内 `df` 的 `Use%` 应显著上升。**容器内 `df` 才是判据** —— 节点侧看到文件存在
只说明写成功了，不代表业务容器感知到空间不足。

恢复：
```bash
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host rm -f <步骤2确认的路径>/fill_file
```

注意事项：
- **写宿主机路径等于写进容器** —— 同一份存储的两个视角，容器内立刻可见
- 目标路径必须是**步骤 2 实测确认过的**，不要凭规律直接拼 —— `volumeHandle` 目录名
  （如 `d-hn3g3bxq9181lyh1roo4`）无法从 Pod spec 推导，只能实地 `ls`
- Pod UID 在此处**保持原样带 `-`**（`/var/lib/kubelet/pods/001f9dc7-7c42-...`），
  与 cgroup 路径要下划线化的规则相反，不要混用
- Pod 重建后 `<PodUID>` 目录会更换，旧目录由 kubelet 回收；若注入后 Pod 已重建，
  填充文件随旧目录一起消失，此时**不要再执行 rm**（路径已不存在）
- 无 `--timeout` 自动恢复，靠上面的 `systemd-run` 定时删除兜底
