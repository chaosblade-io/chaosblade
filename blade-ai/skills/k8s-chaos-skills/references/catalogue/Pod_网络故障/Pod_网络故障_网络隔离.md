**用例名称** 网络隔离 导致 Pod_网络故障

**故障定位**：持续型故障——Pod 网络命名空间内的 iptables DROP 规则是状态型故障，
规则存活即故障存活，贯穿整个故障窗口；窗口到期自删链/实验销毁即自动恢复。
手段1（ChaosBlade）与手段2（kubectl-native）是**并列的注入手段**，底层效果完全
等价（blade 内部也是在目标 Pod netns 下发 iptables DROP 规则），按环境能力选用：
集群装有 ChaosBlade 且 operator Ready、`pod-network` 提供 drop action → 可用手段1
（实验 UID 统一生命周期管理）；否则用手段2。`duration_seconds` 是必填的故障窗口
契约，未给定时先向用户确认。

**故障现象**：
1. 目标 Pod 的网络流量被丢弃，受影响的方向取决于注入时的方向参数（手段1 `--network-traffic`、手段2 规则选择）：
   - 双向（默认）：所有出入流量被丢弃，Pod 完全无法通信
   - `out`（仅出向）：Pod 发起的调用全部超时；**外部仍能访问该 Pod**
   - `in`（仅入向）：外部访问该 Pod 超时；**Pod 自身发起的出向调用仍正常**
2. 入向被阻断时（双向或 `in`），httpGet/tcpSocket 探针无响应 → Pod 被重启或标记 NotReady → Service Endpoints 移除该 Pod
3. 同节点其他 Pod 不受影响（非 hostNetwork 模式下各 Pod 有独立网络命名空间）

**资源准备**：
1. 确认目标应用已正常运行，且有对外网络调用
2. 确认目标 Pod 的标签选择器、命名空间，以及**实际容器名**（手段2 路径B 临时容器
   `--target` 必须填容器名，填 Pod 名/服务名会被 API server 拒绝：`targetContainerName: Not found`）
3. 确认目标 Pod 不是 hostNetwork 模式（hostNetwork Pod 共享宿主机网络栈，无法单独隔离；
   且临时容器里的 iptables 会打穿整个节点）：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.hostNetwork}'
   ```
   空输出或 `false` 表示正常；`true` 表示 hostNetwork，不适用本场景，立即停止并 replan
4. **能力探测（决定手段选择）**——手段1 需要两个条件同时成立，**以当次探测为准**：
   - operator 就绪：`kubectl get pods -n chaosblade -l name=chaosblade-operator`（或对应 label）为 Running；
     0/1 Ready 即判死，不得重试等待
   - drop action 可用：`blade create k8s pod-network -h` 的 Available Commands 列出 `drop`
   两者其一不满足 → 用手段2
5. 手段2 额外前提：
   - 路径A 需要目标容器自带**真 iptables** 且容器持 NET_ADMIN（`CapEff` 全零会报 EPERM）——
     精简镜像的常态是不满足，直接走路径B
   - 路径B 需要集群**能拉取**一个含 iptables 二进制的镜像（CNI 镜像如 terway/calico/cilium
     通常自带；用 `kubectl get pods -A -o jsonpath='{{..image}}'` 找集群已在用的，
     探测该镜像内 `iptables --version` 可用后再使用）

**演练步骤**：
1. 记录注入前基线：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   ```

**手段1（ChaosBlade）**

2. 注入全量网络丢包：
   ```bash
   blade create k8s pod-network drop \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --timeout <duration>
   ```
   - 不指定 `--source-port`/`--destination-port`/`--source-ip`/`--destination-ip` 时为全量丢包
   - `--timeout`：实验自动过期时间（秒），到期后 ChaosBlade 自动撤销规则
   - `--network-traffic`：可选，`in`（仅入方向丢包）或 `out`（仅出方向丢包），不指定则双向丢包
3. 记录返回的 experiment_uid，用于后续恢复

**手段2（kubectl-native）** —— 按容器内是否有可用的 iptables 二选一

> **作用域纪律**：两条路径都全程留在目标 Pod 作用域内（路径A 在目标容器内、
> 路径B 用共享目标 netns 的临时容器），与批准的 pod 目标一致，target_guard 放行。
> **禁止**改走 `kubectl debug node/<node>` + `chroot /host` + `nsenter` 的宿主机路径——
> 该形态被 target_guard 分类为 node 作用域，在 pod 批准下结构性不可达
> （REJECT_DRIFT: blade network targets node under pod approval，集群全拒）。

