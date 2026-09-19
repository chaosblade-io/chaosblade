**用例名称** 端口被占用 导致 Pod_网络故障

**故障定位**：持续型故障——端口被占用进程持有即故障存活，监听 socket 存在即故障
存在，贯穿整个故障窗口；占用进程终止（实验销毁/定时器 kill）即端口释放、自动恢复。
手段1（ChaosBlade `pod-network occupy`）与手段2（kubectl-native：容器内 nc/socat 抢占
端口）是**并列的注入手段**，底层效果等价（blade 内部也是向目标 Pod 注入端口占用进程），
按环境能力选用：集群装有 ChaosBlade 且 operator 健康 → 手段1；否则手段2。手段2 内部
按容器工具二选一：有 `nc` 走 nc 路径，有 `socat` 走 socat 路径（两者皆无则手段2 判死，
如实上报）。**效果语义澄清（决定验证判据）**：端口占用的故障本质是「其他进程无法
bind 该端口」（`Address already in use`），不是「连接被拒」——占用进程本身在 listen，
连接该端口反而会被 accept。原应用有监听时才伴随「服务不可达」现象；靶无监听应用
（如 sleep 型裸负载）时该项为预期阴性，效果主证一律用 **bind 探针失败**。
`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

**故障现象**：
1. Pod 内服务端口被强制占用，应用无法监听预期端口（重启/重发布报 `Address already in use`）
2. 已有监听应用被强制杀死时（手段1 `--force` / 手段2 pkill 分支），现有连接中断
3. 健康检查可能失败（如探针使用被占端口），导致 Pod 被重启
4. Service 端点无法正常接收流量（仅原有监听应用被杀时）

**资源准备**：
1. 确认目标 Pod 的标签选择器、命名空间，以及**实际容器名**（多容器/临时容器混存时
   `kubectl exec` 必须显式 `-c <容器名>`）
2. **能力探测（决定手段选择）**——确认 ChaosBlade 的 `pod-network` 是否提供 occupy
   action，且 operator 实际健康，**以当次探测为准**：
   ```bash
   blade create k8s pod-network -h
   kubectl get pods -A --no-headers | grep -i chaosblade-operator
   ```
   - Available Commands 列出 `occupy` 且 operator Pod Running → 手段1 可用
   - 无 `occupy` 子命令，或 operator 处于 ImagePullBackOff/CrashLoopBackOff（常见形态：operator 0/1
     与 tool Pod 均 ImagePullBackOff，CR 无人 reconcile）→ 手段1
     判死，用手段2，不要在手段1 上空转
3. **容器工具探测（决定手段2 路径选型）**——nc/socat 二有其一即可用，
   netstat/fuser 不必有：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'for t in nc socat netstat ss fuser pkill; do command -v $t >/dev/null 2>&1 && echo "$t YES" || echo "$t NO"; done'
   ```
   典型靶（kone-runtime 类镜像）：**无 nc、无 netstat、无 fuser，有 socat 1.7.4 与
   ss** → socat 路径 + ss 判据。busybox 轻量镜像则通常反过来（有 nc 无 socat）。
   **判据工具规则：有 netstat 用 `netstat -tlnp`，无 netstat 有 ss 用 `ss -tlnp`——
   两者输出语义等价（LISTEN 行 + 进程归属）；两者皆无则本用例验证判据不可达，
   如实上报**
4. **原应用监听形态探测（决定现象面与 pkill 分支）**：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- ss -tlnp   # 或 netstat -tlnp
   ```
   - 有应用监听目标端口 → 记录进程名与 PID（手段2 的 pkill 分支按此杀原进程），
     验证含「服务不可达」现象
   - **无任何监听（sleep 型裸负载即此形态）→ pkill 分支跳过**（没有原进程
     可杀），「服务不可达/应用报错/健康检查失败」全部按预期阴性处理，效果主证改用
     bind 探针（见注入验证）
5. **端口选择**：选一个当前未监听的端口（以第 4 条探测为准），避开集群/容器常用
   端口（80/443/8080/53 等）；无应用监听的靶上任意空闲端口皆可（如 18080 类
   高位端口）
6. 确认容器内 `/tmp` 可写（PID 文件与证据文件落盘；root 不受权限位约束，探测
   `ls -ld /tmp` 即可）

**演练步骤**：
1. 记录注入前基线：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -c <container> -- ss -tlnp
   ```
   记录：Pod RESTARTS 基线（恢复验证对照，自然重启周期工作负载须剔除）、当前监听
   快照（含目标端口无监听的事实）、原应用进程形态（有则记进程名）

**手段1（ChaosBlade）** —— 前提：资源准备第 2 条探测通过（有 occupy action 且
operator 健康）

