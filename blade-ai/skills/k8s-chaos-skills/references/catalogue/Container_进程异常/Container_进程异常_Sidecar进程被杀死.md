**用例名称** Sidecar进程被杀死 导致 Container_进程异常

**故障现象**：
1. Sidecar 容器内主进程被杀死，该容器因主进程退出而重启
2. Pod 内其他容器（主容器）不受影响，Pod 本身不重启
3. Sidecar 提供的能力暂时中断（如日志停止采集、代理不可用、监控数据丢失）
4. Pod Events 中显示特定容器重启记录，Pod 整体状态仍为 Running

**资源准备**：
1. 确认目标 Pod 包含多个容器，明确 Sidecar 容器名称
2. 确认 Sidecar 容器内实际进程名（必须通过 ps 确认，不可猜测）
3. 确认目标 Pod 所在 namespace 和 labels

**演练步骤**：
1. 确认 Pod 内容器列表，获取 Sidecar 容器名称：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].name}' --kubeconfig <kubeconfig-path>
   ```
2. 进入 Sidecar 容器确认实际进程名：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <sidecar-container-name> -- ps aux --kubeconfig <kubeconfig-path>
   ```
3. 记录注入前 Pod 状态和各容器 RestartCount：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses[*].restartCount}' --kubeconfig <kubeconfig-path>
   ```
4. 使用 ChaosBlade 对 Sidecar 容器注入进程杀死故障：
   ```bash
   blade create k8s container-process kill \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --container-names <sidecar-container-name> \
     --process <进程名> \
     --signal 15 \
     --timeout 600 \
     --kubeconfig <kubeconfig-path>
   ```
5. 观察 Sidecar 容器重启行为及主容器是否受影响

**注入验证**：
1. 执行 `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.containerStatuses}'`，确认目标 Sidecar 容器 restartCount 增加
2. 确认主容器仍正常运行，restartCount 未变化
3. 执行 `kubectl describe pod <pod-name> -n <namespace>`，确认 Events 中有目标容器重启记录
4. 验证 Sidecar 提供的服务在重启期间中断（如代理端口不可达、日志缺失）
5. **持续性检查（"反复重启"意图必须做）**：注入动作结束后**停止一切操作、静观 1-2 分钟**，
   再次查看各容器 restartCount，确认 sidecar 容器的计数在无外部干预下仍在递增、
   且主容器计数始终不变。若 sidecar 计数不再增长，说明达成的是**离散重启**，
   验证结论必须如实写"离散重启 N 次"，不得报"反复重启已达成"

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <实验UID>
   ```
2. 或等待 `--timeout` 到期后 ChaosBlade 自动停止杀进程
3. 说明：进程被杀后容器 entrypoint 会自动拉起进程，ChaosBlade timeout 控制"持续杀进程"的时长
4. 路径 B 持续模式（见降级方案）：等待 `systemd-run` timer 到期后循环自动终止；
   如需提前终止，经 debug pod 执行：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host systemctl stop <unit-name>
   ```

**恢复验证**：
1. 执行 `kubectl get pod <pod-name> -n <namespace>`，确认 Pod 状态为 Running 且 Sidecar 容器 restartCount 不再增长
2. 确认 Sidecar 容器内进程稳定运行（PID 不再变化）
3. 确认 Sidecar 提供的服务恢复正常

**基准事实**：
- **根因**：Sidecar 容器内主进程被外部信号（SIGTERM）杀死，容器退出后被 kubelet 重启，但 Pod 内其他容器不受影响
- **必现现象**：目标 Sidecar 容器 restartCount 增长；容器 Last State 为 terminated（Exit Code 非 0）；主容器 restartCount 不变且持续运行；Pod 整体状态保持 Running

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。
> 两条路径按 sidecar 容器内是否有 `kill`/`pgrep` 二选一。

---

**路径 A —— sidecar 容器内有 `kill` 和 `pgrep`**

前提条件：目标容器内需有 `kill` 命令和 `pgrep` 工具可用。先确认：
```bash
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> \
  -- sh -c 'command -v kill; command -v pgrep'
```
两者都有输出才走本路径。sidecar 常是 distroless 极简镜像（istio-proxy、各类
driver-registrar 等），缺失时走路径 B。

