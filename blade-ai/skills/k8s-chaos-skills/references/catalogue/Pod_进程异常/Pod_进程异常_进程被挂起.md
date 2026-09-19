**用例名称** 进程被挂起 导致 Pod_进程异常

**故障定位**：状态型故障（信号驱动）——目标进程收到 SIGSTOP 即故障存活（内核挂起，
不退出、不消耗 CPU，但完全停止执行、无法处理任何请求），SIGCONT 或容器重启即恢复。
手段1（ChaosBlade `pod-process stop`）与手段2（kubectl-native）是**并列的注入手段**，
底层效果等价（blade 内部也是发送 SIGSTOP/SIGCONT），按环境能力选用。
**本用例的四个结构性约束**：①**目标进程身份必须现场确认**——进程名以容器内 `ps aux`
输出为准（不可凭服务名猜测；进程名超 15 字符被内核截断，截断名即 comm，也是
pgrep -x 的匹配词）；②**Liveness 探针决定故障窗口的命运**——配置了 Liveness 时
kubelet 周期探测累积失败（需连续失败达 failureThreshold，存在传播延迟），达到阈值
即重启容器 = 故障提前终止（重启即恢复，新容器进程正常运行），注入前必须审计探针
参数并评估窗口内触发概率，触发了就如实记录「故障导致重启」而非恢复失败；
③路径 B（节点侧 cgroup freezer）冻结的是**整个容器的全部进程**而非单进程，冻结期间
该容器 `kubectl exec` 会挂住，一切验证必须从节点侧或旁路 Pod 做；④**PID 1 信号
豁免（路径 A 判死判据）**——目标进程恰是容器 PID 1（单进程容器常态：`sleep 7200`
等裸 entrypoint，`ps aux` 全表仅一行）时，`kill -STOP 1` 被内核豁免：**rc=0 但
进程状态纹丝不动**（PID 1 只接受注册了 handler 的信号，SIGSTOP/SIGCONT 默认均
无效），路径 A 结构性死锁——重试与变体（`kill -18`、`pgrep -f` 换匹配词、ptrace、
改写 ELF/binutils）全部同因无效，**禁止穷举，空转 70+ 分钟徒劳**。此时唯一活路是
路径 B（cgroup freezer 是内核调度层冻结，不走信号路径，对 PID 1 有效），但路径 B
写入集在节点命名空间，规划期就必须确认批准包含 node scope（见路径 B 边界警示）。
`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

**故障现象**：
1. 应用完全无响应但进程仍存在（与 kill 不同，进程不会退出），进程状态变为 T（Stopped）
2. Pod 状态仍为 Running（进程 PID 存在，容器未退出）
3. 所有入站请求超时，服务完全不可用（模拟应用死锁场景）
4. 容器重启**不出现**（预期阴性：无 Liveness 探针或探针未达 failureThreshold 时——
   出现即 Liveness 触发，按故障定位约束②记录为「故障导致重启」并核对窗口缩短量）
5. 同节点其他 Pod 零影响、节点 Conditions 无漂移（预期阴性——出现即爆炸半径失控）

**资源准备**：
1. 确认目标 Pod 的标签选择器、命名空间，以及**实际容器名**（多容器混存时
   `kubectl exec` 必须显式 `-c <容器名>`）
2. **能力探测（决定手段与路径选择）**：
   ```bash
   # a) ChaosBlade operator 健康（手段1 判死判据，以当次探测为准）
   kubectl get pods -A --no-headers | grep -i chaosblade-operator
   # b) 容器工具（手段2 路径 A 判据）
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'command -v kill; command -v pgrep; echo PROBE_DONE'
   ```
   - operator Pod Running → 手段1 可用（实验 UID 统一生命周期管理）
   - operator 处于 Init:ImagePullBackOff/CrashLoopBackOff（常见形态：operator
     0/1，CR 无人 reconcile，`blade create` 回执成功也不代表注入生效）→ 手段1
     判死，用手段2，不要在手段1 上空转
   - 容器内有 kill 且 pgrep → 手段2 路径 A；任一缺失（distroless/scratch 极简镜像
     常态）→ 路径 B（节点侧 cgroup freezer，不需要容器内任何二进制）
3. **进程身份探测（必做，注入前唯一权威）**：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'ps aux'
   ```
   记录目标进程的 COMMAND 列首词；**管道必须包在 sh -c 载荷内**——kubectl exec
   命令是 argv 直传无 shell，裸 `ps aux | grep` 的 `|`/`grep` 会沦为 ps 的字面
   参数，过滤静默失效。确认的进程名同时就是后续 `pgrep -x` 的匹配词。
   **PID 判死核验（同一份 ps 输出顺带完成，零额外成本）**：目标进程 PID=1 →
   命中结构性约束④，路径 A 判死——立即转路径 B（先确认 node scope 授权）或
   停止并如实报告，**不要注入路径 A 后再靠 verify 抓效果缺失来发现**；PID≠1（多进程容器）→ 路径 A 正常可用
