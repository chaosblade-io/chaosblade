**用例名称** 端口占用 导致 Host_网络故障

**故障现象**：
1. 应用启动失败，端口已被占用（Address already in use）
2. 服务无法绑定到指定端口
3. 监听端口冲突

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标端口号及当前占用情况：`ss -tlnp | grep <port>`

**演练步骤**：
1. 确认目标端口当前未被占用或记录当前占用情况
2. 使用 ChaosBlade 注入端口占用

```bash
blade create network occupy --port <target-port> --force --timeout <duration>
```

参数说明：
- `--port`：目标端口号（必填）
- `--force`：强制杀死正在使用该端口的进程后占用
- `--timeout`：超时自动恢复（秒）

3. 观察应用启动或端口绑定情况

**注入验证**：
1. `ss -tlnp | grep <port>` 确认端口被 ChaosBlade 进程占用
2. 尝试启动目标应用，确认报 "Address already in use" 错误
3. `netstat -tlnp | grep <port>` 确认监听状态

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `ss -tlnp | grep <port>` 确认端口释放
2. 确认目标应用可正常启动并绑定端口

**基准事实**：
- **根因**：目标端口被异常占用，导致需要使用该端口的应用无法启动或绑定
- **必现现象**：端口被占用；应用启动报 Address already in use；服务不可用

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

前提条件：主机需具备 `nc`（netcat）

注入命令：
```bash
# 用 nc 监听占用端口，真实服务将无法 bind
# 只允许监听形态：带命令执行的 -e/-c 是反弹 shell，不是故障，会被拒绝
nc -l -p <port> -k
```

恢复命令：
```bash
# 1) 取占用该端口的 PID
fuser <port>/tcp

# 2) 杀掉它；也可用 fuser 一步完成（目标必须是 <port>/tcp 形态，不能是路径）
kill <pid>
fuser -k <port>/tcp
```

注意事项：
- nc 的 `-k` 参数表示保持监听（accept 后不退出）
- 原生方式无法强制抢占已被其他进程占用的端口
- 无自动超时恢复，必须手动 kill 进程
