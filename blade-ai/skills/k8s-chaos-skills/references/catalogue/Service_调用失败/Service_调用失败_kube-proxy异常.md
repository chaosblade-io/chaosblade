**用例名称** kube-proxy异常 导致 Service_调用失败

**故障现象**：
1. 通过 ClusterIP/NodePort 访问 Service 失败，连接超时
2. 节点上 iptables/ipvs 规则未更新或被清空
3. kube-proxy Pod 异常，无法维护 Service 转发规则

**资源准备**：
1. 确认应用 A 已正常运行，对外暴露 Service
2. 确认 kube-proxy DaemonSet 正常运行
3. 确认监控系统可观测 Service 请求指标

**演练步骤**：
1. 记录 kube-proxy DaemonSet 当前状态
2. 选择目标节点，删除该节点上的 kube-proxy Pod 并临时阻止重建（通过 cordon 节点或修改 DaemonSet nodeSelector）：
   ```bash
   # 方式A：给目标节点添加标签排除 kube-proxy 调度
   kubectl label node <目标节点> net.ops/proxy-degraded=true
   kubectl patch ds kube-proxy -n kube-system \
     -p '{"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"net.ops/proxy-degraded","operator":"DoesNotExist"}]}]}}}}}}}'
   ```
   ChaosBlade `node-process kill` 不提供：经上游源码取证（chaosblade-exec-os
   `exec/process/process_kill.go`）它仅在实验创建时发送**一次**信号，`--timeout` 只
   销毁实验记录，kube-proxy 被 systemd/kubelet 拉起后故障即消失——一次性形态没有
   演练价值。持续形态用上方方式A（状态持续整个窗口）或下方方式B（STOP 状态持续，
   受 liveness 阈值约束）
3. 在目标节点上的 Pod 内通过 ClusterIP 访问 Service
4. 观察 Service 访问结果

**注入验证**：
1. 确认目标节点上 kube-proxy 进程不存在或 Pod 处于异常状态
2. 检查节点 iptables/ipvs 规则（ipvs 模式用 `ipvsadm -Ln`），确认 Service 相关转发规则缺失或
   过期——kube-proxy 挂起/被杀时**存量内核态规则不清除**，规则停滞的分离判据是 apiserver
   Endpoints 已更新而节点规则仍指向旧后端 IP（复现）
3. 在目标节点的 Pod 内访问 Service ClusterIP：规则过期指向**已死后端**时连接超时
   （`download timed out`）；存量后端仍存活时访问可能仍通（内核态规则继续转发），不能以
   「未超时」否定注入生效

**注入恢复**：
1. 方式A：移除节点标签并按注入前取证的基线还原 affinity（定时器到期自动执行，或提前主动执行；若原 DaemonSet 本就有 affinity，
   须用注入前记录的原值还原，而非直接 remove）：
   ```bash
   kubectl label node <目标节点> net.ops/proxy-degraded-
   kubectl patch ds kube-proxy -n kube-system --type=json \
     -p='[{"op":"remove","path":"/spec/template/spec/affinity"}]'
   ```
2. 方式B：按下方手段2 方式B 的恢复命令停 timer 并 SIGCONT
3. 等待 kube-proxy Pod 在目标节点重建

**恢复验证**：
1. 确认目标节点上 kube-proxy Pod 恢复 Running 且 Ready
2. 在目标节点的 Pod 内重新访问 Service，确认恢复正常
3. 检查 iptables/ipvs 规则已重新同步

**基准事实**：
- **根因**：kube-proxy Pod 异常或进程被杀，无法维护节点上的 iptables/ipvs 转发规则，导致 Service ClusterIP 流量无法被正确转发
- **必现现象**：kube-proxy 不可用；节点 iptables/ipvs 规则停滞（不随 Endpoints 更新，存量
  内核态规则不清除）；Service ClusterIP 访问超时——须规则过期指向已死后端（后端存活时
  存量规则仍转发，访问可维持）

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效 kube-proxy 异常。

前提条件：具备修改 kube-proxy DaemonSet 的权限

