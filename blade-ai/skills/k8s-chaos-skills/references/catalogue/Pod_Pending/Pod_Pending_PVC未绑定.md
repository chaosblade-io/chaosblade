---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 移除注入的卷挂载并还原 maxUnavailable），
# 路由进程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+
# 武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本用例的故障机制需要写受害者
# 覆盖之外的对象——靶是 Deployment（Pod 的 owner），patch volumes/volumeMounts/
# maxUnavailable 属受害者自身域内（名字匹配放行），但注入要**创建**（恢复时删除）
# 演练用 PVC——跨对象写，须立法声明。由 case 作者在此声明，确定性代码在意图
# 定案时装载，确认卡渲染、人工批准后冻结进守卫快照。LLM 无权扩写；装配器载体
# 栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由工具内程序化
# 构建——构造保证 + fail-closed 内嵌检查，不经 LLM kubectl 写面，无立法条目。
mechanism_writes:
  # 注入创建 / 恢复删除：演练用 PVC（引用不存在的 StorageClass，瞬态对象）
  - scope: persistentvolumeclaim
    namespace: default
    names: [app-data-claim]
---

**用例名称** PVC未绑定 导致 Pod_Pending

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 移除注入的卷挂载并还原 maxUnavailable；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
# 基线无卷 → add 整键（引用不绑定 PVC）；基线有卷 → add /…/volumes/- 追加
  - op: add
    path: /spec/template/spec/volumes
    value: [{name: app-data, persistentVolumeClaim: {claimName: app-data-claim}}]
  - op: add
    path: /spec/template/spec/containers/0/volumeMounts
    value: [{name: app-data, mountPath: /data}]
  - op: replace
    path: /spec/strategy/rollingUpdate/maxUnavailable
    value: "100%"
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
# 基线空 → remove 整键；基线非空 → replace <基线数组>；maxUnavailable 还原注入前原值
  - op: remove
    path: /spec/template/spec/volumes
  - op: remove
    path: /spec/template/spec/containers/0/volumeMounts
  - op: replace
    path: /spec/strategy/rollingUpdate/maxUnavailable
    value: <基线值>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 道具 PVC（引用不存在 StorageClass、永远 Pending 的瞬态对象）为 execute 计划 manifest 步骤（走 front matter mechanism_writes 立法条目）；收尾清理步删除。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 状态为 Pending，无法启动
2. Pod Events 中显示 `pod has unbound immediate PersistentVolumeClaims`
3. PVC 状态为 Pending，无法绑定到 PV

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认集群中 StorageClass 和 CSI 插件正常工作
3. ⚠️ **靶形态硬性要求**：靶必须是 **Deployment 管辖的应用**（意图以
   Deployment 为靶——机制写的就是 deployment spec；deployment 靶的 pod 观察在
   secondary 覆盖内）。**裸 Pod 不可用**：Pod spec 的 volumes 字段不可 patch，
   裸 Pod 无法注入本故障（需另备
   Deployment 演练靶）。确认方式：`kubectl get pod <pod> -n <ns> -o
   jsonpath='{.metadata.ownerReferences[0].kind}'` 输出 `Deployment` 才可用
4. **RBAC 前置检查**（决定可行性）：用当前执行凭证跑 `kubectl auth can-i
   patch deployments -n <ns>` / `create persistentvolumeclaims -n <ns>` /
   `delete persistentvolumeclaims -n <ns>`，三权皆 yes 即可行。受阻时
   **严禁降级改机制**（如删除重建 Pod 等破坏性替代）

**演练步骤**（主路径 = 载体配方经 `faultdrill_assemble_carrier` 一次调用执行；手动序列仅当装配器 fail-closed 报告不可用时作降级兜底。注意：本用例需要临时修改 Deployment 添加 volume 引用，这是故障注入的必要操作，不违反安全红线。目标应用无需预先配置 PVC——注入的目的就是添加一个无法绑定的 PVC 依赖。恢复步骤会还原所有修改）：
1. 使用 `kubectl apply -f` 创建一个引用不存在的 StorageClass 的 PVC（通过 `stdin_data` 传入 YAML；走 frontmatter mechanism_writes 道具立法条目）：
   ```yaml
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata:
     name: app-data-claim
     namespace: <namespace>
   spec:
     accessModes: ["ReadWriteOnce"]
     storageClassName: "ssd-retain-zone-c"
     resources:
       requests:
         storage: 10Gi
   ```
