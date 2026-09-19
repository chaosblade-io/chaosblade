**用例名称** Pod故障 导致 Pod_被删除

**故障定位**：持续型故障——故障窗口内目标 Pod 反复被删除，控制器每次重建后立即被再次删除，形成有界的自主"重建-删除"风暴；Service Endpoints 在窗口内持续抖动。**本用例不提供一次性删除**：单次 `kubectl delete pod` / ChaosBlade `pod-pod delete` 随控制器重建一次即稳定（15-90s 内自愈），不构成有效演练窗口。上游取证（chaosblade-operator `exec/pod/delete.go`）：`pod-pod delete` 仅在实验创建时执行**一次** Delete，`--timeout` 只控制实验记录何时销毁，**不存在**窗口内反复删除的机制——任何声称 "timeout 窗口内持续删除" 的写法都是错误的。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认，不得默认成一次性操作。

**故障现象**：
1. 目标 Pod 在窗口内反复被删除，每次出现短暂 Terminating 后消失
2. Deployment/ReplicaSet 控制器不断创建新 Pod 替代（Pod 名称持续变化，AGE 持续极短）
3. Service Endpoints 在窗口内持续抖动（旧 Pod 摘除与新 Pod 就绪之间的流量中断反复发生）
4. Pod Events 中反复出现 Killing 事件与 Scheduled/Pulling/Created/Started 事件

**资源准备**：
1. 确认目标应用已正常运行，有明确的 namespace 和 label selector
2. 确认目标 Pod 由 Deployment/ReplicaSet 管理（确保删除后能自动重建，否则故障变成永久消失而非反复删除）：
   `kubectl get pod <pod-name> -n <namespace> -o jsonpath={.metadata.ownerReferences[*].kind}`
3. 确认集群内存在可用的**载体 Pod**（带 kubectl 且有目标命名空间 pods `delete` 权限的常驻 Pod，如演练工具 Pod）——实机验证发现载体 SA 仅有 get/list 时删除会被 RBAC 拒绝
4. 确认 `duration_seconds`（故障窗口）已明确

**演练步骤**：
1. 确认目标 Pod 当前状态为 Running 且 Ready，记录当前 Pod 名称：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 注入 —— 经载体 Pod 派发**墙钟时限的有界删除循环**（循环 shell 逻辑必须作为
   `kubectl exec` 载体载荷派发——直接以 `sh -c '…'` 作为顶层命令派发会被命令守卫
   拦截（unknown_binary: sh））：
   ```bash
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'end=$(( $(date +%s) + <duration> )); i=0;
   while [ "$(date +%s)" -lt "$end" ] && [ $i -lt <rounds> ]; do
     kubectl delete pod -l <label-selector> -n <namespace> --wait=false
     i=$((i+1)); sleep <interval>
   done'
   ```
   参数说明：
   - `<duration>`：故障窗口总时长（秒），取 `duration_seconds`；墙钟到期循环自动终止——
     这是主保险（用 `date +%s` 计时而非 `SECONDS`：后者是 bash 特性，`sh -c` 下不会自增）
   - `<rounds>`：轮数上限，是第二重保险；应满足 `<rounds> × <interval>` ≥ `<duration>`
   - `<interval>`：两轮删除间隔（秒），建议 5-10，让控制器有时间重建再删，
     才能观察到完整的"重建-删除"循环
   - 与节点侧 crictl 类用例不同：kubectl 的 kubeconfig 不在宿主机上，无法用
     `systemd-run` 在节点武装自停 timer，因此持续删除用**墙钟时限的有界循环**实现
     自停兜底。严禁使用无时限的手动反复执行
   - 倒计时即循环本身：循环启动即注入即持续，无独立武装步骤，不存在武装-注入
     间隙侵蚀故障窗口（见 SKILL.md 安全红线「故障窗口完整」）

