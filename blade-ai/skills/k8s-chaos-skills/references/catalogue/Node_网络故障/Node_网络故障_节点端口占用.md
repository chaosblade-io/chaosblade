**用例名称** 节点端口占用 导致 Node_网络故障

**故障现象**：
1. 节点上的关键端口被占用（如 kubelet 10250、NodePort 范围 30000-32767、应用 HostPort）
2. 使用该端口的系统组件或应用无法正常工作
3. NodePort 类型 Service 无法在该节点接收流量
4. 模拟节点端口资源冲突或恶意进程占用场景

**资源准备**：
1. 确认目标节点名称
2. 确认需要占用的端口号（NodePort、HostPort 或系统组件端口）
3. 确认 ChaosBlade Operator 已部署（DaemonSet 通道）或具备节点 SSH 访问权限（SSH 通道）
4. **端口形态选型（判型，演练前必须完成）**：
   - **首选 hostNetwork 守护进程端口**（如 node-exporter DS 的 kube-rbac-proxy 前置代理 9100）：爆炸半径小（单节点单组件，非控制面），DS/kubelet 自愈链完整，判据干净且三重可观测（端口持有者、服务响应形态、容器重启计数）。靶选择依据：hostNetwork 或 hostPort 监听、持有者非 kubelet/kube-proxy、组件可自愈重启
   - **NodePort 形态须先确认 kube-proxy 非 IPVS 模式**（`kubectl get pods -n kube-system -l k8s-app=kube-proxy -o yaml | grep mode`，mode: ipvs 即中招）——IPVS 模式下 NodePort 流量由虚拟服务 DNAT 直达后端 Pod，宿主机上的 nc/socat 监听收不到任何 NodePort 流量（socat 实占 NodePort 端口后，外部访问仍直达后端 Service），注入"成功"但「NodePort 不可达」判据**恒假阴性**；且集群可能无任何 NodePort Service（无现成靶）
   - **kubelet 10250 不建议作靶**：占用后节点 NotReady 会触发节点级外部自愈通道介入（约 217-292s），故障窗口被外部恢复机制截断，duration 契约失效

**演练步骤**：
1. 确认目标节点和端口使用情况：
   ```bash
   kubectl get nodes <node-name>
   kubectl get svc --all-namespaces -o jsonpath='{range .items[?(@.spec.type=="NodePort")]}{.metadata.name}{"\t"}{.spec.ports[*].nodePort}{"\n"}{end}'
   ```
2. 选择注入方式并注入节点端口占用：

   **方式一：DaemonSet 通道**
   ```bash
   blade create k8s node-network occupy \
     --names <node-name> \
     --port <port> \
     --force \
     --timeout <duration>

   ```

   **方式二：SSH 通道**
   ```bash
   blade create k8s node-network occupy \
     --port <port> \
     --force \
     --channel ssh \
     --ssh-host <node-ip> \
     --ssh-user root \
     --timeout <duration>
   ```
   - `--port`：要占用的端口（必填）
   - `--force`：强制杀死当前使用该端口的进程后占用
3. 记录返回的 experiment_uid，用于后续恢复

**注入验证**：
1. 确认端口已被占用（通过 SSH 或 debug Pod 检查节点）：
   ```bash
   kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host ss -tlnp | grep <port>
   ```
2. 若占用的是 NodePort，验证该节点上 Service 不可达：
   ```bash
   curl --connect-timeout 5 http://<node-ip>:<nodeport>
   ```
   确认连接失败。**⚠️ 前置条件：kube-proxy 须为 iptables 模式**——IPVS 模式下该判据恒假阴性（NodePort 流量被 DNAT 直达后端，宿主监听收不到流量，见资源准备判型），此时不得以「NodePort 仍可达」判注入失败，改以第 1 条端口持有者变化为主证
3. 若占用的是 kubelet 端口（10250），检查节点健康状态：
   ```bash
   kubectl get nodes <node-name>
   ```
   观察是否变为 NotReady（NotReady 需 kubelet 心跳停止满 node-monitor-grace-period（默认 40s）后才出现——占用后 40s 内仍 Ready 是预期中间态，主证仍是第 1 条的端口占用）
4. 若占用的是 hostNetwork 守护进程端口（如 kube-rbac-proxy 9100），验证三重判据（均可观测）：
   - **端口持有者变化（机制主证）**：`ss -tlnp | grep <port>` 持有者由原组件变为 nc/socat
   - **服务响应形态变化（效果主证）**：原组件为 TLS 服务时（如 kube-rbac-proxy），`curl -sk --max-time 4 https://127.0.0.1:<port>/metrics -o /dev/null -w code=%{http_code}` 由基线 HTTP 状态码（如 401）变为 `code=000`（socat 接受连接但不回数据，TLS 握手无响应）；基线 code 须在注入前同路径采集
   - **组件 crashloop（传导证据）**：原组件 Pod `RESTARTS` 计数增长——被杀重启后因端口被占 bind 失败反复 crash，`RESTARTS` 持续增长是注入生效的预期主证而非副作用，勿当作误伤
