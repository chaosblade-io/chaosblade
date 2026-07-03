**⚠️ 注意：此场景为 kubectl-native 方案。blade v1.9.0 不支持文件系统级 IO 错误注入（无 pod-IO target），需通过 kubectl exec + dmsetup（device-mapper）在特权容器中实现。**

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
3. 通过 dmsetup 创建 error 映射表，对目标设备注入 IO 错误（需特权）：
   ```bash
   # 获取设备大小（sectors）
   kubectl exec <pod-name> -n <namespace> -- blockdev --getsz /dev/<device>
   # 创建 error 映射（前半段正常，后半段返回 IO error）
   kubectl exec <pod-name> -n <namespace> -- sh -c \
     'SECTORS=$(blockdev --getsz /dev/<device>) && \
      HALF=$((SECTORS / 2)) && \
      echo "0 $HALF linear /dev/<device> 0
   $HALF $HALF error" | dmsetup create error-device'
   ```
   - 原理：device-mapper 的 `error` target 会对所有落入该区间的 IO 请求返回 EIO
   - 注意：此操作会影响块设备上半区数据可用性，仅适用于演练环境
4. 将应用的写入路径指向 error-device（或直接在已挂载的文件系统分区上操作）

**替代方案（更安全，推荐用于非特权环境）**：
使用 `pod-disk burn` 制造高 IO 负载，间接导致 IO 超时和错误：
```bash
blade create k8s pod-disk burn \
  --read --write \
  --path / \
  --size 100 \
  --namespace <namespace> \
  --labels "<label-key>=<label-value>" \
  --timeout 600 \
  --kubeconfig <kubeconfig-path>
```
- `--path`：必须使用 `/`（容器根文件系统）。不要使用 EmptyDir、hostPath 等子目录挂载路径，这些路径在 ChaosBlade nsexec 模式下校验会失败
注意：`pod-disk burn` 制造的是 IO 高负载（吞吐饱和），而非精确的 errno 返回，效果为 IO 延迟剧增而非确定性错误码。

**注入验证**：
1. 在 Pod 内尝试写入文件，确认返回 IO error：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=<目录>/test bs=1M count=1
   ```
2. 查看应用日志，确认出现 `Input/output error` 或 errno 5 相关错误
3. 确认应用数据写入请求失败率上升
4. 查看 Pod Events：`kubectl get events -n <namespace> --field-selector involvedObject.name=<pod-name>`

**注入恢复**：
1. 移除 dmsetup error 设备映射：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- dmsetup remove error-device
   ```
2. 若使用替代方案（pod-disk burn），销毁 blade 实验：`blade destroy <blade_uid>`
3. 若应用未自动恢复，可重启 Pod 清除残留影响

**恢复验证**：
1. 在 Pod 内重新写入文件，确认成功无报错：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=<目录>/test bs=1M count=1
   ```
2. 查看应用日志，确认 IO error 不再出现
3. 确认应用数据写入功能恢复正常，错误率回落基线

**基准事实**：
- **根因**：文件系统 IO 操作返回错误码（EIO），模拟磁盘硬件故障或文件系统损坏场景，导致应用读写操作失败
- **必现现象**：Pod 内文件写入返回 Input/output error；应用日志出现 errno 5 相关错误；数据写入请求失败率上升
- **方案说明**：此为 kubectl-native 方案（blade v1.9.0 无 pod-IO target）。精确 IO 错误注入需要特权容器 + dmsetup；非特权环境可使用 `blade create k8s pod-disk burn` 作为近似替代（效果为 IO 饱和而非精确 errno）
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

**演练步骤**：
1. 定位应用 A 的 Pod，确认目标文件路径：`kubectl exec <pod> -n <namespace> -- ls -ld <目录>`
2. 使用 chaosblade 对目标 Pod 注入文件系统 IO 错误：
   ```bash
   blade create k8s pod-IO errno \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --errno 5 \
     --path <目录> \
     --methods read,write \
     --percent 50 \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在 Pod 内尝试写入文件，确认返回 IO error：
   ```bash
   kubectl exec <pod> -n <namespace> -- dd if=/dev/zero of=<目录>/test bs=1M count=1
   ```
2. 查看应用日志，确认出现 `Input/output error` 或 errno 5 相关错误
3. 确认应用数据写入请求失败率上升（部分请求因 --percent 50 仍可成功）
4. 查看 Pod Events：`kubectl get events -n <namespace> --field-selector involvedObject.name=<pod>`

**注入恢复**：
1. 销毁 chaosblade 实验：`blade destroy <blade_uid>`
2. 若应用未自动恢复，可重启 Pod 清除残留影响

**恢复验证**：
1. 在 Pod 内重新写入文件，确认成功无报错：
   ```bash
   kubectl exec <pod> -n <namespace> -- dd if=/dev/zero of=<目录>/test bs=1M count=1
   ```
2. 查看应用日志，确认 IO error 不再出现
3. 确认应用数据写入功能恢复正常，错误率回落基线

**基准事实**：
- **根因**：文件系统 IO 操作返回错误码（EIO），模拟磁盘硬件故障或文件系统损坏场景，导致应用读写操作间歇性失败
- **必现现象**：Pod 内文件写入返回 Input/output error；应用日志出现 errno 5 相关错误；数据写入请求失败率上升