4. **Liveness 探针审计（决定故障窗口命运，必做）**：
   ```bash
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath='{.spec.containers[*].livenessProbe}'
   ```
   - 无 livenessProbe → 容器不会被 kubelet 重启，窗口完整由 duration 决定
   - 有 → 估算触发时延：periodSeconds × failureThreshold + initialDelaySeconds。
     该值 > duration → 窗口安全；≤ duration → 容器会在窗口内被重启，故障提前
     终止，演练设计须如实声明（或缩短 duration 避开），恢复验证按「重启即恢复」
     分支核对（新容器 RESTARTS +1、进程 S 状态）
5. **基线记录（恢复验证的定量对照）**：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'grep State /proc/$(pgrep -x <process-name>)/status'
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name> --no-headers
   ```
   记录：进程正常状态 `S (sleeping)`（恢复对照）、Pod RESTARTS 基线、同节点
   Pod 清单（爆炸半径对照）

**演练步骤**：
1. 记录注入前基线（资源准备第 5 条全部输出）

**手段1（ChaosBlade）** —— 前提：资源准备第 2a 条探测通过（operator 健康）

2. 注入进程挂起：
   ```bash
   blade create k8s pod-process stop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --process <实际进程名> \
     --timeout <duration>
   ```
   - `--process`：目标进程名，必须与 ps aux 输出一致（资源准备第 3 条）
   - 原理：向目标进程发送 SIGSTOP，进程被内核挂起
3. 记录返回的 experiment_uid，用于后续恢复

**手段2（kubectl-native）** —— 按资源准备第 2b 条工具探测结果二选一

**路径 A —— 容器内有 kill 和 pgrep（适用形态）**

注入命令（**定时器先武装 → SIGSTOP → 自证标记**，严格串行于单个载荷；倒计时
从武装时刻起算，同一载荷内无侵蚀间隙。定时器子 shell 的 comm 是 sh，
`pgrep -x` 不会命中它——这正是必须用 `-x` 的原因，见下方陷阱说明）：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
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
+ `grep -vw $$` 排除的老写法**——后者有两处误匹配（busybox:1.33 复现）：
- ① 武装的定时器后台 sh 的 cmdline 含进程名字面量，注入时会被一起 SIGSTOP
  冻结——定时器被冻结则到期不再触发，**自恢复链断裂**
- ② `$()`/管道的瞬时子进程在 fork 后 exec 前 cmdline 是 sh 副本（同样含进程名
  字面量），pgrep 扫描命中、kill 执行时已退出——报 `can't kill pid <N>: No such
  process`（rc=1）
- 对照：`pgrep -f httpd` 输出目标 PID + 瞬时 PID 两个值，`pgrep -x httpd`
  只输出目标 PID
- `-x` 按进程名（comm）匹配，定时器/瞬时进程的进程名是 sh/pgrep/grep 不会命中

恢复命令（提前恢复用；SIGCONT 幂等，定时器后续到期再触发也无副作用）：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
  'kill -CONT $(pgrep -x <process-name>) && echo PROCSTOP_RESTORED >> /tmp/procstop.evd; true'
```

注意事项：
- 容器内无 systemd，后台 sleep 定时器**不可取消**——提前恢复后只能等它到期空
  触发（SIGCONT 幂等无害）；期间重武装新定时器会叠加多个，到期连发 SIGCONT
  同样幂等无害，但须如实知晓
- 若 Liveness 探针已触发容器重启，进程随新容器自动恢复——此时旧定时器的
  pgrep 在新容器里必然落空（exec 载荷随旧容器消亡），无需处置

**路径 B —— 容器内没有 kill（极简镜像）：节点侧 cgroup freezer**

从节点侧冻结容器的整个 cgroup，**不需要容器内有任何二进制**。冻结范围是该容器的
**全部进程**（而非路径 A 的单个进程），语义上更彻底——与路径 A 的差异要在演练
报告里说明。

> **写入集边界警示（选路前必读）**：本路径全部操作经
> `kubectl debug node/` 落在**节点侧**，写入集在 node 命名空间。批准 scope 仅为
> pod 时，执行守卫 fail-closed 拦截（拦截本身正确）。**replan 中途切入路径 B
> 不会重新过确认门**——写入集批卡只覆盖首次规划（2026-08，产品缺口待修），
> 守卫拦截后 LLM 无法自证越权合法性。因此：意图解析/首次规划阶段就预计走路径 B
> 时（PID 1 靶、distroless 镜像），**写入集投影必须显式声明 node scope 并取得
> 批准**；批准不含 node 时唯一合法出口是 propose_plan_change（如 stop→kill 的
> 语义变更，需用户确认）或 finish_planning(rejected) 带 alternatives 如实报告，
> 不要在容器内穷举规避手段（见结构性约束④）。

