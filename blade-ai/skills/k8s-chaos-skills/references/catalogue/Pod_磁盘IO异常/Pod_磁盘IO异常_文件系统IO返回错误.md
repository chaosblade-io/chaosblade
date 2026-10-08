---
# 机制写入集立法（write-set approval contract，W-56-1 派生节点授权）：
# 本用例受害者是 Pod（Pod_磁盘IO异常），但故障机制必须写受害者所在的宿主机
# node——dmsetup 改写块设备映射表（kubectl debug node + chroot /host 载体路径）。
# 受害 Pod 落在哪个 node 由集群调度运行时决定，节点名既无法在可移植 case 文档里
# 硬编码（换集群即失效），也不共享演练前缀，故用派生选择器 name_from: victim_node
# ——case 只立法语义「受害者所在节点」，确定性代码在意图定案（freeze）时由
# discover_victim_nodes 解析真实节点名、materialize_derived_entries 物化进本条目；
# 冻结后 carriers 节点审批（chroot 载体路径）与 drift 守卫两处执法点据此放行。
# 派生失败（受害 Pod 未调度/不存在）则丢弃本条目、node 写被拒（fail closed）。
# LLM 无权扩写。
mechanism_writes:
  - scope: node
    name_from: victim_node
---
**⚠️ 注意：此场景为 kubectl-native 方案。选用前提是 ChaosBlade 没有 pod-IO target（以 `blade create k8s --help` 探测为准；若本地版本提供 `pod-IO errno`，优先用它），需通过 kubectl exec + dmsetup（device-mapper）在特权节点 debug Pod 中实现。**
**执行边界**：dmsetup 改写的是**块设备映射表**（目标设备指定区段的数据可用性受直接影响）。工具守卫只校验顶层命令与 `--` 之前的参数（载荷内命令不参与 token 检查），真正的把关在框架门禁层：**载体门**要求执行载体是本任务登记的特权节点 debug Pod（`kubectl debug node/<node>` 创建、privileged、节点在批准集合内——含本文件 frontmatter `name_from: victim_node` 派生的受害节点），**自恢复门**要求命令自身携带**有界恢复**（时间界定时器 + 配对的逆操作，见演练步骤 4）；两门缺一即被拒。

**2026-09-23 实测注记**：本环境本地 blade CLI 无 pod-IO target；且目标集群 ChaosBlade Operator 不可用（chaosblade namespace 零资源、chaosblade-tool 全量 ImagePullBackOff）——k8s scope 的 blade 手段（含下方替代方案 pod-disk burn）均不可用，实测走 kubectl-native 主路径（节点 debug Pod + 宿主 systemd 定时器，与 #53 节点宕机用例同族形态）。

**用例名称** 文件系统IO返回错误 导致 Pod_磁盘IO异常

## 故障现象
1. 应用写入数据失败，日志出现 I/O error 或 errno 5（EIO）
2. 数据库/缓存持久化操作异常，数据丢失风险
3. 文件系统读写操作间歇性返回错误码
4. 应用功能降级或部分请求失败

## 资源准备
1. 确认应用 A 已正常运行，且有活跃的磁盘读写操作
2. 确认目标 Pod 内的文件路径存在且有读写活动
3. 确认监控系统可观测应用错误率和日志
4. 确认节点侧能力：集群支持 `kubectl debug node`（K8s 1.18+）；debug 镜像含 `chroot`/`sh`（本环境已验证：terway 镜像）；宿主机（经 `chroot /host` 可达）含 `dmsetup` 与 `systemd-run`：
   ```bash
   # 载体就绪后探测宿主机工具链（均有输出即通过）
   kubectl exec <node-debug-pod> -n <debug-namespace> -- chroot /host sh -c 'command -v dmsetup systemd-run'
   ```
5. （仅「步骤 5 挂载给应用」形态需要）确认目标 Pod 具备访问该设备的条件（特权或 hostPath 卷）；默认纯 error 设备形态对目标 Pod 无要求

