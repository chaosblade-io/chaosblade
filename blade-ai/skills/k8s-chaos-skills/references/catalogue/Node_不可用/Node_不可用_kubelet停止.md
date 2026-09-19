**用例名称** kubelet停止 导致 Node_不可用

**故障现象**：
1. 节点状态由 Ready 变为 NotReady（kubelet 心跳中断约 40s 后由 node-controller 判定）
2. kube-node-lease 中该节点的 Lease renewTime 冻结，停止更新
3. 该节点上已有 Pod 继续运行但无法被 exec/logs 探入（exec 链路经过 kubelet）
4. 超过 Pod 的 tolerationSeconds（ACK 默认 300s）后，Pod 被驱逐到其他节点重建

**资源准备**：
1. 确认目标节点上运行的应用有多副本，单节点失联不造成业务整体不可用
2. 确认 chaosblade-operator 与目标节点上的 chaosblade-tool Pod 均 Running
3. **duration 必须显著小于该集群 Pod 的 tolerationSeconds（建议 ≤120s）**，避免触发 Pod 驱逐扩大爆炸半径
   甄别表一轮探测（勿用 `pods -A -o wide` 宽查询——宽列长输出经执行通道回传可能被截断丢失 tolerations 信息；下述 jsonpath 单节点紧凑形态每 Pod 一行四要素，回传完整）：
   ```bash
   kubectl get pods -A --field-selector spec.nodeName=<node-name> -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}  hostNet={.spec.hostNetwork}  owner={.metadata.ownerReferences[0].kind}  tol={.spec.tolerations}{"\n"}{end}'
   # 输出形态（tol 为 Go 语法表示）：
   # kube-system/kube-proxy-abcde  hostNet=True  owner=DaemonSet  tol=[map[effect:NoExecute key:node.kubernetes.io/not-ready operator:Exists]]
   ```
   甄别锚点：只看 `tol` 中 effect=NoExecute 的项——tolerationSeconds 缺省（nil）= 永久免疫（owner=DaemonSet 的 Pod 由控制器自动注入此类容忍）；有数值 = 驱逐倒计时秒数；key 限定且不匹配 node.kubernetes.io/not-ready / unreachable 标准键的容忍项等同无此项；NoSchedule/PreferNoSchedule 项不参与驱逐判定。消费口径：取全表**最小** tolerationSeconds 为窗口上限基准（最先被驱逐的 Pod 决定爆炸半径边界，勿逐 Pod 查询）；全表免疫时无驱逐风险，duration 上限改由外部自愈周期约束（见基准事实）

**演练步骤**：
1. 定位目标节点与承载应用的 Pod 分布
2. **手段1（blade）**（`--process` 按进程名 comm 匹配，`--timeout` 到期自动 SIGCONT 恢复）：
   ```bash
   # 在任一 chaosblade-tool Pod 内执行；--names 为节点名
   # --waiting-time 60s 规避 CRD Initialized 竞态（见注意事项）
   kubectl exec <chaosblade-tool-pod> -n <tool-namespace> -- blade create k8s node-process stop --process kubelet --names <node-name> --timeout <duration> --waiting-time 60s
   ```
   倒计时从武装时刻起算：--timeout 与注入命令一同下发原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `kubectl exec <chaosblade-tool-pod> -n <tool-namespace> -- blade destroy <experiment_uid>` 旧实验，再重跑上方注入命令重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）
3. 观察节点状态与心跳变化（必须走 apiserver 旁路，见注入验证的判读警告）

**注入验证**：