> **仅适用 cgroup v1。** 先判定版本，v2 的路径与写法完全不同（`cgroup.freeze`，
> 写 `1`/`0`），本用例未覆盖 v2，判定为 v2 时应停止并报告不支持：
> ```bash
> kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
>   -- chroot /host stat -fc %T /sys/fs/cgroup
> ```
> 输出 `tmpfs` → cgroup v1，继续；输出 `cgroup2fs` → v2，**停止**。

1. 取目标容器的 Pod UID、containerID 与 QoS class（三者都是拼路径的必需项）：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.metadata.uid}
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.status.containerStatuses[0].containerID}
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.status.qosClass}
   ```

2. 拼出 cgroup 路径（以下转换规则仅适用 **systemd cgroup driver** 的 `.slice`/`.scope`
   布局；cgroupfs driver 布局不同，这些转换拼不出正确路径——**推荐所有场景都直接
   用下方 find 定位**）。
   systemd driver 的三处转换：
   - Pod UID 里的 `-` 全部换成 `_`（`314aae5f-2566-…` → `pod314aae5f_2566_…`）
   - containerID 去掉 `containerd://` 前缀，包成 `cri-containerd-<id>.scope`
   - **按 QoS 插入中间层**（这是最容易漏的一层）：

     | qosClass | 路径形态 |
     | --- | --- |
     | `BestEffort` | `kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod<UID>.slice/` |
     | `Burstable` | `kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod<UID>.slice/` |
     | `Guaranteed` | `kubepods.slice/kubepods-pod<UID>.slice/` — **没有中间层** |

   完整形态（BestEffort 示例）：
   ```
   /sys/fs/cgroup/freezer/kubepods.slice/kubepods-besteffort.slice/\
   kubepods-besteffort-pod<UID_下划线>.slice/cri-containerd-<containerID>.scope/freezer.state
   ```
   拼不出来时用 containerID 直接搜（更稳，推荐先用这个拿到真实路径）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host find /sys/fs/cgroup/freezer -type d -name *<containerID前12位>*
   ```

3. 注入 —— **必须先武装定时解冻，再冻结**。顺序错了会导致容器永久冻结：
   ```bash
   # ⚠️ 关键顺序：先用 systemd-run 登记定时 THAWED，再写 FROZEN。
   #    冻结后该容器无法 exec 进入（进程被冻结无法响应），且 debug pod 可能先于
   #    解冻被删除 —— 定时器由宿主机 systemd(PID 1) 管理，不受 debug pod 生命周期影响。
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c '
       systemd-run --on-active=<recovery-seconds>s --unit=blade-thaw-<containerID前12位> \
         sh -c "echo THAWED > <freezer.state 路径>" &&
       echo FROZEN > <freezer.state 路径>
     '
   ```

**注入验证**（手段1/手段2 路径 A 共用；路径 B 差异单列。本用例判据全部是只读
操作——ps/grep/wget/describe，verify 阶段 read-only 纪律天然放行）：
1. **效果主证——进程 T 状态**（**仅适用手段1/手段2 路径 A**；procps 镜像看 STAT 列；
   **busybox 镜像 ps 无 STAT 列**——输出仅 PID/USER/TIME/COMMAND（busybox:1.33）——改用 /proc 判据）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'ps aux | grep <进程名>'
   # busybox 形态：
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'grep State /proc/$(pgrep -x <process-name>)/status'
   ```
   procps 输出 STAT 列显示 T；/proc 输出 `State:\tT (stopped)`（正常运行为
   `State:\tS (sleeping)`）
   **路径 B 效果判据形态完全不同（云内核 5.10 级）**：cgroup freezer
   冻结的进程 **/proc State 仍显示 S、内核栈仍显示原睡眠栈（如 hrtimer_nanosleep）、
   wchan=0、PF_FROZEN 不显示**——这些「看起来没冻结」是正常表象，**不是判死证据，
   严禁据此推断注入失败**。路径 B 的效果判据用以下两条：
   - **机制主证**：`freezer.state` 读回 `FROZEN`（空 cgroup 写 FROZEN 也立即成立，
     必须配合下一条行为学判据交叉）
   - **行为学判据（决定性）**：`timeout 5 kubectl exec <pod> -- echo ok` 挂住超时
     （rc=124/无输出）——FROZEN cgroup 内新进程无法调度；解冻后同命令恢复输出。
     这是把结构性约束③（冻结期 exec 挂住）反用为验证手段，零额外侵入。**两种有效
     形态**（按运行时版本，两种形态都出现过）：挂住超时，或 fail-fast
     拒绝——containerd 新版直接报错 `cannot exec in a paused container`（拒绝
     同样是冻结的直接信号，**严禁误读为通道故障**）；恢复对照统一为解冻后同命令
     立即回显
