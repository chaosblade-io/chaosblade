**用例名称** 异常进程占用 导致 Node_内存使用率过高

**故障现象**：
1. 节点内存使用率持续超过注入的目标百分比（`kubectl top` 口径）
2. （仅深水区形态，见下方「两种注入深度」）节点 Status 出现 MemoryPressure 条件为 True
3. （仅深水区形态）节点上 Pod 出现 OOMKilled 或被驱逐

**两种注入深度**（注入前必须选定，判据完全不同）：
- **保守形态（默认）**：目标 = top 口径升至指定百分比（如 80%）。分配量按增量算，节点
  MemAvailable 保持充足（远高于 100Mi），**MemoryPressure 预期保持 False、Pod 不被
  驱逐——这是预期而非反证**；主判据只有 top 读数一条
- **深水区形态（可选，须显式要求）**：目标 = 压穿 MemoryPressure / 触发驱逐。分配量须按
  物理口径另行估算（见注意事项口径条款），接受驱逐波及面与 OOM killer 风险，判据含
  MemoryPressure True 与 Pod 驱逐；本形态对生产节点破坏性大，非显式要求不启用

**资源准备**：
1. 确认应用 A 已正常运行（保守形态下应用不受影响，可作「无波及」旁证）
2. 确认监控系统（如 Prometheus）已配置，可观测节点内存指标
3. **镜像选择（节点缓存约束）**：debug Pod 镜像须为节点已缓存镜像（外网 registry 不可达
   环境 ImagePullBackOff 即注入死锁）。优先复用集群基础组件 DaemonSet 的缓存 tag（定案
   全文：`references/environment/node-cached-images.md`）；规划期至多单命令复核该
   DaemonSet ready/desired 即定案成立。武装镜像需含 `sh` 与 `nsenter`（载荷在宿主机
   执行，`python3` 用宿主机的，镜像内无需自带）
   预探测结论引用：`acs/terway` 镜像含 `sh` 与 `nsenter`
   （`/usr/bin/sh` + `/usr/bin/nsenter` 直证）——planning 直接
   引用该结论即可，**勿再投特权 debug pod 专验镜像内工具**；保留单命令轻探（如 DS
   ready 复核）完成时效核验，镜像 tag 变更时结论须重验（版本相关事实，以当次探测为准）
4. **宿主原语探测**：载荷依赖宿主机 `python3`（分块分配匿名内存的默认实现）；探测时
   宿主机无 python3 才降级 `perl`（`perl -e 'my @b; while (...) { push @b, "x" x (100*1024*1024); ... }'`
   同语义分块）。禁止依赖镜像内 stress-ng——外网镜像不可达 + 节点缓存镜像/宿主机通常均
   不含该二进制

**演练步骤**：
1. 定位目标节点并测基线：`kubectl top node <node-name>`（记当前用量）
2. 计算分配量（保守形态增量口径）：分配量 ≈ 节点总内存 × 目标百分比 − 当前用量
   （例：64G 节点、当前 3.2G、目标 80% → 64×0.8 − 3.2 ≈ 48G；top 口径按 allocatable
   归一时目标值可略高，读数落在目标附近即成立，不必精确卡线）
3. 经 `kubectl debug node/<node-name>` 的一次性命令在**宿主机上武装 systemd 瞬态服务**，
   以宿主 python3 分块分配匿名内存并驻留至窗口结束（`RuntimeMaxSec` 到期自停）

