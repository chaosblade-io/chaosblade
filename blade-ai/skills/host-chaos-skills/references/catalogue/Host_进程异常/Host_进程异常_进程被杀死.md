**用例名称** 进程被杀死 导致 Host_进程异常

## 故障定位
持续型故障——故障窗口内目标进程被**强制杀死（SIGKILL）且无法被拉起**，服务端口
在整窗持续不可用。**持续性来自"杀死后不能再起"，不是"反复杀"**：

- **本质**：一次 abrupt `kill -9` 让进程无清理地立即消失（模拟崩溃 / OOM-kill /
  误杀）。若目标有守护自动拉起（systemd `Restart=always/on-failure`），单杀只是
  数秒 blip（无演练价值）——必须**先破守护重启（`systemctl mask`）再杀**，让被杀
  进程无法被拉起，故障才贯穿整窗；若目标本就无自动拉起（裸进程 / `Restart=no`），
  单杀即整窗持续死亡。两条路径的持续机制都是**静态持续死亡**，非"拉起-杀死风暴"。
- **为什么不做 kill 循环风暴**：紧凑风暴需无时限反复 kill（违反有界窗口契约），
  或触达 systemd 重启限速（`StartLimitIntervalSec`）——而 Agent 逐轮工具调用的
  间隔远大于限速窗、systemd 又会合并同 unit 的并发 job，风暴在 Agent 执行模型下
  **结构性不可达**。且守卫准入面（防任意代码执行）只放行 `kill -9 <数字PID>` 单次
  形式，不放行载荷内 `systemctl kill` 的有界循环。故本用例不实现风暴，以"杀死 +
  破拉起 = 静态持续死亡"达成同等的窗口内持续不可用。
- **本用例不提供一次性杀死**：上游取证（chaosblade-exec-os `exec/process/process_kill.go`
  `KillProcessExecutor.Exec`）：`blade create process kill` 仅在实验创建时发送**一次**信号——
  `--count` 参数虽在声明中存在，但执行器**从不读取**，不存在任何"持续杀死"机制；
  `--timeout` 只控制实验记录何时销毁，destroy 也不会恢复被杀死的进程。
