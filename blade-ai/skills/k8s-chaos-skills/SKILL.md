---
name: k8s-chaos-skills
description: |
  Kubernetes 故障演练与混沌工程专家。当用户需要进行 K8s 故障注入测试、混沌工程演练、系统韧性验证、故障场景模拟时，立即使用此 skill。

  内置多个标准化故障注入用例，覆盖 Pod（Pending、CrashLoopBackOff、OOM、CPU 满载、内存接近 Limit、磁盘满、Terminating 卡住、镜像拉取失败等）、Workload（副本不一致、HPA 副本达到上限、DaemonSet 未完全调度）、Service（状态异常、负载均衡异常）、Node（宕机、磁盘使用率过高、磁盘 IO 过高、CPU 使用率过高、内存使用率过高）、存储（PVC 挂载 Pending）五个基础层级，并扩展至 Ingress 入口（后端服务不存在、TLS 证书过期）、Webhook 准入（回调服务不可达）、Secret 凭证（内容被篡改）、ConfigMap 配置（内容错误）、ServiceAccount 权限（被撤销）、CronJob 任务（被挂起、连续失败）、StatefulSet 滚动（分区更新卡住）、Node 维护（排空被 PDB 阻塞、污点注入）、Pod HTTP 故障（响应延迟、连接被重置、状态码/响应体/响应头篡改）、Pod 网络故障（NetworkPolicy 误配）、Pod 磁盘（IO 数据静默篡改、inode 耗尽）等准入/入口/配置/工作负载细分层级。

  通过「意图识别 → 用例选择 → 意图提交」的决策树流程，精确匹配用户需求。集群连接（kubeconfig / kubewiz 等）由运行时配置自动决定，无需在对话中向用户索要。

  适用环境：本 skill 只能在 Kubernetes 集群环境上真正注入；如果当前连接的是主机（本地或 SSH 远程的机器）而不是集群，则无法执行，应先提示用户当前环境不匹配、再引导切换到集群环境。

skill_type: fault-injection
scripts:
  - name: list_scenarios.py
    description: "列出当前支持的所有故障演练场景（JSON 结构化输出）"
  - name: inject_cni_exhaust.py
    description: "CNI IP 耗尽注入 — 自动计算副本数，精确耗尽目标节点 IP 而不触发 OutOfpods。参数: --namespace <ns> --node <node> --kubeconfig <path>"
---

# K8s 故障演练

```
意图识别 ──→ 用例选择 ──→ 意图提交
(提取语义维度)(决策树匹配)  (提交意图，执行交给引擎)
```

## 安全红线

以下规则优先级高于一切操作指令，在任何步骤中都不得违反：