**注入命令**（保守形态，一次性武装，命令立即返回）：
```bash
# 经 sysadmin debug Pod 进入宿主机 PID namespace，武装 systemd 瞬态服务承载内存载荷
# <CHUNK> 为单块字节数（默认 200MB），<SLEEP> 为块间隔秒（默认 0.2）——分块防一次性
# 峰值约 2 倍越过余量被 OOM killer 杀；<BYTES> 为分配总量（按步骤 2 增量算）
# <duration> 秒后 systemd 终止整个 cgroup（自停，权威停止时刻）
kubectl debug node/<node-name> --profile=sysadmin --image=<cached-image-with-sh-nsenter> -- sh -c \
  'nsenter -t 1 -m -u -i -n -p -- systemd-run --unit=drill-node-mem-load --collect \
   --property=RuntimeMaxSec=<duration> -- python3 -c "
import time
deadline = time.time() + <duration>
blocks = []
target, chunk, gap = <BYTES>, <CHUNK>, <SLEEP>
done = 0
while done < target:
    blocks.append(b\"x\" * chunk)
    done += chunk
    time.sleep(gap)
while time.time() < deadline:
    time.sleep(1)"'
# 回执含 "Running as unit: drill-node-mem-load.service" 即武装成功（武装回执核验红线）
```
> 载荷语义：deadline 从武装时刻起算，分块分配至目标量后驻留到 deadline（与
> RuntimeMaxSec 同基准对齐；systemd 是权威停止时刻，载荷 sleep 是第二道保险）。
> `b\"x\"` 为 bytes 乘法（匿名内存，不落盘）——shell 引号转义按所在层级自行处理。
> 分配阶段失败（如余量不足 MemoryError）进程 fast-fail 退出，服务转 failed，内存即
> 释放——此时无驻留无窗口，按「分配失败处置」重算尺寸重武装，勿在原单元上等待

倒计时从武装时刻起算：注入与到期自停定时（RuntimeMaxSec 与载荷 sleep）在同一条命令内
原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `systemctl stop <unit>`
（旧载荷终止与定时器取消同步完成），再重跑上方注入命令重武装+重注入（见 SKILL.md 安全
红线「故障窗口完整」与「武装载荷单次性」）

**注入验证**：
1. `kubectl top node <node-name>` 确认节点内存使用率升至注入目标附近——读数高于注入前
   基线并接近目标百分比即占用已发生（top 读数来自 metrics-server 采样窗口，注入后立即
   查询可能仍读到旧值，出现低于目标的读数时可稍后复查一次）；「持续保持」由机制存活
   保证（瞬态服务 active + python3 进程驻留即持续占用由构造成立），无需反复采样验证持续
2. （仅深水区形态）`kubectl describe node <node-name>` 确认 MemoryPressure 为 True——该
   条件由 kubelet 周期同步存在滞后，首查仍为 False 不构成反证，top 读数才是即时主证；
   **保守形态下 MemoryPressure 保持 False 为预期**
3. （仅深水区形态）确认 Pod 出现 OOMKilled 或被驱逐
4. 白盒旁证（可选，一次只读探针）：宿主 `/proc/meminfo` 的 MemAvailable 较基线下降量与
   分配量同量级；`systemctl show drill-node-mem-load.service --property=MainPID` 有主
   进程即载荷在驻留

**注入恢复**：
1. 等待 `RuntimeMaxSec` 到期（systemd 终止整个 cgroup——内存即时释放，故障自动停止，
   无需手动 kill；载荷内 sleep 是第二道自停保险）
2. 若需提前停止：`systemctl stop <unit>`（`--collect` 使瞬态单元停止即被 GC，无残留
   单元对象）

**恢复命令**：
```bash
# 正常路径：RuntimeMaxSec 到期自停（systemd 杀整个 cgroup 释放匿名内存），单元因 --collect 即时 GC
# 提前停止 / 兜底清理（经 debug Pod 执行）：
kubectl debug node/<node-name> --profile=sysadmin --image=<cached-image> -- sh -c \
  'nsenter -t 1 -m -u -i -n -p -- sh -c "systemctl stop drill-node-mem-load.service; systemctl reset-failed drill-node-mem-load.service"'
# reset-failed 若报 "Unit not loaded" 属良性——--collect 已在 stop 完成时回收了单元对象
# 终态确认：systemctl show drill-node-mem-load.service \
#   --property=LoadState,ActiveState,SubState → not-found / inactive / dead
```

**恢复验证**：
1. `kubectl top node <node-name>` 确认内存使用率恢复到注入前基线量级（metrics-server
   采样有延迟，给一次复查窗口）
2. 宿主 MemAvailable 恢复至基线量级（白盒即时主证，不受 metrics-server 采样滞后影响）
3. （仅深水区形态）MemoryPressure 回落为 False——注入释放后 condition True→False 由
   kubelet 周期同步，滞后可达约 5 分钟，恢复判读须纳入该窗口，勿在止血后立即判「未恢复」
