**用例名称** 网络隔离 导致 Pod_网络故障

**故障现象**：
1. 目标 Pod 的网络流量被丢弃，受影响的方向取决于注入时的 `--network-traffic`：
   - 不指定（双向）：所有出入流量被丢弃，Pod 完全无法通信
   - `out`：仅出向被丢弃，Pod 发起的调用全部超时；**外部仍能访问该 Pod**
   - `in`：仅入向被丢弃，外部访问该 Pod 超时；**Pod 自身发起的出向调用仍正常**
2. 入向被阻断时（未指定方向或 `in`），httpGet/tcpSocket 探针无响应 → Pod 被重启或标记 NotReady → Service Endpoints 移除该 Pod
3. 同节点其他 Pod 不受影响（非 hostNetwork 模式下各 Pod 有独立网络命名空间）

**资源准备**：
1. 确认目标应用已正常运行，且有对外网络调用
2. 确认目标 Pod 的标签选择器和命名空间
3. 确认目标 Pod 不是 hostNetwork 模式（hostNetwork Pod 共享宿主机网络栈，无法单独隔离）

**演练步骤**：
1. 确认目标 Pod 的标签选择器和命名空间：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```
2. 确认目标 Pod 非 hostNetwork 模式：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.hostNetwork}'
   ```
   空输出或 `false` 表示正常；`true` 表示 hostNetwork，不适用本场景
3. 使用 ChaosBlade 对目标 Pod 注入全量网络丢包（完全断网）：
   ```bash
   blade create k8s pod-network drop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --timeout <seconds> \
     --kubeconfig <kubeconfig-path>
   ```
   - 不指定 `--source-port`/`--destination-port`/`--source-ip`/`--destination-ip` 时为全量丢包
   - `--timeout`：实验自动过期时间（秒），到期后 ChaosBlade 自动撤销规则
   - `--network-traffic`：可选，指定方向 `in`（入方向丢包）或 `out`（出方向丢包），不指定则双向丢包
4. 记录返回的 blade_uid，用于后续恢复

**注入验证**：
1. **（主证据，必做）** 确认实验已生效：
   ```bash
   blade status --uid <blade_uid>
   ```
   状态为 Success/Running 即表示丢包规则已下到目标 Pod 的网络命名空间。
2. **（只做与本次 `--network-traffic` 匹配的分支）** 其余方向的现象在本次注入下**不可能出现**，直接标记为 `expected` 并跳过：
   - **未指定方向（双向）**：a) Pod 内出向不通；b) 外部打进来不通（两条都做）
   - **`out`（仅出向）**：只做 a)。**外部仍能访问该 Pod 是预期，不是失败**
   - **`in`（仅入向）**：只做 b)。**Pod 内出向仍通是预期，不是失败**

   a) Pod 内验证出向：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <目标服务地址>
   ```
   b) 从其他 Pod 验证入向：
   ```bash
   kubectl exec <test-pod> -n <namespace> -- wget -qO- --timeout=5 http://<target-pod-ip>:<port>
   ```
3. **（仅当入向被阻断，即未指定方向或 `in`）** 检查就绪状态与 Endpoints：
   ```bash
   kubectl get pod <pod-name> -n <namespace>
   kubectl get endpoints <service-name> -n <namespace>
   ```
   预期 READY 变为 0/1、Endpoints 移除该 Pod。以下情形**属预期，不是失败**：
   - 探针是 **exec** 型：不走网络，kubelet 直接在容器内执行命令，丢包规则对它无效 → Pod 保持 Ready、Endpoints 不移除
   - 注入带了 `--source-port`/`--destination-port` 且未覆盖探针端口 → 探针流量不被丢弃，同上

> ⚠️ 验证纪律：
> - **严禁为不适用的方向/范围反复更换查询方式找证据**。注入 `out` 时入向必然通、注入 `in` 时出向必然通，查到"通"是**必然**而非失败。
> - 若注入带了 `--source-ip`/`--destination-ip`/`--source-port`/`--destination-port`，只有匹配该过滤条件的流量被丢弃。用不匹配的地址或端口测试必然连通，**不要换目标反复重试**——应改用匹配过滤条件的目标验证。
> - 同一事实（如实验是否生效）确认一次即可，不要重复查询。
> - `kubectl get pod -w` 会持续阻塞，验证时用不带 `-w` 的单次查询。

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <blade_uid>
   ```
