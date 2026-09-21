---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2/ND9）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 PV mountOptions 至基线），
# 但靶为 PersistentVolume（cluster-scoped）——装配器 M1 四对象栈仅支持
# namespaced 靶（Role/RoleBinding 无法授权 cluster-scoped 资源，工具对 PV 靶
# fail-closed 拒绝），本 case 无装配器主路径；恢复走正文降级 SOP（恢复载体
# 标准件 cluster-only 四对象变体：SA/载体 Pod + ClusterRole/ClusterRoleBinding
# ——恢复动词是集群级 PV patch，无 namespaced Role/RoleBinding 可授），属
# recovery-carrier spec「降级路径」回落 agent-online 恢复的结构性形态，恢复
# 闭环不缺失。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（mountOptions 写靶 PV 的 spec 属靶域内，名字匹配放行）+ 删受害 Pod，
# 无跨对象写条目；降级 SOP 的 cluster-only 载体栈（SA/Pod 建在靶点 ns，
# ClusterRole/ClusterRoleBinding 集群级）走 workload 网 secondary_scopes 白名
# 单放行（正文资源准备第 6 条），无立法条目。
---

**用例名称** 无效挂载选项注入 导致 Pod_ContainerCreating

**恢复动作配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 PV mountOptions 至基线。**靶为 PersistentVolume（cluster-scoped），装配器 M1 不支持**（四对象栈 Role/RoleBinding 无法授权 cluster-scoped 资源，工具对 PV 靶 fail-closed 拒绝）——本配方不经装配器，作为正文降级 SOP（恢复载体标准件 cluster-only 四对象变体定时器）与 Agent 主动兜底/恢复验证的权威动作清单：