> ⚠️ **自断链路判读**：kubelet 被挂起后，kubectl exec/logs/debug 到该节点上任何 Pod 都会挂起直到超时——exec 链路本身经过 kubelet。这是**预期成功信号**，不要重试、不要换载体反复探入；立即改用不经过 kubelet 的 apiserver 侧观察（下面两条），拿到心跳冻结即可判定并收敛。
> 历史教训：曾因"exec 进节点看进程状态"这一必然挂起的观察方式，把实际生效的注入误判为"blade 报 Success 但无效"。
> **工具层 Error 判读（手段2 形态）**：注入命令的 debug 载体在 kubelet 停摆后状态冻结（kubelet 是 pod 状态上报通道，载体永不达 terminal 态）——工具层会报 one-shot 120s cap 类 Error。**Error 出现 + 下方效果判据成立 = 注入已成功**：命令已执行、命令失败回执 ≠ 注入失败，效果是唯一判据；勿据 Error 重发注入命令。
1. 执行 `kubectl get nodes`，确认目标节点约 40s 后变为 NotReady
2. 轮询 Lease 心跳（更灵敏，秒级可见冻结）：
   ```bash
   kubectl get lease <node-name> -n kube-node-lease -o jsonpath={.spec.renewTime}
   ```
   确认 renewTime 停止更新（正常约每 10s 更新一次）

**注入恢复**：
1. **提前恢复（首选）**：blade destroy 走 operator/CRD 链路，在 kubelet 停摆窗口内仍可秒级返回并立即恢复心跳：
   ```bash
   kubectl exec <chaosblade-tool-pod> -n <tool-namespace> -- blade destroy <UID>
   ```