2. 如 Pod 因健康检查失败被重启，等待新 Pod Ready

**恢复验证**：
1. 在目标 Pod 内重新验证网络连通性恢复：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- wget -qO- --timeout=5 <目标服务地址>
   ```
2. 确认 Pod 恢复 Ready 状态
3. 确认 Service Endpoints 重新包含该 Pod

**基准事实**：
- **根因**：Pod 网络命名空间内 iptables 链被设置 DROP 规则，匹配的数据包被丢弃（`out` 作用于 OUTPUT、`in` 作用于 INPUT、不指定则两者都下）
- **必现现象（与方向无关）**：实验状态为 Success/Running；被阻断方向的流量全部超时
- **随方向变化的现象**：
  - 双向：Pod 出入均不通
  - `out`：出向不通，入向仍可达
  - `in`：入向不通，出向仍正常
- **探针依赖**：就绪探针失败 → Endpoints 移除，仅在**入向被阻断且探针为 httpGet/tcpSocket 且端口在丢包范围内**时成立；exec 探针不走网络，不受影响
- **作用域边界**：仅影响目标 Pod 的网络命名空间，同节点其他 Pod 不受影响（非 hostNetwork 模式）

---

**降级方案（kubectl-native）**

> 当 ChaosBlade 不可用时，通过 debug Pod + nsenter 进入目标 Pod 网络命名空间实现隔离。

前提条件：集群支持 `kubectl debug node`（K8s 1.18+）；目标 Pod 非 hostNetwork 模式；debug 镜像需包含 `chroot`/`sh`；宿主机需包含 `iptables` 和 `systemd`

> **⚠️ target_guard 说明**：`iptables` 属于 fault binary，`chroot`/`nsenter` 属于 escape primitive，均会触发 carrier resolution。以下命令必须在 privileged debug Pod 上执行才能通过安全检查；直接对普通业务 Pod 执行 `kubectl exec <pod> -- iptables` 会被 target_guard 拦截。

注入命令：
```bash
# 1. 在目标 Pod 所在节点创建 debug Pod
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- sleep 900

# 2. 获取目标 Pod 容器 PID，nsenter 进入其网络命名空间执行 iptables
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  PID=$(crictl inspect $(crictl ps --name <container-name> --namespace <namespace> -q | head -1) -o json 2>/dev/null | grep -o "\"pid\":[0-9]*" | head -1 | cut -d: -f2)
  # 先武装定时恢复（$PID 此刻由外层 shell 展开并固化进 timer），再注入 DROP，与宿主机类用例保持一致的“恢复先于自断”规范。
  # 注意：武装块与各条注入之间必须用 && 串联（不能仅靠换行分隔），
  # 否则 target_guard 的 iptables 正反规则配对解析会把后续 -A 规则误并入前一条 -D 捕获，导致校验失败被拦截。
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-podiso sh -c "nsenter -t $PID -n iptables -D OUTPUT -j DROP; nsenter -t $PID -n iptables -D INPUT -j DROP" &&
  nsenter -t $PID -n iptables -A OUTPUT -j DROP &&
  nsenter -t $PID -n iptables -A INPUT -j DROP
'
```

恢复命令：
```bash
# 通过 debug Pod 手动恢复（如 systemd timer 尚未到期）
kubectl exec <debug-pod> -n <debug-namespace> -- chroot /host sh -c '
  PID=$(crictl inspect $(crictl ps --name <container-name> --namespace <namespace> -q | head -1) -o json 2>/dev/null | grep -o "\"pid\":[0-9]*" | head -1 | cut -d: -f2)
  nsenter -t $PID -n iptables -D OUTPUT -j DROP
  nsenter -t $PID -n iptables -D INPUT -j DROP
'
```

注意事项：
- nsenter 进入的是 Pod 的网络命名空间（`-n`），不是宿主机命名空间，操作仅影响目标 Pod
- systemd-run 创建的 timer 由宿主机 PID 1 管理，debug Pod 被删除也不影响恢复
- 若目标 Pod 容器重启，PID 变化会导致 systemd timer 中的 nsenter 失败，但网络命名空间重建后规则也自然消失
- `crictl ps` 需要 `--namespace` 过滤确保匹配到正确 Pod 的容器
