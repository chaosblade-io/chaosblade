**用例名称** 节点Taint无对应Toleration 导致 Pod_Pending

**故障现象**：
1. Pod 状态为 Pending，无法被调度到任何节点
2. Pod Events 中显示 `N node(s) had untolerated taint {node.ops/pending-reboot: true}`
3. 目标 Pod 可调度的所有节点均带有 Pod 无法容忍的污点

**RCA症状**：
1. Pod 状态为 Pending，无法被调度到任何节点
2. Pod Events 中显示 `had untolerated taint`
（以上为kubectl直接可观测的现象，不包含诊断结论）

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群中有多个可调度节点
3. 记录应用 A 的 Pod 当前运行在哪些节点上（这些节点即为"目标节点"）；同时记录目标节点当前 taints 与 workload-affinity 标签基线（本用例基线预期均为空——**若节点有既有 taint/label，恢复时用精确移除**（taint 用 `$patch: delete` 按注入项 key 定向删除、label 只对注入键置 null），**不得整体清空 taints 数组**，否则误删集群固有污点）

**演练步骤**：

> **爆炸半径控制**：本用例仅 taint 目标 Pod 所在的节点（而非全部集群节点），
> 通过 nodeSelector 约束目标 Pod 只能调度到这些节点，从而在保证故障复现的同时
> 避免影响集群中其他工作负载的调度。
> **爆炸半径分类（定案）**：`cluster-wide`——单节点 taint 与五件套变体的
> ClusterRole/ClusterRoleBinding 均为 ns 外变更，如实按此分类并在描述里注明
> 「单节点 taint + 集群级 RBAC 辅助对象」，勿纠结是否降格。

1. 记录目标节点的当前 taint 信息，以及 Deployment 的当前 nodeSelector 原值 JSON（用于恢复；
   无 nodeSelector 时该命令输出为空字符串）：
   ```bash
   kubectl get deployment <name> -n <ns> -o jsonpath='{.spec.template.spec.nodeSelector}'
   ```
