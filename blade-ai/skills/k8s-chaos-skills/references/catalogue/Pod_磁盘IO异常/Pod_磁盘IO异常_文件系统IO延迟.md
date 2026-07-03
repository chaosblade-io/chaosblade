**⚠️ 注意：此场景为 kubectl-native 方案。blade v1.9.0 不支持文件系统级 IO 延迟注入（无 pod-IO target），需通过 kubectl exec + tc（块设备级）或 blade pod-disk burn（IO 饱和）实现近似效果。**

**用例名称** 文件系统IO延迟 导致 Pod_磁盘IO异常

**故障现象**：
1. 应用读写操作耗时明显增加，响应延迟上升
2. 数据库慢查询增多，出现查询超时
3. 文件系统操作阻塞导致请求处理变慢
4. 应用吞吐量下降，P99 延迟显著升高

**资源准备**：
1. 确认应用 A 已正常运行，且有活跃的磁盘读写操作
2. 确认目标 Pod 内的根文件系统可写：`kubectl exec <pod-name> -n <namespace> -- df -h /`
3. 确认监控系统可观测应用延迟指标
4. 确认容器内有 `dd` 工具（用于验证 IO 性能）

**演练步骤**：
1. 定位应用 A 的 Pod，确认根文件系统可写：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- touch /chaos_burnio_test && rm -f /chaos_burnio_test
   ```
2. 使用 `blade create k8s pod-disk burn` 对目标 Pod 注入持续高 IO 负载，间接制造 IO 延迟：
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
   - `--read --write`：同时制造读写 IO 负载
   - `--path`：必须使用 `/`（容器根文件系统）。不要使用 EmptyDir、hostPath 等子目录挂载路径，这些路径在 ChaosBlade nsexec 模式下校验会失败
   - `--size`：每次写入块大小（MB），默认 10，增大可加剧 IO 竞争
   - 原理：通过持续大量读写 IO 操作使磁盘 IO 队列饱和，间接导致应用的 IO 请求排队等待，表现为 IO 延迟显著增加
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在 Pod 内执行写入操作，确认耗时明显增加：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=/chaos_iodelay_test bs=1M count=10 oflag=dsync
   ```
2. 对比注入前后写入耗时（注入后因 IO 队列饱和，写入吞吐显著下降）
3. 查看应用日志，确认出现 slow query 或 timeout 相关告警
4. 确认应用请求延迟 P99 显著上升
5. 查看 Pod 内 IO 等待情况：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/diskstats
   ```

**注入恢复**：
1. 销毁 blade 实验：`blade destroy <blade_uid>`
2. 或等待 `--timeout` 600 秒到期后自动恢复
3. 若应用存在连接池超时，可能需等待连接回收或重启 Pod

**恢复验证**：
1. 在 Pod 内重新执行写入操作，确认耗时恢复正常：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=/chaos_iodelay_test bs=1M count=10 oflag=dsync
   ```
2. 查看应用日志，确认 slow query 和 timeout 告警消失
3. 确认应用请求延迟 P99 恢复到基线水平

**基准事实**：
- **根因**：通过 pod-disk burn 使磁盘 IO 队列饱和，应用的正常 IO 请求需排队等待，表现为 IO 操作延迟显著增加
- **必现现象**：Pod 内文件读写耗时显著增加；磁盘 IO 利用率接近 100%；应用出现慢查询或超时；请求延迟 P99 升高
- **方案说明**：此为 blade pod-disk burn 近似方案（blade v1.9.0 无 pod-IO target）。与精确 IO 延迟注入（每次 IO 固定增加 Nms）不同，burn 方案通过 IO 竞争间接制造延迟，效果为非确定性延迟增加而非固定值注入
**用例名称** 文件系统IO延迟 导致 Pod_磁盘IO异常

**故障现象**：
1. 应用读写操作耗时明显增加，响应延迟上升
2. 数据库慢查询增多，出现查询超时
3. 文件系统操作阻塞导致请求处理变慢
4. 应用吞吐量下降，P99 延迟显著升高

**资源准备**：
1. 确认应用 A 已正常运行，且有活跃的磁盘读写操作
2. 确认目标 Pod 内的文件路径存在且有读写活动
3. 确认监控系统可观测应用延迟指标

**演练步骤**：
1. 定位应用 A 的 Pod，确认目标文件路径：`kubectl exec <pod> -n <namespace> -- ls -ld <目录>`
2. 使用 chaosblade 对目标 Pod 注入文件系统 IO 延迟：
   ```bash
   blade create k8s pod-IO delay \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --time 500 \
     --path <目录> \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在 Pod 内执行写入操作，确认耗时明显增加：
   ```bash
   kubectl exec <pod> -n <namespace> -- dd if=/dev/zero of=<目录>/test bs=1M count=10
   ```
2. 对比注入前后写入耗时（注入后每次 IO 操作增加约 500ms 延迟）
3. 查看应用日志，确认出现 slow query 或 timeout 相关告警
4. 确认应用请求延迟 P99 显著上升

**注入恢复**：
1. 销毁 chaosblade 实验：`blade destroy <blade_uid>`
2. 若应用存在连接池超时，可能需等待连接回收或重启 Pod

**恢复验证**：
1. 在 Pod 内重新执行写入操作，确认耗时恢复正常：
   ```bash
   kubectl exec <pod> -n <namespace> -- dd if=/dev/zero of=<目录>/test bs=1M count=10
   ```
2. 查看应用日志，确认 slow query 和 timeout 告警消失
3. 确认应用请求延迟 P99 恢复到基线水平

**基准事实**：
- **根因**：文件系统 IO 操作被注入额外延迟，模拟磁盘性能退化或存储后端响应慢的场景，导致应用读写操作耗时增加
- **必现现象**：Pod 内文件读写耗时显著增加（每次 IO 增加约 500ms）；应用出现慢查询或超时；请求延迟 P99 升高
