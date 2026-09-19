**用例名称** 应用主进程异常 导致 Pod_进程被杀死

**故障定位**：持续型故障——故障窗口内容器反复被终止重建，Pod RestartCount 持续增长；
超过 kubelet 退避阈值后进入 CrashLoopBackOff。**本用例不提供一次性注入**：单次杀死
随 kubelet 重建收敛（15-90s）即自愈，不构成有效演练窗口；`duration_seconds` 是必填的
故障窗口契约，未给定时先向用户确认，不得默认成一次性操作。
上游取证（chaosblade-exec-os `exec/process/process_kill.go`）：`pod-process kill` 仅在
实验创建时发送**一次**信号，`--timeout` 只控制实验记录何时销毁，**不存在**窗口内持续
杀进程的机制——任何声称 "timeout 窗口内持续杀" 的写法都是错误的，ChaosBlade kill 类
action 的一次性形态已被本用例废除。

**故障现象**：
1. 容器内应用主进程反复被杀死，容器因主进程退出而被 kubelet 重建
2. Pod RestartCount 在故障窗口内持续增长
3. 持续杀进程超过退避阈值后，Pod 状态进入 CrashLoopBackOff
4. Pod Events 中显示 `Back-off restarting failed container`

**资源准备**：
1. 确认应用 A 已正常运行，确认 `duration_seconds`（故障窗口）已明确
2. 确认目标 Pod 所在 namespace 和 labels
3. 确认目标容器名（循环里 `crictl ps --name` 依赖它；多容器 Pod 必须核对归属，
   跨 Pod 同名容器会命中多个）
4. 确认 `restartPolicy`：`kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.restartPolicy}`——`Never` 的 Pod 被杀后不会重建，故障变成永久停机而非反复重启，需先与用户确认

**演练步骤**：
1. 记录应用 A 当前 Pod 状态和 RestartCount：
   ```bash
   kubectl get pods -l <labels> -n <namespace> -o wide
   ```
2. 探测可行性（必须先探测再下结论，不得未探测即否决）：确认节点侧 `crictl` 可用
   （位于 `/usr/bin/crictl`，已连通 containerd；docker/CRI-O 运行时 CLI 不同，
   先 `chroot /host crictl version` 确认）与 debug pod 镜像可用
   （`<verified-cluster-image>`）。确实不可用时如实报告"持续注入不可行"并请用户
   确认，不得静默降级成其他形态。
