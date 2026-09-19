**用例名称** Sidecar进程被挂起 导致 Container_进程异常

**故障定位**：状态型故障（信号驱动）——目标进程收到 SIGSTOP 即故障存活（内核挂起，
不退出、不消耗 CPU，但完全停止执行、无法处理任何请求），SIGCONT 或容器重启即恢复；
`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。手段1（ChaosBlade
`container-process stop`）与手段2（kubectl-native）是**并列的注入手段**，底层效果
等价（blade 内部也是发送 SIGSTOP/SIGCONT），按环境能力选用。**本用例的四个结构性
约束**：①**目标进程身份必须现场确认**——进程名以容器内 `ps aux` 输出为准（不可凭
服务名猜测；进程名超 15 字符被内核截断，截断名即 comm，也是 pgrep -x 的匹配词）；
②**Liveness 探针决定故障窗口的命运**——sidecar 配置了 Liveness 时 kubelet 周期探测
累积失败（需连续失败达 failureThreshold，存在传播延迟），达到阈值即重启该容器 =
故障提前终止（重启即恢复），注入前必须审计探针参数并评估窗口内触发概率，触发了
就如实记录「故障导致重启」而非恢复失败；③路径 B（节点侧 cgroup freezer）冻结的是
**该 sidecar 容器的全部进程**而非单进程，冻结期间 `kubectl exec -c <sidecar>` 会
挂住，sidecar 侧验证必须旁路；④**PID 1 信号豁免（路径 A 判死判据）**——目标进程
恰是 sidecar 容器 PID 1（单进程容器常态：`sleep 7200` 等裸 entrypoint，`ps aux`
全表仅一行）时，`kill -STOP 1` 被内核豁免：rc=0 但进程状态纹丝不动（PID 1 只接受
注册了 handler 的信号，SIGSTOP/SIGCONT 默认均无效），路径 A 结构性死锁——重试与
变体全部同因无效，**禁止穷举**。此时唯一活路是路径 B（cgroup freezer 是内核调度层
冻结，不走信号路径，对 PID 1 有效），但路径 B 写入集在节点命名空间，规划期就必须
确认批准包含 node scope。**本用例与「Pod 主进程挂起」的分界**：挂的是 sidecar 容器
内的进程，主容器必须全程正常（exec/重启计数双重对照）。

**故障现象**：
1. Sidecar 容器内主进程被 SIGSTOP 信号挂起，进程不退出但完全停止处理请求
2. 如果 Sidecar 容器配置了独立 Liveness 探针，探针超时后可能触发容器重启
3. 如果无 Liveness 探针，Sidecar 持续不可用但 Pod 状态仍显示 Running
4. 模拟 Sidecar 死锁/卡死场景，主容器依赖 Sidecar 的功能不可用
5. 主容器不受影响（预期阴性：主容器 restartCount 零漂移、exec 正常——出现漂移即
   爆炸半径失控）

**资源准备**：
1. 确认目标 Pod 包含多个容器，明确 Sidecar 容器名称；确认目标 Pod 所在 namespace 和 labels
2. **多容器靶接线（靶为单容器 Pod 时）**：业务靶常见单容器形态——用 JSON patch 给靶
   Deployment 模板追加一个 sidecar 容器（演练资产，结束拆线归还）。sidecar 须同时承载
   **可挂起的受害服务进程**（验证「挂起期间服务无响应、恢复后服务复活」现象面）：socat
   监听 + sleep 保活（镜像选**节点缓存或 VPC registry 可达**镜像，VPC 受限网络下
   docker.io 公网镜像会 `ImagePullBackOff`；terway 类 CNI 镜像自带 socat 且 busybox
   工具集完整——kill/pgrep/ps 可用，路径 A 直接成立，节点缓存零拉取风险。
   **镜像覆盖定案（免推导）**：CNI 组件镜像与 terway-eniip DS 同 tag 时由
   kube-system DaemonSet 全节点常驻缓存——规划期至多单命令复核 DS
   `ready/desired`，勿逐节点或全表扫描重新论证覆盖面（定案全文见
   `references/environment/node-cached-images.md`））：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"add","path":"/spec/template/spec/containers/-","value":{"name":"drill-sidecar","image":"<节点缓存镜像>","command":["sh","-c","socat TCP-LISTEN:8080,fork,reuseaddr EXEC:/bin/cat & sleep 7200"]}}]'
   kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=120s
   ```
   此形态下 PID 1 是 sh、socat 与 sleep 是其子进程——挂 socat（PID≠1）走路径 A；
   **勿配 livenessProbe**（无探针则窗口完整由 duration 决定，故障现象取「Pod Running
   持续不可用」形态；探针触发的重启分支见故障定位约束②）。拆线用
   `kubectl patch --type='json' -p='[{"op":"remove","path":"/spec/template/spec/containers/<sidecar索引>"}]'`
   后 rollout status 等收敛。**拆线归恢复生命周期**：接线资产拆除不进执行计划
   （SKILL.md 安全红线「拆线不进执行计划」）——执行器下发完「接线+注入」即止，
   等待恢复/拆线由 recover 生命周期接管
