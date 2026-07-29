**用例名称** 节点网络隔离 导致 Node_网络故障

**故障现象**：
1. 节点上所有 Pod 的网络通信中断（或指定方向/端口的流量被屏蔽）
2. 该节点上的 Pod 健康检查失败，可能触发驱逐和重新调度
3. kubelet 与 API Server 通信中断时，节点状态变为 NotReady
4. 影响范围为整个节点（宿主机网络栈），不限于单个 Pod

**资源准备**：
1. 确认目标节点名称及其上运行的关键工作负载
2. 确认集群有足够冗余节点承载被驱逐的 Pod
3. 确认 ChaosBlade Operator 已部署（DaemonSet 通道）或具备节点 SSH 访问权限（SSH 通道）

**演练步骤**：
1. 确认目标节点名称和当前状态：
   ```bash
   kubectl get nodes
   kubectl get pods -o wide --field-selector spec.nodeName=<node-name>
   ```
2. 选择执行通道并注入节点网络屏蔽：

   **方式一：DaemonSet 通道**（⚠️ 注入后可能无法通过 K8s API 恢复，必须指定 `--timeout`）
   ```bash
   blade create k8s node-network drop \
     --names <node-name> \
     --destination-port <port> \
     --network-traffic out \
     --timeout 60 \
     --kubeconfig <kubeconfig-path>
   ```

   **方式二：SSH 通道**（推荐，恢复更可靠）
   ```bash
   blade create k8s node-network drop \
     --source-port <port> \
     --network-traffic in \
     --channel ssh \
     --ssh-host <node-ip> \
     --ssh-user root \
     --timeout 60
   ```
   - `--destination-port`/`--source-port`：限定屏蔽端口范围（不指定则全量屏蔽，慎用）
   - `--network-traffic`：`in`（入站）或 `out`（出站）
   - `--timeout`：必须指定，超时后自动恢复
3. 记录返回的 blade_uid，用于后续恢复

**注入验证**：

> ⚠️ **自断链路故障的判读规范（务必先读）**：**当屏蔽范围覆盖 6443/10250 时**（不指定端口的全量屏蔽必然覆盖），被切断的正是 `kubectl exec` / kubewiz 依赖的通道。因此**注入用的那条 exec 命令自身会超时或断连（如 `task timed out after 10s`），这是预期的成功信号，不是失败**。收到超时后：
> 1. **不要重试同一条 exec，也不要归因为"镜像没 shell / 通道坏了"**——通道正是被你成功切断的；
> 2. **立即改从集群侧（apiserver 视角）验证**，而不是继续探入该节点：`kubectl get nodes <node-name>` 应变为 `NotReady`，`kubectl describe node` 的 Condition 出现 `Kubelet stopped posting node status`；
> 3. **停止对被隔离节点的探入式取证**（`kubectl exec`/`get` 该节点上的 Pod 都会 10s 超时，纯属浪费）——节点侧探测超时本身即佐证，拿到 NotReady + kubelet 失联即可判定成功并收敛。
>
> 若屏蔽范围**不覆盖** 6443/10250（即指定了业务端口），则控制面通道完好，上述自断现象与 NotReady **都不会出现**，按下方分支验证。

1. **（主证据，必做）** 确认实验已生效：
   ```bash
   blade status --uid <blade_uid>
   ```
   状态为 Success/Running 即表示屏蔽规则已下到节点网络栈。
2. **（只做与本次屏蔽范围匹配的分支）** 另一分支的现象在本次注入下**不可能出现**，直接标记为 `expected` 并跳过：
   - **全量屏蔽（未指定 `--destination-port`/`--source-port`）**：检查节点状态，**这是首要且可靠的判据**
     ```bash
     kubectl get nodes <node-name>
     ```
     预期变为 `NotReady`；`kubectl describe node <node-name>` 可见 `Kubelet stopped posting node status`。此条成立即可判定成功并收敛。
   - **端口级屏蔽（指定了端口且不含 6443/10250）**：**节点不会 NotReady，不要去等、也不要因为 Ready 就判失败**。改为验证被屏蔽端口不可达（见第 3 步），节点保持 `Ready` 是预期。
3. **（按本次 `--network-traffic` 选择测试发起侧）** 验证被屏蔽端口的连通性：
   - **`out`（出站屏蔽）**：从被隔离节点侧向外访问不通。若控制面通道未被切断可用 debug Pod 发起；已被切断则跳过（节点侧探测超时本身即佐证）
   - **`in`（入站屏蔽）**：**从其它正常节点上的 Pod 发起**，不要 exec 进被隔离节点
     ```bash
     kubectl exec <其它节点上的pod> -n <namespace> -- wget -qO- --timeout=5 http://<被隔离节点IP>:<被屏蔽端口>
     ```
     预期连接超时/拒绝。**必须用被屏蔽的那个端口测试**——用未屏蔽端口测必然连通，那是预期，不是失败。
4. **（仅全量屏蔽时）** 检查节点上 Pod 的状态与事件：
   ```bash
   kubectl get pods -o wide --field-selector spec.nodeName=<node-name>
   kubectl get events --field-selector involvedObject.name=<node-name>
   ```
   预期部分 Pod 进入 Terminating/Unknown。端口级屏蔽下 kubelet 正常上报，**Pod 状态不变是预期**，不要查。

