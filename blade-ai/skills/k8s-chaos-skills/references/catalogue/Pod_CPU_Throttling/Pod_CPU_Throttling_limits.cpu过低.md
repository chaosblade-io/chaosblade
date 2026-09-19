---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 CPU limits），路由进 FaultDrill
# CR 通道；CRD 不可装时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：CR 通道本体（FaultDrill CR）
# 落 victim ns（P10 显式写入），scope 在受害者覆盖与同 ns secondary 网之外——
# 写集准入唯一路径 = 本条目；CR 名 = fd-<任务派生短哈希>（前缀与正文 CR 模板
# 同源默认值），走 name_prefix 动态形态；条目 ns 与本 case 演练窗口 ns 对齐。
mechanism_writes:
  # CR 通道本体（FaultDrill CR 落 victim ns——P10 显式写入；名 = fd-<任务派生
  # 短哈希>，前缀与正文 CR 模板同源默认值）：scope 在受害者覆盖与同 ns secondary
  # 网之外，写集准入唯一路径 = 本立法条目（guard 3.6 mechanism-entries 分支）
  - scope: faultdrill
    namespace: default
    name_prefix: "fd-"
---

**用例名称** limits.cpu过低 导致 Pod_CPU_Throttling

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 CPU limits；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

```yaml
apiVersion: drill.blade-ai.io/v1alpha1   # 组名可配（faultdrill_crd_group）
kind: FaultDrill
metadata:
  name: fd-<任务派生短哈希>               # 前缀可配（faultdrill_name_prefix）；零演练签名词根
  namespace: <namespace>                  # 必须显式写入——见下方 P10 条款
spec:
  action: specPatch
  targetRef:
    kind: Deployment
    name: <deployment-name>
    namespace: <namespace>
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: replace
    path: /spec/template/spec/containers/0/resources/limits/cpu
    value: <过低的 limits.cpu 值>
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  - op: replace
    path: /spec/template/spec/containers/0/resources/limits/cpu
    value: <注入前记录的基线值>
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- 多容器 Pod 调整 containers/N 索引至目标容器；滚动由 patch 自动触发。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 内进程频繁被内核 CPU 节流（throttle），`cpu.stat` 中 `nr_throttled` 持续增长
2. 应用请求延迟显著增大，P99 延迟飙升
3. `kubectl top pod` 显示 CPU 使用率接近 limits 但实际未满载

**资源准备**：
1. 确认应用 A 已正常运行，且 Pod 配置了 resources.limits.cpu
2. 确认监控系统可观测 Pod CPU 使用率及 throttle 指标

**演练步骤**：
1. 定位应用 A 的 Deployment，并记录其 CPU limits 原始值
2. **先武装定时自恢复，再注入**（先捕获原始 CPU limits 并武装定时器，再注入。恢复命令幂等：
   定时器到期自动恢复为主，Agent 在演练结束时主动执行同一条命令兜底，定时器迟到重复执行无
   副作用。定时器 shell 逻辑必须作为 `kubectl exec` 载体载荷派发——直接以 `sh -c '…'` 作为
   顶层命令派发会被命令守卫拦截（unknown_binary: sh），载体内 `sh -c` 同时解决 exec-form
   通道不解释裸 `( sleep … ) &` 语法的问题；载体 Pod 为多副本时无法可靠终止定时器，
   故不设 pidfile。载体 Pod 选集群内带 kubectl 且有足够 RBAC 权限的常驻 Pod（如演练工具 Pod）。
   恢复脚本落盘形态按 recovery-carrier.md 第七节「四档定案表」按明文字节数查表选定（Phase 2 无
   base64 生成器，勿留 <restore-b64> 占位符）。chaosblade 负载实验建议注入时带 `--timeout` 自带到期销毁，
   其主动销毁见"注入恢复"第 1 步）：
   ```bash
   # 基线捕获：Agent 读取输出并记录原始 limits（多容器 Pod 请调整 containers 索引至目标容器）
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}'
   # 武装定时自恢复（"注入恢复"第 2 步 patch 命令按第七节四档表选定落盘形态；下行为旧契约历史形态示例，勿套用）
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-cpulimit.sh; ( sleep <duration>; sh /tmp/blade-restore-cpulimit.sh ) >/tmp/restore.log 2>&1 & echo armed'
   # 调低 limits 注入
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/cpu","value":"<调低后的limits值>"}]'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-cpulimi[t]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
3. 使用 chaosblade 对应用 A 的 Pod 注入 CPU 负载，确保实际 CPU 需求超过 limits，触发内核 throttle
4. 观察 Pod CPU throttle 指标变化及应用响应延迟