注入命令：
```bash
# 对指定容器内的目标进程发送信号杀死
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- sh -c 'kill -15 $(pgrep -f <process-name>)'
# 常用信号：15 (SIGTERM, 优雅终止) 或 9 (SIGKILL, 强制杀死)
# 分步执行：
# 1. 获取进程 PID
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- pgrep -f <process-name>
# 2. 发送信号
kubectl exec <pod-name> -c <sidecar-container-name> -n <namespace> -- kill -15 <pid>
```

恢复命令：
```bash
# 容器编排自动重启：进程被杀后，容器 entrypoint 会自动重启进程
# 无需手动恢复，但如需停止“持续杀进程”的行为，停止执行 kill 命令即可
```

注意事项：
- 必须通过 `ps aux` 或 `pgrep` 确认实际进程名，不可猜测
- "反复重启/持续崩溃"意图不要用 `watch`/循环脚本在容器内堆次数——应改用
  路径 B 第 3 步的**持续模式**（节点侧 systemd-run 有界循环），它有定时自停兜底；
  主方案 ChaosBlade `--timeout` 持续杀进程可用时优先用主方案
- 与 ChaosBlade 相比，kubectl exec 单次 kill 缺少持续杀进程和自动超时停止的能力

---

**路径 B —— sidecar 容器内没有 `kill`：节点侧 `crictl stop`**

从节点侧停掉**该 sidecar 容器**，不需要容器内有任何二进制。同 Pod 内其它容器
（含主容器）**不受影响** —— kubelet 只重建被停的那一个容器。

> **语义差异（必须写进演练报告）**：`crictl stop` 停的是**整个 sidecar 容器**，
> 不是容器内某个进程。它绕过 kubelet 直接操作运行时，kubelet 事后发现容器终止并重建：
> - 路径 A：sidecar 内进程被杀 → 该容器 exit code 137/143
> - 路径 B：容器被运行时停止 → exit code 由 `-t` 决定（`-t 0` 为 137，优雅停止为 0/143）
> 若演练目的是精确验证 exit code 匹配规则，优先用路径 A。
>
> **故障模式差异（同样必须写进演练报告）**：
> - **离散重启**（单次或 N 轮手动 stop）：每次重启都由外部操作触发，操作一停重启即停——
>   故障效果随操作结束而消失，不构成持续故障状态
> - **持续模式**（下方 systemd-run 有界循环）：timer 窗口内 sidecar 容器**自主**反复终止重建，
>   不依赖外部持续操作，才是"反复重启"的持续状态
> 用户意图为"反复重启/持续崩溃"时，必须使用持续模式；若只做了离散多轮，
> 报告必须如实写"离散重启 N 次"，不得报"反复重启已达成"。

1. 定位目标节点，并**按容器名精确取** sidecar 的 containerID
   （多容器 Pod 用 `containerStatuses[0]` 会取错容器）：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.nodeName}
   # 列出全部容器名与 ID，从中挑 sidecar 那一条
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath={range.status.containerStatuses[*]}{.name}{.containerID}{end}
   ```
   containerID 形如 `containerd://<64位十六进制>`，**去掉 `containerd://` 前缀**后使用。