2. 按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈——四对象同名 `drill-rc-<hash>`、全部建在被批准的靶点命名空间内，SA/Role/RoleBinding 必须用 `kubectl create` 命令构造。**本用例 RBAC 是五件套变体**（恢复动作跨 namespaced 与 cluster-scoped 两类资源）：deployment 的 nodeSelector 还原是 namespaced 写（Role），node 的 taint 摘除 + label 移除是 cluster-scoped 写（**必须 ClusterRole——namespaced Role 写 nodes 规则对 SA token 不生效，GET 返回 403**；cluster-scoped 资源只能经 ClusterRole+ClusterRoleBinding 授权）。注意多资源语法是**逗号分隔**（`--resource=deployments,nodes`；点号分隔会被解析为不存在的资源名导致创建静默失败）：
   ```bash
   kubectl create serviceaccount drill-rc-<hash> -n <namespace>
   kubectl create role drill-rc-<hash> -n <namespace> --resource=deployments --verb=get,patch
   kubectl create rolebinding drill-rc-<hash> -n <namespace> --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl create clusterrole drill-rc-<hash> --resource=nodes --verb=get,patch
   kubectl create clusterrolebinding drill-rc-<hash> --clusterrole=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=busybox:1.36 --restart=Never --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   ```
   （镜像按标准件第九节判别法选定——受限网络（VPC 无法拉 docker.io）时选节点已缓存且含 curl/sh/sleep 工具链的镜像；第九节「本集群实证档案」中精确 tag 匹配的镜像可直接引用免工具链探测。SA/Role/RoleBinding 须先于载体 Pod 创建：overrides 挂接的 Pod 引用该 SA，SA 不存在时 Pod 卡 ContainerCreating。清理时为**六连删除**：载体 Pod + SA + Role + RoleBinding + ClusterRole + ClusterRoleBinding——后两者是集群级对象，漏删会留全局残留。）
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节，含写动词 SSAR 对账——Role verbs 以恢复脚本实际载荷动词为准；验权清单覆盖**两类对象**——deployment GET 200 且 node GET 200，任一 403 即中止）。定时器用载体内 SA token 直调 apiserver REST——本用例三个恢复动作跨两类对象（node 两项 + deployment 一项），**用紧凑变量形态的逐 curl 变体**（两条 curl：node 的 taints 还原 + label 移除合并为一次 patch——同一对象多字段一个 JSON 文档；deployment 的 nodeSelector 单独一次），**Content-Type 用标准 `application/merge-patch+json`**——taints 数组是 atomic 整组替换语义（`$patch: delete` 无效：strategic patch 后元素残留仅 value 被剥离），恢复形态由步骤 3 的基线记录决定：**节点 taints 基线为空 → `taints:null` 删键**（完全清除）；基线非空 → `taints:[基线完整数组]` 整组写回（规划期已知基线，直接代入——勿用读改写，载体内无 JSON 工具）。禁止改写为 printf/heredoc/base64 写脚本文件形态（三禁一许纪律，见标准件第四节）：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); S="Content-Type: application/merge-patch+json"; N=https://kubernetes.default.svc/api/v1/nodes/<目标节点名>; D=https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<deployment-name>; curl -s -X PATCH --cacert $C -H "Authorization: Bearer $T" -H "$S" -d "{\"spec\":{\"taints\":<null或基线数组>},\"metadata\":{\"labels\":{\"workload-affinity\":null}}}" $N; curl -s -X PATCH --cacert $C -H "Authorization: Bearer $T" -H "$S" -d "{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":<null或基线JSON>}}}}" $D ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （自断授权尾步——标准件第四节立法：**最后一个**恢复 curl（deployment nodeSelector 还原）升 `curl -sf` 并以 `&&` 链自删 DELETE 自己的 Binding（复用 `$C`/`$T`/`$S` 变量；**本用例是五件套六对象栈——两个 Binding 都删**：`&&` 链 RoleBinding 在前、ClusterRoleBinding 在后，两个 DELETE 均带 `-sf`，任一失败即停剩余授权交收车六连删除；前序动作保持 `;` 串联不中断），建栈后按标准件第二节两步建栈法 + **六对象栈双自删形态**，Role 与 ClusterRole 各 json-patch 追加一条独立自删规则（Role 的规则锁名删自己的 RoleBinding、ClusterRole 的规则锁名删自己的 ClusterRoleBinding；**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行；字节挤不下两个 DELETE 时按标准件第二节优先删 ClusterRoleBinding——集群级授权残留面更大。**本模板预算参考（免现场重算，2026-09-17 实算）**：按本模板 739B 口径外推双 DELETE 约 263B，双删满尾步约 1002B——硬限内但贴限（余量 <25B，真实节点名/Deployment 名/非空 taints 基线代入继续上探，超 1024B 即按上述优先序降级单删 ClusterRoleBinding，约 863B）。fail-open：主恢复未确认成功则授权保留——完整形态与五纪律以标准件第四节为准）
   （nodeSelector 还原形态按步骤 1 基线确定：为空用 null 整体移除，非空写基线原值 JSON；label 还原对注入键置 null，节点既有标签不受影响。载荷约 739B（真实节点名+deployment 名代入）在通道 1024B 限内。倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）——**本用例武装点定案：步骤 2-7 均为 setup，武装推迟至步骤 8 taint 紧邻前执行**（本步骤给出的武装模板仅在此定稿，勿在步骤 2 执行武装——中间隔着第一次滚动等待，机械照抄步骤位置会侵蚀故障窗口；见标准件第四节「多步注入序列的武装点」）；武装后发生任何修复须先停旧定时器再全额重武装——定位进程用 `pkill -f 'kubernetes.default.svc'` 匹配 payload 含 apiserver URL 的定时器 shell（见 SKILL.md 安全红线「故障窗口完整」））
3. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁——**机理已定案，勿重推百分比取整与死锁边界语义**，按步骤执行即可；本用例第一次滚动发生在 taint 之前、新 Pod 必可调度 Ready，100% 是防御性设置）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
4. 给目标节点添加标签：`kubectl label node <node> workload-affinity=<app-name>`（仅目标 Pod 所在节点）
5. 给应用 A 的 Deployment 添加 nodeSelector，约束 Pod 只能调度到目标节点：
   `kubectl patch deployment <name> -n <ns> -p '{"spec":{"template":{"spec":{"nodeSelector":{"workload-affinity":"<app-name>"}}}}}'`
6. 等待 rollout 完成（Pod 仍在原节点上运行，因为只有目标节点有此标签）
7. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段；**与拓扑约束类 case 的差异**——那边必须保持 100% 至恢复完成是因为反亲和「每节点排他」会让第二次滚动死锁，本用例无反亲和、恢复移除 nodeSelector 后的新 RS Pod 可调度任意节点，第二次滚动无死锁机理，按本步立即还原即可——勿跨用例迁移教训）
8. 给目标节点添加污点：`kubectl taint node <node> node.ops/pending-reboot=true:NoSchedule`（仅目标节点）
9. 删除应用 A 的一个 Pod，触发重建调度（单副本 Deployment 下重建 Pod 即 Pending——应用不可用正是本用例的故障效果，爆炸半径已被步骤 4-5 的 nodeSelector 锁定）
10. 观察新 Pod 的调度状态

