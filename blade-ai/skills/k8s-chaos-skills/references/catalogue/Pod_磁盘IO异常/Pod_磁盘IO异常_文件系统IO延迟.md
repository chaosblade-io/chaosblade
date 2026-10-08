**⚠️ 注意：此场景为 kubectl-native 方案。选用前提是 ChaosBlade 没有 pod-IO target（以 `blade create k8s --help` 探测为准；若本地版本提供 `pod-IO delay`，优先用它），需通过 kubectl exec + tc（块设备级）或 blade pod-disk burn（IO 饱和）实现近似效果。**

**2026-09-22 实测注记（#63）：本环境本地 blade CLI 无 pod-IO target；且目标集群 ChaosBlade Operator 不可用（chaosblade namespace 零资源、operator Pod 长期 Init:ImagePullBackOff）——k8s scope 的 blade 手段（含 pod-disk burn）均不可用，实测走手段2 kubectl-native。**

**用例名称** 文件系统IO延迟 导致 Pod_磁盘IO异常

## 故障现象
1. 应用读写操作耗时明显增加，响应延迟上升
2. 数据库慢查询增多，出现查询超时
3. 文件系统操作阻塞导致请求处理变慢
4. 应用吞吐量下降，P99 延迟显著升高

## 资源准备
1. 确认应用 A 已正常运行，且有活跃的磁盘读写操作
2. 确认目标 Pod 内的写入路径可写（`<data-path>` = 靶的专属数据盘挂载路径，本靶实测 `/var/lib/data`，优先使用——爆炸半径零外部；无专属盘时用空串即容器根 `/`——注意根盘 IO 与同节点其他 Pod 共享底层设备）：`kubectl exec <pod-name> -n <namespace> -- df -h / <data-path>`
3. 确认监控系统可观测应用延迟指标
4. 确认容器内有 `dd` 工具并探测其实现（用于验证 IO 性能）：`kubectl exec <pod-name> -n <namespace> -- dd --version 2>&1 | head -1`——`oflag=direct` 型载荷仅 coreutils dd 支持（本靶 coreutils 8.32 实测支持）；busybox dd 仅支持 `conv=fsync`

## 演练步骤
1. 定位应用 A 的 Pod，确认根文件系统可写（&& 必须包在 sh -c 载荷内——命令是 argv 直传无 shell，
   裸 touch 形态下 `&&`/`rm`/`-f` 会沦为 touch 的字面参数，在 / 下创建同名垃圾文件且 rm 永不执行）：
   ```bash
   # 写入路径优先用数据盘（本靶 /var/lib/data）；无数据盘时把路径换成 /
   kubectl exec <pod-name> -n <namespace> -- sh -c 'touch /.iobench.tmp && rm -f /.iobench.tmp'
   ```
2. 使用 `blade create k8s pod-disk burn` 对目标 Pod 注入持续高 IO 负载，间接制造 IO 延迟：
   ```bash
   blade create k8s pod-disk burn \
     --read --write \
     --path / \
     --size <size> \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --timeout <duration>

   ```
   - `--read --write`：同时制造读写 IO 负载
   - `--path`：必须使用 `/`（容器根文件系统）。不要使用 EmptyDir、hostPath 等子目录挂载路径，这些路径在 ChaosBlade nsexec 模式下校验会失败（注：此限制仅限手段1 的 nsexec 校验；手段2 kubectl-native 载荷不受此限，可写靶的数据盘路径）
   - `--size`：每次写入块大小（MB），默认 10，增大可加剧 IO 竞争
   - 原理：通过持续大量读写 IO 操作使磁盘 IO 队列饱和，间接导致应用的 IO 请求排队等待，表现为 IO 延迟显著增加
3. 记录返回的 experiment_uid，用于后续恢复

## 注入验证
1. 在 Pod 内执行写入操作，确认耗时明显增加（`conv=fsync` 写完强制落盘，busybox 与 coreutils 公共可用；
   不要用 `oflag=dsync`——busybox dd 不支持任何 oflag，会报 `unrecognized option`）：
   ```bash
   # 与注入载荷同盘（本靶 /var/lib/data）；至少采样 3 次——IO 竞争延迟非确定性波动
   kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=<data-path>/.iolatency.tmp bs=1M count=10 conv=fsync
   ```
2. 对比注入前后写入耗时（注入后因 IO 队列饱和，写入吞吐显著下降）。2026-09-22 实测锚点（靶 drill-sts-pvc-target-0 的 /var/lib/data，ext4 云盘）：基线 10MB fsync 写 ≈ 0.010s（约 1GB/s）；单路文档载荷运行 8s 后判据耗时 0.098s → 0.098s → 0.330s（10x–33x 上升，非确定性），判定阈值建议 ≥5x
3. 查看应用日志，确认出现 slow query 或 timeout 相关告警
4. 确认应用请求延迟 P99 显著上升
5. 查看 Pod 内 IO 等待情况：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- cat /proc/diskstats
   ```

## 注入恢复
1. 销毁 blade 实验：`blade destroy <experiment_uid>`
2. 或等待 `--timeout`（`<duration>`）到期后自动恢复
3. 若应用存在连接池超时，可能需等待连接回收或重启 Pod

## 恢复验证
1. kill 后先 `sleep 2–5s`（等单轮 dd 残影退场）再重新执行写入操作，确认耗时恢复正常（同样用 `conv=fsync`，不要用 busybox 不支持的 `oflag=dsync`）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c 'sleep 3; dd if=/dev/zero of=<data-path>/.iolatency.tmp bs=1M count=10 conv=fsync'
   ```
