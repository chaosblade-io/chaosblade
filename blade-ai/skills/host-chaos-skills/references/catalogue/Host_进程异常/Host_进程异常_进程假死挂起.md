**用例名称** 进程假死挂起 导致 Host_进程异常

**故障现象**：
1. 目标进程存在但不响应任何请求
2. 服务端口监听但连接后无响应（超时）
3. 进程状态显示为 T（Stopped）

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标进程名或 PID：`ps aux | grep <process>`

**演练步骤**：
1. 确认目标进程正在运行：`ps aux | grep <process>`
2. 使用 ChaosBlade 注入进程假死（SIGSTOP）

```bash
blade create process stop --process <process-name> --timeout <duration>
```

参数说明：
- `--process`：进程名关键词
- `--process-cmd`：可选，按命令名匹配
- `--pid`：可选，直接指定 PID
- `--local-port`：可选，按监听端口匹配
- `--timeout`：超时自动恢复（秒），恢复时发送 SIGCONT

3. 观察进程状态及服务响应情况

**注入验证**：
1. `ps aux | grep <process>` 确认进程状态为 T（Stopped）
2. `curl --connect-timeout 5 <service-url>` 确认连接后无响应
3. 健康检查（如 LB 心跳）是否触发报警

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

> destroy 会自动发送 SIGCONT 恢复进程

**恢复验证**：
1. `ps aux | grep <process>` 确认进程状态恢复为 S/R
2. 确认服务请求响应恢复正常

**基准事实**：
- **根因**：进程被 SIGSTOP 挂起，虽然进程存在但完全不处理任何请求
- **必现现象**：进程存在但状态为 T；端口监听但不响应；健康检查超时

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

注入命令：
```bash
# 1) 先取 PID（输出可能多行，逐个处理）
pgrep -f <process-name>

# 2) 对取到的 PID 发送 SIGSTOP 挂起
kill -STOP <pid>
```

恢复命令：
```bash
# 对注入时记录的同一 PID 发送 SIGCONT
kill -CONT <pid>
```

注意事项：
- SIGSTOP 信号无法被进程捕获或忽略，进程必定被挂起
- 与 kill 不同，stop 后进程仍存在，资源未释放
- 原生方式无自动超时恢复，必须手动发送 SIGCONT
