**用例名称** 系统时钟漂移 导致 Host_时间偏移

**故障现象**：
1. 系统时间与真实时间不一致
2. 证书验证失败（SSL/TLS 证书过期判断异常）
3. 日志时间戳混乱，分布式系统因果序关系错乱
4. 定时任务触发异常（cron 提前或延迟执行）

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认监控系统可观测时间指标
3. 确认目标主机上是否运行 NTP 服务

**演练步骤**：
1. 记录当前系统时间：`date` 和 `timedatectl status`
2. 使用 ChaosBlade 注入时间偏移

```bash
blade create time travel --offset <offset> --timeout <duration>
```

参数说明：
- `--offset`：时间偏移量（必填），支持格式如 5m30s、-2h30m、1h
- `--disableNtp`：可选，是否禁用 NTP（默认 true；如系统不支持 NTP 设为 false）
- `--timeout`：超时自动恢复（秒）

3. 观察依赖时间的服务和组件的反应

**注入验证**：
1. `date` 确认系统时间已偏移
2. 尝试建立 HTTPS 连接，观察是否出现证书相关错误
3. 观察定时任务是否异常触发
4. 检查分布式系统日志时间戳一致性

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `date` 确认系统时间恢复正常
2. `timedatectl status` 确认 NTP 同步状态恢复
3. 确认应用时间相关功能恢复正常

**基准事实**：
- **根因**：系统时钟发生漂移（NTP 故障、硬件时钟异常等），导致时间敏感的功能异常
- **必现现象**：系统时间与实际时间不符；TLS 证书可能验证失败；定时任务异常；日志时间戳混乱

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

前提条件：具备 root 权限

注入命令（**先武装定时恢复，再注入**；到期自动重启时间同步服务并校时，补齐自恢复能力；
武装→停服务→改时间逐条独立执行，武装未确认成功（输出含 Running timer as unit）时不得执行偏移）：
```bash
# 1) 先查本机用的是哪个时间同步服务（三者取其一，不要盲试）
systemctl is-active ntpd
systemctl is-active chronyd

# 2) 武装定时恢复（定时器由宿主机 systemd(PID 1) 管理）→ 停服务 → 改时间。
#    三条命令分次独立执行（执行通道不支持 && 串联）；前一条成功返回（武装以输出含
#    Running timer as unit 为准）后再执行下一条，武装失败时不得继续
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-ntp \
  sh -c 'systemctl start chronyd; chronyc makestep 2>/dev/null || true'
systemctl stop chronyd
date -s "<offset>"   # 偏移量按演练目标确定，如 "+2 hours"（向前）、"-30 minutes"（向后）

# 若两者都没有，改用 timedatectl 关闭同步（武装对应还原，同样逐条独立执行）：
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-ntp \
  timedatectl set-ntp true
timedatectl set-ntp false
date -s "<offset>"
```

恢复命令（提前恢复；先停武装的定时器再手动还原）：
```bash
# 0) 终止武装的定时器
systemctl stop blade-restore-ntp.timer 2>/dev/null

# 1) 启回注入时停掉的那个服务（与注入步骤对应，不要盲试）
systemctl start chronyd

# 若注入时用的是 timedatectl
timedatectl set-ntp true

# 2) 强制拉一次时间（按实际可用工具选一条）
chronyc makestep
ntpdate pool.ntp.org
```

注意事项：
- 时间偏移会影响所有依赖系统时钟的应用（日志、证书、定时器、分布式一致性）
- 自恢复基于注入前武装的 systemd-run transient timer（到期自动启回时间同步服务并校时）；提前恢复仍用上方手动命令
- 同名 transient timer 重复武装会报 `Unit blade-restore-ntp.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-restore-ntp.service; systemctl reset-failed blade-restore-ntp.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
- 恢复后时间是否立即校回取决于偏移量与 chrony.conf `makestep` 阈值（如 `makestep 10 3` 表示仅启动后前 3 次更新中偏差 >10 秒才自动步进）：偏移量 ≤ 阈值时 chronyd 走纯 slew 缓慢修正（如 +10 秒偏移约 2.5 分钟收敛），时间敏感的恢复验证需预留等待窗口或手动 `date -s` 精确校回；`chronyc makestep` 在 chronyd 刚启动、尚未完成首次测量时执行是 no-op
