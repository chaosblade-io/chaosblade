**用例名称** 应用主进程异常 导致 Pod_进程被杀死

**故障现象**：
1. 容器内应用主进程被杀死，容器因主进程退出而重启
2. Pod RestartCount 持续增长
3. 若持续杀进程超过退避阈值，Pod 状态可能进入 CrashLoopBackOff
4. Pod Events 中显示 `Back-off restarting failed container`

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认目标 Pod 所在 namespace 和 labels
3. 确认容器内实际进程名（不可凭服务名猜测）

**演练步骤**：
1. 记录应用 A 当前 Pod 状态和 RestartCount：
   ```bash
   kubectl get pods -l <labels> -n <namespace> -o wide
   ```
2. **验证实际进程名（必须）** — 进入容器确认目标进程的二进制名称：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ps aux
   ```
   注意：实际进程名可能与服务名不同（如 prometheus 服务的实际二进制为 `prometheus-agent-linux`），必须以 ps 输出为准
3. 使用 ChaosBlade 注入进程杀死故障：
   ```bash
   blade create k8s pod-process kill \
     --process <实际进程名> \
     --signal 15 \
     --namespace <namespace> \
     --labels <labels> \
     --timeout <秒> \
     --kubeconfig <路径>
   ```
   说明：`--signal 15` 发送 SIGTERM，如需强制杀死可用 `--signal 9`（SIGKILL）
4. 观察 Pod 重启行为

**注入验证**：
1. 执行 `kubectl get pods -l <labels> -n <namespace>`，确认 RESTARTS 数相比注入前增加
2. 执行 `kubectl exec <pod-name> -n <namespace> -- ps aux`，确认主进程 PID 已变化（容器重启后 PID 重新分配）
3. 执行 `kubectl describe pod <pod-name> -n <namespace>`，确认 Events 中有 `Back-off restarting failed container` 或 Last State 显示 terminated 且 reason 为 Error/Signal
4. 若 timeout 期间持续杀进程，确认 Pod 状态是否进入 CrashLoopBackOff
5. **持续性检查（"反复重启"意图必须做）**：注入动作结束后**停止一切操作、静观 1-2 分钟**，
   再次执行 `kubectl get pods -l <labels> -n <namespace>`，确认 RESTARTS 在无外部干预下仍在递增。
   若计数不再增长，说明达成的是**离散重启**（每次重启都靠外部触发），不是持续的"反复重启"状态，
   验证结论必须如实写"离散重启 N 次"，不得报"反复重启已达成"

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动停止杀进程
3. 说明：进程被杀后容器 entrypoint 会自动拉起主进程，ChaosBlade 的 timeout 控制的是"持续杀进程"的时长，超时后不再杀进程，容器自行恢复
4. 路径 B 持续模式（见降级方案）：等待 `systemd-run` timer 到期后循环自动终止；
   如需提前终止，经 debug pod 执行：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host systemctl stop <unit-name>
   ```

**恢复验证**：
1. 执行 `kubectl get pods -l <labels> -n <namespace>`，确认 Pod 状态为 Running 且 RESTARTS 不再增长
2. 执行 `kubectl exec <pod-name> -n <namespace> -- ps aux`，确认主进程稳定运行（PID 不再变化）
3. 确认应用 A 服务正常响应

**基准事实**：
- **根因**：容器内应用主进程被外部信号（SIGTERM/SIGKILL）杀死，导致容器退出并被 kubelet 重启
- **必现现象**：Pod RestartCount 增长；容器 Last State 为 terminated（Exit Code 非 0）；进程 PID 在重启后变化；Events 显示容器重启记录

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效进程杀死注入。
> 两条路径按容器内是否有 `kill`/`pgrep` 二选一。

---

**路径 A —— 容器内有 `kill` 和 `pgrep`**

前提条件：容器内需有 `kill` 和 `pgrep` 工具。先确认：
```bash
kubectl exec <pod-name> -n <namespace> -- sh -c 'command -v kill; command -v pgrep'
```
两者都有输出才走本路径；任一缺失（distroless / scratch 极简镜像的常态）走路径 B。

注入命令：
```bash
# 杀死目标进程（SIGTERM）
kubectl exec <pod-name> -n <namespace> -- sh -c 'kill -15 $(pgrep -f <process-name>)'
# 强制杀死（SIGKILL）：
kubectl exec <pod-name> -n <namespace> -- sh -c 'kill -9 $(pgrep -f <process-name>)'
```

恢复命令：
```bash
# 进程由容器编排自动重启（K8s restartPolicy）
# 无需手动恢复，容器 entrypoint 会自动拉起主进程
```

注意事项：
- 必须确认实际进程名（通过 `ps aux` 确认），不可凭服务名猜测
- 单次执行只 kill 一次，不像 ChaosBlade 可在 timeout 窗口内持续 kill
- "反复重启/持续崩溃"意图不要用 `watch`/循环脚本在容器内堆次数——应改用
  路径 B 第 3 步的**持续模式**（节点侧 systemd-run 有界循环），它有定时自停兜底；
  主方案 ChaosBlade `--timeout` 持续 kill 可用时优先用主方案
- 效果与 ChaosBlade 单次 kill 等价，但缺少持续 kill 和自动超时停止的能力

---

**路径 B —— 容器内没有 `kill`（极简镜像）：节点侧 `crictl stop`**

从节点侧直接停掉容器，不需要容器内有任何二进制。

