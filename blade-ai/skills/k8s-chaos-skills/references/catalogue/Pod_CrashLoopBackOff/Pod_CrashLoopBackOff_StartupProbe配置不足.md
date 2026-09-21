---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 startupProbe 配置），路由进程序化
# 恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+武装+
# 注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch startupProbe/maxUnavailable 属受害者域内，名字匹配放行），无
# 跨对象写条目；装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash>
# 四件套）由工具内程序化构建——构造保证 + fail-closed 内嵌检查（RBAC 从
# restorePatches 同源推导禁通配、SA 真实 token 验权 403 中止+清理），不经
# LLM kubectl 写面，无立法条目。
---

**用例名称** StartupProbe配置不足 导致 Pod_CrashLoopBackOff

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 startupProbe 配置；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态）：

```yaml
targetRef:
  kind: Deployment
  name: <deployment-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
- op: replace
  path: /spec/template/spec/containers/0/startupProbe
  value: <配置不足的探针（如 failureThreshold 过小）>
# MU 100% 并入注入域（防滚动死锁：本案新 Pod 在 startup 探针窗口耗尽前绝无
# 可能 Ready（故障本身），默认参数即确定性死锁，连 race 都没有；100% ⇒ 旧
# Pod 可先下线，滚动确定性完成——与 PVC/OOM/ReadinessProbe case 既有模式
# 一致；基线本无 startupProbe 时探针 op 改 add 整对象——remove 分支键级
# 对称见注入恢复段）
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: "100%"
restorePatches:                           # 恢复域：载体 TTL 到点执行；Agent 死亡后 recover 重放同源
- op: replace
  path: /spec/template/spec/containers/0/startupProbe
  value: <注入前记录的基线探针配置（基线本无 startupProbe 时：注入改 add 整对象、本 op 改 remove 整键——键级对称）>
- op: replace
  path: /spec/strategy/rollingUpdate/maxUnavailable
  value: <注入前记录的基线 maxUnavailable 值>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 多容器 Pod 调整 containers/N 索引至目标容器；滚动由 patch 自动触发。
- **MU 100% 的承载分工**：主路径下步骤 2 的手动置 100% 由配方注入域承载（一次 patch 面内原子并入，无需单独执行），其还原随 restorePatches 由载体 TTL 还原——窗口内维持 100%（故障态本身，无其他滚动触发源时无增量风险），不再泄漏到恢复期之后；降级路径下步骤 2/7 的手动序列照常执行。
- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 反复重启，状态为 CrashLoopBackOff
2. Pod Events 中显示 `Startup probe failed` 后容器被杀
3. 慢启动应用尚未完成初始化就被 StartupProbe 判定为失败

**RCA症状**：
1. Pod 反复重启，状态为 CrashLoopBackOff
2. Pod Events 中显示 `Startup probe failed`，容器在初始化完成前被反复杀死
3. 容器日志显示应用启动过程被中断（无应用自身异常退出记录）
（以上为kubectl直接可观测的现象，不包含诊断结论）

**资源准备**：
1. 确认应用 A 已正常运行（应用启动时间较长，如 Java 应用）
2. 确认应用 A 启动过程中健康检查接口不可用

**演练步骤**（主路径 = 步骤 1 基线捕获（startupProbe 配置 JSON——restorePatches 基线值来源，两条路径共用）→ 调 faultdrill_assemble_carrier（参数取自载体配方：target_kind=deployment、target_name=<deployment-name>、target_namespace=<namespace>、patches=…（startupProbe replace + MU 100% 注入域）、restorePatches=…、duration_seconds=<duration>），注入+武装+readback 工具内同步完成——步骤 2 的手动置 MU 100% 由配方注入域承载，无需单独执行 → 步骤 6 等待滚动更新完成（两路径共用）→ 步骤 7 的 MU 还原随 restorePatches 由载体 TTL 承载，无需手动执行。步骤 3 的手动定时器序列与步骤 4 的手动 patch 仅当装配器 fail-closed 报告不可用时作降级兜底——降级路径下步骤 2/4/7 手动序列照常）：
1. 记录应用 A 当前的探针配置（基线捕获：Agent 读取输出并记录 JSON，恢复时使用；
   原本无 startupProbe 时输出为空）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].startupProbe}'
   ```
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新确定性完成。机制定案勿再推导：maxSurge 向上取整、maxUnavailable 向下取整，1 副本默认 25%/25% ⇒ 0 个旧 Pod 可先下线、滚动退化为纯 surge 必须等新 Pod Ready；本案新 Pod 在 startup 探针窗口耗尽前绝无可能 Ready（故障本身），默认参数即确定性死锁，连 race 都没有。100% ⇒ 1 个旧 Pod 可先下线，滚动确定性完成。详见 recovery-carrier.md 第九节「滚动更新容量语义」。**引号传输定案**：patch 命令的 `-p='[...]'` 单引号形态是传输契约——构造执行参数时原样保留计划冻结的引号，argv 层剥掉单引号会让内层 JSON 双引号被吞、服务端 400 拒绝（#44 实测：26.9s 自纠轮才重发成功））——**降级路径形态；主路径下写入 100% 由载体配方注入域承载（restorePatches 基线值来源 = 本步的读取部分，两条路径共用），无需单独执行**：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. **武装定时自恢复**（**降级兜底形态——主路径下由装配器载体 TTL 承载，无需手动武装**；恢复命令幂等：定时器到期自动还原为主，Agent 在演练结束时主动执行
   同一条命令兜底，定时器迟到重复执行无副作用。定时器 shell 逻辑必须作为 `kubectl exec` 载体载荷派发——直接以
   `sh -c '…'` 作为顶层命令派发会被命令守卫拦截（unknown_binary: sh）；载体 Pod 为多副本
   时无法可靠终止定时器，故不设 pidfile。恢复含 json patch 引号嵌套，且整对象探针载荷 +
   kubectl 命令骨架天然逼近/超出通道 1024B 上限——**落盘形态按 recovery-carrier.md 第七节「四档定案表」按明文字节数查表选定（Phase 2 无 base64 生成器，勿在计划里留 `<restore-b64>` 占位符或手算 b64 长度——直接写明文+档位指令），严禁为压字节缩小 patch 字段域**（SMP 少写字段会使未写字段保留注入值，
   B80 实证；对称律见注入恢复段——本案 remove 分支是键级对称）。`<duration>` 需覆盖滚动更新与观察窗口）。
   载体 Pod 选集群内带 kubectl 且有足够 RBAC 权限的常驻 Pod（如演练工具 Pod）：
   ```bash
   # 武装定时自恢复（"注入恢复"第 1 步命令按基线选定 replace/remove 定形后，按第七节四档表选形态落盘；
   # 本案明文 ~600B ⇒ 档②加引号定界符 heredoc 直书，或档③载体内 base64 编码）
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-startup.sh; ( sleep <duration>; sh /tmp/blade-restore-startup.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （上为旧契约历史形态示例——`<restore-b64>` 须由计划侧持有编码产物填充，Phase 1 无 base64 生成器、勿套用；现行档③为载体内 `base64 -w0` 编码（编码产物不出载体，四档表判据栏）。档②形态见第七节四档表——`sh -c "cat > /tmp/blade-restore-startup.sh <<\"EOF\"\n<明文>\nEOF"` 直书，回显目检门相同）
   武装前先校验落盘正确（回显目检恢复命令全文——字段域与注入对齐（remove 分支
   验证整键路径无 value 残留）才过；编码错或字段缺都是中止信号，禁止带病武装）：
   ```bash
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'cat /tmp/blade-restore-startup.sh && echo validated'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-startu[p]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
4. 修改应用 A 的 Deployment，添加或修改 startupProbe 使其窗口不足以覆盖应用启动时间（**降级路径形态；主路径下注入 patch（startupProbe replace/add + MU 100%）由装配器工具内同步执行**）：
   ```yaml
   startupProbe:
     httpGet:
       path: <应用实际健康检查路径>   # 占位符：必须按目标应用实际探针配置替换，/healthz 仅为示例写法
       port: <应用实际健康检查端口>   # 占位符：必须按目标应用实际探针配置替换，8080 仅为示例写法
     failureThreshold: 3
     periodSeconds: 5
   ```
   （总等待时间 = failureThreshold × periodSeconds = 15 秒，远小于应用实际启动时间。窗口参数为示例默认值，可按应用启动时长调整；**path/port 必须取自目标应用的真实探针配置，探针本身应指向可达端点，直接照抄示例值会把故障变成端口错配，偏离用例语义**）
