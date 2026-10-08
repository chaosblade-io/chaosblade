---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（DELETE 注入的演练道具 NetworkPolicy），
# 但注入动作是 create 新对象而非 patch 既有靶标——装配器 M1 只承载「patch 靶标」
# 注入域，create 类道具注入不经装配器；恢复走标准件载体 timer（curl DELETE REST，
# 幂等）+ Agent 主动兜底双通道，无装配器主路径（如实声明）。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：故障机制创建一个演练道具
# NetworkPolicy（新对象、带 drill-netpol- 前缀、不触及既有资源）。**新对象也必须
# 立法**——networkpolicy 不在 pod 受害靶的 scope 及其 secondary_scopes 内，drift
# 守卫第 4 步对 approved=pod / effective=networkpolicy 判 scope drift 直接拒绝，
# 注入根本到不了 execute 阶段的 armed 门（#65 实测根因；此前误判「新对象无需条目」）。
# namespace 走 namespace_from: victim 派生：netpol 靠 podSelector 选中同 ns 的靶 Pod，
# 必须建在受害者运行时所在 ns，而可移植 case 无法硬编码该 ns（换靶即失效）；freeze 时
# 由确定性代码物化为 spec.namespace（无需集群查询，受害者 ns 已知），与
# name_from: victim_node 同构（DERIVED, never re-declared）。对比 NXDOMAIN
# （configmap 固定 kube-system）、未绑定 PVC（default）——那两类 ns 静态可定，本 case
# 的 netpol ns 跟随受害者，故走派生。载体栈（SA/Role/RoleBinding/裸 Pod 同名
# drill-rc-<hash>）均建在受害者 ns 内，且 serviceaccount/role/rolebinding/pod 都在
# pod 受害靶的 secondary_scopes（同 ns 二级域跳过名字校验），无需在此重复立法；按
# recovery-carrier.md 标准件命令式构造，Role 仅 get,delete networkpolicies。
mechanism_writes:
  - scope: networkpolicy
    namespace_from: victim
    name_prefix: drill-netpol-
---

**用例名称** NetworkPolicy误配 导致 Pod_网络故障

## 故障定位
配置型故障——一个 selector 过宽或策略语义错误的 NetworkPolicy 被创建后，CNI
插件按其规则对匹配 Pod 的流量执行 allow/deny，**策略对象存活即故障存活**，贯穿
整个故障窗口；窗口到期载体 timer 删除策略对象即自动恢复。与既有数据面形态
（iptables DROP/丢包/延迟）的本质区别：本 case 是**配置面拒绝**——丢包发生在
CNI 策略执行层而非 Pod netns 内，`iptables -S` 看不到演练注入的规则（部分 CNI
实现会渲染策略链，但语义归 NetworkPolicy 对象所有）。手段1（ChaosBlade）不适用
——ChaosBlade 无 NetworkPolicy 配置类故障靶点，本用例为 kubectl-native 专属
注入。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 恢复动作配方
（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：DELETE 演练道具 NetworkPolicy。**注入为 create 新对象，装配器不适用**（M1 注入域只承载 patch 既有靶标）——恢复走标准件载体 timer 自治 + Agent 主动兜底双通道，配方如下：

