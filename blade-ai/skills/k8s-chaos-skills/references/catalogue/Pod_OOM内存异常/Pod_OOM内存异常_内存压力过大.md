**用例名称** 内存压力过大 导致 Pod_OOM内存异常

**故障现象**：
1. Pod 内存使用率接近 Limit 上限
2. 应用响应变慢，出现延迟
3. 存在被 OOMKill 的风险

**资源准备**：
1. 确认应用 A 已正常运行
2. 确认应用 A 的 Pod 已配置 resources.limits.memory
3. 确认容器内有注入载体：`stress-ng`（首选）；否则 `python`/`perl`（多数业务容器自带）；最低保底 `dd`（见方案 3 的驻留陷阱）

> **本场景不使用 ChaosBlade（`blade create k8s pod-mem load`）**：其注入进程是 Pod 内
> 最大的内存占用者，内存逼近 Limit 时会被 OOM killer 优先杀掉——进程一死内存立即释放，
> 故障无法真正持续生效。内存压力注入一律走下方 kubectl-native 方案。

**演练步骤**：
1. 定位应用 A 的 Pod
2. 测量内存基线并计算分配量（必须，防超量 OOMKill）
3. 在 Pod 内注入驻留式内存压力（kubectl exec），模拟内存占用增长接近 Limit 的场景
4. 观察 Pod 内存使用率变化

**注入命令**：

**先测基线、算增量（必须，防超量 OOMKill）**：
```bash
# 当前 Pod 内存用量
kubectl top pod <pod-name> -n <namespace> --no-headers
# Pod 内存 limit
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.containers[0].resources.limits.memory}'
```
**分配量 = limit × 目标百分比 − 当前用量**。例：limit=8Gi、当前 3.3Gi、目标 80% → 8×0.8 − 3.3 ≈ 3.1G。
**严禁直接按「limit × 目标百分比」的绝对值分配**——Pod 已有基础用量，超量会越过剩余余量直接触发 OOMKill，注入进程被杀、效果不出现。

注入（按容器内可用载体择一）：
```bash
# 方案1：指定绝对大小（推荐，精确控制；后台+重定向让 exec 立即返回，--timeout 自带自动恢复）
kubectl exec <pod-name> -n <namespace> -- \
  sh -c 'stress-ng --vm 1 --vm-bytes <按上式算出的分配量，如 3G> --timeout <duration>s >/dev/null 2>&1 &'
# 方案2：无 stress-ng 时用分块分配（推荐，python/perl 容器普遍自带）。
# 关键：必须分块逐段分配——一次性构造全量（如 bytearray(N) 或 perl "x"xN）有 ~2 倍瞬时峰值，
# 会越过余量直接 OOMKill。分块后瞬时峰值只有一个块，且 python 不可用时可换 perl 同构写法：
kubectl exec <pod-name> -n <namespace> -- sh -c 'cat > /tmp/mem_stress.py << "EOF"
import time
chunks = []
for _ in range(<MB> // 100):        # 每块 100MB，块数 = 分配量(MB)/100
    chunks.append(bytearray(100 * 1024 * 1024))
    time.sleep(0.2)
with open("/tmp/memcache-warmup.pid", "w") as f:
    f.write(str(__import__("os").getpid()))
time.sleep(<duration>)              # 到期进程退出即自动释放
EOF
nohup python /tmp/mem_stress.py >/dev/null 2>&1 &'
# 方案2'（闭环 cgroup 计数，perl；#47 实测后立法的首选形态）：固定分配量估算对解释器开销的
# 假设不可靠——perl 的 SvGROW 超额分配使每块驻留 RSS 放大 ~1.26x（实测：请求 398Mi →
# VmRSS 502.75Mi，占 limit 98.3%，距 OOMKill 仅一步）。闭环形态不假设开销，直接读 cgroup
# 计数器分配到目标水位，解释器放大被闭环自动吸收；块更小（4Mi）且过冲只剩单块余量：
kubectl exec <pod-name> -n <namespace> -- sh -c 'cat > /tmp/mem_stress.pl << "EOF"
use strict;
my $tgt = $ARGV[0] || <目标字节数，如 429496729>;   # = limit × 目标百分比
my $dur = $ARGV[1] || <duration>;
my @c;
open(my $p, ">", "/tmp/memcache-warmup.pid") or die $!;
print $p $$; close($p);
sub u { open(my $f, "<", "/sys/fs/cgroup/memory/memory.usage_in_bytes") or return -1;
        my $v = <$f>; close($f); $v + 0 }
while (1) { my $x = u(); last if $x < 0 || $x >= $tgt;
            push @c, "\0" x (4 * 1024 * 1024); select(undef,undef,undef,0.03); }
sleep($dur);
exit 0;
EOF
setsid nohup perl /tmp/mem_stress.pl <目标字节数> <duration> >/dev/null 2>&1 &'
# （cgroup v1 路径；v2 环境读 /sys/fs/cgroup/memory.current。脚本含双引号字符，外层
# 用 quoted heredoc << "EOF" 保真落盘——分词器修复后双引号转义形态可用，单引号形态仍首推）
# 方案3：仅有 dd 时，必须用 sleep 挂住管道保持驻留——`( dd … | tail )` 单独用有驻留缺陷：
# dd 拷贝一结束 tail 即退出、内存立即释放，内存冲高后 30 秒内塌回基线；
# sleep 让管道 EOF 延迟到 <duration> 后，tail 的缓冲才能撑住全程。
# 另注意：dd 是逐块拷贝，速度慢于方案 2 的匿名内存直接分配：
kubectl exec <pod-name> -n <namespace> -- sh -c '
  ( ( dd if=/dev/zero bs=1M count=<MB> 2>/dev/null; sleep <duration> ) | tail ) >/dev/null 2>&1 &
  echo $! > /tmp/memcache-warmup.pid
'
```
倒计时从武装时刻起算：内存驻留与到期释放定时在同一载荷内原子紧邻（无侵蚀间隙）；武装后发生任何修复需全额重武装：先 `kubectl exec <pod-name> -n <namespace> -- sh -c 'kill $(cat /tmp/memcache-warmup.pid) 2>/dev/null; pkill -f mem_stress.p[y]; true'` 停掉旧驻留进程（方案2/3 通吃），再重跑对应方案的注入命令原子重武装+重注入（见 SKILL.md 安全红线「故障窗口完整」）。#47 实测：注入成功后 Agent 框架层崩溃且自动回滚失败（无人清理的最坏情形），正是载荷内原子 timer 到期自释放收的尾（600s 精确自清，memory 压力残留归零）——窗口自持设计不依赖任何上层存活
> **不要走 tmpfs 文件写路线**（`dd of=/dev/shm/…`）：部分环境对页缓存写入路径限速，可低至 ~0.5MB/s（3.5G 需 1 小时以上）；匿名内存分配（方案 2）同环境 <30 秒可完成同量级分配。

