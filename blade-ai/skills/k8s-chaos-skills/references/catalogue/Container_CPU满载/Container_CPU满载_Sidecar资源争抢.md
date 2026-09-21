**用例名称** Sidecar容器CPU资源争抢 导致 Container_CPU满载

**故障现象**：
1. Pod 内特定 Sidecar 容器（如 istio-proxy、filebeat）CPU 使用率飙升至 100%
2. 主容器可能因 CPU cgroup 共享而出现响应变慢（取决于 limits/requests 配置）
3. Sidecar 提供的辅助功能（流量代理、日志采集等）性能显著下降或超时
4. Pod 整体 CPU 使用率上升，可能触发 HPA 扩容

**资源准备**：
1. 确认目标 Pod 包含多个容器（至少有一个 Sidecar 容器）
2. 确认目标 Pod 所在 namespace、labels 和 Sidecar 容器名称
3. 记录注入前各容器的 CPU 使用基线
4. **多容器靶接线（靶为单容器 Pod 时）**：业务靶常见单容器形态，无现成 sidecar 可注入——用 JSON patch 给靶 Deployment 模板**追加一个带 CPU limit 的 sidecar 容器**（演练资产，演练结束拆线归还）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"add","path":"/spec/template/spec/containers/-","value":{"name":"drill-sidecar","image":"<节点已缓存镜像>","command":["sleep","7200"],"resources":{"limits":{"cpu":"500m"},"requests":{"cpu":"100m"}}}}]'
   kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=120s
   ```
   （镜像选节点缓存镜像（原 entrypoint 不安全时必须以 `command` 覆盖为 sleep）；**sidecar 必须配 CPU limit**——无 limit 的满载会吃节点 CPU 造成集群级爆炸半径，limit 把争抢框定在容器 cgroup 内，throttling 判据（nr_throttled）也依赖 quota 存在；拆线用 `kubectl patch --type='json' -p='[{"op":"remove","path":"/spec/template/spec/containers/<sidecar索引>"}]'` 后 rollout status 等收敛）
5. **手段前置探测**：ChaosBlade operator 就绪性以当次探测为准（`kubectl get pods -n chaosblade`）——operator 未就绪时手段1（container-cpu fullload）判死，直接转手段2（kubectl-native）；metrics-server 就绪性同样当次探测（`kubectl top nodes`）——`kubectl top pod --containers` 判据依赖它

**演练步骤**：

> **爆炸半径分类（定案）**：`target-only`——满载被 sidecar 的 CPU limit 框定在其自身 cgroup 内（无 limit 才会升级为节点级争抢，本用例禁止），变更面 = 靶 Pod 内进程操作 + 靶 Deployment 模板（接线/拆线），不触及任何非靶资源。

1. 确认 Pod 内容器列表，获取 Sidecar 容器名称：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].name}'
   ```
2. 记录注入前各容器 CPU 使用情况：
   ```bash
   kubectl top pod <pod-name> -n <namespace> --containers
   ```
3. 使用 ChaosBlade 对 Sidecar 容器注入 CPU 满载故障：
   ```bash
   blade create k8s container-cpu fullload \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --container-names <sidecar-container-name> \
     --cpu-percent 100 \
     --timeout <duration>

   ```
4. 观察 Sidecar 容器 CPU 飙升后对主容器和整体服务的影响

**注入验证**：
1. 执行 `kubectl top pod <pod-name> -n <namespace> --containers`，确认目标 Sidecar 容器 CPU 接近 100%
2. 确认主容器 CPU 和响应时间是否受到影响（取决于是否配置了 CPU limits）
3. 验证 Sidecar 提供的服务是否降级（如代理转发延迟增加、日志采集中断）
4. 确认 CPU throttling 直接证据：`kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- cat /sys/fs/cgroup/cpu.stat`（cgroup v2；v1 路径为 `/sys/fs/cgroup/cpu/cpu.stat`，版本探测与字段详见「Pod_CPU_Throttling_limits.cpu过低」），确认 `nr_throttled` 高于注入前基线（单调递增计数器，高于基线即节流已发生）——throttling 是内核 cgroup 行为，kubelet 不为其产生 K8s Events，`kubectl describe pod` 的 Events 里查不到 CPU throttling 相关条目（勿以 Events 为判据）

**持续性检查（必做）**——故障窗口内故障必须持续存活（负载型故障：负载进程在即故障在，throttle 持续发生）：
以「注入生效确认」为时点锚（注入验证第 1 + 4 条通过 = 生效：top 顶格 + nr_throttled 高于基线），生效后一次 `time_wait 30`（间隔 = 2 × 传播上限：本案为规则/字段/进程型即时生效故障，无传播过程，30s 为最小复测窗，按 SKILL.md「持续性采样间隔 per-case 推导」），到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：负载进程仍在（`kubectl exec <pod> -c <sidecar> -- sh -c 'ps -o pid,args | grep "[w]hile :"'`——进程在即负载在）
2. 行为复查：`kubectl top pod --containers` sidecar CPU 仍顶格（limit 的 100%）
3. 计数器复查：nr_throttled 相比注入生效时**继续递增**（节流仍在周期性发生——与 nr_periods 同步前进）

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动停止 CPU 负载注入

