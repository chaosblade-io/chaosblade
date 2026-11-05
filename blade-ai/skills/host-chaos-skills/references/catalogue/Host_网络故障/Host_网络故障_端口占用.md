**用例名称** 端口占用 导致 Host_网络故障

## 故障定位
持续型故障——占用进程绑定目标端口后持续持有，贯穿整个故障窗口，令需要使用该端口的真实服务无法 bind（`Address already in use`）；窗口结束（手段1 `--timeout` 到期实验销毁释放端口 / 手段2 武装的自恢复载体到期 `fuser -k <port>/tcp` 杀掉占用进程）即自动恢复。

**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是让一个进程占住目标端口、真实服务无法绑定），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理，且 `--force` 可抢占已被占用的端口）；否则手段2（`nc` 监听占用，仅能占用当前空闲端口）。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认，不得默认成一次性占用。

- **占用的本质是「端口被非预期进程持有」**：真实故障场景是残留进程 / 错误配置的服务 / 端口冲突占住了目标端口，使合法服务启动即报 `Address already in use`。演练用一个 benign 占用进程复现该状态，**不是** DoS 攻击、**不是**反弹 shell（手段2 明确禁止 `nc -e/-c` 带命令执行的形态，只允许 `-l … -k` 纯监听）。
- **`--force` 会杀死当前端口持有者（手段1 爆炸半径 caveat）**：手段1 `--force` 的语义是「强制杀死正在使用该端口的进程后再占用」。故注入前必须确认目标端口的当前持有者是**演练专用靶**（或本就空闲），**严禁**对生产服务端口用 `--force`（会误杀生产进程）。手段2（nc）无抢占能力，只能占用当前空闲端口。
- **判据用 ss 端口过滤，勿单查 IPv4 内核表**：占用进程（尤其 busybox `nc -l -p <port>` 不带 host 时）可能绑定 IPv6 通配地址（socket 记在 `/proc/net/tcp6`、`/proc/net/tcp` IPv4 表为空）。故端口占用/释放判据一律用 `ss -H -tlnp sport = :<port>`（ss 原生端口过滤，免管道，同时覆盖 v4/v6），**不得**以单查 `/proc/net/tcp` 读空即判「未占用 / 交叉验证失败」。

## 故障现象
1. 应用启动失败，端口已被占用（Address already in use）
2. 服务无法绑定到指定端口
3. 监听端口冲突

## 资源准备
1. 手段1 确认目标主机上 ChaosBlade 已安装（`blade version`，只读）；用 `--force` 前须确认目标端口当前持有者是演练专用靶或空闲（见故障定位 caveat）
2. 确认目标端口号及当前占用情况：`ss -H -tlnp sport = :<port>`（ss 原生端口过滤，免管道；空输出=当前空闲）
3. 确认 `duration_seconds`（故障窗口）已明确

## 演练步骤
1. 记录基线：`ss -H -tlnp sport = :<port>`（确认目标端口当前占用情况；若为演练专用靶服务占用，记录其 PID 以便恢复后核对）
2. 注入端口占用 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）：

```bash
blade create network occupy --port <target-port> --force --timeout <duration>
```

参数说明：
- `--port`：目标端口号（必填）
- `--force`：强制杀死正在使用该端口的进程后占用（**爆炸半径 caveat**：会杀死当前持有者，须确认是演练靶 / 空闲，见故障定位）
- `--timeout`：超时自动恢复（秒），到期 destroy 释放端口

3. 观察应用启动或端口绑定情况

## 注入验证
1. `ss -H -tlnp sport = :<port>`（ss 原生端口过滤，免管道）确认端口被占用进程（手段1 = ChaosBlade occupy 进程 / 手段2 = nc）持有、处于 LISTEN
2. **（主证据，必做）反向 bind 探针**：尝试让真实服务（演练靶）绑定该端口，确认报 `Address already in use` / bind 失败——这是端口占用的**行为级正证**（区别于仅看 ss 有监听，坐实端口对合法服务确实不可用）
3. **持续性检查（必做）**——判据是“无外部干预下端口将被持续占用到窗口结束”。白盒主证：`ss -H -tlnp sport = :<port>` 持续显示占用进程 PID 在 LISTEN，且恢复载体已武装（手段1 `blade status --uid <uid>` 仍 Success、`--timeout` 未到 / 手段2 `systemctl list-timers --all` 含 `blade-portocc-<port>.timer` pending；瞬态 timer 须 `--all` 方可见）
4. （诊断，仅当上述效果证据未出现时执行）确认实验已生效：`blade status --uid <experiment-uid>`（机制回执，为机制代言不为结果代言，用于定位失败层〔占用进程未起 vs 起了未持有端口〕，不构成效果证据）

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复，释放端口）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的 timer 再 `fuser -k <port>/tcp` 杀掉占用进程）

