**用例名称** 可用区网络分区 导致 Node_网络故障

**故障现象**：
1. 目标可用区内所有节点的网络通信中断，模拟整个可用区网络分区
2. 可用区内所有 Pod 与外部通信中断，跨 AZ 流量被切断
3. 多个节点同时变为 NotReady（kubelet 与 API Server 断联）
4. 集群调度器将工作负载从故障 AZ 迁移至健康 AZ

**资源准备**：
1. 确认目标可用区名称（AZ 标签值），查询方式：
   ```bash
   kubectl get nodes --show-labels | grep topology.kubernetes.io/zone
   ```
2. 确认集群其他可用区有足够冗余节点承载被驱逐的工作负载
3. 确认 ChaosBlade Operator 已部署（DaemonSet 通道）
4. 确认目标 AZ 内节点数量，评估影响面
5. **识别禁止注入的排除节点 `EXCLUDED_NODES`**（承载控制面与命令下发通道的节点，**必须从注入目标中剔除、禁止注入**——一旦被分区，集群控制/带内下发与恢复通道即失联，演练将不可控、不可恢复；它们是刻意留存的观测与恢复支点）：
   - **API Server / 控制面节点**（无论用哪种通道都禁止注入）。落在目标 AZ 内时从注入列表剔除：
     ```bash
     kubectl get nodes -l node-role.kubernetes.io/control-plane -o wide
     ```
   - **kubewiz-executor Pod 所在节点**：命令经 `wiz task exec` → 集群内 `kubewiz-executor` Pod 下发。该 Pod 是单副本 Deployment，它所在节点是带内下发咽喉，禁止注入。用**正确的 sigma 标签**定位（注意不是 `app=...`）：
     ```bash
     kubectl get pods -n kubewiz -l sigma.ali/app-name=kubewiz-executor -o wide
     # 取 NODE 列即为排除节点；亦可用 -l sigma.ali/instance-group=kubewiz-executor_<cluster-uuid> 精确匹配本集群实例
     ```
   - 上述节点即使落在目标 AZ 内也**一律从注入列表剔除**；若查不到（托管控制面/标签缺失）则记为"排除节点未定"，按现状注入其余节点，并依赖运行期纪律兜底：注入期间任何查询超时按"暂不可得（indeterminate）"处理并重试（见守卫/验证启发式），绝不当作"节点已断/全部成功"，报告中说明排除节点未能确认、观测通道可能随注入中断。

**演练步骤**：
1. 查询目标可用区的所有节点名称：
   ```bash
   kubectl get nodes -l topology.kubernetes.io/zone=<az-name> -o jsonpath='{.items[*].metadata.name}'
   ```
   若集群使用旧版标签：
   ```bash
   kubectl get nodes -l failure-domain.beta.kubernetes.io/zone=<az-name> -o jsonpath='{.items[*].metadata.name}'
   ```

2. 对目标 AZ **除排除节点外**的全部节点注入网络屏蔽（`EXCLUDED_NODES` 不在注入范围内）：

   **方式一：DaemonSet 通道**（⚠️ 注入后无法通过 K8s API 恢复，必须指定 `--timeout`）

   对目标节点分批注入（各节点彼此无依赖，可批量同时 DROP）：
   ```bash
   # 批次 1..N：目标 AZ 节点去掉 EXCLUDED_NODES 后的列表。每批 --names 长度须控制在下发通道内联上限内
   #（kubewiz 通道约 1024 字节 ≈ 15-20 个节点名），超出则拆成多批顺序提交。
   blade create k8s node-network drop \
     --names <node1>,<node2>,...,<nodeK> \
     --timeout 120 \
     --kubeconfig <kubeconfig-path>
   ```
   ⚠️ 严禁把 `EXCLUDED_NODES`（API Server/控制面节点、kubewiz-executor Pod 所在节点）纳入任何一批。

3. 记录返回的 blade_uid，用于后续恢复