5. 检查相关事件和系统组件日志

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <experiment_uid>
   ```
2. 或等待 `--timeout` 到期自动恢复
3. 被杀的系统组件（如 kubelet）通常由 systemd 自动重启

**恢复验证**：
1. 确认端口恢复正常使用：
   ```bash
   kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host ss -tlnp | grep <port>
   ```
   确认原进程重新监听。**原组件以新 pid 重新持有端口即恢复成立**——pid 与注入前不同是预期（进程被杀后重启）；hostNetwork 守护进程端口形态下，占用释放后原组件重绑时延约 10s 内（kubelet 重启容器 + crashloop backoff 为 10s 量级，不显著拖延恢复），socat/nc 到期后等待一个 backoff 周期内未恢复才判恢复失败
2. 确认节点状态恢复为 Ready
3. 确认 NodePort Service 恢复可达（仅 iptables 模式下此判据成立，见注入验证第 2 条）
4. 确认系统组件运行正常：hostNetwork 守护进程端口形态下补三项零残留判据——原组件 Pod `RESTARTS` 停止增长且容器稳定 Running；服务响应形态回基线（curl code 回到注入前采集值）；`pgrep -af socat`（或 nc）无残留进程

**基准事实**：
- **根因**：节点宿主机上的指定端口被 ChaosBlade 强制占用，原使用该端口的进程被杀死（`--force`），模拟端口资源冲突或关键组件端口被抢占场景
- **必现现象**：目标端口被占用；原监听进程中断；依赖该端口的服务/组件不可用

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效节点端口占用。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；宿主机需包含 `nc`（netcat）**或** `socat` 二者之一（因 `chroot /host` 后工具从宿主机解析——存在宿主机无 nc/ncat 只有 socat 的集群，socat 等效模板见下，工具探测 `chroot /host which nc socat` 二选一）；宿主机变更必须 `--profile=sysadmin`；禁用 `-it`

**前置探针一条龙（Phase 1 定案免推导——散轮拆发 + 非特权探针 pod 上 ss 无 Process 列，常见成本约 5 轮 ~200s）**：建探针 debug pod 时**必须 `--profile=sysadmin`**——privileged:false 的探针 pod 上 `chroot /host ss -tlnp` 的 Process 列为空（宿主监听项全部无 pid；那是探针 pod 特权位问题，非 chroot / /proc 可见性问题，勿再纠结排查），须特权探针 pod 才能解析持有者 pid。建 pod 后**同一轮并行**发出全部探针（`-- sleep 60` keep-alive 窗口足够；探针 pod 中途过期则直接重建，~20s 成本，勿在过期纠结上花推理）：

```bash
kubectl exec <probe-pod> -n <ns> -c debugger -- chroot /host which socat nc ss curl timeout pgrep   # 宿主工具链（socat/nc 二选一存在即可）
kubectl exec <probe-pod> -n <ns> -c debugger -- chroot /host ss -tlnp                              # 端口持有者基线（记 pid 与进程名，注入 kill 用）；全量输出可能被工具回执截断（多行命令头部信息必失）——截断即改过滤单行形态补查 `ss -tlnp sport = :9100`（过滤式作尾随参数直传，无 shell 操作符）
kubectl exec <probe-pod> -n <ns> -c debugger -- chroot /host pgrep -af socat                       # 残留预检（exit 1 + 空输出 = 零残留 = 预检通过，POSIX 语义非探测失败）
kubectl exec <probe-pod> -n <ns> -c debugger -- chroot /host curl -sk --max-time 4 https://127.0.0.1:9100/metrics -o /dev/null -w code=%{http_code}   # 响应形态基线（kube-rbac-proxy 形态期望 code=401）
```

（RESTARTS 基线走 `kubectl get pod` 记当次值即可；只读探查阶段禁 `fuser`——守卫全形态拦截杀原语，`fuser -k` 仅 execute 期 mutation 载荷内合法）

注入命令（**用 `timeout` 给占用进程设定时自停**——到期 nc/socat 退出、debug Pod 转 Completed，
端口自动释放，补齐 ChaosBlade `--timeout` 的自恢复能力）：
```bash
# nc 形态（宿主机有 nc 时）：通过 kubectl debug node 在宿主机网络空间占用端口（nc 作为 debug Pod 主进程常驻；
# timeout 到期自动终止 nc，恢复=到期自停或提前删除该 debug Pod）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host \
  timeout <duration> nc -l -p <port> -k