```yaml
targetRef:                                # 靶 = 演练道具自身（新建对象，无既有靶标）
  kind: NetworkPolicy
  name: drill-netpol-<hash>
  namespace: <namespace>
patches:                                  # 注入域：create 整个对象（execute 计划 kubectl 步骤，非 json-patch）
- op: create
  path: /apis/networking.k8s.io/v1/namespaces/<namespace>/networkpolicies
  value: <下方演练步骤 3 的完整 YAML>
restorePatches:                           # 恢复域：载体 TTL 到点自治 DELETE；Agent 死亡后 recover 从台账重放同源
- op: delete
  path: /apis/networking.k8s.io/v1/namespaces/<namespace>/networkpolicies/drill-netpol-<hash>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 恢复由标准件载体 TTL 自治承载（curl DELETE，幂等——对象已不存在时返回 404，restore.log 如实记录即「已恢复」证据）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。
- 载体 Role 动词集：`get,delete networkpolicies`（恢复载荷实际动词 = DELETE，按 recovery-carrier.md 第二节恢复形态无关总则推导）。

## 故障现象
1. 靶 Pod 的入向（或出向）流量被 CNI 策略层拒绝：连通性探测超时或收到拒绝（不同 CNI 渲染不同——calico/cilium 多为静默丢包超时，少数实现回 ICMP 不可达）
2. 与 iptables 注入形态的关键区别：Pod netns 内 `iptables -S` **看不到**演练注入的 DROP 规则（策略在 CNI 自己的执行层）；白盒主证是 NetworkPolicy 对象本身在位
3. selector 匹配范围内的**所有** Pod 同时受影响（不止靶 Pod）——这是误配形态的必然后果，也是爆炸半径声明的直接输入
4. Pod 本身 Running 不崩溃——网络策略不改变 Pod 生命周期，只改变流量可达性

## 资源准备
1. 确认目标应用已正常运行，且有可复验的连通性基线（从测试 Pod 或靶 Pod 内探测目标服务地址，记录注入前可达）
2. 确认集群 CNI 支持 NetworkPolicy（calico/cilium/weave 等；**flannel 裸装不支持**——NetworkPolicy 对象能创建但无任何效果，注入静默无效。探测法：`kubectl get networkpolicy -A` 看既有策略是否被业务使用，或查 CNI DaemonSet 镜像名）
3. 确认目标命名空间当前没有同名的演练 NetworkPolicy（命名冲突预检：`kubectl get networkpolicy drill-netpol-<hash> -n <namespace> -o name`——NotFound 即无冲突）
4. **selector 波及面甄别（规划期义务）**：演练用 selector 必须精确匹配靶 Pod（`matchLabels` 取靶 Pod 独有标签）；若演练目标是复现「误配波及面」，selector 允许放宽到应用组标签，但**必须在预检阶段显式枚举将被波及的 Pod 清单**（`kubectl get pods -n <namespace> -l <放宽后的selector> -o name`），清单即爆炸半径声明的直接输入，不得留到注入后才发现波及了无关负载
5. 按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈——四对象同名 `drill-rc-<hash>`、全部建在被批准的靶点命名空间内，SA/Role/RoleBinding 必须用 `kubectl create` 命令构造：
   ```bash
   kubectl create sa drill-rc-<hash> -n <namespace>
   kubectl create role drill-rc-<hash> -n <namespace> --verb=get,delete --resource=networkpolicies
   kubectl create rolebinding drill-rc-<hash> -n <namespace> --role=drill-rc-<hash> --serviceaccount=<namespace>:drill-rc-<hash>
   kubectl run drill-rc-<hash> -n <namespace> --image=busybox:1.36 --restart=Never --overrides='{"spec":{"serviceAccountName":"drill-rc-<hash>"}}' --command -- sleep <duration+1800>
   ```
   （镜像按标准件第九节判别法选定；SA/Role/RoleBinding 须先于载体 Pod 创建。定名后按标准件第九节做精确同名冲突预检。）
   载体创建后武装前**必须先做 SA 真实 token 验权**（禁 `can-i --as` 假放行，见标准件第三节——Role verbs 以恢复脚本实际载荷动词 DELETE 为准；验权探针 GET 目标 netpol 404 属预期——对象尚未创建，验权判据是「非 403」）：
   ```bash
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <recovery-seconds>; curl -s -X DELETE --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" https://kubernetes.default.svc/apis/networking.k8s.io/v1/namespaces/<namespace>/networkpolicies/drill-netpol-<hash> ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （倒计时从武装时刻起算：先校验后武装、与注入紧邻 ≤60s——本用例注入序列只有 create netpol 一条快命令，武装紧邻其前执行即可；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f networkpolicie[s]; true'`，见 SKILL.md 安全红线「故障窗口完整」。自断授权尾步——标准件第四节立法：主恢复 curl 为裸 `-s`（body 天然落 restore.log，按取证包裹律 K1 界定无需再包裹），尾步在其后以 `&&` 链一条 `curl -sf -X DELETE` 自删自己的 Binding（本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`）；**自删规则须在武装之后**按标准件第二节两步建栈法 json-patch 追加（不可在建栈 Role 时同期追加——armed-before-inject 门禁止武装前 patch 载体 Role、会被 REJECT_BANNED；自删规则只在定时器 fire 时消费，武装后、注入前这条秒级 patch 补上完全来得及，执行序见标准件第九节）；**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行）。fail-open：主恢复未确认成功则授权保留；字节挤不下就不带——完整形态与五纪律以标准件第四节为准。演练结束后按标准件第六节四连删除并带外核实零残留：
   ```bash
   kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete rolebinding drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete role drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
   ```