**注入验证**：
1. 执行 `kubectl get pods`，确认新 Pod 状态为 Pending
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 中显示 untolerated taint 相关的调度失败原因。
   消息形态随 K8s 版本而异：老版本显示明细形态 `N node(s) had untolerated taint
   {node.ops/pending-reboot: true}`；新版本（K8s 1.35 级）为**合并形态**（与其他不满足条件合并为一行、
   不带 taint key 明细）：`0/8 nodes are available: 1 node(s) had untolerated taint(s),
   7 node(s) didn't match Pod's node affinity/selector`。判定要点是 `untolerated taint`
   关键字，不要依赖 taint key 明细
   （**事件时间戳形态注**：部分集群 events 呈新式字段形态——lastTimestamp 为 null、count 为空，属 API 字段差异**非数据缺失**；判读以 eventTime 与 series.lastObservedTime 为准，直接解读即可，无需交叉推算重建时间线）
3. 确认目标节点均有 node.ops/pending-reboot taint（`kubectl get node <node> -o jsonpath='{.spec.taints}'`）

**持续性检查（必做）**——故障窗口内故障必须持续存活（配置型故障：taint 在节点 spec、nodeSelector 在 Pod 模板，字段在即故障在）：
以「注入生效确认」为时点锚（注入验证第 1-3 条通过 = 生效：重建 Pod Pending + Events 见 untolerated taint + 节点 taint 在位），生效后一次 `time_wait 30`（间隔 = 2 × 传播上限：本案为规则/字段/进程型即时生效故障，无传播过程，30s 为最小复测窗，按 SKILL.md「持续性采样间隔 per-case 推导」），到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：节点 taint 仍在 spec（`kubectl get node <node> -o jsonpath='{.spec.taints}'` 输出含注入项；nodeSelector 仍在 Pod 模板）
2. 行为复查：Pending Pod 未变 Running（`kubectl get pods -n <namespace> -l <app-label>`——调度器对 Pending Pod 周期重试，taint 未摘则永不成）
3. 事件复查：FailedScheduling 事件 LAST SEEN 相比注入时有新增（调度器仍在周期性重试；新式字段形态下看 series.lastObservedTime 前进）

**注入恢复**：
1. 等待 `<duration>` 到期，载体定时器自动执行 REST 还原（步骤 2 武装的两条 merge-patch curl：node 对象 taints 置 null/写回基线数组 + label 置 null 合并 patch、deployment nodeSelector 还原——taints 基线为空时 null 删键即精确恢复，节点既有污点零误伤）；演练提前结束时由 Agent 主动执行下方 kubectl 三连兜底（幂等，定时器迟到再执行一次无副作用；多条命令独立执行，多个目标节点时对每个
   节点各执行一遍污点与标签还原。nodeSelector 按步骤 1 基线还原——为空则整体移除、
   非空则用基线原值精确替换，避免无条件 remove 丢失原有键值）：
   ```bash
   # ① 摘除目标节点污点（定向移除注入项，保留节点既有 taint）
   kubectl taint node <node> node.ops/pending-reboot=true:NoSchedule-
   # ② 还原 nodeSelector（基线为空时 remove；非空时 replace 为基线原值 JSON）
   kubectl patch deployment <name> -n <ns> --type='json' \
     -p='[{"op":"remove","path":"/spec/template/spec/nodeSelector"}]'
   # ③ 移除目标节点标签
   kubectl label node <node> workload-affinity-
   ```
2. 等待 Pod 滚动更新完成（恢复触发的第二次滚动：新 RS Pod 无 nodeSelector 可调度任意节点，无死锁机理；**用 RS 视角判据，不要用 `kubectl rollout status`**——Pending 期间 rollout status 必然超时误判，判据是旧 RS DESIRED=0、新 RS DESIRED=副本数）
3. 演练全清（载体四件套四连删除 + 带外收尾）：任务先于窗口退出时载体资产清理依赖带外收尾——首选 `blade-ai recover --task-id`（三层收尾：补恢复/确认 + 恢复效果核实 + 程序化四连删除，见标准件第六节）；恢复效果核实注意 restore.log 取证（载体定时器 fire 的直接证据）与状态转移证据（taint 摘除 + nodeSelector 移除 + Pending Pod 转 Running）互为补充

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态变为 Running
2. 确认目标节点 taint 已恢复到演练前状态
3. 确认 Deployment spec 已恢复到演练前状态

**基准事实**：
- **根因**：目标 Pod 可调度的所有节点被标记了 Taint，而 Pod 未配置对应的 Toleration，导致调度器无法找到合适节点
- **必现现象**：Pod Pending；Events 显示 untolerated taint；目标节点带有不可容忍的污点