> ⚠️ **注入范围与批量约束（排除节点禁止注入、其余可分批）**：目标 AZ 节点须先剔除 `EXCLUDED_NODES`，仅对剩余节点注入，必须同时满足两条不变量：
>
> 1. **排除节点禁止注入**：把"资源准备"识别出的排除节点 `EXCLUDED_NODES`（API Server/控制面节点，以及 kubewiz 通道下的 `kubewiz-executor` Pod 所在节点）**从注入列表中彻底剔除，任何批次都不得包含它们**——一旦它们被分区，集群控制面/带内下发与恢复通道即失联，演练将不可控、不可恢复。它们是刻意留存的观测与恢复支点，不是"漏注入"，报告中应说明其为"按安全策略排除"。
> 2. **其余节点可分批并发**：非排除节点不承载控制/下发通道，**可批量同时 DROP，无需逐个串行**。唯一约束是**每批 `--names` 长度须控制在下发通道内联上限内**（kubewiz 通道约 1024 字节 ≈ 5-10 个节点名），超出则拆成多批顺序提交。若某一批整体失败（如命令超长被拒、下发通道抖动），**可对该批内失败的节点逐个单独重试**，成功一个记一个，不因整批失败而放弃剩余节点。
>
> 守卫仍逐调用（逐批）复核每条命令。若排除节点未定（查不到控制面/kubewiz-executor），仍按分批方式注入其余节点，依赖运行期"超时即重试、不从局部推整体"的纪律兜底，并在报告中标注排除节点未能确认。

**注入验证**：

> ⚠️ **自断链路故障的判读规范（务必先读）**：本故障屏蔽的 6443/10250 正是 `kubectl exec` / kubewiz 依赖的通道。因此通过 exec/kubectl-native 方式注入时，**注入用的那条 exec 命令自身会超时或断连（如 `timed out after 10s`），这是预期的成功信号，不是失败**。收到超时后：
> 1. **不要重试同一条 exec，也不要归因为“镜像没 shell / 通道坏了”**——通道正是被你成功切断的；
> 2. **立即改从集群侧（apiserver 视角）验证**：`kubectl get nodes -l topology.kubernetes.io/zone=<az-name>` 应见该 AZ 节点陆续变为 `NotReady`；
> 3. **停止对被隔离节点的探入式取证**（exec/get 该 AZ 节点上的 Pod 都会 10s 超时，纯属浪费）——拿到多节点 NotReady 即可判定成功并收敛。
> 4. **需交叉验证连通性时，从健康 AZ 的节点/Pod 发起**（探目标 AZ 节点的 6443/10250），勿 exec 进被隔离节点。
>
> ⚠️ **超时是预期，但"成功"必须看节点，不能凭超时判定**：
> - exec/命令超时只说明"结果观测不到"，**本身不等于注入成功**；成功与否本质上要看**集群侧节点是否真的变 NotReady/Unknown**。
> - **给"超时=成功"加上界**：若连**集群侧 `kubectl get nodes -l topology.kubernetes.io/zone=<az-name>` 本身也超时**，判为**观测丢失（UNKNOWN）**——此时应**退避重试该查询**（通道多为间歇抖动，重试常能拿到结果），**不要**把后续超时继续累加计为成功。
> - **最终成功数只能取"最近一次成功的集群侧快照里已确认变 NotReady/Unknown 的节点数"**，其余节点标记为"未确认"，不得心算叠加、不得沿用注入前的基线快照。
> - **禁止从局部推断整体**：成功口径以**已注入的目标节点（目标 AZ 去掉 `EXCLUDED_NODES` 后的集合）**为分母——只有当集群侧快照覆盖到全部已注入节点且均异常，才可称"全部成功"；仍有未覆盖/仍 Ready 的已注入节点时，只能报"部分成功 N/已注入总数"。`EXCLUDED_NODES`（API Server/kubewiz-executor 节点）本就不在注入范围内、预期保持 Ready，**不得**计入失败或部分，也**不得**输出"集群整体不可用/全部节点已注入"。

1. 检查目标 AZ 节点状态（预期全部变为 NotReady，**这是首要且可靠的判据**）：
   ```bash
   kubectl get nodes -l topology.kubernetes.io/zone=<az-name>
   ```
2. 检查工作负载迁移情况：
   ```bash
   kubectl get pods -o wide -A | grep -E "Terminating|Pending|ContainerCreating"
   ```
3. 检查健康 AZ 是否承接了被驱逐的 Pod：
   ```bash
   kubectl get pods -o wide -l app=<关键服务标签> 
   ```
4. 检查事件：
   ```bash
   kubectl get events --field-selector reason=NodeNotReady
   ```

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
   AZ 网络分区场景下，DaemonSet 通道的 blade destroy 大概率无法下达（API Server 不可达）。
   依赖 `--timeout` 自动恢复。SSH 通道可逐节点 SSH 登录恢复。
2. 超时自动恢复后确认节点状态

**恢复验证**：
1. 确认所有节点恢复为 Ready：
   ```bash
   kubectl get nodes -l topology.kubernetes.io/zone=<az-name>
   ```
