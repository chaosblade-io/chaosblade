**用例名称** 日志或临时文件堆积 导致 Node_磁盘空间不足

**故障现象**：
1. 节点磁盘使用率告警（大于85%）
2. 容器无法写入数据
3. `/var/log`、`/tmp` 目录占用空间大

**资源准备**：
1. 确认应用 A/B 已正常运行
2. 确认节点磁盘空间充足

**演练步骤**：
1. 定位运行应用 A Pod 的节点
2. 使用 chaosblade 或其他故障注入工具向节点 `/var/log` 或 `/tmp` 目录持续写入数据，模拟日志或临时文件堆积
   - **手段1 可用性判据（以当次探测为准）**：chaosblade operator 无可用副本（无 ready 的 operator Pod）或 chaosblade-tool Pod 全员 ImagePullBackOff/CrashLoopBackOff 时，手段1 判死，直接走手段2 kubectl-native（勿再投探测 Pod 复验已判死结论——单命令轻探 chaosblade ns 下 operator ready 数即可）
   - **路径语义**：在 K8s CRD 模式下（默认），`--path /tmp` 或 `--path /var/log` 是相对于容器 overlay 文件系统的路径，填充数据**通常**写入 imagefs 分区（需节点有独立 imagefs）；`--path /var/lib/docker` 等宿主机路径**通常**写入 nodefs 分区。实际分区取决于节点配置，验证时需确认检查的分区与填充目标分区一致
3. 观察节点磁盘使用率变化

**CRD 模式路径→分区映射表**：
**注意**：下表为常见配置下的启发式映射。实际分区取决于节点的挂载配置：
- 如果节点没有独立的 imagefs（容器运行时数据在根磁盘），所有路径都在 nodefs 上
- `/var/lib/docker`、`/var/lib/containerd` 在独立磁盘时属于 imagefs（它们定义了 imagefs），不是 nodefs
- 验证时以 `df -h`（无路径参数）的实际输出为准，而非下表

在 K8s CRD 模式下（ChaosBlade Operator），`--path` 参数决定填充数据写入哪个分区。验证时**必须**检查对应分区，否则会产生假阴性。

| `--path` 值 | 目标分区 | 典型后端设备 | 正确验证命令 | 常见假阴性命令 |
|---|---|---|---|---|
| `/tmp`, `/var/log`, `/var/run`, `/run` | imagefs（容器 overlay） | `/dev/vdb` 等独立磁盘 | `df -h`（无路径参数）列出所有挂载点，找 overlay 分区 | `df -h /host`（仅显示 nodefs） |
| `/var/lib/docker`, `/var/lib/containerd`, `/var/lib/kubelet` | nodefs（根文件系统） | `/dev/vda3` 等根分区 | `df -h /host` 显示根分区使用率 | `df -h`（找错 overlay 分区） |
| `/etc`, `/root`, `/home` | nodefs（根文件系统） | `/dev/vda3` 等根分区 | `df -h /host` 显示根分区使用率 | 同上 |
| 其他路径 | 需 `df -h` 列出全部后判定 | 不确定 | `df -h`（无路径参数）识别哪个分区使用率变化 | 仅检查单一分区 |

**注入验证**：
0. **基线完整性检查**（验证阶段第一步，必须先于其他验证步骤执行）：
   - **首条命令必须**是 `df -h`（无路径参数），列出所有分区使用率，标注与注入 `--path` 对应的分区（根据上方路径→分区映射表）。禁止先执行 `df -h /host` 或 `df -h /host/<path>` — 这些命令只显示 nodefs，会在 imagefs 场景下产生错误基线（**注意场景区分**：本条适用于**注入后验证期**（需全分区对比防假阴性）；**注入前基线预探测**相反——用带路径形态 `chroot /host df -h /var/log` 单行精确定位（见手段2 命令 0 注释：无路径全列回执截断会丢根分区行，勿泛化混用两场景））
   - 后续验证中，只对比**同一分区**的使用率变化。禁止将 nodefs 基线与 imagefs 注入后数据做对比
   - 若未在注入前记录目标分区基线，须在验证结论中注明"No pre-injection baseline available for <分区类型>"，并以首次检查值作为参考点
   - 若首次检查值已接近注入参数预期值（如 `--percent 85` 对应 84%），此即故障生效的证据，无需等待"变化"
   - **取证时序红线（故障自阻取证通道）**：DiskPressure 翻转后节点被 taint `node.kubernetes.io/disk-pressure:NoSchedule`——**新 debug pod 调度被故障本身阻断**（磁盘满 case 特有的结构性现象：取证通道被故障关闭）。取证策略：①前段取证（填充后立即的 df/ls）赶在 taint 翻转前（DiskPressure 翻转约在填充落地后 ~1min）；②taint 已翻时用第三通道（见下条）；③verify 判据须容忍取证通道降级（crictl 类宿主深度取证标 skipped + 理由，非判据步骤不阻断裁决）