## 演练步骤
1. 定位应用 A 的 Pod，确认目标文件路径：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- ls -ld <目录>
   ```
2. （可选，环境核查）获取目标目录所在块设备信息——纯 error 设备形态下注入命令不依赖此结果，仅用于理解受害 Pod 存储形态（见下方云盘前置核查）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- df <目录>
   kubectl exec <pod-name> -n <namespace> -- lsblk
   ```
3. **创建节点 debug Pod 载体**（后续所有宿主命令经它 exec 派发——载体门要求 exec 目标为本任务登记的节点 debug Pod）：
   ```bash
   # <node> 为批准集合内的目标节点（本文件 frontmatter name_from: victim_node 的派生值，
   # 即受害 Pod 所在节点）；镜像须可被节点拉取且含 chroot/sh（本环境已验证：terway 镜像，
   # #53 节点宕机用例同款）；sleep 覆盖演练全程
   kubectl debug node/<node> -n <debug-namespace> --profile=sysadmin --image=<pullable-image> -- sleep 3600
   ```
   - `--profile=sysadmin` 创建的 Pod 默认 privileged，并把宿主根文件系统挂载到 `/host`
   - 载体创建成功后自动登记为 debug_pod（scope=node + 节点绑定）——登记是后续 `chroot /host` exec 通过载体门的前提；执行前以 `kubectl get pod -n <debug-namespace> -o wide` 复核 Running 与节点
   - timer 由宿主 PID 1 管理、不依赖载体存活（载体退出后仍按期 fire 移除映射）；但在线段的注入/恢复验证探针须经载体 exec 派发——`sleep` 取值需覆盖到 Agent 提交结论收尾为止

4. **一条命令内完成「武装定时恢复 + 注入」**（先武装，`&&` 保证武装成功才注入；恢复命令
   幂等：定时器到期自动移除 error 映射为主恢复路径（自恢复，不依赖 Agent 在线）；Agent
   在线段提交结论即收尾、从不亲自执行恢复（#43 立法），定时器丢失或需提前收尾时移交带外
   `blade-ai recover --task-id`，迟到重复执行无副作用（映射已移除时报 not found）。dmsetup 是节点本地操作，恢复定时器
   用**宿主机 systemd-run 登记**——timer 由宿主机 PID 1 管理，不依赖 debug Pod 存活
   （debug Pod 到期退出后，timer 仍按期自动移除了 dm 设备）；登记命令须经 `kubectl exec`
   载体派发，顶层裸 `sh -c` 不被工具守卫放行）：
   ```bash
   # <dm-name> 必须与实际创建的 dm 设备名一致——曾因中途改设备名，首个 timer 到期时报
   # 移除目标不存在，需二次武装正确名字
   # <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
   kubectl exec <node-debug-pod> -n <debug-namespace> -- chroot /host sh -c \
     'systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-dmerr \
        sh -c "dmsetup remove <dm-name>" && \
      echo "0 1024 error" | dmsetup create <dm-name>'
   ```
   - 原理：device-mapper 的 `error` target 会对所有落入该区段的 IO 请求返回 EIO
   - **武装与注入必须在同一条命令内**：门禁按**单条命令**判定「时间界 + 配对逆操作」——
     拆成「先单独武装」「再单独注入」两条时，各自都缺另一半而分别被拒
   - **不要**用 `dd`/`blockdev` 改写原有分区来构造映射（对挂载中设备不可行，见下方云盘前置核查）
   - 武装后发生任何修复需全额重武装：先停旧定时器 `systemctl stop blade-restore-dmerr.timer blade-restore-dmerr.service 2>/dev/null; systemctl reset-failed blade-restore-dmerr.service 2>/dev/null`，再重跑上方命令（重武装+重注入，幂等；见 SKILL.md 安全红线「故障窗口完整」）
   - 载荷零 `$` 字符（wiz 通道对 `$` 语法拒斥——#63 沉淀）；双层引号形态（内层 `sh -c "..."`）与 #53 节点宕机用例同构、已实弹验证；若通道对管道 `|` 报错，可用等价形态 `dmsetup create <dm-name> --table "0 1024 error"`

   > ⚠️ **linear 段源设备占用前置核查（限阿里云 ECS 云盘环境）**：对**挂载中的云盘**
   > 创建含 linear 段的映射一律失败——应用 PVC 云盘（挂载中）与容器盘
   > /var/lib/containerd 均报 `device-mapper: reload ioctl on <name> (252:0) failed:
   > Device or resource busy`（挂载文件系统对源设备持有独占打开，与 linear target 的设备
   > 获取冲突），「对已挂载文件系统所在分区直接建映射」的路径在本环境**不可达**。可行
   > 形态为**纯 error 设备**（无源设备依赖，创建即成功）：
   > ```bash
   > # 创建纯 error 设备（示例 512KB = 1024 sectors）
   > kubectl exec <node-debug-pod> -n <debug-namespace> -- chroot /host sh -c \
   >   'echo "0 1024 error" | dmsetup create <dm-name>'
   > ```
   > 纯 error 设备独立于 debug Pod 存活（载体退出后设备仍在），且不影响宿主机既有挂载
   > （原云盘读写全程正常——爆炸半径仅为 dm 设备本身）。

