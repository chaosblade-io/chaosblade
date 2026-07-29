**用例名称** 磁盘IO读写负载 导致 Pod_磁盘IO过高

**故障现象**：
1. Pod 内磁盘 IO 利用率飙升，读写吞吐量异常升高
2. 应用读写延迟明显增加，请求处理变慢
3. 同节点其他 Pod 可能受 IO 带宽争抢影响
4. 应用出现读写超时或性能退化

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认目标 Pod 内的根文件系统可写：`kubectl exec <pod> -n <namespace> -- df -h /`
3. 确认监控系统可观测 Pod 级磁盘 IO 指标

**演练步骤**：
1. 定位应用 A 的 Pod，确认根文件系统路径可写：`kubectl exec <pod> -n <namespace> -- touch /chaos_burnio_test && rm -f /chaos_burnio_test`
2. 使用 chaosblade 对目标 Pod 注入磁盘 IO 读写负载：
   ```bash
   blade create k8s pod-disk burn \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --path / \
     --read \
     --write \
     --size 10 \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
   - `--path` 必须使用 `/`（容器根文件系统，overlay 挂载）。不要使用 EmptyDir、hostPath 等子目录挂载路径，这些路径在 ChaosBlade nsexec 模式下校验会失败。
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. 在 Pod 内查看磁盘 IO 指标，确认 IO 活动异常升高：
   ```bash
   kubectl exec <pod> -n <namespace> -- cat /proc/diskstats
   ```
   （间隔 3-5 秒采样两次，计算写入/读取吞吐量增量）
2. 确认 burn 进程在运行：`kubectl exec <pod> -n <namespace> -- ps aux | grep dd`
3. 查看应用日志，确认出现读写超时或延迟告警
4. 确认应用请求处理延迟上升（对比注入前基线）

**注入恢复**：
1. 销毁 chaosblade 实验：`blade destroy <blade_uid>`
2. burn 产生的临时文件会随实验销毁自动清理

**恢复验证**：
1. 在 Pod 内再次查看 `/proc/diskstats`，确认 IO 吞吐量恢复基线
2. 确认 dd 压测进程已终止：`kubectl exec <pod> -n <namespace> -- ps aux | grep dd`
3. 确认应用读写延迟恢复正常，请求处理耗时回落基线

**基准事实**：
- **根因**：Pod 内产生大量磁盘读写负载，模拟应用异常 IO 操作或日志洪峰场景，导致磁盘 IO 带宽被占满，影响正常业务读写性能
- **必现现象**：Pod 内磁盘 IO 吞吐量异常升高；dd 压测进程持续运行；应用读写延迟增大

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效磁盘 IO 负载注入。

前提条件：容器内需有 `dd` 工具（大多数容器均内置）

注入命令：
```bash
# 关键点：循环用子 shell 后台 + 重定向（否则 exec 挂到 10s 超时）；PID 落盘 + 定时自动 kill。
# 读压力（先造 500MB 源文件再循环读）：
kubectl exec <pod-name> -n <namespace> -- sh -c '
  dd if=/dev/zero of=/chaos_burnio.read bs=1M count=500 2>/dev/null
  ( while :; do dd if=/chaos_burnio.read of=/dev/null bs=1M count=100 2>/dev/null; done ) >/dev/null 2>&1 &
  echo $! > /tmp/chaos_ioread.pid
  ( sleep <duration>; kill $(cat /tmp/chaos_ioread.pid) 2>/dev/null; rm -f /tmp/chaos_ioread.pid /chaos_burnio.read ) >/dev/null 2>&1 &
'
# 写压力：
kubectl exec <pod-name> -n <namespace> -- sh -c '
  ( while :; do dd if=/dev/zero of=/chaos_burnio.write bs=1M count=100 2>/dev/null && rm -f /chaos_burnio.write; done ) >/dev/null 2>&1 &
  echo $! > /tmp/chaos_iowrite.pid
  ( sleep <duration>; kill $(cat /tmp/chaos_iowrite.pid) 2>/dev/null; rm -f /tmp/chaos_iowrite.pid /chaos_burnio.write ) >/dev/null 2>&1 &
'
```

恢复命令（从精确到兜底）：
```bash
# 首选：按落盘 PID 精确 kill 并清理文件
kubectl exec <pod-name> -n <namespace> -- sh -c \
  'kill $(cat /tmp/chaos_ioread.pid /tmp/chaos_iowrite.pid) 2>/dev/null; rm -f /tmp/chaos_ioread.pid /tmp/chaos_iowrite.pid /chaos_burnio.read /chaos_burnio.write'
# 兜底：ps+kill（比 pkill 通用）
kubectl exec <pod-name> -n <namespace> -- sh -c \
  "ps -o pid,args 2>/dev/null | grep '[d]d if=' | awk '{print \$1}' | xargs -r kill -9"
```

注意事项：
- 无法精确控制 IO 带宽比例，只能尽量打满 IO
- 循环命令必须用子 shell 后台 + 重定向 `>/dev/null 2>&1`，否则占住 exec 输出管道导致 `kubectl exec` 挂起到 10s 超时
- 自动恢复基于 PID 文件 + 定时 kill，可靠；切勿用 `$(jobs -p)` 定时自杀（脱离子 shell 取不到 PID）
- 写压力循环会反复创建和删除文件，读压力会预置 500MB 源文件，注意确保分区有足够空间
