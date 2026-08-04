**⚠️ 注意：此场景为 kubectl-native 方案。选用前提是 ChaosBlade 没有 pod-IO target（以 `blade create k8s --help` 实测为准；若本地版本提供 `pod-IO delay`，优先用它），需通过 kubectl exec + tc（块设备级）或 blade pod-disk burn（IO 饱和）实现近似效果。**

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
   kubectl exec <pod-name> -n <namespace> -- touch /.iobench.tmp && rm -f /.iobench.tmp
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
   kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=/.iolatency.tmp bs=1M count=10 oflag=dsync
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
   kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=/.iolatency.tmp bs=1M count=10 oflag=dsync
   ```
2. 查看应用日志，确认 slow query 和 timeout 告警消失
3. 确认应用请求延迟 P99 恢复到基线水平

**基准事实**：
- **根因**：通过 pod-disk burn 使磁盘 IO 队列饱和，应用的正常 IO 请求需排队等待，表现为 IO 操作延迟显著增加
- **必现现象**：Pod 内文件读写耗时显著增加；磁盘 IO 利用率接近 100%；应用出现慢查询或超时；请求延迟 P99 升高
- **方案说明**：此为 blade pod-disk burn 近似方案（选用前提：无 pod-IO target，以 `--help` 实测为准）。与精确 IO 延迟注入（每次 IO 固定增加 Nms）不同，burn 方案通过 IO 竞争间接制造延迟，效果为非确定性延迟增加而非固定值注入

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效 IO 负载注入。

前提条件：容器内需有 `dd` 工具；容器文件系统可写

注入命令：
```bash
# 通过 kubectl exec 在 Pod 内持续制造 IO 负载（读写同时）
# 关键点：子 shell 后台 + 重定向（否则 exec 挂到 10s 超时）；PID 落盘 + 定时自动 kill。
kubectl exec <pod-name> -n <namespace> -- sh -c '
  ( while :; do dd if=/dev/zero of=/.iocache.dat bs=1M count=100 oflag=direct 2>/dev/null; dd if=/.iocache.dat of=/dev/null bs=1M 2>/dev/null; done ) >/dev/null 2>&1 &
  echo $! > /tmp/iostat-sampler.pid
  ( sleep <duration>; kill $(cat /tmp/iostat-sampler.pid) 2>/dev/null; rm -f /tmp/iostat-sampler.pid /.iocache.dat ) >/dev/null 2>&1 &
'
```

恢复命令（从精确到兜底）：
```bash
# 首选：按落盘 PID 精确 kill 并清理文件
kubectl exec <pod-name> -n <namespace> -- sh -c \
  'kill $(cat /tmp/iostat-sampler.pid) 2>/dev/null; rm -f /tmp/iostat-sampler.pid /.iocache.dat'
# 兜底：ps+kill（比 pkill 通用）
kubectl exec <pod-name> -n <namespace> -- sh -c \
  "ps -o pid,args 2>/dev/null | grep '[i]ocache.dat' | awk '{print \$1}' | xargs -r kill -9; rm -f /.iocache.dat"
```

注意事项：
- `oflag=direct` 绕过页缓存，确保 IO 负载直接作用于磁盘
- 循环命令必须用子 shell 后台 + 重定向 `>/dev/null 2>&1`，否则占住 exec 输出管道导致 `kubectl exec` 挂起到 10s 超时
- 自动恢复基于 PID 文件 + 定时 kill，可靠；切勿用 `$(jobs -p)` 定时自杀（脱离子 shell 取不到 PID）
- 如容器无 dd 工具，可用 `cat /dev/urandom > /.iocache.dat` 替代（但无法控制块大小）