5. **（可选，非本 case 必需）** 将应用的写入路径指向 error 设备——通过 hostPath 或块设备卷
   将 `/dev/mapper/<dm-name>` 挂给目标 Pod。本 case 默认形态为**纯 error 设备**（未挂载），
   应用层无感知，判据以「注入验证」第 1 条（机制层直读返 EIO）为准；仅当需要观察应用层
   I/O error 日志（判据 2/3）时才执行此步

**替代方案（更安全，推荐用于非特权环境；注：本环境 ChaosBlade Operator 不可用、此段实际不可达——见头部实测注记，保留供其他环境参考）**：
使用 `pod-disk burn` 制造高 IO 负载，间接导致 IO 超时和错误：
```bash
blade create k8s pod-disk burn \
  --read --write \
  --path / \
  --size <size> \
  --namespace <namespace> \
  --labels "<label-key>=<label-value>" \
  --timeout <duration>

```
倒计时从武装时刻起算：--timeout 与注入命令一同下发原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `blade destroy <experiment_uid>` 旧实验，再重跑上方注入命令重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）
- `--path`：必须使用 `/`（容器根文件系统）。不要使用 EmptyDir、hostPath 等子目录挂载路径，这些路径在 ChaosBlade nsexec 模式下校验会失败
注意：`pod-disk burn` 制造的是 IO 高负载（吞吐饱和），而非精确的 errno 返回，效果为 IO 延迟剧增而非确定性错误码。

## 注入验证
1. 对 error 映射设备发起 **direct 读**，确认返回 IO error（在节点 debug Pod 内执行）：
   ```bash
   kubectl exec <node-debug-pod> -n <debug-namespace> -- chroot /host sh -c \
     'dd if=/dev/mapper/<dm-name> of=/dev/null bs=64k count=1 iflag=direct'
   ```
   - ⚠️ **判据必须带 `iflag=direct`**：默认（buffered）读取命中页缓存即返回成功（RC=0），
     EIO 不出现，形成「注入未生效」误判；iflag=direct 直读立即报 `Input/output error`
     （写入用 oflag=direct 同样 EIO）
   - ⚠️ **用读、不用写**：写原始块设备的形态（`of=/dev/mapper/...`）在宿主通道会被工具守卫
     的「防磁盘破坏」硬底线拒绝（不可 reshape）；且若映射带 linear 段（前半指向源设备），
     写给段将**透写源设备数据**造成真实破坏——读形态无此风险，判据等价
   - ⚠️ 读取量必须小于 dm 设备大小（示例 1024 sectors = 512KB，读 64KB 足够），越界读先报
     `No space left on device`（ENOSPC，是设备大小限制而非 error target 生效）
   - ⚠️ **verify 阶段只读纪律**：写型判据
     （`dd of=/dev/mapper/<dm-name>`）在 verify 阶段会被只读守卫判为设备资源变更而拒绝
     （`readonly_phase_violation` —「would mutate ... resources」），且拒绝一次即不可再试；
     写型直写只属 execute 阶段的效果自证。本条判据取**读路径**（`of=/dev/null` 不落设备、
     `iflag=direct` 绕过页缓存直读），一条命令同时满足 execute 与 verify 两阶段的判据需求，
     无需记 deviation；若某次执行确曾以写型自证、事后按读路径等价替代，按 verifier 契约
     「等价观测允许，记 deviation 并说明为何等价」记 `passed —（deviation: …）`，程序侧不降级。
