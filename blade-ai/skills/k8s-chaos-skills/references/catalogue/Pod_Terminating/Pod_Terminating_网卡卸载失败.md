**用例名称** 网卡卸载失败 导致 Pod_Terminating

**故障现象**：
1. Pod 状态长时间停留在 Terminating
2. 容器已停止，但 Pod sandbox 清理失败
3. Events 或 kubelet 日志中显示 CNI DEL 调用失败或网络资源释放异常

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群使用 ENI/Terway 等需要显式清理网络资源的 CNI 插件
3. 确认监控系统可观测 Pod 状态和 CNI 插件日志

**演练步骤**：
1. 定位应用 A 的 Pod 所在节点
2. 使用 chaosblade 挂起节点上的 CNI 插件进程（如 terway-daemon），模拟 CNI 响应异常：
   ```bash
   blade create k8s node-process stop \
     --names <节点名> \
     --process terway \
     --timeout <duration>

   ```
   （不建议改用删除 CNI Pod 的路径：cordon 挡不住 DaemonSet 重建——DS controller 直接绑
   nodeName 绕过调度器，删除后秒级重建，无法维持故障窗口，见手段2 方式B）

   进程名以当次探测为准：Terway 的 daemon 进程名是 terwayd、伴生 terway-cli——`--process`
   按关键词匹配会同时命中两者；注入前先在节点上 `pidof terwayd` 核对目标进程存在
3. 删除应用 A 的 Pod，触发 Terminating 流程
4. 观察 Pod Terminating 状态

**注入验证**：
1. ⚠️ **按注入形态判读，两种形态判据相反**：
   - **daemon 被杀形态**（kill/crash）：unix socket 关闭 → terway CNI binary connect
     立即失败（快速失败路径）→ kubelet 仍能完成 Pod 删除（不卡 Terminating）——此形态
     **不要以「Pod 卡 Terminating」为判据**，否则会把已生效的注入误判为失败
   - **SIGSTOP 挂起形态**（blade `node-process stop` 与手段2 方式A）：unix socket 保持
     LISTEN、内核照常完成 connect → CNI binary 发送请求后挂起等待响应直至超时（挂起
     超时路径）→ Pod 真卡 Terminating，FailedKillPod 事件周期性重试——此形态「卡
     Terminating」与 kubelet 日志 CNI DEL 失败**两判据同现**，仍以 CNI DEL 失败记录为主
     证据、Terminating 停留为辅证（实测：600s 窗口内 FailedKillPod ×25）
2. 查看 kubelet 日志（`journalctl -u kubelet`），确认有 CNI DEL 调用失败或超时的记录（主证据，必做）。journalctl 不可达（节点无权限/验证载体不可 exec）时，目标 Pod 的 FailedKillPod 事件为等价同源通道——事件由 kubelet 自身产生写入 API，文本含 plugin type 与 eni.socket 报错原文，经 `kubectl describe pod` 读取即主证据成立（记 deviation）
3. 确认 CNI 插件进程处于 T（Stopped）状态。liveness probe 是否导致窗口坍缩须先做
   **操作性判定**：`ss -lntp` 查 probe 端口持有者并核对其与被 STOP 的 daemon 的进程
   关系——probe 端口由 daemon **子进程**持有时（Terway 双容器形态：policy 容器 probe
   9099 由 terwayd 的子进程 cilium-agent 持有），SIGSTOP 不传播子进程 → probe 持续
   通过 → 容器不重启，故障窗口 = 批准 duration 全程；probe 端口由 daemon 自身持有时
   才发生 <60s 重启自愈（窗口坍缩）。判定在规划期一次探测完成（进程树 + 端口归属），
   勿照抄「<60s 坍缩」预判

**注入恢复**：
1. 恢复 CNI 插件进程：等待 chaosblade 超时或执行 `blade destroy <UID>`
2. 若删除了 CNI Pod：uncordon 节点，等待 CNI DaemonSet Pod 重建
3. kubelet 将自动重试 sandbox 清理

**恢复验证**：
1. 确认 CNI 插件进程恢复正常
2. 执行 `kubectl get pods`，确认 Pod 删除正常完成（实际形态下删除本来就不被阻塞，此条为不退化检查而非恢复信号）
3. 确认节点网络资源（ENI/IP）已释放（CNI DEL 失败期间可能产生泄漏残留，daemon 恢复后的回收存在异步性，以 terwayd 日志/节点 ENI 实际状态核对为准）
4. 确认新 Pod 可以正常创建和分配网络

**基准事实**：
- **根因**：CNI 插件异常或不可用，导致 Pod 删除时网卡/ENI 资源无法正常释放，sandbox 清理失败，Pod 卡在 Terminating
- **必现现象**：kubelet 日志显示 CNI DEL 失败；CNI 插件进程 stopped/不可用（形态分叉见注入验证第 1 条：daemon 被杀→不卡 Terminating；SIGSTOP 挂起→卡 Terminating + FailedKillPod 周期重试；潜在 ENI/IP 泄漏）

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令模拟 CNI 插件异常。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+），或可操作 CNI DaemonSet

