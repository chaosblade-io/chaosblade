---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 移除注入的卷挂载），路由进程序化
# 恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本用例的故障机制需要写受害者
# 覆盖之外的对象——靶是 StatefulSet（Pod 的 owner），patch spec.template 的
# volumes/volumeMounts 属受害者自身域内（名字匹配放行），但注入要**创建**（恢复
# 时删除）演练用 PVC——跨对象写，须立法声明。由 case 作者在此声明，确定性代码
# 在意图定案时装载，确认卡渲染、人工批准后冻结进守卫快照。LLM 无权扩写；装配器
# 载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由工具内程序化
# 构建——构造保证 + fail-closed 内嵌检查，不经 LLM kubectl 写面，无立法条目。
mechanism_writes:
  # 注入创建 / 恢复删除：演练用 PVC（引用不存在的 StorageClass，瞬态对象；
  # 名字与 Deployment 版用例的 app-data-claim 刻意区分，避免并行演练与审计混淆）
  - scope: persistentvolumeclaim
    namespace: default
    names: [sts-unbound-claim]
---

**用例名称** PVC未绑定（StatefulSet 靶） 导致 Pod_Pending

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 移除注入的卷挂载；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；通道仲裁预立法：faultdrill_assemble_carrier 即 apiserver-write 恢复通道的程序化实现定案——CR 通道已退役（通道横跳三测三撞三拒历史教训后退役），faultdrills CRD 在位/Established 不构成启用 CR 通道的理由，CR 通道仅当配方显式声明时使用；装配不可用时降级正文 SOP 形态——计划写作纪律：降级路径在计划中只落差异点（载体命名前缀/RBAC 动词集/恢复载荷体/镜像选型/落盘档位），四件套标准形态与武装序列不逐字抄录进计划——正文降级兜底段与 recovery-carrier.md 标准件是权威源；降级执行时按计划引用回读权威源、照差异点执行——标准件形态以权威源为准不自创；遇环境与预期不符时允许临场应变，应变连同依据如实记录）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: StatefulSet
  name: <sts-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
# 基线无卷 → add 整键；基线有卷 → add /…/volumes/- 追加
- op: add
  path: /spec/template/spec/volumes
  value: [{name: sts-unbound-vol, persistentVolumeClaim: {claimName: sts-unbound-claim}}]
- op: add
  path: /spec/template/spec/containers/0/volumeMounts
  value: [{name: sts-unbound-vol, mountPath: /var/lib/drill-unbound}]
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
# 基线空 → remove 整键；基线非空 → replace <基线数组>
- op: remove
  path: /spec/template/spec/volumes
- op: remove
  path: /spec/template/spec/containers/0/volumeMounts
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 道具 PVC 为 execute 计划 manifest 步骤（走 front matter mechanism_writes 立法条目）；收尾清理步删除。
- 删除卡住的错误 revision Pod（revision-hash 不等标签选择器——正文 OrderedReady 立法：控制器对 Pending 错误 revision Pod 零动作）保留为 execute 计划收尾步骤。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作（删卡住 Pod、删演练 PVC）保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. StatefulSet 最高序号 Pod 状态为 Pending，READY < DESIRED 持续不收敛
2. 新 Pod Events 中显示 `pod has unbound immediate PersistentVolumeClaims`
3. 演练用 PVC 状态为 Pending，无法绑定到 PV

**RCA症状**：
1. 最高序号 Pod Pending（滚动更新重建后新 Pod 不被调度）
2. Events 显示 unbound PersistentVolumeClaims；演练用 PVC Pending
（以上为 kubectl 直接可观测的现象，不包含诊断结论）

**资源准备**：
1. 确认应用 A（StatefulSet 管辖）已正常运行，全部副本 Ready
2. 确认集群中 StorageClass 和 CSI 插件正常工作；确认本用例引用的 StorageClass
   `ssd-retain-zone-c` 在集群中**不存在**（若已存在则另选一个不存在的名字并全文
   替换——PVC 必然无法绑定是本用例的注入前提）
3. ⚠️ **靶形态硬性要求**：靶必须是 **StatefulSet 管辖的应用**（意图以 StatefulSet
   为靶——机制 patch 的是 statefulset 的 `spec.template`）。确认方式：`kubectl
   get pod <pod> -n <ns> -o jsonpath='{.metadata.ownerReferences[0].kind}'`
   输出 `StatefulSet` 才可用。owner 是 Deployment 的靶走
   `Pod_Pending_PVC未绑定.md`（Deployment 版，机制同构、patch 对象不同）