```yaml
targetRef:                                # 靶（cluster-scoped，无 namespace）
  kind: PersistentVolume
  name: <pv-name>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
# 基线空 → add 整键；基线非空 → add /spec/mountOptions/- 追加（不覆盖既有选项）
- op: add
  path: /spec/mountOptions
  value: [chaos-invalid-mntopt]
restorePatches:                           # 恢复域：cluster-only 载体 timer 到点执行；Agent 死亡后 recover 重放同源
# 基线空 → remove 整键；基线非空 → replace <基线选项数组>（如 [nolock, noatime]）
- op: remove
  path: /spec/mountOptions
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- cluster-scoped 目标（PersistentVolume）：targetRef 不写 namespace；恢复不经装配器（见配方标题），降级 SOP 的载体 Pod/SA 建在被批准的靶点命名空间、ClusterRole/ClusterRoleBinding 为集群级（workload 网 secondary_scopes 白名单放行，资源准备第 6 条）。
- 恢复后 kubelet 下一轮 mount 重试（分钟级）自动成功、Pod 原地转 Running——无需删 Pod（正文立法）；删除目标 Pod 触发重建保留为 execute 计划普通步骤。
- 恢复由降级 SOP 载体 timer 承载（restorePatches 单动作 json-patch，与注入同 patch 类型，无 merge-patch 数组语义坑）：timer 到期自动恢复为主，`blade-ai recover` 从任务台账幂等兜底（迟到重放无副作用）；不再由 LLM 另建 namespaced recovery carrier timer（恢复语义单一来源）。

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `FailedMount`，mount 命令报 `bad option` / `wrong fs type, bad option, bad superblock`
3. mount 命令参数中可见非预期的挂载选项（如 `chaos-invalid-mntopt`）

**机制特点**：
本用例通过无效挂载选项在 mount 阶段阻断 Pod 启动：不创建任何占用者 Pod，不触发 Multi-Attach 竞争，故障根因直接落在 PV 挂载参数上。注意本用例同样需要删除目标 Pod 触发重建（见资源准备第 4 条），因此删除动作伴随的环境反应（如级联清理策略）同样须在资源准备阶段评估。

**资源准备**：
1. 确认目标 Pod 挂载了云盘类型 PVC（accessMode ReadWriteOnce），定位其 PV：`kubectl get pod <pod> -n <ns> -o jsonpath='{.spec.volumes[*].persistentVolumeClaim.claimName}'`，再查 PV 名
2. **PV 当前 mountOptions 基线取证（必做）**：`kubectl get pv <pv> -o jsonpath='{.spec.mountOptions}'`，记录原值（可能为空）。恢复时必须还原为此基线，而不是无条件置空；该基线同时决定注入形态（空 → add 新键 / 非空 → 追加）与定时恢复载荷形态（空 → remove / 非空 → replace 基线数组）
3. **PV 生命周期风险核查（必做）**：确认该 PV 的 `persistentVolumeReclaimPolicy`，并确认没有外部控制器会周期性刷新 PV spec（若管控面会覆写 mountOptions，注入会被自动修复或产生竞争，需重新评估）。PV 是集群级资源，修改影响面大于命名空间内资源，确认它只被目标 PVC 绑定（RWO 一对一）
4. 确认删除目标 Pod 后控制器（StatefulSet/Deployment）会自动重建——重建是触发重新 mount 的必要条件
5. **演练靶资产（自建，无既有靶时的标准形态）**：`drill-mntopt-target` Deployment（default ns、`app=drill-mntopt-target`、单副本、挂载 PVC `drill-mntopt-pvc`）+ 该 PVC（RWO、20Gi、`storageClassName: alicloud-disk-topology-alltype`）。镜像沿用常驻靶统一 VPC 镜像（节点缓存可靠）。**SC 选型约束**：`alicloud-disk-efficiency` 在部分可用区（如 cn-shanghai-cloudspe-a）不受支持（`cloud_efficiency: not supported in zone`，Immediate binding 下 provision 直接失败）——选 `topology-alltype`（WaitForFirstConsumer：Pod 先调度定 zone 再按可用性 essd/ssd/efficiency 选型，约 50s 内 Bound）。资产确认：PVC Bound + Pod Running + 记录动态 PV 名与 mountOptions 基线（空）。**严禁触碰集群既有 PV**（多为生产数据盘）
6. **守卫通道核对（scope 判型）**：本用例写入集 = 删受害 Pod + patch PV。scope=pod 时 PV 不在 OWNER_SCOPES 也不在 pod 网（secondary_scopes 空）→ PV patch 必被 REJECT_DRIFT；**意图须按目标 Deployment 叙述使 scope 判为 workload**（workload 网含 pv/persistentvolume，[freeze.py secondary_scopes]），PV 在 CLUSTER_SCOPED_KINDS 豁免 ns 检查后放行。执行身份须有 `patch persistentvolumes` 权限（`kubectl auth can-i patch persistentvolumes` 预检）。若意图仍判 pod，PV patch 被拒时按 manifest-missing 指引走 drill-loop 归档补法，勿重试。自恢复载体栈的 clusterrole/clusterrolebinding 创建同被 workload 网放行（secondary_scopes 白名单含之，见恢复载体标准件五件套变体注），执行身份须另过 `kubectl auth can-i create clusterrole` / `create clusterrolebinding` / `patch clusterrole` 预检（patch 是两步建栈第二步追加自删规则所需——create 有而 patch 无则自删规则追加不上、timer 自删 DELETE 必 403）

**演练步骤**（本 case 无装配器主路径——靶 PersistentVolume 是 cluster-scoped，装配器 M1 fail-closed 拒绝（见恢复动作配方标题），正文降级 SOP 即主路径：步骤 1 自建 cluster-only 载体栈+验权 → 步骤 2 先武装 timer 再注入（倒计时从武装时刻起算，与注入紧邻 ≤60s）→ 步骤 3 patch PV → 步骤 4 删目标 Pod 触发控制器重建）：
1. **自建定时恢复载体栈并验权（先于注入）**：本用例恢复动作唯一且为集群级写（PV 的 mountOptions patch），无任何 namespaced 恢复动作——按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）的 cluster-only 四对象变体（五件套变体去掉无可授 verb 的 namespaced Role/RoleBinding）：载体 Pod + 同名 SA + 同名 `ClusterRole`（`--resource=persistentvolumes --verb=get,patch` 最小授权面；**严禁对 persistentvolumes 授 delete verb**：靶 PV reclaimPolicy=Delete，误删级联 PVC/PV 属不可逆破坏；启用自断授权尾步（步骤 2 模板已含）时**严禁把自删 flag 合并进 create 命令**——按标准件第二节两步建栈法，create 后 json-patch 追加独立自删规则：`kubectl patch clusterrole drill-rc-<hash> --type=json -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":["rbac.authorization.k8s.io"],"resources":["clusterrolebindings"],"resourceNames":["drill-rc-<hash>"],"verbs":["delete"]}}]'`——独立规则 resourceNames 锁名后只能删自己这根 Binding，对象域与靶 PV 不交叠、不在上述禁令之列）+ 同名 `ClusterRoleBinding`，全部建在被批准的靶点命名空间（载体 Pod 五条件、镜像按标准件第九节判别法选节点缓存可达镜像、骨架时长 = 故障窗口 + 1800s 取证缓冲（2026-09-17 立法升级：#46/#48/#51 三现 restore.log 不可回读——600s 不够 fire 后带外取证节奏，1800s 覆盖「发现异常→诊断链→取证」全程）、预检撞到同名遗留按标准件拆旧建新不得复用）。守卫已立法放行 clusterrole/clusterrolebinding（workload 网 secondary_scopes 白名单含之）；执行身份预检 `kubectl auth can-i create clusterrole` / `create clusterrolebinding` / `patch clusterrole`（资源准备第 6 条），建栈后按标准件第四节验权读回双规则判据目检（主授权规则无锁名 + 自删规则独立成条），再过标准件第三节 SA 真实 token 验权（载体内 GET 目标 PV → 200 硬门，403 即中止，禁 `can-i --as` 假放行；第三节含写动词 SSAR 对账——verb 清单按钦定恢复形态推导，换形态时以恢复脚本实际载荷动词为准重建，见第二节形态无关总则）
2. **先武装定时恢复，再注入**（倒计时从武装时刻起算、与注入紧邻 ≤60s）。`<duration>` 必须覆盖武装后的注入、删 Pod 重建调度（RWO 跨节点 detach 重调度可达数分钟级）、ContainerCreating 观察与验证判据采齐全程——宁宽勿窄，提前 fire 会销毁验证主证据（见 SKILL.md 安全红线「故障窗口完整」）。恢复是单动作 json-patch（与注入同 patch 类型，无 merge-patch 数组语义坑），基线为空用 remove、非空用 replace 代入基线数组（基线由资源准备第 2 条取证决定）：
   ```bash
   # 基线为空（remove）——基线非空改 replace + <基线选项JSON数组>
   # 自断授权尾步（标准件第四节，#36 inject-dfee9d3d 立法）：主恢复 curl 升 -sf 并 && 链自删自己的
   # ClusterRoleBinding——主恢复未确认成功则授权保留（fail-open）；字节挤不下就不带（授权清退交收车路径）
   kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); curl -sf -X PATCH --cacert $C -H "Authorization: Bearer $T" -H "Content-Type: application/json-patch+json" -d "[{\"op\":\"remove\",\"path\":\"/spec/mountOptions\"}]" https://kubernetes.default.svc/api/v1/persistentvolumes/<pv> && curl -sf -X DELETE --cacert $C -H "Authorization: Bearer $T" https://kubernetes.default.svc/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/drill-rc-<hash> ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （`>/tmp/restore.log` 留 timer fire 直接取证证据；载体 Pod 单副本 `--restart=Never`，武装后定位不到定时器进程即视为异常，中止本次演练改人工恢复；武装后发生任何修复须先停旧定时器再全额重武装：`kubectl exec drill-rc-<hash> -n <namespace> -- sh -c 'pkill -f persistentvolume[s]; true'`）
3. 向 PV 注入无效挂载选项（追加，不覆盖既有选项）：
   ```
   kubectl patch pv <pv> --type json -p '[{"op":"add","path":"/spec/mountOptions","value":["chaos-invalid-mntopt"]}]'
   ```
   若 PV 已有 mountOptions，改用 `add` 到 `/spec/mountOptions/-` 追加。注入选项命名使用 `chaos-` 前缀标识，便于恢复审计。还原由步骤 2 的定时器到期自动承载、恢复段幂等兜底（见注入恢复），**严禁编入 execute 计划的收尾步骤**（见 SKILL.md 安全红线「拆线不进执行计划」）；**注入验证完成前不得执行还原**——提前还原会使故障消失、验证失去主证据，早于验证的任何修复动作都须先还原基线再重新注入（见 SKILL.md 安全红线「故障窗口完整」）
4. 删除目标 Pod 触发控制器重建：`kubectl delete pod <pod> -n <ns>`（用普通删除即可，本机制不依赖强制删除）
5. 新 Pod 调度后 kubelet 执行 mount 时携带无效选项失败，进入 ContainerCreating 并周期性重试（默认分钟级，可通过 Events 观察重试计数）

**机制反证条件（命中即停）**：
删除目标 Pod 后，若观察到以下任一现象，说明机制不可达，**立即停止一切尝试，上报偏离并转入恢复**（中止路径的恢复二选一：留 timer 到点自然 fire（还原幂等），或先停旧定时器再人工立即还原——两条等价取其一，不得并行）：
1. PV 或 PVC 进入 Terminating 状态或被删除（存在级联删除/清理策略，继续操作会扩大破坏）
2. 新 Pod 未进入 ContainerCreating 而是直接 Running（说明 mount 未经过该 PV 或选项被旁路，注入无效）
3. mount 报错与注入选项无关（如 attach 失败、磁盘损坏），说明命中的是其他故障，须按真实故障处理

**注入验证**：
1. `kubectl get pods -n <ns>`：目标新 Pod 状态为 ContainerCreating
2. `kubectl describe pod <pod> -n <ns>`：Events 显示 `FailedMount`，报错信息包含注入的选项名（机制归因——症状相同但由其他原因产生的不算注入成功）
3. `kubectl get pv <pv> -o jsonpath='{.spec.mountOptions}'`：确认注入选项仍在 PV 上（故障持续的原因）

**注入恢复**（自治承载 = 降级 SOP 的 cluster-only 载体 timer（本 case 即主路径，恢复动作唯一且为集群级 PV patch，无装配器形态）——timer 到点对 PV 执行 restorePatches 单动作 json-patch，fire 证据落载体 /tmp/restore.log；`blade-ai recover` 从任务台账幂等兜底（迟到重放无副作用）。以下序列为该 SOP 的执行与收尾形态）：
1. **timer 到期自动恢复（主路径）**：步骤 2 武装的定时器 fire，对 PV 执行同一 json-patch 还原 mountOptions 至基线；fire 后 kubelet 下一轮 mount 重试（分钟级）自动成功，Pod 原地转为 Running——**无需删除或重建 Pod**，恢复动作只有一个 patch。timer fire 直接证据：带外 `kubectl exec drill-rc-<hash> -n <ns> -- cat /tmp/restore.log`
2. **`blade-ai recover` 幂等兜底（演练结束/任务收尾时执行）**：恢复命令与 timer 同一还原语义（按资源准备第 2 条记录的基线二选一，幂等，重复执行无副作用；remove on absent path 的报错 = 已恢复的证据形态）：
   ```bash
   # 基线为空时
   kubectl patch pv <pv> --type json -p '[{"op":"remove","path":"/spec/mountOptions"}]'
   # 基线非空时（<基线选项JSON数组> 为注入前记录的原值，如 ["nolock","noatime"]）
   kubectl patch pv <pv> --type json -p '[{"op":"replace","path":"/spec/mountOptions","value":<基线选项JSON数组>}]'
   ```
3. 演练结束后全清载体栈（cluster-only 变体 = **四连删除**，均 `--ignore-not-found`；两个集群级对象不带 `-n`）并带外核实零残留（步骤 2 尾步若已删 Binding：`--ignore-not-found` 静默跳过、幂等接力；此时残留 ClusterRole/SA 为零授权空壳中间态——四连删除收壳即终态，见标准件第六节）：
   ```bash
   kubectl delete pod drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete sa drill-rc-<hash> -n <namespace> --ignore-not-found
   kubectl delete clusterrolebinding drill-rc-<hash> --ignore-not-found
   kubectl delete clusterrole drill-rc-<hash> --ignore-not-found
   ```

> ⚠️ 本注入的自恢复由载体定时器承载（先武装再注入，timer 为主路径），`blade-ai recover` 是幂等兜底而非唯一恢复手段，两者重复执行无副作用；timer 未按期 fire（载体异常/进程丢失）时中止演练改人工恢复。恢复命令为单条幂等 patch，满足 30 秒可回滚红线

**恢复验证**：
1. `kubectl get pod <pod> -n <ns>`：状态恢复 Running，READY 1/1，且 Pod 对象未变（AGE 与注入前一致，证明原地恢复而非重建）——timer fire 后 kubelet 分钟级重试滞后属预期形态，判读窗口须容纳该滞后
2. `kubectl get pv <pv> -o jsonpath='{.spec.mountOptions}'`：与注入前基线完全一致
3. Events 中不再出现新的 FailedMount
4. timer fire 直接证据链：带外 `kubectl exec drill-rc-<hash> -n <ns> -- cat /tmp/restore.log`（curl 回执在案；remove on absent path 的 422 = 已恢复的幂等证据）
5. 载体栈四连删除后零残留核实：靶点命名空间无 drill-rc-* 残留 + clusterrole/clusterrolebinding 零残留

**基准事实**：
- **根因**：PV mountOptions 中的非法选项使 mount 命令失败，kubelet 在 volume mount 阶段阻塞并周期性重试
- **必现现象**：Pod ContainerCreating；Events 显示 FailedMount 且报错包含注入选项名；PV mountOptions 含注入标记
- **恢复特征**：timer 到期自动还原（recover 幂等兜底，重复执行无副作用）后 kubelet 自动重试成功，零爆炸半径
