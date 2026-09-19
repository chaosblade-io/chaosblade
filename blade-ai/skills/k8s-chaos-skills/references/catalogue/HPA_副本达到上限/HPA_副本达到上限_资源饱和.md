**用例名称** 资源饱和 导致 HPA_副本达到上限

**故障现象**：
1. HPA 的当前副本数达到 maxReplicas 上限，无法继续扩容
2. 应用 CPU 或内存使用率仍持续高于 HPA 目标阈值
3. 应用响应延迟增大，出现超时

**资源准备**：
1. 确认应用 A 已正常运行，且已配置 HPA。**集群内无可破坏 HPA 时，由运维带外
   预置常驻演练靶**（drill-hpa-target Deployment + 同名 HPA，同 drill-pvc-target /
   drill-ds-target 常驻靶模式——HPA 与 Deployment 都是 workload 系 kind，manifest
   通道为守卫机制级禁令，**Agent 只注入既有工作负载，勿在执行计划里创建**；常驻靶
   跨演练复用，演练结束不删除）。**靶标设计三定案**：
   - **小 requests 反稀释**（核心算术）：容器 requests cpu=10m 时，单核 shell 循环
     （1000m）= 100×requests——扩容出的新 Pod 无负载（0m）被注入 Pod 恒 100×requests
     摊薄后平均利用率仍远超目标阈值（如 3 副本时平均 >3000% vs 目标 60%），desired
     计算恒超 maxReplicas，**单轮注入存量 Pod 即可达 ScalingLimited 稳态，无需追注
     扩容出的新 Pod**（若 requests 常规大小，扩容稀释会使平均回落到阈值以下、
     扩容提前停止——这是本故障能否成立的设计分水岭）
   - **maxReplicas 距初始副本近**（1→3）：减少扩容轮数与资源占用
   - **behavior 缩容稳定窗设短**（scaleDown stabilizationWindowSeconds=60，默认 300
     会使恢复验证等待 5 分钟以上；scaleUp 稳定窗 0 加快扩容收敛）
2. **指标管道前置检查**：HPA 依赖 metrics-server——`kubectl get hpa` 的 TARGETS 列
   显示 `<unknown>` 即指标管道断（此时注入压力不会触发扩容，属环境前置不满足）；
   显示实时百分比即就绪
3. 确认监控系统可观测 HPA 状态和 Pod CPU/内存指标

**演练步骤**（kubectl-native 主路径；chaosblade operator 不可用的集群走此路径，
可用时 blade pod-cpu fullload 等效）：
1. 定位演练靶 HPA，记录 maxReplicas 配置与基线 REPLICAS
2. 对应用 A 的**全部存量 Pod** 注入 CPU 压力（单核 shell 循环即足够——见资源准备
   小 requests 算术；载荷形态与自恢复定时器按下方手段 2 模板，与武装原子紧邻）
3. 观察 HPA 扩容行为（15s/轮同步逐步逼近，1→3 约 3 轮 ≈ 45-60s），等待副本数
   达到 maxReplicas 上限

**注入验证**：
1. 执行 `kubectl get hpa`，确认 REPLICAS 已达到 MAXPODS 上限——扩到上限需多轮同步周期逐步逼近（HPA 同步默认 15s/轮，1→3 约 45-60s），首查未达上限不构成反证，无需反复轮询等它到顶
2. `kubectl describe hpa <hpa-name> -n <namespace>` 确认 Conditions 中 `ScalingLimited: True`（reason: TooManyReplicas，message: the desired replica count is more than the maximum replica count）——这是达到上限的权威信号。**不要在 Events 里找告警**：desiredReplicas 超过 max 时直接被 clamp 到上限、扩容静默跳过，不会发出告警 Event；`FailedGetScale` 是获取 scale 子资源失败的错误（scaleTargetRef 问题），与达到上限无关，勿作为判据。Events 中可见的是逐级扩容的 `SuccessfulRescale` 记录
3. `kubectl top pod -n <namespace> -l <label-selector>` 确认 CPU 使用率仍高于目标阈值（小 requests 形态下注入 Pod 应接近满核、TARGETS 平均百分比远超目标——平均数值本身即反稀释定案成立的直接回显）
4. （可选，仅当演练方提供了应用访问入口时）确认请求延迟增大或超时；无入口时上述 HPA 与 CPU 证据成立即可判定

**注入恢复**：
1. 停掉全部 Pod 的 CPU 负载（按手段 2 恢复命令——kill PID 文件内进程）
2. 等待 HPA 自动缩容（**缩容时序预算**：behavior scaleDown 稳定窗 60s + 同步周期
   ≈ 90-150s 内回落；期间 CPU 已归零而 REPLICAS 尚未回落不构成恢复失败——HPA
   缩容按设计滞后于负载消失）
3. **常驻靶不删除**（跨 case 复用资产，无拆线动作；HPA 与 Deployment 均保留）

**恢复验证**：
1. 执行 `kubectl get hpa`，确认 REPLICAS 回落至基线（minReplicas 或注入前值——
   给足缩容稳定窗时序预算，见注入恢复第 2 条）
2. `kubectl top pod -n <namespace> -l <label-selector>` 确认 CPU 使用率恢复到基线（无负载即 ~0m）
3. （可选，有访问入口时）确认请求延迟恢复正常
4. 常驻靶形态验证独立性：HPA 与 Deployment 跨演练存续，判据恢复完成后随时可独立复核