**路径 A —— 容器内有 iptables 且持 NET_ADMIN**

先判定容器内的 `iptables` 是不是真的能用（BusyBox 精简镜像通常没有；存在但 `CapEff`
无 NET_ADMIN 时注入会报 `Permission denied`，转路径B）：
```bash
kubectl exec <pod-name> -n <namespace> -- iptables --version
```

注入命令（**先武装定时自删，再注入规则**；两条命令分两次独立执行——不能用 && 串联：
第二段 kubectl 会沦为第一条 exec 载荷（sh -c）的死参数，注入静默丢失）：
```bash
kubectl exec <pod-name> -n <namespace> -- sh -c \
  '( sleep <duration>; iptables -D OUTPUT -j DROP; iptables -D INPUT -j DROP ) >/dev/null 2>&1 &'
kubectl exec <pod-name> -n <namespace> -- sh -c \
  'iptables -A OUTPUT -j DROP && iptables -A INPUT -j DROP && iptables -S'
```
末尾 `iptables -S` 即注入时刻的规则表自证快照（应见 `-A OUTPUT -j DROP` 与 `-A INPUT -j DROP`）。

**路径 B —— 容器内没有可用 iptables（精简镜像的常态）：临时容器载体**

临时容器与目标容器**共享同一个网络命名空间**，在其内对 iptables 操作等价于操作目标 Pod；
`iptables` 来自调试镜像而非目标镜像，`--profile=netadmin` 提供 NET_ADMIN capability。

```bash
# 0) 前置安全检查：确认目标 Pod 不是 hostNetwork（资源准备第 3 条）。
#    hostNetwork=true 时禁止此路径，临时容器里的 iptables 会打穿整个节点。

# 1) 注入 + 内置定时自删（一条命令完成：注入成功后 sleep <duration> 到期自动删除规则）。
#    --target 必须填实际容器名；--quiet 不进入交互附着，不要加 -it。
kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
  --target=<container-name> --profile=netadmin --quiet -- sh -c \
  'iptables -A OUTPUT -j DROP && iptables -A INPUT -j DROP \
   && iptables -S && echo INJECTED && sleep <duration> \
   && iptables -D OUTPUT -j DROP && iptables -D INPUT -j DROP \
   && iptables -S && echo RECOVERED'
```
链条是**自证的**：`-A` 后、`INJECTED` 前的 `iptables -S` 把生效规则原文写入容器日志，
`-D` 后、`RECOVERED` 前的 `iptables -S` 把回落后的规则表写入同一日志——白盒主证
在注入时刻即被捕获，验证阶段读日志即可取证，**不再受故障窗口是否已关闭的时序约束**
（窗口短于验证启动延迟时，窗口内探针结构性不可达；链内快照消除此竞态）。两处 `-S` 必须保留，
不得为缩短命令而省略（注意 wiz 等通道 sh -c 载荷有 1024 字节上限，本链含占位符约 360 字节，安全）。
倒计时从注入成功时刻起算：`INJECTED` 回显与倒计时起点在同一条命令链内严格串行，无侵蚀间隙；
武装后发生任何修复需重新注入时，先用下方提前恢复命令另起临时容器删规则（旧链到期后的重复删除是幂等空触发、无害），再重跑注入命令（见 SKILL.md 安全红线「故障窗口完整」）
- 方向适配：仅出向隔离去掉两处 INPUT 规则（`-A INPUT -j DROP` / `-D INPUT -j DROP`）；仅入向同理去掉 OUTPUT
- 该命令整体阻塞 `<duration>` 秒（命令自身就是保活载体，自恢复随命令完成而闭环）；
  如需后台执行，将整条 kubectl debug 置于后台并轮询其输出/临时容器日志
- 输出未直接回流时，从临时容器日志读取（`INJECTED`/`RECOVERED` 标记即注入/自恢复的确证；
  标记之间的 `iptables -S` 输出即白盒主证原文）：
  ```bash
  kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.ephemeralContainers[-1].name}'
  kubectl logs <pod-name> -n <namespace> -c <ephemeral-container-name>
  ```