3. **能力探测（决定手段与路径选择，以当次探测为准）**：
   ```bash
   # a) ChaosBlade operator 健康（手段1 判死判据）
   kubectl get pods -A --no-headers | grep -i chaosblade-operator
   # b) sidecar 容器工具（手段2 路径 A 判据）
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- sh -c \
     'command -v kill; command -v pgrep; echo PROBE_DONE'
   ```
   - operator Pod Running → 手段1 可用（实验 UID 统一生命周期管理）
   - operator 未就绪（常见形态：operator 0/1，CR 无人 reconcile，`blade create` 回执
     成功也不代表注入生效）→ 手段1 判死，用手段2，不要在手段1 上空转
   - sidecar 内有 kill 且 pgrep → 路径 A；任一缺失（distroless/scratch 极简镜像
     常态）→ 路径 B（节点侧 cgroup freezer，不需要容器内任何二进制）
4. **进程身份探测（必做，注入前唯一权威）**：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- sh -c 'ps aux'
   ```
   记录目标进程的 COMMAND 列首词；**管道必须包在 sh -c 载荷内**——kubectl exec
   命令是 argv 直传无 shell，裸 `ps aux | grep` 的 `|`/`grep` 会沦为 ps 的字面
   参数，过滤静默失效。确认的进程名同时就是后续 `pgrep -x` 的匹配词。**PID 判死
   核验（同一份 ps 输出顺带完成）**：目标进程 PID=1 → 命中结构性约束④，路径 A
   判死——立即转路径 B（先确认 node scope 授权）或停止并如实报告，**不要注入
   路径 A 后再靠 verify 抓效果缺失来发现**；PID≠1 → 路径 A 正常可用
5. **Liveness 探针审计（决定故障窗口命运）**：
   ```bash
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath='{.spec.containers[?(@.name=="<sidecar-container-name>")].livenessProbe}'
   ```
   - 无 livenessProbe → sidecar 容器不会被 kubelet 重启，窗口完整由 duration 决定
   - 有 → 估算触发时延：periodSeconds × failureThreshold + initialDelaySeconds。
     该值 > duration → 窗口安全；≤ duration → 容器会在窗口内被重启，故障提前
     终止，演练设计须如实声明（或缩短 duration 避开），恢复验证按「重启即恢复」
     分支核对（该容器 RESTARTS +1、新进程 S 状态）
6. **基线记录（恢复验证的定量对照）**：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- sh -c \
     'grep State /proc/$(pgrep -x <process-name>)/status'
   ```
   记录：进程正常状态 `S (sleeping)`（恢复对照）、两容器 restartCount 基线、
   sidecar 受害服务连通基线（`echo PING | socat -T 3 - TCP:127.0.0.1:<port>` 回显）

**演练步骤**：

