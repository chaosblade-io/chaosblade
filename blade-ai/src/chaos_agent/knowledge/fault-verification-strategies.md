---
title: "Fault Verification Strategies"
topics:
  - verification patterns
  - kubectl verification mapping
  - delay handling
  - minimal container workarounds
  - fault-specific checks
  - data interpretation pitfalls
  - coverage-verification
  - anomaly-detection
  - application-impact-verification
fault_types:
  - pod-kill
  - cpu-stress
  - network-delay
  - network-loss
  - dns-fault
  - disk-fill
  - disk-io
  - oom
  - node-disk-fill
  - node-cpu-stress
summary: "Fault-specific verification methodology, kubectl verification mapping by fault type, data interpretation pitfalls, coverage/anomaly/application-impact verification. Includes verification command design principles."
---

# 故障验证策略与方法论(Agent 专用)

> **文件用途**: 本文件系统阐述故障验证的分层模型、验证方法设计原则、常见故障场景的验证方案,以及 Agent 在 Layer 2 验证阶段的决策逻辑。帮助 Agent 理解"如何验证故障生效",设计出精确、可执行的验证方案。

> **Agent 快速检索索引**:
> - **验证模型**: 三层验证模型 → [Q1](#q1-为什么需要三层验证模型单层验证不够吗); 设计原则 → [Q2](#q2-如何设计一个有效的-layer-2-验证方案)
> - **Pod 级验证**: CPU 满载 → [Q3](#q3-pod-cpu-满载的验证方案是什么); 内存/OOM → [Q4](#q4-pod-内存压力oom-的验证方案是什么); 网络延迟 → [Q5](#q5-pod-网络延迟的验证方案是什么); 网络丢包 → [Q6](#q6-pod-网络丢包的验证方案是什么); DNS 故障 → [Q7](#q7-pod-dns-故障的验证方案是什么); 磁盘填充 → [Q8](#q8-pod-磁盘填充的验证方案是什么)
> - **Node 级验证**: CPU 满载 → [Q9](#q9-node-cpu-满载的验证方案是什么); 磁盘满 → [Q10](#q10-node-磁盘满的验证方案是什么); 磁盘IO过高 → [Q11](#q11-node-磁盘-io-过高的验证方案是什么)
> - **失败处理**: 验证失败处理 → [Q12](#q12-如果-layer-2-验证失败agent-应该如何处理); 超时区分 → [Q13](#q13-如何区分验证失败和验证超时)
> - **Skill 规范**: 验证方法编写 → [Q14](#q14-skill-的验证方法章节应该包含哪些内容)
> - **数据陷阱**: 数据解读常见陷阱 → [Q15](#q15-layer-2-验证中常见的数据解读陷阱有哪些)
> - **覆盖验证**: 覆盖率验证 → [Q16](#q16-如何验证故障注入的覆盖率是否完整); 异常指标检测 → [Q17](#q17-如何检测和调查非预期指标变化); 应用影响验证 → [Q18](#q18-如何验证应用层面的故障影响)

---

## 一、验证的分层模型回顾

### Q1: 为什么需要三层验证模型?单层验证不够吗?

**A1**: 三层验证模型是故障验证的核心框架，详见 `chaos-engineering-principles.md` Q7-Q8 的完整阐述。

简要对照：
- **Layer 1（注入动作验证）**: 确认 ChaosBlade 实验创建成功 → 快速过滤无效注入
- **Layer 2（现象验证）**: 通过 kubectl 确认故障现象确实出现 → 穿透 blade 状态抽象，看到真实系统状态
- **Layer 3（影响验证）**: 横向对比，确认影响范围可控 → 验证爆炸半径

Agent 验证策略：必须执行 Layer 1 + Layer 2；Layer 3 可选（取决于 Skill 定义）。验证失败时触发回滚（blade destroy）。

---

## 二、Layer 2 验证的设计原则

### Q2: 如何设计一个有效的 Layer 2 验证方案?

**A2**: 有效的 Layer 2 验证方案应遵循以下五个原则:

#### 2.1 针对性原则

验证方法必须针对具体的故障类型,而不是通用的"检查系统是否正常"。

**示例对比**:
- ❌ **通用验证**:`kubectl get pods -n default`(只检查 Pod 是否存在)
- ✅ **针对性验证**(Pod CPU 满载):`kubectl top pod my-pod -n default`,断言 CPU 使用率 > 80%

> **🤖 Agent 的实现**:
> - Skill 的 SKILL.md 中应包含"验证方法"章节,明确列出推荐的 kubectl 命令和预期输出
> - Agent 在 Layer 2 验证阶段,读取该章节,生成验证计划
> - 不同故障类型的验证方法差异很大,不能套用统一模板

---

#### 2.2 可量化原则

验证结果应该是可量化的指标,而不是主观判断。

**示例对比**:
- ❌ **主观判断**:"应用响应变慢了"
- ✅ **可量化**:"P99 延迟从 100ms 增加到 3000ms"、"CPU 使用率从 10% 增加到 95%"

> **🤖 Agent 的实现**:
> - 优先使用 `kubectl top`、`kubectl get -o json` 等返回数值型输出的命令
> - 解析 JSON 或表格输出,提取关键指标(如 CPU 百分比、内存字节数、延迟毫秒数)
> - 将提取的指标与阈值比较,做出通过/失败的判断

---

#### 2.3 多源交叉验证原则

单一数据源可能不可靠,应结合多个数据源交叉验证。

**示例**:Pod OOM 验证
- **数据源 1**:`kubectl get pod -o json` → 检查 `exitCode=137`、`reason=OOMKilled`
- **数据源 2**:`kubectl describe pod` → 检查 Events 中是否有 OOMKilling 事件
- **数据源 3**:`kubectl top pod` → 检查内存是否接近 limit
- **数据源 4**:`kubectl logs --previous` → 检查容器崩溃前的日志,是否有 out of memory 记录

> **🤖 Agent 的实现**:
> - Skill 中应提供多个验证命令,形成验证链条
> - Agent 依次执行这些命令,综合判断
> - 如果某个数据源不可用(如精简镜像没有 top 命令),尝试备选方案

---

#### 2.4 时序性原则

验证应在注入后的合理时间窗口内执行,过早或过晚都可能得到错误结论。

**示例**:
- **过早验证**:注入后立即检查,但 chaos 进程可能还在启动中,CPU 尚未升高
- **过晚验证**:注入后等待太久,Pod 可能已被 HPA 扩容或自愈机制重建,故障现象消失

> **🤖 Agent 的实现**:
> - 注入成功后,等待短暂的时间(如 **2-5 秒**),让故障生效
> - 然后执行 Layer 2 验证
> - 如果验证失败,可以重试 **1-2 次**(每次间隔 3-5 秒),排除时序问题
> - 如果多次重试仍失败,判定为验证失败,触发回滚

---

#### 2.5 可回滚原则

验证过程中不应引入新的副作用,确保验证失败时可以安全回滚。

**示例**:
- ❌ **有副作用的验证**:`kubectl exec my-pod -- rm -rf /data/*`(删除了数据,无法恢复)
- ✅ **无副作用的验证**:`kubectl exec my-pod -- df -h`(只读操作,不影响系统状态)

> **🤖 Agent 的实现**:
> - Layer 2 验证应只使用**只读命令**(get、describe、top、logs、exec 中的查询类命令)
> - 如果需要执行有副作用的命令(如清理磁盘填充文件),应在**恢复阶段**执行,而不是验证阶段
> - 验证失败时,**立即调用** `blade destroy`,不执行额外的清理操作

---

## 三、常见故障场景的验证方案

> **注**: 以下各故障场景仅提供验证方法论（方法名称、验证目标、判定标准、失败原因），具体验证命令见对应技能用例的「注入验证」章节。若无技能用例，使用 `kubectl(subcommand="top"/"exec"/"logs")` 并参考 kubectl-guide.md。

### Q3: Pod CPU 满载的验证方案是什么?

**A3**:

**Layer 2 验证**:

**方法 1:通过 kubectl top 验证 CPU 使用率**
预期:CPU 使用率接近 limit(如 limit=500m,实际使用 450-500m)

**JSONPath 断言**:
获取 CPU limit,然后与 top 输出比较

**方法 2:通过 kubectl exec 验证 chaos 进程存在**
预期:输出中包含 chaos_cpu 进程,且 CPU 占用高

**方法 3:通过应用日志验证延迟增加**
预期:日志中出现 timeout、slow request、high latency 等关键词

**Layer 3 验证**(可选):
- 横向对比:同 Deployment 的其他 Pod CPU 是否正常。预期:只有目标 Pod CPU 高,其他 Pod CPU 正常(< 20%)
- 验证 HPA 是否扩容。预期:如果 currentReplicas < maxReplicas 且 CPU 持续高,currentReplicas 应增加

**验证失败的可能原因**:
- Pod 的 CPU limit 设置过大(如 4 核),chaos 进程无法占满
- Pod 所在节点资源紧张,chaos 进程被节流
- 应用本身是 CPU 密集型,已经占用了大部分 CPU,chaos 进程无法进一步提升

> **🤖 Agent 的决策逻辑**:
> - 如果 `top pod` 显示 CPU < 50% limit,判定为验证失败
> - 重试 1-2 次,排除时序问题
> - 如果仍失败,调用 `blade destroy` 回滚,记录失败原因到实验历史

---

### Q4: Pod 内存压力/OOM 的验证方案是什么?

**A4**:

**Layer 2 验证**:

**方法 1:通过 kubectl top 验证内存使用率**
预期:内存使用接近 limit(如 limit=512Mi,实际使用 480-512Mi)

**方法 2:通过 kubectl get pod -o json 验证 OOMKilled**
预期:返回 "OOMKilled"；返回 137(128+9,SIGKILL)；restartCount > 0(表示容器已重启)

**方法 3:通过 kubectl describe 验证 Events**
预期:Events 中出现 "OOMKilling" 或 "Memory cgroup out of memory"

**方法 4:通过 kubectl logs --previous 验证崩溃前日志**
预期:日志中出现 "out of memory"、"Killed"、"signal 9" 等关键词

**Layer 3 验证**(可选):
- 验证 Deployment 是否自动重建 Pod。预期:availableReplicas 短暂下降后恢复
- 验证新 Pod 是否正常启动。预期:新 Pod 处于 Running 状态,READY=1/1

**验证失败的可能原因**:
- `--mem-size` 参数设置过小,未达到 memory limit
- Pod 没有设置 memory limit,导致可以无限使用内存,不会触发 OOMKill
- 应用在内存压力下主动降级(如减少缓存),避免了 OOM

> **🤖 Agent 的决策逻辑**:
> - 如果目标是验证 OOM,应检查 `lastState.terminated.reason == "OOMKilled"`
> - 如果目标是验证内存压力(不一定要 OOM),应检查 `top pod` 中内存接近 limit
> - 根据 Skill 中的验证要求,选择合适的断言条件

---

> **⚠️ 网络故障通用注意事项**（Q5 网络延迟、Q6 网络丢包均适用）:
> - **localhost 不受 tc 规则影响**: ChaosBlade 网络故障注入底层使用 Linux `tc`(traffic control)，`tc` 规则作用于网络接口(如 eth0)但不影响 localhost(127.0.0.1)回环流量。验证时必须使用 Pod 的 ClusterIP 或 Service DNS 名称，严禁使用 localhost 测试连通性
> - **Readiness Probe 兼容性**: pod-network 故障是否导致 Endpoints 移除取决于目标 Pod 的 Readiness Probe 类型:
>   - `exec` 类型探针: 在容器内通过 localhost 执行，**不受** tc 规则影响 → Pod 保持 Ready → Endpoints **不会**移除
>   - `httpGet`/`tcpSocket` 类型探针(端口在受影响范围内): **可能**因网络故障而失败 → Pod 变为 NotReady → Endpoints 被移除
>   - 验证前必须通过 `kubectl describe pod <pod>` 确认探针类型，据此调整验证预期

### Q5: Pod 网络延迟的验证方案是什么?

**A5**:

**Layer 2 验证**:

**方法 1:通过 kubectl exec ping 验证延迟**
预期:rtt min/avg/max/mdev 中的 avg 接近注入的延迟值(如 3000ms)

**注意事项**:
- `<target-ip>` 应是集群内的另一个 Pod IP 或 Service ClusterIP
- 不要 ping 外部地址(如 8.8.8.8),因为延迟可能受外部网络影响
- 如果目标 Pod 使用 distroless 镜像,可能没有 ping 命令,需换用其他方法

**方法 2:通过 kubectl exec curl 验证 HTTP 延迟**
预期:time_total 接近注入的延迟值

**方法 3:通过应用日志验证超时**
预期:日志中出现 "timeout"、"i/o timeout"、"deadline exceeded"、"connection timed out" 等关键词

**方法 4:通过 kubectl exec ss/netstat 验证连接状态**
预期:可以看到连接处于 ESTABLISHED 状态,但可能有大量重传

**Layer 3 验证**(可选):
- 验证调用链下游是否受影响。预期:上游服务的日志中出现 retry、fallback、circuit breaker open 等记录
- 验证 Service 整体错误率。预期:Endpoints 非空(网络延迟不会导致 Pod 从 Endpoints 移除,除非健康检查失败)

**验证失败的可能原因**:
- 注入的延迟值过小(如 10ms),被网络抖动掩盖
- **(见上方网络故障通用注意事项)** localhost 不受 tc 规则影响 + Readiness Probe 兼容性问题

> **🤖 Agent 的决策逻辑**:
> - 解析 ping 或 curl 的输出,提取延迟值(单位 ms)
> - 如果延迟值 < 注入值的 50%,判定为验证失败(允许一定误差)
> - 重试 1-2 次,排除网络抖动
> - 如果仍失败,调用 `blade destroy` 回滚

---

### Q6: Pod 网络丢包的验证方案是什么?

**A6**:

**Layer 2 验证**:

**方法 1:通过 kubectl exec ping 验证丢包率**
预期:输出中包含 "X% packet loss",X 接近注入的丢包率(如 50%)

**解析示例**:
- 提取 "50% packet loss",与注入的 `--percent 50` 比较

**方法 2:通过 kubectl exec curl 验证连接失败**
预期:部分请求失败,返回 "Connection reset by peer"、"Operation timed out" 等错误

**方法 3:通过应用日志验证连接重置**
预期:日志中出现 "connection reset"、"broken pipe"、"no route to host"、"retry" 等关键词

**Layer 3 验证**(可选):
- 验证重试机制是否生效。预期:上游服务日志中出现 "retrying request"、"attempt 2/3" 等记录
- 验证熔断器是否触发。预期:如果丢包率高且持续时间长,熔断器可能打开,日志中出现 "circuit breaker open"

**验证失败的可能原因**:
- 丢包率设置过低(如 5%),被 TCP 重传掩盖,应用层感知不到
- **(见上方网络故障通用注意事项)** localhost 不受 tc 规则影响 + Readiness Probe 兼容性问题
- 应用层有强大的重试机制,自动恢复了丢包导致的失败

> **🤖 Agent 的决策逻辑**:
> - 解析 ping 输出,提取丢包率百分比
> - 如果丢包率 < 注入值的 50%,判定为验证失败
> - 重试 1-2 次,排除偶然性
> - 如果仍失败,调用 `blade destroy` 回滚

---

### Q7: Pod DNS 故障的验证方案是什么?

**A7**:

**Layer 2 验证**:

**ChaosBlade pod-network dns 的实现机制**:修改目标 Pod 的 /etc/hosts 文件,添加 `<forged-ip> <domain> #chaosblade` 条目。这意味着故障效果只在使用系统解析器(getaddrinfo/gethostbyname)时才会生效。

**方法 1(首选):通过 kubectl exec cat /etc/hosts 验证劫持条目**
预期:/etc/hosts 中包含 `#chaosblade` 标记的条目,如 `1.1.1.1 example.com #chaosblade`

**方法 2(效果验证):通过 kubectl exec ping/wget/curl 验证域名解析到伪造 IP**
预期:ping 输出 `PING example.com (1.1.1.1)`;wget 连接到伪造 IP(可能返回 403/连接拒绝)

**方法 3:通过应用日志验证解析异常**
预期:日志中出现 "Connection refused"、"Unknown host"、"连接超时" 等关键词
注意:仅当目标应用实际使用被劫持域名时,此方法才有效

**方法 4:验证其他域名不受影响**
预期:ping <其他域名> 解析到正常 IP,证明劫持仅针对特定域名

**nslookup/dig 不适用于此故障类型**:
nslookup 和 dig 直接查询 DNS 服务器,完全绕过 /etc/hosts 文件。因此它们返回的始终是真实 DNS 记录,而非 /etc/hosts 中的劫持条目。使用 nslookup/dig 验证此类型 DNS 劫持会得到"故障未生效"的错误结论。

**Layer 3 验证**(可选):
- 验证 DNS 缓存是否生效。预期:如果应用有 DNS 缓存,第二次查询可能仍然成功(缓存未过期)
- 验证 CoreDNS 本身是否正常(不应受到影响)。预期:CoreDNS Pod 处于 Running 状态(验证故障只影响目标 Pod,不影响集群 DNS)

**验证失败的可能原因**:
- 应用使用了 IP 直连而非域名,DNS 故障不影响应用
- 应用有 DNS 缓存,缓存未过期前仍然可以解析
- 注入的 `--domain` 参数与应用实际使用的域名不匹配
- 使用了 nslookup/dig 验证(这两种工具绕过 /etc/hosts,无法检测此类型 DNS 劫持)

> **Agent 的决策逻辑**:
> - 首选 `cat /etc/hosts` 确认劫持条目(直接证据),再用 `ping` 或 `wget` 确认效果(应用层证据)
> - 如果目标应用不依赖被劫持域名,标记应用影响验证为 skipped 并建议用户选择应用实际使用的域名
> - 不要使用 `nslookup` 或 `dig` 验证 ChaosBlade DNS 故障

---

### Q8: Pod 磁盘填充的验证方案是什么?

**A8**:

**Layer 2 验证**:

**方法 1:通过 kubectl exec df 验证磁盘使用率**
预期:Use% 接近 100%(如 95-100%)

**解析示例**:
- 提取 "98%",与预期(接近 100%)比较

**方法 2:通过 kubectl exec ls 验证填充文件存在**
预期:输出中包含大文件(如 1G 大小的 chaos_fill_xxx)

**方法 3:通过应用日志验证写入失败**
预期:日志中出现 "no space left on device"、"write error"、"disk full"、"ENOSPC" 等关键词

**方法 4:通过 kubectl exec touch 验证无法创建新文件**
预期:返回错误 "No space left on device"

**Layer 3 验证**(可选):
- 验证同节点其他 Pod 是否受影响。预期:只有目标 Pod 的挂载卷被填充,其他 Pod 不受影响(除非共享同一 PV)
- 验证日志轮转机制是否生效。预期:如果应用有日志轮转,旧日志文件应被清理,释放部分空间

**验证失败的可能原因**:
- `--size` 参数设置过小,未达到磁盘容量上限
- 目标路径 `/data` 不是 Pod 挂载的 Volume,而是容器根文件系统,填充可能影响容器运行时
- 应用有自动清理机制(如日志轮转、临时文件清理),抵消了填充效果

> **🤖 Agent 的决策逻辑**:
> - 解析 `df -h` 输出,提取 Use% 字段
> - 如果 Use% < 90%,判定为验证失败
> - 重试 1-2 次,排除文件系统统计延迟
> - 如果仍失败,调用 `blade destroy` 回滚
> - **重要**:如果 `--retain=true`(默认),恢复后需提醒用户手动清理填充文件

---

### Q9: Node CPU 满载的验证方案是什么?

**A9**:

**Layer 2 验证**:

**方法 1:通过 kubectl top node 验证 CPU 使用率**
预期:CPU 使用率接近注入值(如 90%)

**解析示例**:
- 提取 "90%",与注入的 `--cpu-percent 90` 比较

**方法 2:通过 kubectl describe node 验证 Conditions**
预期:Conditions 中 Ready=True(除非 CPU 高到影响 kubelet)

**方法 3:通过 kubectl top pod 验证同节点 Pod 受影响**
预期:同节点上的 Pod CPU 使用率可能升高(因为 CPU 竞争)

**Layer 3 验证**(可选):
- 验证调度器是否避开该节点。预期:新 Pod 不被调度到 worker-1(如果其他节点有空闲资源)
- 验证 HPA 是否因节点 CPU 高而扩容。预期:如果 Pod CPU 因节点竞争而升高,HPA 可能触发扩容

**验证失败的可能原因**:
- 节点 CPU 核心数过多(如 32 核),`--cpu-count` 未指定,导致只影响了部分核心
- 节点上运行的负载很轻,即使注入 CPU 满载,整体 CPU% 仍然不高
- 注入的 `--cpu-percent` 参数与实际测量方式不一致(如 blade 按单核计算,kubectl top 按总核计算)

> **🤖 Agent 的决策逻辑**:
> - 解析 `top node` 输出,提取 CPU% 字段
> - 如果 CPU% < 注入值的 70%,判定为验证失败(节点级验证允许更大误差)
> - 重试 1-2 次,排除瞬时波动
> - 如果仍失败,调用 `blade destroy` 回滚

---

### Q10: Node 磁盘满的验证方案是什么?

**A10**:

**Layer 2 验证**:

**方法 1:通过 kubectl describe node 验证 DiskPressure**
预期:Conditions 中 DiskPressure=True

**解析示例**:
- 关键指标:DiskPressure=True

**方法 2:通过 kubectl get events 验证磁盘压力事件**
预期:Events 中出现 "NodeHasDiskPressure"、"insufficient disk" 等记录

**方法 3:通过 kubectl top node 验证磁盘使用率(如果 metrics-server 支持)**
注意:kubectl top 通常只显示 CPU/内存,不显示磁盘。需用 describe 或 df

**替代方法:通过 SSH 登录节点执行 df**(如果 Agent 有节点访问权限)
预期:Use% 接近 100%

**方法 4:通过集群工具 Pod 验证**
- 查找集群内的工具 Pod（如 otel-c-tool）用于执行 ChaosBlade 命令和 kubectl API 检查
- **注意**：otel-c-tool 不挂载 /host，`df -h` 显示的是 overlay 文件系统，不能用于宿主机磁盘验证
- 工具 Pod 可用于：ChaosBlade 命令（blade status/destroy）、kubectl API 检查（describe node、top node）

**方法 5:通过 kubectl debug node/ 验证（宿主机文件系统检查的正确方法）**
- `kubectl debug node/<node> --image=busybox -- sleep 3600` 创建临时调试容器，节点的根文件系统自动挂载到 `/host/` 目录
- **两步法（必须）**：先创建 debug pod（含 `-- sleep 3600` 保活），再 kubectl exec 进去执行命令。裸 busybox 会立即退出（Succeeded phase）
- 所有宿主机路径必须加 `/host/` 前缀（如 `/host/tmp`、`/host/var/log`）
- 不要使用 `-it` 标志（Agent 执行环境为非交互式）
- 调试容器使用后应清理
- **宿主机磁盘/IO/进程验证应使用此方法**：otel-c-tool 不挂载 /host，无法执行 `df -h /host`、`iostat` 等命令
- **版本偏差注意**：如果 `kubectl debug node/` 返回 "NotFound" 错误，可能是 kubectl 客户端与服务端版本差异超过 ±1 minor 版本。此时无法通过 kubectl 工具获取宿主机文件系统访问，需回退到 API 层面检查（Method 1: `kubectl describe node` 看 DiskPressure + Method 2: `kubectl get events` 看磁盘压力事件）。**注意**：`kubectl run` 不在允许的子命令列表中，不可用作备选

**⚠️ Overlay 文件系统陷阱**:
- `kubectl exec <任意Pod> -- df -h` 查看的是**容器 overlay 文件系统**，而非宿主机文件系统
- **otel-c-tool 也受此限制**：它不挂载 /host，`df -h` 显示的仍是 overlay
- 只有 kubectl debug node/ 提供 /host 挂载，能查看宿主机真实磁盘使用情况

**⚠️ 多磁盘分区陷阱 (Multi-Disk Topology)**:
- K8s 区分 **nodefs**（根分区，kubelet/配置文件所在）和 **imagefs**（容器运行时存储：镜像、容器可写层、日志）。它们可能在不同的物理磁盘上
- 当 `kubectl describe node` 的 allocatable 中同时出现 `nodefs` 和 `imagefs` 字段时，说明两者分离
- DiskPressure 可由**任一**文件系统触发。`df -h /host` 只检查 nodefs
- 当 `--path` 指向容器 overlay 路径（如 `/tmp`、`/var/log`），填充写入 imagefs。通过 `df -h /host` 验证将显示无变化
- 正确验证：使用 `df -h`（无路径参数）列出所有挂载的文件系统，识别使用率上升的分区，与注入 `--path` 对应

**`--path` 参数语义**:
- **CRD 模式**（默认，`blade create k8s`）：`--path` 是容器文件系统内的相对路径。`/tmp`、`/var/log` 在容器 overlay 内，由 imagefs 支持；`/var/lib/docker`、`/var/lib/containerd` 如存在于宿主机根分区则由 nodefs 支持
- **exec-os 模式**（宿主机上直接运行 `blade`）：`--path` 是宿主机字面路径。`/tmp` 填充宿主机的 `/tmp`，由 nodefs 支持
- 验证时需根据注入模式和 `--path` 值推理填充作用在哪个分区

**Layer 3 验证**(可选):
- 验证节点上的 Pod 是否受影响。预期:部分 Pod 可能处于 Pending 或 FailedMount 状态
- 验证 kubelet 日志。预期:日志中出现 "disk pressure"、"evicting pods" 等记录

**验证失败的可能原因**:
- `--size` 参数设置过小,未达到磁盘容量上限
- 填充的路径不是节点根文件系统,而是某个挂载卷,不影响节点级别的 DiskPressure
- 节点有大容量磁盘(如 1TB),填充 10GB 不足以触发 DiskPressure
- 填充路径（via `--path`）对应的是 imagefs 分区，但 `df -h /host` 只检查 nodefs 分区，导致误判为填充无效
- kubectl debug 因版本偏差失败，未能获取宿主机文件系统信息

> **🤖 Agent 的决策逻辑**:
> - 优先检查 `describe node` 中的 DiskPressure 条件
> - 如果 DiskPressure=False,但 `df -h` 显示 Use% > 90%,说明填充生效但未触发 kubelet 的压力阈值
> - 如果 `df -h /host` 显示使用率无变化，执行 `df -h`（无路径参数）查看所有分区，确认填充是否作用在 imagefs 上
> - 根据 Skill 中的验证要求,选择合适的断言条件(是要求 DiskPressure=True,还是只要求磁盘使用率高)

### Q11: Node 磁盘 IO 过高的验证方案是什么?

**A11**: Node 磁盘 IO 过高的验证分三层指标:

**节点级指标（必须验证）**:

| 指标 | 标准命令 | BusyBox 备选 | 判定标准 |
|------|---------|-------------|---------|
| %util | `iostat -xd 1 3` | 不支持 | 接近 100% |
| tps / 吞吐量 | 同上 | `iostat -d -k 1 3` | 显著高于基线 |
| %iowait | `iostat -c 1 3` | 同上（支持） | 显著升高（如 >10%）|
| dd 进程 | `ps aux \| grep dd` | `ps \| grep dd` | ChaosBlade dd 进程存在 |

**Pod 级指标（应验证）**:

| 指标 | 命令 | 判定标准 |
|------|------|---------|
| 写入延迟 | `kubectl exec <pod> -- dd if=/dev/zero of=/tmp/test bs=1M count=100` | 耗时显著增加 |
| 磁盘 Events | `kubectl describe pod <pod>` | 出现 IO 相关事件 |

**验证优先级**:
1. `iostat -c 1 3` 确认 %iowait 升高（最通用，BusyBox 也支持）
2. `ps | grep dd` 确认 dd 进程运行（直接证据）
3. `iostat -d -k 1 3` 确认 tps/吞吐量变化（增量数据）
4. Pod 级 dd 测试确认读写延迟影响

**注意事项**:
- BusyBox `iostat` 不支持 `-x` 标志，无法直接获取 %util
- `iostat -d` 的累积行可能出现整数溢出（如 922337203685...），应只关注增量间隔（第 2 行起）
- `iostat` 必须指定间隔和次数（如 `-k 1 3`），否则只输出开机以来的累积平均

> **🤖 Agent 的决策逻辑**:
> - 优先尝试 `iostat -c 1 3`（BusyBox 兼容），确认 %iowait 升高
> - 如果 `iostat -x` 报 "unrecognized option"，立即切换到 BusyBox 备选方案，不要重复尝试 `-x`
> - `ps | grep dd` 发现 ChaosBlade 的 dd 进程是故障生效的直击证据
> - Pod 级验证应选择同节点上的 Running Pod 执行 dd 写入测试

---

## 三-B、blade_status 与 blade_query_k8s 选用规则

blade-ai 有两个查询实验状态的工具，用途不同：

| 工具 | 数据来源 | 返回值 | 用途 |
|------|---------|--------|------|
| `blade_status` | 本地 CLI 侧 | `Status="Running"/"Destroyed"/"Error"` | 确认实验创建/销毁是否成功 |
| `blade_query_k8s` | K8s 集群侧 CRD | `statuses[]` 数组（per-resource 详情、affected_count） | 确认影响了哪些资源、覆盖率检查 |

**选用规则**：

| 场景 | 用哪个 | 原因 |
|------|--------|------|
| 注入后确认实验创建成功 | `blade_status` | 本地查询即可，无需集群侧详情 |
| 销毁后确认实验已清除 | `blade_status` | 同上 |
| 验证覆盖率（受影响资源数） | `blade_query_k8s` | 返回 `statuses[]` 数组，可统计 `affected_count` |
| 诊断部分注入失败 | `blade_query_k8s` | 可查看每个 target 的 status 是 success/fail |
| 获取受影响资源的具体列表 | `blade_query_k8s` | `blade_status` 不返回 per-resource 信息 |

> **💡 Agent 使用提示**：
> - Layer 1 验证优先用 `blade_status` 做快速检查
> - 需要覆盖率数据或诊断注入问题时，切换到 `blade_query_k8s`
> - `blade_query_k8s` 的结果包含了 `blade_status` 的全部信息，但响应体积更大、耗时更长

### blade_query_k8s 输出格式示例

```json
{
  "code": 200,
  "success": true,
  "result": {
    "uid": "abc123def456",
    "statuses": [
      {
        "state": "Success",
        "kind": "pod",
        "identifier": "default/node-name/pod-name/container-name/docker"
      },
      {
        "state": "Success",
        "kind": "pod",
        "identifier": "default/node-name2/pod-name2/container-name2/docker"
      }
    ]
  }
}
```

**关键字段解读**：
- `result.statuses[].state`：每个受影响资源的状态（`Success` / `Error`）。部分注入失败时，部分条目为 `Error`
- `result.statuses[].kind`：资源类型（`pod` / `node`）
- `result.statuses[].identifier`：格式为 `namespace/node/pod/container/runtime`，可定位具体受影响资源
- `statuses` 数组为空或不存在：CRD 可能尚未就绪，等待几秒后重新查询

---

## 四、验证失败的处理策略

### Q12: 如果 Layer 2 验证失败,Agent 应该如何处理?

**A12**: Layer 2 验证失败的处理流程:

> **分析+决策**: 失败原因分类和重试/回滚决策逻辑详见 `failure-modes.md` Mode 3（Verification Failure）和 `verification-heuristics.md`。以下仅补充**执行层面的操作规程**。

**回滚执行**:

```bash
blade destroy <uid>
```

- 确认 `blade status --uid <uid>` 返回 Status="Destroyed"（`blade status` 不支持 `--kubeconfig` flag，Agent 内部通过环境变量传递）
- 记录失败原因到实验历史(Operational Memory)
- 返回明确的错误提示给用户,包括:
  - 注入的故障类型和目标
  - 验证失败的详细信息(如"CPU 使用率仅为 15%,预期 > 80%")
  - 建议的排查步骤(如"检查 Pod 的 CPU limit 设置是否过大")

**知识库更新**:

- 如果发现了新的失败模式(如某种镜像不支持 chaos 进程),记录到 MEMORY.md
- 如果 Skill 中的验证方法有误,标记该 Skill 需要修订

---

### Q13: 如何区分"验证失败"和"验证超时"?

**A13**: 

**验证失败**:
- 验证命令执行成功,但输出不符合预期
- 例如:`kubectl top pod` 返回 CPU=10%,预期 > 80%
- **处理**:立即回滚,记录失败原因

**验证超时**:
- 验证命令执行超时(如 `kubectl exec` 超过 60 秒无响应)
- 可能原因:目标 Pod 无响应、网络中断、kubelet 故障
- **处理**:
  - 首先检查 `blade status --uid <uid>`,确认实验状态
  - 如果 Status="Running",可能是验证命令本身的问题,尝试备选验证方法
  - 如果 Status="Error" 或查询超时,可能是 chaosblade-operator 异常,强制回滚
  - 记录超时信息,作为诊断线索

> **🤖 Agent 的实现**:
> - 每个验证命令设置合理的超时时间(如 kubectl exec 超时 60 秒)
> - 超时时捕获异常,区分是命令超时还是实验异常
> - 如果是命令超时,尝试简化的验证方法(如用 `kubectl get` 替代 `kubectl exec`)
> - 如果所有验证方法都超时,判定为严重异常,强制回滚并告警

---

## 五、Skill 中的验证方法编写规范

### Q14: Skill 的"验证方法"章节应该包含哪些内容?

**A14**: 一个完整的"验证方法"章节应包含以下内容:

**1. Layer 1 验证指令**
```markdown
### Layer 1 验证

执行 `blade status --uid <uid>`,确认 Status="Running"。
```

**2. Layer 2 验证命令列表**
```markdown
### Layer 2 验证

按顺序执行以下命令,至少有一项通过即视为验证成功:

**方法 1: 通过 kubectl top 验证 CPU 使用率**
```bash
kubectl top pod {{target_name}} -n {{namespace}}
```
预期输出:CPU 使用率 > 80% 的 limit 值。

**方法 2: 通过 kubectl exec 验证 chaos 进程**
```bash
kubectl exec {{target_name}} -n {{namespace}} -- ps aux | grep chaos
```
预期输出:包含 chaos_cpu 进程。

**方法 3: 通过应用日志验证延迟**
```bash
kubectl logs {{target_name}} -n {{namespace}} --tail=50
```
预期输出:日志中出现 "timeout"、"slow"、"latency" 等关键词。
```

**3. 验证失败的可能原因**
```markdown
### 验证失败排查

如果验证失败,检查以下可能原因:
1. Pod 的 CPU limit 设置过大(如 4 核),chaos 进程无法占满
2. Pod 所在节点资源紧张,chaos 进程被节流
3. 注入后等待时间不足,chaos 进程尚未完全启动
```

**4. Layer 3 验证(可选)**
```markdown
### Layer 3 验证(可选)

如果需要验证影响范围,执行:
```bash
kubectl top pod -l app={{app_label}} -n {{namespace}}
```
预期输出:只有目标 Pod CPU 高,其他副本 CPU 正常。
```

**5. 恢复后的清理步骤**
```markdown
### 恢复后清理

实验销毁后,无需额外清理。如果使用 `--retain=true` 填充磁盘,需手动删除填充文件:
```bash
kubectl exec {{target_name}} -n {{namespace}} -- rm -f /data/chaos_fill_*
```
```

> **🤖 Agent 的使用方式**:
> - LLM 读取"验证方法"章节,解析出验证命令列表
> - 依次执行这些命令,解析输出,判断是否符合预期
> - 如果所有方法都失败,判定为验证失败,触发回滚
> - 如果至少有一种方法通过,判定为验证成功,继续后续流程

### Q15: Layer 2 验证中常见的数据解读陷阱有哪些？

**A15**: Layer 2 验证依赖 kubectl exec 获取的指标数据，以下陷阱可能导致误判：

**1. 累积计数器溢出**

BusyBox iostat、/proc/diskstats 等数据源可能因整数溢出出现异常大值（如 tps > 10^8）。
判断标准：数值明显超出物理设备能力 → 标记为溢出，不作为正证。
正确做法：关注增量间隔数据（跳过第1行累积值），仅用增量判断当前状态。

**2. 预期为非零但实际为零**

当 skill case 预期某指标应升高（如 iowait 升高、CPU 使用率升高、进程数增加），
但实际测量值接近零或为零时，这是故障未生效的强反证。
常见错误：用推测性解释（如"异步IO"、"内核缓冲"、"调度优化"）合理化零值。
正确做法：除非有直接证据支持该解释（如独立的日志输出证明异步IO模式），
否则应接受零值作为反证，而非合理化。

**3. 进程缺失**

当故障类型依赖特定进程（如 dd、stress-ng、chaos_*）产生效果时，
ps/grep 未找到该进程是故障未生效的直接证据。
注意：ps 输出可能截断长命令，用 `ps aux` 或 grep 完整命令名。

**4. 采样间隔不一致**

iostat、top 等工具的第一次输出通常是开机以来的累积值，不代表当前状态。
正确做法：至少采集 2-3 个间隔，跳过第一个，用后续间隔做判断。

> **🤖 Agent 的使用方式**:
> - 读取命令输出时，先判断数据是否属于上述陷阱场景
> - 对异常数据标记 `[ANOMALY]`，不作为正证使用
> - 对零值/缺失型反证，不使用推测性解释合理化，除非有直接佐证

---

## 五-B、验证覆盖与完整性

### Q16: 如何验证故障注入的覆盖率是否完整?

**A16**: 覆盖率 = 实际受影响资源数 / 期望目标资源数。覆盖率不完整意味着故障注入未覆盖所有预期目标。

**数据来源**:
- Layer 1 的 `blade_query_k8s` 返回 `statuses[]` 数组，包含每个受影响资源的状态
- `affected_count` = `statuses[]` 的长度，表示实际受影响的资源数
- 期望目标数 = 标签选择器匹配的 Pod/Node 总数

**如何确定期望目标数**:
```bash
# Pod 级别
kubectl get pods -l <label> -n <ns> --no-headers | wc -l
# 或从故障上下文中获取 target.names 的长度
```

**常见覆盖率不足原因**:
1. **ChaosBlade 默认单目标**: `blade create k8s pod-* --labels <label>` 默认只选择 **1 个**匹配资源。若要覆盖所有匹配资源，需指定 `--effect-count` 参数
2. **Node 级别故障**: `blade create k8s node-*` 通常只影响指定节点，不受此限制
3. **CRD 尚未就绪**: `blade_query_k8s` 返回的 `statuses[]` 可能在注入后短时间内为空

**决策规则**:
- 覆盖率 = 100% → 正常，无需额外处理
- 覆盖率 < 100% → 在 VERIFICATION_RESULT → Warnings 中报告覆盖率比例（如 "Coverage: 1/3 pods affected"）
- 覆盖率 < 100% 但已知 ChaosBlade 默认行为 → 作为 Warning 报告，不降级 Layer2 状态

**示例**:
```
affected_count=1, target.names=["pod-a", "pod-b", "pod-c"]
→ Warning: "Coverage: 1/3 pods affected. ChaosBlade defaults to single-target unless --effect-count is specified."
```

### Q17: 如何检测和调查非预期指标变化?

**A17**: 非预期指标变化 = 不符合故障注入预期效果的指标变化。例如：内存压力注入后，非目标 Pod 的内存反而下降；CPU 压力注入后，目标 Pod 的 CPU 未升高但磁盘 IO 异常。

**检测方法**:
1. 对比 **所有** 匹配标签的目标资源指标，而非仅关注显示预期效果的资源
2. 记录每个资源的注入前基线和注入后指标
3. 识别与预期方向相反的变化（如预期内存升高但实际下降）

**调查路径**:
```bash
# 1. 检查 Pod 是否重启（重启后 metrics 重置）
kubectl describe pod <name> -n <ns> | grep -A5 "Restart Count"

# 2. 检查近期事件
kubectl get events -n <ns> --sort-by='.lastTimestamp' | tail -20

# 3. 检查 HPA 是否触发扩容（新 Pod 有基线指标）
kubectl get hpa -n <ns>

# 4. 确认 metrics-server 采样间隔（多数集群 15s，二进制默认 60s）
kubectl top pod <name> -n <ns>  # 连续两次执行，观察数值是否波动
```

**常见原因**:
| 异常现象 | 可能原因 | 验证方式 |
|---------|---------|---------|
| 内存反而下降 | Pod 重启（metrics 重置为基线） | `kubectl describe pod` 检查 restartCount |
| CPU 未升高 | 进程未启动或已退出 | `kubectl exec -- ps aux \| grep stress` |
| 指标上下波动 | metrics-server 采样间隔（多数集群 15s） | 连续检查 2-3 次确认趋势 |
| 新 Pod 出现 | HPA 自动扩容 | `kubectl get hpa` |

**决策规则**:
- 异常必须记入 Negative Evidence 段，不可用推测性解释合理化
- 如果异常可被证实为无关因素（如 Pod 重启与故障注入无关），在 Negative Evidence 中说明原因
- 如果异常无法解释，这削弱了验证结论的可信度

### Q18: 如何验证应用层面的故障影响?

**A18**: 应用影响 = 故障注入导致的应用行为可观测退化（延迟增大、错误率上升、可用性下降）。这是验证的最终目标——确认故障的"爆炸半径"确实到达了应用层。

**验证方法**（仅使用 kubectl 工具）:

**方法 1: kubectl logs 搜索异常关键字**
```bash
# 搜索超时/错误/延迟关键字
kubectl logs <pod> -n <ns> --tail=100 | grep -iE "timeout|error|latency|slow|retry|refused"

# 对比注入前后的错误率
kubectl logs <pod> -n <ns> --since=5m | grep -ci "error"
```

**方法 2: kubectl exec 发起请求测试**
```bash
# 从集群内测试 Service 可达性和响应时间
kubectl exec <test-pod> -n <ns> -- curl -s -o /dev/null -w "HTTP %{http_code} Time: %{time_total}s\n" http://<service>:<port>/health

# 如果无 curl，尝试 wget
kubectl exec <test-pod> -n <ns> -- wget -q -O /dev/null --timeout=5 http://<service>:<port>/health
```

**方法 3: kubectl get events 检查应用级事件**
```bash
kubectl get events -n <ns> --field-selector reason=Unhealthy --sort-by='.lastTimestamp'
```

**方法 4: kubectl get endpoints 检查服务端点变化**
```bash
# 网络故障可能导致 Endpoints 变化
kubectl get endpoints <service> -n <ns>
```

**容器缺少工具时的备选方案**:
- 如果容器内无 `curl`/`wget`：使用 `kubectl describe pod` 查看 Events 中的探针失败记录
- 如果无法从 Pod 内发起请求：通过 `kubectl logs` 间接观察应用行为变化
- 如果 Service 无外部端点：检查 `kubectl get endpoints` 变化

**决策规则**:
- Skill 用例要求的应用影响验证步骤**不可省略**，无法执行时标记 `[SKIPPED]` 并说明原因
- 应用影响验证通过 → 加强 Layer2 结论可信度
- 应用影响验证失败（如延迟未增大）→ 需要调查原因（故障效果是否真的传导到应用层）
- 应用影响验证跳过（无可用工具）→ 在 Warning 中说明，不降级 Layer2 状态

---

## 六-A、故障场景→kubectl验证映射

> 迁移自 kubectl-guide.md 第六、七节，与各故障类型的验证方案(Q3-Q11)互补。

### Pod 级故障验证命令组合

| 故障场景 | 推荐验证命令组合 |
|----------|-----------------|
| Pod OOM | `get pod -o json`（exitCode=137, reason=OOMKilled）+ `describe pod`（Events 中 OOMKilling）+ `top pod`（内存接近 limit） |
| Pod CPU 高 | `top pod`（CPU 飙升）+ `exec -- ps aux`（chaos 进程存在）+ `get pod -o wide`（确认节点） |
| Pod 磁盘满 | `exec -- df -h`（磁盘使用率接近 100%）+ `describe pod`（Events 中磁盘相关） |
| Pod 网络延迟 | `exec -- ping`（延迟增大）+ `exec -- ss -tlnp`（连接状态）+ 应用日志 timeout |
| Pod 网络丢包 | `exec -- ping`（丢包率）+ 应用日志 connection reset |
| Pod DNS 故障 | `exec -- cat /etc/hosts`（确认 #chaosblade 劫持条目）+ `exec -- ping <domain>`（解析到伪造 IP；glibc 镜像可用 `getent hosts`）+ 应用日志 resolve failed。**⚠️ 不要用 nslookup/dig——它们绕过 /etc/hosts** |
| Pod 镜像拉取失败 | `get pod -o json`（waiting.reason=ImagePullBackOff）+ `describe pod`（Events 中 ErrImagePull） |
| Pod Terminating 卡住 | `get pod -o json`（deletionTimestamp 非空，phase 不为 Terminated）+ `describe pod`（finalizers / 删除原因） |
| Pod 被删除 | `get pod -o json`（返回 NotFound）或 `get pods -l`（Pod 列表变化） |

### Node 级故障验证命令组合

| 故障场景 | 推荐验证命令组合 |
|----------|-----------------|
| Node CPU 高 | `top node`（CPU 飙升）+ `describe node`（Conditions 正常） |
| Node 内存高 | `top node`（内存飙升）+ `describe node`（MemoryPressure=True）+ `get events`（NodeHasInsufficientMemory） |
| Node 磁盘高 | `describe node`（DiskPressure=True）+ `get events`（NodeHasDiskPressure） |
| Node 不可用 | `get node -o json`（Ready=False）+ `describe node`（Events 中 Kubelet 停止上报）+ 该节点上 Pod 状态 |
| Node 磁盘 IO 高 | `exec` 到 Node 上（如 DaemonSet）查看 io stat，或观察 Pod 启动延迟 |

### Workload / Service 级故障验证命令组合

| 故障场景 | 推荐验证命令组合 |
|----------|-----------------|
| Deployment 副本不一致 | `get deployment -o json`（readyReplicas < replicas）+ `get pods -l`（部分 Pod 异常） |
| HPA 达到上限 | `get hpa -o json`（currentReplicas == maxReplicas）+ `top pod`（CPU/内存触发扩容） |
| DaemonSet 未完全调度 | `get ds -o json`（desiredNumberScheduled != numberReady）+ `get pods -l`（部分 Pending） |
| Service 负载均衡异常 | `get endpoints -o json`（addresses 为空）+ `get svc -o json`（selector 正确）+ `get pods -l`（匹配 selector 的 Pod 状态） |
| Workload 被缩容 | `get deployment -o json`（replicas 减少）+ `get events`（ScaledDown） |

### Service 目标发现规范
当故障目标为 Service 时，必须通过 Service 的 selector 发现匹配的 Pod，禁止猜测 label selector：
- 获取 selector: `kubectl get svc <name> -n <ns> -o jsonpath='{.spec.selector}'`
- 用 selector 查找 Pod: `kubectl get pods -n <ns> -l '<selector-key>=<selector-value>'`
- 禁止凭 Service 名称推断 label（如假设 svc=mysql → app=mysql）

### 验证命令设计原则

1. **先观测再断言**：先通过 `get -o json` 或 `describe` 获取当前状态，再在 LLM 推理中做断言，不要假设输出格式。
2. **多维度交叉验证**：单一指标可能误导，应组合使用状态字段 + Events + 资源指标 + 应用日志。
3. **对比基准状态**：注入前记录 Pod 状态、Events 列表、资源指标作为基准，注入后和基准对比。
4. **关注状态变化而非绝对值**：某些指标本身就在波动，验证时应看"是否出现了预期变化"（如 restartCount 增加、新 Event 产生、CPU 从 10% 涨到 90%）。
5. **优先 JSON 结构化输出**：做程序化断言时优先用 `-o json`；需要人类可读的事件描述时用 `describe`。
6. **善用 label selector**：通过 `-l app=<app>` 批量获取同应用的多个 Pod 状态，而不是逐个查询。
7. **注意时间窗口**：Events 和日志都有时效性，验证时应关注注入时间点附近的新事件，用 `--since=5m` 或按 `lastTimestamp` 排序。
8. **区分容器内和宿主机视角**：`kubectl exec` 看到的是容器内部；`kubectl top` 和 `kubectl get` 看到的是宿主机/集群视角。

---

## 六-B、恢复验证策略

恢复（`blade destroy`）后必须验证故障效果已完全消除，否则可能存在残余影响（如残留进程、未清理的 /etc/hosts 条目、未恢复的 iptables 规则）。

### 恢复验证通用原则

1. **恢复验证 ≠ 反向断言** — 不是简单地把注入验证的断言条件取反，而是确认系统恢复到了**注入前的基线状态**
2. **必须对比基线** — 恢复验证前应记录注入前的关键指标（CPU%、内存%、磁盘使用率、网络延迟等），恢复后对比确认回到基线
3. **等待恢复窗口** — `blade destroy` 执行后，故障效果可能需要 5-30s 才能完全消除（metrics-server 采样间隔多数集群为 15s），不要立即验证
4. **残留检查** — 恢复后必须检查是否有残留：进程残留（stress 进程未退出）、文件残留（/etc/hosts 未清理）、规则残留（iptables/tc 规则未删除）

### 各故障类型的恢复验证方法

| 故障类型 | 恢复验证方法 | 残留检查 |
|---------|------------|---------|
| Pod CPU 满载 | `kubectl top pod` CPU 回到基线 + `exec -- ps aux` 无 stress 进程 | stress-ng/stress 进程是否存在 |
| Pod 内存压力 | `kubectl top pod` 内存回到基线 + Pod 非 OOMKilled 状态 | `/proc/meminfo` 是否恢复正常 |
| Pod 网络延迟 | `exec -- ping -c 3 <target>` 延迟回到基线 | `tc qdisc show` 无 ChaosBlade tc 规则 |
| Pod 网络丢包 | `exec -- ping -c 10 <target>` 丢包率回到 0% | `tc qdisc show` 无 ChaosBlade tc 规则 |
| Pod DNS 故障 | `exec -- cat /etc/hosts` 无 `#chaosblade` 条目 + `ping <domain>` 解析到真实 IP | /etc/hosts 中 `#chaosblade` 条目是否已删除 |
| Pod 磁盘填充 | `exec -- df -h` 使用率回到基线 | 临时填充文件是否已删除 |
| Node CPU 满载 | `kubectl top node` CPU 回到基线 + Node Conditions 无异常 | Node 上 stress 进程是否存在 |
| Node 网络故障 | 节点间 ping 延迟/丢包回到基线 | iptables/tc 规则是否已清理 |
| Node 磁盘填充 | `kubectl describe node` DiskPressure=False + `df -h` 使用率回到基线 | 填充文件是否已删除 |
| Pod 被删除 | `kubectl get pod <name>` Pod 存在且 Running | — |
| Process 被杀 | `exec -- ps aux \| grep <process>` 进程恢复运行 | — |

### 恢复验证失败处理

当恢复验证发现残余影响时：

| 异常现象 | 可能原因 | 处理方式 |
|---------|---------|---------|
| CPU 未回落 | stress 进程残留 | `exec -- kill <pid>` 手动终止残留进程 |
| /etc/hosts 未清理 | blade destroy 未成功移除条目 | `exec -- grep -v '#chaosblade' /etc/hosts > /tmp/hosts && mv /tmp/hosts /etc/hosts`（跨平台；⚠️ Alpine/BusyBox 的 `sed -i` 不兼容 GNU sed，不要用 `sed -i`） |
| tc 规则残留 | ChaosBlade 未成功删除 tc 规则 | `exec -- tc qdisc del dev eth0 root` 手动删除 |
| 磁盘未释放 | 填充文件残留 | `exec -- rm <path>` 手动删除填充文件 |
| Pod 仍处 Evicted 状态 | 节点资源压力未完全消除 | 等待节点资源恢复，或手动删除 Pod 让其重建 |

> **安全底线**: 如果手动清理后残余仍未消除，必须**上报人类操作者**（而不是继续尝试更多破坏性操作），并记录到 Negative Evidence 中。

---

## 六、术语表

| 术语 | 解释 |
|------|------|
| Layer 1 验证 | 通过 `blade status` 确认实验状态 |
| Layer 2 验证 | 通过 kubectl 验证故障现象是否出现 |
| Layer 3 验证 | 横向对比,验证影响范围是否可控 |
| 验证失败 | 验证命令执行成功,但输出不符合预期 |
| 验证超时 | 验证命令执行超时,无法获得结果 |
| 回滚 | 调用 `blade destroy` 停止故障注入 |
| 重试 | 验证失败后,等待片刻再次尝试验证 |
| 自愈 | Kubernetes 自动修复故障的机制,可能掩盖故障现象 |