- `duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认，不得默认成一次性操作。

## 故障现象
1. 目标进程消失，且在整个窗口内不再复现（守护被 mask / 本就无守护）
2. 服务端口在窗口内持续不监听
3. 形态A：`systemctl is-enabled <service>` = `masked`，`is-active` = `inactive`/`failed`；
   systemd 无法拉起被杀进程（mask 阻断重启）
4. 形态B：进程死后无拉起记录，直到武装的 timer 到期才恢复

## 资源准备
1. **确认守护自动拉起行为（决定注入形态）**：
   - 有对应 unit：`systemctl show <service> -p Restart`（read-only，免管道）——
     `Restart=always`/`on-failure` → **形态A（须先 mask 破重启）**；
     `Restart=no` → **形态B（单杀即持续死亡，无需 mask）**
   - 无 unit 托管的裸进程 → **形态B**（注入前必须先武装定时拉起，否则无法恢复）
2. 确认目标进程名 / PID：`ps -C <process> -o pid,comm`（按名免管道）；
   形态A 主进程 PID 走 `systemctl show <service> -p MainPID`（精确，免管道）
3. 确认 `duration_seconds`（故障窗口）已明确

## 演练步骤
1. 确认目标进程正在运行并记录监听端口：
   `ps -C <process> -o pid,comm`（按名免管道）；`ss -tlnp`（-p 输出含进程名，免管道直接读目标端口）
2. 注入 —— 两形态都是"**先武装定时恢复载体，再 abrupt `kill -9`**"，区别只在形态A
   多一步 `systemctl mask` 破守护重启。**三条 / 两条命令均分次独立执行**（执行通道不
   支持 `&&` 串联）；武装成功（输出含 `Running timer as unit`）后再继续。`<recovery-seconds>`
   一律取 prompt 下发的 `recovery_timer_seconds`（= duration + grace，见 SKILL.md 双数窗口契约）。

   **形态A（有守护自动拉起：`Restart=always/on-failure`，主路径）**：
   ```bash
   # ① 武装恢复载体（timer 载荷必须单行；到期 unmask + 清 failed 标记 + 拉起）
   systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-<service> sh -c 'systemctl unmask <service>; systemctl reset-failed <service>; systemctl start <service>'
   # ② mask 破守护重启（否则 kill 后 systemd 立即拉起，只是数秒 blip）
   systemctl mask <service>
   # ③ abrupt 强杀主进程（<MainPID> 由 host_read `systemctl show <service> -p MainPID` 解析得到）
   kill -9 <MainPID>
   ```
   - mask 后 unit 不可被启动（含依赖拉起 / 自动重启），`kill -9` 让主进程无清理地消失，
     故障即静态持续到 timer 到期 unmask+start
   - 武装与注入须紧邻（≤60s），grace 覆盖间隙；单杀 + mask latch 天然贯穿整窗，无风暴侵蚀

   **形态B（无自动拉起：裸进程 / `Restart=no`）**：
   ```bash
   # ① 武装定时拉起（裸进程用完整启动命令；Restart=no 的 unit 用 systemctl start）
   systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-<process-name> sh -c '<应用启动命令 或 systemctl start <service>>'
   # ② abrupt 强杀（<pid> 由 host_read `ps -C <process> -o pid,comm` 或 `pgrep -f "[<name>]"` 解析得到）
   kill -9 <pid>
   ```
   - 启动命令必须写全（工作目录、环境变量、用户）；timer 载荷同样必须单行
   - 无守护 → 单杀即整窗保持死亡（状态型故障），timer 到期拉起完成恢复
   - `pgrep -f` 用 `[首字符]` 括号技巧（如 `pgrep -f "[m]yapp"`）防模式自匹配；
     已知进程名优先 `ps -C <process>`（按 comm 精确匹配，无 -f 全 cmdline 自匹配问题）

## 注入验证
1. `ps -C <process> -o pid,comm`（按名免管道）确认进程不存在
2. `ss -H -tlnp sport = :<port>`（ss 原生端口过滤，免管道；空输出=不监听）确认服务端口不再监听
3. 形态A：`systemctl is-enabled <service>` = `masked` 且 `systemctl is-active <service>` =
   `inactive`/`failed`（read-only，免管道）——证明守护被破、进程无法被拉起
4. **持续性检查（必做）**——判据是"无外部干预下故障机制仍在运转"。本步证明的是
   持续性（机制将运转到窗口结束），不是效果存在——效果已由第 1-3 步证明；
   对持续性命题，机制状态就是直接证据，白盒主证即时且单独充分：
   - **白盒主证（即时，单独充分）**：形态A——`systemctl is-enabled <service>` = `masked`
     即"守护被破、进程被杀后无法拉起"由构造成立，故障静态持续到 timer 到期；
     形态B——武装 timer 仍 pending（`systemctl list-timers --all` 含 blade-restore-<process-name>.timer；瞬态 timer 须 `--all` 方可见），
     timer 未触发即进程将持续死亡到窗口结束。本步即完成，无需佐证窗口
   - **有界佐证（仅当机制不可查时的回退）**：静观短窗口后端口仍不监听即强确认
   - 若窗口内提前恢复，说明故障窗口契约未达成，必须如实报告实际持续时长

## 注入恢复
1. 等待 timer 到期自恢复（主保险）：形态A 载荷 `unmask + reset-failed + start`；
   形态B 载荷执行启动命令 / `systemctl start`
2. 提前恢复：
   ```bash
   # 形态A：先停武装 timer（避免迟到重复拉起），再 unmask + 清 failed + 拉起
   systemctl stop blade-restore-<service>.timer 2>/dev/null
   systemctl unmask <service>
   systemctl reset-failed <service> 2>/dev/null; systemctl start <service>
   # 形态B：停掉已武装的 timer 再手动拉起
   systemctl stop blade-restore-<process-name>.timer 2>/dev/null
   systemctl start <service>   # 或执行应用启动命令
   ```

## 恢复验证
1. `ps -C <process> -o pid,comm`（按名免管道）确认进程恢复运行
2. `ss -H -tlnp sport = :<port>`（ss 原生端口过滤，免管道）确认端口恢复监听
3. 形态A：`systemctl is-enabled <service>` 不再是 `masked`（已 unmask）且
   `systemctl is-active <service>` = `active`；形态B：`systemctl status <service>` = active (running)

## 基准事实
- **根因**：关键进程在窗口内被强制杀死（SIGKILL）且无法被守护拉起，服务持续中断
- **必现现象**：进程消失 / 端口不监听贯穿整窗（形态A 伴随 `is-enabled=masked`，
  形态B 死后无拉起）；窗口结束 timer 到期后恢复

注意事项：
- **与「服务被停止」的边界（严禁互替）**：本用例是 **abrupt `kill -9 <数字PID>`**——
  进程无清理地立即消失，模拟崩溃 / OOM-kill / 误杀；`Host_系统服务异常_服务被停止`
  是 **graceful `systemctl stop`**——触发服务的 `ExecStop`/SIGTERM 优雅停止流程。
  两者对 `Restart=always` 服务都用 `mask` 作持续 latch，但**故障动作本质不同**：
  被杀死用 `kill -9`（不走 ExecStop），被停止用 `systemctl stop`（走 ExecStop）。
- **守卫准入面（构造命令时只能用这些原语）**：
  - 杀死：只准入 `kill -9 <显式数字PID>`（PID > 1）。**不准入** `kill -9 <进程名>`、
    `kill -9 $(...)` 命令替换（exec-form 无 shell，不解析）——须先 host_read
    `ps -C <process>` / `pgrep -f` / `systemctl show -p MainPID` 解析出具体数字 PID 再 kill
  - `pkill` / `killall` / `systemctl kill` **均不在守卫白名单**，不可用
  - 破重启 / 恢复：`systemctl mask`/`unmask`/`reset-failed`/`start`/`stop` 准入；
    `enable`/`disable`/`daemon-reload`/`reboot` 等机器级动词不准入
  - 载体：只准入 `systemd-run --on-active=<N>s --unit=<name> <载荷>` 定时形态；
    **同步 `systemd-run <cmd>`（无 `--on-active`）= 任意代码执行绕过，被拒**
- SIGKILL 不可被捕获，进程不做任何清理逻辑；需要优雅退出时改用 SIGTERM
  （`kill -15 <PID>`）——但那已属「服务被停止」语义，非本用例
- **mask 残留卫生**：形态A 恢复必须 `unmask`，否则服务永久不可启动。武装载荷已含
  `unmask`（主保险）；提前恢复也须显式 `unmask`（见注入恢复）
- 同名 transient unit 重复武装会报 `Unit ... was already loaded`（上次武装失败时
  unit 以 failed 状态残留所致）；重武装前先清理残留：
  `systemctl stop <unit>.service; systemctl reset-failed <unit>.service`
- 严禁使用无时限的反复手动 kill（无墙钟边界，违反故障窗口契约）
- 不使用 blade `process kill`：上游取证为一次性信号且 `--count` 执行器从不读取
  （见故障定位），不满足持续型故障要求