> **爆炸半径分类（定案）**：`target-only`。路径 A：`kill -STOP` 经 `pgrep -x <进程名>`
> 精确匹配 sidecar 容器内目标进程——exec 通道已用 `-c` 限定到 sidecar 容器，容器
> PID namespace 隔离天然不越界；变更面 = 靶 Pod 单个 sidecar 容器内一个进程的调度
> 状态 + 靶 Deployment 模板（接线/拆线），不触及任何非靶资源。路径 B：freezer
> 写的是该 sidecar 容器自己的 `.scope` cgroup（每容器独立 cgroup，同 Pod 主容器
> 不受影响——这正是本用例「只挂 sidecar」语义所要求的）；变更面 = 靶节点上一个
> transient unit（解冻 timer）+ 该 cgroup 文件 + 靶 Deployment 模板。
> **恢复机制边界（定案）**：路径 A 是**容器内进程型自恢复**（容器内后台 sleep
> 定时器到期 kill -CONT），路径 B 是**节点侧 systemd 型**（systemd-run 定时
> THAWED，宿主机 PID 1 托管）——两类机制，均非 API 对象写，恢复载体标准件判据一
> 不满足，**勿建四件套**。

1. 确认 Pod 内容器列表，获取 Sidecar 容器名称：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].name}'
   ```
2. 完成资源准备第 3-6 条（能力探测、进程身份与 PID 核验、Liveness 审计、基线记录）

**手段1（ChaosBlade）** —— 前提：资源准备第 3a 条探测通过（operator 健康）

3. 使用 ChaosBlade 对 Sidecar 容器注入进程挂起故障：
   ```bash
   blade create k8s container-process stop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --container-names <sidecar-container-name> \
     --process <进程名> \
     --timeout <duration>
   ```
   倒计时从武装时刻起算：--timeout 与注入命令一同下发原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `blade destroy <experiment_uid>` 旧实验，再重跑上方注入命令重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）。记录返回的 experiment_uid 用于后续恢复

**手段2（kubectl-native）** —— 按资源准备第 3b 条工具探测结果二选一

4. 观察 Sidecar 容器进程挂起后的行为（是否触发探针重启、主容器是否受影响）

**注入验证**（手段1/手段2 路径 A 共用；路径 B 差异单列）：
1. **效果主证——进程 T 状态**（仅适用手段1/路径 A；procps 镜像看 STAT 列；
   **busybox 镜像 ps 无 STAT 列**——输出仅 PID/USER/TIME/COMMAND——改用 /proc 判据）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- sh -c \
     'grep State /proc/$(pgrep -x <process-name>)/status'
   ```
   输出 `State:\tT (stopped)`（正常运行为 `State:\tS (sleeping)`）。
   **verify 阶段命令形态（门禁预立法）**：verify 只读门禁拒 `$()` 命令替换——
   上式含 `$(pgrep ...)` 在 verify 阶段必被拒；等价两步形态为 verify 主形态：
   先 `pgrep -x <process-name>` 解析 PID，再 `grep State /proc/<字面PID>/status`
   （同 /proc State 字段，观察等价）。`$()` 形态仅执行段机制确认可用（执行段
   exec 载荷无此门禁）——本用例持续性检查（第 6 条）同理用两步字面量形态。
   **路径 B 效果判据形态完全不同**：cgroup freezer 冻结的进程 **/proc State 仍显示
   S、内核栈仍显示原睡眠栈、PF_FROZEN 不显示**——这些「看起来没冻结」是正常表象，
   **不是判死证据，严禁据此推断注入失败**。路径 B 的效果判据用以下两条：
   - **机制主证**：`freezer.state` 读回 `FROZEN`（空 cgroup 写 FROZEN 也立即成立，
     必须配合下一条行为学判据交叉）
   - **行为学判据（决定性）**：`timeout 5 kubectl exec <pod> -c <sidecar> -- echo ok`
     挂住超时（rc=124/无输出）或 fail-fast 拒绝（containerd 新版报
     `cannot exec in a paused container`——拒绝同样是冻结的直接信号，严禁误读为
     通道故障）；恢复对照统一为解冻后同命令立即回显
