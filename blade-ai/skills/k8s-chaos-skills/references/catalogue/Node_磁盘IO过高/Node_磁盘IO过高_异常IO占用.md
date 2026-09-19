**用例名称** 异常IO占用 导致 Node_磁盘IO过高

**故障现象**：
1. 节点磁盘 IO 使用率持续过高（iostat 显示 %util 接近 100%）
2. 节点上 Pod 的磁盘读写延迟增大，应用响应变慢
3. iowait 占比升高

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认监控系统可观测节点磁盘 IO 指标
3. **镜像选择（节点缓存约束）**：debug Pod 镜像须为节点已缓存镜像（外网 registry 不可达
   环境 ImagePullBackOff 即注入死锁）。优先复用集群基础组件 DaemonSet 的缓存 tag（定案
   全文：`references/environment/node-cached-images.md`）；规划期至多单命令复核该
   DaemonSet ready/desired 即定案成立。武装镜像需含 `sh` 与 `nsenter`（载荷在宿主机
   执行，`dd`/`iostat` 用宿主机的，镜像内无需自带）
   预探测结论引用：`acs/terway` 镜像含 `sh` 与 `nsenter`（`/usr/bin/sh` +
   `/usr/bin/nsenter` 直证）——planning 直接
   引用该结论即可，**勿再投特权 debug pod 专验镜像内工具**；保留单命令轻探（如 DS
   ready 复核）完成时效核验，镜像 tag 变更时结论须重验（版本相关事实，以当次探测为准）
4. **目标路径校验（必须）**：注入前验证路径存在且可写（宿主机侧）：
   - 常见可用路径：`/tmp`、`/var/log`（根分区/nodefs）；`/var/lib/containerd`、
     `/var/lib/docker`（imagefs，多盘集群可能独立分区）
   - 路径所在盘与空间：宿主 `df -h <路径>` 确认；**禁止使用未校验的路径**——ChaosBlade
     或裸 dd 接受任意路径但底层写进程会静默失败或写错盘
   - 预探测结论引用：常见形态为**单盘节点**（`vda3` 118G 根分区承载 nodefs+imagefs+容器
     overlay），`/tmp` 可写、dd `oflag=direct` 单流吞吐约 900 MB/s（以当次探测为准）
5. **宿主工具链探测**：载荷依赖宿主机 `dd`；验证判据依赖宿主 `iostat`（sysstat）。
   探测宿主无 iostat 时验证降级 `/proc/diskstats` 两采样差分（免工具依赖，见注入验证）

**演练步骤**：
1. 定位目标节点并测基线（注入前必采，供恢复对比）：
   - 宿主 `iostat -xd 1 2`（记目标盘 %util、r/s、w/s、MB/s 基线）
   - 宿主 `iostat -c 1 2`（记 %iowait 基线）
   - `cat /proc/diskstats` 快照（记整盘设备 sectors_written 初值）
2. **载体选型红线**：600s 级持续 IO 载荷**禁止**用 one-shot debug Pod 承载（kubectl 工具
   对 one-shot debug CMD 有硬 120s probe cap，超时即截断自动清理——持续载荷必须由宿主
   systemd 瞬态服务承载：`systemd-run --unit=<名> --collect --property=RuntimeMaxSec=<秒>`，
   到期自停 + unit 即时 GC，零外部清理依赖；详见 `Node_CPU使用率过高_异常进程占用.md` 手段2
   的同款载体选型）
3. 注入（systemd-run 瞬态服务 + trap 自清理载荷，见下方注入命令）
4. 观察节点磁盘 IO 指标及 Pod 性能变化

**注入命令**（手段2 kubectl-native 形态）：

前提条件：`kubectl debug node` 可用（K8s 1.18+）；镜像已缓存且含 `sh`/`nsenter`；宿主机
变更必须 `--profile=sysadmin`；禁用 `-it`；目标路径已校验存在且可写

