**用例名称** 进程被杀死 导致 Host_进程异常

**故障现象**：
1. 目标进程突然消失
2. 服务中断，端口不再监听
3. 如有守护进程（systemd/supervisor），进程可能被自动拉起

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标进程名或 PID：`ps aux | grep <process>`

**演练步骤**：
1. 确认目标进程正在运行：`ps aux | grep <process>`
2. 使用 ChaosBlade 注入进程杀死

```bash
blade create process kill --process <process-name> --signal 9 --timeout <duration>
```

参数说明：
- `--process`：进程名关键词（包含该关键词的进程将被杀死）
- `--process-cmd`：可选，按命令名匹配
- `--pid`：可选，直接指定 PID
- `--local-port`：可选，按监听端口匹配
- `--signal`：杀死信号（9=SIGKILL，15=SIGTERM）
- `--count`：可选，限制杀死次数（0=无限，配合 timeout 持续杀死）
- `--exclude-process`：可选，排除的进程名
- `--timeout`：超时自动恢复（秒）

3. 观察进程状态及服务可用性

**注入验证**：
1. `ps aux | grep <process>` 确认进程不存在
2. `ss -tlnp | grep <port>` 确认服务端口不再监听
3. 观察是否有守护进程自动拉起

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

> 注意：进程被 kill 后 destroy 实验不会恢复进程，需手动重启服务

**恢复验证**：
1. 手动重启服务：`systemctl start <service>` 或应用启动命令
2. 确认进程恢复运行，端口重新监听

**基准事实**：
- **根因**：关键进程被异常杀死，导致服务中断
- **必现现象**：进程消失；服务端口不再监听；依赖该服务的调用失败

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

注入命令：
```bash
# 1) 先取 PID（按进程名，或按端口用 fuser <port>/tcp 查）
pgrep -f <process-name>

# 2) 杀死取到的 PID
kill -9 <pid>

# 按端口一步杀死（需宿主机有 fuser）
fuser -k <port>/tcp
```

恢复命令：
```bash
# 手动重启服务
systemctl start <service>
# 或执行应用启动命令
```

注意事项：
- kill -9 发送 SIGKILL 信号，进程无法处理该信号（不会执行清理逻辑）
- 使用 kill -15 可让进程优雅退出
- 原生方式无法实现持续杀死（count + timeout 模式）