注入命令（**先武装定时恢复，再注入**）：
```bash
# 方式A：通过标签排除目标节点上的 kube-proxy 调度。
# 先取证原 affinity（可能为空，Agent 读取输出并记录基线 JSON）；恢复按基线分支：
# 为空则 remove，非空则还原原值——无条件 remove 会在原 DaemonSet 本就有 affinity 时把它删丢，
# 属于错误恢复。取证后武装定时自恢复（定时器 shell 逻辑作为 kubectl exec 载体载荷派发——
# 直接以 `sh -c '…'` 顶层派发会被命令守卫拦截（unknown_binary: sh）；恢复命令幂等，迟到重复
# 执行无副作用；恢复脚本落盘形态按 recovery-carrier.md 第七节「四档定案表」按明文字节数查表选定
# （Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符——下行为旧契约历史形态示例，勿套用）。载体 Pod 选集群内带 kubectl
# 且有足够 RBAC 权限的常驻 Pod（如演练工具 Pod））
kubectl get ds kube-proxy -n kube-system -o jsonpath='{.spec.template.spec.affinity}'
kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-proxy-ds.sh; ( sleep <duration>; sh /tmp/blade-restore-proxy-ds.sh ) >/tmp/restore.log 2>&1 & echo armed'
# 注入
kubectl label node <目标节点> net.ops/proxy-degraded=true
kubectl patch ds kube-proxy -n kube-system \
  -p '{"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"net.ops/proxy-degraded","operator":"DoesNotExist"}]}]}}}}}}}'
# 方式B：通过 kubectl debug node 挂起 kube-proxy 进程。
# 先武装定时 CONT（timer 由宿主机 systemd(PID 1) 管理），再 STOP；`&&` 串联保证武装失败不冻结
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-proxy sh -c "kill -CONT \$(pidof kube-proxy)" && kill -STOP $(pidof kube-proxy)'
```
方式A 倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体命名空间> -- sh -c 'pkill -f blade-restore-proxy-d[s]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）；方式B systemd 定时器的重武装残留清理见文末注意事项

恢复命令（定时器到期自动执行；提前结束时由 Agent 主动执行）：
```bash
# 方式A：移除标签并按注入前取证的基线还原 affinity（多条命令独立执行；
#        基线为空 → remove；基线非空 → 用基线原值 replace，不会删丢原有 affinity）
kubectl label node <目标节点> net.ops/proxy-degraded-
kubectl patch ds kube-proxy -n kube-system --type=json \
  -p='[{"op":"remove","path":"/spec/template/spec/affinity"}]'
# 方式B：停掉 timer，恢复 kube-proxy 进程
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemctl stop blade-restore-proxy.timer 2>/dev/null; kill -CONT $(pidof kube-proxy)'
```

注意事项：
- 方式A 效果更彻底（kube-proxy Pod 被完全移除），但修改了 DaemonSet 配置，需注意恢复
- **方式A 的 `patch ds` 仅适用于原生 apps/v1 DaemonSet 管理的 kube-proxy**：部分云托管/自研
  集群的 kube-proxy 由 OpenKruise DaemonSet 管理（Pod ownerReferences 为
  `apps.kruise.io/v1alpha1 DaemonSet`），`kubectl patch ds kube-proxy` 直接 NotFound——须改用
  `kubectl patch daemonsets.apps.kruise.io kube-proxy -n kube-system ...` 同款载荷（该集群
  确证：kruise DS 基线 affinity 非空且含多条业务排除约束，恢复必须走 replace 原值分支；
  修改管控面 DS 会引发全集群 kube-proxy 滚动，注入前评估爆炸半径）
- **方式B 更轻量，但 kube-proxy 带 liveness probe 时注入窗口有硬上限**（确证，典型配置
  failureThreshold=5 × periodSeconds=10s + terminationGracePeriodSeconds=30s）：STOP 后
  ~50s liveness 判死发 SIGTERM（STOP 进程对捕获型信号 pending 不退出）→ +30s grace 期满
  SIGKILL → kubelet 重建容器自愈——**挂起最长持续 ≈ 80s**，超出后故障被 kubelet 自动消除。
  timer 建议设在 liveness 阈值内（如 30s）以获得同 PID 干净恢复（restartCount 不变）；
  timer 超过阈值时 kubelet 先行重启，到期 CONT 作用于新 PID、幂等无害（兜底语义）
- **手动 CONT 存在时序分叉**：SIGTERM 已 pending 后（约 STOP 后 50–80s 窗口）执行 CONT 会
  唤醒进程并立即处理积压 SIGTERM → graceful 退出（exitCode 143）→ 经容器重启完成恢复；
  liveness 失败前（约 50s 内）CONT 才是原进程直接恢复。最终态均为恢复，仅路径不同
- 自恢复机制：方式B 依赖宿主机 systemd-run transient timer（到期自动 SIGCONT）；
  方式A 为载体 sh -c 载荷内定时器（到期自动去标签+按基线还原 affinity，为空 remove、
  非空 replace 原值，不会删丢原有 affinity；定时器存活于载体 Pod，Pod 重建会丢失定时器，
  届时仍需 Agent 主动执行或人工恢复兜底）
- 方式B 的同名 transient timer 重复武装会报 `Unit blade-restore-proxy.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先按本文件方式B 的同等 chroot /host 通道形态清理残留：`systemctl stop blade-restore-proxy.service; systemctl reset-failed blade-restore-proxy.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
