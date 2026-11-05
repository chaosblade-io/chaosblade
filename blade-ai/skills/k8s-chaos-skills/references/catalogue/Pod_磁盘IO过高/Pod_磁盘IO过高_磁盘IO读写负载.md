**用例名称** 磁盘IO读写负载 导致 Pod_磁盘IO过高

## 故障定位
负载型故障——IO 压测进程存活即故障存活，进程终止（实验销毁/定时器
kill）即 IO 吞吐回落、自动恢复。手段1（ChaosBlade `pod-disk burn`）与手段2
（kubectl-native：容器内 `dd conv=fsync` 循环）是**并列的注入手段**，底层效果
等价（blade 内部同样是容器内 dd 循环），按环境能力选用。**本用例的两个核心
结构性约束**：
1. **IO 落点与爆炸半径**——`--path /` 打的是容器根文件系统（overlay），其
   **backing 块设备由该节点 containerd data-root（snapshotter 目录）的挂载位置
   决定，是随节点而变的量、不是固定的「节点根盘」**：生产环境常把 containerd
   data-root 放独立数据盘以隔离容器存储与系统盘，此时 overlay 落数据盘而非根盘
   （同一用例在不同节点可分别落根盘或独立数据盘、结论完全相反，故 backing 设备是
   随节点而变的量、不存在固定设备名）。
   **切勿预设设备名，一律以注入后 diskstats 差分定位 backing 设备**（见资源准备
   第 5 条）。磁盘 IO 带宽是**该 backing 设备级的共享资源**：打满即共享同一
   overlay backing 的同节点全部 Pod 读写延迟上升。爆炸半径天然外溢到同节点
   （与 diskfill 的空间占用型不同：IO 争抢**不触发** DiskPressure/驱逐——故相对
   自身基线**新增的** Evicted/重启才是失控信号；预存异常 Pod 按自身节奏的自重属
   基线内、不在此列，delta 判据见资源准备第 3 条）。
2. **判据与页缓存**——效果主证是 `/proc/diskstats` 增量，但页缓存陷阱已确证：
   不落盘的写（写后立删）与热读（读刚写入的文件）增量近乎为零；
   可靠形态是 `conv=fsync` 持续落盘（落盘写入速率较底噪高出约三个数量级；
   绝对速率随磁盘硬件而变、不预设具体值）。