2. 确认跨 AZ 网络连通性恢复
3. 确认被驱逐的 Pod 已重新调度并 Running

**基准事实**：
- **根因**：目标可用区内所有节点宿主机网络栈被注入 iptables DROP 规则，模拟 AZ 级网络分区
- **必现现象**：AZ 内所有节点变为 NotReady；跨 AZ 流量中断；集群触发大规模 Pod 重新调度

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现可用区网络分区。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择当前集群已验证可拉取且包含 `chroot`/`sh` 的 debug 镜像；宿主机需包含 `sh`、`iptables` 和 `systemd`。宿主机变更必须使用 `--profile=sysadmin`。

每个节点均须先创建并等待 debug Pod Ready，再使用工具返回的实际 Pod 名称和命名空间执行 `kubectl exec`。禁止硬编码 Pod 名、使用 `-it` 或假定 `default` 命名空间。**每个节点（每一批）都必须用 `kubectl debug node` 新建属于自己的特权 debug Pod 来执行 `chroot`/`iptables` 等宿主机逃逸操作；严禁复用现成 Pod（如 chaosblade-tool DaemonSet Pod）跑逃逸命令——守卫只接受经 `kubectl debug node` 新建的特权 debug Pod，复用现成 Pod 会被拒绝。**以下循环表示编排语义，Agent 应以结构化 kubectl 工具调用来编排、不要提交 shell 循环；但这不意味着必须逐个串行——**可在同一回合内对多个目标节点并发发起结构化 kubectl 工具调用（执行引擎会并发执行，但必须小批量分批：每批并发节点数不超过 10 个，并按目标节点总数拆成多个小批次——例如 40 个节点拆成 5 批左右，一批全部完成后再发起下一批，切勿一次性并发全部节点）**。kubectl-native 通道每节点需独立 debug Pod，无法像 DaemonSet 通道那样用 `--names` 合并成一条命令，但可靠多工具调用并发达到同样的提速效果，故按“创建 debug Pod”→“exec 注入”两步分别扇出：**先对目标节点（目标 AZ 去掉 `EXCLUDED_NODES` 后）分批（每批不超过 10 个）并发创建并等待 debug Pod（拿回各自 Pod 名），再对这些 Pod 分批（每批不超过 10 个）并发下发 exec 注入**；批次大小按节点总数合理切分，节点越多批次越多，任何一批都不得超过 10 个节点；单节点内 create→exec 因依赖返回的 Pod 名仍需串行。顺序仍遵循同一约束——**排除节点 `EXCLUDED_NODES`（API Server/控制面节点、kubewiz-executor Pod 所在节点）禁止注入，须从节点列表中彻底剔除，不为其创建 debug Pod、不下发 exec**。若某个目标节点的 debug Pod 创建失败或 exec 异常，单独重试该节点即可，成功一个记一个，不因个别节点失败而放弃剩余节点。

---

**方案 A：端口级屏蔽（推荐，保留 SSH 恢复通道）**

仅屏蔽 K8s 控制面通信端口，保留 SSH(22) 可达，确保恢复通道不被切断。

注入命令：
```bash
# 1. 获取目标 AZ 所有节点；剔除排除节点 EXCLUDED_NODES（见"资源准备"：API Server/控制面节点、kubewiz-executor Pod 所在节点）
NODES=$(kubectl get nodes -l topology.kubernetes.io/zone=<az-name> -o jsonpath='{.items[*].metadata.name}')
TARGET_NODES="<NODES 去掉 EXCLUDED_NODES 后的列表>"

# 2. 遍历目标节点屏蔽 K8s 控制面端口（kubelet + API Server）；EXCLUDED_NODES 禁止注入
for NODE in $TARGET_NODES; do
  kubectl debug node/$NODE --profile=sysadmin --image=<verified-cluster-image> -- sleep 900
  # ⚠️ 关键顺序：先用 systemd-run 武装定时恢复（仅登记闹钟、不删规则），再下 DROP。
  #    DROP 会切断 10250/6443——本条 exec 依赖的通道，若恢复排在 DROP 后，exec 流会在闹钟登记前被切断 → 永不恢复。
  #    ✅ 注入后本条 exec 会因 6443/10250 被切断而超时（如 timed out after 10s）——这是预期成功信号，**不要重试该 exec、不要换镜像**；继续下一个节点，并改用集群侧 `kubectl get nodes -l topology.kubernetes.io/zone=<az-name>` 验证。
  kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
    systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-az sh -c "
      iptables -D OUTPUT -p tcp --dport 6443 -j DROP;
      iptables -D INPUT -p tcp --sport 6443 -j DROP;
      iptables -D INPUT -p tcp --dport 10250 -j DROP;
      iptables -D OUTPUT -p tcp --sport 10250 -j DROP" &&
    iptables -I OUTPUT -p tcp --dport 6443 -j DROP &&
    iptables -I INPUT -p tcp --sport 6443 -j DROP &&
    iptables -I INPUT -p tcp --dport 10250 -j DROP &&
    iptables -I OUTPUT -p tcp --sport 10250 -j DROP
  '
done
# 注意：EXCLUDED_NODES（API Server/控制面节点、kubewiz-executor Pod 所在节点）禁止注入，
#      保留为观测与 SSH 恢复支点，不纳入上面的循环。
```