2. **Sidecar 服务完全无响应**——路径 A 从容器内自探（exec 仍可用：载体 sh 是新
   进程不受目标挂起影响）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- sh -c \
     'echo PING | socat -T 3 - TCP:127.0.0.1:<port>'
   ```
   确认超时无回显。**路径 B 必须旁路**——被冻结的容器 exec 会挂住，借同 Pod
   主容器访问 sidecar 端口（共享网络栈）或从节点侧/旁路 Pod 探测。
   若行为探针被 verify 只读门禁判 load-generating 拒绝（socat/wget 形态常见）：
   **勿重试同类命令**，按状态型判据裁决（进程 T 状态/freezer.state FROZEN 即
   故障在——挂起是内核态，进程状态是机制执法的直接回显）+ deviation 文档化
   （记录被拒命令与原因），服务中断行为对照由恢复后带外核实补强（解冻/SIGCONT
   后探针恢复回显 = 服务曾被中断的反向证明）
3. **主容器不受影响（本用例与 Pod 级挂起的分界判据，必查）**：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <主容器名> -- echo alive
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[*].restartCount}'
   ```
   exec 正常回显 + 主容器 restartCount 与基线一致（零漂移）
4. Pod 状态仍 Running（进程 PID 存在，容器未退出）；若配置了 Liveness 探针，
   `kubectl describe pod` 查探针失败事件——探针失败需连续失败达
   failureThreshold 才触发，存在传播延迟：**首查未见事件不构成反证**，挂起的
   直接证据是第 1 条的进程状态
5. **爆炸半径交叉确认**：
   ```bash
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name> --no-headers
   ```
   同节点 Pod 与基线清单一致（无 Evicted、无异常重启）——进程级故障不外溢
6. **持续性检查（必做）**——状态型故障，挂起存活即故障存活。白盒主证（单独
   充分）：`grep State /proc/$(pgrep -x <进程名>)/status` 仍为 T（或路径 B
   freezer.state 仍 FROZEN）；白盒不可查时有界佐证（≤60s 静观后复查状态不变）
7. 路径 A 补自证链：`cat /tmp/procstop.evd` 含 `PROCSTOP_INJECTED`（含
   `PROCSTOP_RESTORED` 即窗口已关，如实报告实际时长）

**注入恢复**：
1. 手段1：`blade destroy <实验UID>`（向进程发送 SIGCONT 恢复执行）；或等待
   `--timeout` 到期后自动恢复（stop 动作超时后自动发 SIGCONT，进程从挂起状态继续执行）
2. 手段2 路径 A：定时器到期自动 SIGCONT + 写 `PROCSTOP_RESTORED`（主恢复路径）；
   提前恢复用演练步骤的恢复命令（SIGCONT 幂等）
