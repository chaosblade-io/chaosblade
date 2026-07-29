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

前提条件：容器内需有 `fallocate` 或 `dd` 工具

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