手段2 参数含义（两条路径相同）：
- `-A OUTPUT -j DROP`：出向全部丢弃（对应手段1 `--network-traffic out`）
- `-A INPUT -j DROP`：入向全部丢弃（对应手段1 `--network-traffic in`）
- 两条同下 = 双向（手段1 不指定 `--network-traffic`）
- **注入报 `another app is currently holding the xtables lock`**：目标 netns 的规则表被其他写者持锁
  （如 CNI 组件正在改规则）。加等待重试一次：`iptables -w 5 -A ...`；仍失败则取证
  `iptables -S` 当前规则表后 replan，不要盲目反复重试
- **注入报 `Permission denied` / `Operation not permitted`**：载体缺 NET_ADMIN——路径A 说明
  目标容器无该 capability，转路径B；路径B 说明 `--profile=netadmin` 未生效或镜像异常，如实报告

**注入验证**（两种手段共用——底层是同一组 iptables DROP 规则）：
1. 白盒确认 DROP 规则已生效（主证，必做）：
   - 手段1：`blade status --uid <experiment_uid>` 状态为 Success/Running 即表示丢包规则已下到目标 Pod netns
   - 手段2 路径B：**首选证据是注入容器日志**——`kubectl logs <pod-name> -n <namespace> -c <ephemeral-container-name>`，
     `INJECTED` 之前的 `iptables -S` 输出应含 `-A OUTPUT -j DROP`（及/或 `-A INPUT -j DROP`）原文；
     `RECOVERED` 之前的输出应不再含对应 DROP 规则
   - 手段2 路径A：注入命令的输出即注入时刻快照；需要**当前**规则状态时在目标容器内再执行
     `kubectl exec <pod-name> -n <namespace> -- iptables -S`（路径B 同理可另起临时容器复验）