1. 查看节点磁盘使用率，确认磁盘使用率变化。通过 `kubectl debug` 两步法查看宿主机磁盘使用率：
   - **第三取证通道（hostPath bind-mount 容器）**：目标节点上挂载了宿主 `/var/log` hostPath 的存量平台 Pod（如 kube-system/csi-plugin，其 /var/log 是宿主真实目录的 bind-mount 而非 overlay）——`kubectl exec <csi-plugin-pod> -n kube-system -c csi-plugin -- df -h /var/log` 的 statvfs 落在**真实宿主 /dev/vda3** 上（输出显式命名 /dev/vda3 即为验证）。此通道与被禁的「exec 普通 Pod 看 overlay 假视图」本质不同：bind-mount 使容器文件系统视图直接就是宿主路径。taint 阻断 debug pod 时的首选降级通道；判别标准：df 输出必须显式命名宿主设备名（/dev/vda3），否则视为 overlay 假视图弃用
   - Step 1：`kubectl debug node/<节点名> --image=<verified-cluster-image> -- sleep 3600`（`-- sleep 3600` 覆盖镜像 ENTRYPOINT 作保活骨架；**镜像定案：勿用 busybox**——受限网络集群 docker.io 常不可达，busybox 拉取会 ImagePullBackOff 死循环烧约 60s/次；用集群缓存镜像（如 terway 系，以当次探测为准），框架层 debug pod 候选链轮试已自动适配，Agent 规划层只需钦定镜像一处）
   - Step 2：从 Step 1 输出找到 debug pod 名称，然后 `kubectl exec <debug-pod> -n default -- df -h`
   - `df -h` 列出所有挂载的文件系统。如果节点有多个磁盘分区（如 nodefs 和 imagefs 分开），需检查**所有分区**的使用率变化。`df -h /host` 仅显示 nodefs（根分区）；imagefs 的变化需通过 `df -h` 列出所有挂载点来识别
   - **注意**：otel-c-tool 不挂载 /host，`kubectl exec <otel-c-tool> -- df -h` 显示的是 overlay 文件系统，不能用于宿主机磁盘验证。禁止仅通过 `kubectl exec <Pod> -- df -h` 验证（容器 overlay 文件系统≠宿主机文件系统）
2. 检查 `/var/log`、`/tmp` 目录大小，确认占用显著。需通过 kubectl debug 两步法进入 debug pod，路径加 `/host/` 前缀（如 `du -sh /host/tmp`、`du -sh /host/var/log`）

   **CRD 模式重要提醒**：`du -sh /host/<path>` 检查的是宿主机文件系统（nodefs）。如果注入使用了 `--path /var/log` 或 `--path /tmp`（在 CRD 模式下写入容器 overlay/imagefs），`du` 将看到宿主机路径的**原始大小**，不会反映 fill 数据。此时 `df -h`（bare, 无路径参数）展示的 overlay/imagefs 使用率变化才是 fill 效果的正确证据。

   - 若 `df -h` 已确认目标分区使用率达到预期 → Step 2 可标记为 `expected: du checks host nodefs, fill in imagefs overlay (confirmed by df -h)`，不要标记为 `failed`
   - 若 fill 路径映射到 nodefs（如 `--path /var/lib/docker`）→ `du -sh /host/<path>` 应能观察到增长