5. 靶形态建议带 livenessProbe（贴近真实慢启动应用形态，也为恢复验证「两探针均正常」提供检查面）。⚠️ 两条机制事实勿混淆：**触发链不依赖 livenessProbe**——startupProbe 窗口耗尽时 kubelet 直接杀容器重启（Events：`Startup probe failed` + Killing）；livenessProbe 的检查在 startup 成功之后才开启。**基线态硬约束**：基线 livenessProbe 参数必须与慢启动时长相容（initialDelaySeconds + failureThreshold × periodSeconds 的首个检查窗 ≥ 应用启动时间），否则无 startupProbe 的基线态本身就会被 liveness 误杀——靶建成即 CrashLoop，基线就是坏的
6. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
7. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）——**降级路径形态；主路径下此项随载体配方 restorePatches 由载体 TTL 还原，无需手动执行**
8. 观察 Pod 启动行为

**注入验证**：
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 永不 Ready（startup 探针超时，故障本身），`rollout status` 等待 available 副本必然超时报错，按其退出码会把已完全生效的故障误判为「滚动未完成」；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 已 CrashLoopBackOff）
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 的 RESTARTS 已高于注入前读数（单调递增计数器，高于基线即重启已发生，无需等待持续增长），状态为 CrashLoopBackOff——状态标签是重启的渲染，不等其稳定出现（不是仅一个新 Pod，而是全部副本）
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 `Startup probe failed`
4. 查看容器日志，确认应用正在启动但未完成初始化就被杀

