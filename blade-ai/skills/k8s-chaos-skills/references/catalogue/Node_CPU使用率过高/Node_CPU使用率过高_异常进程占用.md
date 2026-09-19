**用例名称** 异常进程占用 导致 Node_CPU使用率过高

**故障现象**：
1. 节点 CPU 使用率持续超过 90%
2. 节点上 Pod 响应变慢，出现超时
3. Load Average 显著升高

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认监控系统（如 Prometheus）已配置，可观测节点 CPU 指标
3. **循环数 N 按节点实际核数定**（debug Pod 容器内 `nproc` 即节点核数——无 cgroup
   CPU 虚拟化的常规形态；N=核数时 top node 读数逼近 100%，N 取核数的 90% 以上
   才能满足「>90%」判据；以当次探测值为准）
4. **镜像选择（节点缓存约束）**：debug Pod 镜像须为节点已缓存镜像（外网 registry
   不可达环境 ImagePullBackOff 即注入死锁——载体 Pod 起不来）。优先复用集群基础
   组件 DaemonSet 的缓存 tag（定案全文：`references/environment/node-cached-images.md`）；
   规划期至多单命令复核该 DaemonSet ready/desired 即定案成立。武装镜像需含 `sh`
   与 `nsenter`（载荷在宿主机执行，`timeout` 用宿主机 coreutils，镜像内无需自带）

**演练步骤**：
1. 定位目标节点（选择其上有可观测业务 Pod 的节点，便于「同节点 Pod 影响」判据取样）
2. 经 `kubectl debug node/<node-name>` 的一次性命令在**宿主机上武装 systemd 瞬态
   服务**承载 N 个单核循环满载（`RuntimeMaxSec` 到期自停），模拟异常进程占用节点 CPU
3. 观察节点 CPU 使用率及 Load Average 变化

**注入验证**：
1. `kubectl top node <node-name>` 确认节点 CPU 使用率高于注入目标——单次采样读数高于目标即满载已发生（top 读数来自 metrics-server 采样窗口，注入后立即查询可能仍读到旧值，出现低于目标的读数时可稍后复查一次）；「持续保持」由机制存活保证（瞬态服务 active + 循环任务在即持续满载由构造成立），无需反复采样验证持续
2. Load Average 确认显著升高——debug Pod 挂载宿主机根文件系统于 `/host`，读 `/host/proc/loadavg`（1/5/15 分钟三值，与基线对比）；节点核数即 100% 满载时 loadavg 的理论上限参考
3. （可选，仅当演练方提供了应用访问入口时）确认请求延迟增大；无入口时上述节点级证据成立即可判定。同节点若有演练常驻靶 Pod，其容器内 `uptime`/响应亦可作旁证（节点满载下调度延迟的弱证据，非主判据）

**注入恢复**：
1. 等待 `RuntimeMaxSec` 到期（systemd 终止整个 cgroup——故障自动停止，无需手动
   kill；载荷内每循环的 `timeout <duration>` 是第二道自停保险）
2. 若需提前停止：`systemctl stop <unit>`（`--collect` 使瞬态单元停止即被 GC，无残
   留单元对象）

**恢复验证**：
1. `kubectl top node <node-name>` 确认 CPU 使用率恢复到正常水平（回落到注入前基线量级——metrics-server 采样有延迟，给一次复查窗口）
2. `/host/proc/loadavg` 回落（15 分钟值回落数据滞后，1 分钟值优先）
3. （可选，有访问入口时）确认请求延迟恢复正常
4. 载体零残留：systemd 单元 `LoadState=not-found`（经 `systemctl show <unit> --property=LoadState,ActiveState,SubState` 确认）；`kubectl get pods -A | grep node-debugger` 为空（一次性武装/探针 Pod 均已被自动清理）

**基准事实**：
- **根因**：节点上存在异常进程大量占用 CPU，导致节点 CPU 使用率过高，影响同节点上所有 Pod 的性能
- **必现现象**：节点 CPU 使用率持续超过 90%；Load Average 显著升高；同节点 Pod 响应变慢
- **载荷形态**：循环计数用字面 N 项 `for i in 1 2 … N` 列表——`$(seq 1 N)` 在无 seq
  的环境展开为空会使循环零注入（静默失败）；载荷中无 `$` 变量展开即无此风险