```bash
# 宿主 systemd 瞬态服务承载 dd direct 写压力循环（守卫认可形态：
#   故障原语 + timeout 时间界 + 显式回收三要素同命令可见，载荷体双引号包裹 + \$ 转义）
# 载荷设计要点：
#   - oflag=direct 绕过页缓存，产生真实 IO 压力（稳态约 130-137MB/s）
#   - wall-clock deadline 循环（date +%s 比较）：迭代时长不确定（burst ~0.5s/稳态 ~3.7s），
#     固定迭代数会跑偏窗口；deadline = duration-50（600→550），为回收 tail 留清理余量
#   - 每轮 timeout -k 5 20：防单轮 dd 卡死卡住整个循环，同时是守卫可见的 timeout 时间界原语
#   - truncate 归零 + unlink 删除 burn_test 回收 tail（先于 RuntimeMaxSec 强杀完成，
#     否则 512MB 文件滞留）；RuntimeMaxSec=<duration> 为 systemd 兜底（unit 到期自 GC）
#   - 守卫时间界认可清单：--on-* timer / timeout N / background sleep N + 显式回收——
#     RuntimeMaxSec 单独出现不被认可为时间界；timeout 原语 + 显式回收满足执法
#   - 载荷体（systemd-run 之后的部分）必须整体被内层双引号包裹成一个参数，宿主侧变量用
#     \$ 转义（外层算好的字面量如 e=<epoch>+<duration-50> 不转义）：
#     `'\''` 多层嵌套引号会被命令传输层打碎（dash exit 2「Unterminated quoted
#     string」）；而零引号形态有更隐蔽的致命缺陷：脚本中的 `;` 被外层
#     容器 sh 解析为命令分隔符，systemd-run 只收到首段 `sh -c e=<字面量>`（瞬即退出），
#     while/dd 循环从未进入宿主 unit——回执照常含 Running as unit 行但故障未注入（假武装）
kubectl debug node/<node-name> --profile=sysadmin --image=<cached-image> -- sh -c \
  'nsenter -t 1 -m -u -i -n -p -- systemd-run --unit=drill-node-disk-burn --collect \
   --property=RuntimeMaxSec=<duration> -- /bin/sh -c \
   "e=$(( $(date +%s) + <duration-50> )); while [ \$(date +%s) -lt \$e ]; do timeout -k 5 20 dd if=/dev/zero of=<path>/burn_test bs=1M count=512 oflag=direct; done; truncate -s 0 <path>/burn_test; unlink <path>/burn_test"'
```

武装回执核验红线：回执须含 `Running as unit drill-node-disk-burn.service` 且**不伴随任何
`sh: N: ...` 报错行**——`sh: 1: [: -lt: argument expected` 类报错 = 载荷 `;` 被外层解析的假武装铁证
（回执通过但 dd 循环未进宿主）。回执异常或首次使用新形态时，另发单次确认检查：宿主
`systemctl is-active drill-node-disk-burn.service` = active + `pgrep -a -x dd` 见 dd 进程 +
`<path>/burn_test` 文件在写。三项确认后才开始效果验证。

**引号坑与 staged-script 降级形态**：引号形态边界——外层单引号内嵌
双引号 + `\$` 转义**可**通过命令传输层（判定要点：unit active + 宿主 dd 运行 + 写吞吐在位）；
`'\''` 多层嵌套引号会被打碎（dash exit 2「Unterminated quoted string」）；零引号形态的
`;` 被外层 shell 分割导致假武装（比打碎更危险——静默无故障）。若需承载更复杂载荷，降级
方案：先经 one-shot debug 把载荷写成宿主脚本文件（base64 编码落地，免引号），再
`systemd-run ... -- /bin/sh <宿主脚本>` 以 plain argv 武装（无嵌套引号），武装成功后删除宿主脚本
文件（host unit 与脚本解耦，删除不影响运行中的服务）。

倒计时从武装时刻起算：注入与到期自停定时在同一 systemd 单元内原子紧邻（无侵蚀间隙）；
武装后发生任何修复需全额重武装：先让旧 unit 到期或人工带外 `systemctl stop` 清除，再重跑
上方注入命令重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）

可选叠加（读压力，验证读吞吐场景时追加第二个 unit）：
```bash
# 先写好种子文件后，读压力循环（iflag=direct 绕页缓存读；载荷体 quote-free，
# 回收 tail 用 deadline 后显式 truncate，与主命令同构）
kubectl debug node/<node-name> --profile=sysadmin --image=<cached-image> -- sh -c \
  'nsenter -t 1 -m -u -i -n -p -- systemd-run --unit=drill-node-disk-burn-r --collect \
   --property=RuntimeMaxSec=<duration> -- sh -c \
   e=$(( $(date +%s) + <duration-50> )); while [ $(date +%s) -lt $e ]; do timeout -k 5 20 dd if=<path>/burn_test of=/dev/null bs=1M count=512 iflag=direct; done'
```

**注入验证**：
1. **主证（/proc/diskstats 两采样差分，免工具依赖）**：宿主侧 `cat /proc/diskstats` 两次
   采样（间隔 3-5s），目标整盘（如 `vda`，跳过分区条目 `vda1/vda3`）的
   delta(sectors_written) × 512 / 间隔秒数 / 1048576 = MB/s；**任何整盘持续写入 >10MB/s
   即 burn 生效**（direct 稳态约 137MB/s，判据 13 倍余量）
   **字段定位红线（防列位错认）**：每行 `major minor name reads reads_merged sectors_read
   ms_reading writes writes_merged sectors_written ms_writing ...`——**sectors_written 是
   name 后第 7 个数字（全行第 10 列）**，不是第 5 个（那是 writes completed 次数）。列位
   错认会使差分结果失真三个数量级（把 writes completed 当 sectors_written，
   137MB/s 会被算成 0.3MB/s，致误判注入失败）。稳妥形态：`awk '$3==vda {print $10}'`
   直接按列名提取