3. 手段2 路径 B：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'echo THAWED > <freezer.state 路径>'
   ```
   或等待 systemd-run 定时解冻 timer 到期自动 THAWED
4. 若 sidecar 的 Liveness 探针已触发容器重启，等待新容器 Ready 即可（重启即恢复）
5. **拆线执行者的取样义务（接线靶，路径 A）**：recover Layer 1 在 patch remove
   sidecar（拆线）**之前**，必须先在容器内取齐进程级判据——State 回 S（两步
   字面量形态）+ socat 8080 回显 + `cat /tmp/procstop.evd` 双标记——这是它们
   的最后取样窗口（拆线后载体容器随 Pod 重建消失，恢复验证步骤 1/3/5 只能退化为
   teardown 超越裁决）。取样义务属于**拆线动作的执行者**（Layer 1），不是恢复
   验证层（Layer 2）——拆线前不取样，Layer 2 将永远失去直接判据

**恢复验证**：
1. **进程恢复运行（主证，路径 A）**：`grep State /proc/$(pgrep -x <进程名>)/status`
   回到 `S (sleeping)`（或 procps STAT 列不再显示 T）
2. **路径 B 恢复主证**：`freezer.state` 读回 `THAWED` + **exec 恢复响应**
   （`timeout 5 kubectl exec <pod> -c <sidecar> -- echo ok` 立即回显——与冻结期
   的挂住超时形成行为学对照）；进程 State 全程为 S 不是恢复证据（见注入验证
   第 1 条路径 B 说明）
3. Sidecar 服务恢复响应：`echo PING | socat -T 3 - TCP:127.0.0.1:<port>` 正常
   回显（同时是「窗口内服务曾被中断」的反向证明——verify 阶段行为探针被拒时，
   此处带外补强）
4. 主容器持续正常：exec 正常 + restartCount 与基线一致（全程零漂移——本用例
   爆炸半径的最终证据）
5. 路径 A 自证链双标记：`cat /tmp/procstop.evd` 含 `PROCSTOP_INJECTED` +
   `PROCSTOP_RESTORED`（双标记齐备 = 注入与还原全程自证）
6. Pod 状态 Running、RESTARTS 与基线一致（或按 Liveness 分支该容器 +1 且与
   事件对账）
7. **拆线归还（接线靶）**：patch remove sidecar 容器 + `kubectl rollout status`
   等收敛——靶 Deployment 回单容器基线形态（同 RS 归还为模板精确回滚的数学
   证据），归 recover 生命周期执行。进程级判据（State S + socat 回显 + 双标记）
   已由拆线执行者在拆线前取样（注入恢复第 5 条的取样义务），本层（Layer 2）
   **消费**该取样结果——拆线后载体容器随 Pod 重建消失，第 1/3/5 条不可再
   实例化时按 teardown 超越裁决（载体消失本身即故障效果消失的证据），直接
   判据缺失须如实记 deviation

**基准事实**：
- **根因**：Sidecar 容器内目标进程被 SIGSTOP 信号挂起，进程不退出但停止调度执行，
  模拟死锁/卡死场景
- **必现现象**：目标进程状态变为 T（Stopped）；Sidecar 服务完全无响应；Pod 状态
  仍为 Running（除非探针触发重启）；主容器本身正常但通过 Sidecar 的功能链路
  断裂——T 状态与服务超时为路径 A 形态；**路径 B（freezer）形态：进程 State
  保持 S（内核不暴露冻结态），必现现象是 sidecar exec 挂住超时/fail-fast 拒绝 +
  freezer.state=FROZEN**（解冻后同命令立即回显——行为学对照干净可逆）
- **条件现象**：sidecar 的 Liveness 探针超时失败 → 该容器单独重启（仅配置了
  Liveness 且达到 failureThreshold 时；重启即恢复，故障窗口提前终止）
- **PID 1 边界**：目标进程为 sidecar 容器 PID 1 时 SIGSTOP/SIGCONT 被内核豁免
  （rc=0 无效果），信号路径全部失效；cgroup freezer（调度层冻结）不受此限，
  对 PID 1 有效——路径 B 是 PID 1 靶的唯一可行手段

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。
> 两条路径按 sidecar 容器内是否有 `kill`/`pgrep` 二选一。

---

**路径 A —— sidecar 容器内有 `kill` 和 `pgrep`**

前提条件：目标容器内需有 `kill` 命令和 `pgrep` 工具可用（资源准备第 3b 条已探测）。
sidecar 常是 distroless 的极简镜像（istio-proxy、各类 driver-registrar 等），缺失时
走路径 B。

注入命令（**定时器先武装 → SIGSTOP → 自证标记**，严格串行于单个载荷；倒计时
从武装时刻起算，同一载荷内无侵蚀间隙。定时器子 shell 的 comm 是 sh，
`pgrep -x` 不会命中它——这正是必须用 `-x` 的原因，见下方陷阱说明）：
```bash
kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- sh -c \
  '( sleep <duration>; kill -CONT $(pgrep -x <process-name>); echo PROCSTOP_RESTORED >> /tmp/procstop.evd ) >/dev/null 2>&1 & \
   kill -STOP $(pgrep -x <process-name>) && echo PROCSTOP_INJECTED >> /tmp/procstop.evd'
