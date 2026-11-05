**用例名称** 小文件堆积耗尽 inode 导致 Pod_磁盘空间使用率过高（inode 维度）

## 故障定位
持续型故障——堆积的小文件存在即故障存活（占用的 inode 不会自行消失），
贯穿整个故障窗口；小文件删除（定时器清理/提前恢复）即 inode 释放、自动恢复。

**注入手段**：容器内创建海量小文件（kubectl-native exec）。ChaosBlade 无 inode
维度的原生动作，本用例不用 blade。

**核心结构性约束：inode 表是按文件系统计的，落点决定爆炸半径**——
- 目标目录在独立块设备（PVC/云盘 csi）上：该卷有**自己独立的 inode 表**，
  打满只影响本 Pod 的这个卷，允许"填到 IUse%≈100%"设计。
- 目标目录落在节点根盘（容器 rootfs overlay、emptyDir，即无独立卷形态）：
  它与 kubelet、容器运行时、同节点全部 Pod **共享同一张节点 inode 表**。耗尽它
  = 节点上任何进程都无法再创建文件（写日志、建临时文件、容器启动全失败）。
  **且内核/kubelet 没有 "InodePressure" 这类专用节点 Condition**（不同于空间
  维度的 `DiskPressure`）——inode 耗尽没有任何节点级告警信号，是**静默**的
  灾难。因此无独立卷靶**只允许有界小增量**（声明的固定文件数，远不足以耗尽
  节点 inode 表），"应用建文件失败/ENOSPC"现象声明为**预期阴性**。

**只有 path A（容器内 exec）一条路径**：释放 inode 必须 `rm`/`unlink` 真正删除
文件（`truncate -s 0` 只清数据块、保留 inode，对 inode 维度无效）；而 `rm` 是
宿主面刻意封禁词，节点侧（`chroot /host`）的定时清理载荷无法经守卫投递——
path B 的 timer 自恢复**结构性不可能**（非成本考量，见「注意事项」）。故本用例
只在容器内注入与恢复，靶优先选独立卷。

`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. Pod 视角 `df -i` 的 IUse% 上升（独立卷靶：可达 ≈100%；无独立卷靶：上升声明的增量）
2. **inode 耗尽的判别特征**：文件创建失败并返回 `No space left on device`（ENOSPC），
   但**同目录 `df`（空间维度）仍显示有剩余空间**——"有空间却建不出文件"正是
   inode 耗尽区别于空间耗尽的签名（独立卷打满设计下可验证）
3. 无独立卷小增量设计下：建文件失败**不出现**（预期阴性，见故障定位——出现反而
   说明增量过大逼近节点 inode 表，立即恢复并如实上报）
4. 节点侧无专用 Condition：inode 耗尽**不产生** `InodePressure`（不存在此条件），
   也不会翻 `DiskPressure`（空间没满）——不能靠节点 Condition 兜底，收敛只能靠
   靶选择（独立卷）与有界增量

## 资源准备
1. 确认目标 Pod 的标签选择器、命名空间，以及**实际容器名**（多容器/临时容器混存时
   `kubectl exec` 必须显式 `-c <容器名>`）
2. **填充落点判定（决定爆炸半径与形态，必做，先于一切创建）**：
   ```bash
   # a) 目标目录是否挂独立卷（volumes + volumeMounts 对照）
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.volumes}'
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[*].volumeMounts}'
   # b) inode 基线与剩余量（IFree 决定可创建文件数上界）
   kubectl exec <pod-name> -n <namespace> -c <container> -- df -i <目标目录>
   ```
   - 目标目录挂独立卷（PVC/云盘 csi）→ 允许打满设计（`<N>` 取该卷 IFree，
     循环创建到 `touch` 因 ENOSPC 失败为止），耗尽只影响本 Pod 的卷
   - 无独立卷（rootfs 普通目录 / emptyDir，典型形态）→ 落**节点 inode 表**，
     只允许有界小增量：`<N>` 取一个远小于节点 IFree 的固定值（如 20000），
     使节点 IUse% 只上升可忽略的一档，绝不逼近耗尽（无 InodePressure 告警，
     逼近即静默灾难，禁止打满设计）
3. **容器工具探测（决定 path A 可行性）**——需要 shell + `touch`/`mkdir`：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'command -v touch; command -v mkdir; command -v df; echo PROBE_DONE'
   ```
   典型靶：`sh`/`touch`/`mkdir`/`df` 全有 → path A 可行。若为 distroless/scratch
   镜像（无 shell）→ 本用例**无 path B 兜底**（见故障定位：节点侧恢复结构性不可能），
   须换有 shell 的靶 Pod 或如实上报该靶不可演练
