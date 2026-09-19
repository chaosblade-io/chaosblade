**用例名称** 应用日志数据积累 导致 Pod_磁盘空间使用率过高

**故障定位**：持续型故障——填充文件存在即故障存活（占用的空间不会自行消失），
贯穿整个故障窗口；填充文件删除（实验销毁/定时器清理）即空间释放、自动恢复。
手段1（ChaosBlade `pod-disk fill`）与手段2（kubectl-native：容器内 fallocate/dd）
是**并列的注入手段**，底层效果等价（blade 内部也是向目标路径写填充文件），
按环境能力选用。**本用例的核心结构性约束：填充落点决定爆炸半径与可执行形态**——
目标目录在独立块设备（PVC/云盘）上才允许"打满到目标百分比"；落在节点根盘
（容器 rootfs overlay、emptyDir，即此形态：无任何独立卷，`/` 与 `/tmp`
同一 overlay，同盘还有 kubelet、容器运行时与同节点全部 Pod）时，打满 = 节点
`DiskPressure` = 驱逐同节点所有 Pod（同节点常含 zookeeper、node-local-dns、
terway、csi-plugin 等系统/生产组件，甚至可能有演练通道自身的载体 Pod）——
此时**只允许有界小增量填充**（设计：8G，使用率 32%→约 39%），"应用写入
失败/ENOSPC"现象声明为**预期阴性**。`duration_seconds` 是必填的故障窗口契约，
未给定时先向用户确认。

**故障现象**：
1. Pod 视角 `df` 使用率上升（独立卷靶：可达目标百分比；无独立卷靶：上升声明的增量）
2. 独立卷打满设计下：应用写入失败（`ENOSPC: No space left on device`）
3. 无独立卷小增量设计下：写入失败**不出现**（预期阴性，见故障定位——出现反而异常）
4. 节点 `DiskPressure` 事件：本用例设计上**不触发**（触发即爆炸半径失控，立即恢复并如实上报）

**资源准备**：
1. 确认目标 Pod 的标签选择器、命名空间，以及**实际容器名**（多容器/临时容器混存时
   `kubectl exec` 必须显式 `-c <容器名>`）
2. **能力探测（决定手段选择）**——确认 ChaosBlade operator 实际健康，**以当次探测为准**：
   ```bash
   blade create k8s pod-disk -h
   kubectl get pods -A --no-headers | grep -i chaosblade-operator
   ```
   - operator Pod Running → 手段1 可用（实验 UID 统一生命周期管理）
   - operator 处于 Init:ImagePullBackOff/CrashLoopBackOff（常见形态：operator
     0/1，CR 无人 reconcile，`blade create` 回执成功也不代表注入生效）→ 手段1
     判死，用手段2，不要在手段1 上空转
3. **填充落点判定（决定爆炸半径与形态，必做，先于一切填充）**：
   ```bash
   # a) 目标目录是否挂独立卷（volumes + volumeMounts 对照）
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.volumes}'
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].volumeMounts}'
   # b) 无独立卷时：记录文件系统基线与驱逐爆炸半径
   kubectl exec <pod-name> -n <namespace> -c <container> -- df -k <目标目录>
   kubectl get node <node-name> -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}'
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name> --no-headers
   ```
   - 目标目录挂独立卷（PVC/云盘 csi）→ 允许打满设计（`--percent`/算量到 95%+），
     填满只影响本 Pod
   - 无独立卷（rootfs 普通目录 / emptyDir，典型形态）→ 填充落**节点根盘**，
     只允许小增量：**填充量必须使 available% 远离 kubelet 驱逐阈值**
     （imagefs/nodefs available < 15%/10% 触发硬驱逐）。定量示例（典型靶）：
     117.7G 总量、80.9G 可用，填 8G 后 available 仍约 62%，使用率 32%→约 39%，
     安全；填到打满（约 63G+）必然触发 DiskPressure——**禁止**
   - 同节点 Pod 清单必须先记录（恢复验证时对照：无驱逐/无重启漂移即爆炸半径未失控）
4. **容器工具探测（决定手段2 路径）**——有 fallocate 或 dd 即可走路径 A：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'command -v fallocate; command -v dd; command -v truncate; echo PROBE_DONE'
   ```
   典型靶：fallocate/dd/truncate 全有 → 路径 A。都没有（distroless/scratch 镜像
   常态）→ 路径 B（节点侧写）
5. 确认目标目录当前用户可写（典型靶 uid=0、`/tmp` 1777，探测 `ls -ld <目标目录>` 即可）

**演练步骤**：
1. 记录注入前基线（恢复验证的定量对照）：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -c <container> -- df -k <目标目录>
   ```
   记录：Pod RESTARTS 基线、`df -k` 的已用 KB/可用 KB/Use%（精确到 KB，
   恢复对照用 Use% 粒度太粗）、节点 Conditions 基线、同节点 Pod 清单（资源准备第 3 条）