**恢复验证**：
1. 执行 `kubectl top pod <pod-name> -n <namespace> --containers`，确认 Sidecar 容器 CPU 回落至正常水平
2. 确认 Sidecar 提供的辅助服务恢复正常（代理可用、日志恢复采集）
3. 确认主容器服务响应时间恢复基线
4. **throttling 计数器终态判据**：nr_throttled 是单调递增计数器（append-only，历史计数不消失）——恢复后的判据是**计数不再前进**：复查 cpu.stat，`nr_throttled` 停在恢复时刻的值（与 nr_periods 一并对比，两者均不再增长即节流停止）；勿因计数非零误判"未恢复"
5. **拆线归还（接线靶）**：patch remove sidecar 容器 + `kubectl rollout status` 等待收敛，靶 Deployment 回到单容器基线形态（演练资产拆除，见资源准备第 4 条）

**基准事实**：
- **根因**：Sidecar 容器内 CPU 被 ChaosBlade 注入满载，消耗该容器 CPU cgroup 配额
- **必现现象**：目标 Sidecar 容器 CPU 使用率 100%；Sidecar 提供的功能（代理/采集/监控）延迟增加或超时；Pod 整体 CPU 使用率上升；主容器在无独立 CPU limits 时可能受到资源争抢影响

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。

前提条件：目标容器内需有 `stress-ng` 工具，或至少有 `sh` shell 可用

注入命令：
```bash
# 方式一：容器内有 stress-ng（后台+重定向让 exec 立即返回，--timeout 自带自动恢复）
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c 'stress-ng --cpu 0 --cpu-load <percent> --timeout <duration>s >/dev/null 2>&1 &'
# 方式二：容器无 stress-ng，用 shell 循环（重定向避免 exec 挂起；PID 落盘定时自动 kill；
# 计数用 while 自增而非 $(seq)——busybox 1.33 无 seq applet（exit 127），
# $(seq 1 N) 展开为空会使 for 空转零注入（静默失败））：
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c '
  : > /tmp/loadgen-worker.pids
  i=1
  while [ $i -le <N> ]; do
    ( while :; do :; done ) >/dev/null 2>&1 &
    echo $! >> /tmp/loadgen-worker.pids
    i=$((i+1))
  done
  ( sleep <duration>; kill $(cat /tmp/loadgen-worker.pids) 2>/dev/null; rm -f /tmp/loadgen-worker.pids ) >/dev/null 2>&1 &
'
```
倒计时从武装时刻起算：负载发生器启动与定时器武装在同一 sh -c 载荷内原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c 'pkill -f "loadgen-worker.pid[s]"; true'` 一并停掉故障与旧定时器（后台 subshell 共享载荷 cmdline，此杀同时命中定时器与负载循环，即全停语义），再重跑上方注入命令原子重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）

恢复命令（从精确到兜底）：
```bash
# stress-ng：kill 进程
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- pkill -f stress-ng
# shell 循环 首选：按注入落盘的 PID 精确 kill
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c 'kill $(cat /tmp/loadgen-worker.pids) 2>/dev/null; rm -f /tmp/loadgen-worker.pids'
# 兜底：ps+kill（比 pkill 通用，精简镜像常无 pkill）
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- \
  sh -c "ps -o pid,args 2>/dev/null | grep '[w]hile :' | awk '{print \$1}' | xargs -r kill -9"
```

注意事项：
- shell 循环单个只能打满单核，需按容器 CPU 上限起 N 个循环逼近目标百分比；精细百分比应优先 stress-ng；**容器已配 CPU limit 时 N=1 即可**——单循环想吃 1 核而 quota 只有 limit 核数，每周期被节流，top 显示 limit 的 100% 且 nr_throttled 递增，多起循环无增益（同被节流到同一配额）
- shell 循环命令必须重定向 `>/dev/null 2>&1`，否则占住 exec 输出管道导致 `kubectl exec` 挂起到 10s 超时
- 自动恢复基于 PID 文件（`/tmp/loadgen-worker.pids`）+ 定时 kill，可靠；切勿用 `$(jobs -p)` 定时自杀（脱离子 shell 取不到 PID）
- stress-ng 方式支持 `--cpu-load` 精确控制负载百分比，但需容器镜像包含该工具
- 本用例手段2的恢复是**容器内进程操作**（kill 负载进程），不是 API 对象写——恢复载体标准件判据一不满足，勿建四件套；定时器由容器内 sleep + PID 文件自带（见上方注入命令），与恢复载体是两类自恢复机制（进程型 vs API 型）
