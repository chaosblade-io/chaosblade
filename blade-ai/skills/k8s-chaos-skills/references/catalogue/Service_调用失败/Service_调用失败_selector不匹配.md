---
# 恢复通道路由声明（openspec faultdrill-cluster-native-recovery，design ND2）：
# 本 case 恢复动作住址 = apiserver 写（逆 patch 还原 Service selector），路由进
# 程序化恢复载体装配器（faultdrill_assemble_carrier 工具一次调用：建栈+验权+
# 武装+注入+readback 工具内同步完成）；装配不可用（镜像不可拉/节点不容纳/RBAC
# 不可授/验权 403）时降级正文 recovery-carrier SOP 路径。
recovery_channel: apiserver-write
# 机制写入集立法（write-set approval contract）：本 case 故障机制只需写受害者
# 自身（patch Service selector 属受害者域内，名字匹配放行），无跨对象写条目；
# 装配器载体栈（SA/Role/RoleBinding/裸 Pod 同名 drill-rc-<hash> 四件套）由工具
# 内程序化构建——构造保证 + fail-closed 内嵌检查（RBAC 从 restorePatches 同源
# 推导禁通配、SA 真实 token 验权 403 中止+清理），不经 LLM kubectl 写面，无立
# 法条目。
---

**用例名称** selector不匹配 导致 Service_调用失败

**载体配方**（`recovery_channel: apiserver-write`——恢复动作住址 = apiserver 写：逆 patch 还原 Service selector；主路径经程序化装配器 `faultdrill_assemble_carrier` 一次调用执行——LLM 从本配方取参（靶标三元组/patches/restorePatches/durationSeconds），工具内确定性完成：基线校验（restorePatches 值对账活体对象，基线漂移即中止）→ 载体栈（SA/Role/RoleBinding/裸 Pod 同名 `drill-rc-<hash>`，RBAC 从 restorePatches 同源推导禁通配）→ SA 真实 token 验权 → 两步 exec 武装（倒计时从武装时刻起算）→ 同步注入 patch 靶标 → landing readback；任一步失败 fail-closed 清理已建对象并如实报告；装配不可用时降级正文 SOP 形态）：

```yaml
targetRef:                                # 靶标（装配器 target_kind/name/namespace 参数）
  kind: Service
  name: <service-name>
  namespace: <namespace>
patches:                                  # 注入域（json-patch，value 任意 JSON 形态逐字保留）
  - op: replace
    path: /spec/selector
    value: <与后端 Pod 标签不匹配的 selector>
restorePatches:                           # 恢复域：载体 TTL 到点自治执行；Agent 死亡后 recover 从台账重放同源
  - op: replace
    path: /spec/selector
    value: <注入前记录的基线 selector>
durationSeconds: <duration>               # TTL 从武装时刻起算，取正文演练窗口同值（宁宽勿窄）
```

- 恢复由载体 TTL 自治承载（restorePatches）：配方随注入写进任务台账 fault_handle，Agent 死亡后 `blade-ai recover` 从台账重放同源配方（与载体幂等双执行——先到先收敛、后到读回 no-op）；演练提前结束时 recover 即提前收敛，不再由 LLM 武装 recovery carrier timer（恢复语义单一来源）。非 patch 域动作保留为 execute 计划普通 kubectl 步骤。

**故障现象**：
1. Service 的 Endpoints 列表为空
2. 通过 Service 访问应用返回 connection refused 或无响应
3. Pod 正常运行但未被 Service 选中

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service
2. 确认监控系统可观测 Service 请求指标和 Endpoints 状态

**演练步骤**（主路径 = 载体配方经 `faultdrill_assemble_carrier` 一次调用执行；手动序列仅当装配器 fail-closed 报告不可用时作降级兜底）：
1. 基线捕获：记录应用 A 的 Service 当前 selector 配置（restorePatches 的基线值来源，两条路径共用）：
   ```bash
   kubectl get svc <service-name> -n <namespace> -o jsonpath='{.spec.selector}'
   ```
2. 调用 `faultdrill_assemble_carrier`（参数取自载体配方：target_kind=Service、target_name=<service-name>、target_namespace=<namespace>、patches=<不匹配 selector 的 replace>、restore_patches=<基线 selector 的 replace>、duration_seconds=<duration>）——工具内同步完成建栈+验权+武装+注入+readback，回执 status=success 即注入落地且载体已武装；status=partial = 载体已武装但注入未确认，勿重建载体，用 `blade-ai recover` 提前收敛
3. 观察 Endpoints 变化和服务可用性

**降级兜底（装配器不可用时，SOP 手动序列）**：
1. **先武装定时自恢复，再注入**（基线捕获 → 武装定时器 → 注入三步。恢复命令幂等：定时器到期
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
   # 武装定时自恢复（"注入恢复"第 1 步命令按第七节四档表选定落盘形态；下行为旧契约历史形态示例，勿套用）
   kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-selector.sh; ( sleep <duration>; sh /tmp/blade-restore-selector.sh ) >/tmp/restore.log 2>&1 & echo armed'
   # 篡改 selector 注入（json patch replace 整体替换——与恢复段同形态对称。勿用 strategic merge patch：
   # SMP 对 map 是键级合并，多键 selector 下注入只覆盖被改键、其余键保留，selector 可能仍匹配
   # ——与恢复段「replace 而非 SMP」同一陷阱的注入侧镜像）
   kubectl patch svc <service-name> -n <namespace> --type='json' \
     -p='[{"op":"replace","path":"/spec/selector","value":{"app":"non-existent-app"}}]'
   ```
   倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-selecto[r]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）

**注入验证**：
1. 执行 `kubectl get endpoints <service-name>`，确认 Endpoints 列表为空（无子集）。patch 后
   endpoints 清空有 endpoints controller 同步的秒级传播滞后（实测 ~5-15s 量级），采样前短等收敛；
   恢复后 endpoints 回填同理
2. 向 Service 发送请求，确认返回 connection refused 或超时
3. 执行 `kubectl get pods -l app=<原标签>`，确认 Pod 实际正常运行
4. 对比 Service selector 与 Pod labels，确认不匹配

**注入恢复**（主路径下恢复无需 Agent 执行动作——载体 TTL 自治 fire）：
1. 等待 `<duration>` 到期，载体自治将 selector 整体替换回基线（fire 证据落载体 `/tmp/restore.log` + 任务台账 recovery_handle，可查不静默）；演练提前结束时执行 `blade-ai recover` 从台账重放同源配方提前收敛（幂等，与载体双执行——先到先收敛、后到读回 no-op）。恢复语义注意用 json patch 的 `replace`
   而非 strategic merge patch——后者对 map 是键级合并，若注入期间键集变化会残留多余键导致
   selector 永久不匹配（装配器与载体 REST 载荷均按 replace 构造，此陷阱仅手动降级路径需自防）：
   ```bash
   # 降级兜底路径的手动恢复命令（主路径无需执行）
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
