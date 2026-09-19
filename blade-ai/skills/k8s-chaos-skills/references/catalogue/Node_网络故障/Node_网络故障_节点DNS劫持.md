**用例名称** 节点DNS劫持 导致 Node_网络故障

**故障现象**：
1. 节点宿主机与 hostNetwork Pod 对特定域名的 DNS 解析被劫持到错误 IP
2. 多个应用同时出现对同一域名的连接异常
3. 与 Pod 级 DNS 劫持不同：作用点为宿主机 /etc/hosts；但非 hostNetwork Pod（绝大多数业务 Pod）的 /etc/hosts 由 kubelet 独立管理，宿主机劫持对其零传导（见注意事项）
4. 模拟节点级 DNS 污染或中间人攻击场景

**资源准备**：
1. 确认目标节点名称及其上运行的依赖特定域名的工作负载
2. 确认目标域名当前可正常解析
3. 确认 ChaosBlade Operator 已部署（DaemonSet 通道）或具备节点 SSH 访问权限（SSH 通道）

**演练步骤**：
1. 确认目标节点名称和上面运行的 Pod：
   ```bash
   kubectl get pods -o wide --field-selector spec.nodeName=<node-name>
   ```
2. 确认目标域名当前解析正常：
   ```bash
   kubectl exec <该节点上的pod> -n <namespace> -- nslookup <target-domain>
   ```
3. 选择注入方式并注入节点级 DNS 劫持：

   **方式一：DaemonSet 通道**
   ```bash
   blade create k8s node-network dns \
     --names <node-name> \
     --domain <target-domain> \
     --ip <错误IP地址> \
     --timeout <duration>

   ```

   **方式二：SSH 通道**
   ```bash
   blade create k8s node-network dns \
     --domain <target-domain> \
     --ip <错误IP地址> \
     --channel ssh \
     --ssh-host <node-ip> \
     --ssh-user root \
     --timeout <duration>
   ```
   两种方式倒计时均从武装时刻起算：--timeout 与注入命令一同下发原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `blade destroy <experiment_uid>` 旧实验，再重跑上方注入命令重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）
   - `--domain`：要劫持的域名（必填）
   - `--ip`：劫持后指向的错误 IP（必填）
4. 记录返回的 experiment_uid，用于后续恢复

**注入验证**：
1. 在目标节点宿主机侧验证劫持生效（hosts 修改路径的正证据）：
   ```bash
   kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host getent hosts <target-domain>
   ```
   确认解析到注入的错误 IP。**不要用 nslookup 验证 hosts 劫持**——nslookup 是纯 DNS 查询、不读 /etc/hosts，在宿主机与非 hostNetwork Pod 内均验不出劫持；getent 走完整 resolver（/etc/hosts 优先）才是正证。**getent hosts 的 IPv6-first artifact**：glibc `getent hosts <domain>` 按地址族序 AF_INET6 优先——若注入的劫持记录是 IPv4-only 而目标域名存在真实 AAAA 记录，`files` 对 v6 miss 后 `dns` 返回真实 AAAA，getent 在尝试 AF_INET 前即返回——**劫持行明明在 /etc/hosts 里，`getent hosts` 输出却不含注入 IP（假阴性）**。判据形态须二选一：①验证命令 pin 地址族 `getent ahostsv4 <target-domain>`（推荐——等价观察、同一 resolver 语义）；②注入时同时写 A + AAAA 两条劫持记录（IPv6 用 TEST-NET 保留段 2001:db8::）
2. 验证其他域名解析不受影响
3. 验证其他节点解析正常（确认故障限定在目标节点）：
   ```bash
   kubectl debug node/<其他节点名> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host getent hosts <target-domain>
   ```
4. 查看应用日志确认出现连接异常（受影响的是宿主机进程与 hostNetwork Pod 上的应用；非 hostNetwork Pod 的应用不受影响）
5. **验证传导边界（case 现象 3 的判据形态——正验证「故障边界」）**：在该节点上的非 hostNetwork Pod（普通业务 Pod）内执行 `getent hosts <target-domain>`（或 nslookup）→ 仍返回真实 IP——kubelet 独立管理其 /etc/hosts（仅 localhost 与 Pod IP 条目），宿主劫持零传导；该判据与宿主侧错误 IP 恰构成「故障在位 + 边界清晰」的完整证据链

**注入恢复**：
1. 销毁 ChaosBlade 实验：
   ```bash
   blade destroy <experiment_uid>
   ```
2. 或等待 `--timeout` 到期自动恢复

**恢复验证**：
1. 在目标节点宿主机侧验证解析恢复（与注入验证第 1 条同路径对比，确认回到注入前基线的真实 IP）：
   ```bash
   kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host getent hosts <target-domain>
   ```
   不要用 nslookup 验证恢复——nslookup 不读 /etc/hosts，注入期间与恢复后返回相同结果（真实 IP），无法区分两种状态；也不要用非 hostNetwork Pod 内验证（宿主机劫持对其零传导，恒为真实 IP）
