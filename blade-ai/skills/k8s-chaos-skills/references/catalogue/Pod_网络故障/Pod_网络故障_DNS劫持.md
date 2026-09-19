**用例名称** DNS劫持 导致 Pod_网络故障

**故障定位**：持续型故障——/etc/hosts 劫持记录是状态型故障，记录存活即故障存活，
贯穿整个故障窗口；窗口结束记录移除（实验销毁/定时器还原）即自动恢复。手段1（ChaosBlade）
与手段2（kubectl-native）是**并列的注入手段**，底层效果完全等价（blade 内部也是向目标
Pod 的 /etc/hosts 写入劫持记录），按环境能力选用：集群装有 ChaosBlade 且 operator 健康、
`pod-network` 提供 dns action → 可用手段1（实验 UID 统一生命周期管理）；否则用手段2。
手段2 内部再按目标形态二选一：工作负载管理且可滚动重建 → 路径 A（改 hostAliases）；
裸 Pod/无主形态或不可滚动重建 → 路径 B（容器内直改 /etc/hosts）。
`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

**故障现象**：
1. Pod 对特定域名的解析被劫持到错误 IP 地址
2. 应用连接到非预期的服务端点，请求失败或返回异常数据
3. 与 DNS 解析失败（NXDOMAIN/超时）不同：域名仍可解析，但结果为错误 IP
4. 仅影响指定域名，其他域名解析正常

**资源准备**：
1. 确认目标应用已正常运行，且依赖特定域名进行外部服务调用
2. 确认目标 Pod 的标签选择器、命名空间，以及**实际容器名**（多容器/临时容器混存时
   `kubectl exec` 必须显式 `-c <容器名>`，否则命中默认容器可能不是业务容器）
3. **能力探测（决定手段选择）**——确认 ChaosBlade 的 `pod-network` 是否提供 dns action，
   且 operator 实际健康，**以当次探测为准**：
   ```bash
   blade create k8s pod-network -h
   kubectl get pods -A --no-headers | grep -i chaosblade-operator
   ```
   - Available Commands 列出 `dns` 且 operator Pod Running → 手段1 可用
   - 无 `dns` 子命令，或 operator 处于 ImagePullBackOff/CrashLoopBackOff（常见形态：
     operator 与 tool Pod 均 ImagePullBackOff，CR 无人 reconcile）→ 手段1 判死，用手段2，
     不要在手段1 上空转
4. **判定目标 Pod 的工作负载形态（决定手段2 路径选型）**：
   ```bash
   kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.metadata.ownerReferences[*].kind}'
   ```
   输出为 Deployment/StatefulSet/DaemonSet 且可承受滚动重建 → 路径 A、B 均可（优先 A）；
   输出为**空（裸 Pod）或 Job 等无主形态 → 路径 A 物理不可用**（无 Deployment 可 patch，
   hostAliases 注入机制不存在），直接走路径 B，不要在路径 A 上空转
5. 确认目标域名当前可正常解析到正确 IP（ping 基线，见演练步骤第 1 条判据陷阱）
6. 路径 B 额外前提：容器内 `/etc/hosts` 当前用户可写（见路径 B 前提验证，不要假定可写）

**演练步骤**：
1. 记录注入前基线（⚠️ 判据陷阱：本场景注入层在 /etc/hosts，而
   nslookup 只查 DNS 服务器、**不读 /etc/hosts**——nslookup 看不见劫持效果，不能作判据。
   ping/wget 走 getaddrinfo（含 hosts 层），是正确判据工具）：
   ```bash
   kubectl get pods -n <namespace> -l <label-selector> -o wide
   kubectl exec <pod-name> -n <namespace> -c <container> -- ping -c 1 -W 3 <target-domain>
   ```
   从输出首行 `PING <domain> (<IP>)` 读取当前解析 IP 作为基线；再记录一份 /etc/hosts
   原文基线（路径 B 恢复验证的白盒对照）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- cat /etc/hosts
   ```

**手段1（ChaosBlade）** —— 前提：资源准备第 3 条探测通过（有 dns action 且 operator 健康）

2. 使用 ChaosBlade 对目标 Pod 注入 DNS 劫持：
   ```bash
   blade create k8s pod-network dns \
     --namespace <namespace> \
     --labels "<label-key>=<label-value>" \
     --domain <target-domain> \
     --ip <错误IP地址> \
     --timeout <duration>
   ```
   参数含义：
   - `--domain`：要劫持的域名（必填）
   - `--ip`：劫持后指向的错误 IP（必填；240.0.0.0/4 是保留段，必然不可达，适合做靶地址）
   - `--replace`：若域名已有本地解析记录，是否覆盖（默认不覆盖）
   - `--timeout`：到期自动恢复（秒）
