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

注入命令：
```bash
# 1) 先查本机用的是哪个时间同步服务（三者取其一，不要盲试）
systemctl is-active ntpd
systemctl is-active chronyd

# 2) 停掉实际在跑的那个，防止时间被自动校正
systemctl stop chronyd

# 若两者都没有，改用 timedatectl 关闭同步
timedatectl set-ntp false

# 修改系统时间（向前偏移 2 小时）
date -s "+2 hours"

# 或向后偏移 30 分钟
date -s "-30 minutes"
```

恢复命令：
```bash
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
- 原生方式修改后，NTP 可能在短时间内自动校正回来
- 无自动超时恢复，必须手动恢复