注入命令（**先武装定时恢复，再注入**）：
```bash
# 方式A：通过 kubectl debug node 挂起 CNI 插件进程
# ⚠️ 先用 systemd-run 登记定时 SIGCONT 再 STOP —— 定时器由宿主机 systemd(PID 1) 管理，
#    不受 debug pod 生命周期影响
# ⚠️ 进程名以当次探测为准：Terway 的 daemon 进程名是 terwayd（伴生 terway-cli），不是
#    terway-daemon——照抄错误进程名会使 $() 展开为空、STOP 落空（注入静默失败）；
#    注入前先 kubectl exec <debug-pod> -- chroot /host sh -c 'pidof terwayd' 确认
# ⚠️ SIGSTOP 挂起形态的判据见「注入验证」第 1 条形态分叉：socket 保持 LISTEN → CNI
#    binary 挂起至超时 → Pod 真卡 Terminating；liveness probe 是否坍缩窗口见第 3 条
#    操作性判定（Terway 双容器形态：9099 由子进程 cilium-agent 持有，窗口=duration 全程）
# ⚠️ 不设计 early recovery：daemon 挂起形态按批准 duration 全窗设计。载体武装 timer
#    后即处于 recovery_armed 锁定态，fire 前对其派发第二次 mutation（含提前 kill -CONT）
#    会被 target_guard 拒绝；且提前恢复会使 verify 失去故障态采样（T 态进程、
#    FailedKillPod 累积都在窗口内取得）——窗口尾部与 verify 报告生成天然重叠，
#    全窗设计即零浪费
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemd-run --on-active=<recovery-seconds>s --unit=blade-cont-cni \
     sh -c "kill -CONT \$(pidof terwayd)" && \
   kill -STOP $(pidof terwayd)'
# ⚠️ 回退链已移除：pidof 未命中即中止注入、人工排查进程名——`|| pidof cilium-agent
#    || pidof calico-node` 会盲停其他活数据面进程（Cilium/Calico 进程名仅作探测参考，
#    不属于本注入的目标集）

# 方式B：删除节点上的 CNI Pod（先武装定时 uncordon 再注入；定时器必须经 kubectl exec
#        载体派发——顶层裸 sh -c 不被工具守卫放行，载体需含 kubectl 与集群凭证；
#        恢复命令幂等，迟到重复执行无副作用）
kubectl exec <载体Pod> -n <载体ns> -- sh -c '( sleep <duration>; kubectl uncordon <node-name> ) >/tmp/restore.log 2>&1 & echo armed'
kubectl cordon <node-name>
kubectl delete pod -n kube-system -l app=terway-eniip --field-selector spec.nodeName=<node-name>
# ⚠️ label 以当次探测为准：Terway DaemonSet 的 Pod label 是 app=terway-eniip（不是 app=terway），
#    删除前先 kubectl get pods -n kube-system -o wide | grep -i terway 核对
# ⚠️ cordon 挡不住 DaemonSet 重建：DS controller 直接绑 nodeName 绕过调度器（删除后
#    秒级重建），「先 cordon 防重建」的前提不成立——本方式在 DS 形态 CNI 上无法维持故障窗口
```
方式B 倒计时从武装时刻起算：武装（exec 定时器）与注入（cordon + 删 Pod）必须是紧邻步骤（≤60s）；武装后发生任何修复须先 `kubectl exec <与武装时相同的载体Pod> -n <载体ns> -- sh -c 'pkill -f uncordo[n]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）；方式A systemd 定时器的重武装同样先停旧 unit 再重跑武装命令

恢复命令（带外兜底；正常路径为武装定时器到期自动 SIGCONT，Agent 在线段不执行提前
恢复——见方式A 注释 recovery_armed 锁定与全窗设计）：
```bash
# 方式A：恢复 CNI 插件进程（SIGCONT 幂等，武装的定时器后续再触发也无副作用）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'kill -CONT $(pidof terwayd)'
# 方式B：Agent 主动 uncordon 节点（定时器到期也会自动 uncordon，幂等），等待 DaemonSet 重建 CNI Pod
kubectl uncordon <node-name>
# 删除 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
```

注意事项：
- CNI 插件名称因集群而异：Terway（阿里云）、Cilium、Calico 等，需根据实际环境探测进程名与
  Pod label（Terway：进程 terwayd，Pod label app=terway-eniip）
- 方式B 删除 CNI Pod 后，DaemonSet controller 直接绑 nodeName 重建（cordon 无效，秒级
  重建），故障窗口不可维持——本用例在 DS 形态 CNI 上仅方式A 可用
- 自恢复机制：方式 A 为宿主机 systemd-run 定时 SIGCONT（timer 由 systemd 管理，到期自动恢复）；
  Terway 的 liveness probe 自愈仅当 probe 端口由 daemon 自身持有时成立（见注入验证第 3 条
  操作性判定；双容器形态下 probe 在 policy 容器且端口由子进程持有，容器不重启、
  唯一自愈即武装定时器）；
  方式 B 为 kubectl exec 载体内定时 uncordon（定时器存活于载体 Pod，Pod 重建会丢失定时器，
  届时仍需 Agent 主动执行或人工 uncordon 兜底）