4. 载体零残留：systemd 单元 `LoadState=not-found`（经 `systemctl show <unit>
   --property=LoadState,ActiveState,SubState` 确认）；`kubectl get pods -A | grep
   node-debugger` 为空（一次性武装/探针 Pod 均已被自动清理）

**基准事实**：
- **根因**：节点上存在异常进程大量占用内存，导致节点内存使用率过高；深水区形态下压穿
  available 引发 MemoryPressure 与驱逐
- **必现现象**：保守形态 = 节点内存使用率持续超过注入目标（top 口径）；深水区形态另加
  MemoryPressure True 与 Pod OOMKilled/驱逐
- **载体选型**：持续内存载荷必须宿主 systemd 瞬态服务承载，**不可**用 debug Pod 一次性
  命令（`-- sh -c '<stress-ng/python>'`）直接承载——一次性 debug 命令被工具层按探针
  处理，120 秒硬上限后载体被自动清除，故障窗口被截断（详见手段2注意事项）

---

**手段2（kubectl-native）**

> 当 ChaosBlade 不可用时，可使用 kubectl 原生命令实现等效故障注入。主形态即上方演练步
> 骤的 systemd-run 路径，本节为补充说明与备选降级。

前提条件：集群需支持 `kubectl debug node` 功能（K8s 1.18+）；节点宿主机 systemd 可用
（`systemd-run` 存在、PID 1 为 systemd）；武装镜像为节点缓存镜像且含 `sh` 与
`nsenter`（见资源准备第 3 条）；宿主机有 `python3`（或降级 `perl`，见资源准备第 4 条）

注意事项：
- **top 口径 ≠ available 口径（两口径无推导关系）**：`kubectl top` 的内存百分比按
  working set / allocatable 计算；而 MemoryPressure / hard eviction 按
  `memory.available < 100Mi` 的**物理口径**判定。top 打到 100% 不等于触发驱逐——
  30G 节点注入 26G（top 100%）可能仍不触发，需接近 28.5G 才能把 available 压穿。若目标
  是触发 MemoryPressure/驱逐，分配量须按物理口径估算：压穿量 ≈ 物理容量 − 当前匿名
  内存占用（页缓存可回收、不计入），建议分段逼近
- **勿用一次性 debug 命令直接承载持续载荷**：`-- sh -c '<载荷>'` 形态会被工具层按探针
  处理（120 秒硬上限后载体被自动清除），故障窗口被截断为 ~2 分钟；正确形态即上方命令
  ——一次性命令只做「武装」，持续载荷交给宿主 systemd
- **内存分配三失效模式（载荷实现红线）**：① 禁止一次性构造全量分配（单次
  `bytes(N)` / `"x" x N`）——瞬时峰值约为目标量 2 倍，即使增量尺寸算对也会越过余量被
  OOM killer 杀，必须分块（每块 100-200MB + 短间隔）；② 禁止 tmpfs 文件写路线
  （`dd of=/dev/shm`）——部分环境页缓存写入被限速，不可用于内存注入；③ 管道驻留形态
  （`dd | tail` 类）管道 EOF 即释放，如确需 dd 路线必须让管道 EOF 延迟到窗口结束。
  首选 python3 分块匿名内存，次选 perl 同语义分块
- debug 命令在**无 TTY 时立即返回**（不阻塞，仅打印 Pod creating 消息）；debug Pod 服务
  端持续运行，武装动作在宿主 systemd 内异步完成——回执以 "Running as unit" 行为准，
  而非 debug Pod 的退出码
- **深水区形态的驱逐波及面**：压穿 available 后 kubelet hard eviction 会按优先级驱逐节
  点上 Burstable/BestEffort Pod，OOM killer 可能波及任何进程（含节点关键组件）——该形
  态仅适用于专门搭建的演练节点，生产节点默认保守形态
- stress-ng 路径仅当环境同时满足「stress-ng 镜像可达」与「接受 one-shot 承载」时才可
  考虑，且仍须解决 120s cap 问题（systemd-run 包装）——实质上已被 python3 路径覆盖，
  不再单列
- debug Pod 在节点 MemoryPressure 时可能被 OOM killer 终止，这本身就是预期行为（深水
  区形态下）