**手段1（ChaosBlade）** —— 前提：资源准备第 2 条探测通过（operator 健康）

2. 使用 ChaosBlade 注入磁盘填充：
   ```bash
   blade create k8s pod-disk fill \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --path <目标目录> \
     --percent <percent> \
     --timeout <duration>
   ```
   参数含义：
   - `--path`：填充目标路径（目标目录，不是盲目用 `/`——落点判定见资源准备第 3 条）
   - `--percent`：使用率目标百分比，优先级高于 `--size`；**仅独立卷靶可用**（无独立卷
     靶的高百分比 = 节点根盘打满 = DiskPressure，禁止）
   - `--size`：填充大小（MB），与 `--percent` 二选一；无独立卷靶用它做小增量
   - `--retain-handle`：保留文件句柄（rm 后空间不释放，更真实模拟日志占用；恢复需
     销毁实验或重启 Pod）
3. 记录返回的 experiment_uid，用于后续恢复

**手段2（kubectl-native）** —— 按资源准备第 4 条工具探测结果二选一

**路径 A —— 容器内 fallocate/dd（适用形态）**

前提条件：容器内有 fallocate（快，仅分配元数据）或 dd（慢，真实写入）；
落点判定已完成（资源准备第 3 条），填充量已按爆炸半径上界算出。

填充量计算（**按增量，不是按百分比**）：
- 打满设计（仅独立卷）：填充量 = 文件系统总量 × 目标使用率 − 当前已用量
- 小增量设计（无独立卷）：固定量（如 8G）或「可用量 × 小比例」，硬约束是
  available% 距驱逐阈值有充足余量（见资源准备第 3 条定量示例）

注入命令（**武装定时清理 → 填充 → 自证标记**，严格串行于单个载荷；定时器
先于填充武装——填充后若武装失败，故障将无人回收。定时器 `rm -f` 幂等，到期
自动清理并写还原标记）：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
  '( sleep <duration>; rm -f /tmp/fill_file; echo DISKFILL_RESTORED >> /tmp/diskfill.evd ) >/dev/null 2>&1 & \
   fallocate -l <填充量>G /tmp/fill_file && \
   ls -l /tmp/fill_file >> /tmp/diskfill.evd && \
   echo DISKFILL_INJECTED >> /tmp/diskfill.evd'
```
- `dd` 替代（无 fallocate 时）：`dd if=/dev/zero of=/tmp/fill_file bs=1M count=<MB数>`
  ——注意 dd 是真实写入，GB 级填充耗时明显（wiz 通道注意 exec 超时预算）
- 载荷必须单次下发：拆成两条独立 kubectl exec 时，武装与填充间隔不可控且
  违反紧邻原则；更不能用 `&&` 把两条 kubectl 连进同一个外层 `sh -c`
  （第二条 kubectl 会沦为第一条 exec 载荷的死参数，填充静默丢失）
- 链条是**自证的**：`DISKFILL_INJECTED` + `ls -l` 的文件大小取证（注入时刻写入）、
  `DISKFILL_RESTORED`（定时器还原后写入）落盘证据文件 `/tmp/diskfill.evd`，
  验证阶段 `cat` 取证，不受故障窗口是否已关闭的时序约束。标记不得为缩短
  命令省略（载荷约 250 字节，wiz 通道 sh -c 1024 字节上限内安全）
- **打满设计专属陷阱（仅独立卷靶适用）**：文件系统打满后**任何写操作都会
  ENOSPC，包括写证据标记**。若走打满设计，`DISKFILL_INJECTED` 必须在
  fallocate 之前写入证据文件（顺序：定时器武装 → 写 INJECTED → fallocate
  打满 → fallocate 退出码即成功证据）；`DISKFILL_RESTORED` 由定时器在 rm
  之后写（空间已释放，写入必成功）。小增量设计无此约束

路径A 倒计时从武装时刻起算：武装与填充在同一载荷内严格串行（无侵蚀间隙）；
武装后发生任何修复须先停旧定时器再全额重武装：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
  'pkill -f "sleep.*; rm -f /tmp/fill_fil[e]" 2>/dev/null; true'
```
（模式串末字符加 `[]` 防自杀——载体 sh -c 命令行含模式串时 pkill -f 会把自己
杀掉；容器无 pkill 时旧定时器无法停止，到期会提前清理侵蚀故障窗口——须中止
演练改人工恢复或如实上报缩短的窗口，见 SKILL.md 安全红线「故障窗口完整」）