2. **主证（iostat 口径，宿主有 sysstat 时）**：宿主 `iostat -xd 1 3` 目标盘 %util 显著
   抬升、w/s 与写入吞吐远超基线。注意：多队列高速云盘 %util 语义弱化（反映采样期有 IO
   在飞时长，非带宽饱和度）——%util 未达 100% 不构成注入失败判据，以吞吐量主证为准
3. **辅助（iowait）**：宿主 `iostat -c 1 3` 的 %iowait 相对基线显著抬升（如 0.25% →
   数%）。注意：N 核节点单流 dd 的 iowait 物理上限 ≈ 1/N（16 核 ≈ 6%）——iowait 绝对
   值天然受核数稀释，以相对倍数表述，且仅为辅助证据非必要判据
4. **进程确认（可选）**：宿主 `ps -ef | grep dd` 确认 dd 循环进程存在
5. **多磁盘注意**：`iostat` 显示节点所有物理盘指标。多盘节点须按 `--path` 所在盘（`df -h
   <路径>`）只关注目标盘指标变化；单盘节点（nodefs+imagefs 同盘）无此区分

**Pod 级磁盘 IO 验证方法**（弱化为可选辅助）：
- 方法 1（有前提，仅作辅助）：目标节点选一个 Running Pod，`kubectl exec <pod> -- dd
  if=/dev/zero of=/tmp/.iobench.probe bs=1M count=100` 对比注入前后写入耗时。**两个盲区**：
  ① 同盘盲区——Pod 无 hostPath 时 dd 写的是容器 overlay（由 imagefs 所在盘承载），多盘
  节点上常与注入路径所在盘（nodefs）不同，异盘时该方法恒显示无影响 → 误判注入失败；依赖
  此方法前先在宿主机 `df -h <注入路径> /var/lib/containerd` 确认两路径同盘（单盘集群恒
  成立）。② 页缓存掩盖——Pod 内 busybox dd 无 `oflag` 参数，默认经页缓存写入，吞吐虚高，
  即使同盘也可能掩盖真实 IO 压力。**节点级验证（diskstats/iostat）为主证，Pod 级 dd 仅在
  确认同盘后作辅助佐证，不可单独定结论**
- 方法 2：`kubectl exec <pod> -- df -h` 确认 Pod 容器 overlay 文件系统状态，
  `kubectl describe pod <pod>` 检查 Events 中是否有磁盘相关告警
- 方法 3（容器无 dd 时）：`kubectl get events -n <ns> --field-selector
  involvedObject.name=<pod>` 观察是否有 IO 相关事件

**注入恢复**：
1. 标准恢复路径 = **到期自停**（三层时序，零外部干预）：
   - t≈duration-50s：wall-clock deadline 到点 → dd 循环退出（单轮卡死由 per-dd
     `timeout -k 5 20` 兜底）
   - deadline 后立即：`truncate -s 0` + `unlink` 回收 burn_test（在 systemd 强杀 unit
     之前完成——deadline = duration-50 为清理留出 ≥14s 余量，这是零残留的关键时序）
   - t=duration：RuntimeMaxSec 到期 → systemd 停 unit → `--collect` 即时 GC
2. 提前终止（应急）：人工带外 `nsenter -t 1 -m -- systemctl stop drill-node-disk-burn`
   （Agent 恢复阶段该命令会撞禁词拦截，走带外或等到期）

**恢复验证**：
1. **主证（吞吐回落）**：宿主 `/proc/diskstats` 两采样差分，目标整盘持续写入回落至
   <10MB/s（对照基线）
2. **iostat 口径**：宿主 `iostat -xd 1 3` 目标盘 %util 与 w/s 回落至基线水平
3. **残留三查**（零残留判据）：
   - 宿主 `<path>/burn_test` 文件不存在（truncate+unlink 回收回执）
   - `systemctl status drill-node-disk-burn` 报 unit not-found（--collect GC 回执；只读
     阶段等价形态：宿主 `/run/systemd/transient/drill-node-disk-burn.service` 不存在）
   - 宿主 `pgrep -a dd` 无残留压测进程（注意 kthreadd/ipv6_addrconf 是 'dd' 子串误报）