```
- 链条是**自证的**：`PROCSTOP_INJECTED`（kill -STOP 成功后写入）、
  `PROCSTOP_RESTORED`（定时器 SIGCONT 后写入）落盘证据文件 `/tmp/procstop.evd`，
  验证阶段 `cat` 取证，不受故障窗口是否已关闭的时序约束
- 载荷必须单次下发（约 200 字节，wiz 通道 sh -c 1024 字节上限内安全）；不能用
  `&&` 把两条 kubectl 连进同一个外层 `sh -c`——第二条 kubectl 会沦为第一条
  exec 载荷的死参数，注入静默丢失

**必须用 `pgrep -x`（按进程名精确匹配），严禁 `pgrep -f`（按 cmdline 匹配）
+ `grep -vw $$` 排除的老写法**——后者有两处误匹配：
- ① 武装的定时器后台 sh 的 cmdline 含进程名字面量，注入时会被一起 SIGSTOP
  冻结——定时器被冻结则到期不再触发，**自恢复链断裂**
- ② `$()`/管道的瞬时子进程在 fork 后 exec 前 cmdline 是 sh 副本（同样含进程名
  字面量），pgrep 扫描命中、kill 执行时已退出——报 `can't kill pid <N>: No such
  process`（rc=1）
- 对照：`pgrep -f <进程名>` 输出目标 PID + 瞬时 PID 两个值，`pgrep -x <进程名>`
  只输出目标 PID
- `-x` 按进程名（comm）匹配，定时器/瞬时进程的进程名是 sh/pgrep/grep 不会命中

恢复命令（提前恢复用；SIGCONT 幂等，定时器后续到期再触发也无副作用）：
```bash
kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- sh -c \
  'kill -CONT $(pgrep -x <process-name>) && echo PROCSTOP_RESTORED >> /tmp/procstop.evd; true'
```

注意事项：
- **容器 PID 1 不可挂起**：sidecar 容器的主进程（PID 1）受内核 SIGNAL_UNKILLABLE 保护，
  `kill -STOP 1` 返回成功但进程不进入 T 状态（静默无效）——本路径只能作用于容器内
  **非 init 进程**；若目标就是主进程，改走路径 B（freezer 冻结整个容器 cgroup，
  等效实现「主进程挂起」语义）
- 必须通过 `ps aux` 确认实际进程名，不可猜测；busybox 镜像 ps 无 STAT 列时改用
  `/proc/<pid>/status` 的 State 行判状态
- 容器内无 systemd，后台 sleep 定时器**不可取消**——提前恢复后只能等它到期空
  触发（SIGCONT 幂等无害）；期间重武装新定时器会叠加多个，到期连发 SIGCONT
  同样幂等无害，但须如实知晓
- 若 sidecar 的 Liveness 探针已触发容器重启，进程随新容器自动恢复——此时旧定时器的
  pgrep 在新容器里必然落空（exec 载荷随旧容器消亡），无需处置

---

**路径 B —— sidecar 容器内没有 `kill`：节点侧 cgroup freezer**

从节点侧冻结**该 sidecar 容器**的 cgroup，不需要容器内有任何二进制。同 Pod 内的
其它容器（含主容器）**不受影响** —— 每个容器有独立的 `.scope` cgroup，这正是
本用例「只挂起 sidecar」语义所要求的。

> **写入集边界警示（选路前必读）**：本路径全部操作经
> `kubectl debug node/` 落在**节点侧**，写入集在 node 命名空间。批准 scope 仅为
> pod 时，执行守卫 fail-closed 拦截（拦截本身正确）。**replan 中途切入路径 B
> 不会重新过确认门**——写入集批卡只覆盖首次规划，守卫拦截后 LLM 无法自证越权
> 合法性。因此：意图解析/首次规划阶段就预计走路径 B 时（PID 1 靶、distroless
> 镜像），**写入集投影必须显式声明 node scope 并取得批准**；批准不含 node 时
> 唯一合法出口是 propose_plan_change（语义变更，需用户确认）或
> finish_planning(rejected) 带 alternatives 如实报告，不要在容器内穷举规避手段
> （见故障定位约束④）。

> **仅适用 cgroup v1。** 先判定版本，v2 的路径与写法不同（`cgroup.freeze`，写 `1`/`0`），
> 本用例未覆盖 v2，判定为 v2 时应停止并报告不支持：
> ```bash
> kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
>   -- chroot /host stat -fc %T /sys/fs/cgroup
> ```
> 输出 `tmpfs` → v1，继续；输出 `cgroup2fs` → v2，**停止**。

1. 定位目标节点，并取 sidecar 容器的 containerID。**多容器 Pod 必须按容器名精确取**
   （`containerStatuses[0]` 是不确定的那一个，取错会冻错容器）：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.nodeName}
   # 列出全部容器名与 ID，从中挑 sidecar 那一条
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath={range.status.containerStatuses[*]}{.name}{.containerID}{end}
   ```