**注入验证**（两种手段共用；本用例判据全部是只读操作——df/ls/cat，verify 阶段
read-only 纪律天然放行，无探针形态冲突）：
1. **效果主证——使用率增量（对比基线）**：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- df -k <目标目录>
   ```
   已用 KB 增量 ≈ 填充量（8G → +约 8388608 KB），Use% 上升幅度与设计一致
   （小增量：32%→约 39%；打满：到目标百分比）。**容器内 `df` 才是判据**——
   容器视角的使用率就是业务视角的感知
2. 白盒确认填充文件存在且大小正确：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- ls -l /tmp/fill_file
   kubectl exec <pod-name> -n <namespace> -c <container> -- cat /tmp/diskfill.evd
   ```
   `ls` 见填充文件、字节数与填充量一致；证据文件含 `DISKFILL_INJECTED`
   （含 `DISKFILL_RESTORED` 即窗口已关，如实报告实际时长）
3. **爆炸半径未失控交叉确认（无独立卷靶必做）**：
   ```bash
   kubectl get node <node-name> -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}'
   kubectl get pods -A -o wide --field-selector spec.nodeName=<node-name> --no-headers
   ```
   节点 DiskPressure 仍 False；同节点 Pod 与基线清单一致（无 Evicted、无异常重启）
4. **预期阴性声明（小增量设计）**：以下现象**不出现且不作为失败证据**——
   应用写入失败/ENOSPC（磁盘远未满）、Pod 事件出现磁盘异常、Pod 被驱逐。
   出现任一项 = 爆炸半径失控，立即按恢复命令处置并如实上报。
   （写入失败现象仅打满设计下可验证；且注意 verify 阶段 readonly 纪律拒绝
   一切写探针——打满设计须在注入载荷后立即做执行期写入探针，不可留到验证阶段）
5. **持续性检查（必做）**——占用是状态型故障，填充文件存活即故障存活：
   45s 后复查 `df -k` 已用量仍在高位（增量未消失）、`ls -l` 填充文件仍存在

**注入恢复**：
1. 手段1：`blade destroy <experiment_uid>`，填充文件随实验销毁自动清理；
   使用了 `--retain-handle` 且空间未释放时重启目标 Pod
2. 手段2：定时器到期自动 `rm -f` + 写 `DISKFILL_RESTORED`（主恢复路径）；
   提前恢复（幂等：文件不存在时 rm -f 空触发无害）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'pkill -f "sleep.*; rm -f /tmp/fill_fil[e]" 2>/dev/null; \
      rm -f /tmp/fill_file; echo DISKFILL_RESTORED >> /tmp/diskfill.evd; true'
   ```

**恢复验证**：
1. **空间定量回落（主证）**：`df -k <目标目录>` 已用 KB 回落到基线水平
   （±少量日志写入噪声，通常 < 几百 MB），Use% 回到基线值
2. 填充文件不存在：`ls /tmp/fill_file` 报 No such file；证据文件含
   `DISKFILL_RESTORED`（双标记齐备 = 注入与还原全程自证）
3. 节点健康：DiskPressure 仍 False；同节点 Pod 与基线清单一致（对照资源准备
   第 3 条记录，无 Evicted/重启漂移——爆炸半径全程受控的最终证据）
4. Pod 状态 Running、RESTARTS 与基线一致

**基准事实**：
- **根因**：应用日志未清理或数据写入过多，导致 Pod 存储空间被占满
- **必现现象**：容器内 `df` 使用率上升（增量 = 填充量，定量可对账）
- **条件现象**：应用写入失败/ENOSPC——仅独立卷打满设计下出现；无独立卷
  小增量设计下为预期阴性

---

**路径 B —— 容器内没有 fallocate/dd：从节点侧写目标卷**

> 前置：本路径同样必须先完成资源准备第 3 条的落点判定与爆炸半径评估——
> 独立块设备（PVC/云盘）才允许打满；emptyDir/rootfs 同节点根盘只允许小增量。

目标目录在容器里，但它的**真实存储在宿主机上**。从节点侧直接写那个路径，
工具来自 debug 镜像，不需要业务容器内有任何二进制。

1. 判定目标目录的卷类型与宿主机路径：
   ```bash
   # a) 看 Pod 声明的卷类型
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.volumes}
   # b) 看目标目录对应哪个卷
   kubectl get pod <pod-name> -n <namespace> \
     -o jsonpath={.spec.containers[*].volumeMounts}
   # c) 取 Pod UID（宿主机路径以它为目录名，注意此处【不做】下划线化）
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.metadata.uid}
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.nodeName}
   ```

2. 在节点上定位并确认设备归属。**宿主机路径规律（前提是 kubelet root-dir
   为默认值）**：
   ```
   PVC/云盘 : <kubelet-root-dir>/pods/<PodUID>/volumes/kubernetes.io~csi/<volumeHandle>/mount
   emptyDir : <kubelet-root-dir>/pods/<PodUID>/volumes/kubernetes.io~empty-dir/<卷名>
   ```
   ⚠️ **kubelet root-dir 不一定是 `/var/lib/kubelet`（确证）**：有的集群 kubelet 带
   `--root-dir=/home/t4/kubernetes/lib/kubelet` 之类的自定义参数，按默认路径拼会
   `No such file or directory`。先从 kubelet 进程命令行推导真实 root-dir：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'tr "\0" " " < /proc/$(pidof kubelet)/cmdline | grep -o "root-dir=[^ ]*" || echo /var/lib/kubelet'
   ```
   下文的 `/var/lib/kubelet` 均按探测结果替换。
   ```bash
   # 列出该 Pod 的全部卷，并确认各自所在设备 —— 只有独立设备才允许打满
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c 'for d in /var/lib/kubelet/pods/<PodUID>/volumes/*/*/; do echo "$d"; df -h "$d" | tail -1; done'
   ```
   **判据**：目标卷的 `Filesystem` 列**不是**节点根设备（不是 `/` 那一行）才是
   打满形态的适用场景；若与 `/` 同设备，按小增量设计执行（填充量约束同路径 A）。
   也可用 `crictl inspect <containerID>` 读 `hostPath`↔`containerPath` 映射交叉核对。

