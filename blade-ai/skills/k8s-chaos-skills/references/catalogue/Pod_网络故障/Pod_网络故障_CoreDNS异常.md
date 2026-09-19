---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 缩回基线副本数），路由进 FaultDrill
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
    namespace: kube-system
    name_prefix: "fd-"
---

**用例名称** CoreDNS异常 导致 Pod_网络故障

**故障定位**：持续型故障——CoreDNS Deployment 副本数缩零是状态型故障，
副本为零即集群 DNS 服务中断，贯穿整个故障窗口；窗口到期定时器恢复副本数即自动恢复。
本用例为**单手段用例（kubectl-native）**：ChaosBlade 无 deployment 缩容的等价
action——`pod-process kill` 杀掉 CoreDNS 进程后 Deployment 控制器秒级重建，
故障无法持续，不等价。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认；
CoreDNS 是**集群级依赖**（缩零期间全集群 DNS 解析瘫痪），窗口应取小值（如 120 秒）。
⚠️ **通道依赖死锁（硬性前置）**：若控制通道本身依赖集群 DNS（如有的执行链路在命令执行前就要解析外部域名上传工件，解析走的正是集群 DNS），CoreDNS 缩零后**一切经该通道的命令——包括定时器武装与所有恢复命令——都不可达**，形成拓扑死锁。因此：定时器必须在注入前武装完毕，**且**必须存在不依赖集群 DNS 的带外恢复手段（直连 kubeconfig/控制台手动）；无带外手段时不得注入本用例。

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 缩回基线副本数；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

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
    name: <coredns-deployment>
    namespace: kube-system
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: replace
    path: /spec/replicas
    value: 0
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  - op: replace
    path: /spec/replicas
    value: <基线副本数>
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- 目标是 kube-system 基础设施对象：targetRef.namespace 写 kube-system，CR 本体仍落 victim ns（metadata.namespace 两者独立，均须显式）。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 内 DNS 解析失败，应用报 `Name or service not known` 或 `NXDOMAIN` 错误
2. CoreDNS Pod 全部消失（副本数为 0）
3. 集群内服务间调用因 DNS 解析失败而中断
4. 不受影响的：kubelet/API server 通信（走 IP 不依赖 DNS）、已建立的连接与已缓存的解析结果

**资源准备**：
1. 确认应用 A 已正常运行，且依赖集群 DNS 进行服务发现（验证探针用；其 Pod 名记为 `<app-pod>`）
2. 确认 CoreDNS Deployment 正常运行，记录其名称（不同发行版叫 `coredns` 或 `kube-dns`）与副本数
3. 获取 CoreDNS Pod 选择器标签：
   ```bash
   kubectl get deployment <coredns-deployment> -n kube-system -o jsonpath='{.spec.selector.matchLabels}'
   ```
   记录返回的标签（如 `k8s-app=kube-dns`），后续步骤中用 `<coredns-label>` 表示
4. **载体探测（定时器必需）**：定时自恢复须经 `kubectl exec` 载体派发——顶层裸 `sh -c`
   不被工具守卫放行。载体需**同时**满足：容器内有 `kubectl` 二进制、有集群凭证
   （能访问 API server）。探测候选载体（如集群工具 Pod）：
   ```bash
   kubectl exec <载体Pod> -n <载体ns> -- kubectl version --client
   kubectl exec <载体Pod> -n <载体ns> -- kubectl get deployment <coredns-deployment> -n kube-system
   ```
   两条都成功才是合格载体；找不到合格载体时**不得注入**（先武装后注入是硬序——
   无定时器即无自动恢复保障，不存在 Agent 在线保活形态），任务如实失败收尾；
   故障已落地而定时器不可用的，恢复靠带外 `blade-ai recover --task-id`（恢复命令幂等）
5. ⚠️ **载体 RBAC 前置检查**：载体 Pod 的 ServiceAccount 若无 kube-system 的
   deployments/scale 权限，定时器内的 scale 会因 Forbidden 静默失败——输出被
   `>/dev/null 2>&1` 丢弃，表面 armed 实际永不恢复，集群 DNS 瘫痪到人工介入。
   注入前必须在载体内验证：
   ```bash
   kubectl exec <载体Pod> -n <载体ns> -- kubectl auth can-i scale deployment <coredns-deployment> -n kube-system
   ```
   返回 no 则定时器方案不可用——按资源准备第 4 条不得注入，任务如实失败收尾；故障已落地则靠带外 `blade-ai recover --task-id` 或控制台手动 scale（仅限控制通道不依赖集群 DNS 的环境）
6. ⚠️ **带外恢复手段确认（无则不得注入）**：确认存在一条不依赖集群 DNS 的恢复路径——如直连集群的 kubeconfig（`kubectl scale` 走 API server IP，不依赖集群 DNS）或控制台手动改副本数。控制通道若依赖集群 DNS（如任务封装需在命令执行前解析外部域名），故障一旦落地，经该通道的任何恢复命令（含定时器武装、盲发重放）都会在执行前失败——无带外手段时本用例**禁止注入**。反之，若控制通道是直连 kubeconfig（不经依赖集群 DNS 的任务封装），本条件天然满足：集群内定时器的 kubectl 走 in-cluster 配置的 IP 字面量（KUBERNETES_SERVICE_HOST），带外恢复走本机 resolver，两者均不经集群 DNS，故障期间照常可达，定时器+带外恢复双通道有效

