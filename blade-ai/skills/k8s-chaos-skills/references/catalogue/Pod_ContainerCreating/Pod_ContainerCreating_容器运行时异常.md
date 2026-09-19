**用例名称** 容器运行时异常 导致 Pod_ContainerCreating

**故障现象**：
1. Pod 长时间停留在 ContainerCreating 状态
2. Pod Events 中显示 `container runtime is not ready` 或 `rpc error` 相关错误
3. 节点上的 containerd/docker 进程异常或 hang，无法响应容器创建请求

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认目标节点上有多个应用副本（避免单点影响）
3. 确认监控系统可观测节点和容器运行时状态

**演练步骤**：
1. 定位应用 A 所在的目标节点
2. 使用 chaosblade 挂起（stop）目标节点上的 containerd 进程，模拟容器运行时 hang：
   ```bash
   blade create k8s node-process stop \
     --names <节点名> \
     --process containerd \
     --timeout <duration>

   ```
   倒计时从武装时刻起算：blade 武装后，与后续的删 Pod 触发步骤必须是紧邻操作（≤60s）；武装后发生任何修复须先 `blade destroy <experiment_uid>` 旧实验再全额重武装，然后才触发删除（见 SKILL.md 安全红线「故障窗口完整」）
3. 删除应用 A 在目标节点上的 Pod，触发重建
4. 观察新 Pod 的创建停滞状态（实际形态见注入验证——SIGSTOP 全冻结下为 Pending 停滞，不是 ContainerCreating）

**注入验证**：

> ⚠️ **按 SIGSTOP 全冻结实际形态判读**（blade `node-process stop` 与手段2同为 SIGSTOP，实际形态一致，见手段2注意事项）：新 Pod 停留 Pending（已调度但 kubelet 首次 CRI 调用同步阻塞，Pod status 不更新为 ContainerCreating）；节点保持 Ready（kubelet lease 心跳走独立通道不经过 CRI，阻塞调用不报错，RuntimeNotReady 不触发）；Events 可能不出现 runtime 报错（阻塞调用不报错）。勿按「ContainerCreating + Events 运行时报错 + NotReady」理论形态判读，否则会把已生效的注入误判为失败

1. 执行 `kubectl get pods`，确认新 Pod 停留 Pending、status 长时间无更新——创建停滞即注入生效（勿等待 ContainerCreating 形态）
2. 节点上 `kubectl exec`/`kubectl logs` 全部超时失效是 CRI 冻结的即时确证（任一 Pod 上验证即可）
3. （可选，带外通道）SSH 确认 containerd 进程状态为 T (stopped)

**注入恢复**：
1. 等待 chaosblade 实验自动超时恢复（`<duration>` 内），containerd 进程自动恢复
2. 如超时后仍未恢复，通过 `blade destroy <UID>` 强制恢复
3. 等待容器运行时恢复正常，Pod 自动完成创建

**恢复验证**：
1. 确认 containerd 进程恢复正常运行
2. 执行 `kubectl get nodes`，确认目标节点 Ready（SIGSTOP 形态下节点全程保持 Ready，此条为不退化检查而非恢复信号）
3. 执行 `kubectl get pods`，确认 Pod 状态恢复为 Running

**基准事实**：
- **根因**：容器运行时（containerd/docker）进程异常、hang 或状态不一致，无法响应 kubelet 的容器创建请求
- **必现现象**：新 Pod 创建停滞（SIGSTOP 挂起实际形态：Pod 停留 Pending、status 不更新，节点保持 Ready）；节点上 exec/logs 全部超时失效；ContainerCreating/Events rpc error/NotReady 形态在挂起（而非杀死）注入下不可达，勿以其缺失判负

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现容器运行时挂起。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；恢复需 SSH 访问权限

