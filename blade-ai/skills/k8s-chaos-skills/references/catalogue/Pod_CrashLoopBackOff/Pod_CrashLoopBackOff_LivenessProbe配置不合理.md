---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 livenessProbe 配置），路由进 FaultDrill
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

**用例名称** LivenessProbe配置不合理 导致 Pod_CrashLoopBackOff

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 livenessProbe 配置；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

```yaml
apiVersion: drill.blade-ai.io/v1alpha1   # 组名可配（faultdrill_crd_group）
kind: FaultDrill
metadata:
  name: fd-<任务派生短哈希>               # 前缀可配（faultdrill_name_prefix）；零演练签名词根
  namespace: <namespace>                  # 必须显式写入——见下方 P10 条款
spec:
  action: specPatch
  targetRef:
    kind: Deployment
    name: <deployment-name>
    namespace: <namespace>
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: replace
    path: /spec/template/spec/containers/0/livenessProbe
    value: <不合理的探针配置（如超时阈值过低）>
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  - op: replace
    path: /spec/template/spec/containers/0/livenessProbe
    value: <注入前记录的基线探针配置>
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- 多容器 Pod 调整 containers/N 索引至目标容器；滚动由 patch 自动触发。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Pod 反复重启，状态为 CrashLoopBackOff
2. Pod Events 中显示 `Liveness probe failed` 后容器被杀
3. 容器本身运行正常，但 Liveness Probe 配置参数不合理导致误判

**RCA症状**：
1. Pod 反复重启，状态为 CrashLoopBackOff
2. Pod Events 中显示 `Liveness probe failed`，容器被反复杀死
3. 容器日志无应用异常退出记录
（以上为kubectl直接可观测的现象，不包含诊断结论）

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认应用 A 有健康检查接口

**演练步骤**：
1. 记录应用 A 当前的 livenessProbe 配置（基线捕获：Agent 读取输出并记录 JSON，恢复时使用；
   原本无 livenessProbe 时输出为空）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.template.spec.containers[0].livenessProbe}'
   ```
2. 记录 Deployment 当前 maxUnavailable 值，并临时设为 100%（确保滚动更新确定性完成。机制定案勿再推导：maxSurge 向上取整、maxUnavailable 向下取整，1 副本默认 25%/25% ⇒ 0 个旧 Pod 可先下线、滚动退化为纯 surge 必须等新 Pod Ready；本案新 Pod 短暂 Ready 后即被误杀 liveness 打入 CrashLoop，默认参数是在赌「Ready 先于被杀」的 race。100% ⇒ 1 个旧 Pod 可先下线，滚动确定性完成。详见 recovery-carrier.md 第九节「滚动更新容量语义」）：
   ```bash
   kubectl get deployment <deployment-name> -n <namespace> \
     -o jsonpath='{.spec.strategy.rollingUpdate.maxUnavailable}'
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":"100%"}]'
   ```
