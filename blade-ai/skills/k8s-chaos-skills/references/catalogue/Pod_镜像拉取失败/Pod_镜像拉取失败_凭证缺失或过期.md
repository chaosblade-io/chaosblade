---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 imagePullSecrets/imagePullPolicy 并由调和器删除派生的失效凭证道具），路由进 FaultDrill
# CR 通道；CRD 不可装时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：CR 通道本体（FaultDrill CR）
# 落 victim ns（P10 显式写入），scope 在受害者覆盖与同 ns secondary 网之外——
# 写集准入唯一路径 = 本条目；CR 名 = fd-<任务派生短哈希>（前缀与正文 CR 模板
# 同源默认值），走 name_prefix 动态形态；条目 ns 与本 case 演练窗口 ns 对齐。
mechanism_writes:
  # CR 通道本体（FaultDrill CR 落 victim ns——P10 显式写入；名 = fd-<任务派生
  # 短哈希>，前缀与正文 CR 模板同源默认值）：scope 在受害者覆盖与同 ns secondary
  # 网之外，写集准入唯一路径 = 本立法条目（guard 3.6 mechanism-entries 分支）
  - scope: faultdrill
    namespace: default
    name_prefix: "fd-"
---

**用例名称** 凭证缺失或过期 导致 Pod_镜像拉取失败

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 imagePullSecrets/imagePullPolicy 并由调和器删除派生的失效凭证道具；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

```yaml
apiVersion: drill.blade-ai.io/v1alpha1   # 组名可配（faultdrill_crd_group）
kind: FaultDrill
metadata:
  name: fd-<任务派生短哈希>               # 前缀可配（faultdrill_name_prefix）；零演练签名词根
  namespace: <namespace>                  # 必须显式写入——见下方 P10 条款
spec:
  action: secretSwap
  targetRef:
    kind: Deployment
    name: <deployment-name>
    namespace: <namespace>
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: replace
    path: /spec/template/spec/imagePullSecrets
    value: [{name: registry-cred-rotating}]
  - op: replace
    path: /spec/template/spec/containers/0/imagePullPolicy
    value: Always
  - op: replace
    path: /spec/strategy/rollingUpdate/maxUnavailable
    value: "100%"
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  # 逆 patch 还原三字段；失效 Secret 是 CR 派生道具——调和器恢复时自动删除，无需手删
  - op: replace
    path: /spec/template/spec/imagePullSecrets
    value: <基线值（注入前记录的 imagePullSecrets）>
  - op: replace
    path: /spec/template/spec/containers/0/imagePullPolicy
    value: <基线值（如 IfNotPresent 则连基线一并还原）>
  - op: replace
    path: /spec/strategy/rollingUpdate/maxUnavailable
    value: <基线值>
  invalidSecret:                         # 凭证道具：源引用 + 变换配方，零凭证材料内联
    name: registry-cred-rotating
    sourceName: <有效源 Secret 名（含真实凭证）>
    registryHostOverride: <registry-server>
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- **invalidSecret 源引用立法**：只携带 sourceName（源 Secret）与变换参数——零凭证材料内联（CR 不在 Secret encryption-at-rest 覆盖内，内联 = 凭证明文落 etcd）；调和器读源派生失效副本（invalid-user/invalid-password）写入 name 指定的道具 Secret。
- **host 匹配律警示（实验第二坑）**：registryHostOverride 必须与镜像地址中的 registry host 逐字匹配——dockerconfigjson 的 auths 键 host 不匹配时拉取不引用该凭证，故障静默失效（CR 已落地、现象不出现）。
- 恢复由通道调和承载（restorePatches + 删 invalidSecret 派生道具），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划 普通 kubectl 步骤。

**故障现象**：
1. Pod 状态为 ImagePullBackOff 或 ErrImagePull
2. Pod Events 中显示 `unauthorized` 或 `authentication required`
3. 镜像仓库返回 401/403 认证错误

**资源准备**：
1. 确认应用 A 已正常运行，且使用私有镜像仓库
2. 确认应用 A 的 Pod 配置了 imagePullSecrets

**演练步骤**：
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
7. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）
8. 观察 Pod 状态变化

**注入验证**：
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 永不 Ready（镜像拉取认证失败，故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 处于 ImagePullBackOff）
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 状态为 ImagePullBackOff 或 ErrImagePull——两者是同一故障的先后渲染（首拉失败即 ErrImagePull，进入退避后转为 ImagePullBackOff），出现任一即判（不是仅一个新 Pod，而是全部副本）
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 中显示认证失败相关错误
4. 确认错误信息包含 `unauthorized` 或 `authentication required`

**注入恢复**：
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