- **载体选型**：持续满载载荷必须宿主 systemd 瞬态服务承载，**不可**用 debug Pod
  一次性命令（`-- sh -c '<长循环>'`）直接承载——一次性 debug 命令被工具层按探针
  处理，120 秒硬上限后载体被自动清除，故障窗口被截断（详见手段2注意事项）

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用以下 kubectl 原生命令实现等效故障注入。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；节点宿主机 systemd 可用（`systemd-run` 存在、PID 1 为 systemd）；武装镜像为节点缓存镜像且含 `sh` 与 `nsenter`（见资源准备第 4 条）

注入命令（一次性武装，命令立即返回）：
```bash
# 经 sysadmin debug Pod 进入宿主机 PID namespace，武装 systemd 瞬态服务承载满载载荷
# N=节点核数（见资源准备第 3 条）；<duration> 秒后 systemd 终止整个 cgroup（自停）
# 计数用字面 N 项列表而非 $(seq)——无 seq 环境下 $(seq 1 N) 展开为空会零注入（静默失败）
kubectl debug node/<node-name> --profile=sysadmin --image=<cached-image-with-sh-nsenter> -- sh -c \
  'nsenter -t 1 -m -u -i -n -p -- systemd-run --unit=drill-node-cpu-fullload --collect \
   --property=RuntimeMaxSec=<duration> -- sh -c '"'"'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do timeout <duration> sh -c "while :; do :; done" & done; wait'"'"''
# 回执含 "Running as unit: drill-node-cpu-fullload.service" 即武装成功
# （for 列表按 N 展开；示例为 16 核）
```
倒计时从武装时刻起算：注入与到期自停定时（RuntimeMaxSec 与 per-loop timeout）在同一条命令内原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `systemctl stop <unit>`（旧故障停止与定时器取消同步完成），再重跑上方注入命令重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」与「武装载荷单次性」）

恢复命令：
```bash
# 正常路径：RuntimeMaxSec 到期自停（systemd 杀整个 cgroup），单元因 --collect 即时 GC
# 提前停止 / 兜底清理（经 debug Pod 执行）：
kubectl debug node/<node-name> --profile=sysadmin --image=<cached-image> -- sh -c \
  'nsenter -t 1 -m -u -i -n -p -- sh -c "systemctl stop drill-node-cpu-fullload.service; systemctl reset-failed drill-node-cpu-fullload.service"'
# reset-failed 若报 "Unit not loaded" 属良性——--collect 已在 stop 完成时回收了单元对象
# 终态确认：systemctl show drill-node-cpu-fullload.service \
#   --property=LoadState,ActiveState,SubState → not-found / inactive / dead
```

注意事项：
- 载体是**宿主机 systemd 瞬态服务**（非 Pod、非 API 对象）：满载进程运行在宿主机，
  直接推高节点 CPU；`--collect` 保证 stop/到期后单元对象即时回收，天然零残留
- **勿用一次性 debug 命令直接承载持续载荷**：`-- sh -c '<循环载荷>'` 形态会被工具
  层按探针处理（120 秒硬上限后载体被自动清除），故障窗口被截断为 ~2 分钟；正确形
  态即上方命令——一次性命令只做「武装」，持续载荷交给宿主 systemd
- 自停是双保险：`RuntimeMaxSec`（systemd 终止整个 cgroup，权威停止时刻）+ 每循环
  `timeout <duration>`（逐进程自终止）；两者在同一命令内原子紧邻武装
- 武装命令是一次性 debug Pod：命令立即返回（服务端 systemd-run 分离执行），Pod
  完成后被工具自动清理；验证期读 `/host/proc/loadavg` 等探针同样用一次性 debug
  命令（自动清理），无需常驻载体
- `systemctl stop` 会向 cgroup 发 SIGTERM 并阻塞至停（秒级）；提前停止是唯一需要
  主动恢复的场景——到期自停无需人工介入