2. 使用 ChaosBlade 对目标 Pod 注入端口占用：
   ```bash
   blade create k8s pod-network occupy \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --port <port> \
     --force \
     --timeout <duration>
   ```
   参数含义：
   - `--port`：要占用的端口（必填）
   - `--force`：强制杀死当前使用该端口的进程后占用（不加则端口被占时注入失败；
     靶无监听进程时本参数无对象、不报错）
   - `--timeout`：到期自动恢复（秒）
3. 记录返回的 experiment_uid，用于后续恢复

**手段2（kubectl-native）** —— 按资源准备第 3 条工具探测结果二选一

**路径 A —— socat 抢占（有 socat 的镜像；适用形态）**

前提条件：容器内有 socat；目标端口空闲（资源准备第 5 条）。

注入命令（**抢占 → PID 落盘 → 自证标记 → 武装定时自恢复**，严格串行于单个载荷；
到期自动 kill 占用进程并写还原标记）：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
  'pkill -x <原应用进程名> 2>/dev/null; \
   socat TCP-LISTEN:<port>,fork,reuseaddr /dev/null >/dev/null 2>&1 & echo $! > /tmp/portbind-agent.pid; \
   echo PORTBIND_INJECTED >> /tmp/portbind-agent.evd; \
   ( sleep <duration>; kill $(cat /tmp/portbind-agent.pid) 2>/dev/null; rm -f /tmp/portbind-agent.pid; echo PORTBIND_RESTORED >> /tmp/portbind-agent.evd ) >/dev/null 2>&1 &'
```
结构说明：
- `pkill -x <原应用进程名>`：仅当资源准备第 4 条探测到原应用监听时保留（实现手段1
  `--force` 语义）；无监听负载整段跳过。**不要用 pkill -f**——载体 sh -c 命令行含
  模式串时会把自己杀掉（RC=143）
- socat 必须 `>/dev/null 2>&1 &` 后台 + 重定向：否则继承 exec 管道导致 `kubectl exec`
  挂起到超时（进程其实已启动；占用进程跨 exec 会话存活，stdin EOF 不会杀掉它）
- `fork` 使占用跨连接持久（不带 fork 处理首个连接后即退出，占用不持久）；
  `reuseaddr` 保证恢复后端口可立即重 bind（TIME_WAIT 残留不再阻塞）
- kill 主进程即释放端口：listen socket 由 socat 主进程持有，fork 子进程只继承已
  accept 的连接 fd（孤儿连接自然结束，不影响端口释放）
- 链条是**自证的**：`PORTBIND_INJECTED`（占用进程启动即写）与 `PORTBIND_RESTORED`
  （定时器还原后写）落盘证据文件 `/tmp/portbind-agent.evd`，验证阶段读该文件取证，
  **不受故障窗口是否已关闭的时序约束**。两个标记必须保留，不得为缩短命令省略
  （载荷约 320 字节，wiz 通道 sh -c 1024 字节上限内安全）

路径A 倒计时从武装时刻起算：抢占→落盘→标记→武装在同一载荷内严格串行（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'pkill -f portbind-agent.pi[d]; true'` 停旧定时器（socat 占用进程不受影响，如需重注入先按恢复命令释放端口），再重跑上方注入命令原子重抢占+重武装（见 SKILL.md 安全红线「故障窗口完整」）

**执行期效果自证探针（强烈推荐，注入载荷后、进入验证前执行）**——bind 探针是
变异形态操作（后台起进程再 kill），**verify 阶段的 read-only 纪律会结构性拒绝它**
（曾见 readonly_phase_violation 拒绝记录，被迫现场改用白盒替代）。正确位置是
执行期：注入载荷落地后立即追加一条探针载荷，把效果主证在注入时刻写进证据文件：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
  'socat TCP-LISTEN:<port>,fork /dev/null >/dev/null 2>&1 & P=$!; sleep 2; \
   if kill -0 $P 2>/dev/null; then kill $P; echo PROBE_BIND_UNEXPECTEDLY_OK >> /tmp/portbind-agent.evd; \
   else echo BIND_REJECTED >> /tmp/portbind-agent.evd; fi'