**注入验证**：
1. 进入容器查看 CPU cgroup 统计（路径按 cgroup 版本选择——同一集群混合 cgroup 视图是常态，勿按集群或 K8s 版本假设，一律以容器内探针实测为准：`ls /sys/fs/cgroup/cgroup.controllers` 存在即 v2，absent 即 v1；2026-09-15 实测本集群 terway 靶容器为 v1 视图，字段名为 `throttled_time`——两个版本字段名均以实际读到的为准）：v2 路径 `cat /sys/fs/cgroup/cpu.stat`（字段 `nr_throttled`、`throttled_usec`，微秒）；v1 路径 `cat /sys/fs/cgroup/cpu/cpu.stat`（字段 `nr_throttled`、`throttled_time`）。确认 `nr_throttled` 已高于注入前读数、节流时间已增长——两者是单调递增计数器，高于基线即节流已发生，无需反复采样观察增长（可看 `nr_throttled/nr_periods` 占比）。内核归因强化判据（可选）：同目录读 `cpu.cfs_quota_us`/`cpu.cfs_period_us`，quota/period 比值应等于注入后 limits（如 5000/100000 = 50m）——证明 limits 降低真实传导进内核 CFS bandwidth controller，而非仅 API 对象变化。verify 期 exec 探针内层勿带 `2>/dev/null` 重定向——redirect 形态会被只读屏拒（不可证明形态，改道重发即可，但会烧一轮）
2. `kubectl top pod <pod-name> -n <namespace>` 确认 Pod CPU 使用率接近 limits
3. （可选，仅当演练方提供了应用访问入口时）确认请求延迟显著增大；无入口时上述 throttling 与 CPU 证据成立即可判定

**注入恢复**：
1. 销毁 chaosblade CPU 负载实验（见演练步骤 3 的实验 UID）
2. 等待 `<duration>` 到期，定时器自动将 CPU limits 还原为基线；演练提前结束时由 Agent 主动
   执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/cpu","value":"<基线捕获的原始值>"}]'
   ```

**恢复验证**：
1. 查看 cpu.stat（路径按 cgroup 版本，同注入验证第 1 条），确认 `nr_throttled` 停止增长、节流时间冻结
2. `kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}'` 确认已还原为步骤 1 基线值——负载销毁后即使 limits 仍为注入值也不再 throttle，第 1 条全绿不代表配置已回置，须单独确认
3. （可选，有访问入口时）确认请求延迟恢复正常

**基准事实**：
- **根因**：容器 limits.cpu 设置过低，实际 CPU 需求超过 limit，内核对容器 CPU 时间片进行 throttle，导致应用性能下降
- **必现现象**：cpu.stat 中 nr_throttled 持续增长；应用延迟飙升；CPU 使用率接近 limits 上限

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效 CPU 负载注入。

前提条件：容器内有 `stress-ng` 时优先使用；否则用 shell 循环替代。多容器 Pod 请用 `-c <container>` 指定目标容器。

注入命令：
```bash
# 方式一：容器内有 stress-ng（后台+重定向让 exec 立即返回，--timeout 自带自动恢复）
kubectl exec <pod-name> -n <namespace> -c <container> -- \
  sh -c 'stress-ng --cpu 0 --cpu-load <percent> --timeout <duration>s >/dev/null 2>&1 &'
# 方式二：容器无 stress-ng，用 shell 循环（重定向避免 exec 挂起；PID 落盘定时自动 kill；
# 计数用 while 自增而非 $(seq)——busybox 1.33 无 seq applet（exit 127），
# $(seq 1 N) 展开为空会使 for 空转零注入（静默失败））
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c '
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

恢复命令（从精确到兜底）：
```bash
# stress-ng：kill 进程
kubectl exec <pod-name> -n <namespace> -c <container> -- pkill -f stress-ng
# shell 循环 首选：按注入落盘的 PID 精确 kill
kubectl exec <pod-name> -n <namespace> -c <container> -- \
  sh -c 'kill $(cat /tmp/loadgen-worker.pids) 2>/dev/null; rm -f /tmp/loadgen-worker.pids'
# 兜底：ps+kill（比 pkill 通用，精简镜像常无 pkill）
kubectl exec <pod-name> -n <namespace> -c <container> -- \
  sh -c "ps -o pid,args 2>/dev/null | grep '[w]hile :' | awk '{print \$1}' | xargs -r kill -9"
```

注意事项：
- shell 循环单个只能打满单核，需按 CPU 上限起 N 个循环逼近目标百分比；精细百分比应优先 stress-ng
- shell 循环命令必须重定向 `>/dev/null 2>&1`，否则占住 exec 输出管道导致 `kubectl exec` 挂起到 10s 超时
- 自动恢复基于 PID 文件 + 定时 kill，可靠；切勿用 `$(jobs -p)` 定时自杀（脱离子 shell 取不到 PID）
- 精度不如 ChaosBlade 的 cgroup 级 CPU 控制