3. 记录返回的 experiment_uid，用于后续恢复

**手段2（kubectl-native）** —— 按资源准备第 4 条的工作负载形态二选一

**路径 A —— 改 workload 的 `hostAliases`（工作负载管理形态优先）**

`hostAliases` 是 Kubernetes 原生的 /etc/hosts 注入机制，由 kubelet 在创建容器时写入，
不需要容器内可写、不需要 root、不需要任何容器内工具。

前提条件：目标 Pod 由 Deployment / StatefulSet / DaemonSet 管理（能承受一次滚动重建；
裸 Pod/无主形态此前提不成立，直接走路径 B，见资源准备第 4 条）

注入命令（**先武装定时恢复，再注入**）：
```bash
# 1) 基线捕获 + 武装定时还原（恢复命令幂等：定时器到期自动还原为主，Agent 在演练结束时
#    主动执行同一条命令兜底，迟到重复执行无副作用。定时器必须经 kubectl exec 载体派发
#    ——顶层裸 sh -c 不被工具守卫放行，且载体需含 kubectl 与集群凭证；载体 Pod 为多副本
#    时无法可靠终止，故不设 pidfile。恢复脚本落盘形态按 recovery-carrier.md 第七节「四档定案表」
#    按明文字节数查表选定（Phase 2 无 base64 生成器，勿留 <restore-b64> 占位符——下行命令为旧契约历史形态示例，勿套用）；
#    <duration> 需覆盖滚动重建窗口。
#    ⚠️ 载体 RBAC 前置检查：载体 SA 若无目标 ns 的 deployments/patch 权限，
#    定时器还原会因 Forbidden 失败（恢复输出落 /tmp/restore.log 可带外 cat 取证，静默期已
#    消除——armed 回执不等于恢复成功），劫持将随滚动重建长期生效。注入前先验证：
#    kubectl exec <载体Pod> -n <载体ns> -- kubectl auth can-i patch deployment <deployment-name> -n <namespace>，
#    返回 no 则定时器方案不可用——不得注入（先武装后注入），任务如实失败收尾；
#    故障已落地则靠带外 blade-ai recover --task-id 或控制台手动 patch 兜底）
kubectl get deployment <deployment-name> -n <namespace> -o jsonpath='{.spec.template.spec.hostAliases}'
kubectl exec <载体Pod> -n <载体ns> -- sh -c 'echo <restore-b64> | base64 -d > /tmp/blade-restore-hostaliases.sh; ( sleep <duration>; sh /tmp/blade-restore-hostaliases.sh ) >/tmp/restore.log 2>&1 & echo armed'

# 2) 注入劫持记录 —— 把域名指向一个不可达 IP（240.0.0.0/4 是保留段，必然不可达）
kubectl patch deployment <deployment-name> -n <namespace> --type=strategic -p \
  '{"spec":{"template":{"spec":{"hostAliases":[{"ip":"<错误IP>","hostnames":["<target-domain>"]}]}}}}'

# 3) 等滚动重建完成，新 Pod 才带上劫持记录
kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=120s
```
路径A 倒计时从武装时刻起算：先校验后武装、与注入紧邻（≤60s）；武装后发生任何修复须先 `kubectl exec <载体Pod> -n <载体ns> -- sh -c 'pkill -f blade-restore-hostalias[e]; true'` 停旧定时器再全额重武装（见 SKILL.md 安全红线「故障窗口完整」）

恢复命令（定时器到期自动执行；提前结束时由 Agent 主动执行）：
```bash
# 原本没有 hostAliases —— 整个字段移除（幂等：remove 对已不存在的路径仅报错无害）
kubectl patch deployment <deployment-name> -n <namespace> --type=json -p \
  '[{"op":"remove","path":"/spec/template/spec/hostAliases"}]'

# 原本有 hostAliases —— 用注入前记录的原值 json patch 整体替换（strategic patch 对数组按
# 合并键合并，注入项会残留，不可用于还原）
kubectl patch deployment <deployment-name> -n <namespace> --type=json -p \
  '[{"op":"replace","path":"/spec/template/spec/hostAliases","value":<注入前记录的原值>}]'

kubectl rollout status deployment/<deployment-name> -n <namespace> --timeout=120s
```

**路径 B —— 直接改容器内 `/etc/hosts`（裸 Pod/无主形态的唯一路径；受限，先验证前提）**

前提条件：容器内 `/etc/hosts` 必须**当前用户可写**。这一条经常不成立 ——
该文件由 kubelet 生成，属主是 `root` 且权限通常是 `644`，而多数生产镜像以非 root 用户运行
（如 UID 1200），此时写入会 `Permission denied`。**必须先验证，不要假定可写**：

