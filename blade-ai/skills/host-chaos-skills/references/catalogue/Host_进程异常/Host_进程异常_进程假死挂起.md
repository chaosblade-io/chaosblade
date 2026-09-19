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
3. 健康检查（如 LB 心跳）是否触发报警（报警由 LB 多轮失败检测累积触发，存在传播
   延迟——首查未见报警不构成反证，挂起的直接证据是第 1 条的进程 T 状态）

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

注入命令（**先武装定时恢复，再注入**；到期自动发送 SIGCONT，补齐自恢复能力）：
```bash
# 1) 先取 PID（输出可能多行，逐个处理）
pgrep -f <process-name>

# 2) 先武装定时 SIGCONT（定时器由宿主机 systemd(PID 1) 管理，到期重新 pgrep 取 PID），再挂起。
#    两条命令分两次独立执行（执行通道不支持 && 串联）；武装成功（输出含 Running timer as unit）后再执行挂起
systemd-run --on-active=<recovery-seconds>s --unit=blade-cont-<process-name> \
  sh -c 'kill -CONT $(pgrep -f <process-name>)'
kill -STOP <pid>
```

恢复命令（提前恢复；SIGCONT 幂等，武装的定时器后续再触发也无副作用）：
```bash
# 可选：先终止武装的定时器
systemctl stop blade-cont-<process-name>.timer 2>/dev/null
# 对注入时记录的同一 PID 发送 SIGCONT
kill -CONT <pid>
```

注意事项：
- SIGSTOP 信号无法被进程捕获或忽略，进程必定被挂起
- 与 kill 不同，stop 后进程仍存在，资源未释放
- 自恢复基于注入前武装的 systemd-run transient timer（到期自动 SIGCONT）；提前恢复仍用上方手动命令
- 同名 transient timer 重复武装会报 `Unit blade-cont-<process-name>.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-cont-<process-name>.service; systemctl reset-failed blade-cont-<process-name>.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