2. **零残留三件套**：`ls /etc/hosts.bak` 预期 No such file（exit 2 = 备份已被 mv 消费——预期通过形态）+ `systemctl status blade-restore-hosts.timer` exit 4 not-found（一次性 --on-active timer fire 后自毁）+ 宿主 `/etc/hosts` 尾部无劫持行（`tail -3 /etc/hosts` 不含 <错误IP> 条目）
3. 确认应用日志不再出现连接错误
4. 确认业务调用恢复正常

**基准事实**：
- **根因**：节点宿主机级别 DNS 解析被劫持（/etc/hosts 修改），特定域名被解析到错误 IP；影响宿主机进程与 hostNetwork Pod（其 /etc/hosts 为宿主机 hosts 的 kubelet 管理副本）；非 hostNetwork Pod 的容器内解析不受影响（独立 /etc/hosts + 集群 DNS）
- **必现现象**：该节点宿主机与 hostNetwork Pod 对目标域名解析结果为错误 IP；非 hostNetwork Pod 解析不受影响（零传导）；其他节点不受影响

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效节点级 DNS 劫持。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；选择已验证可拉取且含 `chroot`/`sh` 的镜像；宿主机变更必须 `--profile=sysadmin`；禁用 `-it`

注入命令（**先备份、武装定时还原，再注入劫持记录**）：
```bash
# 通过 kubectl debug node 修改宿主机 /etc/hosts 注入 DNS 劫持。
# timer 由宿主机 systemd(PID 1) 管理，到期自动用备份还原 hosts。
# **载荷形态红线（引号红线 + 拆两条简单命令）**：
#   - 恢复 timer 载荷 = `mv /etc/hosts.bak /etc/hosts` 单命令，systemd-run 直接 exec argv
#     传递零引号零嵌套——mv 同文件系统 = rename 原子替换，一条命令同时完成「还原 + 清备份」
#     双重语义（比 `cp && rm` 复合链少一层 sh -c 嵌套）
#   - 注入链拆两条 one-shot 简单命令（第 1 条备份+武装闹钟，第 2 条注入故障——故障上膛前
#     闹钟先武装）
# 冲突预检（timer unit 残留；exit 4 not-found = 无残留可武装——预期通过形态，勿误读为
# 探测失败）：
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host systemctl status blade-restore-hosts.timer
# 第 1 条：备份 + 武装定时还原（`&&` 串联保证武装失败时不会留下无主备份）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c 'cp /etc/hosts /etc/hosts.bak && systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-hosts mv /etc/hosts.bak /etc/hosts'
# 第 2 条：注入劫持记录（宿主 glibc resolver 对新连接立即生效）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c 'echo "<错误IP> <target-domain>" >> /etc/hosts'
```
注入前基线探测与注入验证同路径（宿主 `getent hosts <target-domain>` 记录真实 IP——基线/验证同形态原则，勿用 Pod 内 nslookup 充当宿主基线）

恢复命令（timer 到期前可提前手动恢复）：
```bash
# 提前恢复：还原宿主机 /etc/hosts（同时停掉已武装的 timer）
kubectl debug node/<node-name> --profile=sysadmin --image=<verified-cluster-image> -- chroot /host sh -c \
  'systemctl stop blade-restore-hosts.timer 2>/dev/null; cp /etc/hosts.bak /etc/hosts && rm -f /etc/hosts.bak'
# 删除 debug Pod
kubectl delete pod <debug-pod-name> --force --grace-period=0
```

注意事项：
- 修改宿主机 /etc/hosts 对宿主机上所有使用 glibc 的进程立即生效；对节点上容器的传导取决于 Pod 网络模式：**非 hostNetwork Pod（绝大多数业务 Pod）零传导**——其 /etc/hosts 由 kubelet 独立管理（仅 localhost 与 Pod IP 条目），宿主机劫持期间容器内解析仍返回真实 IP；**hostNetwork Pod 仅创建时传导**——kubelet 在 Pod 创建时把宿主 hosts 内容生成到 hostNetwork Pod 的 /etc/hosts（文件头标注 "Kubernetes-managed hosts file (host network)"），**此后不随宿主变化重新同步**：故障窗之前已存在的 hostNetwork Pod（如 node-local-dns 等长龄组件）的 /etc/hosts 不含注入劫持行；只有故障窗口内新创建的 hostNetwork Pod 才继承劫持。因此实际传导面 = 宿主进程 + 窗口内新建 hostNetwork Pod，比「存量 hostNetwork Pod 全体传导」的直觉窄——验证传导面时勿以存量 hostNetwork Pod 兜底判据（恒阴性），宿主侧 getent 才是主判据
- 部分应用有 DNS 缓存（如 JVM），修改 hosts 后可能需等待缓存过期
- 自恢复基于 systemd-run transient timer 到期自动用备份还原 hosts，补齐了 ChaosBlade `--timeout` 的自恢复能力；备份文件 `/etc/hosts.bak` 是还原的唯一依据，注入前必须确认备份成功（`&&` 串联已保证）
- 同名 transient timer 重复武装会报 `Unit blade-restore-hosts.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先按本文件注入命令的同等 chroot /host 通道形态清理残留：`systemctl stop blade-restore-hosts.service; systemctl reset-failed blade-restore-hosts.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