2. 取该容器的 freezer cgroup 路径。**推荐用 crictl 按容器名反查，不要手工拼路径**
   —— 路径含 QoS 层（`kubepods-burstable.slice` / `kubepods-besteffort.slice`，
   Guaranteed 则无此层）和 Pod UID 的下划线化，手工拼极易错：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'CID=$(crictl ps -q --name <sidecar-container-name> | head -1); \
        find /sys/fs/cgroup/freezer -type d -name "*$CID*"'
   ```
   输出形如（样例，Burstable QoS）：
   ```
   /sys/fs/cgroup/freezer/kubepods.slice/kubepods-burstable.slice/\
   kubepods-burstable-pod<UID_下划线>.slice/cri-containerd-<containerID>.scope
   ```
   **同名容器跨 Pod 时 `--name` 会命中多个** —— 用 `crictl ps --pod <podSandboxId>
   --name <容器名> -q` 限定到目标 Pod，或核对上一步拿到的 containerID 前缀。

3. 注入 —— **必须先武装定时解冻，再冻结**：
   ```bash
   # ⚠️ 顺序不可颠倒：冻结后无法 exec 进该容器，且 debug pod 可能先于解冻被删除。
   #    定时器由宿主机 systemd(PID 1) 管理，不受 debug pod 生命周期影响。
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c '
       P=<步骤2得到的路径>/freezer.state
       systemd-run --on-active=<recovery-seconds>s --unit=blade-thaw-<containerID前12位> \
         sh -c "echo THAWED > $P" &&
       echo FROZEN > $P
     '
   ```

验证：
```bash
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host cat <步骤2得到的路径>/freezer.state
```
应输出 `FROZEN`。再确认**主容器未受影响**（这是本用例与「整个 Pod 挂起」的分界）：
```bash
kubectl exec <pod-name> -c <主容器名> -n <namespace> -- echo alive
```
应正常返回 `alive`。sidecar 侧的业务影响按其职责验证（如 istio-proxy 被冻则出入流量中断、
driver-registrar 被冻则插件注册失效）。

恢复：
```bash
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host sh -c 'echo THAWED > <步骤2得到的路径>/freezer.state'
```

注意事项：
- **冻结的是该容器的全部进程**，不是路径 A 的单个进程；差异要在演练报告里说明
- **sidecar 的 liveness 探针会失败** → 该容器被 kubelet 单独重启 → cgroup 目录消失、
  故障自动结束。此时记录为「故障导致容器重启」，且**不要再写 THAWED**（路径已不存在）
- 冻结期间 `kubectl exec -c <sidecar>` 会挂住；主容器仍可正常 exec
- `freezer.state` 权限为 `-rw-r--r-- root root`，`chroot /host` 后可写
- 路径依赖 **systemd cgroup driver**（`.slice`/`.scope` 命名）；cgroupfs driver 的路径形如
  `/sys/fs/cgroup/freezer/kubepods/burstable/pod<UID>/<containerID>/`，
  本用例未覆盖 —— 用步骤 2 的 `find` 现场确认后再操作

