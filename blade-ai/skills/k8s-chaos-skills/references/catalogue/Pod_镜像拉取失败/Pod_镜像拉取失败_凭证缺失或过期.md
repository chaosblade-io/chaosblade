---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 imagePullSecrets/
# imagePullPolicy/maxUnavailable），路由进程序化恢复载体装配器
# （faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+注入+readback
# 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC 不可授/验权 403）
# 时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本用例的故障机制需要写受害者
# 覆盖之外的对象——靶是 Deployment（Pod 的 owner），patch imagePullSecrets/
# imagePullPolicy/maxUnavailable 属受害者自身域内（名字匹配放行），但注入要
# **创建**（恢复时删除）失效凭证道具 Secret——跨对象写，须立法声明。由 case
# 作者在此声明，确定性代码在意图定案时装载，确认卡渲染、人工批准后冻结进守卫
# 快照。LLM 无权扩写；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名
# drill-rc-<hash> 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌
# 检查，不经 LLM kubectl 写面，无立法条目。
mechanism_writes:
  # 注入创建 / 恢复删除：失效凭证道具 Secret（invalid-user/invalid-password，
  # 零真实凭证材料——仅用于使拉取认证失败；原 CR secretSwap 源引用派生形态随
  # CR 通道退役，改 LLM kubectl create 直建，走本条目）
  - scope: secret
    namespace: default
    names: [registry-cred-rotating]
---

**用例名称** 凭证缺失或过期 导致 Pod_镜像拉取失败

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 imagePullSecrets/imagePullPolicy/maxUnavailable（凭证道具 Secret 的创建/删除为非 patch 域动作，走 execute 计划步骤+frontmatter 立法条目）；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
# 基线有 imagePullSecrets → replace 指向道具 Secret；基线无 → 改 add 整键
- op: replace
  path: /spec/template/spec/imagePullSecrets
  value: [{name: registry-cred-rotating}]
# 基线 policy 为 Always 时省略本条；IfNotPresent → 改 Always（否则 K8s 直接使用
# 本地缓存镜像启动 Pod，不触发凭证校验，故障无法注入）
- op: replace
  path: /spec/template/spec/containers/0/imagePullPolicy
  value: Always
# maxUnavailable 100% 并入注入域（正文步骤 2 的防滚动死锁操作——注入期新 Pod
# 永不 Ready，默认 MU 下 K8s 不会终止旧 Pod，滚动死锁）
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: "100%"
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
# 基线空 → remove 整键；基线有 → replace <基线值>；失效 Secret 为 execute 计划
# 道具，收尾清理步删除
- op: replace
  path: /spec/template/spec/imagePullSecrets
  value: <基线值（注入前记录的 imagePullSecrets）>
- op: replace
  path: /spec/template/spec/containers/0/imagePullPolicy
  value: <基线值（如 IfNotPresent 则连基线一并还原）>
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: <步骤2记录的基线值>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- **凭证道具零材料纪律**：失效凭证 Secret（步骤 4 `kubectl create secret docker-registry` 直建，invalid-user/invalid-password）零真实凭证材料内联——仅创建指向失效凭据的新 Secret，不复制/不读取有效源 Secret 的材料；原 CR secretSwap 源引用派生形态（sourceName + registryHostOverride）随 CR 通道退役。
- **host 匹配律警示（实验第二坑）**：`--docker-server`（原 registryHostOverride 同位参数）必须与镜像地址中的 registry host 逐字匹配——dockerconfigjson 的 auths 键 host 不匹配时拉取不引用该凭证，故障静默失效（Secret 已落地、现象不出现）。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作（Secret 创建/删除）保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 状态为 ImagePullBackOff 或 ErrImagePull
2. Pod Events 中显示 `unauthorized` 或 `authentication required`
3. 镜像仓库返回 401/403 认证错误

**资源准备**：
1. 确认应用 A 已正常运行，且使用私有镜像仓库
2. 确认应用 A 的 Pod 配置了 imagePullSecrets