3. 使用容器运行时工具查看容器、镜像和日志占用
4. 使用 `kubectl describe node <节点名>` 检查 DiskPressure 条件，确认 K8s 层面已检测到磁盘压力
   - **DiskPressure 两深度分叉（预期管理）**：kubelet 默认 eviction 阈值 `imagefs.available<15%`（单盘节点 nodefs=imagefs 同分区取更严者）。**填到 85% 使用率时 available≈15%，恰在阈值临界**——DiskPressure **可能翻转**（使用率在阈值附近波动即触发）。这不是注入失败，恰是磁盘满的真实 K8s 层现象；DiskPressure 翻转后 kubelet 先启动镜像 GC，BestEffort Pod 驱逐是最后手段（验证用的 debug pod 若为 BestEffort 有被驱逐风险，取证应在翻转前后尽早完成）。保守深度（85% 即止 + timer 及时恢复）下 DiskPressure 翻转应记录为**正向证据**（K8s 层检测到压力 = case 现象「磁盘使用率告警」的机制回显）而非异常；深水区（>90% 饱和填充）会触发激进驱逐与节点级 GC，非本 case 默认形态。填充量算法若分区已用 >80%，增量可能不足以让使用率曲线明显移动，须用填充文件的**字节数直证**（ls -l 精确字节）作辅助主证——比百分比更鲁棒

**验证语义说明**：区分两个概念：(1) 注入验证——填充数据是否写入成功（通过填充文件或磁盘增量判断）；(2) 场景验证——磁盘使用率是否达到 85%。注入验证通过但场景验证未达标时，Layer 2 应报告 'passed' 并在 Warnings 中注明阈值未达到。如果使用率未达 85%，说明 --size 参数过小，应增加 size 或改用 percent 参数。这不代表注入失败。

**持续性判据语义（与 IO 类故障的根本差异）**：磁盘空间堆积是**静态故障**——填充文件落地后使用率持续保持，无需像 IO 类那样依赖连续写负载。verify 的「持续性」体现为**两次观察**（间隔 ≥30s）使用率/填充文件大小稳定不回落（确认非瞬时缓存效应），一次观察即可证明「在位」；不需要观察「活动」（无持续写入过程）。fallocate 填充的字节直证（ls -l 精确字节）比使用率百分比更早可判（metrics 采样滞后不影响文件大小）。

**注入恢复**：
1. 销毁 chaosblade 实验
2. 停止数据写入或删除创建的大文件
3. 清理日志文件

**恢复验证**：
1. 查看节点磁盘使用率，确认恢复到正常水平。使用 kubectl debug 两步法（同注入验证），执行 `df -h`（无路径参数）检查所有磁盘分区均恢复
   - **宿主 df 通道边界**：`df` 读的是宿主文件系统**非 host-global**（与 /proc/diskstats 不同）——`kubectl exec <任意 DaemonSet Pod> -- df -h` 看到的是容器 overlay，**不能**复用「exec 平台 DaemonSet Pod 免 debug pod」通道。宿主 df/du 取证必须走 debug pod（chroot /host 或 --profile=sysadmin 挂 /host）。若 verify 只读阶段拒建 debug pod，用逻辑等价论证补：填充文件不存在（`ls /host/tmp/app-archive.log` 或宿主侧 stat）+ 使用率回基线 ⇒ 磁盘空间必然已释放
2. 确认应用 A/B 可正常写入数据
3. 确认 `/var/log`、`/tmp` 目录大小恢复正常（需通过宿主机文件系统访问方式验证）
4. **恢复期 Agent 侧禁止宿主 rm/systemctl**（mutation 与 host-escape 门禁）：timer 到期自动 rm 是设计内主恢复路径；Agent 侧只做只读核实。若 timer 落空（fire 后文件仍在），走守卫兼容四路径（逻辑等价证明 / 人工带外兜底），不得伪造证据形态绕过
5. **DiskPressure/taint 翻回的 ~5min transition period**：timer fire 删除填充文件**即时生效**（df 使用率立即可见回落），但 DiskPressure=False 翻转与 taint 清除**非即时**——kubelet 压力状态翻转有过渡期（防抖动设计），常见形态为 fire 后约 5min 才翻回 False。恢复验证时窗内 taint 仍在 / DiskPressure 仍 True **不是恢复失败**：主恢复证据 = 填充文件不存在 + 使用率回基线；DiskPressure 翻回作为最终一致项（带外终验等 ~5min 或异步注明预期延迟，勿在翻回前反复轮询）
6. **Evicted husks 分诊（与第 5 条同一过渡窗）**：DiskPressure=True 窗口内被 kubelet 准入拒绝的 Pod（含 DS 控制器的重建尝试）留下 Evicted 终态壳，分诊两问：
   - **谁拥有？** DaemonSet-owned Evicted husks（node-exporter / chaosblade-tool / drill-ds-target / NPD 等）**勿删**——DS 控制器在 DiskPressure 翻回后会自动重建健康副本，过渡窗内强删是徒劳 churn（替代 Pod 同样被准入拒绝、变成新 husk）；它们随 DiskPressure=False 自愈，属第 5 条过渡窗的滞后项而非未恢复残留。**ownerless Evicted husks**（无控制器、如 kubectl debug 留下的 node-debugger-*）**必须显式 delete**——无主 Pod 永不自愈，是终验残留（唯一合法的 K8s 层 mutation）
   - **判别法**：`kubectl get pods -A --field-selector spec.nodeName=<node>` 列名对 DS 名册（DS 名下 = 等；不在名册 = 删）。判别不清时看 AGE 是否随观察窗推进变 churn（churn = 控制器仍在尝试 = 等）
   - 预期成本收益：recover 免去常见 3-4 轮现场分诊推理（每轮 30-67s 反复推演「删还是等」）