4. **RBAC 前置检查**（决定可行性）：用当前执行凭证跑 `kubectl auth can-i patch
   statefulsets -n <ns>` / `create persistentvolumeclaims -n <ns>` /
   `delete persistentvolumeclaims -n <ns>`，三权皆 yes 即可行。受阻时
   **严禁降级改机制**（如删除/篡改 STS 原生 PVC、抢占 `<claim>-<pod>` 名、
   删 Pod 重建等破坏性替代）
5. **写入集契约锚定**：mechanism_writes 立法锚定 **default 命名空间**（与
   Deployment 版用例同约定）——非 default 命名空间的靶不在本用例契约内，如实
   告知能力边界，不得即兴越域
6. **卷名/挂载路径查重**（strategic merge 按名合并的改写风险）：注入卷名
   `sts-unbound-vol` 与挂载路径 `/var/lib/drill-unbound` 不得与靶 Pod 模板基线
   中现有卷名/挂载路径重名——重名时 strategic merge 会**改写**现有卷定义而非
   追加。查重：读基线 volumes/volumeMounts 数组确认无重名；有冲突则换名并全文
   替换
7. **爆炸半径预读**：机制使**最高序号**副本进入 Pending（其余序号保持旧版本
   继续运行）；quorum 类应用（如 3 副本）窗口内降级为 N-1 存活——单副本 STS
   窗口内服务完全不可用。机制**不创建、不删除、不篡改 STS 原生 PVC**
   （volumeClaimTemplates 派生的数据卷零触碰），对齐 SKILL.md「无备份对
   StatefulSet 做破坏性实验」红线（本用例非破坏性）

**演练步骤**（主路径 = 基线捕获（步骤 1）→ 创建道具 PVC（步骤 2，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=StatefulSet、patches=模板卷挂载注入、restorePatches=模板还原、duration_seconds=<duration>），模板注入+武装+readback 工具内同步完成；步骤 3-4 的手动序列仅当装配器 fail-closed 报告不可用时作降级兜底。updateStrategy 为 `OnDelete` 时主路径同样须在装配器注入后手动删除最高序号 Pod 触发重建，见步骤 1 注）：
1. **基线捕获**（恢复对照基准；restorePatches 的基线值来源，两条路径共用。Agent 读取输出并记录原始 JSON。updateStrategy
   输出为空即默认 `RollingUpdate`；`rollingUpdate.partition` 非零时仅序号 ≥
   partition 的副本参与更新，最高序号仍会更新、故障照常成立，但需在方案中知悉）：
   ```bash
   kubectl get statefulset <sts-name> -n <namespace> \
     -o jsonpath='{.spec.replicas} {.spec.updateStrategy.type}{"\n"}'
   kubectl get statefulset <sts-name> -n <namespace> \
     -o jsonpath='{.status.currentRevision} {.status.updateRevision}{"\n"}'
   kubectl get statefulset <sts-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.volumes}'
   kubectl get statefulset <sts-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].volumeMounts}'
   ```
   （同时记录第一容器名——注入 patch 按容器名定位。updateStrategy 为 `OnDelete`
   时模板变更不触发滚动，注入步骤 4 的 patch 后须**手动删除最高序号 Pod** 触发
   重建；`RollingUpdate`（默认）不需要）
2. 使用 `kubectl apply -f` 创建一个引用不存在的 StorageClass 的 PVC（通过
   `stdin_data` 传入 YAML）：
   ```yaml
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata:
     name: sts-unbound-claim
     namespace: <namespace>
   spec:
     accessModes: ["ReadWriteOnce"]
     storageClassName: "ssd-retain-zone-c"
     resources:
       requests:
         storage: 10Gi
   ```
