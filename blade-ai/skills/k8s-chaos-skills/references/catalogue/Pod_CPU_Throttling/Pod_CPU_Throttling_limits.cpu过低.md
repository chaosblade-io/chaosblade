---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 CPU limits），路由进程序化
# 恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch Deployment 模板的 resources 属受害者域内，名字匹配放行），无跨
# 对象写条目；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash>
# 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌检查（RBAC 从
# restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），不经 LLM
# kubectl 写面，无立法条目。
---

**用例名称** limits.cpu过低 导致 Pod_CPU_Throttling

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 CPU limits；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace
  path: /spec/template/spec/containers/0/resources/limits/cpu
  value: <过低的 limits.cpu 值>
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
- op: replace
  path: /spec/template/spec/containers/0/resources/limits/cpu
  value: <注入前记录的基线值>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 多容器 Pod 调整 containers/N 索引至目标容器；滚动由 patch 自动触发。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作（chaosblade CPU 负载注入/销毁）保留为 execute 计划普通步骤。

**故障现象**：
1. Pod 内进程频繁被内核 CPU 节流（throttle），`cpu.stat` 中 `nr_throttled` 持续增长
2. 应用请求延迟显著增大，P99 延迟飙升
3. `kubectl top pod` 显示 CPU 使用率接近 limits 但实际未满载

**资源准备**：
1. 确认应用 A 已正常运行，且 Pod 配置了 resources.limits.cpu
2. 确认监控系统可观测 Pod CPU 使用率及 throttle 指标

**演练步骤**（主路径 = 基线捕获（步骤 2 前半的 limits 读取，restorePatches 的基线值来源，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、patches=limits.cpu 调低、restorePatches=基线还原、duration_seconds=<duration>），注入+武装+readback 工具内同步完成 → chaosblade CPU 负载注入（步骤 3，execute 计划普通步骤，两条路径共用）；步骤 2 的手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
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

**注入恢复**（主路径下 limits 还原无需 Agent 执行动作——载体 TTL 自治还原 CPU limits 基线（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。chaosblade 负载销毁（第 1 条）为非 patch 域动作，保留为 execute 计划普通步骤，两条路径共用。第 2 条手动命令为降级兜底形态）：
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