**演练步骤**（主路径 = 基线捕获（步骤 1 的 imagePullSecrets/policy 记录 + 步骤 2 的 MU 读取，restorePatches 的基线值来源，两条路径共用）→ 创建失效凭证 Secret（步骤 4，道具，走 frontmatter mechanism_writes 立法条目，两条路径共用）→ 调 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Deployment、patches=imagePullSecrets 指向道具+policy Always+maxUnavailable 100%、restorePatches=三字段基线还原、duration_seconds=<duration>），注入+武装+readback 工具内同步完成——步骤 2 的手动置 100% 在主路径下由配方注入域承载；步骤 1 的基线 JSON 导出（剥离 resourceVersion）仅为降级兜底的 replace 形态所需，主路径配方为 json-patch 三字段无需整体替换；步骤 2-3、5 的手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 记录应用 A 当前的 imagePullSecrets 名称和 imagePullPolicy 值，并导出还原基线（**必须剥离
   metadata 中的 resourceVersion/uid/creationTimestamp/generation 与整个 status**——结论：
   带 resourceVersion 的 `kubectl get -o yaml` 原样输出，无论 apply 还是 replace 都会因乐观锁
   Conflict 报错，武装还原永不生效）。Agent-native 形态：Agent 经 `kubectl get deployment
   <deployment-name> -n <namespace> -o json` 取回 JSON，在自身进程内剥离上述字段并压缩为
   单行，内嵌进步骤 3 的还原脚本（顶层管道 exec-form 通道不解释、命令守卫也不放行，
   下面的运维侧一行流仅供参考，Agent 勿派发）：
   ```bash
   # 运维侧参考（顶层管道会被守卫拦截）
   kubectl get deployment <deployment-name> -n <namespace> -o json | python3 -c "
   import json,sys; d=json.load(sys.stdin)
   m=d['metadata']
   for k in ('resourceVersion','uid','creationTimestamp','generation','managedFields'): m.pop(k,None)
   d.pop('status',None); json.dump(d,open('/tmp/blade-imgsecret-baseline.json','w'))"
   ```
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新能完成，故障注入的新 Pod 不会 Ready，默认策略下 K8s 不会终止旧 Pod，导致滚动更新死锁）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. **先武装定时恢复，再注入**（载体 Pod 武装形态——直接以顶层 `( sleep … ) &` 后台子 shell
   派发会被命令守卫拦截（unknown_binary: `(`），载体内 `sh -c` 载荷同时解决 exec-form 通道
   不解释裸后台语法的问题；到期自动用基线整体替换还原 imagePullSecrets/imagePullPolicy
   并删除无效 Secret，补齐自恢复能力；**必须用 `kubectl replace` 而非 `kubectl apply`**——
   结论：apply 的三方合并会保留注入后新增的字段（还原不彻底）；replace 为 PUT 整体
   替换，在 live 被多次修改后依然精确还原；duration 需覆盖滚动更新耗时；载体 Pod 为
   多副本时无法可靠终止定时器，故不设 pidfile，恢复命令幂等——迟到重复执行无副作用）。
   还原脚本内容（落盘形态按 recovery-carrier.md 第七节「四档定案表」按明文字节数查表选定——Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符；`<基线JSON单行>` 为步骤 1 剥离后的单行 JSON，
   由 Agent 构造——Deployment JSON 不含单引号，单引号包裹安全）：
   ```sh
   echo '<基线JSON单行>' > /tmp/blade-imgsecret-baseline.json
   kubectl replace -f /tmp/blade-imgsecret-baseline.json
   kubectl delete secret registry-cred-rotating -n <namespace>
   ```
   ```bash
   # 下为旧契约历史形态示例——<restore-b64> 计划侧无法填充，勿套用；现行形态按第七节四档表
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-imgsecret.sh; ( sleep <duration>; sh /tmp/blade-restore-imgsecret.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-imgsecre[t]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
   ⚠️ 载体 RBAC 前置（形态无关立法——#51 B85 判例：规划期钦定 PUT/update 而 Agent
   合法切换 json-patch 恢复后 Role 未联动追加 patch，timer fire PATCH 403 静默失败、
   仅 DELETE 成功部分恢复）：Role verbs 以**恢复脚本载荷实际使用的全部写动词**为准
   （REST 映射：PATCH→patch / PUT→update / DELETE→delete / POST→create），不以规划期
   钦定的恢复形态为准——Agent 换恢复形态（如 PUT 整体替换 ↔ json-patch 对称）时
   授权面必须按实际载荷动词重新对账（recovery-carrier.md §2 形态无关总则）。验权门
   不止读路径 GET 200——按 §3 对恢复载荷每个写动词逐一验证（载体内 curl POST
   SelfSubjectAccessReview 或 `kubectl auth can-i <verb> <resource> -n <namespace>`，
   如 `can-i patch deployments` / `can-i update deployments` / `can-i delete secrets`
   按实际动词集），任一 no/false 则先补 Role 重验再武装——GET 200 只证明凭证链路正确，
   写动词授权从未被验证（#51 实弹翻车路径）。（恢复输出落 /tmp/restore.log 可带外
   取证，静默期已消除）
4. 创建一个包含无效凭证的 Secret 来替换原有的有效凭证：
   ```bash
   kubectl create secret docker-registry registry-cred-rotating \
     --docker-server=<registry-server> \
     --docker-username=invalid-user \
     --docker-password=invalid-password \
     --namespace <namespace>
   ```
5. 修改应用 A 的 Deployment，将 imagePullSecrets 指向无效 Secret（或直接移除 imagePullSecrets）。
   同时检查 imagePullPolicy：如果当前为 `IfNotPresent`，需同时改为 `Always`，否则 K8s 直接使用本地缓存镜像启动 Pod，不会触发凭证校验，故障无法注入
6. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
7. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段。主路径下此项随载体配方 restorePatches 由载体 TTL 还原，无需手动执行）
8. 观察 Pod 状态变化

**注入验证**：
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 永不 Ready（镜像拉取认证失败，故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 处于 ImagePullBackOff）
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 状态为 ImagePullBackOff 或 ErrImagePull——两者是同一故障的先后渲染（首拉失败即 ErrImagePull，进入退避后转为 ImagePullBackOff），出现任一即判（不是仅一个新 Pod，而是全部副本）
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 中显示认证失败相关错误
4. 确认错误信息包含 `unauthorized` 或 `authentication required`

**注入恢复**（主路径下三字段还原无需 Agent 执行动作——载体 TTL 自治按 restorePatches 还原 imagePullSecrets/imagePullPolicy/maxUnavailable（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle）；演练提前结束时 `blade-ai recover` 从台账重放同源配方提前收敛，与载体幂等双执行。失效凭证 Secret 删除为非 patch 域动作，走 execute 计划收尾清理步。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期后武装的定时器自动用基线整体替换还原 imagePullSecrets/imagePullPolicy
   并删除无效 Secret。如需提前恢复，Agent 幂等重执行同款恢复命令（先把步骤 1 的基线 JSON 写入
   本地临时文件再 replace；定时器迟到触发无害——replace 对已还原对象是 no-op，delete 对已删
   Secret 仅报 NotFound）：
   ```bash
   kubectl replace -f /tmp/blade-imgsecret-baseline.json
   kubectl delete secret registry-cred-rotating -n <namespace>
   ```
2. 基线 replace 已同时还原 imagePullSecrets、imagePullPolicy（如注入时改过）与 maxUnavailable
   （演练步骤 2 临时改的 100%）——三项都在基线快照内，无需逐项手动还原
3. 等待 Pod 滚动更新完成

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running
2. 确认镜像拉取成功，无认证错误

**基准事实**：
- **根因**：imagePullSecrets 缺失或 Secret 中的凭证已过期/无效，导致向私有镜像仓库拉取镜像时认证失败
- **必现现象**：Pod ImagePullBackOff；Events 显示 unauthorized/authentication required；镜像仓库返回 401/403
