**用例名称** Sidecar进程被杀死 导致 Container_进程异常

**故障定位**：持续型故障——故障窗口内 Sidecar 容器反复被终止重建，其 restartCount
持续增长，主容器不受影响。**本用例不提供一次性注入**：单次杀进程/单次停容器随 kubelet
重建一次即自愈，不构成有效演练窗口；`duration_seconds` 是必填的故障窗口契约，未给定
时先向用户确认。上游取证（chaosblade-exec-os `exec/process/process_kill.go`）：
`container-process kill` 仅在实验创建时发送**一次**信号，`--timeout` 只控制实验记录
何时销毁，**不存在**窗口内持续杀进程的机制——ChaosBlade kill 类 action 的一次性形态
已被本用例废除。

**故障现象**：
1. Sidecar 容器反复被终止重建，Pod 内其他容器（主容器）不受影响，Pod 本身不重启
2. Sidecar 提供的能力在窗口内反复中断（如日志停止采集、代理不可用、监控数据丢失）
3. Pod Events 中反复出现该容器重启记录，Pod 整体状态仍为 Running

**资源准备**：
1. 确认目标 Pod 包含多个容器，明确 Sidecar 容器名称
2. 确认目标 Pod 所在 namespace 和 labels
3. 记录注入前 Sidecar 容器的 Container ID 和 restartCount
4. 确认 `duration_seconds`（故障窗口）已明确
5. **多容器靶接线（靶为单容器 Pod 时）**：业务靶常见单容器形态——用 JSON patch 给靶 Deployment 模板追加一个 sidecar 容器（演练资产，结束拆线归还）。sidecar 须同时承载**可受害的服务进程**（验证「重启期间服务中断」现象面）：socat 监听 + sleep 保活（镜像选**节点缓存或 VPC registry 可达**镜像，VPC 受限网络下 docker.io 公网镜像会 `ImagePullBackOff`；terway 类 CNI 镜像自带 socat，节点缓存零拉取风险）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"add","path":"/spec/template/spec/containers/-","value":{"name":"drill-sidecar","image":"<节点缓存镜像>","command":["sh","-c","socat TCP-LISTEN:8080,fork,reuseaddr EXEC:/bin/cat & sleep 7200"]}}]'
   kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=120s
   ```
   拆线用 `kubectl patch --type='json' -p='[{"op":"remove","path":"/spec/template/spec/containers/<sidecar索引>"}]'` 后 rollout status 等收敛。**拆线归恢复生命周期**：接线资产拆除不进执行计划（SKILL.md 安全红线「拆线不进执行计划」）——执行器下发完「接线+武装+注入」即止，等待恢复/拆线由 recover 生命周期接管

**演练步骤**：

> **爆炸半径分类（定案）**：`target-only`——`crictl stop` 以 `--pod <podSandboxId>` 把解析限定到目标 Pod 自己的 sandbox，只停目标 sidecar 容器；变更面 = 靶 Pod 单容器生命周期 + 靶节点上两个 transient unit（循环 service + 终止 timer，名称含 pod 名锚定，窗口结束自动 inactive，无跨靶扩散）+ 靶 Deployment 模板（接线/拆线），不触及任何非靶资源。集群里同名 sidecar 容器遍布多 Pod——sandbox 解析不经 `--namespace/--name` 双重限定时会停错 Pod 的容器（scope 逃逸级事故）。
> **恢复机制边界（定案）**：本用例恢复是**节点侧 systemd 型**（宿主机 PID 1 托管 transient unit，timer 到期 pkill 循环），不是 API 对象写——恢复载体标准件判据一不满足，**勿建四件套**；与容器内进程型自恢复同类、与载体（API 型）是两类机制。

1. 确认 Pod 内容器列表，获取 Sidecar 容器名称：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].name}'
   ```
