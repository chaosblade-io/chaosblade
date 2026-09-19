---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 移除注入的 finalizer），路由进 FaultDrill
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

**用例名称** Finalizers未清理 导致 Pod_Terminating

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 移除注入的 finalizer；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

```yaml
apiVersion: drill.blade-ai.io/v1alpha1   # 组名可配（faultdrill_crd_group）
kind: FaultDrill
metadata:
  name: fd-<任务派生短哈希>               # 前缀可配（faultdrill_name_prefix）；零演练签名词根
  namespace: <namespace>                  # 必须显式写入——见下方 P10 条款
spec:
  action: specPatch
  targetRef:
    kind: Pod
    name: <pod-name>
    namespace: <namespace>
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: add
    path: /metadata/finalizers
    value: [vol.ops/detach-pending]
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  - op: remove
    path: /metadata/finalizers
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- 删除 Pod（`--wait=false`——触发终止流程的必要步）保留为 execute 计划普通步骤。
- 注入后 Pod 带 deletionTimestamp 停留 Terminating——目标对象持续可读，readback 守卫正常；恢复 remove finalizer 后 Pod 被 GC 消失，属恢复完成的预期形态。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 无法完成删除，`metadata.deletionTimestamp` 已设置但 Pod 对象仍存在于 API 中
2. Pod 的 metadata 中存在 finalizers 字段，等待外部控制器清理
3. kubectl 显示状态可能是 `Terminating`（容器仍在运行时）或 `Error`/`Failed`（容器已退出时）

> **注意**："Terminating" 是 kubectl 的显示状态，仅当 `deletionTimestamp` 已设置
> 且容器仍在运行时才显示。如果容器在 delete 信号后已退出，kubectl 会按容器状态
> 显示（如 Error/Completed），但 Pod 对象仍因 finalizer 滞留在 API 中。
> 判定依据是 `deletionTimestamp` + `finalizers` 同时存在，而非 kubectl 显示状态。

**RCA症状**：
1. `kubectl get pod` 显示 Pod 状态为 Terminating 或 Error/Failed，但 Pod 对象持续存在
2. `kubectl get pod -o jsonpath='{.metadata.deletionTimestamp}'` 返回非空时间戳
3. `kubectl get pod -o jsonpath='{.metadata.finalizers}'` 返回非空列表
（以上为kubectl直接可观测的现象，不包含诊断结论）

**资源准备**：
1. 确认目标应用已正常运行
2. 确认目标 Pod 当前没有 finalizers（`kubectl get pod <name> -o jsonpath='{.metadata.finalizers}'` 返回空）

**演练步骤**：
1. 定位目标 Pod
2. **先武装定时自恢复，再注入**（恢复命令幂等：定时器到期自动恢复为主，Agent 在演练结束时
   主动执行同一条命令兜底——第一次执行后 Pod 即被 GC，定时器迟到再执行报 NotFound 无副作用）。
   倒计时从**武装时刻**起算而非注入时刻，武装必须遵守三条纪律：
   - **先校验后武装**：恢复脚本落盘形态按 recovery-carrier.md 第七节「四档定案表」按明文字节数查表选定
     （Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符）；武装前先在载体内
     **回显全文，与预期的恢复命令逐字比对**，一致才允许进入倒计时（校验用回显目检，
     不用 `sh -n` 语法检查——命令名拼写错误如 `kuberctl` 语法合法，`sh -n` 照样放行，
     唯有回显到眼前才能被看出；带错脚本武装再边修边烧窗口，
     600s 定时器武装后花 9 分钟修脚本，注入时窗口仅剩 64s，验证未开始故障已恢复）：
     ```bash
     kubectl exec <载体Pod> -n <载体ns> -- sh -c 'cat /tmp/blade-restore-finalizer.sh && echo validated'
     ```
   - **武装与注入必须是紧邻步骤（间隔 ≤ 60 秒）**：若武装后发生任何计划变更/调试/
     修复，窗口被等额侵蚀，必须重新武装后再注入
   - **修复必重武装**：武装后修改了恢复脚本（哪怕只是改文件内容），旧倒计时不会重置，
     必须先停旧定时器再以全额时长重新武装（`finalize[r]` 括号法防 pkill 自匹配）：
     ```bash
     kubectl exec <载体Pod> -n <载体ns> -- sh -c 'pkill -f blade-restore-finalize[r]; true'
     ```
   定时器必须经 `kubectl exec` 载体派发——顶层裸 `sh -c '… & echo armed'` 不被工具守卫放行；
   载体 Pod 需含 kubectl 与集群凭证（如集群内工具 Pod，业务镜像多为
   极简镜像无 kubectl，不可作载体）：
   ```bash
   # 武装定时自恢复（"注入恢复"第 1 步命令按第七节四档表选定落盘形态；下行为旧契约历史形态示例，勿套用）
   kubectl exec <载体Pod> -n <载体ns> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-finalizer.sh; ( sleep <duration>; sh /tmp/blade-restore-finalizer.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
3. 使用 kubectl patch 给 Pod 添加自定义 finalizer：
   `kubectl patch pod <pod-name> -n <namespace> -p '{"metadata":{"finalizers":["vol.ops/detach-pending"]}}'`
4. 使用 `--wait=false` 删除 Pod，触发终止流程：
   `kubectl delete pod <pod-name> -n <namespace> --wait=false`
   > 不加 `--wait=false` 会导致 kubectl 等待删除完成，因 finalizer 阻塞而超时。
5. 观察 Pod 状态变化

**注入验证**：
0. 注入验证必须在注入完成后**立即发起**，不等常规验证轮次——deletionTimestamp+finalizer
   签名在注入后数秒内即可观测，而故障窗口可能短于预期（等验证阶段开始时
   Pod 已被定时器恢复并 GC，核心证据缺失）
1. 执行 `kubectl get pod <pod-name>`，确认 Pod 对象仍存在（显示 Terminating 或 Error）
2. 执行 `kubectl get pod <pod-name> -o jsonpath='{.metadata.deletionTimestamp}'`，确认已设置
3. 执行 `kubectl get pod <pod-name> -o jsonpath='{.metadata.finalizers}'`，确认包含注入的 finalizer
4. 确认没有控制器在处理该 finalizer（`vol.ops/detach-pending` 无对应控制器，因此不会被自动清理）

**注入恢复**：
1. 等待 `<duration>` 到期，定时器自动移除注入的 finalizer，Pod 将被 Kubernetes GC 自动清除；
   演练提前结束时由 Agent 主动执行同一条恢复命令（幂等——第一次执行后 Pod 已被 GC，
   定时器迟到再执行报 NotFound 无副作用）：
   ```bash
   kubectl patch pod <pod-name> -n <namespace> --type=json \
     -p '[{"op":"remove","path":"/metadata/finalizers"}]'
   ```

**恢复验证**：
1. 执行 `kubectl get pod <pod-name>`，确认 Pod 已从集群中删除（返回 NotFound）
2. 确认 ReplicaSet 已创建替代 Pod 且状态为 Running

**基准事实**：
- **根因**：Pod 的 metadata 中存在 finalizers，Kubernetes 在删除资源时会设置 deletionTimestamp 但不会从 etcd 中移除资源对象，直到所有 finalizer 被外部控制器清除
- **必现现象**：Pod 对象持续存在于 API 中；deletionTimestamp 已设置；metadata.finalizers 非空
