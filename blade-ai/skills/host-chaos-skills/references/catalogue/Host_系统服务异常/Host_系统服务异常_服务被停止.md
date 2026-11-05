**用例名称** 服务被停止 导致 Host_系统服务异常

## 故障定位
状态型故障——窗口内 systemd 托管的目标服务被**优雅停止（graceful `systemctl stop`，走服务的
`ExecStop`/SIGTERM 清理流程）**并持续 inactive，服务端口在整窗不可用，直到窗口结束（手段1
`--timeout` 到期 destroy 自动 `systemctl start` / 手段2 武装的 systemd timer 到期 `systemctl start`）
即恢复。

**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是对目标 unit 执行
graceful `systemctl stop`、到期 `systemctl start` 恢复），按环境能力选用：主机装有 ChaosBlade
（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理）；否则手段2。

- **本质是 graceful stop，严禁与「进程被杀死」互替**：本用例走服务的 `ExecStop`/SIGTERM 优雅停止
  （模拟运维误操作 `systemctl stop`、依赖编排停服、资源回收触发的正常停止流程）；sibling
  `Host_进程异常_进程被杀死` 是 abrupt `kill -9`（无清理地立即消失，模拟崩溃 / OOM-kill / 误杀）。
  两者故障动作本质不同，**严禁互替**（否则范畴合并）。
- **graceful stop 不需要 mask（与被杀死相反）**：`systemctl stop` 是**意图性停止**，systemd 的
  `Restart=always/on-failure` 只在服务**意外退出**（崩溃 / 被信号杀）时触发自动拉起，**不**对显式
  `systemctl stop` 生效——故无论目标是否 `Restart=always`，一次 `systemctl stop` 即让服务持续
  inactive 到窗口结束，**无需 mask**。（对比：#12「进程被杀死」用 `kill -9` 是意外退出，会触发
  `Restart=`，必须先 `systemctl mask` 破守护重启才能持续死亡。把 sibling 的 mask latch 搬到本用例
  是多余动作，且引入 mask 残留风险。）
- **systemd-scoped**：本用例针对 systemd 托管的服务，手段1（blade systemd 执行器）与手段2（systemctl）
  都要求主机有 systemd；无 systemd 环境（最小容器 / 裸进程）不适用本用例（裸进程持续死亡改由
  「进程被杀死」形态B 覆盖）。
- `duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认，不得默认成一次性停止。

## 故障现象
1. systemd 管理的服务被意外停止，状态 inactive/dead
2. 服务端口不再监听
3. 依赖该服务的其他组件报连接失败

## 资源准备
1. 确认目标主机有 systemd（`systemctl is-system-running`，只读免管道）；手段1 另需确认 ChaosBlade 可用（`blade version`）
2. 确认目标服务存在与当前状态：`systemctl is-active <service>`（只读免管道，应为 active）
3. 确认目标服务监听端口（若有）：`ss -H -tlnp sport = :<port>`（ss 原生端口过滤，免管道；无端口服务如 atd/crond 跳过）
4. 确认 `duration_seconds`（故障窗口）已明确

## 演练步骤
1. 记录基线：`systemctl is-active <service>`（应为 active）、`ss -H -tlnp sport = :<port>`（有端口服务应在监听）
2. 注入 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）：

```bash
blade create systemd stop --service <service-name> --timeout <duration>
```

参数说明：
- `--service`：systemd 服务名（必填，不需要 .service 后缀）
- `--ignore-not-found`：可选，服务不存在时不报错
- `--timeout`：超时自动恢复（秒），到期 destroy 会自动 `systemctl start` 该服务

3. 观察服务停止后的系统状态

## 注入验证
1. `systemctl is-active <service>`（只读免管道）确认服务状态为 `inactive`/`failed`
2. `ss -H -tlnp sport = :<port>`（ss 原生端口过滤，免管道；空输出=不监听）确认端口不再监听（仅适用于有监听端口的服务；无端口服务如 atd/crond 跳过此条，以第 1 条 systemctl 状态为准）
3. 观察依赖该服务的其他组件是否报连接失败
4. **持续性检查（必做）**——判据是"无外部干预下服务将持续停止到窗口结束"。graceful stop 是**意图性
   停止**，systemd 不会自动拉起（`Restart=` 不对显式 stop 生效），故停止状态由构造持续，白盒主证即时
   且单独充分：`systemctl is-active <service>` = `inactive` 且 `systemctl show <service> -p NRestarts`
   不增长，即证明服务持续停止、未被守护重启。手段2 可另查武装 timer 仍 pending
   （`systemctl list-timers --all` 含 `blade-restore-<service>.timer`；瞬态 timer 须 `--all` 方可见），到期才会拉起。

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
> destroy 会自动执行 `systemctl start <service>` 恢复服务

**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的 timer 再 `systemctl start`）

## 恢复验证
1. `systemctl is-active <service>`（只读免管道）确认服务状态为 `active`
2. `ss -H -tlnp sport = :<port>`（ss 原生端口过滤，免管道）确认端口恢复监听（有端口服务）
3. 确认依赖组件恢复正常

## 基准事实
- **根因**：关键系统服务被优雅停止（人为误操作 `systemctl stop`、依赖编排停服、资源回收触发正常停止流程等）
- **必现现象**：服务状态持续 inactive；端口不再监听；依赖组件报连接失败；窗口结束后自动 `systemctl start` 恢复

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（都是 graceful `systemctl stop` + 到期 `systemctl start`）；主机未装 ChaosBlade 时选用本手段。

前提条件：主机有 systemd（`systemctl is-system-running` 可达）；自恢复载体用 systemd-run transient timer。本用例 systemd-scoped，无 systemd 环境不适用（见故障定位）。

注入命令（**先武装定时恢复，再 graceful stop**；到期自动拉起服务，补齐自恢复能力。
两条命令分两次独立执行——执行通道不支持 && 串联；武装成功（输出含 `Running timer as unit`）后再 stop）：
```bash
# <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-<service-name> \
  systemctl start <service-name>
systemctl stop <service-name>
```

恢复命令（提前恢复）：
```bash
# 先终止武装的定时器，再手动拉起
systemctl stop blade-restore-<service-name>.timer 2>/dev/null
systemctl start <service-name>
```

注意事项：
- `systemctl stop` 触发服务的 `ExecStop`/SIGTERM 优雅停止流程（区别于「进程被杀死」的 `kill -9` abrupt 强杀，见故障定位）
- **无需 mask**：graceful `systemctl stop` 是意图性停止，`Restart=always/on-failure` 不会自动拉起被显式停止的服务（实测坐实：`Restart=always` 服务 stop 后 `NRestarts` 不增长、持续 inactive、端口不监听）；mask 只在「进程被杀死」的 abrupt kill 场景才需要（破守护重启）。**严禁把 sibling 的 mask latch 逻辑搬到本用例**（否则多余 mask/unmask + mask 残留风险 + 范畴合并）
- 自恢复基于注入前武装的 systemd-run transient timer（到期自动 `systemctl start`）；提前恢复仍用上方手动命令
- 同名 transient timer 重复武装会报 `Unit blade-restore-<service-name>.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-restore-<service-name>.service; systemctl reset-failed blade-restore-<service-name>.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