2. 查看应用日志，确认 slow query 和 timeout 告警消失
3. 确认应用请求延迟 P99 恢复到基线水平

## 基准事实
- **根因**：通过 pod-disk burn 使磁盘 IO 队列饱和，应用的正常 IO 请求需排队等待，表现为 IO 操作延迟显著增加
- **必现现象**：Pod 内文件读写耗时显著增加；磁盘 IO 利用率接近 100%；应用出现慢查询或超时；请求延迟 P99 升高
- **方案说明**：此为 blade pod-disk burn 近似方案（选用前提：无 pod-IO target，以 `--help` 探测为准）。与精确 IO 延迟注入（每次 IO 固定增加 Nms）不同，burn 方案通过 IO 竞争间接制造延迟，效果为非确定性延迟增加而非固定值注入
- **2026-09-22 实测（#63）**：靶 drill-sts-pvc-target-0（/var/lib/data，ext4 云盘，coreutils dd 8.32）：基线 10MB fsync 写 ≈ 10ms；单路载荷（bs=1M count=100 oflag=direct 写读交替）8s 后判据 98ms→330ms（10x–33x）；$! PID 落盘 + 定时 kill 恢复幂等、kill 后零残留

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效 IO 负载注入。

前提条件：容器内需有 `dd` 工具；容器文件系统可写

注入命令：
```bash
# 通过 kubectl exec 在 Pod 内持续制造 IO 负载（读写同时）
# 关键点：子 shell 后台 + 重定向（否则 exec 挂到 10s 超时）；PID 落盘 + 定时自动 kill。
# 路径：优先靶的专属数据盘（本靶实测 /var/lib/data，爆炸半径零外部）；无数据盘时用 /（与同节点 Pod 共享底层设备）
# $! 机理已实测（2026-09-22）：$! 捕获真实 PID、kill 精确可达、kill 后零残留（进程树核实）
kubectl exec <pod-name> -n <namespace> -- sh -c '
  ( while :; do dd if=/dev/zero of=<data-path>/.iocache.dat bs=1M count=100 oflag=direct 2>/dev/null; dd if=<data-path>/.iocache.dat of=/dev/null bs=1M 2>/dev/null; done ) >/dev/null 2>&1 &
  echo $! > /tmp/iostat-sampler.pid
  ( sleep <recovery-seconds>; kill $(cat /tmp/iostat-sampler.pid) 2>/dev/null; rm -f /tmp/iostat-sampler.pid <data-path>/.iocache.dat ) >/dev/null 2>&1 &
'
```
倒计时从武装时刻起算：IO 负载启动与定时器武装在同一 sh -c 载荷内原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `kubectl exec <pod-name> -n <namespace> -- sh -c 'pkill -f "iostat-sampler.pi[d]"; true'` 一并停掉故障与旧定时器（后台 subshell 共享载荷 cmdline，此杀同时命中定时器与 IO 循环，即全停语义），再重跑上方注入命令原子重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）

`<recovery-seconds>`：安全网窗总时长（秒），取 prompt 下发的 `recovery_timer_seconds`
（= duration + grace，见 SKILL.md 双数窗口契约）——定时 kill 的 sleep 定时器以它武装，
让框架在观察窗终点主动派发的恢复先于自治到期落地；上方 blade 形态的 `--timeout` 由
引擎在派发前按同一单源钉定，文档占位符保持 `<duration>` 不动

恢复命令（从精确到兜底）：
```bash
# 首选：按落盘 PID 精确 kill 并清理文件
kubectl exec <pod-name> -n <namespace> -- sh -c \
  'kill $(cat /tmp/iostat-sampler.pid) 2>/dev/null; rm -f /tmp/iostat-sampler.pid /.iocache.dat'
# 兜底：ps+kill（比 pkill 通用）——清理动作必须拆为独立 exec：若与 grep 同载荷，rm 行的裸文件名
# 会被主进程 sh -c 的 cmdline 携带、被 grep 正则命中导致自杀（实测同族证据）
kubectl exec <pod-name> -n <namespace> -- sh -c \
  "ps -o pid,args 2>/dev/null | grep '[i]ocache.dat' | awk '{print \$1}' | xargs -r kill -9"
kubectl exec <pod-name> -n <namespace> -- sh -c 'rm -f /.iocache.dat /tmp/iostat-sampler.pid'
# 恢复语义：kill 终止的是循环持有者（后台子 shell）——正在执行的单轮 dd 会跑完自然退出（<1s）；
# 恢复验证请在 kill 后 sleep 2–5s 再采样（见恢复验证）
```

注意事项：
- `oflag=direct` 绕过页缓存，确保 IO 负载直接作用于磁盘
- 循环命令必须用子 shell 后台 + 重定向 `>/dev/null 2>&1`，否则占住 exec 输出管道导致 `kubectl exec` 挂起到 10s 超时
- 自动恢复基于 PID 文件 + 定时 kill，可靠；切勿用 `$(jobs -p)` 定时自杀（脱离子 shell 取不到 PID）
- 如容器无 dd 工具，可用 `cat /dev/urandom > /.iocache.dat` 替代（但无法控制块大小）
- 同载荷内 `ps|grep` 匹配/清理进程时：模式用 `[x]` 方括号防 grep 自身自匹配；且载荷其他位置的裸文件名会随主进程 `sh -c` cmdline 被携带命中（自杀风险）——匹配与 rm 清理务必拆到不同 exec（见恢复兜底示例）