**注入验证**：
1. 首选零滞后直查：`kubectl exec <pod-name> -n <namespace> -- cat /proc/<注入进程PID>/status`（注入时已落盘 PID 的用 `cat /tmp/memcache-warmup.pid` 取；未落盘的用 `ps` 找 stress/mem_stress 进程）看 VmRSS 确认接近目标量——`kubectl top` 滞后一个 metrics 窗口（同集群不同 Pod 可差 11s~60s，且部分 adapter 不暴露快照时间戳），注入后短期内 top 无变化**不构成效果否定证据**，仅作聚合确认
2. 仅当已触发 OOMKill 时，`kubectl describe pod` 才会在 Events 中见到 OOMKilled；只接近 Limit 而未 OOM 时**没有任何内存相关 Event，查不到是必然，不要反复找**
3. （可选，仅当演练方提供了应用访问入口时）向入口发请求确认延迟增大；无入口时上述内存级证据成立即可判定

**注入恢复**：
1. 杀掉 Pod 内注入进程释放内存（按注入时实际使用的方案择一）：
```bash
# stress-ng：kill 进程
kubectl exec <pod-name> -n <namespace> -- sh -c 'pkill -f stress-ng 2>/dev/null'
# 分块分配：按落盘 PID kill（python 方案）
kubectl exec <pod-name> -n <namespace> -- \
  sh -c 'kill $(cat /tmp/memcache-warmup.pid) 2>/dev/null; rm -f /tmp/memcache-warmup.pid /tmp/mem_stress.py'
# dd 管道方案同上式（kill 管道进程即可释放）
# 兜底：ps+kill（比 pkill 通用）
kubectl exec <pod-name> -n <namespace> -- \
  sh -c "ps -o pid,args 2>/dev/null | grep -E '[s]tress-ng|[m]em_stress|[d]d if=/dev/zero' | awk '{print \$1}' | xargs -r kill -9"
```

**恢复验证**：
1. 直查注入进程 PID 是否已退出（`cat /proc/<pid>/status` 报 No such file 即已释放）；`kubectl top pod` 回落仅作聚合确认（同样滞后一个窗口，恢复初期 top 仍高**不构成恢复失败证据**）
2. （可选，有访问入口时）确认应用响应恢复正常

**基准事实**：
- **根因**：应用内存使用增长或注入内存压力，导致 Pod 内存使用率接近 Limit，存在被 OOMKill 的风险
- **必现现象**：Pod 内存使用率接近 Limit；应用响应变慢；存在 OOMKill 风险

注意事项：
- stress-ng `--vm-bytes` 按系统内存百分比计算，非 Pod cgroup 百分比，需手动转算绝对值
- **严禁一次性构造全量分配**（`bytearray(总量)`、`perl "x"x总量`）：瞬时峰值约为目标量 2 倍，即使增量算对了也会越过余量被 OOMKill——分块（每块 100MB + 短间隔）是唯一稳妥写法
- **perl 分块的驻留量也有放大，不只是瞬时峰值**（#47 实测）：SvGROW 超额分配使每块实际 RSS ≈ 请求量 ×1.26——固定分配量估算会把累计驻留推到 98%+ 水位（距 OOMKill 一步）。perl 载体一律用方案 2' 闭环 cgroup 计数形态，让分配量由实测计数器收敛而非估算
- dd 管道方案驻留时长 = sleep 挂管道的时长（到期 EOF → tail 退出 → 释放），且逐块拷贝速度慢于分块分配，仅作无 python/perl 时的保底
- 无 cgroup 感知能力，可能直接触发 OOMKill 而非停留在“接近 Limit”状态
- **量必须按增量算**（分配量 = limit × 目标百分比 − 当前用量）。按目标百分比的绝对值直接分配会越过剩余余量触发 OOMKill——注入进程被杀、效果不出现，表现为「执行了但 top 无变化」
- **注入后 top 冲高又塌回基线时，按顺序排查**：① `kubectl describe pod` 查 Events 是否有 OOMKill——有则是分配过量被杀，按增量重算后重试同一方法；② 无 OOMKill 则是驻留载体已退出（典型如 dd|tail 未挂 sleep，dd 结束 tail 即退出），换持续驻留的载体（方案 2 分块分配），**不要**误判为「该容器无法驻留内存」