4. 确认应用 A 的磁盘读写延迟恢复正常（Pod 级 dd 写入测试耗时回落，仅作辅助）

**恢复期取证通道备选**：recover 只读阶段拒建新 debug pod（`kubectl debug`
被 readonly 门禁判 mutation）。此时 `/proc/diskstats` 是 **host-global 的**（非 mount
namespace 隔离）——可 exec 目标节点上任一 Running 的平台 DaemonSet Pod（terway/csi/
kube-proxy 等）`cat /proc/diskstats` 拿到与宿主完全一致的整盘计数器，完成两采样差分；
%util 可由第 13 列 ms_doing_io 差分推算、w/s 由第 8 列 writes-completed 差分推算（与
iostat 自身算法等价）。但宿主文件路径（/tmp、/run）无法经此通道核实——依赖 Layer 1
或注入期已采证据 + 当前吞吐的排他性论证（活 dd 循环与 0.4MB/s 吞吐不可共存）。

**基准事实**：
- **根因**：节点上存在异常进程大量占用磁盘 IO，导致磁盘 IO 使用率过高，影响同节点上所有
  Pod 的磁盘读写性能
- **必现现象**：目标整盘持续写入吞吐远超基线（diskstats 差分 >10MB/s）；iowait 相对基线
  抬升（核数稀释下绝对值有限）；同盘 Pod 磁盘读写延迟增大（页缓存掩盖时不可见）
- **环境常见形态（以当次探测为准）**：单盘节点（vda3 承载全部文件系统）；
  宿主工具链完整（dd/iostat/vmstat/mpstat）；dd direct 吞吐分层——**首轮 burst ~900MB/s
  （云盘前端缓存），持续写 ~2 轮后衰减至稳态 ~137MB/s（云盘真实性能）**；稳态 iowait
  基线 0.25%。吞吐判据以稳态口径设计（137MB/s ≫ 10MB/s）；首轮 burst 高读数不构成
  「吞吐异常」判据噪音（稳态才代表持续压力）

**CRD 模式 overlay 文件系统关键说明**（手段1 通用知识，ChaosBlade 可用环境适用）：
- ChaosBlade K8s CRD 模式下，`node-disk burn --path /tmp` 的 dd 进程运行在 DaemonSet tool
  pod 内，写入的是**容器 overlay 文件系统**（通常由 imagefs 支持），而非宿主机 /tmp（通常
  由 nodefs 支持）——多盘集群两者可异盘，单盘集群同盘等效
- **验证 burn 时不应该检查 /host/tmp/ 下的文件**：burn 产生的临时文件在 overlay 中，不会
  出现在宿主机路径
- **df -h 对 burn 验证无效**：burn 产生的是 I/O 压力（临时文件自动清理/循环覆写），不造成
  磁盘使用量持续增长。用 df -h 验证 burn 可能误判为「注入失败」
- **fill 与 burn 的验证方法差异**：fill（空间填充）用 df -h 主证（持久数据）；burn（IO
  压力）用 /proc/diskstats 差分主证（不产生持久数据）
- **手段可用性判据（以当次探测为准）**：手段1（blade node-disk burn）依赖 chaosblade tool
  pod Running——受限网络环境常见形态为 tool pod 全员 ImagePullBackOff（外网镜像不可达），手段1
  不可用，手段2（kubectl-native）为唯一路径。tool pod 状态以当次探测为准

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时（受限网络环境常见形态），使用 kubectl 原生命令实现等效故障注入。
> 注入命令、载体选型红线、武装回执核验、恢复路径见上方「演练步骤/注入命令/注入恢复」
> （systemd-run 瞬态服务 + trap 自清理载荷）。

注意事项：
- `oflag=direct` 绕过页缓存直接写磁盘，确保产生真实 IO 压力（吞吐远超判据阈值）
- dd 写入会产生实际文件（恒 512MB 循环覆写不增长），路径分区须有相应可用空间
- 循环覆写语义：dd 每轮重开输出文件即 truncate，文件大小恒为单轮 count——多轮不累积
- 600s 级 duration 必须由 systemd 瞬态服务承载（RuntimeMaxSec 到期自停），禁止 one-shot
  debug Pod + timeout 形态（120s probe cap 截断）
- debug 命令客户端会阻塞或断连，但宿主 systemd 单元服务端持续运行，客户端超时不代表注入
  失败（systemd-run 武装回执秒回，此形态客户端不阻塞）
- 恢复期禁止依赖 Agent 侧宿主 `rm`/`systemctl`（mutation/host-escape 门禁）——trap EXIT
  载荷级自清理 + unit --collect 自 GC 已覆盖全部清理需求；确有残留时人工带外兜底