**基准事实**：
- **根因**：应用负载超过 HPA 的 maxReplicas 能覆盖的处理能力，HPA 达到扩容上限后无法继续扩容，导致服务资源饱和
- **必现现象**：HPA REPLICAS 达到 MAXPODS；Conditions 显示 ScalingLimited=True（TooManyReplicas）；CPU 使用率持续超过目标阈值；应用性能下降
- **扩容稀释算术**：HPA desired = ceil(当前副本 × 平均利用率 / 目标)——扩容出的新 Pod 不带注入负载会稀释平均；小 requests 设计使注入 Pod 利用率达 100×requests，稀释后平均仍数倍于目标，desired 恒超 max 被 clamp（扩容停止但 ScalingLimited 置位——「上限」与「停止」的机制区分）

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效 CPU 压力触发 HPA 扩容。

前提条件：容器内需包含 `stress-ng` 或支持 `dd`/shell 循环

注入命令：
```bash
# 0) 先列出目标 Pod —— 下面每条注入命令对【每一个】Pod 各执行一次。
#    不要写 shell for 循环：执行层按 argv 下发，没有 shell 来展开它。
kubectl get pods -n <namespace> -l <label-selector> -o name

# 方式一：容器内有 stress-ng（后台+重定向让 exec 立即返回，--timeout 自带自动恢复）
kubectl exec <pod-name> -n <namespace> -- sh -c 'stress-ng --cpu 0 --cpu-load <percent> --timeout <duration>s >/dev/null 2>&1 &'

# 方式二：容器无 stress-ng，用 shell 循环。
# 关键点：① 每个循环重定向到 /dev/null（否则 exec 会挂到 10s 超时）；
# ② PID 落盘 + 按文件定时 kill 实现可靠自动恢复；③ 单核循环，多核需起多个（N）；
# ④ 计数用 while 自增而非 $(seq)——busybox 1.33 无 seq applet（exit 127），
#    $(seq 1 N) 展开为空会使 for 空转零注入（静默失败）。
# 同样对每个 Pod 各执行一次。sh -c 的载荷整体是一个参数，内部的 for/while/&
# 由容器内的 sh 解释，不需要外层 shell。
kubectl exec <pod-name> -n <namespace> -- sh -c ': > /tmp/loadgen-worker.pids; i=1; while [ $i -le <N> ]; do ( while :; do :; done ) >/dev/null 2>&1 & echo $! >> /tmp/loadgen-worker.pids; i=$((i+1)); done; ( sleep <duration>; kill $(cat /tmp/loadgen-worker.pids) 2>/dev/null; rm -f /tmp/loadgen-worker.pids ) >/dev/null 2>&1 &'
```
倒计时从武装时刻起算：负载发生器启动与定时器武装在同一 sh -c 载荷内原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `kubectl exec <pod-name> -n <namespace> -- sh -c 'pkill -f "loadgen-worker.pid[s]"; true'` 一并停掉故障与旧定时器（后台 subshell 共享载荷 cmdline，此杀同时命中定时器与负载循环，即全停语义），再重跑上方注入命令原子重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）

恢复命令（从精确到兜底）：
```bash
# 对每个 Pod 各执行一次。
# 首选：kill stress-ng + 按 PID 文件 kill shell 循环（只杀 stress-ng 会漏掉循环）
kubectl exec <pod-name> -n <namespace> -- sh -c 'pkill stress-ng 2>/dev/null; kill $(cat /tmp/loadgen-worker.pids) 2>/dev/null; rm -f /tmp/loadgen-worker.pids'

# 兜底：按命令特征 kill（ps+kill 比 pkill 通用，精简镜像常无 pkill）
kubectl exec <pod-name> -n <namespace> -- sh -c "ps -o pid,args 2>/dev/null | grep -E '[w]hile :|[s]tress-ng' | awk '{print \$1}' | xargs -r kill -9"

# 全部 Pod 处理完后，等待 HPA 自动缩容（cooldown period 后）
```

注意事项：
- 需对所有目标 Pod 逐个注入 CPU 压力，否则 HPA 可能不会触发扩容（常驻靶形态下存量 Pod 即全部——扩容出的新 Pod 无需追注，见资源准备小 requests 反稀释定案）
- **勿重复武装（载荷单次性）**：`kubectl exec` 落位（立即返回的空回执）即武装完成——再次下发同一载荷会截断 `/tmp/loadgen-worker.pids`（`: >` 开头清文件），第一轮循环 PID 失联成孤儿（timer kill 不到）。武装前若不确定是否已落位，先只读探针 `kubectl exec <pod> -- cat /tmp/loadgen-worker.pids`：文件在 = 已武装，跳过
- `stress-ng --timeout` 提供自动超时，配合后台+重定向让 exec 立即返回，建议始终指定
- shell 循环命令必须重定向 `>/dev/null 2>&1`，否则会占住 exec 输出管道导致 `kubectl exec` 挂起到 10s 超时
- shell 循环单核，需按 CPU 上限起 N 个循环逼近目标；自动恢复基于 PID 文件 + 定时 kill，切勿用 `$(jobs -p)`（脱离子 shell 取不到 PID 会失效）
- 小 requests 形态（requests cpu=10m 量级）下单循环（N=1）即足以触发满核压力——多循环无必要
