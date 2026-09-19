**用例名称** 磁盘IO读写负载 导致 Pod_磁盘IO过高

**故障定位**：负载型故障——IO 压测进程存活即故障存活，进程终止（实验销毁/定时器
kill）即 IO 吞吐回落、自动恢复。手段1（ChaosBlade `pod-disk burn`）与手段2
（kubectl-native：容器内 `dd conv=fsync` 循环）是**并列的注入手段**，底层效果
等价（blade 内部同样是容器内 dd 循环），按环境能力选用。**本用例的两个核心
结构性约束**：
1. **IO 落点与爆炸半径**——`--path /` 打的是容器根文件系统（节点根盘
   overlay），磁盘 IO 带宽是**节点级共享资源**：打满即同节点全部 Pod 的读写
   延迟上升。爆炸半径天然外溢到同节点（与 diskfill 的空间占用型不同：IO
   争抢**不触发** DiskPressure/驱逐——Evicted/重启属于预期阴性，出现即失控）。
2. **判据与页缓存**——效果主证是 `/proc/diskstats` 增量，但页缓存陷阱已确证
   确证：不落盘的写（写后立删）与热读（读刚写入的文件）增量近乎为零；
   可靠形态是 `conv=fsync` 持续落盘（739KB/s vs 底噪 1KB/s）。
`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

**故障现象**：
1. 目标盘 IO 吞吐异常升高（容器内 `/proc/diskstats` 双采样差分显著超底噪）
2. 应用读写延迟明显增加，请求处理变慢
3. 同节点其他 Pod 受 IO 带宽争抢影响，延迟上升（爆炸半径内预期现象）
4. （有应用访问入口时）应用出现读写超时或性能退化

**资源准备**：
1. 确认目标 Pod 的标签选择器、命名空间、实际容器名，以及根文件系统可写：
   ```bash
   kubectl exec <pod> -n <namespace> -c <container> -- sh -c 'touch /.iobench.tmp && rm -f /.iobench.tmp'
   ```
   （`&&` 必须包在 sh -c 载荷内——命令是 argv 直传无 shell，裸 touch 形态下
   `&&`/`rm`/`-f` 会沦为 touch 的字面参数，在 / 下创建同名垃圾文件且 rm 永不执行。
   容器 `readOnlyRootFilesystem: true` 时本用例两个手段都不可用，判死）
2. **能力探测（决定手段选择）**——确认 ChaosBlade operator 实际健康，以当次探测为准：
   ```bash
   blade create k8s pod-disk -h
   kubectl get pods -A --no-headers | grep -i chaosblade-operator
   ```
   - operator Pod Running → 手段1 可用（实验 UID 统一生命周期管理）
   - operator Init:ImagePullBackOff/CrashLoopBackOff（常见形态）→ 手段1
     判死，用手段2，不要在手段1 上空转
3. **爆炸半径基线（必做，恢复验证对照用）**：
   ```bash
   kubectl get node <node-name> -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}'
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name> --no-headers
   ```
   记录：节点 Conditions（关注 DiskPressure）、同节点 Pod 清单与各自 RESTARTS
   ——IO 争抢的预期影响是延迟上升，不是 Pod 故障；任何 Evicted/重启漂移即
   爆炸半径失控
4. **容器工具探测（手段2 前提）**：
   ```bash
   kubectl exec <pod> -n <namespace> -c <container> -- sh -c 'command -v dd; echo PROBE_DONE'
   ```
   有 dd → 手段2 可用（busybox dd 亦可，见注意事项等号判据）；distroless/
   scratch 镜像常态无 dd → 手段2 判死，只能依赖手段1（operator 健康时）
5. **diskstats 基线采样**（效果主证的对照零点）：
   ```bash
   kubectl exec <pod> -n <namespace> -c <container> -- cat /proc/diskstats
   ```
   容器内 `/proc/diskstats` 是**节点级视角**（/proc 未_namespaced 隔离），而
   burn 的效果本来就是节点根盘 IO 饱和——视角与效果对齐，此判据成立；采样
   记录各行计数，注入后差分看哪行增量爆发（该行即容器 overlay 的 backing
   设备；可由节点侧 `lsblk` 或 `df` 交叉核对设备名）

**演练步骤**：
1. 记录注入前基线：资源准备第 3/5 条的节点条件、同节点 Pod 清单、diskstats
   快照；`kubectl get pods -n <namespace> -l <label-selector> -o wide` 记录
   目标 Pod RESTARTS

**手段1（ChaosBlade）** —— 前提：资源准备第 2 条探测通过（operator 健康）

2. 注入磁盘 IO 读写负载：
   ```bash
   blade create k8s pod-disk burn \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --path / \
     --read \
     --write \
     --size <size> \
     --timeout <duration>
   ```
   - `--path` 必须使用 `/`（容器根文件系统，overlay 挂载）。不要使用
     EmptyDir、hostPath 等子目录挂载路径——这些路径在 ChaosBlade nsexec
     模式下校验会失败
   - `--size`：单轮读写数据量（MB），循环覆写不累积，空间占用有界
3. 记录返回的 experiment_uid，用于后续恢复

**手段2（kubectl-native dd 循环）** —— 前提：资源准备第 4 条 dd 探测通过

注入命令（**先武装定时器再启动循环**——循环启动后若武装失败，故障将无人
回收；IO 负载启动与定时器武装在同一 sh -c 载荷内原子紧邻，无侵蚀间隙）：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c '
  ( sleep <duration>; kill $(cat /tmp/iobench-writer.pid) 2>/dev/null; rm -f /tmp/iobench-writer.pid /.iobench.write.dat; echo IOBENCH_RESTORED >> /tmp/iobench.evd ) >/dev/null 2>&1 &
  ( while :; do dd if=/dev/zero of=/.iobench.write.dat bs=1M count=50 conv=fsync 2>/dev/null; done ) >/dev/null 2>&1 &
  echo $! > /tmp/iobench-writer.pid
  echo IOBENCH_INJECTED >> /tmp/iobench.evd
'
```
- `conv=fsync` 是判据成立的关键——每轮强制落盘（页缓存不落盘时
  diskstats 零增量，判据双失效）；busybox dd 不支持 `oflag/iflag=direct`