3. **先武装定时自恢复，再注入**（恢复命令幂等：定时器自动执行，迟到重复执行
   无副作用。恢复 = ①模板 volumes/volumeMounts 按基线还原 + ②删除卡住的错误
   revision Pod + ③删除演练 PVC——**②是 STS 用例的必要动作，不可省略**：OrderedReady
   更新策略下控制器只删除/重建 Running 且 Ready 的错误 revision Pod，对卡在
   Pending（Unschedulable）的错误 revision Pod **零动作**（K8s 官方语义：控制面等
   待更新后的 Pod Running 且 Ready 才推进前序序号；实测恢复后 12 分钟零 delete
   事件，手动删除后 45 秒收敛）。删除用 revision-hash 不等标签选择器限定卡住 Pod：
   `-l '<pod-label>,controller-revision-hash!=<基线revision名>'`——注入期 pod-0
   始终停在基线 revision（OrderedReady 从高序号推进，卡在最高序号时低序号未动），
   迟到重执行时健康 Pod 的 hash 已等于基线，选择器匹配空集，零副作用幂等。
   **恢复顺序即安全顺序：先模板、次删卡住 Pod、后删 PVC**——模板还原使重建 Pod 不再引用
   演练 PVC；Pending Pod 从未挂载过该 PVC（pvc-protection finalizer 不阻删，实测
   PVC 在 Pending Pod 引用期间删除成功）。**武装点定案：步骤 1-2 均为 setup，武装
   推迟至步骤 4 模板 patch 紧邻前执行**——机械照抄步骤位置会让 setup 时间侵蚀
   故障窗口）。定时器宿主形态二选一：
   - 集群内有 kubectl 且具备恢复 RBAC 的常驻工具 Pod（宿主要求见 SKILL.md 安全
     红线；宿主凭证与当前执行凭证一致时可直接复用资源准备第 4 条检查结果）。恢复
     三动作（幂等顺序执行）：
     ```bash
     kubectl exec <宿主Pod> -n <宿主ns> -- sh -c '( sleep <duration>; \
       kubectl patch statefulset <sts-name> -n <namespace> --type=json \
       -p=<基线还原 json patch，与注入恢复第 1 条同一条命令>; \
       kubectl delete pod -n <namespace> -l "<pod-label>,controller-revision-hash!=<基线revision名>" --ignore-not-found; \
       kubectl delete pvc sts-unbound-claim -n <namespace> --ignore-not-found \
       ) >/tmp/restore.log 2>&1 & echo armed'
     ```
   - 无可用外部宿主时按**恢复载体标准件**（`references/carrier/recovery-carrier.md`）
     自建载体栈（busybox/curl 镜像、Pod/SA/Role/RoleBinding 四对象同名、同靶点
     命名空间）。Role 覆盖三个恢复动作（含卡住 Pod 删除；verb 清单按钦定恢复形态推导，
     换恢复形态时以恢复脚本实际载荷动词为准重建——见标准件第二节形态无关总则、第三节写动词 SSAR 对账）：
     ```bash
     # 两步法（多 flag 单命令 verbs 并集广播，三族全部 rule 均携带
     # [get,patch,delete,list] 超授权面——见标准件第一节立法）
     kubectl create role drill-rc-<hash> -n <namespace> --verb=get,patch --resource=statefulsets
     # 载体 Pod 注册后，单条 json-patch 追加两族（实测一次成型、三 rule 零交叉）：
     kubectl patch role drill-rc-<hash> -n <namespace> --type=json \
       -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["persistentvolumeclaims"],"verbs":["get","delete"]}},{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["pods"],"verbs":["get","list","delete"]}}]'
     ```
     载体内无 kubectl，武装形态用 SA token 直调 apiserver REST——模板还原是
     json patch（Content-Type: application/json-patch+json），volumes 与
     volumeMounts 两 op 合一个文档；基线为空的字段对应 op 用 remove（对不存在
     路径仅报错不误删）；卡住 Pod 删除 = GET pods（labelSelector 含
     `controller-revision-hash!=<基线>`）后逐个 DELETE。Pod 名提取管道是实测
     踩坑点：apiserver 回 pretty-printed JSON（`"name": "xxx"` 冒号后带空格），
     `grep '"name"' | cut -d\" -f4` 类管道实测返回**零个名字**（步骤②静默
     no-op，恢复必失败）；实证可用的形态是 token 化后整行精确匹配（见下方
     代码块）；勿用 `grep -o`：会从 revision hash（形如
     `<sts-name>-5f5785...`）里截出 `<sts-name>-5` 假阳性。**恢复脚本写入后、
     武装前，必须对提取管道做一次只读实证**：GET 本靶全部 Pod（不带 hash 过滤）
     + 本地跑同一管道，返回恰为真实 Pod 名集合才可武装；不一致则改管道，
     不得带病武装。PVC 删除单次 DELETE。恢复动作**先模板、次卡住 Pod、后
     PVC**（顺序依据见上）；两种顺序均收敛：
     ```bash
     kubectl exec drill-rc-<hash> -n <namespace> -- sh -c '( sleep <duration>; \
       C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; \
       T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); \
       S=https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/statefulsets/<sts-name>; \
       curl -s -X PATCH --cacert $C -H "Authorization: Bearer $T" \
       -H "Content-Type: application/json-patch+json" \
       -d "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/volumes\",\"value\":<基线volumes JSON>},{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/<idx>/volumeMounts\",\"value\":<基线volumeMounts JSON>}]" $S; \
       P=$(curl -s --cacert $C -H "Authorization: Bearer $T" \
       "https://kubernetes.default.svc/api/v1/namespaces/<namespace>/pods?labelSelector=<pod-label>%2Ccontroller-revision-hash%21%3D<基线revision名>"); \
       for pn in $(echo "$P" | tr -c "a-zA-Z0-9._-" "\n" | grep -x "<sts-name>-[0-9][0-9]*" | sort -u); do \
         curl -s -X DELETE --cacert $C -H "Authorization: Bearer $T" \
         https://kubernetes.default.svc/api/v1/namespaces/<namespace>/pods/$pn; done; \
       curl -s -X DELETE --cacert $C -H "Authorization: Bearer $T" \
       https://kubernetes.default.svc/api/v1/namespaces/<namespace>/persistentvolumeclaims/sts-unbound-claim \
       ) >/tmp/restore.log 2>&1 & echo armed'
     ```
     （自断授权尾步——标准件第四节立法：**最后一个**恢复 curl（PVC DELETE）升 `curl -sf` 并以 `&&` 链自删 DELETE 自己的 Binding（复用 `$C`/`$T` 变量；本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`；前序动作保持 `;`/`for` 结构不中断），建栈 Role 后按标准件第二节两步建栈法 json-patch 追加独立自删规则（**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行）。fail-open：主恢复未确认成功则授权保留；字节挤不下就不带——完整形态与五纪律以标准件第四节为准）
     （基线数组代入后的载荷字节压力按标准件第七节处置链校验；标准件建不起来时
     按标准件第八节降级——任务如实失败收尾，不得注入）
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复
   须先 `pkill -f sts-unbound-clai[m]`（两种宿主形态的定时器载荷均含该 PVC 名）
   停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
4. 注入：`kubectl patch` 修改 StatefulSet 的 Pod 模板，追加引用该 PVC 的 volume
   和 volumeMount（strategic merge 按卷名/挂载路径合并，基线数组原样保留，纯
   追加）。**注意**：`spec.volumeClaimTemplates` 是不可变字段（API 直接拒绝
   patch），本机制不触碰它——template.volumes 外部卷引用与 STS 原生 PVC
   （volumeClaimTemplates）是两条正交通路：
   ```bash
   kubectl patch statefulset <sts-name> -n <namespace> \
     -p='{"spec":{"template":{"spec":{"volumes":[{"name":"sts-unbound-vol","persistentVolumeClaim":{"claimName":"sts-unbound-claim"}}],"containers":[{"name":"<container-name>","volumeMounts":[{"name":"sts-unbound-vol","mountPath":"/var/lib/drill-unbound"}]}]}}}}'
   ```
5. 等待滚动更新推进到最高序号：STS 控制器按序号从高到低滚动（默认每批 1 个），
   最高序号 Pod 被删除重建后因 PVC 未绑定而 Pending，OrderedReady 门槛使更新
   **停在该序号**——其余序号保持旧版本继续运行（这是本用例的爆炸半径边界，
   不是异常）
6. 观察新 Pod 的状态

**注入验证**：
1. 确认滚动停在高序号：**用 revision/Pod 状态判据，不要用 `kubectl rollout
   status`**——注入期新 Pod 永不被调度（PVC 未绑定，故障本身），`rollout
   status` 等待必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」。
   正确判据：`kubectl get statefulset <sts-name> -n <namespace>` READY <
   DESIRED 且 `currentRevision != updateRevision` 持续不收敛
2. `kubectl get pods -n <namespace> -l <pod-label>`：最高序号 Pod 为 Pending
   （AGE 较新），其余序号 Pod 仍 Running（旧 revision，未被动过）
3. `kubectl get pvc`，确认演练 PVC 状态为 Pending
4. Events 显示 unbound PVC 调度失败（事件时间戳形态注：部分集群 events 呈新式
   字段形态——lastTimestamp 为 null、count 为空，属 API 字段差异**非数据缺失**；
   判读以 eventTime 与 series.lastObservedTime 为准）

取证组织（单批发起，判据不变）：上述判据的证据读取互不依赖，可同批一轮发起——
STS/Pod 清单走标签选择器、PVC 走计划内已知名直读、事件流一次覆盖调度与供给两类
证据：
```bash
kubectl get statefulset <sts-name> -n <namespace>
kubectl get pods -n <namespace> -l <pod-label> -o wide
kubectl get pvc sts-unbound-claim -n <namespace> -o jsonpath='{.status.phase} {.spec.storageClassName}{"\n"}'
kubectl get events -n <namespace> --sort-by=.lastTimestamp | grep -E 'FailedScheduling|unbound|not found'
```
（事件流同时承载新 Pod 的 `FailedScheduling — pod has unbound immediate
PersistentVolumeClaims` 与 PVC 的 `storageclass.storage.k8s.io
"ssd-retain-zone-c" not found`）

⚠️ **verify 时序（E-decides）**：本故障是**稳态**（PVC 未绑定前 scheduler 永不
调度），verify **不必等恢复**——定时器武装 + 注入生效确认（上面 1-4 的观察）
后即可进入效果裁决：窗口内观察到「最高序号 Pod Pending + FailedScheduling
unbound PVC 事件 + PVC Pending」即效果确证。恢复由定时器带外完成，恢复确认
（Pod Running + PVC 清理 + revision 收敛）走演练结束后的带外核验

**持续性检查（必做）**——故障窗口内故障必须持续存活（配置型故障：注入卷在 Pod
模板 spec、PVC Pending，字段在即故障在）：
以「注入生效确认」为时点锚（注入验证第 1-4 条通过 = 生效），生效后一次
`time_wait 30`（间隔 = 2 × 传播上限：本案为规则/字段/进程型即时生效故障，无传播过程，30s 为最小复测窗，按 SKILL.md「持续性采样间隔 per-case 推导」），到点**同轮下发**三条探针并**具体记录命令与输出**——效果证据须在
故障存活期内采集，恢复完成后无法再采集；若已恢复，取证定时器是否提前触发/人工
介入后如实报告：
1. 白盒复查：模板 volumes 仍含 `sts-unbound-claim` 引用（`kubectl get
   statefulset <sts-name> -n <namespace> -o
   jsonpath='{.spec.template.spec.volumes}'`）且 PVC 仍 Pending（配置即状态）
2. 行为复查：最高序号 Pod 仍 Pending、READY 仍 < DESIRED 不收敛
3. 事件复查：FailedScheduling 事件 LAST SEEN 相比注入时有新增（调度器仍在
   周期性重试）——**新式事件形态注（实测）**：部分集群对进入 unschedulable
   队列的 Pod 不重发 FailedScheduling（单次 eventTime、count/series 空，LAST SEEN
   原地变老），此时本条判据结构性不可达，如实记 partial，持续性证明由第 1/2 条
   承载（配置即状态 + 行为不收敛）；勿把事件不前进误判为故障已恢复

**注入恢复**（主路径下模板还原无需 Agent 执行动作——载体 TTL 自治按 restorePatches 还原 Pod 模板（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。非 patch 域动作——删卡住错误 revision Pod（**不可省略**）与删演练 PVC——保留为 execute 计划收尾步骤，在模板还原后执行（先模板、次卡住 Pod、后 PVC，顺序即安全顺序，见步骤 3）。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期执行基线还原：定时器宿主形态由定时器自动执行；演练
   提前结束时由 Agent 主动执行同一组恢复命令（幂等，定时器迟到再执行一次无
   副作用。**数组整体 replace 回基线而非按索引 remove**——remove 按位置删除，
   重复触发时数组已变化，同索引会误删其他卷；基线为空/字段原本不存在时对应
   op 改为 remove——remove 对已不存在的路径仅报错，不会误删其他数组项）：
   ```bash
   # ① 模板还原（volumes 基线为空时 op 改 remove）
   kubectl patch statefulset <sts-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/volumes","value":<步骤1基线volumes JSON>},{"op":"replace","path":"/spec/template/spec/containers/<idx>/volumeMounts","value":<步骤1基线volumeMounts JSON>}]'
   # ② 删除卡住的错误 revision Pod（幂等：健康 Pod hash 已等于基线，选择器空集）
   kubectl delete pod -n <namespace> -l "<pod-label>,controller-revision-hash!=<基线revision名>" --ignore-not-found
   # ③ 删除演练 PVC
   kubectl delete pvc sts-unbound-claim -n <namespace> --ignore-not-found
   ```
   （`<idx>` 为挂载注入卷的容器 index；多容器且注入目标非第一容器时用其实际
   index。**②不可省略**——OrderedReady 控制器对非 Running/Ready 的错误 revision
   Pod 零动作，不删则 STS 永不回 READY==DESIRED（实测：恢复后 12 分钟零 delete
   事件，手动删除后 45 秒收敛））
2. 等待滚动更新完成（被卡序号的 Pod 由步骤 ②触发重建为基线 Pod 并调度运行；
   判据见恢复验证第 1 条，勿用 rollout status）

**恢复验证**：
1. `kubectl get statefulset <sts-name> -n <namespace>`：READY == DESIRED，
   全部副本 Running/Ready，`currentRevision == updateRevision`（滚动完成；
   模板还原回基线 hash，不产生新代——cur/up 均等于步骤 1 基线 revision 名）
2. 模板已还原：volumes/volumeMounts 的 jsonpath 输出与步骤 1 基线捕获一致
   （基线原本为空则输出为空）
3. 注入时创建的 PVC 已被清理（`kubectl get pvc sts-unbound-claim -n
   <namespace>` 返回 NotFound）
4. 卡住的 Pending Pod 已被步骤 ② 删除并按基线 revision 重建（重建 Pod 的
   controller-revision-hash 等于基线 revision 名，AGE 重置，Running）
5. FailedScheduling 事件不再新增——事件列表是 append-only 的（历史事件不会
   消失），判据是 **LAST SEEN 时戳不再前进**：恢复后复查一次，最后一条
   FailedScheduling 的 LAST SEEN 停在恢复时刻之前即确证「不再新增」
   （新式事件形态下以 eventTime 与 series.lastObservedTime 为准）

**基准事实**：
- **根因**：StatefulSet Pod 模板被挂载引用了无法绑定的 PVC（StorageClass 不
  存在），滚动更新重建的最高序号 Pod 因 PVC 未绑定无法调度，OrderedReady 使
  更新停在该序号
- **必现现象**：最高序号 Pod Pending；PVC Pending；Events 显示 unbound
  PersistentVolumeClaims；READY < DESIRED 持续不收敛；currentRevision !=
  updateRevision

**注意事项**：
- `spec.volumeClaimTemplates` 是不可变字段（API 直接拒绝 patch）——本用例机制
  走 `spec.template.spec.volumes` 外部卷引用，与 STS 原生 PVC 通路正交
- 本用例对 STS 存储**零触碰**：不创建/删除/篡改任何 volumeClaimTemplates 派生
  PVC，恢复后数据卷原样——区别于「删原 PVC 再抢占名字」类破坏性方案（该类
  方案销毁目标序号数据、且 PVC 名为靶标派生无法静态立法，触碰 SKILL.md
  「无备份对 StatefulSet 做破坏性实验」红线，不得采用）
- **恢复期 OrderedReady 楔子（本用例区别于 Deployment 版的核心差异）**：注入期
  「停在高序号」与恢复期「控制器不删卡住 Pod」是同一机制的两面——OrderedReady
  从高序号推进且等待每个更新后 Pod Running 且 Ready；错误 revision 的 Pending
  Pod 卡住滚动，控制器对它零动作（与 Deployment 的 ReplicaSet 语义不同：
  Deployment 恢复时控制器自动缩容故障 RS、重建旧 RS Pod，无需人工删 Pod）。
  恢复动作链必须含「删卡住 Pod」，否则模板还原后 STS 永不收敛
- 无需操纵 maxUnavailable：STS 滚动更新无 Deployment 式滚动死锁机理
  （OrderedReady 停在最高序号本身就是故障形态），比 Deployment 版用例少一个
  mutation
- 单副本 STS 窗口内服务完全不可用（唯一副本 Pending）；quorum 类应用窗口内
  N-1 存活，如实向用户声明后再确认 duration
- `<duration>` 必须覆盖注入生效确认、持续性检查与恢复全程（恢复含一次滚动
  重建，宁宽勿窄）