`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 目标盘 IO 吞吐异常升高（容器内 `/proc/diskstats` 双采样差分显著超底噪）
2. 应用读写延迟明显增加，请求处理变慢
3. 同节点其他 Pod 受 IO 带宽争抢影响，延迟上升（爆炸半径内预期现象）
4. （有应用访问入口时）应用出现读写超时或性能退化

## 资源准备
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
3. **爆炸半径基线（必做，恢复验证对照用）**——生产节点基线**不是干净的**：
   常含预存异常 Pod（CrashLoopBackOff 按自身节奏持续重启、ContainerCreating/
   Terminating 长期卡住）。故爆炸半径判据是**逐 Pod 对照自身基线的 delta、
   不是绝对态**（定案，勿再按「任何重启漂移即失控」的绝对判据推演——那会把
   预存 Pod 的自重误归因为注入；框架 side-effect 检测层本就按 delta 对照并
   过滤注入前重启，文档判据须与之一致）：
   ```bash
   # 节点 Conditions：定点查关注项（节点可能带十余项云厂商 NPD 自定义
   # condition，全量单行 jsonpath 会被输出预算截断→逼出重查）
   kubectl get node <node-name> -o jsonpath='DiskPressure={.status.conditions[?(@.type=="DiskPressure")].status} Ready={.status.conditions[?(@.type=="Ready")].status}{"\n"}'
   # 同节点 Pod 普查：每 Pod 一行紧凑 jsonpath（`-o wide` 在数十 Pod 节点
   # 会截断丢 RESTARTS/状态；且其 RESTARTS 列聚合 initContainer 重启、与主
   # 容器 restartCount 不一致→直读容器级 restartCount 天然绕开聚合陷阱）
   kubectl get pods -A --field-selector spec.nodeName=<node-name> -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}:{.status.phase}:{.status.containerStatuses[0].restartCount}{"\n"}{end}'
   ```
   记录：节点 Conditions（关注 DiskPressure）、同节点 Pod 清单与**各自
   restartCount 基线值**，并**显式锁定预存异常 Pod 的基线态**（计划里点名，
   声明其窗口内按自身节奏的继续漂移属基线内自重、非注入所致，供 verifier
   勿误判）。IO 争抢的预期影响是延迟上升、不是 Pod 故障；**注入新增的**
   Evicted、或相对自身基线的**新增**重启漂移，即爆炸半径失控
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
   burn 的效果就是容器 overlay backing 块设备的 IO 饱和——视角与效果对齐，此
   判据成立；采样记录各行计数，注入后差分看哪行增量爆发（该行即容器 overlay
   的 backing 设备）。**backing 设备随节点而变（containerd data-root 位置决定，
   可能是根盘 vda/sda、也可能是独立数据盘 vdb/sdb），不可预设为根盘**——只信
   差分爆发行；可由节点侧 `lsblk`/`df` 或 overlay upperdir 路径交叉核对设备名

## 演练步骤
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
  ( sleep <recovery-seconds>; kill $(cat /tmp/iobench-writer.pid) 2>/dev/null; rm -f /tmp/iobench-writer.pid /.iobench.write.dat; echo IOBENCH_RESTORED >> /tmp/iobench.evd ) >/dev/null 2>&1 &
  ( while :; do dd if=/dev/zero of=/.iobench.write.dat bs=1M count=50 conv=fsync 2>/dev/null; done ) >/dev/null 2>&1 &
  echo $! > /tmp/iobench-writer.pid
  echo IOBENCH_INJECTED > /tmp/iobench.evd
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
    ( sleep <recovery-seconds>; kill $(cat /tmp/iobench-reader.pid) 2>/dev/null; rm -f /tmp/iobench-reader.pid /.iobench.read.dat ) >/dev/null 2>&1 &
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

`<recovery-seconds>`：安全网窗总时长（秒），取 prompt 下发的 `recovery_timer_seconds`
（= duration + grace，见 SKILL.md 双数窗口契约）——写/读压力两处 sleep 定时器以它
武装，让框架在观察窗终点主动派发的恢复先于自治到期落地；手段1 的 `--timeout` 由
引擎在派发前按同一单源钉定，文档占位符保持 `<duration>` 不动

## 注入验证
（两种手段共用；diskstats/cat/ps 判据全部只读，verify 阶段
read-only 纪律天然放行）：
1. **效果主证——diskstats 增量**：间隔 3-5 秒采样两次差分：
   ```bash
   kubectl exec <pod> -n <namespace> -c <container> -- cat /proc/diskstats
   ```
   backing 设备行的写入扇区数/写入次数增量显著超基线底噪（conv=fsync
   落盘形态的写入速率较底噪高约三个数量级，判据无歧义；绝对速率随磁盘
   硬件而变、不预设具体值）
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
3. **爆炸半径交叉确认**（复用资源准备第 3 条的抗截断 jsonpath 查询）：
   节点 DiskPressure 仍 False；同节点 Pod **逐 Pod 对照自身基线**——无注入
   新增的 Evicted、无相对基线的**新增**重启漂移（预存异常 Pod 按自身节奏的
   继续漂移属基线内自重、非失控；IO 争抢的预期影响是延迟，不是 Pod 故障）
4. **预期阴性声明**：以下现象**不出现且不作为失败证据**——目标 Pod 被驱逐、
   节点 DiskPressure=True、**相对自身基线新增的** Pod 重启/RESTARTS 漂移、
   应用 OOMKilled。出现任一项 = 爆炸半径失控，立即按恢复命令处置并如实上报
   （预存 CrashLoopBackOff 等自重 Pod 的基线内继续漂移**不算**失控——见资源
   准备第 3 条 delta 判据）
5. **持续性检查**——负载型故障，进程在即故障在：45s 后复查
   diskstats 增量仍在高位、`ps` 佐证 dd 进程仍存活

## 注入恢复
1. 手段1：`blade destroy <experiment_uid>`，burn 进程随实验销毁终止，临时
   文件自动清理
2. 手段2：定时器到期自动 kill + 清理 + 写 `IOBENCH_RESTORED`（主恢复路径）；
   提前恢复（幂等）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'kill $(cat /tmp/iobench-writer.pid /tmp/iobench-reader.pid) 2>/dev/null; rm -f /tmp/iobench-writer.pid /tmp/iobench-reader.pid /.iobench.write.dat /.iobench.read.dat; echo IOBENCH_RESTORED >> /tmp/iobench.evd; true'
   ```
   （或用上方全停 pkill 后手动清理）
3. kill 循环 subshell 后，至多残余一个正在本轮 fsync 的 dd（单轮 50MB 落盘
   耗时随磁盘硬件而变、慢盘可达数十秒量级），本轮落盘完成即自行退出——恢复
   验证的 dd 进程判据须在定时器触发后留足单轮落盘余量（如 90s）再观察，或以
   diskstats 回落为准

## 恢复验证
1. **效果主证——IO 吞吐回落基线**：diskstats 双采样差分回落到底噪水平
   （与资源准备第 5 条基线对照）
2. 压测进程已终止：`ps -o pid,args | grep "[d]d if"` 无输出（残余单轮滞后
   见注入恢复第 3 条）；手段2 证据文件含 `IOBENCH_RESTORED`（双标记齐备 =
   注入与还原全程自证）
3. 节点健康：DiskPressure 仍 False；同节点 Pod **逐 Pod 对照自身基线**无注入
   新增的 Evicted/重启漂移（预存自重 Pod 的基线内漂移排除归因——爆炸半径
   全程受控的最终证据）
4. Pod 状态 Running、RESTARTS 与基线一致；（有访问入口时）应用读写延迟
   恢复正常

## 基准事实
- **根因**：Pod 内产生大量磁盘读写负载，模拟应用异常 IO 操作或日志洪峰，
  磁盘 IO 带宽被占满，影响同节点正常业务读写
- **必现现象**：容器 overlay backing 块设备 IO 吞吐异常升高（diskstats 增量
  显著超底噪；backing 设备以注入后差分探测为准、不预设为根盘）；压测 dd 进程持续运行
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