4. 确认目标目录当前用户可写（典型靶 uid=0、`/tmp` 1777，探测 `ls -ld <目标目录>`）
5. 记录同节点 Pod 逐 Pod restartCount 基线（无独立卷靶恢复验证时对照——判据是
   逐 Pod 对照自身基线的 delta、非绝对态，详见 SKILL.md「爆炸半径验证判据」）：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath={.spec.nodeName}
   # 每 Pod 一行紧凑 jsonpath（-o wide 在数十 Pod 节点截断丢 RESTARTS/状态，且其
   # RESTARTS 列聚合 initContainer 重启、与主容器 restartCount 不一致）
   kubectl get pods -A --field-selector spec.nodeName=<node-name> -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}:{.status.phase}:{.status.containerStatuses[0].restartCount}{"\n"}{end}'
   ```
   记录同节点各 Pod 的 restartCount 基线值，并**显式锁定预存异常 Pod 的基线态**
   （CrashLoopBackOff/长期 Pending/Terminating 的 Pod 窗口内按自身节奏继续自重属
   基线内、非注入所致，计划点名供 verifier 勿误判）

## 演练步骤
1. 记录注入前基线（恢复验证的定量对照）：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -c <container> -- df -i <目标目录>
   ```
   记录：Pod RESTARTS 基线、`df -i` 的 Inodes/IUsed/IFree/IUse%（精确到 IUsed 个数，
   恢复对照用 IUse% 粒度太粗）、同节点 Pod 清单（资源准备第 5 条）

2. 计算创建文件数 `<N>`（按落点判定，资源准备第 2 条）：
   - 独立卷打满设计：`<N>` = 该卷 IFree（循环会在 `touch` 因 inode 耗尽返回 ENOSPC
     时自行 break，`<N>` 只需足够大到触底）
   - 无独立卷小增量设计：`<N>` = 固定小值（如 20000），硬约束是创建后节点 IUse%
     只上升可忽略的一档、绝不逼近耗尽

3. 注入 —— **武装定时清理 → 创建海量小文件 → 自证标记**，严格串行于单个载荷
   （定时器先于创建武装——创建后若武装失败，故障将无人回收；定时器 `rm -rf`
   幂等，到期自动清理并写还原标记）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     '( sleep <recovery-seconds>; rm -rf <目标目录>/inode_fill; echo INODE_RESTORED >> <目标目录>/inode.evd ) >/dev/null 2>&1 & \
      mkdir -p <目标目录>/inode_fill; i=0; : > <目标目录>/inode.evd; \
      while [ "$i" -lt <N> ]; do touch "<目标目录>/inode_fill/f$i" 2>><目标目录>/inode.evd || { echo "INODE_EXHAUSTED_AT=$i" >> <目标目录>/inode.evd; break; }; i=$((i+1)); done; \
      echo "INODE_INJECTED count=$i" >> <目标目录>/inode.evd'
   ```
   - 全部创建在**单一子目录** `<目标目录>/inode_fill` 内，恢复时 `rm -rf` 该目录一次性
     释放全部 inode（幂等：目录不存在时空触发无害）
   - 打满设计下循环靠 `touch` 的 ENOSPC 退出码自然 break，`INODE_EXHAUSTED_AT`
     记录触底时的文件数（即该卷可用 inode 数的探测值）
   - 有界小增量设计（非打满）无需 ENOSPC break，可把逐个 `touch` 循环换成批量
     `cd <目标目录>/inode_fill && seq 1 <N> | xargs -n 500 touch`（约 `<N>`/500 次 fork），
     大幅减少创建耗时、避免击穿故障窗口（见注意事项）
   - 载荷必须单次下发：拆成多条独立 kubectl exec 时，武装与创建间隔不可控且违反
     紧邻原则；`&&` 连两条 kubectl 会让第二条沦为第一条 exec 载荷的死参数
   - 链条是**自证的**：`INODE_INJECTED count=N`（注入时刻写入）、
     `INODE_EXHAUSTED_AT`（打满设计触底时写入）、`INODE_RESTORED`（定时器还原后
     写入）落盘证据文件 `<目标目录>/inode.evd`，验证阶段 `cat` 取证，不受故障窗口是否
     已关闭的时序约束。标记不得为缩短命令省略
   - 若目标目录 inode 基线本就近满，写证据标记本身可能 ENOSPC——此时把
     `INODE_INJECTED` 写在创建循环之前（顺序：武装定时器 → 写 INJECTED → 循环创建）

   `<recovery-seconds>`：安全网窗总时长（秒），取 prompt 下发的 `recovery_timer_seconds`
   （= duration + grace，见 SKILL.md 双数窗口契约）——sleep 定时器以它武装，让框架
   在观察窗终点主动派发的恢复先于自治到期落地

   倒计时从武装时刻起算：武装与创建在同一载荷内严格串行（无侵蚀间隙）；武装后发生
   任何修复须先停旧定时器再全额重武装：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'pkill -f "sleep.*; rm -rf <目标目录>/inode_fil[l]" 2>/dev/null; true'
   ```
   （模式串末字符加 `[]` 防自杀——载体 sh -c 命令行含模式串时 pkill -f 会把自己
   杀掉；容器无 pkill 时旧定时器无法停止，到期会提前清理侵蚀故障窗口——须中止
   演练改人工恢复或如实上报缩短的窗口，见 SKILL.md 安全红线「故障窗口完整」）