2. **（只做与本次注入方向匹配的分支）** 其余方向的现象在本次注入下**不可能出现**，直接标记为 `expected` 并跳过：
   - **双向**：a) Pod 内出向不通；b) 外部打进来不通（两条都做）
   - **`out`（仅出向）**：只做 a)。**外部仍能访问该 Pod 是预期，不是失败**
   - **`in`（仅入向）**：只做 b)。**Pod 内出向仍通是预期，不是失败**

   a) Pod 内验证出向（wget/nslookup/curl 按容器内实际可用者选，探测命令包进 sh -c 载荷）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c 'wget -qO- --timeout=5 <目标服务地址>'
   ```
   b) 从其他 Pod 验证入向：
   ```bash
   kubectl exec <test-pod> -n <namespace> -- wget -qO- --timeout=5 http://<target-pod-ip>:<port>
   ```
3. **（仅当入向被阻断，即双向或 `in`）** 检查就绪状态与 Endpoints：
   ```bash
   kubectl get pod <pod-name> -n <namespace>
   kubectl get endpoints <service-name> -n <namespace>
   ```
   预期 READY 变为 0/1、Endpoints 移除该 Pod。以下情形**属预期，不是失败**：
   - 探针是 **exec** 型：不走网络，kubelet 直接在容器内执行命令，丢包规则对它无效 → Pod 保持 Ready、Endpoints 不移除
   - 注入带了端口过滤（手段1 `--source-port`/`--destination-port`）且未覆盖探针端口 → 探针流量不被丢弃，同上
   - 目标 Pod 没有配置探针/不挂 Service（如纯 sleep 靶 Pod）→ 无 Endpoints 现象可观察，跳过本条

> ⚠️ 验证纪律：
> - **严禁为不适用的方向/范围反复更换查询方式找证据**。注入 `out` 时入向必然通、注入 `in` 时出向必然通，查到"通"是**必然**而非失败。
> - 若注入带了 `--source-ip`/`--destination-ip`/`--source-port`/`--destination-port`，只有匹配该过滤条件的流量被丢弃。用不匹配的地址或端口测试必然连通，**不要换目标反复重试**——应改用匹配过滤条件的目标验证。
> - 同一事实（如实验是否生效）确认一次即可，不要重复查询。
> - `kubectl get pod -w` 会持续阻塞，验证时用不带 `-w` 的单次查询。

**持续性检查（必做）**——故障窗口内故障必须持续存活，而非注入一次即消失：
1. 手段1：窗口中段复查 `blade status --uid <experiment_uid>` 仍为 Success/Running
2. 手段2 路径A：窗口中段在目标容器内复查 `iptables -S` 仍含 DROP 规则
3. 手段2 路径B：以日志为准——`INJECTED` 已现而 `RECOVERED` 未现即窗口仍开、规则仍活
4. 窗口内连通性探测与白盒复查至少各一次（方向匹配的那一侧）

**注入恢复**：

手段1（ChaosBlade）：
1. 提前恢复：销毁实验（移除 DROP 规则）`blade destroy <experiment_uid>`
2. 或等待 `--timeout`（`<duration>`）到期后 ChaosBlade 自动恢复。
   注意：到期后 `blade status <uid>` 可能仍显示 `Success` 不翻状态，**不要以 status 判断规则是否还在**，
   以白盒复验（`iptables -S`）或连通性探测为准
3. 如 Pod 因健康检查失败被重启，等待新 Pod Ready

手段2（kubectl-native）：
1. 主保险：路径A 武装的后台自删 / 路径B 内置自删链到期自动移除规则
   （路径B 的自删与注入同在一个临时容器进程内，随命令完成而闭环；
   临时容器被删但 sleep 未走完时规则会残留，用下方提前恢复兜底）
2. 提前恢复：
   - 路径A：`kubectl exec <pod-name> -n <namespace> -- sh -c 'iptables -D OUTPUT -j DROP; iptables -D INPUT -j DROP; iptables -S'`
   - 路径B：另起一个临时容器执行删除，与注入容器无关——iptables 规则挂在共享 netns 上，
     任何持 NET_ADMIN 进入该 netns 的载体都能删：
     ```bash
     kubectl debug <pod-name> -n <namespace> --image=<verified-cluster-image> \
       --target=<container-name> --profile=netadmin --quiet -- sh -c \
       'iptables -D OUTPUT -j DROP; iptables -D INPUT -j DROP; iptables -S'
     ```
   （方向过滤注入时只删对应方向的规则；`-D` 对不存在的规则报错无害但需预期）

**恢复验证**（两种手段共用）：
1. 白盒确认 DROP 规则已移除：手段2 路径B 首选注入容器日志（`RECOVERED` 之前的 `iptables -S`
   不再含对应 DROP 规则）；到期自动恢复场景或需当前状态时，按提前恢复形态另起载体复验
   `iptables -S`；手段1 可辅以 `blade status <uid>` 但以其不可靠著称，最终以规则表/连通性为准
2. 在目标 Pod 内重新验证出向连通性恢复：
   ```bash
   kubectl exec <pod-name> -n <namespace> -- sh -c 'wget -qO- --timeout=5 <目标服务地址>'
   ```
3. **（仅当入向曾被阻断）** 确认 Pod 恢复 Ready 状态、Service Endpoints 重新包含该 Pod
4. 确认目标 Pod 无重启（`RESTARTS` 与基线一致）

**基准事实**：
- **根因**：Pod 网络命名空间内 iptables 链被设置 DROP 规则，匹配的数据包被丢弃（`out` 作用于 OUTPUT、`in` 作用于 INPUT、双向则两者都下）
- **必现现象（与方向无关）**：白盒规则表可见 DROP 规则（手段1 以 blade status 为辅证）；被阻断方向的流量全部超时
- **随方向变化的现象**：
  - 双向：Pod 出入均不通
  - `out`：出向不通，入向仍可达
  - `in`：入向不通，出向仍正常
- **探针依赖**：就绪探针失败 → Endpoints 移除，仅在**入向被阻断且探针为 httpGet/tcpSocket 且端口在丢包范围内**时成立；exec 探针不走网络，不受影响
- **作用域边界**：仅影响目标 Pod 的网络命名空间，同节点其他 Pod 不受影响（非 hostNetwork 模式）

**手段2 注意事项**：
- 两条路径操作的都是目标 Pod 的网络命名空间（路径A 容器内直接操作；路径B 临时容器 `--target` 共享同一 netns），作用范围仅目标 Pod
- 目标 netns 内 `iptables -S` 用的是**载体镜像**的二进制（路径B 是调试镜像，非目标镜像），无需目标镜像自带 iptables
- 若目标 Pod 容器重启，网络命名空间重建，规则自然消失（故障自愈，如实报告即可）
- 自恢复基于路径A 武装的后台定时删除或路径B 注入命令内置的 `sleep <duration>` 自删链；
  二者都不依赖宿主机 systemd/timer，也不产生需要额外管理的临时单元