- 循环必须用子 shell 后台 + 重定向 `>/dev/null 2>&1`，否则占住 exec 输出
  管道导致 `kubectl exec` 挂起到 10s 超时
- `$!` 取的是循环 subshell 的 PID（定时器在前、循环在后）；自动恢复基于
  PID 落盘 + 定时 kill，可靠；切勿用 `$(jobs -p)` 定时自杀（脱离子 shell
  取不到 PID）
- 每轮覆写同一 50MB 文件，空间占用有界（不会累积）
- 自证链：`IOBENCH_INJECTED`（注入时刻）与 `IOBENCH_RESTORED`（定时器
  还原后）落盘 `/tmp/iobench.evd`，验证阶段 `cat` 取证，不受故障窗口是否
  已关闭的时序约束
- 手段2的读压力（可选，受页缓存限制见注意事项）：
  ```bash
  kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c '
    dd if=/dev/zero of=/.iobench.read.dat bs=1M count=500 2>/dev/null
    ( while :; do dd if=/.iobench.read.dat of=/dev/null bs=1M count=100 2>/dev/null; done ) >/dev/null 2>&1 &
    echo $! > /tmp/iobench-reader.pid
    ( sleep <duration>; kill $(cat /tmp/iobench-reader.pid) 2>/dev/null; rm -f /tmp/iobench-reader.pid /.iobench.read.dat ) >/dev/null 2>&1 &
  '
  ```
  读热文件全命中页缓存，diskstats 增量近乎为零——读方向只在与写方向叠加时
  作辅助观察，不单独作判据

手段2 倒计时从武装时刻起算；武装后发生任何修复须先全停再全额重武装：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
  'pkill -f "iobench-.+\.pi[d]" 2>/dev/null; true'