```
判读：`BIND_REJECTED` 写入证据文件 = 效果主证在注入时刻被捕获（与自证链标记同构，
不受窗口时序约束）；`PROBE_BIND_UNEXPECTEDLY_OK` = 注入未生效，立即按恢复命令处置。
探针进程存活检测不可省——bind 成功的 socat 会挂住 exec（前台裸跑会挂到超时）。
有 nc 无 socat 的镜像换 `nc -l -p <port> -e cat` 同构探针（判读相同）

**路径 B —— nc 抢占（有 nc 的 busybox 类镜像；无 nc 时本路径不可用）**

两个陷阱（busybox:1.33 确证）：
1. **杀原进程不能用裸 `fuser <port>/tcp`**：busybox fuser 按 `/proc/net/tcp`（IPv4）
   与 `/proc/net/tcp6`（IPv6）分表查询，监听形态决定查哪张表（IPv4-only 监听
   `127.0.0.1:port` 时 `fuser <port>/tcp` 正常输出 PID；IPv6 双栈 `:::port` 时返回
   空 RC=1，须用 `fuser <port>/tcp6`）。原应用监听形态不确定时裸 tcp 查询可能静默
   不杀原进程 → 占用进程 bind 失败退出 → 注入静默失败且无任何报错。改用
   `pkill -x <原应用进程名>` 按进程名精确匹配（不依赖监听形态）；**不要用
   pkill -f**（自杀陷阱同路径 A）
2. **busybox nc 持久监听必须配 -e，且 -k 不可移植**：无 -e 的监听形态（`nc -l -p <port>`）
   在首个连接处理后即退出，占用不持久；必须配 `-e cat` 才能持续抢住端口。`-k`
   仅部分 busybox 编译版支持（nc110 选项集定制版 `-lk -e cat` 可持久；官方
   busybox.net 1.33.1 源码无 -k，遇 -k 直接报 usage 退出）——可移植形态是重复 -l：
   `nc -l -l -p <port> -e cat`（官方源码语义：多次 -l 时父进程 fork 循环 accept；
   定制编译版同样跨连接持久）

```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
  'pkill -x <原应用进程名> 2>/dev/null; \
   ( nc -l -l -p <port> -e cat ) >/dev/null 2>&1 & echo $! > /tmp/portbind-agent.pid; \
   echo PORTBIND_INJECTED >> /tmp/portbind-agent.evd; \
   ( sleep <duration>; kill $(cat /tmp/portbind-agent.pid) 2>/dev/null; rm -f /tmp/portbind-agent.pid; echo PORTBIND_RESTORED >> /tmp/portbind-agent.evd ) >/dev/null 2>&1 &'
```
（自证链、重武装纪律、执行期效果自证探针与路径 A 完全一致）

**注入验证**（两种手段共用——底层都是端口被占用进程持有；⚠️ verify 阶段
read-only 纪律约束：探针类变异形态操作（后台起进程+kill 的 bind 探针）会被拒绝，
效果主证依赖执行期探针标记（见手段2 执行期效果自证）或下列白盒观测）：
1. 白盒确认端口已被占用进程监听（主证）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- ss -tlnp   # 或 netstat -tlnp
   ```
   应出现 `<port>` 的 LISTEN 行，进程归属 socat/nc（手段1 为 blade 注入进程）。
   手段2 另读证据文件：`cat /tmp/portbind-agent.evd` 应含 `PORTBIND_INJECTED`
   （含 `PORTBIND_RESTORED` 即窗口已关，如实报告实际时长）；执行期探针已跑过则
   另含 `BIND_REJECTED`（效果主证，注入时刻捕获）
2. **效果交叉验证（verify 阶段可用形态）——双层白盒观测**：占用进程持有的
   LISTEN socket 本身就是导致 bind 失败的状态，用两个独立观测层交叉确证：
   - ss/netlink 层：`ss -tlnp` 见 `<port>` LISTEN 归属 socat/nc 进程
   - 内核层：`cat /proc/net/tcp`（端口十六进制，如 18080 = 0x46A0）见
     `00000000:<HEXPORT> ... 0A`（0A = LISTEN）行——与 ss 观测互为独立佐证
   - （bind 不可用的直接证明只能由执行期探针提供，见手段2；verify 阶段不要
     尝试重跑探针形态——会被 readonly 纪律拒绝，如实换用本条白盒交叉即可，
     不要重试被拒的变异形态探针）
3. 原应用有监听时（资源准备第 4 条探测到）：确认原进程已停止监听（手段1 `--force`
   / 手段2 pkill 分支生效）、本地服务不可达：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- wget -qO- --timeout=5 http://localhost:<port>
   ```
4. **预期阴性声明（无监听负载形态）**：以下现象**不出现且不作为失败证据**——
   应用日志报 `Address already in use`（无应用要 bind）、健康检查失败（无探针端口）、
   Service 端点异常（原本无端点流量）。效果主证以第 2 条 bind 探针为准
5. **持续性检查（必做）**——占用是状态型故障，监听 socket 存活即故障存活：白盒主证
   为 `ss -tlnp` 仍见 `<port>` LISTEN（手段1 实验未 destroy 且未到 `--timeout`；
   手段2 定时器未走完）。手段2 以证据文件为准：`PORTBIND_INJECTED` 已现而
   `PORTBIND_RESTORED` 未现即窗口仍开。若窗口内提前恢复，说明故障窗口契约未达成，
   必须如实报告实际持续时长

**注入恢复**：

手段1（ChaosBlade）：
1. 提前恢复：销毁实验（释放端口）`blade destroy <experiment_uid>`
2. 或等待 `--timeout`（`<duration>`）到期后自动恢复。注意：到期后 `blade status <uid>`
   可能仍显示 `Success` 不翻状态，**不要以 status 判断端口是否已释放**，以白盒
   `ss -tlnp` 为准

手段2（kubectl-native）：
1. 主保险：容器内定时器到期自动 kill 占用进程（路径 A/B 同构）
2. 提前恢复（幂等：PID 文件不存在说明定时器已还原，整链空触发无害）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'kill $(cat /tmp/portbind-agent.pid) 2>/dev/null; rm -f /tmp/portbind-agent.pid; echo PORTBIND_RESTORED >> /tmp/portbind-agent.evd; true'
   ```