> **语义差异（必须写进演练报告）**：`crictl stop` 停的是**整个容器**，不是容器内某个进程。
> 它绕过 kubelet 直接操作容器运行时，kubelet 事后发现容器已终止并按 restartPolicy 重建，
> 因此 Events 与 exit code 的表现和路径 A 的 `kill -9` **不完全一致**：
> - 路径 A：主进程被杀 → 容器 exit code 137（128+9）→ `Reason: Error`
> - 路径 B：容器被运行时停止 → exit code 由 `-t` 决定（`-t 0` 为 137，优雅停止为 0/143）
>   → 可能显示 `Reason: Completed` 或 `Error`
> 若演练目的是**精确验证 OOM/崩溃告警的 exit code 匹配规则**，优先用路径 A。
>
> **故障模式差异（同样必须写进演练报告）**：
> - **离散重启**（单次或 N 轮手动 stop）：每次重启都由外部操作触发，操作一停重启即停——
>   故障效果随操作结束而消失，不构成持续故障状态
> - **持续模式**（下方 systemd-run 有界循环）：timer 窗口内容器**自主**反复终止重建，
>   不依赖外部持续操作，才是"反复重启"的持续状态
> 用户意图为"反复重启/持续崩溃"时，必须使用持续模式；若只做了离散多轮，
> 报告必须如实写"离散重启 N 次"，不得报"反复重启已达成"。

1. 定位目标节点与容器 ID：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.nodeName}
   # 单容器 Pod 可直接取；多容器 Pod 必须按容器名挑，不要用 [0]
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath={range.status.containerStatuses[*]}{.name}{.containerID}{end}
   ```
   containerID 形如 `containerd://<64位十六进制>`，**去掉 `containerd://` 前缀**后使用。

2. 注入 —— 停掉容器：
   ```bash
   # -t 0 立即 SIGKILL，等价于路径 A 的 kill -9；
   # 省略 -t 或给正值则先 SIGTERM、超时后才 SIGKILL（模拟优雅关闭失败场景）
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host crictl stop -t 0 <containerID去前缀>
   ```
   也可用容器名反查 ID（跨 Pod 同名会命中多个，需核对上一步的 ID 前缀）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'crictl stop -t 0 $(crictl ps -q --name <container-name> | head -1)'
   ```

3. 注入（**持续模式**——"反复重启/持续崩溃"意图必须用本模式）：
   单次 `crictl stop` 只换来一次重启，操作停止后故障即消失。要形成有界的自主重启风暴，
   用 `systemd-run` 在宿主机武装一个**定时自停的 stop 循环**（恢复先于自断，
   与网络隔离类用例一致）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c '
     systemd-run --on-active=<duration>s --unit=blade-stoploop-<pod-name> sh -c "
       pkill -f \"crictl sto\$((0+0))p -t 0\"; pkill -x crictl; true" &&
     sh -c "for i in $(seq 1 <rounds>); do
       CID=$(crictl ps -q --name <container-name> | head -1)
       [ -n \"$CID\" ] && crictl stop -t 0 $CID
       sleep <interval>
     done"
   '
   ```
   参数说明：
   - `<duration>`：故障窗口总时长（秒），timer 到期自动终止循环，**无需 destroy**；
     应 ≥ `<rounds> × <interval>` 并留余量
   - `<interval>`：两轮 stop 的间隔（秒），建议 ≥ 15，给 kubelet 留出重建与退避爬坡空间
   - `<rounds>`：循环轮数上限，是 timer 之外的第二重保险
   - **循环体内必须每轮用 `crictl ps -q --name` 重新解析容器 ID**——kubelet 每轮重建容器后
     ID 会变化，把第 1 轮的 ID 固化进循环，第 2 轮起就会空转（实测教训）
   - timer 载荷里的 `sto\$((0+0))p` 是防自匹配技巧：若直接写 `stop`，pkill -f 会先命中
     timer 自己的 shell 命令行把它杀了，根本轮不到杀循环；用 `$((0+0))` 拆开后语义等价、
     命令行不再含字面 `stop` 模式
   - timer 由宿主机 PID 1 管理，debug pod 删除后循环与自停恢复均不受影响
   - 提前终止见上方"注入恢复"第 4 条

验证：
```bash
# 容器重启次数 +1，且 lastState 显示上次终止原因
kubectl get pod <pod-name> -n <namespace>
kubectl describe pod <pod-name> -n <namespace>
```
`RESTARTS` 应递增；`Last State: Terminated` 的 `Exit Code` 与上面语义差异说明一致。

恢复：
```bash
# 无需手动恢复 —— kubelet 按 restartPolicy 自动重建容器。
# 确认新容器已 Ready：
kubectl get pod <pod-name> -n <namespace> -o wide
```
持续模式的恢复：等 `<duration>` 到期 timer 自动终止循环（或按上方"注入恢复"第 4 条
`systemctl stop <unit-name>` 提前终止），之后 kubelet 完成最后一次重建即稳定。
恢复验证以"RESTARTS 停止增长 + Pod 1/1 Running"为准。

注意事项：
- **不需要也不应该手动重启 Pod** —— kubelet 会自动重建；手动 delete 会掩盖真实的自愈行为
- `restartPolicy: Never` 的 Pod **不会重建**，容器停了就一直停着。注入前先确认：
  `kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.restartPolicy}`
- 停的是整个容器 → **该容器内所有进程都终止**，不是路径 A 的单个进程
- 单次执行只停一次；"反复重启/持续崩溃"意图**必须用上方第 3 步的持续模式**
  （systemd-run 有界循环），不要靠手动反复执行单轮 stop 堆次数——那只是离散重启，
  操作一停故障即消失
- `crictl` 实测在节点上位于 `/usr/bin/crictl`，已连通 containerd；其它运行时（docker/CRI-O）
  的 CLI 不同，先用 `chroot /host crictl version` 确认可用
