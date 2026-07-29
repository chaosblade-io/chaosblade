**用例名称** 进程CPU满载 导致 Host_CPU使用率过高

**故障现象**：
1. 主机 CPU 使用率持续超过 90%
2. 系统 Load Average 显著升高
3. 主机上运行的应用响应变慢，出现超时

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测主机 CPU 指标（如 Prometheus node_exporter、top、vmstat）

**演练步骤**：
1. 记录当前 CPU 基线：`top -bn1 | head -5` 或 `vmstat 1 3`
2. 使用 ChaosBlade 注入 CPU 满载

```bash
blade create cpu fullload --cpu-percent <percent> --timeout <duration>
```

参数说明：
- `--cpu-percent`：CPU 使用率百分比（如 80、90）
- `--cpu-count`：可选，指定占用的核心数（如仅压 2 核）
- `--cpu-list`：可选，指定核心索引（如 0,1）
- `--timeout`：超时自动恢复（秒）

3. 观察 CPU 使用率及应用性能变化

**注入验证**：
1. `top` 或 `mpstat -P ALL 1` 确认 CPU 使用率持续超过目标百分比
2. `uptime` 确认 Load Average 显著升高
3. 观察应用请求延迟是否增大

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `top` 确认 CPU 使用率恢复正常水平
2. 确认应用请求延迟恢复正常

**基准事实**：
- **根因**：异常进程大量占用 CPU，导致主机 CPU 使用率过高，影响同主机上所有应用性能
- **必现现象**：CPU 使用率持续超过目标百分比；Load Average 显著升高；应用响应变慢

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用 `stress-ng` 实现等效 CPU 满载。

前提条件：主机已安装 `stress-ng`。

先探测是否安装（优先用 shell 内建 `command -v`：不依赖额外装包，也不依赖安装路径）：
```bash
command -v stress-ng
```

判读：返回路径 = 已安装；返回 `(no output)` = **未安装**（探测本身是成功的，不要重复执行）。

若未安装或需确认具体路径，再按绝对路径确认。**不要只猜 `/usr/bin`**——一次列出所有候选路径，`ls` 只会列出真实存在的那个：
```bash
ls /usr/bin/stress-ng /usr/sbin/stress-ng /usr/local/bin/stress-ng
```

> 路径提示：`stress-ng`/`stress`/`dd` 等通常在 `/usr/bin`，而 `iptables`/`ip6tables`/`nft`/`tc` 等网络类注入二进制通常在 `/usr/sbin` 或 `/sbin`。只探测 `/usr/bin` 会把已安装的二进制误判为「未安装」。

注入命令（探测到 stress-ng 时）：
```bash
# 使用 stress-ng 注入 CPU 压力，--timeout 到期后自动退出
stress-ng --cpu 0 --cpu-load <percent> --timeout <duration>s
```

恢复命令：
```bash
# stress-ng 在 --timeout 到期后自动退出；如需提前恢复：
kill <stress-ng-pid>
```

注意事项：
- **若 `stress-ng` 未安装**：主机原生 CPU 满载降级不可行（`host_inject` 的安全护栏仅放行故障二进制，`sh`/`bash` 等 shell 解释器被拒，无法用 shell 死循环兜底）。此时应明确报告“host 原生 CPU 降级不可行”并交回 replan 重新规划（例如换用其它已安装工具或改用 ChaosBlade）。
- 与 ChaosBlade 相比，原生方式缺少集中式实验记录，需自行记录 PID 以便恢复。
