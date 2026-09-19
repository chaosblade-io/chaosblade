---
# 恢复通道路由声明（openspec faultdrill-cr-channel，design D3 第一源）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 Service selector），路由进 FaultDrill
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

**用例名称** selector不匹配 导致 Service_调用失败

**CR 通道模板**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 Service selector；planning 优先路由 FaultDrill CR 通道，CRD 不可装时降级正文 SOP 形态）：

```yaml
apiVersion: drill.blade-ai.io/v1alpha1   # 组名可配（faultdrill_crd_group）
kind: FaultDrill
metadata:
  name: fd-<任务派生短哈希>               # 前缀可配（faultdrill_name_prefix）；零演练签名词根
  namespace: <namespace>                  # 必须显式写入——见下方 P10 条款
spec:
  action: specPatch
  targetRef:
    kind: Service
    name: <service-name>
    namespace: <namespace>
  patches:                                # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: replace
    path: /spec/selector
    value: <与后端 Pod 标签不匹配的 selector>
  restorePatches:                         # 恢复域：调和器 TTL 到点执行；Agent 死亡后 recover 重放同源
  - op: replace
    path: /spec/selector
    value: <注入前记录的基线 selector>
  durationSeconds: <duration>             # TTL 从 Injected 相位起算，取正文演练窗口同值（宁宽勿窄）
```

- **P10 立法（namespace 显式写入）**：`metadata.namespace` 必须显式写入（victim ns；stealth 配置 ops ns 时写 ops ns）——恢复句柄水合链是 manifest ns > `-n` flag > context default，不读 settings 落位字段；省略则 CR 落位与恢复句柄错位（句柄指向配置 ns 而 CR 实落默认 ns），recover get NotFound 误判实验丢失。
- 恢复由通道调和承载（restorePatches），不再武装 recovery carrier timer（恢复语义单一来源）；非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Service 的 Endpoints 列表为空
2. 通过 Service 访问应用返回 connection refused 或无响应
3. Pod 正常运行但未被 Service 选中

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service
2. 确认监控系统可观测 Service 请求指标和 Endpoints 状态

**演练步骤**：
1. 记录应用 A 的 Service 当前 selector 配置
2. **先武装定时自恢复，再注入**（基线捕获 → 武装定时器 → 注入三步。恢复命令幂等：定时器到期
   自动恢复为主，Agent 在演练结束时主动执行同一条命令兜底，定时器迟到重复执行无副作用。定时器
   shell 逻辑必须作为 `kubectl exec` 载体载荷派发——直接以 `sh -c '…'` 作为顶层命令派发会被
   命令守卫拦截（unknown_binary: sh），载体内 `sh -c` 同时解决 exec-form 通道不解释裸
   `( sleep … ) &` 语法的问题；载体 Pod 为多副本时无法可靠终止定时器，故不设 pidfile。
   载体 Pod 选集群内带 kubectl 且有足够 RBAC 权限的常驻 Pod（如演练工具 Pod）。自建载体按
   recovery-carrier 标准件建栈时，SA Role 须含 services 资源的 get+patch 动词——恢复脚本
   本体就是 patch svc，恢复形态与 RBAC 绑定（缺 patch 则 timer 到期恢复静默失败）。
   恢复脚本落盘形态按 recovery-carrier.md 第七节「四档定案表」按明文字节数查表选定
   （Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符或手算 b64 长度）：
   ```bash
   # 基线捕获：Agent 读取输出并记录原始 selector（定时器与主动恢复均使用）
   kubectl get svc <service-name> -n <namespace> -o jsonpath='{.spec.selector}'
   # 武装定时自恢复（"注入恢复"第 1 步命令按第七节四档表选定落盘形态；下行为旧契约历史形态示例，勿套用）
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-selector.sh; ( sleep <duration>; sh /tmp/blade-restore-selector.sh ) >/tmp/restore.log 2>&1 & echo armed'
   # 篡改 selector 注入（json patch replace 整体替换——与恢复段同形态对称。勿用 strategic merge patch：
   # SMP 对 map 是键级合并，多键 selector 下注入只覆盖被改键、其余键保留，selector 可能仍匹配
   # ——与恢复段「replace 而非 SMP」同一陷阱的注入侧镜像）
   kubectl patch svc <service-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/selector","value":{"app":"non-existent-app"}}]'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-selecto[r]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）
3. 观察 Endpoints 变化和服务可用性

**注入验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认 Endpoints 列表为空（无子集）。patch 后
   endpoints 清空有 endpoints controller 同步的秒级传播滞后（实测 ~5-15s 量级），采样前短等收敛；
   恢复后 endpoints 回填同理
2. 向 Service 发送请求，确认返回 connection refused 或超时
3. 执行 `kubectl get pods -l app=<原标签>`，确认 Pod 实际正常运行
4. 对比 Service selector 与 Pod labels，确认不匹配

**注入恢复**：
1. 等待 `<duration>` 到期，定时器自动将 selector 整体替换回基线；演练提前结束时由 Agent 主动
   执行同一条恢复命令（幂等，定时器迟到再执行一次无副作用。注意用 json patch 的 `replace`
   而非 strategic merge patch——后者对 map 是键级合并，若注入期间键集变化会残留多余键导致
   selector 永久不匹配）：
   ```bash
   kubectl patch svc <service-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/selector","value":<基线捕获的原始 selector JSON>}]'
   ```
2. 等待 Endpoints 自动更新

**恢复验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认 Endpoints 列表恢复，包含后端 Pod IP
2. 向 Service 发送请求，确认恢复正常
3. 确认服务可用性恢复

**基准事实**：
- **根因**：Service 的 selector 与后端 Pod 的 label 不匹配，导致 Endpoints 控制器无法关联任何 Pod，Service 无后端可转发
- **必现现象**：Endpoints 为空；Service 请求失败（connection refused/超时）；Pod 正常但未被选中
