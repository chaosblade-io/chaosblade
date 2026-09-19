---
# 机制写入集立法（write-set approval contract）：本用例的故障机制需要写受害者
# 覆盖之外的对象——靶是 Deployment（应用 A），patch nodeSelector/nodeName 属受害者
# 自身域内（名字匹配放行），但注入要**创建**（恢复时删除）占位 Deployment
# chaos-ip-exhaust——跨对象写，须立法声明。由 case 作者在此声明，确定性代码在意图
# 定案时装载，确认卡渲染、人工批准后冻结进守卫快照。LLM 无权扩写。
mechanism_writes:
  # 注入创建 / 恢复删除：占位 Deployment（nodeName 直绑目标节点，副本数恰填满 IP 池）
  - scope: deployment
    namespace: default
    names: [chaos-ip-exhaust]
---

**用例名称** CNI分配失败 导致 Pod_ContainerCreating

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `failed to allocate for ENI` 或 `no available IP in subnet` 或 CNI 相关错误
3. 节点上的 IP 资源池耗尽或 ENI 数量达到上限

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群使用 ENI 或 vSwitch 分配 Pod IP 的 CNI 插件（如 Terway）
3. **余量核查（必做，命中则改道或停）**：对比目标节点 `status.allocatable.pods`（pod 容量 C）与
   `metadata.annotations.k8s.aliyun.com/max-available-ip`（IP 池总上限 P——静态规格值，由实例
   ENI×IP 数决定，与当前 Pod 数无关；例：ecs.r6.2xlarge→30、ecs.c7.2xlarge→45、
   ecs.g7.4xlarge→210，同规格节点同值）。可达性判据须计入 hostNetwork Pod（H）的错位项
   ——它们占 pod slot 但不占 IP 池：
   - **调度器路径可达条件：C − P ≥ H + 1**（IP 池填满时 Pod 总数 = H + P + 靶 1 个 ≤ C）。
     仅看 C − P 差值（如 53 vs 50 余量 3 即判死）不完整：hostNetwork 系统开销
     （kube-proxy/CNI DS/node-local-dns/node-exporter/csi-plugin 等，典型 ≥5）未被计入。
     ACK terway 标准集群 C − P = 3 恒定设计（同构节点池全节点同差值）+ H 典型 ≥6
     → 此类集群调度器路径全集群结构性不可达
     （批量 Pod 先撞 pods fit 呈 OutOfpods/Pending，调度失败的 Pod 不建 sandbox 不占 IP，
     IP 池永远填不满）。
   - **kubelet admit 同样执行 pods 容量检查（v1.31 源码，nodeName 直绑不豁免）**：
     predicateAdmitHandler → scheduler.AdmissionCheck → noderesources fitsRequest 的
     `len(Pods)+1 > allocatable.pods`（"Too many pods"，pkg/kubelet/lifecycle/predicate.go
     L63/L131 + noderesources/fit.go L426）；admit 失败的普通 Pod 直接拒绝（kubelet 抢占
     腾位仅对 Critical Pod 生效，criticalPodAdmissionHandler）。判据形态（靶 Pod 卡
     ContainerCreating + CNI 分配失败）要求「IP 池已满（非 hostNetwork Pod 数 W = P）且
     靶 admit 通过（H + W ≤ C − 1）」→ **统一可达条件 H ≤ C − P − 1（调度器路径与
     直绑路径同构，直绑只是绕开 nodeSelector/taint/亲和类约束的强制落点手段）**。
   - **H > C − P − 1 时判据形态结构性不可达**：直绑超量无增益（占位 Pod 先撞
     "Too many pods" 卡 Pending，无 sandbox 无 CNI 调用），且靶 Pod 自删自建恰好腾出
     slot 让自身 admit 通过（IP 池差 H 个永远不满）——此时终止演练并如实上报形态
     退化，禁止继续加压；机制立法保留供 C − P 余量充足集群复用
   - **记账层 vs 物理层二分法（替代手段判死总纲）**：
     ContainerCreating + CNI 分配失败 ⟸ **物理层池空**（池中 idle IP/ENI = 0 且云侧
     配额满）；记账层耗尽（annotation/扩展资源/condition 改小）⟹ 调度器或 kubelet
     记账拒绝 ⟹ Pod **Pending**（非判据形态）。逐手段判死依据：①patch
     `k8s.aliyun.com/max-available-ip` annotation——节点注册期写入的静态值
     （managedFields 可核 writer），无活跃 controller 看护时改后不被回写，但 terway
     池是 daemon 内存态，**annotation 无分配闸门 reader、仅元数据**，改之无效；
     ②patch node status `aliyun/member-eni` 扩展资源或 SufficientIP condition——
     由 terway-controlplane 活跃维护（周期回写），且记账改变不影响
     物理分配；③trunk member ENI 槽耗尽（独立物理账，cap 以节点实态为准）——需 Pod 匹配
     PodNetworking（namespace 标签，官方文档语义），PodNetworking/PodENI CRD 未安装的集群
     该路径不存在（以当次探测为准）；即便安装，记账刷新后新 Pod 走调度器/kubelet 记账拒绝呈
     Pending，物理失败仅存在于记账刷新时滞竞态（非稳定可依赖形态）；④杀 terway daemon——
     机制偷换（Events 为 CNI timeout 而非 IP 分配失败，属「CNI 插件异常」另一机制）；
     ⑤云侧（vSwitch 网段 IP 耗尽 / ECS ENI 配额）——带外云 API 非注入域，且
     vSwitch 为跨节点共享资源爆炸半径全集群级