**注入恢复**（主路径下恢复无需 Agent 执行动作——载体 TTL 自治还原（restorePatches 的探针 replace/remove + MU 基线 replace 由载体内 timer 到点执行，恢复自动触发回滚滚动；fire 证据落载体 /tmp/restore.log + 任务台账 recovery_handle）；演练提前结束时 blade-ai recover 从台账重放同源配方提前收敛，与载体幂等双执行。以下手动命令为降级兜底形态）：
1. 等待 `<duration>` 到期，定时器自动将 startupProbe 还原为步骤 1 基线；演练提前结束时由
   Agent 主动执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用——基线非空时 json patch
   replace 回原值 JSON，原本无探针时 remove。json patch 按字段精确替换，天然规避
   resourceVersion 乐观锁问题，也不会像 apply 三方合并那样保留注入新增的字段。**对称律按基线形态二分**：
   **replace 分支**——注入是整对象 replace，恢复 value 必须携带步骤 1 基线的**全部字段**（字段域
   与注入对齐）；构造时预算字节，超 1024B 走 base64 两步法，**禁止删字段/换 SMP 缩域挤
   字节**——SMP 未写字段保留的是注入值，failureThreshold 漏写即残留（B80 实证，见
   recovery-carrier.md 第七节三条铁律）。**remove 分支**（基线本无 startupProbe）——注入用
   json patch **add 整对象**、恢复用 **remove 整键**，键级对称：remove 后 template 回到无键
   基线态，任一字段残留都会阻断 RS hash 回基线；remove 二次触发报 path 不存在属无害噪音
   （幂等语义，recovery-carrier.md 第九节「恢复 REST 细节」））：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/startupProbe","value":<步骤1基线JSON>}]'
   # 原本无 startupProbe 时改用 remove：
   # kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
   #   -p='[{"op":"remove","path":"/spec/template/spec/containers/0/startupProbe"}]'
   ```
2. 等待 Pod 滚动更新完成（RS 视角判据同注入验证第 1 条：注入代 RS DESIRED=0；恢复代 RS
   hash 回基线值——hash 不回即 template 有残留，零漂移归还未达成）

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Running 且不再重启（慢启动应用的恢复代新 Pod
   在启动期内 RESTARTS 冻结即为「不再重启」——不等应用完成初始化）
2. 确认 StartupProbe 和 LivenessProbe 检查均正常通过
3. 确认应用 A 完成初始化并正常服务
4. **恢复判据锄基线（勿从恢复命令的字段域派生）**：jsonpath 读回**整个 startupProbe 子树**
   与步骤 1 基线比对——**replace 分支**逐字段全比（勿只查恢复命令写过的字段——判据域跟着
   恢复命令窄化是 B80 验证层失效机制）；**remove 分支**（基线本无 startupProbe）判据是
   读回**空输出**（jsonpath 对不存在的键返回空串——空即键已移除，任何非空输出都是残留）；
   另读 `kubectl get rs -n <namespace> -l <label>` 确认**当前代 RS hash 回到注入前
   基线值**（RS hash 是 pod template 全字段相等性的免费校验和——remove 分支同样适用：
   键移除后 template 与基线逐字段相等 ⇒ hash 必回基线）。verify 窗口先于 timer fire 结束
   属常态（效果证据在窗口存活期采集），本条判据由带外终验兑现（timer fire 后读子树 +
   RS hash；Agent 收尾报告须注明恢复正确性待带外终验确认）

**基准事实**：
- **根因**：StartupProbe 未配置或 failureThreshold × periodSeconds 总窗口不足以覆盖应用启动时间，慢启动应用在初始化完成前被判定为启动失败，反复被杀重启
- **必现现象**：Pod CrashLoopBackOff；Events 显示 Startup probe failed；容器日志显示应用启动中被中断