2. 记录注入前 Sidecar 容器的 Container ID 与各容器 RestartCount：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[?(@.name=="<sidecar-container-name>")].containerID}'
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[*].restartCount}'
   ```
3. 定位目标节点与 sidecar 的 pod sandbox：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.nodeName}
   ```
4. 注入 —— 节点侧武装**双 systemd-run 的定时自停 stop 循环**（唯一形态，持续注入；
   循环本体与终止 timer 均为宿主机 transient unit——武装命令秒回即返，不依赖
   debug pod 存续、不撞 one-shot debug 命令的硬时限；从节点侧反复停掉该 sidecar
   容器，kubelet 每轮只重建被停的那一个容器）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c '
     systemd-run --unit=blade-stoploop-sidecar-<pod-name> sh -c "for i in \$(seq 1 <rounds>); do SID=\$(crictl pods --namespace <namespace> --name <pod-name> -q | head -1); CID=\$(crictl ps --pod \$SID --name <sidecar-container-name> -q | head -1); [ -n \"\$CID\" ] && crictl stop -t 0 \$CID; sleep <interval>; done" &&
     systemd-run --on-active=<duration>s --unit=blade-stoploop-term-sidecar-<pod-name> sh -c "pkill -f \"crictl st[o]p -t 0\"; pkill -x crictl; true"
   '
   ```
   参数与要点：
   - `<duration>`：故障窗口总时长（秒），取 `duration_seconds`；终止 timer 到期
     自动终止循环，**无需 destroy**；应 ≥ `<rounds> × <interval>` 并留余量
   - `<interval>`：两轮 stop 的间隔（秒），建议 ≥ 15，给 kubelet 留出重建与退避爬坡空间
   - `<rounds>`：循环轮数上限，是终止 timer 之外的第二重保险
   - 循环本体是 transient unit `blade-stoploop-sidecar-<pod-name>.service`
     （`--unit=` 不带 `--on-active`：武装即启动、后台运行，宿主机 PID 1 托管）；
     终止 timer 是 `blade-stoploop-term-sidecar-<pod-name>.timer`，到期执行 pkill
     载荷——正则 `crictl st[o]p -t 0` 命中循环 sh 的 cmdline 中的字面
     `crictl stop -t 0`，循环 unit 主进程被杀、systemd 清收 cgroup 内残余
     crictl（终止器自身 cmdline 含 `st[o]p` 而非 `stop` 字面，不自匹配）
   - **systemd-run 的 timer 载荷必须写成单行**（双引号内不得换行）——载荷含换行时
     systemd 组装 transient unit 的 ExecStart 解析失败（journal 报 `Missing '='`，
     终止器形同虚设，复现），因此循环体用分号连接写在一行内
   - **循环体里的 `$(...)` 与 `$VAR` 必须整体转义**（`\$(...)`、`\$CID`）——外层
     `sh -c '...'` 的双引号载荷里，未转义的 `\$()` 会在**武装时刻**被外层 shell 提前
     展开：彼时 `\$SID` 未定义，空值被 word-split 吞掉后 `crictl ps --pod` 会把
     `--name` 当作 pod 值 → 查询无匹配 → `CID=` 恒空，循环每轮空转零注入
     （静默失败；尾部未转义的 `\$CID` 同样被提前展开为空，`crictl stop -t 0` 无目标）
   - **test 命令右括号前必须留空格**：`[ -n \"\$CID\" ]` 的 `]` 是独立参数——写成
     `[ -n \"\$CID\"]`（缺空格）时引号扩展值与右括号被拼成一个 word，`test`
     报 `missing ']'` 返回非零，`&&` 后的 `crictl stop` 永不执行——循环 active
     running 但 restartCount 全程 0（静默空转，与转义缺失同表象、不同根因；
     转义正确也会栽在这里）
   - **每轮必须重新解析容器 ID**——kubelet 每轮只重建被停的 sidecar 容器，其 ID 每轮都变；
     固化第 1 轮 ID 会导致第 2 轮起空转
   - **必须用 `--pod <podSandboxId>` 把解析限定到目标 Pod**——集群里同名 sidecar
     （如 `istio-proxy`）遍布多个 Pod，不限定会停错 Pod 的 sidecar；sandbox 解析用
     `crictl pods --namespace <namespace> --name <pod-name>` 按 Pod 名过滤，确保命中
     目标 Pod 自己的 sandbox；sandbox 本身不随容器重启而变化，每轮重新解析即可
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
5. 观察 Sidecar 容器重启行为及主容器是否受影响

> **语义（必须写进演练报告）**：`crictl stop` 停的是**整个 sidecar 容器**（其内所有
> 进程一起终止），不是容器内某个进程；它绕过 kubelet 直接操作运行时，kubelet 事后
> 发现容器终止并重建。exit code 由 `-t` 决定：`-t 0` 立即 SIGKILL（exit code 137）。
> 若演练目的包含**精确验证 exit code 匹配规则**，按需选择 `-t` 取值并在报告中注明。
> 若演练目的精确到"只杀 sidecar 内某个非 init 进程而容器不重启"，本用例不适用
> （进程级单次 kill 属一次性形态，已废除），如实报告并请用户确认。

**注入验证**：
> **verify 阶段行为探针不可用（定案）**：verify 只读门禁可能把 socat/wget 服务连通性探针判为 load-generating 拒绝（基线采集阶段不受限）。被拒时**勿重试同类命令**，按状态型判据裁决（restartCount 递增即故障在——kubelet 重建是机制执法的直接回显）+ deviation 文档化（记录被拒命令与原因），服务中断行为对照由恢复后带外核实补强（窗口结束后 socat 探针恢复回显 = 服务曾被中断的反向证明）。
> **白盒取证通道注意**：节点侧 `systemctl is-active`/`list-timers` 查询须在长驻 debug 载体内 exec（两步法），勿用 one-shot `kubectl debug node` 直查——wiz 通道对 one-shot debug 命令不回流输出，静默失败会被误读为 unit 不存在。

1. 执行 `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses}'`，确认目标 Sidecar 容器 restartCount 增加
2. 确认主容器仍正常运行，restartCount 未变化
3. 执行 `kubectl describe pod <pod-name> -n <namespace>`，确认 Events 中有目标容器重启记录（第 1-3 步相互独立，可同批并行；只读探针一律单命令直发，不做 `sh -c 'a && b'` 串联——串联形态下一条失败会连坐整链）
4. 验证 Sidecar 提供的服务在重启期间中断（如代理端口不可达、日志缺失）
5. **持续性检查（必做）**——判据是"无外部干预下停容器仍在继续"。本步证明的是持续性（机制将运转到窗口结束），不是效果存在——效果已由第 1-4 步证明；对持续性命题，机制状态就是直接证据。三层按可得性取用，**任一层成立即完成本步，下层仅为上层不可查时的回退**：
   - **白盒主证（即时，单独充分）**：故障机制本身仍存活——`systemctl is-active blade-stoploop-sidecar-<pod-name>` 为 active（循环 unit）且终止 timer pending（`systemctl list-timers` 含 `blade-stoploop-term-sidecar-<pod-name>.timer`；经 debug pod `chroot /host` 查询）。机制存活且循环指向目标容器时，"持续在停"由构造成立——本步即完成，无需佐证窗口
   - **有界佐证（仅当白盒不可查时）**：静观一个短窗口（≤60 秒）后再次查看各容器 restartCount，sidecar 计数无干预递增、主容器计数始终不变即完成。CrashLoopBackOff 深期 kubelet 退避可达 60-90 秒，窗口内未见递增不等于机制已停——此情形如实记录退避歧义即可
   - **黑盒回退（仅当以上均不可查时）**：停止一切操作、静观 1-2 分钟后再查 restartCount
   - 若机制在窗口内提前终止，说明故障窗口契约未达成，必须如实报告实际持续时长，不得报"持续注入已达成"

**注入恢复**：
1. 等待终止 timer 到期后循环自动终止（主保险）：pkill 载荷杀循环 sh，systemd
   清收 cgroup，循环 unit 随之 inactive
2. 如需提前终止，经 debug pod 依次执行两条命令——停掉终止 timer + 手动执行与
   timer 载荷同款的终止 pkill（pkill 命中的正是循环 sh——其 cmdline 含字面
   `crictl stop -t 0`；两条独立命令，不要串联；括号写法防 pkill 自匹配）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host systemctl stop blade-stoploop-term-sidecar-<pod-name>.timer
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host pkill -f 'crictl st[o]p -t 0'
   ```

**恢复验证**：
1. 执行 `kubectl get pod <pod-name> -n <namespace>`，确认 Pod 状态为 Running 且 Sidecar 容器 restartCount 不再增长
2. 确认 Sidecar 容器内进程稳定运行（容器 ID 不再变化）
3. 确认 Sidecar 提供的服务恢复正常（socat 探针回显——窗口结束后受害服务的恢复回显，同时是「窗口内服务曾被中断」的反向证明）
4. **节点侧终态（经长驻 debug 载体 chroot /host 查询）**：循环 unit 已 inactive/failed 终态、终止 timer 已消失（`systemctl list-timers` 无残留）——节点零残留
5. **拆线归还（接线靶）**：patch remove sidecar 容器 + `kubectl rollout status` 等收敛，靶 Deployment 回单容器基线（同 RS 归还为模板精确回滚的数学证据）；如 Pod 因容器重建已漂移节点，以当次调度为准如实记录

**基准事实**：
- **根因**：Sidecar 容器被节点侧循环反复终止（绕过 kubelet 直接操作运行时），kubelet 每轮检测后只重建该容器，Pod 内其他容器不受影响，形成有界的自主重启风暴
- **必现现象**：目标 Sidecar 容器 restartCount 在窗口内持续增长；容器 Last State 为 terminated（Exit Code 非 0）；主容器 restartCount 不变且持续运行；Pod 整体状态保持 Running

---

**手段2（kubectl-native）**

> 上方演练步骤的节点侧 `systemd-run` 有界循环即唯一注入形态，**不存在独立的手段2**。
> 一次性形态已被本用例废除：容器内单次 kill、单次 `crictl stop` 只换来一次重启、随
> kubelet 重建即自愈；ChaosBlade `container-process kill` 经上游源码取证为一次性信号
> 投递（`--timeout` 只销毁实验记录），均不构成持续故障窗口，没有演练价值。

注意事项：
- 同名 transient unit 重复武装会报 `Unit ... was already loaded`（上次武装失败时
  unit 以 failed 状态残留所致）；重武装前先清理残留（经 debug pod `chroot /host`
  执行）：`systemctl stop <unit>; systemctl reset-failed <unit>`——循环 unit 用
  `blade-stoploop-sidecar-<pod-name>.service`、终止 timer 用
  `blade-stoploop-term-sidecar-<pod-name>.timer`
- **不要手动 delete Pod** —— kubelet 会单独重建那个容器；delete 整个 Pod 会掩盖
  「sidecar 单独重启、主容器存活」这一关键现象
- `restartPolicy: Never` 的 Pod 容器不会重建，注入前先确认：
  `kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.restartPolicy}`
- 不得用 `watch`/手动反复执行单轮 stop 堆次数——那依赖外部持续操作，操作一停故障即消失，不是本用例定义的自主持续故障
- **istio-proxy 类 sidecar 被停会中断整个 Pod 的出入流量**（它是流量代理），
  影响面等同于主容器不可用 —— 爆炸半径评估时要按此计
- `crictl` 位于节点 `/usr/bin/crictl`，已连通 containerd；其它运行时的 CLI 不同，
  先用 `chroot /host crictl version` 确认
