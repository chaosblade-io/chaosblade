---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 扩回原始副本数），路由进 FaultDrill
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

**用例名称** 人为误操作 导致 workload_副本被缩容

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 扩回原始副本数；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

```yaml
apiVersion: drill.blade-ai.io/v1alpha1   # 组名可配（faultdrill_crd_group）
kind: FaultDrill
metadata:
  name: fd-<任务派生短哈希>               # 前缀可配（faultdrill_name_prefix）；零演练签名词根
  namespace: <namespace>                  # 必须显式写入——见下方 P10 条款
spec:
  action: specPatch
  targetRef:
    kind: <workload-kind>
    name: <workload-name>
    namespace: <namespace>
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: replace
    path: /spec/replicas
    value: <缩容后的低副本数（如 0）>
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  - op: replace
    path: /spec/replicas
    value: <注入前记录的原始副本数>
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Deployment/StatefulSet 的 `spec.replicas` 被改小（注意稳态语义：kubectl scale 后 READY==DESIRED==缩后新值，正确的判读是「spec.replicas 与 Pod 总数均小于注入前基线」，而非「READY < DESIRED」——后者是缩容未完成的暂态，不是本故障的稳态现象）
2. 应用可用实例减少，部分请求无法处理
3. 服务响应延迟增大或出现超时

**资源准备**：
1. 确认应用 A 的 Deployment/StatefulSet 已正常运行，副本数大于 1
2. 确认监控系统可观测 Pod 副本数和请求指标
3. **HPA 管控排除预检（选靶硬条件）**：`kubectl get hpa -n <namespace>` 确认目标 workload 不被任何 HPA 引用——HPA 管控的 workload 被缩容后控制器会立即扩回（scale 至 0 时扩回 minReplicas，缩至中间值时按指标扩回），故障窗口秒级消失，演练不可成立；须改选无 HPA 管控的同域 workload

**演练步骤**：
1. 定位应用 A 的 Deployment/StatefulSet，记录当前副本数，并完成基线快照（每个 Pod 的名字/UID/nodeName——UID 对照是恢复后「重建而非复活」判据的前后参照）
2. **恢复通道对称性定案（本 case 与 daemon 类故障的本质分野）**：kubectl scale 是声明式 API mutation——注入与恢复是同一条命令的镜像（`--replicas=<较小值>` ↔ `--replicas=<基线值>`），恢复通道与注入通道同源同复杂度且幂等（<5s），**Agent 主动执行恢复即满足「30 秒内可恢复」红线，定时自恢复非必要**；timer 自恢复的必要性仅当恢复通道可能比注入通道更难达（daemon 故障/载体失联/宿主网络断）——声明式 mutation 稳态无失控恶化风险，不在其列。通用模板（载体 Pod 内 kubectl 定时器）在本集群三通道判死：①演练载体镜像（terway 等 CNI 组件镜像）无 kubectl 二进制；②宿主 systemd timer + `--kubeconfig=/etc/kubernetes/kubelet.conf` 仅放行节点自操作（uncordon 族），scale deployment 属跨资源写，Node authorizer 语义外必拒；③集群内无可复用的带 kubectl 常驻 Pod（第三方组件 Pod 不得征用）。**恢复主路径 = Agent 在演练结束时主动执行**：
   ```bash
   # 基线捕获：Agent 读取输出并记录原始副本数（恢复命令使用）
   kubectl get <workload-kind> <name> -n <namespace> -o jsonpath='{.spec.replicas}'
   # 缩容注入（误操作经典形态：脚本 bug 或打错数字直接归零）
   kubectl scale <workload-kind> <name> -n <namespace> --replicas=<较小值>
   # 恢复（幂等，迟到重复执行无副作用）
   kubectl scale <workload-kind> <name> -n <namespace> --replicas=<基线捕获的原始副本数>
   ```
   若演练环境存在带 kubectl 且有 scale 权限的常驻工具 Pod，可按通用模板加注定时自恢复（`kubectl exec <载体Pod> -n <载体ns> -- sh -c '( sleep <duration>; kubectl scale ... --replicas=<基线> ) >/tmp/restore.log 2>&1 & echo armed'`）作为兜底——但须先按上述三通道逐一实证可达，不可默认在位；兜底 timer 在场时，武装后发生任何修复须先 `kubectl exec <同一载体Pod> -- sh -c 'pkill -f --replica[s]=; true'` 停旧定时器再全额重武装（SKILL.md 安全红线「故障窗口完整」）
3. 观察 Pod 缩容过程和应用状态变化

**标签选择器提示**：
- Kubernetes 推荐标签格式为 `app.kubernetes.io/name=<name>`，而非简单的 `app=<name>`
- 建议先不带 `-l` 过滤器查询 `kubectl get deployment <name> -n <ns>`，再从返回结果中提取实际标签
- 若需用标签过滤，优先使用 `-l app.kubernetes.io/name=<name>` 或 `-l app.kubernetes.io/component=<name>`

**注入验证**：
1. 执行 `kubectl get pods`，确认 Pod 总数减少，部分 Pod 被终止（`--replicas=0` 形态下目标 selector 的 Pod 列表为空集）
2. 执行 `kubectl get deployment/statefulset <name> -o jsonpath='{.spec.replicas}'`，确认 spec.replicas 等于注入值且小于缩容前的基线值；RS 的 desired/ready 同步收敛（`kubectl get rs -l <selector>`）
3. 缩容事件佐证：Deployment/RS 事件流出现 `ScalingReplicaSet` 缩容记录（Scaled down replica set ...），事件时间落在注入窗口内
4. （可选，仅当演练方提供了应用访问入口时）确认请求延迟增大/超时或可用性下降；无入口时上述副本数证据成立即可判定

**注入恢复**：
1. **主路径（本集群定案）= Agent 主动执行恢复命令**（幂等，重复执行无副作用；若已按通用模板加注兜底 timer，定时器到期自动恢复与主动恢复互为冗余，迟到重放无副作用）：
   ```bash
   kubectl scale <workload-kind> <name> -n <namespace> --replicas=<基线捕获的原始副本数>
   ```
2. 等待 Pod 自动扩容

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 总数恢复到缩容前的值
2. 执行 `kubectl get deployment/statefulset <name>`，确认 READY==DESIRED==基线副本数（注意 UID 对照：恢复后的 Pod 是新 UID 新 Pod 名——「重建而非复活」，旧 Pod UID 不得再现）
3. （可选，有访问入口时）确认请求延迟与可用性恢复正常

**基准事实**：
- **根因**：人为误操作（如 kubectl scale、修改 YAML 等）导致 Deployment/StatefulSet 的副本数被意外缩小，可用实例不足
- **必现现象**：`spec.replicas` 小于注入前基线值，且稳态 READY==DESIRED==缩后新值（「READY < DESIRED」只是缩容过渡暂态，非稳态现象——勿作为注入判据）；Pod 被终止（总数较基线减少）；服务可用性下降

**注意事项**：
- 若目标 Deployment/StatefulSet 由 Helm 管理（label `app.kubernetes.io/managed-by: Helm`），kubectl scale 修改会被 Helm reconciliation 覆盖
- 注入期间应避免触发 Helm upgrade/rollback 操作，否则故障会被意外恢复
- 反之，若需快速恢复，可通过 `helm rollback` 或 `helm upgrade` 强制还原