3. **武装定时自恢复**（恢复命令幂等：定时器到期自动还原为主，Agent 在演练结束时主动执行
   同一条命令兜底，定时器迟到重复执行无副作用。定时器 shell 逻辑必须作为 `kubectl exec` 载体载荷派发——直接以
   `sh -c '…'` 作为顶层命令派发会被命令守卫拦截（unknown_binary: sh）；载体 Pod 为多副本
   时无法可靠终止定时器，故不设 pidfile。恢复含 json patch 引号嵌套，且整对象 probe 基线
   JSON + kubectl 命令骨架天然逼近/超出通道 1024B 上限——**落盘形态按 recovery-carrier.md 第七节「四档定案表」按明文字节数查表选定（Phase 2 无 base64 生成器，勿在计划里留 `<restore-b64>` 占位符或手算 b64 长度——直接写明文+档位指令；#43-R 实证档③载体内 base64 -w0 编码、#43-R2 第五跑实证档④分块并行），严禁为压字节缩小 patch 字段域**：删字段或换 SMP 少写字段都会使未写字段
   保留注入值（SMP 合并语义下漏写的 failureThreshold 等字段静默残留，B80 实证）——恢复
   patch 必须与注入 patch 同类型同字段域，值取基线快照（recovery-carrier.md 第七节铁律
   1/2）。`<duration>` 需覆盖滚动更新与观察窗口）。
   载体 Pod 选集群内带 kubectl 且有足够 RBAC 权限的常驻 Pod（如演练工具 Pod）：
   ```bash
   # 武装定时自恢复（"注入恢复"第 1 步命令按基线选定 replace/remove 定形后，按第七节四档表选形态落盘）
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-liveness.sh; ( sleep <duration>; sh /tmp/blade-restore-liveness.sh ) >/tmp/restore.log 2>&1 & echo armed'
   ```
   （上为旧契约历史形态示例——`<restore-b64>` 须由计划侧持有编码产物填充，Phase 1 无 base64 生成器、勿套用；现行档③为载体内编码、档④为分块并行（四档表判据栏，#43-R / #43-R2 实证）。档②形态见第七节四档表——`sh -c "cat > /tmp/blade-restore-liveness.sh <<\"EOF\"\n<明文>\nEOF"` 直书，回显目检门相同）
   武装前先校验落盘正确（回显目检恢复命令全文——字段域与注入对齐、一个字段
   不缺才过；编码错或字段缺都是中止信号，禁止带病武装）：
   ```bash
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'cat /tmp/blade-restore-liveness.sh && echo validated'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-livenes[s]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
4. 修改应用 A 的 Deployment，设置不合理的 livenessProbe 参数：
   ```yaml
   livenessProbe:
     httpGet:
       path: <应用实际健康检查路径>   # 占位符：必须按目标应用实际探针配置替换，/healthz 仅为示例写法
       port: <应用实际健康检查端口>   # 占位符：必须按目标应用实际探针配置替换，8080 仅为示例写法
     initialDelaySeconds: 1
     timeoutSeconds: 1
     periodSeconds: 2
     failureThreshold: 1
   ```
   （initialDelaySeconds 过短，应用未完成初始化；timeoutSeconds 过短，正常响应来不及返回；failureThreshold 为 1，无容错空间。时序参数为示例默认值，可按应用情况调整；**path/port 必须取自目标应用的真实探针配置——本用例的故障机制是时序过严，探针本身应指向可达端点；直接照抄示例值会把故障变成端口错配，偏离用例语义**）
5. 等待 Pod 滚动更新完成，确认所有旧 Pod 已被替换
6. 滚动更新完成后，立即还原 maxUnavailable 为原始值（maxUnavailable 只是使滚动更新完成的手段，不是故障本身，不应泄漏到恢复阶段）
7. 观察 Pod 重启行为

**注入验证**：
1. 确认所有旧 Pod 已被替换（滚动更新完成）：**用 RS 视角判据，不要用 `kubectl rollout status`**——注入期新 Pod 先短暂 Ready 随即 CrashLoop 抖动（liveness 在启动后才失败），`rollout status` 等待 available 副本的行为依赖时序、可能长时间阻塞；正确判据是 `kubectl get rs -n <namespace> -l <label>`：旧 RS DESIRED=0、新 RS DESIRED=目标副本数（或旧 Pod 名消失、新 Pod 已 CrashLoopBackOff）
2. 执行 `kubectl get pods`，确认**所有**目标 Pod 的 RESTARTS 已高于注入前读数（单调递增计数器，高于基线即重启已发生，无需等待持续增长），状态为 CrashLoopBackOff——状态标签是重启的渲染，不等其稳定出现（不是仅一个新 Pod，而是全部副本）
3. 执行 `kubectl describe pod <pod-name>`，确认 Events 显示 `Liveness probe failed` 和 `Container will be killed`
4. 查看容器日志，确认应用本身无异常退出（非应用 bug）

**注入恢复**：
1. 等待 `<duration>` 到期，定时器自动将 livenessProbe 还原为步骤 1 基线；演练提前结束时由
   Agent 主动执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用——基线非空时 json patch
   replace 回原值 JSON，原本无探针时 remove。json patch 按字段精确替换，天然规避
   resourceVersion 乐观锁问题，也不会像 apply 三方合并那样保留注入新增的字段。**replace 的
   value 必须携带步骤 1 基线的全部字段**——注入是整对象 replace，恢复字段域与注入对齐（同
   语义对称律）；构造时预算字节，超 1024B 走 base64 两步法，**禁止删字段/换 SMP 缩域挤
   字节**——SMP 未写字段保留的是注入值，failureThreshold 漏写即残留（B80 实证，见
   recovery-carrier.md 第七节三条铁律））：
   ```bash
   kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe","value":<步骤1基线JSON>}]'
   # 原本无 livenessProbe 时改用 remove：
   # kubectl patch deployment <deployment-name> -n <namespace> --type='json' \
   #   -p='[{"op":"remove","path":"/spec/template/spec/containers/0/livenessProbe"}]'
   ```
2. 等待 Pod 滚动更新完成（RS 视角判据同注入验证第 1 条：注入代 RS DESIRED=0；恢复代 RS
   hash 回基线值——hash 不回即 template 有残留，零漂移归还未达成）

**恢复验证**：
1. 执行 `kubectl get pods`，确认 Pod 状态为 Running 且不再重启
2. 确认 Liveness Probe 检查正常通过
3. 确认应用 A 服务正常
4. **恢复判据锄基线（勿从恢复命令的字段域派生）**：jsonpath 读回**整个 livenessProbe 子树**
   与步骤 1 基线逐字段比对（initialDelaySeconds/periodSeconds/timeoutSeconds/failureThreshold
   四字段全比，勿只查恢复命令写过的两个字段——判据域跟着恢复命令窄化是 B80 验证层失效
   机制）；另读 `kubectl get rs -n <namespace> -l <label>` 确认**当前代 RS hash 回到注入前
   基线值**（RS hash 是 pod template 全字段相等性的免费校验和——template 任一字段残留则
   Deployment controller 创建新 RS，hash 必不回基线）。verify 窗口先于 timer fire 结束
   属常态（效果证据在窗口存活期采集），本条判据由带外终验兑现（timer fire 后读子树 +
   RS hash；Agent 收尾报告须注明恢复正确性待带外终验确认）

**基准事实**：
- **根因**：Liveness Probe 的 initialDelaySeconds/timeoutSeconds/failureThreshold 配置不合理，导致健康检查在应用正常运行时误判为失败，kubelet 反复杀死并重启容器
- **必现现象**：Pod CrashLoopBackOff；RESTARTS 持续增长；Events 显示 Liveness probe failed；容器日志无异常