2. **服务不可用**——路径 A 从容器内自探（exec 仍可用：载体 sh 是新进程不受目标
   挂起影响）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- \
     wget -qO- --timeout=5 localhost:<port>
   ```
   确认请求超时。**路径 B 必须旁路**——被冻结的容器 exec 会挂住，借同节点正常
   Pod 访问目标 Pod IP（先 `kubectl get pod -o jsonpath={.status.podIP}`），
   或从 Endpoints 摘除侧确认（`kubectl get endpoints <service> -n <ns>`）
3. Pod 状态仍 Running、RESTARTS 与基线一致（预期阴性核对：无 Liveness 触发时
   RESTARTS 零漂移；路径 B 读 `freezer.state` 输出 `FROZEN` 而非 exec）
4. Liveness 探针事件（配置了才有）——探针失败由 kubelet 周期探测累积触发，存在
   传播延迟：**首查未见事件不构成反证**，挂起的直接证据是第 1 条的进程 T 状态
5. **爆炸半径交叉确认**：
   ```bash
   kubectl get node <node-name> -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}'
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name> --no-headers
   ```
   节点 Conditions 与基线一致；同节点 Pod 与基线清单一致（无 Evicted、无异常
   重启）——进程级故障不外溢
6. **持续性检查（必做）**——占用是状态型故障，挂起存活即故障存活：45s 后复查
   进程 STAT 仍为 T（或 /proc State 仍 stopped）
7. 路径 A 补自证链：`cat /tmp/procstop.evd` 含 `PROCSTOP_INJECTED`（含
   `PROCSTOP_RESTORED` 即窗口已关，如实报告实际时长）

**注入恢复**：
1. 手段1：`blade destroy <experiment_uid>`（向进程发送 SIGCONT 恢复执行）
2. 手段2 路径 A：定时器到期自动 SIGCONT + 写 `PROCSTOP_RESTORED`（主恢复路径）；
   提前恢复用演练步骤的恢复命令（幂等）
3. 手段2 路径 B：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'echo THAWED > <freezer.state 路径>'
   ```
4. 若 Liveness 探针已触发容器重启，等待新 Pod Ready 即可（重启即恢复）

**恢复验证**：
1. **进程恢复运行（主证，路径 A）**：/proc State 回到 `S (sleeping)`（或 procps STAT 列
   不再显示 T）
1a. **路径 B 恢复主证**：`freezer.state` 读回 `THAWED` + **exec 恢复响应**
   （`timeout 5 kubectl exec <pod> -- echo ok` 立即回显——与冻结期的挂住超时形成
   行为学对照）；进程 State 全程为 S 不是恢复证据（见注入验证第 1 条路径 B 说明）
2. 服务恢复响应：wget localhost:<port> 正常返回
3. 路径 A：证据文件含 `PROCSTOP_RESTORED`（双标记齐备 = 注入与还原全程自证）
4. Pod 状态 Running、RESTARTS 与基线一致（或按 Liveness 分支 +1 且与事件对账）
5. 爆炸半径最终对照：节点 Conditions 与同节点 Pod 清单与基线一致——进程级故障
   全程未外溢的最终证据

**基准事实**：
- **根因**：容器内应用主进程收到 SIGSTOP 信号被内核挂起，进程不退出但完全停止
  执行，无法处理任何请求
- **必现现象**：进程状态变为 T（Stopped）；应用端口请求全部超时；Pod 状态保持
  Running（进程 PID 仍存在）——T 状态与端口超时为路径 A 形态；**路径 B（freezer）
  形态：进程 State 保持 S、内核栈保持原样（内核不暴露冻结态），必现现象是
  exec 挂住超时 + freezer.state=FROZEN**（冻结中 `timeout 6
  kubectl exec -- echo` rc=1 无输出，解冻后立即回显 rc=0——冻结/解冻行为学
  对照干净可逆）
- **条件现象**：Liveness 探针超时失败 → 容器重启（仅配置了 Liveness 且达到
  failureThreshold 时；重启即恢复，故障窗口提前终止）
- **PID 1 边界**：目标进程为容器 PID 1 时 SIGSTOP/SIGCONT 被内核豁免（rc=0
  无效果），信号路径全部失效；cgroup freezer（调度层冻结）不受此限，对 PID 1
  有效——路径 B 是 PID 1 靶的唯一可行手段（`kill -STOP 1`
  rc=0 进程仍 S；非 PID 1 进程对照实验 T→S 干净往返）