2. 查看应用日志，确认出现 `Input/output error` 或 errno 5 相关错误
3. 确认应用数据写入请求失败率上升
4. 查看 Pod Events：`kubectl get events -n <namespace> --field-selector involvedObject.name=<pod-name>`

## 注入恢复
1. 等待 `<recovery-seconds>` 到期后 systemd 定时器自动移除 error 映射（journalctl 可见
   `<unit> Started … dmsetup remove` + `Succeeded` 记录）——主恢复路径，自恢复不依赖 Agent
   在线（Agent 提交结论即收尾，#43 立法）；需提前收尾或定时器丢失时，恢复移交带外
   `blade-ai recover --task-id`（幂等——映射移除后再执行报 not found 无副作用，定时器迟到重复执行无
   副作用；systemd timer 到期前无法可靠撤销，不设 pidfile、不做 kill）：
   ```bash
   kubectl exec <node-debug-pod> -n <debug-namespace> -- chroot /host dmsetup remove <dm-name>
   ```
   - 带外恢复经同一 exec 通道派发；若载体已退出，在同一节点重建载体（同步骤 3）再执行——恢复命令幂等且不依赖原载体（定时器 fire 更是完全不依赖载体存活）
2. 若使用替代方案（pod-disk burn），销毁 blade 实验：`blade destroy <experiment_uid>`
3. 若应用未自动恢复，可重启 Pod 清除残留影响

## 恢复验证
1. 确认 error 映射已移除——**主判据为设备节点消失**（在节点 debug Pod 内执行）：
   ```bash
   kubectl exec <node-debug-pod> -n <debug-namespace> -- chroot /host \
     ls -l /dev/mapper/<dm-name>
   ```
   应报 `No such file or directory`（符号链接已消失，退出码非 0）；辅助确认映射表整洁：
   `dmsetup ls` 的输出不含 `<dm-name>`（列表为空，或只剩其他本就不相关的映射）
2. 在 Pod 内重新写入文件，确认成功无报错（注入期间原挂载读写不受影响——纯 error 设备
   爆炸半径仅限 dm 设备本身；此步验证恢复后基线仍正常；若容器无 `dd`，可用其他写操作替代）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- dd if=/dev/zero of=<目录>/test bs=1M count=1
   ```
3. 查看应用日志，确认 IO error 不再出现
4. 确认应用数据写入功能恢复正常，错误率回落基线

## 基准事实
- **根因**：文件系统 IO 操作返回错误码（EIO），模拟磁盘硬件故障或文件系统损坏场景，导致应用读写操作失败
- **必现现象**：Pod 内文件写入返回 Input/output error；应用日志出现 errno 5 相关错误；数据写入请求失败率上升
- **方案说明**：此为 kubectl-native 方案（选用前提：无 pod-IO target，以 `--help` 探测为准）。精确 IO 错误注入需要节点 debug Pod + dmsetup；非特权环境可使用 `blade create k8s pod-disk burn` 作为近似替代（效果为 IO 饱和而非精确 errno）。阿里云 ECS 环境：对挂载中云盘创建含 linear 段的映射一律 EBUSY，可行形态为纯 error 设备——应用需另行挂载该设备才有感知，否则故障仅表现为对 `/dev/mapper/<dm-name>` 的 direct IO 报 EIO