```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'ls -l /etc/hosts; id -u; test -w /etc/hosts && echo WRITABLE || echo NOT_WRITABLE'
```
输出 `WRITABLE`（或 `id -u` 为 0——root 不受文件权限位约束）才可用本路径；
`NOT_WRITABLE` 且非 root → 两路皆死，发起 replan（不要降级成改 DNS 服务器，那是另一个用例）。

注入命令（**备份 → 武装定时恢复 → 注入 → 自证标记**，四步严格串行于单个载荷；
到期自动用备份还原 /etc/hosts）：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
  'cp /etc/hosts /etc/hosts.bak &&
   { ( sleep <duration>; cat /etc/hosts.bak > /etc/hosts; rm -f /etc/hosts.bak; echo DNS_HIJACK_RESTORED >> /etc/hosts.bak.evd ) >/dev/null 2>&1 & } &&
   echo "<错误IP> <target-domain>" >> /etc/hosts &&
   echo DNS_HIJACK_INJECTED >> /etc/hosts.bak.evd'
```
链条是**自证的**：注入成功后的 `DNS_HIJACK_INJECTED` 与定时器还原后的 `DNS_HIJACK_RESTORED`
写入证据文件 `/etc/hosts.bak.evd`，白盒主证在注入/还原时刻即被捕获，验证阶段读
`cat /etc/hosts.bak.evd` 即可取证，**不再受故障窗口是否已关闭的时序约束**
（窗口短于验证启动延迟时，窗口内探针结构性不可达；链内标记消除此竞态）。两个标记必须保留，
不得为缩短命令而省略（注意 wiz 等通道 sh -c 载荷有 1024 字节上限，本链含占位符约
380 字节，安全）。

路径B 倒计时从武装时刻起算：备份→武装→注入在同一载荷内严格串行（无侵蚀间隙）；武装后发生任何修复需重武装时，须先用存量备份还原再重跑：`kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c 'pkill -f hosts.ba[k]; true'` 停旧定时器后用 `cat /etc/hosts.bak > /etc/hosts` 还原，然后重跑上方注入命令重武装+重注入——不可直接重跑，否则备份会混入劫持记录（见 SKILL.md 安全红线「故障窗口完整」）

结构说明：外层 `{ ... & }` 在前台执行（立即返回），保证备份**先于**注入完成；
若写成 `cp ... && ( ... ) & echo ...` 会把备份与注入并行，备份可能混入劫持记录，
导致到期"恢复"成被劫持状态——**严禁该写法**。
还原必须用 `cat >`（截断写），**不能用 `cp` 覆盖**：/etc/hosts 由 kubelet bind-mount，
busybox cp 向它覆盖写入必报 `can't create '/etc/hosts': File exists`；若用在
定时器里，失败输出被丢弃、分号链继续删备份——劫持记录将永久残留。`cat >` 以 O_TRUNC
打开既有 inode，可用；追加注入 `>>` 同理可用。

提前恢复命令（幂等：备份不存在说明定时器已还原，整链空触发无害；
兜底执行务必包 `test -f` 守卫，否则 `cat` 报错会污染退出码。同 D50 陷阱：
`cp` 覆盖 /etc/hosts 必报 File exists，必须用 `cat >` 截断写）：
```bash
kubectl exec <pod-name> -n <namespace> -c <container> -- sh -c \
  'pkill -f hosts.ba[k]; test -f /etc/hosts.bak && { cat /etc/hosts.bak > /etc/hosts; rm -f /etc/hosts.bak; echo DNS_HIJACK_RESTORED >> /etc/hosts.bak.evd; }; true'
```

**注入验证**（两种手段共用——底层都是 /etc/hosts 劫持记录）：
1. 白盒确认劫持记录已生效：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- cat /etc/hosts
   ```
   应出现 `<错误IP> <target-domain>` 一行。路径 B 另读证据文件确认注入标记：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- cat /etc/hosts.bak.evd
   ```
   应含 `DNS_HIJACK_INJECTED`（含 `DNS_HIJACK_RESTORED` 即窗口已关，如实报告实际时长）
2. 效果确认——在目标 Pod 内验证域名解析已被劫持（nslookup 不读 /etc/hosts，用 ping 判读）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- ping -c 1 -W 3 <target-domain>
   ```
   确认输出首行解析 IP 为注入的错误 IP 地址
3. 在目标 Pod 内尝试访问该域名，确认连接失败或返回异常：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- wget -qO- --timeout=5 http://<target-domain>
   ```
4. 查看应用日志确认出现连接错误（connection refused/timeout/非预期响应）：
   ```bash
   kubectl logs <pod-name> -n <namespace> -c <container> --tail=20
   ```