2. 注入 —— 停掉该 sidecar 容器：
   ```bash
   # -t 0 立即 SIGKILL；省略 -t 或给正值则先 SIGTERM、超时后 SIGKILL
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host crictl stop -t 0 <containerID去前缀>
   ```
   用容器名反查时**必须限定到目标 Pod** —— 集群里同名 sidecar（如 `istio-proxy`）
   遍布多个 Pod，`--name` 会命中一片：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'crictl stop -t 0 $(crictl ps --pod <podSandboxId> --name <sidecar-container-name> -q | head -1)'
   ```
   `<podSandboxId>` 由 `crictl ps` 输出的 `POD ID` 列给出（按 Pod 名匹配那一行）。

3. 注入（**持续模式**——"反复重启/持续崩溃"意图必须用本模式）：
   单次 `crictl stop` 只换来一次重启，操作停止后故障即消失。要形成有界的自主重启风暴，
   用 `systemd-run` 在宿主机武装一个**定时自停的 stop 循环**（恢复先于自断，
   与网络隔离类用例一致）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c '
     systemd-run --on-active=<duration>s --unit=blade-stoploop-sidecar-<pod-name> sh -c "
       pkill -f \"crictl sto\$((0+0))p -t 0\"; pkill -x crictl; true" &&
     sh -c "for i in $(seq 1 <rounds>); do
       SID=$(crictl pods --namespace <namespace> -q | head -1)
       CID=$(crictl ps --pod \$SID --name <sidecar-container-name> -q | head -1)
       [ -n \"$CID\" ] && crictl stop -t 0 $CID
       sleep <interval>
     done"
   '
   ```
   参数与要点：
   - `<duration>`：故障窗口总时长（秒），timer 到期自动终止循环，**无需 destroy**；
     应 ≥ `<rounds> × <interval>` 并留余量
   - `<interval>`：两轮 stop 的间隔（秒），建议 ≥ 15，给 kubelet 留出重建与退避爬坡空间
   - `<rounds>`：循环轮数上限，是 timer 之外的第二重保险
   - **每轮必须重新解析容器 ID**——kubelet 每轮只重建被停的 sidecar 容器，其 ID 每轮都变；
     固化第 1 轮 ID 会导致第 2 轮起空转（实测教训）
   - **必须用 `--pod <podSandboxId>` 把解析限定到目标 Pod**——集群里同名 sidecar
     （如 `istio-proxy`）遍布多个 Pod，不限定会停错 Pod 的 sidecar；
     sandbox 本身不随容器重启而变化，每轮用 `crictl pods --namespace` 重新解析即可
     （多 Pod 同 namespace 时先按 Pod 名核对出唯一 sandbox）
   - timer 载荷里的 `sto\$((0+0))p` 是防自匹配技巧：若直接写 `stop`，pkill -f 会先命中
     timer 自己的 shell 命令行把它杀了；用 `$((0+0))` 拆开后语义等价、命令行不再含字面模式
   - timer 由宿主机 PID 1 管理，debug pod 删除后循环与自停恢复均不受影响
   - 提前终止见上方"注入恢复"第 4 条

验证：
```bash
# 该 sidecar 容器的 RESTARTS 递增，其它容器不变 —— 这是本用例的关键判据
kubectl get pod <pod-name> -n <namespace> \
  -o jsonpath={range.status.containerStatuses[*]}{.name}{.restartCount}{end}
kubectl describe pod <pod-name> -n <namespace>
```
只有 sidecar 那一项 `restartCount` 增加。再确认**主容器未受影响**：
```bash
kubectl exec <pod-name> -c <主容器名> -n <namespace> -- echo alive
```
应正常返回 `alive`。

恢复：
```bash
# 无需手动恢复 —— kubelet 自动重建该容器。确认全部容器 Ready：
kubectl get pod <pod-name> -n <namespace> -o wide
```
持续模式的恢复：等 `<duration>` 到期 timer 自动终止循环（或按上方"注入恢复"第 4 条
`systemctl stop <unit-name>` 提前终止），之后 kubelet 完成最后一次重建即稳定。
恢复验证以"sidecar 容器 restartCount 停止增长 + 全部容器 Ready + 主容器未受影响"为准。

注意事项：
- **不要手动 delete Pod** —— kubelet 会单独重建那个容器；delete 整个 Pod 会掩盖
  「sidecar 单独重启、主容器存活」这一关键现象
- `restartPolicy: Never` 的 Pod 容器不会重建，注入前先确认：
  `kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.restartPolicy}`
- 停的是整个 sidecar 容器 → 其内所有进程终止，不是路径 A 的单个进程
- 单次执行只停一次；"反复重启/持续崩溃"意图**必须用上方第 3 步的持续模式**
  （systemd-run 有界循环），不要靠手动反复执行单轮 stop 堆次数——那只是离散重启，
  操作一停故障即消失
- **istio-proxy 类 sidecar 被停会中断整个 Pod 的出入流量**（它是流量代理），
  影响面等同于主容器不可用 —— 爆炸半径评估时要按此计
- `crictl` 实测位于节点 `/usr/bin/crictl`，已连通 containerd；其它运行时的 CLI 不同，
  先用 `chroot /host crictl version` 确认