4. **带内通道适配核查**：`execute_skill_script` 的脚本本地子进程跑 `kubectl --kubeconfig`，
   集群连接为代理通道形态（无本地 kubeconfig，如 kubewiz）时脚本路径不可达——改用 Agent
   带内 kubectl 工具复刻脚本序列（create deployment → patch 绑 nodeName+replicas 两步），
   载具登记行缺失的补偿 = 恢复动作显式编入意图（timer 载体兜底 + recover 主动删）

**演练步骤**：
1. 查看目标节点的 ENI 和 IP 分配情况，确认目标应用 Pod 所在节点（记为 `<目标节点>`）
2. **先武装定时恢复，再注入**（恢复命令幂等：定时器到期自动删除耗尽 Deployment、还原
   nodeSelector、移除节点标签；Agent 在演练结束时主动执行同一条命令兜底，定时器迟到重复执行无副作用。定时器 shell 逻辑必须作为 `kubectl exec` 载体载荷派发——直接以
   `sh -c '…'` 作为顶层命令派发会被命令守卫拦截（unknown_binary: sh）；载体 Pod 为
   多副本时无法可靠终止定时器，故不设 pidfile。恢复脚本落盘形态按 recovery-carrier.md 第七节
   「四档定案表」按明文字节数查表选定（Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符）；
   `<duration>` 需覆盖批量 Pod 创建与观察窗口）。
   载体 Pod 选集群内带 kubectl 且有足够 RBAC 权限的常驻 Pod（如演练工具 Pod）：
   ```bash
   # 下列三条恢复命令按第七节四档表选定落盘形态（Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符——
   #   下行命令为旧契约历史形态示例，勿套用。nodeSelector 按基线选定 remove 单 key 或 remove 整字段；label 移除为最后一步）：
   #   kubectl delete deployment chaos-ip-exhaust -n <namespace> --ignore-not-found=true
   #   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
   #     -p='[{"op":"remove","path":"/spec/template/spec/nodeSelector/net.ops~1ipam-audit"}]'
   #   kubectl label node <目标节点> net.ops/ipam-audit-
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-cni.sh; ( sleep <duration>; sh /tmp/blade-restore-cni.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-cn[i]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
3. 给目标节点添加标签，并给应用 A 的 Deployment 添加 nodeSelector，确保新 Pod 只能调度到目标节点（防止调度器规避耗尽节点）：
   ```bash
   kubectl label node <目标节点> net.ops/ipam-audit=true
   kubectl patch deployment <deployment-name> -n <namespace> --type='merge' \
     -p='{"spec":{"template":{"spec":{"nodeSelector":{"net.ops/ipam-audit":"true"}}}}}'
   ```
   等待 rollout 完成（Pod 仍在原节点运行，因为目标节点已有此标签）。
   记录原始 nodeSelector 值，恢复时还原（武装还原仅移除新增的 key，若原本还有其他 nodeSelector 不受影响）。
4. 使用 `execute_skill_script` 在目标节点批量创建 Pod 耗尽 IP/ENI 资源（**`kubectl create/apply` 不可用时必须使用脚本；脚本不可达时用带内 kubectl 复刻两步序列**）：
   ```
   execute_skill_script(
     skill_name="k8s-chaos-skills",
     script_name="inject_cni_exhaust.py",
     params="--namespace <namespace> --node <目标节点>"
   )
   ```
   脚本会创建 `chaos-ip-exhaust` Deployment 并绑定到目标节点，脚本输出中的 `[drill-vehicle: ...]` 登记行会被框架自动解析，将该 Deployment 注册为演练占位载具（恢复阶段与任务中途崩溃时的兜底清理都依赖此登记）。

   **变体：nodeName 直绑形态（脚本不可达集群的带内复刻 / 需强制落点场景）**：
   用带内 kubectl 两步复刻（create --replicas=0 → patch {replicas, template.nodeName}）。
   直绑语义与脚本一致（批量 Pod 全落目标节点，绕开 nodeSelector/taint/亲和类调度约束的
   强制落点），**但不豁免 kubelet 的 pods 容量检查**——前提 H ≤ C − P − 1（见资源准备 3），
   本变体仅是执行通道替代，不改变可达性判据。镜像选节点缓存镜像（如 terway；docker.io
   busybox 在受限网络集群 ImagePullBackOff——sandbox 先于镜像拉取放 IP 占用机制仍成立，但
   形态判读复杂化，钦定缓存镜像）。副本数计算：`replicas = P − W`（W = 节点上非
   hostNetwork Pod 数，含终态未 GC 尸体；从 `kubectl get pods -A -o wide` 的 IP 列区分
   ——节点 IP 即 hostNetwork），恰填满 IP 池。
5. 删除应用 A 在目标节点上的 Pod，触发重建。调度器路径下新 Pod 只能调度到已耗尽的目标节点，将进入 ContainerCreating 状态；**直绑形态下应用 A 的 Deployment template 须带 nodeName**（重建 Pod 直达 kubelet 撞 CNI 失败——否则走调度器撞 pods fit 呈 Pending/OutOfpods，非目标判据形态。前提 H ≤ C − P − 1 已满足，否则重建 Pod 因自删腾位 admit 通过而正常运行，故障形态不出现——见资源准备 3）
6. 观察新 Pod 的 ContainerCreating 状态

**注入验证**：
1. 执行 `kubectl get pods`，确认应用 A 新 Pod 状态为 ContainerCreating
2. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 CNI/IP 分配失败相关错误
3. 查看节点 ENI/IP 使用情况，确认资源已耗尽
4. **反证形态（命中即停）**：若批量 Pod 呈 `OutOfpods` 状态而非 Running 满载，说明先撞
   的是 pod 容量而非 IP 池（见资源准备第 3 条余量核查），本用例判据不可达，转入恢复并上报
   形态退化，禁止继续加压

**注入恢复**：
1. 等待 `<duration>` 到期后武装的定时器自动删除耗尽 Deployment、还原 nodeSelector、移除节点标签；如需提前恢复，Agent 直接执行下列第 2–4 步恢复命令（幂等，定时器迟到再执行一次无副作用；载体 Pod 为多副本时无法可靠终止容器内的定时器进程，不依赖 pidfile）
2. 删除批量创建的 Deployment：`kubectl delete deployment chaos-ip-exhaust -n <namespace>`（该 Deployment 已由脚本登记行注册为演练载具，此删除会被守卫豁免）
3. 移除应用 A 的 Deployment 上添加的 nodeSelector（还原为原始值，若原本无 nodeSelector 则移除整个 nodeSelector）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"remove","path":"/spec/template/spec/nodeSelector/net.ops~1ipam-audit"}]'
   ```
4. 移除目标节点上添加的标签：`kubectl label node <目标节点> net.ops/ipam-audit-`
5. 等待 IP/ENI 资源释放和 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认应用 A 的 Pod 状态恢复为 Running
2. 确认节点 IP/ENI 资源恢复可用
3. 确认应用 A 网络连通正常

**基准事实**：
- **根因**：节点可用 IP 池耗尽或 ENI 数量达到上限或 vSwitch IP 不足，CNI 插件无法为新 Pod 分配网络资源
- **必现现象**：Pod ContainerCreating；Events 显示 CNI/IP/ENI 分配失败；节点网络资源耗尽