**基准事实**：
- **根因**：容器日志或临时文件未清理，导致磁盘空间被占满
- **必现现象**：节点磁盘使用率大于85%，`/var/log` 或 `/tmp` 目录占用空间大

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效节点磁盘填充。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择已验证可拉取且含 `chroot`/`sh` 的镜像；宿主机变更必须 `--profile=sysadmin`；禁用 `-it`

注入命令（填充量必须按**增量**计算，先经 debug pod 测目标分区基线）：
```bash
# 0) 先测目标分区基线：总容量、已用量（df 的路径用宿主机真实路径）
#    **基线预探测必须用带路径形态（勿用无路径 df -h 全列）**：无路径全列的回执是
#    logs-tail 截断视图——头部的根分区行（/dev/vda3 ... /）会被截掉，auto-extract
#    只能从 overlay 行提取（标签 "overlay" 而非根分区名），须二次补探确认分区归属
#    （截断后补探的常见成本约 78s 思考+重探）。带路径单行输出无截断风险，且一步
#    确认「目标路径落在哪个分区」——这正是基线探测的两个目的（容量数字 + 分区归属）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host df -h /var/log

# 1) 填充量 = 分区总容量 × 目标使用率(如 85%) − 当前已用量
#    例：分区 100G、已用 50G、目标 85% → 100×0.85 − 50 = 35G

# 2) 通过 kubectl debug node 在 /var/log 或 /tmp 目录填充数据。
#    **先武装定时清理，再填充**：timer 由宿主机 systemd(PID 1) 管理，到期自动删除填充文件；
#    `&&` 串联保证武装失败时不会执行填充
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-diskfill rm -f /var/log/app-archive.log &&
   dd if=/dev/zero of=/var/log/app-archive.log bs=1M count=<算出的填充量换算的MB数>'
# 或使用 fallocate（更快）：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-diskfill rm -f /tmp/app-archive.log &&
   fallocate -l <算出的填充量>G /tmp/app-archive.log'
```

**引号形态红线（假武装风险，三形态边界）**：上述命令外层单引号内嵌**零嵌套引号、零 `;`、零 `$()`** 的简单载荷可以原样使用；一旦载荷复杂化（循环/算术/多语句 `;`），外层单引号内的**双引号 + `\$` 转义**是唯一经验证安全的形态（外层单引号包住 `sh -c "..."`，内层 `$(date +%s)`、`$e` 等变量写成 `\$(date +%s)`、`\$e` 传递给宿主 shell 展开），`'\''` 多层嵌套会被传输层打碎（dash exit 2），**零引号 `;` 被外层容器 sh 分割致假武装**（systemd-run 只收到首段瞬即退出，回执照常含 Running as unit 行但故障从未注入——比引号打碎更危险，静默无故障）。本 case 的填充+timer 复合载荷推荐拆两条命令分别传输（先武装 timer、后填充），每条保持零引号 `&&` 串联——比嵌套引号更稳。