5. 验证其他域名解析不受影响（确认故障范围可控）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- ping -c 1 -W 3 <无关域名>
   ```
   解析应仍为正常 IP
6. **持续性检查（必做）**——劫持记录是状态型故障，记录存活即故障存活：白盒主证为
   `cat /etc/hosts` 仍含劫持行（手段1 实验未 destroy 且未到 `--timeout`；手段2 定时器未走完）。
   路径 B 以证据文件为准：`DNS_HIJACK_INJECTED` 已现而 `DNS_HIJACK_RESTORED` 未现即窗口仍开
   （链条严格串行，标记先后顺序即注入/还原的确证）。有界佐证为静观短窗口后请求该域名
   仍连接错误 IP。若窗口内提前恢复，说明故障窗口契约未达成，必须如实报告实际持续时长

**注入恢复**：

手段1（ChaosBlade）：
1. 提前恢复：销毁实验（移除劫持记录）`blade destroy <experiment_uid>`
2. 或等待 `--timeout`（`<duration>`）到期后 ChaosBlade 自动恢复。
   注意：到期后 `blade status <uid>` 可能仍显示 `Success` 不翻状态，**不要以 status 判断
   记录是否还在**，以白盒 `cat /etc/hosts` 为准

手段2（kubectl-native）：
1. 主保险：路径 A 武装的载体定时器 / 路径 B 容器内定时器到期自动还原
2. 提前恢复：
   - 路径 A：执行路径 A 恢复命令（按基线是否有 hostAliases 选 remove/replace 形态）
   - 路径 B：执行路径 B 提前恢复命令（幂等，含 pkill 停定时器 + 备份还原 + 还原标记）
3. 容器重启语义：路径 A 的劫持随 Pod 生命周期持续（重启不丢）；路径 B 改的是容器内
   临时文件，容器一旦重启，kubelet 重新生成 /etc/hosts，修改**自动丢失**（这也算一种
   兜底恢复，但故障窗口契约因此提前终结，须如实报告）

**恢复验证**（两种手段共用）：
1. 在目标 Pod 内验证域名解析恢复正确（同样用 ping 而非 nslookup）：
   ```bash
   kubectl exec <pod-name> -n <namespace> -c <container> -- ping -c 1 -W 3 <target-domain>
   ```
   确认输出首行解析 IP 回到基线记录的正确 IP 地址
2. 白盒确认劫持记录已移除：`cat /etc/hosts` 不再含劫持行（与基线原文一致）；
   路径 B 证据文件应新增 `DNS_HIJACK_RESTORED` 标记；备份 `/etc/hosts.bak` 已删除
3. 在目标 Pod 内验证服务访问恢复正常
4. 确认应用日志不再出现连接错误
5. 确认 Pod 无 RESTARTS、无新增异常事件

**基准事实**：
- **根因**：Pod 内 /etc/hosts 被写入劫持记录，特定域名经 getaddrinfo 解析到错误 IP 地址，
  导致应用连接到非预期端点
- **必现现象**：目标域名解析（ping/getaddrinfo 路径）返回注入的错误 IP；应用对该域名的
  请求失败或返回异常；其他域名解析不受影响（注：nslookup 不读 /etc/hosts，看不见劫持效果，
  验证一律用 ping/wget + cat /etc/hosts 白盒）
- **blade 可用性因环境而异**：`pod-network dns` 需要 operator 健康才能 reconcile
  （常见形态：chaosblade-operator 与 chaosblade-tool Pod 均 ImagePullBackOff，
  CR 创建成功但无人执行——`blade create` 回执成功**不代表**注入生效，必须以靶 Pod 内
  /etc/hosts 白盒为准）；不满足即用手段2

**手段2 注意事项**：
- 路径 A 会触发滚动重建，故障在**新 Pod** 上生效，原 Pod 名会变 —— 注入后需重新获取 Pod 名
- 自恢复基于注入前武装的定时器：路径 A 为载体 sh -c 载荷内定时器（到期自动按基线
  还原 hostAliases，json patch 规避乐观锁与三方合并问题），路径 B 为容器内 sleep <duration> +
  备份还原；路径 A 定时器存活于载体 Pod，Pod 重建会丢失定时器，届时仍需 Agent
  主动执行（或人工）恢复命令兜底
- 若应用绕过 hosts 直连 DNS 解析器（自带 resolver 或 DNS 缓存），两条路径都可能不生效；
  这种情况要在 DNS 层面做（见 `Pod_网络故障_CoreDNS异常`）——注意该用例是集群级依赖
  故障，存在通道依赖死锁风险，注入前必须满足其带外恢复硬性前置
- 效果与手段1 完全等价——blade 底层就是向同一个 /etc/hosts 写入劫持记录