3. 注入 —— 节点侧武装**双 systemd-run 的定时自停 stop 循环**（唯一形态，持续注入；
   循环本体与终止 timer 均为宿主机 transient unit——武装命令秒回即返，不依赖
   debug pod 存续、不撞 one-shot debug 命令的硬时限；恢复先于自断，与网络隔离类
   用例一致）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c '
     systemd-run --unit=blade-stoploop-<pod-name> sh -c "for i in \$(seq 1 <rounds>); do SID=\$(crictl pods --namespace <namespace> --name <pod-name> -q | head -1); CID=\$(crictl ps --pod \$SID --name <container-name> -q | head -1); [ -n \"\$CID\"] && crictl stop -t 0 \$CID; sleep <interval>; done" &&
     systemd-run --on-active=<duration>s --unit=blade-stoploop-term-<pod-name> sh -c "pkill -f \"crictl st[o]p -t 0\"; pkill -x crictl; true"
   '
   ```
   参数说明：
   - `<duration>`：故障窗口总时长（秒），取 `duration_seconds`；终止 timer 到期
     自动终止循环，**无需 destroy**；应 ≥ `<rounds> × <interval>` 并留余量
   - `<interval>`：两轮 stop 的间隔（秒），建议 ≥ 15，给 kubelet 留出重建与退避爬坡空间
   - `<rounds>`：循环轮数上限，是终止 timer 之外的第二重保险
   - 循环本体是 transient unit `blade-stoploop-<pod-name>.service`（`--unit=` 不带
     `--on-active`：武装即启动、后台运行，宿主机 PID 1 托管）；终止 timer 是
     `blade-stoploop-term-<pod-name>.timer`，到期执行 pkill 载荷——正则
     `crictl st[o]p -t 0` 命中循环 sh 的 cmdline 中的字面 `crictl stop -t 0`，
     循环 unit 主进程被杀、systemd 清收 cgroup 内残余 crictl（终止器自身 cmdline
     含 `st[o]p` 而非 `stop` 字面，不自匹配）
   - **循环体里的 `$(...)` 与 `$VAR` 必须整体转义**（`\$(...)`、`\$CID`）——外层
     `sh -c '...'` 的双引号载荷里，未转义的 `$(...)`/`$CID` 会在**武装时刻**被外层
     shell 提前展开：彼时变量未定义，`[ -n "" ]` 固化进命令文本恒假短路，
     循环每轮空转零注入（静默失败）
   - **systemd-run 的 timer 载荷必须写成单行**（双引号内不得换行）——载荷含换行时
     systemd 组装 transient unit 的 ExecStart 解析失败（journal 报
     `/run/systemd/transient/<unit>.service:4: Missing '='`，终止器形同虚设，复现）
   - `[ -n \"\$CID\" ]` 的 `]` 前必须有空格——POSIX test 语法要求，缺空格时 sh 报
     `[ missing ']'` 且循环每轮短路，crictl stop 从不执行（零注入静默失败，复现；
     守卫与 corpus 测试不执行 shell 语义，此类错误只能现场执行才能发现）
   - **每轮必须重新解析容器 ID**——kubelet 每轮重建容器后
     ID 会变化，把 ID 固化进循环（无论武装时刻还是第 1 轮），第 2 轮起就会空转；
     解析必须用 `--pod <podSandboxId>` 限定（先用 `crictl pods --namespace <namespace> --name <pod-name>` 按 Pod 名解析 sandbox）——同名容器遍布集群多个 Pod，不限定会停错 Pod 的容器
   - timer 载荷里的 `st[o]p` 是防自匹配技巧（括号法）：若直接写 `stop`，pkill -f 的
     正则命中不了目标却可能误伤自身命令行；写成 `st[o]p` 后模式文本不含字面 `stop`
     （自身 cmdline 不自匹配），而正则字符类 `[o]` 仍匹配 `o`（目标 `crictl stop`
     照常命中）。**不要用 `sto$((0+0))p` 算术拆开法**——timer 触发时第二层展开把
     `$((0+0))` 求值为 `0`，模式变 `sto0p` 永不匹配，终止器形同虚设
   - 循环与终止 timer 均由宿主机 PID 1 管理（双 transient unit），kubectl debug
     仅作一次性武装通道（命令秒回即返）——debug pod 删除后循环与自停恢复均不受
     影响。**不要把循环写成 debug 命令的前台载荷**（`... && sh -c "for ..."` 形态）：
     one-shot debug 命令有硬时限（120s 自动清理），前台循环会被掐断，批准的故障
     窗口无法兑现——工具运行时约束优先于任何文档形态
4. 观察 Pod 重启行为

> **语义（必须写进演练报告）**：`crictl stop` 停的是**整个容器**（该容器内所有进程
> 一起终止），不是容器内某个进程；它绕过 kubelet 直接操作容器运行时，kubelet 事后
> 按 restartPolicy 重建。exit code 由 `-t` 决定：`-t 0` 立即 SIGKILL（exit code 137，
> `Reason: Error`）；省略 `-t` 或给正值则先 SIGTERM、超时后才 SIGKILL（可能显示
> `Reason: Completed` 或 `Error`）。若演练目的包含**精确验证 OOM/崩溃告警的
> exit code 匹配规则**，按需选择 `-t` 取值并在报告中注明。

**注入验证**：
1. 执行 `kubectl get pods -l <labels> -n <namespace>`，确认 RESTARTS 数相比注入前增加
2. 确认容器发生过重建：优先从 `kubectl get pod <pod-name> -n <namespace> -o json` 读取 containerID 变化与 Last State（terminated 时间戳）——**故障生效期间容器正在崩溃，exec 大概率失败（unable to upgrade connection），不要把 exec 作为首选**；exec `ps aux` 看 PID 变化仅作容器已稳定时的补充手段
3. 执行 `kubectl describe pod <pod-name> -n <namespace>`，确认 Events 中有 `Back-off restarting failed container` 或 Last State 显示 terminated 且 reason 为 Error/Signal（第 1、3 步相互独立，应同批并行执行；只读探针一律单命令直发，不做 `sh -c 'a && b'` 串联——串联形态下一条失败会连坐整链）
4. CrashLoopBackOff 由构造成立：RESTARTS 持续递增（第 1 步）+ 第 3 步**任一分支**（Back-off 事件，或 Last State terminated/Error）即证明已越过退避阈值、kubelet 已进入退避控制——状态标签是这些量的渲染，不等其文本出现；首检已观测到标签时如实记录即可
5. **持续性检查（必做）**——判据是"无外部干预下杀进程仍在继续"。本步证明的是持续性（机制将运转到窗口结束），不是效果存在——效果已由第 1-3 步证明；对持续性命题，机制状态就是直接证据。三层按可得性取用，**任一层成立即完成本步，下层仅为上层不可查时的回退**：
   - **白盒主证（即时，单独充分）**：故障机制本身仍存活——`systemctl is-active blade-stoploop-<pod-name>` 为 active（循环 unit）且终止 timer pending（`systemctl list-timers` 含 `blade-stoploop-term-<pod-name>.timer`；经 debug pod `chroot /host` 查询）。机制存活且循环指向目标容器时，"持续在杀"由构造成立——本步即完成，无需佐证窗口
   - **有界佐证（仅当白盒不可查时）**：静观一个短窗口（≤60 秒）后再次执行 `kubectl get pods`，RESTARTS 无干预递增即完成。CrashLoopBackOff 深期 kubelet 退避可达 60-90 秒，窗口内未见递增不等于机制已停——此情形如实记录退避歧义即可
   - **黑盒回退（仅当以上均不可查时）**：停止一切操作、静观 1-2 分钟后再查 RESTARTS
   - 若机制在窗口内提前终止，说明故障窗口契约未达成，必须如实报告实际持续时长，不得报"持续注入已达成"

**注入恢复**：
1. 等待终止 timer 到期后循环自动终止（主保险）：pkill 载荷杀循环 sh，systemd
   清收 cgroup，循环 unit 随之 inactive
2. 如需提前终止，经 debug pod 依次执行两条命令——停掉终止 timer + 手动执行与
   timer 载荷同款的终止 pkill（pkill 命中的正是循环 sh——其 cmdline 含字面
   `crictl stop -t 0`；两条独立命令，不要串联；括号写法防 pkill 自匹配）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host systemctl stop blade-stoploop-term-<pod-name>.timer
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host pkill -f 'crictl st[o]p -t 0'
   ```

**恢复验证**：
1. 执行 `kubectl get pods -l <labels> -n <namespace>`，确认 Pod 状态为 Running 且 RESTARTS 不再增长
2. 执行 `kubectl exec <pod-name> -n <namespace> -- ps aux`，确认主进程稳定运行（PID 不再变化）
3. 确认应用 A 服务正常响应

**基准事实**：
- **根因**：容器内应用主进程被外部信号（SIGTERM/SIGKILL）反复杀死，容器退出并被 kubelet 重建，形成有界的自主重启风暴
- **必现现象**：Pod RestartCount 在窗口内持续增长；容器 Last State 为 terminated（Exit Code 非 0）；进程 PID 在重启后变化；Events 显示容器重启记录

---

**手段2（kubectl-native）**

> 上方演练步骤的节点侧 `systemd-run` 有界循环即唯一注入形态，**不存在独立的手段2**。
> 一次性形态已被本用例废除：单次容器内 kill、单次 `crictl stop` 只换来一次重启、随
> kubelet 重建即自愈；ChaosBlade `pod-process kill` 经上游源码取证为一次性信号投递
> （`--timeout` 只销毁实验记录），三者均不构成持续故障窗口，没有演练价值。

注意事项：
- 同名 transient unit 重复武装会报 `Unit ... was already loaded`（上次武装失败时
  unit 以 failed 状态残留所致）；重武装前先清理残留（经 debug pod `chroot /host`
  执行）：`systemctl stop <unit>; systemctl reset-failed <unit>`——循环 unit 用
  `blade-stoploop-<pod-name>.service`、终止 timer 用
  `blade-stoploop-term-<pod-name>.timer`
- **不需要也不应该手动重启 Pod** —— kubelet 会自动重建；手动 delete 会掩盖真实的自愈行为
- `restartPolicy: Never` 的 Pod **不会重建**，容器停了就一直停着，不构成反复重启故障，注入前必须确认（见资源准备第 4 条）
- 停的是整个容器 → **该容器内所有进程都终止**，若演练目的精确到"只杀某个非 init 进程而容器不重启"，本用例不适用（该语义没有持续形态——进程级单次 kill 属一次性，已废除），如实报告并请用户确认
- 不得用 `watch`/手动反复执行单轮 stop 堆次数——那依赖外部持续操作，操作一停故障即消失，不是本用例定义的自主持续故障