**武装回执核验红线**：timer 武装回执须含 `Running as unit blade-restore-diskfill.timer`（或 service 名）且**无 `sh: N:` 报错行**；回执为空或含报错行时，须另发确认检查（`systemctl list-timers blade-restore-diskfill*` 经 debug pod）核验 timer 落位，勿直接进入填充步骤。
恢复命令（timer 到期前可提前手动恢复——**Agent 在场时此命令会被 host-escape 门禁拦截 systemctl**，提前恢复走 LLM 只读核实 + timer 自然到期，或人工带外执行；人工带外时用）：
```bash
# 提前恢复：删除填充文件（同时停掉已武装的 timer）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemctl stop blade-restore-diskfill.timer 2>/dev/null; rm -f /var/log/app-archive.log /tmp/app-archive.log'
# 删除 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
```

注意事项：
- **工具层 120s probe cap 红线**：one-shot `kubectl debug` 命令（`-- chroot /host ...` 直接收尾）被工具层视为探针，回执有 120s 硬上限。本 case 的填充载荷（dd 数 GB 级 或 fallocate 秒级）若用 one-shot 形态执行，dd 大填充量会被 cap 截断（文件只写了一部分）。**大填充量必须走持久 debug pod 两步法**（Step 1 建常驻 debug pod + Step 2 `kubectl exec` 执行填充——exec 通道无 probe cap）；fallocate 秒级完成可用 one-shot。timer 武装命令是秒级 one-shot 合法
- **镜像名必须用集群内部 registry 完整名**：`registry-vpc.cn-shanghai.aliyuncs.com/acs/terway:v2.0.0` 公网简名拉取 i/o timeout（ErrImagePull）；正确形态是集群实际缓存 tag 的完整名（如 `registry-cn-shanghai-cloudspe-vpc.ack.aliyuncs.com/acs/terway:v1.12.1-f2d6cd5`，从 terway-eniip DS spec 原样引用，以当次探测为准）
- **填充量安全上界**：目标使用率 85% 是告警线而非饱和线——填充后分区剩余空间须 >10%（避触碰 DiskPressure 硬阈值）；增量计算若为负（已用 >85%）说明分区本已告警，先换节点或清理
- 填充路径对应的分区取决于节点配置，需参考上方「CRD 模式路径→分区映射表」
- 与 ChaosBlade `--percent` 不同，此方式需按**增量**手动计算填充字节数（填充量 = 分区总容量 × 目标使用率 − 当前已用量）；量太小达不到 85% 告警阈值，量太大把分区填满会触发非预期的 DiskPressure/驱逐
- 自恢复基于 systemd-run transient timer 到期自动删除填充文件，补齐了 ChaosBlade `--timeout` 的自恢复能力；timer 载荷里的文件路径必须与填充路径逐字一致
- 同名 transient timer 重复武装会报 `Unit blade-restore-diskfill.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先按本文件注入命令的同等 chroot /host 通道形态清理残留：`systemctl stop blade-restore-diskfill.service; systemctl reset-failed blade-restore-diskfill.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
- **残留预检（ls 形态）回执判读**：planning 期冲突预检 `chroot /host ls -l /var/log/app-archive.log` 在**无残留**时回执是 error 通道——`one-shot debug command failed with exit_code=2` + stderr `ls: cannot access '/var/log/app-archive.log': No such file or directory`。**这恰恰是预检通过的预期形态**（文件不存在 = 无冲突 = 可武装）：ls 的非零 exit 是 POSIX 语义（目标不存在）而非注入/探测失败，勿当异常处理、勿重试、勿换探测通道——读 stderr 即得结论。同理适用于所有「确认不存在」类预检（timer unit 残留、旧填充文件等）；预检**有**残留时 exit 0 + 文件行输出，按上条清理流程处置。（改 ls 命令形态塞 `|| echo ABSENT` 走正常通道不可取——引入引号嵌套违反传输层红线，读回执才是零成本路径）
- **timer 残留预检优先列表形态**：timer unit 存在性预检有比 `systemctl status <unit>`（无残留时 exit 4 error 通道）**更优的形态**——`systemctl list-units '<unit>'`：无残留时回执 exit 0 + `0 loaded units listed`，**自然落成功通道**（错误管线与 RUNTIME EVIDENCE reminder 根本不触发，连上一条的回执判读都免）；有残留时输出该 unit 行直读。此形态规划期可直接套用零浪费——「确认不存在」类预检凡有列表查询等价形态（systemd units / kubectl get）一律优先列表形态，把缺席结论留在 exit 0 里而不是靠非零 exit 语义判读。