## 注入验证
（本用例只读判据 df/ls/cat 天然放行；ENOSPC 建文件探针是**写操作**，verify 阶段
read-only 纪律会拒绝，故建文件探针必须在注入载荷内或执行期完成，不可留到验证阶段）：
1. **效果主证——IUse 增量（对比基线）**：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- df -i <目标目录>
   ```
   IUsed 增量 ≈ 创建文件数（`<N>` 或 `INODE_EXHAUSTED_AT`），IUse% 上升幅度与设计
   一致（小增量：上升可忽略的一档；打满：到 ≈100%）。**容器内 `df -i` 才是判据**——
   容器视角的 inode 使用就是业务视角的感知
2. 白盒确认小文件已创建且证据链齐备：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'ls <目标目录>/inode_fill | wc -l; cat <目标目录>/inode.evd'
   ```
   用普通 `ls`——精简镜像（BusyBox 等）的 `ls` 可能不支持 `-U`，报 invalid option 会使
   管道计数假性得 0。文件数与 `INODE_INJECTED count=` 一致；证据文件含 `INODE_INJECTED`
   （打满设计另含 `INODE_EXHAUSTED_AT`；含 `INODE_RESTORED` 即窗口已关，如实报告实际时长）
3. **inode 签名确认（打满设计专属，执行期做）**——建文件返回 ENOSPC 而空间仍有余：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'touch <目标目录>/.inode_probe 2>&1; df <目标目录> | tail -1; rm -f <目标目录>/.inode_probe 2>/dev/null; true'
   ```
   `touch` 输出 `No space left on device`，而同目录 `df`（空间）仍显示可用空间 > 0——
   这对矛盾证据是 inode 耗尽区别于空间耗尽的判别签名
4. **爆炸半径未失控交叉确认（无独立卷靶必做）**（判据 = 逐 Pod 对照自身基线的
   delta、非绝对态，详见 SKILL.md「爆炸半径验证判据」）：
   ```bash
   kubectl get pods -A --field-selector spec.nodeName=<node-name> -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}:{.status.phase}:{.status.containerStatuses[0].restartCount}{"\n"}{end}'
   ```
   同节点 Pod 逐 Pod 对照资源准备第 5 条记录的自身 restartCount 基线，**无注入
   新增的** Evicted/重启漂移（预存异常 Pod 窗口内按自身节奏的继续自重属基线内、
   非注入所致，勿误判）。注意 inode 维度**没有节点 Condition 可查**（无
   InodePressure），收敛证据只能靠同节点 Pod 无注入新增漂移 + 靶为有界小增量
5. **预期阴性声明（小增量设计）**：以下现象**不出现且不作为失败证据**——
   建文件失败/ENOSPC（inode 远未耗尽）、Pod 事件异常、相对基线新增的 Pod 驱逐。
   出现任一项 = 增量过大逼近节点 inode 表 = 爆炸半径失控，立即按恢复命令处置并如实上报
6. **持续性检查**——inode 占用是状态型故障，小文件存活即故障存活：
   45s 后复查 `df -i` 的 IUsed 仍在高位（增量未消失）、`ls <目标目录>/inode_fill` 仍有文件

## 注入恢复
1. 主路径：定时器到期自动 `rm -rf <目标目录>/inode_fill` + 写 `INODE_RESTORED`（Agent 无需干预）
2. 提前恢复（幂等：目录不存在时 `rm -rf` 空触发无害）——**必须拆成两次 exec：先停
   定时器，再删文件写标记**：
   ```bash
   # (a) 停定时器：单独一次 exec，命令行内只有 pkill 模式串、无字面目录名
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'pkill -f "sleep.*; rm -rf <目标目录>/inode_fil[l]" 2>/dev/null; true'
   # (b) 删文件 + 写还原标记：另一次 exec
   kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
     'rm -rf <目标目录>/inode_fill; echo INODE_RESTORED >> <目标目录>/inode.evd; true'
   ```
   **禁止把 (a)(b) 合并进同一条 sh -c**：合并后执行恢复的 shell 自身命令行同时含
   “sleep”（pkill 模式串里）与字面的 `<目标目录>/inode_fill`（rm 目标），
   `pkill -f "sleep.*; rm -rf .../inode_fil[l]"` 会匹配到这个 shell 自己——末字符
   `[l]` 的防自杀只挡得住模式串自身的 `inode_fil[l]`，挡不住同一命令行里第二个
   字面 `inode_fill`；pkill 先把自己 SIGTERM（退出码 143），后续 `rm` 与还原标记都不
   执行，故障文件残留、提前恢复失效。拆开后 (a) 的命令行无字面目录名，`[l]`
   守卫成立。停定时器只杀 subshell，其 `sleep` 子进程成孤儿但无害：父 subshell 已死，
   到期不再触发 `rm`，孤儿 `sleep` 自行退出。

## 恢复验证
1. **inode 定量回落（主证）**：`df -i <目标目录>` 的 IUsed 回落到基线水平
   （±少量应用自身建文件噪声），IUse% 回到基线值
2. 小文件目录不存在：`ls <目标目录>/inode_fill` 报 No such file；证据文件含
   `INODE_RESTORED`（双标记齐备 = 注入与还原全程自证）
3. **inode 签名消失（打满设计）**：执行期建文件探针 `touch <目标目录>/.probe`
   重新成功（不再 ENOSPC）——inode 已释放的直接证据
4. Pod 状态 Running、RESTARTS 与基线一致
5. 爆炸半径全程受控：同节点 Pod 逐 Pod 对照资源准备第 5 条记录的自身基线，无注入
   新增的 Evicted/重启漂移（预存自重 Pod 的基线内漂移排除归因）

## 基准事实
- **根因**：应用产生海量小文件（会话文件、缓存碎片、临时文件、未清理的碎文件）
  耗尽文件系统 inode 表，而磁盘**空间**仍有剩余
- **必现现象**：容器内 `df -i` 的 IUse%/IUsed 上升（增量 = 创建文件数，定量可对账）
- **判别签名**：建文件返回 ENOSPC（`No space left on device`）但 `df` 空间仍有余——
  区别于空间耗尽（空间耗尽时 `df` 空间也满）
- **条件现象**：建文件失败/ENOSPC——仅独立卷打满设计下出现；无独立卷小增量设计下
  为预期阴性
- **无节点告警**：inode 耗尽不产生任何节点 Condition（无 InodePressure，也不翻
  DiskPressure），爆炸半径收敛只能靠靶选择（独立卷）与有界增量，不能靠 Condition 兜底

## 注意事项
- **path B（节点侧）结构性不可用**：释放 inode 必须真正删除文件（`rm`/`unlink`），
  `truncate -s 0` 只清数据块、保留 inode 故对本维度无效；而 `rm` 是宿主面刻意封禁词，
  one-shot `kubectl debug` 下发的 `chroot /host ... rm` 会被 host-escape 门禁拒，其
  systemd-run 定时清理载荷同样含 `rm` 而无法经守卫投递。这不是成本权衡而是结构性
  不可能——故本用例不设 path B，靶必须是有 shell 的容器（distroless/scratch 无解）
- inode 与空间是两个独立维度：`df`（空间）与 `df -i`（inode）分别对账，不要用空间
  使用率推断 inode 状态；小文件多而小时，空间远未满 inode 却先耗尽
- 创建海量文件耗时随 `<N>` 线性增长，shell `touch` 循环速率有限；打满大卷（数百万
  inode）不现实，此类靶应改用有界小增量设计或换小容量独立卷，`<N>` 按探测 IFree 定。
  有界增量设计下用批量创建（`cd <目标目录>/inode_fill && seq 1 <N> | xargs -n 500 touch`，
  约 `<N>`/500 次 fork）替代逐个 `touch`，把创建耗时与文件数解耦、避免击穿故障窗口；
  逐个 `touch` 循环仅在打满设计需靠 ENOSPC 退出码自然 break 时用
- 填充固定用 `<目标目录>/inode_fill` 单一子目录，恢复 `rm -rf` 一次释放，避免散落文件
  漏删；证据文件 `<目标目录>/inode.evd` 与填充目录分开，`rm -rf` 填充目录不清证据