## 演练步骤
（主路径 = 基线捕获（步骤 1）→ 标准件武装（资源准备第 5 条，`<recovery-seconds>` 取 prompt 下发的 recovery_timer_seconds）→ create 演练道具 NetworkPolicy（步骤 3）；本 case 注入是 create 新对象，无装配器调用）：

> **爆炸半径分类（定案）**：`target-only`——写入集只有一个新建演练道具 NetworkPolicy（新对象不构成"影响其他资源"）；但 consequence 影响面 = selector 匹配范围内全部 Pod 的流量策略（资源准备第 4 条的波及面清单），写入 `blast_radius_detail` 注明，勿因 consequence 抬档（与既有节点 taint 的 cluster-wide 场景不同——本 case 无跨 ns 持久资源写入）。

1. **基线捕获**：记录注入前连通性基线（恢复验证的对照锚点，两条路径共用）
   ```bash
   kubectl exec <test-pod> -n <namespace> -- wget -qO- --timeout=5 <目标服务地址>
   ```
2. **先武装定时自恢复，再注入**（资源准备第 5 条的标准件武装命令——恢复命令幂等：定时器到期 DELETE 为主，Agent 在演练结束时主动执行同一条 DELETE 兜底，对象已不存在时 404 即「已恢复」证据，重复执行无副作用）
3. **注入**：创建 selector 精确匹配靶 Pod 的 deny 策略（示例为入向全拒——policyTypes 与规则的取舍决定故障方向）：
   ```bash
   cat <<'EOF' | kubectl apply -f -
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata:
     name: drill-netpol-<hash>
     namespace: <namespace>
   spec:
     podSelector:
       matchLabels:
         <靶Pod独有标签键>: <靶Pod独有标签值>
     policyTypes:
     - Ingress
     # 空 ingress 规则列表 = 入向全拒（白名单模型：被 selector 选中的 Pod
     # 默认拒绝所有未被显式允许的入向流量）
     ingress: []
   EOF
   ```
   - 仅出向拒绝：`policyTypes: [Egress]` + `egress: []`
   - 双向拒绝：`policyTypes: [Ingress, Egress]` + 两空列表
   - **部分拒绝形态**（更贴近真实误配——只挡特定来源/端口）：ingress 列表写一条窄规则（如只允许某端口），未覆盖的流量即被拒

## 注入验证
1. **（主证据，必做）** 白盒确认策略对象在位且 spec 与注入意图一致：
   ```bash
   kubectl get networkpolicy drill-netpol-<hash> -n <namespace> -o jsonpath='{.spec.podSelector}{"\n"}{.spec.policyTypes}'
   ```
2. 行为确认——被拒方向的连通性探测失败（与基线对照）：
   ```bash
   kubectl exec <test-pod> -n <namespace> -- wget -qO- --timeout=5 <目标服务地址>
   ```
   预期超时/拒绝。**CNI 策略同步有传播窗（calico/cilium 实测 5-15s）**——注入后立即探测可能仍通，等 15s 再采；15s 后仍通则核对 selector 是否真的匹配靶 Pod（`kubectl get pods -n <namespace> -l <selector> -o name` 应列出靶 Pod）