## 恢复验证
1. `ss -H -tlnp sport = :<port>`（ss 原生端口过滤，免管道）确认占用进程已消失、端口释放（或仅由真实靶服务持有）
2. **反向 bind 探针**：确认目标应用（演练靶）可正常启动并绑定端口（bind 成功、不再报 `Address already in use`）——恢复的行为级正证

## 基准事实
- **根因**：目标端口被异常进程占用，导致需要使用该端口的应用无法启动或绑定
- **必现现象**：`ss -H -tlnp sport = :<port>` 显示端口被占用进程持有；真实服务 bind 报 Address already in use；窗口结束后占用进程被清除、端口释放、真实服务可重新 bind

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（一个进程占住目标端口、真实服务无法 bind）；主机未装 ChaosBlade 时选用本手段。**局限**：nc 无抢占能力，只能占用当前空闲端口（手段1 `--force` 可抢占已占用端口）。

前提条件：主机具备 `nc`（netcat）；自恢复载体用 systemd-run transient timer（Tier-1，主机有 systemd）。

注入命令（**先武装定时释放，再监听占用**——timer 由宿主机 systemd(PID 1) 管理，到期自动 `fuser -k <port>/tcp` 杀掉占用进程、释放端口，补齐自恢复能力）：
```bash
# 1) 先武装定时释放（到期 fuser -k 杀掉占用该端口的进程）。
#    两条命令分两次独立执行（执行通道不支持 && 串联）；武装成功（输出含 Running timer as unit）后再执行监听
# <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
systemd-run --on-active=<recovery-seconds>s --unit=blade-portocc-<port> \
  fuser -k <port>/tcp
# 2) 用 nc 监听占用端口，真实服务将无法 bind
#    只允许监听形态：带命令执行的 -e/-c 是反弹 shell，不是故障，会被拒绝
nc -l -p <port> -k
```

恢复命令（timer 到期前可提前手动恢复）：
```bash
# 1) 先停 timer（必须先于杀进程执行：避免监听解除、真实服务抢回端口后，
#    到期 fuser -k 误杀真实服务）
systemctl stop blade-portocc-<port>.timer 2>/dev/null

# 2) 取占用该端口的 PID 并杀掉；也可用 fuser 一步完成
#    （目标必须是 <port>/tcp 形态，不能是路径）
fuser <port>/tcp
kill <pid>
fuser -k <port>/tcp
```

注意事项：
- nc 的 `-k` 参数表示保持监听（accept 后不退出）
- **busybox nc 绑 IPv6 通配**：`nc -l -p <port>` 不带 host 时可能绑 IPv6 通配地址（`/proc/net/tcp6` 有行、`/proc/net/tcp` IPv4 表为空）；端口占用/释放判据一律用 `ss -H -tlnp sport = :<port>`（覆盖 v4/v6），勿单查 IPv4 内核表读空即判失败（见故障定位）
- 原生方式无法强制抢占已被其他进程占用的端口（无 `--force` 等价能力）
- 自恢复基于 systemd-run transient timer 到期 `fuser -k <port>/tcp`，杀掉当时仍占用该端口的进程、端口释放；提前恢复须先停 timer 再杀进程（见上）
- 若监听已提前解除且端口被真实服务接管，必须先 `systemctl stop blade-portocc-<port>.timer` 停掉 timer，否则到期会误杀真实服务
- 同名 transient timer 重复武装会报 `Unit blade-portocc-<port>.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-portocc-<port>.service; systemctl reset-failed blade-portocc-<port>.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