3. 注入 —— 往确认过的宿主机路径写填充文件（**先武装定时删除再填充**，定时器由
   宿主机 systemd 管理，不受 debug pod 生命周期影响）：
   ```bash
   kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
     -- chroot /host sh -c '
       systemd-run --on-active=<recovery-seconds>s --unit=blade-rmfill-<PodUID前8位> \
         "sh -c \"rm -f <步骤2确认的路径>/fill_file; echo DISKFILL_RESTORED >> <步骤2确认的路径>/diskfill.evd\"" &&
       fallocate -l <按路径A同式算出的填充量>G <步骤2确认的路径>/fill_file &&
       ls -l <步骤2确认的路径>/fill_file >> <步骤2确认的路径>/diskfill.evd &&
       echo DISKFILL_INJECTED >> <步骤2确认的路径>/diskfill.evd
     '
   ```
   - `fallocate` 不可用时改 `dd if=/dev/zero of=<路径>/fill_file bs=1M count=<MB>`
   - 文件名沿用 `fill_file`、证据文件 `diskfill.evd`，与路径 A 一致，便于统一清理；
     证据文件写在同一宿主机路径（容器内目标目录可见，`cat` 取证同路径 A）
   - 打满设计时 INJECTED 标记须在 fallocate 之前写入（同路径 A 陷阱说明）

验证（从容器内看使用率上升——这才是业务视角的效果判据；容器内 `df`/`cat`
判据与路径 A「注入验证」完全一致，含爆炸半径交叉确认与预期阴性声明）：
```bash
# 容器内视角：目标目录所在文件系统使用率 + 证据文件
kubectl exec <pod-name> -n <namespace> -c <container> -- df -k <目标目录>
kubectl exec <pod-name> -n <namespace> -c <container> -- cat <目标目录>/diskfill.evd
# 节点侧确认文件已生成
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host ls -lh <步骤2确认的路径>/fill_file
```

恢复：
```bash
# 提前恢复（定时器为主路径，此为兜底；rm -f 幂等）
kubectl debug node/<node-name> --image=<verified-cluster-image> --profile=sysadmin --quiet \
  -- chroot /host sh -c 'rm -f <步骤2确认的路径>/fill_file'
# systemd 定时器撤销（提前恢复时顺手清理）
systemctl stop blade-rmfill-<PodUID前8位>.timer 2>/dev/null; true
```

注意事项：
- **写宿主机路径等于写进容器**——同一份存储的两个视角，容器内立刻可见
- 目标路径必须是**步骤 2 现场确认过的**，不要凭规律直接拼——`volumeHandle` 目录名
  （如 `d-hn3g3bxq9181lyh1roo4`）无法从 Pod spec 推导，只能实地 `ls`；同理 kubelet
  root-dir 也必须先探测（见步骤 2 警示），有集群用 `/home/t4/kubernetes/lib/kubelet`
- Pod UID 在此处**保持原样带 `-`**（`/var/lib/kubelet/pods/001f9dc7-7c42-...`），
  与 cgroup 路径要下划线化的规则相反，不要混用
- Pod 重建后 `<PodUID>` 目录会更换，旧目录由 kubelet 回收；若注入后 Pod 已重建，
  填充文件随旧目录一起消失，此时**不要再执行 rm**（路径已不存在）
- systemd-run 定时器兜底自恢复；定时器单元名带 PodUID 前缀防跨演练串扰