- **隔离**：在隔离命名空间或测试集群演练，严禁在生产核心链路注入
- **最小影响**：用精确标签选择器定位目标，范围尽可能小
- **可回滚**：注入前明确回滚方案，确保 30 秒内可恢复
- **故障窗口完整**：自恢复定时器倒计时从**武装时刻**起算——恢复脚本必须先校验后武装，武装与注入必须是紧邻步骤（间隔 ≤ 60 秒），武装后发生任何修复必须重新武装；用户批准的 duration 是故障窗口契约，严禁被调试/计划变更侵蚀，同样严禁擅自延长
- **恢复定时器值单源（双数窗口契约）**：窗口有两个数——**观察窗 `D` = `duration_seconds`**（框架在场义务，即上条严禁侵蚀/擅延的契约值）与**安全网窗 `D+G` = `recovery_timer_seconds`**（故障自身定时器的武装值，G 为程序立法的恢复宽限）。**native/host 自恢复定时器面**里一切**故障自治恢复点**的武装值——`systemd-run --on-active=<recovery-seconds>s` 的 `<recovery-seconds>`、自停 kill/delete 循环的墙钟寿命——**一律取框架单源下发的 `recovery_timer_seconds=<N>s` 整数值**，**禁止自算缓冲**（`20+duration`、"recovery-seconds 取 duration"、裸取 `duration_seconds` 等自推导形一律废止；结构性前置延迟如"先 STOP 后 CONT"的 20s 载体自噬规避量保留，只把其中的窗口量换成 `recovery_timer_seconds`）。这**不是"擅自延长"**：`recovery_timer_seconds` 是程序单源计算、精确相等可守门的立法值，非操作方自由裁量——让框架在观察窗终点（onset+D）主动派发的恢复先于故障自治恢复（onset+D+G）落地。**边界**：blade `--timeout` 与 CR `durationSeconds` 由框架钉定（模板仍写 `<duration>` 声明观察窗，落地值改写为 D+G）；API-write 恢复载体标准件的 `sleep` 定时器按其自身「duration 双轨」立法武装（见 `references/carrier/recovery-carrier.md`，注入方不主动恢复，不适用本条 grace）；观察载体保活（如 `kubectl debug node … -- sleep <duration>`）不是恢复点，仍取 `D`
- **武装载荷单次性（勿重复武装）**：带 PID/状态文件的自恢复载荷（负载循环、压力发生器等）对同一目标容器只下发一次——`kubectl exec` 立即返回的空回执即武装完成。重复下发同一载荷时其开头的 `: > 状态文件` 会截断前一轮记录，使前一轮进程 PID 失联（定时器 kill 不到它，清理由兜底而非机制保证）。不确定是否已落位时，先只读探针读状态文件（`kubectl exec <pod> -- cat <pids-file>`）：文件在 = 已武装，跳过
- **证据文件跨轮轮转（自证链起点必须干净）**：用 `*_INJECTED`/`*_RESTORED` 标记落证据文件（`/tmp/*.evd` 等）自证的用例，注入载荷对证据文件的**首个写入必须用 `>` 截断**（与 PID 文件 `echo $! > *.pid` 对称），链内后续标记（执行期探针写入、定时器 `*_RESTORED`）再用 `>>` 追加。证据文件跨轮常驻 `/tmp`，`>>` 盲追加会把上一轮遗留的完整周期（尤其 `*_RESTORED`）混入本轮，而「含 `*_RESTORED` 即窗口已关 / 双标记齐备 = 注入与还原全程自证」判据会被残留标记假满足、掩盖本轮定时器是否真 fire。走打满/重排变体（INJECTED 提前到填充之前写）时，提前后的首写同样用 `>` 截断。
- **效果证据在窗口内采齐（生效确认硬门禁）**：故障「生效确认」需行为判据（探针探测到的故障效果）与白盒判据（配置/规则/记录快照）二者缺一不可——规则已下发不等于故障已生效，行为判据未采齐即视为**未生效确认**，不得据此给出「生效」结论；效果证据只能在故障存活期内采集，恢复后/拆线后永久不可再采；机制证据（规则快照）不能替代效果证据
- **持续性复测由故障传播特性自导（无固定间隔/次数/探针数）**：生效确认只证明某一刻故障已落地，不证明它在整个窗口持续存活；持续性复测的意义是把「稳态故障」与「传播收敛窗内的瞬时态」区分开——须待传播收敛之后再次观测仍见故障态，才算坐实持续。**观测多久再采、采几次、是否同轮下发，由操作方依该 case 注入的故障形态与传播特性自行理解判断，不设固定间隔、固定次数或固定探针数**（严禁把某个 `time_wait N` / 探针条数当成一刀切硬预算写死）。传播特性从 case 文档的传播滞后描述取值，均为普世机制事实：秒级传播（如 endpoints controller / CNI 策略同步 5-15s、CoreDNS 负缓存约 30s）、分钟级传播（如 kubelet 挂载同步约 60s）、字段/规则/进程型即时生效无传播过程等。有传播过程的故障须让第二次观测跨过传播窗；即时生效无传播的故障无需为「跨窗」而盲等，只需覆盖足以证明持续的观测跨度。复测证据应覆盖白盒存活（配置/记录/规则仍在）+ 行为效果（故障态仍复现）+ 对照（未受累对象仍正常）几类语义，具体几条依故障形态定；效果证据只能在故障存活期内采集，恢复后/拆线后永久不可再采，若发现已恢复则如实报告定时器是否提前触发/是否人工介入
- **拆线不进执行计划**：演练资产拆除（接线移除/对象删除，以及注入逆操作还原——untaint / uncordon / patch 还原 / scale 回基线等一切使故障现象消失的操作，同属拆线类）属于恢复生命周期（框架 recover 或人工），**不是执行计划里的 mutation step**——执行计划的变更步骤只含「接线 + 武装 + 注入」；把拆线写进执行计划会迫使在线等待恢复完成（破坏「下发即退出」的边界）并销毁验证证据载体
- **恢复载体标准件**：API 平面恢复（patch/delete K8s 对象）无宿主时，按 `references/carrier/recovery-carrier.md` 自建载体栈（Pod/SA/Role/RoleBinding 四对象同名、同靶点命名空间、命令式创建）；武装前必须真实 token 验权（禁 can-i --as 假放行），结束必须四连删除并带外核实零残留
- **爆炸半径 scope 枚举映射（规划期填表纪律）**：`blast_radius_scope` 三值枚举按写入集判，node 等非命名空间 primary target 自身的 mutation（cordon/taint）不算扩散——①仅 target 自身 + 声明在写入集内的 ephemeral 载体（node-debugger 等**无新增 RBAC** 的一次性调试载体，跨 ns 也算）→ `target-only`（**边界**：API 平面恢复的**四件套 carrier**（Pod/SA/Role/RoleBinding，新增 namespaced RBAC 授权面）不属此豁免，其 `blast_radius_scope` 由 `references/carrier/recovery-carrier.md` §9 专管 → `namespace-wide`，case 文档 defer §9、勿重复定性）；②含同命名空间其他资源（如 node 靶下删 ns 内 Pod）→ `namespace-wide`；③含跨命名空间**持久**资源写入（如 ClusterRole/ClusterRoleBinding）→ `cluster-wide`。勿在 target-only 与 namespace-wide 之间反复纠结——写入集里除 target 与一次性载体外**只有 ns 内对象**即为 namespace-wide
- **驱逐波及面甄别（容忍三分法，规划期义务）**：驱逐类故障（taint NoExecute / drain / 节点失联驱逐）预检靶名册时按 tolerations 三分：① `operator: Exists` 无 key 无 effect → 全 effect 免疫（系统 DS 常见形态）；② `operator: Exists` 无 key 但带 `effect:` 限定 → **仅该 effect 免疫**（如 `effect: NoSchedule` 对 NoExecute 驱逐不免——部分组件 Pod 是此形态）；③ 仅容忍标准键（node.kubernetes.io/*）或指定 key → 对自定义 key 必驱逐。判读以逐 Pod jsonpath 读 tolerations 字段为准，勿按 workload 类型猜测。免疫组与必驱逐组都必须显式列入预检结论（防误判「驱逐失败」）
- **爆炸半径验证判据 = 逐 Pod 对照自身基线的 delta（非绝对态，规划期义务）**：生产节点基线**从不干净**——常含预存异常 Pod（CrashLoopBackOff 按自身节奏持续自重、ContainerCreating/Terminating 长期卡住）。故爆炸半径交叉确认与恢复验证的判据一律是**逐 Pod 对照自身基线的 delta**：只有「注入新增的 Evicted」或「相对自身基线**新增**的重启漂移」才算失控；**严禁**「同节点 Pod 与基线清单一致（无异常重启）」类绝对态判据——那会把预存 Pod 窗口内的自重误归因为注入。规划期须用每 Pod 一行紧凑 jsonpath 记录同节点各 Pod 的 `containerStatuses[0].restartCount` 基线（`-o wide` 在数十 Pod 节点会截断丢 RESTARTS/状态，且其 RESTARTS 列聚合 initContainer 重启、与主容器 restartCount 不一致，直读容器级 restartCount 天然绕开聚合陷阱），并**显式锁定预存异常 Pod 的基线态**（计划里点名、声明其窗口内按自身节奏的继续漂移属基线内自重、非注入所致，供效果验证勿误判）；节点 Conditions 用定点过滤 jsonpath 只查关注项（云厂商 NPD 十余项自定义 condition 时全量单行 jsonpath 会被输出预算截断→逼出重查）。此判据与框架的副作用检测一致（框架本就按 delta 对照、过滤注入前重启、剥离 exit-0 自然完成），文档判据须与之一致、勿每 run 从零重推
- **判据形态三原则**：① 事件查询的过滤维度必须与事件挂载对象对齐（节点级事件查节点名、Pod 级事件查 Pod 名/命名空间；挂载维度未定时先按 reason 小样确认再收敛——严禁的是无过滤全量拉取，`type`/`reason` 过滤 + grep 收敛形态合法）；② 状态类裁决判据优先 jsonpath 单行输出（截断免疫），describe|grep 多行管道仅作人读展示、不作裁决载体；③ 验证与恢复验证的每条判据必须可执行可断言（悬空软判据删除或补对应命令）
- **选靶论证字段化（规划期纪律）**：节点选靶以字段级查询逐项核验排除维度（控制面 / 系统组件命名空间 / 既有同名污点 / 容量可接纳驱逐 / 反亲和极点），命中即排除、剩余候选 ≥1 即可行——勿做全集群逐节点拓扑算术论证展开（排除维度以单条字段查询回执为准）
- **有监控**：无监控不演练
- **仅限已有用例**：只能执行 `references/catalogue/` 中已有的故障注入用例，严禁自行编造、拼凑或即兴发挥用例中未涉及的故障注入操作。无法匹配时必须明确告知用户「当前不支持该场景」并停止
- **禁止**：业务高峰期演练 / 对 etcd、kube-apiserver 等控制平面注入 / 无备份对 StatefulSet 做破坏性实验

---

## 第一步：意图识别

从用户输入中提取四个**语义维度**（只关心「注入什么故障、打在哪个应用」，不关心「用什么集群凭证」）：

| 维度 | 说明 | 示例 |
|------|------|------|
| **故障层级** | 作用在哪个层面 | Pod / Workload / Service / Node / 存储 / Ingress / Webhook / Secret / ConfigMap / ServiceAccount / CronJob / StatefulSet |
| **故障现象** | 期望模拟的表现 | Pending、OOM、CPU 高 |
| **故障原因** | 具体根因 | 资源不足、配置错误 |
| **目标应用** | 注入目标 | 应用名、命名空间、标签选择器 |

> **不要向用户索要 kubeconfig 路径**：集群连接（kubeconfig / kubewiz 等）由运行时配置自动注入，不属于意图识别范畴。

**层级判断关键词：**

| 关键词 | 层级 |
|--------|-----|
| pod、容器、崩溃、重启、OOM、CPU高、内存高、镜像、挂载、磁盘空间、磁盘IO | Pod |
| 副本、deployment、扩容、缩容 | Workload |
| service、服务发现、负载均衡、端口、endpoints | Service |
| 节点、node、磁盘、宿主机 | Node |

**信息不全时的处理：**
- 用户明确说出故障现象 → 直接进入第二步
- 用户只说大致目标（如「测试容错」） → 提问引导确认四个维度
- 用户描述生产问题要复现 → 从描述提取现象和原因，匹配用例
- 匹配到多个用例 → 列出候选，让用户选择
- 只说了现象没说根因 → 推荐该分类下最常见的用例，并列出其他选项
- 不在覆盖范围 → 明确告知「当前不支持该场景」，停止操作，严禁自行构造用例外的故障注入

---

## 第二步：用例选择（动态发现）

所有用例存储在 `references/catalogue/` 下，通过目录结构和文件命名规约自动发现，无需维护静态索引。

### 命名规约

```
references/catalogue/
├── <故障分类>/                         # 目录名 = 故障层级_故障现象（每个目录对应唯一的 Scope+Target+Action 组合）
│   ├── <故障分类>_<根因A>.md           # 文件名 = 分类名_具体根因
│   ├── <故障分类>_<根因B>.md
│   └── ...
└── ...
```

示例：
- `references/catalogue/Pod_Pending/Pod_Pending_节点资源不足.md`
- `references/catalogue/Pod_磁盘空间使用率过高/Pod_磁盘空间使用率过高_应用日志数据积累.md`（pod-disk fill）
- `references/catalogue/Pod_磁盘IO过高/Pod_磁盘IO过高_磁盘IO读写负载.md`（pod-disk burn）

### 发现流程

**第 1 步：列出所有分类目录，匹配故障现象**

```bash
ls references/catalogue/
```

从返回的目录名中，根据第一步识别的故障现象选择最匹配的目录。目录名本身已包含故障层级和故障现象信息（如 `Pod_Pending`、`service_conditions状态异常`、`节点容器运行时磁盘使用率过高`）。

**第 2 步：列出该目录下所有用例文件，匹配根因**

```bash
ls references/catalogue/<匹配的目录>/
```

从返回的文件名中，根据用户描述的根因选择最匹配的文件。文件名中 `_` 后的部分即为该用例对应的根因。

- 如果用户明确了根因 → 直接选择对应文件
- 如果用户未说明根因 → 将文件列表展示给用户，让用户选择
- 如果没有匹配的文件 → 告知用户「当前不支持该场景」，停止操作

### 新增/删除用例

遵循命名规约即可，无需修改本文件：
- **新增用例**：在对应分类目录下新建 `<分类>_<根因>.md`
- **新增分类**：新建目录 `<故障层级>_<故障现象>/`，在其中添加用例文件
- **删除用例**：直接删除对应 `.md` 文件

### 辅助脚本

- `scripts/list_scenarios.py`：一键列出所有已支持的演练场景（JSON 结构化输出），用于快速总览或对接外部系统
  用法：`execute_skill_script(skill_name="k8s-chaos-skills", script_name="list_scenarios.py")`

---

## 第三步：意图提交（执行交给引擎）

本 skill 的职责到「提交意图」为止。读取用例文件只是为了确认**故障语义与参数**，不是让你自己去执行。

1. 读取 `references/catalogue/<分类目录>/<用例文件>.md`，确认故障语义（层级 / 现象 / 根因）、目标应用与可调参数
2. 与用户确认后，调用 `submit_fault_intent` 提交语义参数（scope / target / action / namespace / 标签 / params）
3. **不要自己运行** `blade` / `kubectl` 等命令去采集基线或注入——**基线采集、故障注入、注入验证、恢复及恢复验证均由执行引擎按用例模板自动完成**
4. 集群连接（kubeconfig / kubewiz 等）由运行时配置自动注入，无需向用户索要路径
5. 所有实验都会设置自动恢复（安全红线，由引擎保证）

> 用例文件中的「演练步骤 / 注入验证 / 注入恢复 / 恢复验证」是给**执行引擎**的模板（需要时可查阅 `references/chaosblade-commands.md` 获取完整参数），本阶段只需理解 WHAT，**不要在提交前自己执行它们**。