> ⚠️ 验证纪律：
> - **严禁为不适用的分支反复更换查询方式找证据**。端口级屏蔽时 NotReady、Pod Terminating、kubelet 失联**都不会出现**，查不到是**必然**而非失败。
> - 连通性测试必须落在**被屏蔽的端口与方向**上；换端口或换方向重试只会得到"连通"，纯属浪费。
> - 同一事实（如节点是否 NotReady）确认一次即可，不要重复查询。

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
   若使用 DaemonSet 通道且网络已中断无法通过 API 恢复，等待 `--timeout` 自动恢复
2. SSH 通道可直接通过 SSH 登录节点执行恢复

**恢复验证**：
1. 确认节点状态恢复为 Ready：
   ```bash
   kubectl get nodes <node-name>
   ```
2. 确认节点上 Pod 网络连通性恢复
3. 确认被驱逐的 Pod 已重新调度并 Running

**基准事实**：
- **根因**：节点宿主机网络栈被注入 iptables DROP 规则，指定方向/端口的所有流量被丢弃，模拟网络分区或节点隔离场景
- **必现现象**：节点上 Pod 网络中断；节点可能变为 NotReady（全量屏蔽时）；Pod 健康检查失败触发驱逐

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现节点网络隔离。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择当前集群已验证可拉取且包含 `chroot`/`sh` 的 debug 镜像；宿主机需包含 `sh`、`iptables` 和 `systemd`。宿主机变更必须使用 `--profile=sysadmin`。

所有方案均分两步执行：先创建并等待 debug Pod Ready，再使用工具返回的**实际 Pod 名称和命名空间**执行 `kubectl exec`。禁止硬编码生成的 Pod 名，禁止使用 `-it`，也不要假定 Pod 位于 `default`。

---

**方案 A：端口级屏蔽（推荐，保留 SSH 恢复通道）**

仅屏蔽 K8s 控制面或业务端口，SSH(22) 不受影响，确保恢复通道不被切断。

注入命令：
```bash
# 1. 创建有权限的非交互 debug Pod，并等待工具确认 Ready
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- sleep 900

# 2. 使用上一步返回的 <debug-pod> 和 <debug-namespace> 注入。
#    ⚠️ 关键顺序：必须先用 systemd-run 武装定时恢复（此刻只登记闹钟、不删任何规则），
#    再下 DROP 规则。因为 DROP 会切断 10250/6443——正是本条 exec 依赖的通道，
#    一旦恢复武装排在 DROP 之后，exec 流会在闹钟登记前被切断，导致定时器永远没上膛、
#    节点永不恢复。恢复链用 `;` 而非 `&&`，保证每条 -D 都被尝试（某条规则不存在也不中断后续）。
#    systemd-run 创建的 transient timer 由宿主机 systemd(PID 1) 管理，
#    不依赖 debug Pod 存活——debug Pod 被删除后恢复仍然生效。
#    ✅ 注入后本条 exec 会因 6443/10250 被切断而超时（如 task timed out after 10s）——
#    这是预期的成功信号，**不要重试该 exec、不要换镜像**；立即改用集群侧 `kubectl get nodes <node-name>`（应 NotReady）验证。
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-netiso sh -c "
    iptables -D OUTPUT -p tcp --dport 6443 -j DROP;
    iptables -D INPUT -p tcp --sport 6443 -j DROP;
    iptables -D INPUT -p tcp --dport 10250 -j DROP;
    iptables -D OUTPUT -p tcp --sport 10250 -j DROP" &&
  iptables -I OUTPUT -p tcp --dport 6443 -j DROP &&
  iptables -I INPUT -p tcp --sport 6443 -j DROP &&
  iptables -I INPUT -p tcp --dport 10250 -j DROP &&
  iptables -I OUTPUT -p tcp --sport 10250 -j DROP
'

# 或：屏蔽指定业务端口（模拟特定服务不可达）——同样先武装再屏蔽
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c \
  'systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-port sh -c "iptables -D OUTPUT -p tcp --dport <port> -j DROP" && iptables -I OUTPUT -p tcp --dport <port> -j DROP'
```

恢复命令：

主恢复路径是注入时登记的 systemd 定时器，到期由宿主机 PID 1 自动执行 `iptables -D`，Agent 无需干预，也无需保持到该节点的连接。

**提前恢复必须人工带外执行 —— Agent 不执行下面的命令。** 注入切断的正是 kubectl 到该节点的路径，所以任何经集群 API 的恢复方式（`kubectl exec` / `kubectl debug node`）此刻都不可达。若确需提前恢复，请通过 SSH / 控制台 / IPMI 手动执行：

```text
ssh root@<node-ip> 'iptables -D OUTPUT -p tcp --dport 6443 -j DROP; iptables -D INPUT -p tcp --sport 6443 -j DROP; iptables -D INPUT -p tcp --dport 10250 -j DROP; iptables -D OUTPUT -p tcp --sport 10250 -j DROP'

# 或端口级：
ssh root@<node-ip> 'iptables -D OUTPUT -p tcp --dport <port> -j DROP'
```

---

**方案 B：全量屏蔽 + 内置超时自恢复**

完全断网（模拟真实节点隔离），通过 systemd transient timer 定时自动恢复。

注入命令：
```bash
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- sleep 900
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-netfull sh -c "iptables -D OUTPUT -j DROP; iptables -D INPUT -j DROP" &&
  iptables -I OUTPUT -j DROP && iptables -I INPUT -j DROP
'
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
- systemd-run --on-active 创建的 transient timer 由宿主机 systemd 管理，不依赖 debug Pod 存活
- 建议超时设置 30-120 秒，单节点故障影响面可控
- 禁止使用无自恢复机制的全量 DROP 方案（可能导致节点永久失联）