3. 兜底：PID 文件丢失时，用 `ss -tlnp` 定位当前监听 `<port>` 的 PID 后 kill（注意
   ps+grep 定位有自匹配陷阱：载体 sh -c 行的命令行含 "socat"/"nc -l" 关键词，会被
   grep 命中——`[s]ocat` 技巧只能排除 grep 自身，排除不了载体行；`ss`/netstat 看监听
   socket 最准。fuser 须按监听形态选 tcp/tcp6 表，见路径 B 陷阱 1）
4. 原应用进程恢复：由容器 init 系统或 K8s 探针重启机制自动恢复；若未恢复，重启
   Pod：`kubectl delete pod <pod-name> -n <namespace>`

**恢复验证**（两种手段共用；自动化主证用白盒，bind 探针为变异形态仅限执行期/
带外人工——verify 阶段会被 readonly 纪律拒绝，不要重试）：
1. 白盒确认端口已释放（自动化主证）：`ss -tlnp`（或 `netstat -tlnp`）不再有 `<port>`
   的 LISTEN 行（与基线快照一致）；手段2 证据文件应新增 `PORTBIND_RESTORED`、
   PID 文件已删除；`/proc/net/tcp` 无 `<HEXPORT>` 的 0A 行（交叉佐证）
2. bind 探针反向确认（端口可复用；执行期或带外人工执行，Agent 主动恢复路径可跑）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'socat TCP-LISTEN:<port>,fork,reuseaddr /dev/null >/dev/null 2>&1 & P=$!; sleep 2; \
      if kill -0 $P 2>/dev/null; then echo BIND_OK; kill $P; else echo BIND_STILL_BUSY; fi'
   ```
   探针存活 2 秒 = 端口可 bind = 释放确证；`BIND_STILL_BUSY` = 占用进程仍在，恢复
   未完成（勿以 Pod Running 状态替代本判据）
3. 原应用有监听时：确认应用重新监听目标端口、服务恢复可达、Pod Running 且 Ready、
   Service Endpoints 包含该 Pod
4. 确认 Pod RESTARTS 与基线一致（无注入引发的额外重启；自然重启周期工作负载按
   基线预期剔除）

**基准事实**：
- **根因**：Pod 内目标端口被占用进程强制持有（手段1 blade 注入进程 / 手段2 socat/nc），
  其他进程 bind 该端口报 `Address already in use`；原监听进程被杀时（`--force`/pkill）
  叠加服务中断现象
- **必现现象**：`ss`/`netstat` 显示目标端口被占用进程 LISTEN；bind 探针立即失败。
  原应用有监听时另见：服务不可达、应用日志报错、可能的健康检查失败
- **效果语义**：端口占用 ≠ 连接被拒——占用进程在 listen，连接会被 accept；唯一
  必现效果是 **bind 不可用**，验证判据以 bind 探针为主证
- **blade 可用性因环境而异**：`pod-network occupy` 需要 operator 健康才能 reconcile
  （常见形态：chaosblade-operator 与 chaosblade-tool Pod 均 ImagePullBackOff，
  CR 创建成功但无人执行——`blade create` 回执成功**不代表**注入生效，必须以靶容器内
  `ss`/`netstat` 白盒为准）；不满足即用手段2
- **容器工具因地制宜**（常见两形态）：kone-runtime 类镜像有 socat+ss 无 nc/netstat/fuser；
  busybox 轻量镜像有 nc 无 socat。nc/socat 皆无则手段2 判死（busybox ash 不支持
  /dev/tcp，shell 内置重定向占位不可行，报 nonexistent directory）——只能走
  手段1 或镜像内自带的长驻占位程序
- 验证时 ps 里可能残留已 kill 进程的 zombie（PID 1 为 sleep 不收尸，busybox 经典
  现象）：zombie 不持有 socket、不占用端口，`ss` 无监听即为已恢复，无需处理
  （随 Pod 重建消失）