2. **自恢复兜底**：不执行 destroy 时，`--timeout <duration>` 到期由 chaosblade 自动 SIGCONT 恢复
3. **手段2（blade 不可用时）**：kubectl debug node 载体 + systemd-run 武装（先武装定时恢复，再挂起；systemd 定时器由宿主 PID 1 管理，不依赖 debug pod 生命周期；恢复命令用幂等的 kill -CONT）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet -- chroot /host sh -c 'KPID=$(pgrep -x kubelet); systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-kubelet kill -CONT $KPID && kill -STOP $KPID'
   ```
   （unit 名用固定名；勿用 `$$` 后缀做唯一化——wiz 通道会把 `$$` 折叠为单个字面 `$`，经 systemd 转义成 `\x24` 后单元名与预期不符，见注意事项的 `$$` 通道折叠条目；单实例节点的单元名冲突由规划期 list-timers 预检兜底）
   ⚠️ 手段2一旦注入成功，kubelet 停摆期间**无法再向该节点投递任何新 Pod/exec**（包括第二个 debug pod），因此提前恢复必须走带外通道（SSH/host 通道执行 `kill -CONT <pid>`）；无带外通道时只能依赖已武装的 systemd-run 定时器，这正是 duration 必须有界的原因。
   ⚠️ **武装回执核验**：systemd-run 提交的回执若被工具层 Error 吞掉（见注入验证的工具层 Error 判读），恢复定时器的武装状态即不可确认——此时按「定时器可能未武装」的最坏情况预估爆炸半径（duration 无界），并优先依赖外部带外恢复通道；严禁假设定时器必然就位。窗口结束后可事后核验：`journalctl -u <unit>.timer` 记录 timer 的武装时刻（判读方法见注意事项的 journal 判读坑），据此复盘武装是否成功。

**恢复验证**：
1. 轮询 Lease renewTime 恢复更新（最灵敏的恢复信号）
2. 执行 `kubectl get nodes`，确认节点回到 Ready
3. 确认节点上应用 Pod 状态正常、无意外重启

**基准事实**：
- **根因**：kubelet 进程被 SIGSTOP 挂起，停止向 apiserver 上报心跳与节点状态
- **必现现象**：Lease renewTime 冻结；约 40s 后节点 NotReady；该节点 exec/logs 全部挂起
- **环境事实（外部节点自愈）**：部分托管集群存在云侧/平台侧节点自愈通道——节点 NotReady 数分钟后自动执行 daemon-reload + `systemctl restart kubelet`（常见形态：NotReady 后约 4 分钟，kubelet 被替换为新 PID 恢复 Ready，平台侧伴随 NodeRepairStart/Diagnose/Action/Succeed 事件链可作归因佐证）。影响：①本类故障的实际窗口可能远小于设定 duration（被外部自愈截断，常见 257-292s vs 600s 设定值）②自恢复定时器的 fire 时刻会被 daemon-reload 推迟（重置为 reload 时刻 + OnActiveSec），fire 时恢复常已被抢跑、`kill -CONT` 旧 PID 落空（service 进入 failed 残留——该残留与 timer 残留同属 execute 收尾清理义务处置，见注意事项 timer 条目）③`systemctl restart` 的停止序列会向 SIGSTOP 状态的 kubelet 发 SIGCONT（唤醒后再 SIGTERM），节点日志中出现挂起进程的活动输出属正常现象。规划期对「无自动修复」的探测结论只覆盖集群侧 controller，节点侧自愈通道不在其探测范围内，不得据此类结论排除外部抢跑。

**注意事项**：
- blade create 首次可能报 `unexpected status, expected status: create, but the real status: Initialized`——这是 CRD 初始化竞态（加 `--waiting-time 60s` 可规避）；若已报此错，故障可能已实际注入，先用 Lease/节点状态确认再决定重试，并用该 UID 执行 destroy，不要盲目重复 create
- `--process kubelet` 按进程名（comm）匹配，kubelet 的 comm 唯一；节点上虽有多个进程的 cmdline 含 "kubelet" 字样（csi-node-driver-registrar 等），不会误伤。需要精确打击时可用 `--pid <kubelet-pid>`（在 tool pod 内 `pgrep kubelet` 获取，注意部分 BusyBox 构建的 `pgrep -x` 行为异常，建议不带 -x 并核对唯一性）
- blade status 在 timeout 到期后可能仍显示 Success 不翻状态，判断恢复与否以 Lease/节点状态为准
- `systemd-run --on-active` 武装的 transient timer 会被宿主上的 `systemctl daemon-reload` 重置计时基准（fire 时刻推迟 = reload 时刻 + OnActiveSec）；timer fire 时若 kubelet 已被外部替换为新 PID，ExecStart 里的旧字面 PID 使 `kill -CONT` 落空、service 进入 failed 残留——**timer/service 残留清理（`systemctl stop` + `reset-failed`）是注入方 execute 计划的收尾 mutation step 义务**（kubelet 恢复后、timer fire 前投递清理载体执行——fire 前清除同时规避 stray `kill -CONT` 旧 PID 与 failed 残留两类终态；无需依赖 reload 时刻取证即可安全执行）；recover 对该清理幂等兜底（unit not loaded 即 no-op，非失败）；恢复验证的裁决判据不变（仍须含残留清零确认）
- journal 判读坑：systemd 启动 transient **timer** 时会记录一条 `Started <命令行>` 的 job 完成消息（transient timer 的 Description 即命令行文本），挂在 timer unit 的 journal 流且文本与 service 执行消息完全相同——判读延迟语义是否生效须以 fire 时刻的证据（service 流的 `Started` + 实际效果）为准，勿把**武装时刻**的这条 timer 启动消息误读为「service 立即执行、定时延迟失效」；事后核验武装时刻用 `journalctl -u <unit>.timer`
- 经验观察：停摆窗口内 `blade destroy` 约 4s 返回成功且心跳立即恢复（机制未完全澄清，作为经验事实记录；若 destroy 挂起超时，等待 `--timeout` 自恢复兜底）
- `$$` 通道折叠（wiz 通道行为约束）：命令经 wiz 通道下发时，`$$` 会被远端执行层折叠为单个字面 `$`（`$VAR` 变量引用与 `$(...)` 命令替换不受影响，仅 `$$` 特殊）——systemd-run 的 `--unit=name-$$` 预期「展开为 PID 做唯一化」在该通道不可达：单元名会变成 `name-$`、systemd 报 `Invalid unit name escaped as \x24`。工具层各解析环节（参数切分/命令重组/引号包装）均正确传递 `$$`，折叠发生在 wiz 平台远端执行层。凡含 `$$` 语义的命令模板在 wiz 通道一律改用固定单元名 + 冲突预检。