```
（后台 subshell 共享载荷 cmdline，此杀同时命中定时器与读/写循环，即全停
语义；模式末字符加 `[]` 防自杀。容器无 pkill 时用兜底：`ps -o pid,args |
grep "[d]d if" | awk "{print \$1}" | xargs -r kill -9`——注意 awk 的 `$1`
在双引号载荷内须转义为 `\$1`）

**注入验证**（两种手段共用；diskstats/cat/ps 判据全部只读，verify 阶段
read-only 纪律天然放行）：
1. **效果主证——diskstats 增量**：间隔 3-5 秒采样两次差分：
   ```bash
   kubectl exec <pod> -n <namespace> -c <container> -- cat /proc/diskstats
   ```
   backing 设备行的写入扇区数/写入次数增量显著超基线底噪（conv=fsync
   形态 739KB/s vs 底噪 1KB/s，量级差三个数量级，判据无歧义）
2. **白盒佐证——压测进程存活**：
   ```bash
   kubectl exec <pod> -n <namespace> -c <container> -- sh -c 'ps -o pid,args | grep "[d]d if"'
   ```
   （管道必须包在 sh -c 载荷内——裸 ps 形态下 `|`/`grep` 沦为 ps 字面参数，
   过滤静默失效。判据**不含等号**——busybox dd 解析 argv 时把 `=` 原地改写
   为 NUL，ps 显示 `dd if /dev/zero` 无等号（busybox:1.33 源码确证；
   GNU dd 保留等号，无等号判据 `[d]d if` 两种形态都匹配）；`[d]d` 括号技巧
   只排除 grep 自身，手段2的 sh -c 载体行含 `dd if` 字样仍会被匹配——
   载体在即注入在，属预期。手段1 的 burn 进程同样以 dd 形态呈现）
3. **爆炸半径交叉确认**：
   ```bash
   kubectl get node <node-name> -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}'
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name> --no-headers
   ```
   节点 DiskPressure 仍 False；同节点 Pod 与基线清单一致（无 Evicted、无
   异常重启——IO 争抢的预期影响是延迟，不是 Pod 故障）
4. **预期阴性声明**：以下现象**不出现且不作为失败证据**——目标 Pod 被驱逐、
   节点 DiskPressure=True、Pod 重启/RESTARTS 漂移、应用 OOMKilled。出现
   任一项 = 爆炸半径失控，立即按恢复命令处置并如实上报
5. **持续性检查（必做）**——负载型故障，进程在即故障在：45s 后复查
   diskstats 增量仍在高位、`ps` 佐证 dd 进程仍存活

**注入恢复**：
1. 手段1：`blade destroy <experiment_uid>`，burn 进程随实验销毁终止，临时
   文件自动清理
2. 手段2：定时器到期自动 kill + 清理 + 写 `IOBENCH_RESTORED`（主恢复路径）；
   提前恢复（幂等）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'kill $(cat /tmp/iobench-writer.pid /tmp/iobench-reader.pid) 2>/dev/null; rm -f /tmp/iobench-writer.pid /tmp/iobench-reader.pid /.iobench.write.dat /.iobench.read.dat; echo IOBENCH_RESTORED >> /tmp/iobench.evd; true'
   ```
   （或用上方全停 pkill 后手动清理）
3. kill 循环 subshell 后，至多残余一个正在本轮 fsync 的 dd（该速率下单轮
   50MB 最长约 70s），本轮落盘完成即自行退出——恢复验证的 dd 进程判据须在
   定时器触发 90s 后观察，或以 diskstats 回落为准

**恢复验证**：
1. **效果主证——IO 吞吐回落基线**：diskstats 双采样差分回落到底噪水平
   （与资源准备第 5 条基线对照）
2. 压测进程已终止：`ps -o pid,args | grep "[d]d if"` 无输出（残余单轮滞后
   见注入恢复第 3 条）；手段2 证据文件含 `IOBENCH_RESTORED`（双标记齐备 =
   注入与还原全程自证）
3. 节点健康：DiskPressure 仍 False；同节点 Pod 与基线清单一致（无
   Evicted/重启漂移——爆炸半径全程受控的最终证据）
4. Pod 状态 Running、RESTARTS 与基线一致；（有访问入口时）应用读写延迟
   恢复正常

**基准事实**：
- **根因**：Pod 内产生大量磁盘读写负载，模拟应用异常 IO 操作或日志洪峰，
  磁盘 IO 带宽被占满，影响同节点正常业务读写
- **必现现象**：节点根盘 IO 吞吐异常升高（diskstats 增量显著超底噪）；
  压测 dd 进程持续运行
- **条件现象**：应用读写超时/延迟告警——仅应用有 IO 敏感路径且流量在场时
  出现；同节点其他 Pod 延迟上升属爆炸半径内预期现象

注意事项：
- **页缓存陷阱（确证）**：写压力若用「写后立删」形态（`dd ... && rm -f`），
  脏页在回写前被丢弃，diskstats 增量近乎为零；读压力读刚写入的热文件时全部
  命中页缓存，增量同样为零。busybox dd 不支持 `oflag/iflag=direct`，可靠
  落盘形态是 `conv=fsync`
- 读压力要产生真实 IO 需读冷数据（源文件远大于页缓存）或放弃读方向、仅用
  写压力作判据
- 无法精确控制 IO 带宽比例，只能尽量打满 IO
- 读压力会预置 500MB 源文件，注意确保分区有足够空间