2. 基线捕获（restorePatches 的基线值来源，两条路径共用）——记录 volumes/volumeMounts 数组基线与 maxUnavailable 原值：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.volumes}'
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[<container-index>].volumeMounts}'
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   ```
3. 调用 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、target_name=<deployment-name>、target_namespace=<namespace>、patches/restore_patches=载体配方数组、duration_seconds=<duration>）——patches 里的 maxUnavailable 100% 确保滚动更新能完成（故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁），工具内同步完成建栈+验权+武装+注入+readback，回执 status=success 即注入落地且载体已武装；status=partial = 载体已武装但注入未确认，勿重建载体，用 `blade-ai recover` 提前收敛
4. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换；观察新 Pod 的状态
5. ⚠️ **效果采样职责**（两条路径共用）：必须在**注入生效后、恢复执行前**的窗口中段完成一次效果采样并
   记录输出——`kubectl get pods -l <pod-label> -n <ns>`（确认新 Pod 全部
   Pending、旧 Pod 已消失）+ `kubectl describe pod <pending-pod>` 的 Events 段
   （unbound PVC 事件：`FailedScheduling — pod has unbound immediate
   PersistentVolumeClaims`）+ PVC 状态 Pending + 机制归因一句话。**效果证据须在
   故障存活期内采集，恢复完成后无法再采集**——否则 verify 只能判
   recovered_before_observation / unverified（效果证据无人采集）。此探针写入计划
   即成为 approved plan 的一部分，执行器会依计划执行。verify 的 E-decides 裁决依据
   （verifier 采信中段采样）

**降级兜底（装配器不可用时，SOP 手动序列）**：
1. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
2. **先武装定时恢复，再注入**（恢复命令幂等：定时器自动执行，迟到重复执行
   无副作用）。⚠️ **定时器宿主要求**：恢复定时器可武装在
   任何同时满足以下条件的宿主（集群内具备 kubectl 与集群凭证的工具 Pod）：
   - **长寿**：宿主中途重启会丢定时器，恢复退化为人工兜底，演练结束后应带外核实恢复
   - **具备恢复所需 RBAC**：武装前先在宿主内 `can-i` 验证还原命令所需权限
     （patch deployments + delete persistentvolumeclaims，即资源准备第 4 条三权；
     宿主凭证与当前执行凭证一致时可直接复用该检查结果）
   - **恢复命令可达**：本故障仅作用于调度域（PVC 未绑定阻止调度，不阻断任何宿主
     与 API Server 的交互），天然满足
   宿主为集群内 Pod 时的武装形态（恢复脚本落盘形态按 recovery-carrier.md 第七节「四档定案表」按明文字节数
   查表选定——Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符；下行为旧契约历史形态示例，勿套用）：
   ```bash
   kubectl exec <宿主Pod> -n <宿主ns> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-pvc.sh; ( sleep <duration>; sh /tmp/blade-restore-pvc.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   无可用外部宿主时按 **恢复载体标准件**（`references/carrier/recovery-carrier.md`）自建载体栈
   （busybox/curl 镜像、Pod/SA/Role/RoleBinding 四对象同名、同靶点命名空间）——
   载体内无 kubectl，武装形态改用标准件第四节的 SA token REST 模板（本用例 =
   PATCH deployment 摘卷 + DELETE PVC，Role 按标准件第二节推导），长 REST 载荷的
   字节压力按标准件第七节处置链（紧凑变量形态 → 拆串联 → base64 两步法/分块并行）同样适用；自建栈时启用自断授权尾步（标准件第四节：**最后一个**恢复 curl（DELETE PVC）升 `curl -sf` 并以 `&&` 链自删 DELETE 自己的 Binding（本用例 namespaced 栈删 `rolebindings/drill-rc-<hash>`），Role 建栈后按标准件第二节两步建栈法 json-patch 追加独立自删规则（**严禁把自删 flag 合并进 create 命令**——pflag 并集复制会污染主恢复规则，主授权规则带锁名即 GET 目标资源 403、case 不可执行）；fail-open——主恢复未确认成功则授权保留，字节挤不下就不带，完整形态与五纪律以标准件第四节为准）；标准件亦建不起来时按标准件第八节降级——
   任务如实失败收尾，不得注入（无自动恢复通道即无注入许可，不存在
   「Agent 在线保活」形态）。（基线捕获与效果采样职责见主路径步骤 2/5，两条路径共用。）
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先
   `kubectl exec <宿主Pod> -n <宿主ns> -- sh -c 'pkill -f blade-restore-pv[c]; true'`
   停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
3. 使用 `kubectl patch` 修改应用 A 的 Deployment，添加引用该 PVC 的 volume 和 volumeMount；等待 Pod 滚动更新完成，观察新 Pod 的状态
4. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段——主路径下此项随 restorePatches 由载体 TTL 还原，无需手动执行）

**注入验证**：
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 永不被调度（PVC 未绑定，故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 处于 Pending）
2. 执行 `kubectl get pvc`，确认 PVC 状态为 Pending
3. 执行 `kubectl get pods`，确认**所有**目标 Pod 状态为 Pending（不是仅一个新 Pod，而是全部副本）
4. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 unbound PVC 相关信息
5. 执行 `kubectl describe pvc app-data-claim`，确认 StorageClass 不存在或 Provisioner 异常

取证组织（单批发起，判据不变）：上述 5 项判据的证据读取互不依赖，可同批一轮发起——RS 视角与 Pod 清单走标签选择器、PVC 走计划内已知名直读；第 4/5 项的事件证据不必先发现 Pod 名再 describe，命名空间事件流一次覆盖两项：
```bash
kubectl get rs -n <namespace> -l <label>
kubectl get pvc app-data-claim -n <namespace> -o jsonpath='{.status.phase} {.spec.storageClassName}{"\n"}'
kubectl get pods -n <namespace> -l <label>
kubectl get events -n <namespace> --sort-by=.lastTimestamp | grep -E 'FailedScheduling|ProvisioningFailed|unbound'
```
（事件流同时承载新 Pod 的 `FailedScheduling — pod has unbound immediate PersistentVolumeClaims`（判据 4）与 PVC 的 `ProvisioningFailed ... storageclass ... not found`（判据 5）；省的只是「先列表发现 Pod 名、再 describe」的串行轮次，不省任何取证内容）

⚠️ **verify 时序（E-decides）**：本故障是**稳态**（Pod Pending 不会
自愈——PVC 未绑定前 scheduler 永不调度），verify **不必等恢复**——载体武装
+ 注入生效确认（上面 1-5 的观察）后即可进入效果裁决：窗口内观察到「全部目标
Pod Pending + FailedScheduling unbound PVC 事件 + PVC Pending」即效果确证
（EVIDENCE SEMANTICS：效果裁决不等恢复窗口）。恢复由载体 TTL 带外完成，恢复确认
（Pod Running + PVC 清理）走演练结束后的带外核验

**注入恢复**（主路径下恢复无需 Agent 执行动作——载体 TTL 自治 fire）：
1. 等待 `<duration>` 到期，载体自治执行基线还原（fire 证据落载体 `/tmp/restore.log` +
   任务台账 recovery_handle，可查不静默）；演练提前结束时执行 `blade-ai recover`
   从台账重放同源配方提前收敛（幂等，与载体双执行——先到先收敛、后到读回 no-op。
   **数组整体 replace 回基线而非按索引 remove**——remove 按位置删除，重复执行时
   数组已变化，同索引会误删其他卷；整体 replace 重复执行结果不变）：
   ```bash
   # 降级兜底路径的手动恢复命令（主路径无需执行）
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/volumes","value":<步骤2基线volumes JSON>},{"op":"replace","path":"/spec/template/spec/containers/<container-index>/volumeMounts","value":<步骤2基线volumeMounts JSON>}]'
   kubectl delete pvc app-data-claim -n <namespace>
   ```
   （基线为空/字段原本不存在时，对应 replace 改为 remove——remove 对已不存在的路径仅报错，
   不会误删其他数组项）
2. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running
2. 确认注入时创建的 PVC 已被清理

**基准事实**：
- **根因**：Pod 引用的 PVC 无法绑定，原因为 StorageClass 不存在或 Provisioner 异常，导致 Pod 无法挂载所需存储卷而 Pending
- **必现现象**：Pod Pending；PVC Pending；Events 显示 unbound PersistentVolumeClaims