**注入验证**：
1. 执行 `kubectl get pods -n <namespace> -l <label-selector>`，确认旧 Pod 名称已不存在，新 Pod 已被创建（名称不同、AGE 很短）。旧 Pod 短暂处于 Terminating（默认宽限期 30s 内）是删除进行中的预期中间态，非失败
2. 执行 `kubectl get events -n <namespace> --sort-by='.lastTimestamp'`，确认存在 Killing 事件（旧 Pod 被删除）以及 Scheduled/Created/Started 事件（新 Pod 被重建）
3. 执行 `kubectl get endpoints <service-name> -n <namespace>`，观察 Endpoints 抖动（取决于新 Pod 就绪速度）
4. **持续性检查（必做）**——判据是"无外部干预下删除仍在继续"。本步证明的是持续性（机制将运转到窗口结束），不是效果存在——效果已由第 1-2 步证明；对持续性命题，机制状态就是直接证据。分层按可得性取用，**任一层成立即完成本步，下层仅为上层不可查时的回退**：
   - **白盒主证（即时，单独充分）**：故障机制本身仍存活——载体内删除循环仍在运行。机制存活时"持续在删"由构造成立——本步即完成，无需佐证窗口
   - **有界佐证（仅当机制不可查时的回退）**：静观一个短窗口（≤60 秒）后再次执行 `kubectl get pods`，Pod 仍在被反复 Killing、AGE 持续极短即强确认
   - **黑盒回退（仅当以上均不可查时）**：停止一切操作、静观 1-2 分钟后再查
   - 若循环在窗口内提前终止且 Pod 已稳定，说明故障窗口契约未达成，必须如实报告实际持续时长，不得报"持续删除已达成"

**注入恢复**：
1. 等待 `<duration>` 到期循环自动终止（主保险），控制器重建最后一个 Pod 后稳定
2. 如需提前终止：中断载体内的删除循环——`kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f "kubectl delete pod -l <label-selecto[r]"; true'`，之后控制器自动重建 Pod 并稳定
3. 等待新 Pod 完全就绪（Running + Ready）

**恢复验证**：
1. 执行 `kubectl get pods -n <namespace> -l <label-selector>`，确认 Pod 状态为 Running 且 Ready（READY 列为 x/x），Pod 名稳定不变（重建停止的直接证据）
2. 执行 `kubectl get endpoints <service-name> -n <namespace>`，确认 Service Endpoints 数量恢复正常
3. 查 events 最后一条 Killing 事件时间早于恢复时刻即无新删除（事件时间戳即证据，无需静观）

**基准事实**：
- **根因**：载体内的有界删除循环按 label selector 反复删除目标 Pod（等同反复 kubectl delete pod），控制器每次重建后立即被再次删除，形成有界的自主"重建-删除"风暴
- **必现现象**：Pod 名称在窗口内持续变化、AGE 持续极短；Events 中反复出现 Killing 事件；Service Endpoints 持续抖动；循环到期后控制器最后一次重建并稳定

---

**手段2（kubectl-native）**

> 上方演练步骤的有界删除循环即 kubectl-native 形态本身，**不存在独立的手段2**。
> 一次性形态（单次 `kubectl delete pod`、ChaosBlade `pod-pod delete`）已被本用例废除：
> 前者随控制器重建一次即消失；后者经上游源码取证为一次性 Delete（`--timeout` 只销毁
> 实验记录），两者均不构成持续故障窗口，没有演练价值。

注意事项：
- 持续删除期间 Pod 名不断变化，验证与恢复都要用 label selector，不要锁定旧 Pod 名
- 载体 Pod 若为多副本 Deployment，循环进程只落在处理该请求的那一个副本上；后续定位/中断循环时若路由到别的副本会找不到进程——此时不要重复派发循环（会产生多个循环叠加删除），应以"Pod 是否仍在被反复删除"的实际观察为准，窗口到期所有副本上的循环都会自行终止（墙钟时限是每份循环自带的）
- 载体 SA 权限不足（仅 get/list）时循环内删除会每轮被 RBAC 拒绝——静默空转零注入，注入后必须立即按「注入验证」确认 Pod 真的在被删
- `restartPolicy: Never` 且无控制器管理的裸 Pod 删除后不会重建，故障变成永久消失，不适用本用例，注入前先确认归属（见资源准备第 2 条）
