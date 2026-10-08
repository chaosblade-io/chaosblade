**用例名称** 服务被停止 导致 Host_系统服务异常

## 故障现象
1. Systemd 管理的服务被意外停止
2. 服务端口不再监听
3. 依赖该服务的其他组件报连接失败

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标服务名：`systemctl list-units --type=service | grep <service>`
3. 确认该服务的依赖关系

## 演练步骤
1. 确认目标服务当前状态：`systemctl status <service>`
2. 使用 ChaosBlade 注入服务停止

```bash
blade create systemd stop --service <service-name> --timeout <duration>
```

参数说明：
- `--service`：Systemd 服务名（必填，不需要 .service 后缀）
- `--ignore-not-found`：可选，服务不存在时不报错
- `--timeout`：超时自动恢复（秒），恢复时会自动 start 该服务

3. 观察服务停止后的系统状态

## 注入验证
1. `systemctl status <service>` 确认服务状态为 inactive/dead
2. `ss -tlnp | grep <port>` 确认端口不再监听（仅适用于有监听端口的服务；无端口服务如 atd/crond 跳过此条，以第 1 条 systemctl 状态为准）
3. 观察依赖该服务的其他组件是否报错

## 注入恢复
```bash
blade destroy <experiment-uid>
```

> destroy 会自动执行 `systemctl start <service>` 恢复服务

## 恢复验证
1. `systemctl status <service>` 确认服务状态为 active/running
2. 确认端口恢复监听
3. 确认依赖组件恢复正常

## 基准事实
- **根因**：关键系统服务被异常停止（人为误操作、资源不足触发自动停止等）
- **必现现象**：服务状态为 inactive；端口不再监听；依赖组件报连接失败

---

## 降级方案（原生命令）

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

注入命令（**先武装定时恢复，再注入**；到期自动拉起服务，补齐自恢复能力。
两条命令分两次独立执行——执行通道不支持 && 串联；武装成功（输出含 Running timer as unit）后再 stop）：
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
- systemctl stop 会触发服务的 ExecStop 优雅停止流程
- 如果服务配置了 Restart=always，需要先 mask 服务再 stop（武装还原需包含 unmask）：
  ```bash
  # 三条命令逐条独立执行（执行通道不支持 && 串联）；前一条成功返回后再执行下一条
  systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-<service-name> \
    sh -c 'systemctl unmask <service-name> && systemctl start <service-name>'
  systemctl mask <service-name>
  systemctl stop <service-name>
  ```
  提前恢复时：
  ```bash
  systemctl stop blade-restore-<service-name>.timer 2>/dev/null
  systemctl unmask <service-name>
  systemctl start <service-name>
  ```
- 自恢复基于注入前武装的 systemd-run transient timer（到期自动拉起服务）；提前恢复仍用上方手动命令
- 同名 transient timer 重复武装会报 `Unit blade-restore-<service-name>.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-restore-<service-name>.service; systemctl reset-failed blade-restore-<service-name>.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