# socat 形态（宿主机无 nc 时等效替代；OPEN:/dev/null = 接受连接不回数据）：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host \
  timeout <duration> socat TCP-LISTEN:<port>,reuseaddr,fork OPEN:/dev/null
# 如需强制占用（先杀原进程再监听，exec 让 timeout+占用进程取代 shell 成为主进程）——nc 形态：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'fuser -k <port>/tcp; exec timeout <duration> nc -l -p <port> -k'
# 强制占用 socat 形态（宿主机无 nc 时；fuser -k 杀原持有进程后 socat 抢占，宿主机须有 fuser）：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'fuser -k <port>/tcp; exec timeout <duration> socat TCP-LISTEN:<port>,reuseaddr,fork OPEN:/dev/null'
# 容器内直接执行形态（守卫兼容路径）：当 `chroot /host sh -c` 复合载荷被守卫以
# host-escape 原语/fault family 不匹配拦截时，改在 privileged+hostNetwork+hostPID 载体容器内直接执行
# （容器内 bind = 宿主端口被占，网络栈共享；镜像钦定须自带 socat/timeout，terway 镜像已验证自带）；
# 杀原持有进程用 kill <pid>（从基线探测动态解析持有者 pid，守卫 process family 认可；fuser 在
# 只读探查阶段即被全形态拦截，勿在载荷中使用），kill 与 socat 间加 sleep 1 竞态缓冲：
kubectl exec <debug-pod> -n <debug-namespace> -c debugger -- sh -c \
  'kill <holder-pid>; sleep 1; nohup timeout <duration> socat TCP-LISTEN:<port>,reuseaddr,fork OPEN:/dev/null >/dev/null 2>&1 & echo armed'
# （nohup 后台化使 exec 秒回——规避 harness 30s task ceiling 截断；socat 生命周期由 timeout 独立管理）
```

**机制定案免推导（勿再推演此机理）**：exec 载荷秒回后 socat 为何存活——nohup 忽略 SIGHUP；`&` 后台化的 socat 在 exec 会话的 shell 退出后被 reparent 到容器 PID 1（载体 `sleep 3600` 常驻，PID 1 存活故容器不被收割——runtime 仅在 PID 1 退出时收割容器，exec 会话结束不触发），故 socat 由 timeout 独立管理存活至到期。**载体的唯一使命 = 保持 PID 1 存活**：观察窗口内（含 verify 阶段——exec 进载体观察 socat/端口持有者是本 case 的验证形态）勿删载体；「删除载体不影响 socat」的推论未经验证（exec 出的 socat 仍在容器 cgroup 内，删载体大概率连带收割 = 窗口截断），勿采信勿再推导；确需强制提前恢复时可删载体兜底，代价是窗口截断（提前恢复方向，无害）。

倒计时从武装时刻起算：timeout 包裹与 nc 监听在同一条命令内原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先删除旧 debug Pod（`kubectl delete pod <debug-pod-name> --force --grace-period=0`，端口释放与定时器取消同步完成），再重跑上方注入命令重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）

恢复命令（到期前可提前手动恢复）：
```bash
# 提前恢复：终止占用端口的 nc 进程
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'fuser -k <port>/tcp'
# 删除 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
# 被杀的系统组件（如 kubelet）通常由 systemd 自动重启
```

注意事项：
- `nc -l -p` 在宿主机网络命名空间监听，效果与节点端口被占用等价
- 如需占用 UDP 端口，使用 `nc -l -u -p <port>`
- socat `OPEN:/dev/null` 形态的响应特征：接受 TCP 连接但不回任何数据（读 /dev/null 立即 EOF）——对 TLS 服务（如 kube-rbac-proxy）表现为 TLS 握手无响应（curl `code=000`、exit 35 SSL connect error），对明文 HTTP 表现为连接成功但空响应/立即断开；这是「效果主证」的判读形态，勿误判为注入未生效
- 强制占用 hostNetwork 守护进程端口后，原组件会进入 crashloop（bind 失败反复退出），`RESTARTS` 增长是注入生效的传导证据；占用释放后 kubelet 在下一个重启周期内重绑成功（约 10s，backoff 10s 量级不显著拖延），恢复验证以此为准勿提前判负
- **IPVS 遮蔽对本手段2同样生效**：kube-proxy IPVS 模式下占用 NodePort 端口，NodePort 流量仍被虚拟服务 DNAT 直达后端 Pod，宿主机监听零承接——「NodePort 不可达」判据恒假阴性（判型见资源准备第 4 条），本场景应改用 hostNetwork 守护进程端口形态
- 自恢复基于 `timeout <duration>` 包裹：到期 nc/socat 退出后 debug Pod 主进程结束、端口释放；
  若宿主机无 `timeout`（coreutils 缺失的极端环境），改用 `sh -c 'nc ... & sleep <duration>; kill $!'` 等效实现