3. 确认靶 Pod 本身 Running（策略不改变 Pod 生命周期）：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.status.phase}
   ```

> ⚠️ 验证纪律：
> - **严禁为不适用的方向反复更换查询方式找证据**。注入 Ingress-only 时出向必然通，查到"通"是必然而非失败。
> - 同一事实（策略是否在位）确认一次即可，不要重复查询。

**持续性检查（必做）**——故障窗口内故障必须持续存活（配置型故障：netpol 对象在即故障在）：
以「注入生效确认」为时点锚（注入验证第 1-2 条通过 = 生效），生效后一次 `time_wait 30`（间隔 = 2 × 传播上限：CNI 策略同步 5-15s → 30s，按 SKILL.md「持续性采样间隔 per-case 推导」），到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工介入后如实报告：
1. 白盒复查：netpol 对象仍存在且 spec 未变（同注入验证第 1 条）
2. 行为复查：被拒方向连通性探测仍失败（同注入验证第 2 条）
3. 稳定性复查：靶 Pod RESTARTS 计数无增长、phase 仍 Running（排除策略外因素导致的状态漂移）

## 注入恢复
（主路径下恢复无需 Agent 执行动作——载体 TTL 自治 DELETE 演练道具（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动删除演练 NetworkPolicy；演练提前结束时由 Agent 主动执行同一条恢复命令（幂等——`--ignore-not-found` 对已删除对象无副作用，定时器迟到重复执行无害）：
   ```bash
   kubectl delete networkpolicy drill-netpol-<hash> -n <namespace> --ignore-not-found
   ```
2. 等待 CNI 策略撤销传播（与注入同量级 5-15s），流量自动恢复——NetworkPolicy 删除后 CNI 撤除对应规则，**无需重启任何 Pod**

## 恢复验证
1. 白盒确认策略对象已删除：
   ```bash
   kubectl get networkpolicy drill-netpol-<hash> -n <namespace> -o name
   ```
   预期 NotFound（404 即删除确证）
2. 行为确认——连通性恢复基线（与演练步骤 1 的基线对照）：
   ```bash
   kubectl exec <test-pod> -n <namespace> -- wget -qO- --timeout=5 <目标服务地址>
   ```
   预期与基线一致可达。**删除后 CNI 撤规则同样有 5-15s 传播窗**——立即探测可能仍不通，等 15s 再采
3. 确认靶 Pod 无重启（`RESTARTS` 与基线一致）、phase 仍 Running
4. 确认载体 restore.log 取证（`kubectl exec drill-rc-<hash> -n <namespace> -- cat /tmp/restore.log`——定时器 fire 的直接证据，DELETE 响应回显；404 形态 = Agent 提前恢复后定时器迟到空触发，同为已恢复证据）

## 基准事实
- **根因**：一个 selector 过宽或规则语义错误的 NetworkPolicy 被创建，CNI 按其白名单模型对匹配 Pod 执行流量拒绝——模拟运维误配策略/安全组规则错误下发/多租户策略串扰场景
- **必现现象**：NetworkPolicy 对象在位且 selector 匹配靶 Pod；被拒方向流量超时/拒绝；Pod 本身 Running 不受影响；Pod netns 内无演练注入的 iptables 规则（与数据面注入形态的白盒区别）
- **传播窗事实**：CNI 策略生效与撤销均有 5-15s 传播窗（calico/cilium 实测量级）——注入验证与恢复验证的行为判据都要计入该窗口，窗口内探测结果不作裁决依据
- **作用域边界**：仅 selector 匹配的 Pod 受影响；同命名空间未被选中的 Pod、其他命名空间 Pod 均不受影响

## 注意事项
- chaosblade 无 NetworkPolicy/配置类故障靶点，本用例为 kubectl-native 专属注入
- **flannel 裸装集群判死**：NetworkPolicy 对象可创建但无任何执行效果（flannel 不含 policy 控制器），注入静默无效——资源准备第 2 条探测不通过时本 case 结构性不可达，如实报告并停止，不得换 CNI 或装 calico 强行推进（超出演练授权面）
- 空 `ingress: []` 与「不写 ingress 字段」语义不同：前者显式全拒，后者在 policyTypes 含 Ingress 时同样全拒——但显式空列表是更可读的误配复现形态
- 演练道具命名带 `drill-netpol-` 前缀（任务侧资产归属登记依据），**禁止**给演练资源加任何演练标记 label（演练必须对集群呈现为真实事故，见 recovery-carrier.md 第一节命名约定）
- 若靶 Pod 是 hostNetwork 模式，NetworkPolicy 对其无效（策略作用于 Pod netns，hostNetwork Pod 共享宿主网络栈）——预检发现 hostNetwork=true 时本 case 对该靶结构性不可达，换靶或停止
- 恢复后 CNI 撤规则传播窗内（5-15s）连通性探测可能仍失败——这是传播中瞬时态而非恢复失败，按恢复验证第 2 条等待后复采