端口说明：
- `6443`：API Server 端口，屏蔽后 kubelet 无法上报心跳，节点变为 NotReady
- `10250`：kubelet 端口，屏蔽后 kubectl exec/logs/debug 不可用
- SSH(22) **不受影响**，恢复时可直接 SSH 登录节点

恢复命令：

主恢复路径是注入时登记的 systemd 定时器，到期由宿主机 PID 1 自动执行 `iptables -D`，Agent 无需干预，也无需保持到该节点的连接。

**提前恢复必须人工带外执行 —— Agent 不执行下面的命令。** 注入切断的正是 kubectl 到该节点的路径，所以任何经集群 API 的恢复方式（`kubectl exec` / `kubectl debug node`）此刻都不可达。若确需提前恢复，请通过 SSH / 控制台 / IPMI 手动执行：

```text
# 对可用区内每个节点重复执行：
ssh root@<node-ip> 'iptables -D OUTPUT -p tcp --dport 6443 -j DROP; iptables -D INPUT -p tcp --sport 6443 -j DROP; iptables -D INPUT -p tcp --dport 10250 -j DROP; iptables -D OUTPUT -p tcp --sport 10250 -j DROP'
```

---

**方案 B：全量屏蔽 + 内置超时自恢复**

完全断网（模拟真实 AZ 分区），通过 systemd transient timer 定时自动恢复。

注入命令：
```bash
NODES=$(kubectl get nodes -l topology.kubernetes.io/zone=<az-name> -o jsonpath='{.items[*].metadata.name}')
TARGET_NODES="<NODES 去掉 EXCLUDED_NODES 后的列表>"

# 对目标节点注入全量 DROP 并启动 systemd 定时恢复；EXCLUDED_NODES 禁止注入
for NODE in $TARGET_NODES; do
  kubectl debug node/$NODE --profile=sysadmin --image=<verified-cluster-image> -- sleep 900
  kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
    systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-azfull sh -c "iptables -D OUTPUT -j DROP; iptables -D INPUT -j DROP" &&
    iptables -I OUTPUT -j DROP && iptables -I INPUT -j DROP
  '
done
# 注意：EXCLUDED_NODES 禁止注入，保留为观测与 SSH 恢复支点，不纳入上面的循环。
```

恢复机制：
- 超时到期后 systemd 自动执行 iptables -D，节点恢复 Ready
- 恢复进程由宿主机 systemd(PID 1) 管理，debug Pod 被删除也不影响
- 若需提前恢复且 SSH 可达：`ssh root@<node-ip> 'iptables -D OUTPUT -j DROP; iptables -D INPUT -j DROP'`

---

**降级方案选择指南**：

| 方案 | 真实度 | 恢复可靠性 | 前提条件 |
|------|--------|-----------|----------|
| A 端口级屏蔽 | 中（节点 NotReady，但非真正断网） | 高（SSH 始终可达） | SSH 访问权限 |
| B 全量 + 超时 | 高（完全断网） | 高（systemd timer 由 PID 1 管理） | 宿主机有 systemd |

注意事项：
- **默认选方案 A（端口级屏蔽，保住 SSH/观测通道）**；方案 B（全量断网）仅在已确认带外（SSH/systemd）恢复通道可靠时使用。
- systemd-run --on-active 创建的 transient timer 由宿主机 systemd 管理，不依赖 debug Pod 存活
- 建议超时设置 60-180 秒，避免长时间网络分区导致大量 Pod 重新调度后的状态混乱
- 生产环境强烈建议安装 ChaosBlade 后使用 DaemonSet 通道 + `--timeout` 执行 AZ 级网络分区
- 禁止使用无自恢复机制的全量 DROP 方案（可能导致节点永久锁死）