**演练步骤**：
1. 记录注入前基线：
   ```bash
   kubectl get deployment <coredns-deployment> -n kube-system -o jsonpath='{.spec.replicas}'
   kubectl get pods -n kube-system -l <coredns-label> -o wide
   ```
   记录原始副本数（定时器与主动恢复均使用）与当前 Pod 列表
2. **先武装定时自恢复，再缩零注入**（两条命令分两次独立执行——不能用 && 串联）：
   ```bash
   # 武装定时自恢复（载体须通过资源准备第 4/5 条检查；恢复命令幂等：
   # 定时器到期自动恢复为主，演练提前结束时 Agent 主动执行同一条命令兜底）
   kubectl exec <载体Pod> -n <载体ns> -- sh -c '( sleep <duration>; kubectl scale deployment <coredns-deployment> -n kube-system --replicas=<基线副本数> ) >/tmp/restore.log 2>&1 & echo armed'
   # 缩零注入
   kubectl scale deployment <coredns-deployment> -n kube-system --replicas=0
   ```
   倒计时从武装时刻起算，武装与注入紧邻下发。**注入失败处置**：`kubectl scale` 是 API 层
   原子操作，要么整体生效要么整体报错——报错时直接执行恢复命令（幂等）还原副本数后
   replan，不存在需要窗口中段重武装的中间态

**注入验证**：
1. 白盒确认副本已缩零（机制主证）：
   ```bash
   kubectl get deployment <coredns-deployment> -n kube-system -o jsonpath='{.spec.replicas},{.status.replicas}'
   kubectl get pods -n kube-system -l <coredns-label>
   ```
   spec.replicas=0 且无 Running 的 CoreDNS Pod 即机制已落位（存量 Pod 进入 Terminating
   属过渡态，以终态无 Running Pod 为准）
2. 效果确认：在应用 A 的 Pod 内执行 DNS 解析（⚠️ 判据陷阱：busybox nslookup 退出码不稳定
   ——超时（`connection timed out; no servers could be reached`）与 NXDOMAIN 均 RC=1，
   但 **NOERROR 空应答（`*** Can't find ...: No answer`）时 RC=0**——解析失败形态
   却报成功退出码，不能以退出码判定，必须 grep 输出文本）：
   ```bash
   kubectl exec <app-pod> -- sh -c 'nslookup kubernetes.default.svc.cluster.local | grep -E "timed out|no servers|No answer"'
   ```
   （管道必须包在 sh -c 载荷内——命令是 argv 直传无 shell，裸 nslookup 形态下
   `|`/`grep` 会沦为 nslookup 的字面参数，过滤静默失效）
   grep 有输出即确认解析失败。注意：已缓存解析结果的存量连接不受影响，
   **不要**以"存量调用仍正常"反推注入未生效
3. 确认应用 A 依赖 DNS 的新发起调用出现解析错误（可观察项；应用无对外调用时以第 2 条为准）

**持续性检查（必做）**——故障窗口内故障必须持续存活，而非注入一次即消失：
1. 窗口中段复查 `kubectl get pods -n kube-system -l <coredns-label>` 仍无 Running Pod——
   若有 Pod 重建，说明有其他控制器/人工介入恢复了副本数，取证 `kubectl get deployment`
   的 replicas 与事件后如实报告
2. 窗口中段在应用 Pod 内复查一次 DNS 解析仍失败（同注入验证第 2 条形态）

**注入恢复**：
1. 等待 `<duration>` 到期，定时器自动将副本数恢复为基线；演练提前结束时由 Agent 主动执行
   同一条恢复命令（幂等，定时器迟到再执行一次无副作用）：
   ```bash
   kubectl scale deployment <coredns-deployment> -n kube-system --replicas=<基线捕获的原始副本数>
   ```
2. 等待 CoreDNS Pod 启动并就绪（新建副本数与基线一致且全部 Ready）

**恢复验证**：
1. 执行 `kubectl get pods -n kube-system -l <coredns-label>`（使用演练步骤 1 中获取的实际标签），确认 CoreDNS Pod 全部 Running 且 Ready，副本数回到基线
2. 在应用 A 的 Pod 内重新执行 DNS 解析，确认恢复正常（CoreDNS 刚拉起时 kubernetes 插件
   同步 Service 记录需数秒到数十秒，Ready 后立即查询可能仍空应答，稍候重试；部分发行版多副本
   间存在间歇性空应答噪声，判读以多次查询的稳定形态为准）：
   ```bash
   kubectl exec <app-pod> -- sh -c 'nslookup kubernetes.default.svc.cluster.local'
   ```
3. 确认应用 A 的服务间调用恢复（可观察项）

**基准事实**：
- **根因**：CoreDNS Pod 全部不可用（副本缩零），导致集群内 DNS 解析服务中断，依赖 DNS 的服务发现和调用全部失败
- **必现现象**：Pod 内 DNS 解析超时或返回 NXDOMAIN/空应答；CoreDNS Pod 不可用；新发起的依赖 DNS 的调用失败
- **不误报现象**：存量已建立连接与已缓存解析不受影响；kubelet/API server 心跳正常（走 IP）