注入命令（**双 timer 形态——2026-09-15 实测定案，单命令原子形态存在「载体自噬」结构缺陷，见下方注意事项**）：
```bash
# 通过 kubectl debug node 武装两个宿主机 systemd transient 定时器后立即退出：
#   T+20s   → kill -STOP containerd（延迟触发，让 debug pod 在冻结生效前退出）
#   T+20s+<duration> → kill -CONT containerd（恢复定时器，冻结窗口精确 = <duration>）
# ⚠️ 关键顺序：恢复定时器必须在 STOP 定时器触发时刻之前完成登记（两条 systemd-run
#    经 && 串行登记，第二形完成先于第一形触发——20s 缓冲）；debug pod 秒退不等待
#    STOP 生效，避免载体成为自身故障的受害者。systemd transient timer 由宿主机
#    systemd(PID 1) 管理，debug Pod 删除也不影响。
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c '
  systemd-run --on-active=20s --unit=blade-stop-containerd sh -c "kill -STOP $(pidof containerd)" &&
  systemd-run --on-active=<20+duration>s --unit=blade-restore-containerd sh -c "kill -CONT $(pidof containerd)" &&
  echo ARMED_OK
'
# ARMED_OK 回显即武装完成；随后等待 ~20s（STOP timer 触发）再执行删 Pod 触发步骤
```

恢复命令：

主恢复路径是注入时登记的 systemd 定时器（`blade-restore-containerd.timer`，`--on-active=<20+duration>s`），到期自动 `kill -CONT`，Agent 无需干预；注入回执（`ARMED_OK`）秒回不依赖恢复时刻。

**提前恢复必须人工带外执行 —— Agent 不执行下面的命令。** containerd 已停止，`kubectl debug node` 需要新建容器，此刻物理上无法完成；SSH 是唯一通道：

```text
ssh root@<node-ip> 'kill -CONT $(pidof containerd)'
```

注意事项：
- **载体自噬结构缺陷（2026-09-15 实测三 attempt 定案）**：若用单命令原子形态（登记恢复后同步执行 `kill -STOP`），debug pod 自身在 STOP 生效后卡 `Pending/ContainerCreating`——kubelet 冻结无法上报容器退出，工具层 one-shot cap 与故障窗口同时到期，注入命令调用方在窗口结束前拿不到回执，后续删 Pod 步骤无法在窗口内落地（两次实测 attempt 载荷正确但演练效果为零）。双 timer 形态（STOP 延迟 20s 触发）让 debug pod 在冻结生效前 `Succeeded/exit 0` 秒退，是唯一可行形态；冻结窗口仍精确 = `<duration>`（CONT 定时器从同一武装时刻起算）
- **通道幽灵错误处置范式**：注入命令回执报基础设施错误（如 `No executor available for cluster`）不代表载荷未执行——用只读 API 证据（节点事件/debug pod 生命周期）+ 宿主机 journal（`systemd` 的 STOP/CONT Started/Succeeded 日志对）判定载荷实际执行状态，勿据通道错误直接重试（重复 STOP 会叠加冻结窗口）
- 挂起 containerd 后节点上所有容器操作均失效（包括 kubectl debug/exec/logs），恢复只能通过 systemd 定时器自愈或 SSH 带外
- 删除触发步骤用 `kubectl delete pod <name> --wait=false`：优雅终止需 kubelet 经 CRI 确认，冻结期旧 Pod 卡 `Terminating` 数十秒属预期形态，`--wait=false` 让命令立即返回不占用窗口
- **实际形态与故障现象描述有偏差（SIGSTOP 全冻结）**：新 Pod 停留 Pending（已调度但 kubelet
  首次 CRI 调用同步阻塞，Pod status 不更新为 ContainerCreating）；节点保持 Ready（kubelet
  lease 心跳走独立通道不经过 CRI，阻塞调用不报错，RuntimeNotReady 不触发）。注入生效信号
  以「Pod 已调度但 status 长时间停滞 + 节点上 exec/logs 全部超时失效」为准，不以
  ContainerCreating/NotReady 为准（主路径注入验证已按此形态判读）。停滞证据可用 Pod 存储
  的条件时间戳对（`PodScheduled=True` 时刻 vs `Initialized/startedAt` 时刻）追溯证明，
  live Pending 快照抓不到不判负（恢复后 CRI backlog 排空即更新 status）
- 建议超时设置 30-120 秒
- 若节点使用 docker 而非 containerd，将 `pidof containerd` 替换为 `pidof dockerd`
