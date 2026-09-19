**用例名称** 进程被杀死 导致 Host_进程异常

**故障定位**：持续型故障——故障窗口内目标进程反复被杀死，守护机制（systemd）
每次拉起后立即被再次杀死，形成有界的"拉起-杀死"风暴，服务端口在窗口内持续不可用。
**本用例不提供一次性杀死**：上游取证（chaosblade-exec-os `exec/process/process_kill.go`
`KillProcessExecutor.Exec`）：`blade create process kill` 仅在实验创建时发送**一次**信号——
`--count` 参数虽在声明中存在，但执行器**从不读取**，不存在任何"持续杀死"机制；
`--timeout` 只控制实验记录何时销毁，destroy 也不会恢复被杀死的进程。
单次 kill 在 systemd 托管下数秒内自愈（不构成有效演练窗口）；无托管 的裸进程
被杀后则死过窗口（违反有界窗口契约）。`duration_seconds` 是必填的故障窗口契约，
未给定时先向用户确认，不得默认成一次性操作。

**故障现象**：
1. 目标进程反复消失，守护拉起后短暂复现即再次被杀（systemd 托管形态）
2. 服务端口在窗口内持续不监听
3. systemd 日志反复出现拉起记录；触达重启限速时 unit 进入 failed（Reached attempt limit）

**资源准备**：
1. 确认进程托管方式（决定注入形态）：
   - `systemctl status <service>` 或 `ps -o ppid= -p <pid>` —— 有对应 unit → systemd 托管（形态A）
   - 无 unit 托管 → 裸进程（形态B，注入前必须先武装定时拉起，否则无法恢复）
2. 确认目标进程名或 PID：`ps aux | grep <process>`
3. 确认 `duration_seconds`（故障窗口）已明确

**演练步骤**：
1. 确认目标进程正在运行并记录监听端口：
   `ps aux | grep <process>`；`ss -tlnp | grep <process>`
2. 注入 ——
   **形态A（systemd 托管，主路径）**：派发**墙钟时限的有界 kill 循环**。循环由
   systemd-run transient service 承载（宿主机 PID 1 管理，不依赖执行通道保持连接；
   载荷必须单行——transient unit 的 ExecStart 对载荷内换行解析失败）：
   ```bash
   systemd-run --unit=blade-killloop-<service> sh -c 'end=$(( $(date +%s) + <duration> )); i=0; while [ "$(date +%s)" -lt "$end" ] && [ $i -lt <rounds> ]; do systemctl kill -s SIGKILL <service>; i=$((i+1)); sleep <interval>; done'
   ```
   参数说明：
   - `<duration>`：故障窗口总时长（秒），取 `duration_seconds`；墙钟到期循环自停——
     这是主保险（用 `date +%s` 计时而非 `SECONDS`：后者是 bash 特性，`sh -c` 下不会自增）
   - `<rounds>`：轮数上限，是第二重保险；应满足 `<rounds> × <interval>` ≥ `<duration>`
   - `<interval>`：两轮杀死间隔（秒），建议 3-5，让 systemd 有时间拉起再杀，
     才能观察到完整的"拉起-杀死"风暴
   - 倒计时即循环本身：循环启动即注入即持续，无武装-注入间隙侵蚀故障窗口
     （见 SKILL.md 安全红线「故障窗口完整」）
   **形态B（无托管裸进程）**：先武装定时拉起（`recovery-seconds` 取 `duration`），
   再杀死。裸进程单次被杀即整窗保持死亡（状态型故障），timer 到期拉起完成恢复：
   ```bash
   systemd-run --on-active=<duration>s --unit=blade-restore-<process-name> sh -c '<应用启动命令>'
   pkill -9 -f "[<process-name首字符>]<process-name剩余部分>"
   ```
   - 启动命令必须写全（工作目录、环境变量、用户）；timer 载荷同样必须单行
   - 两条命令分两次独立执行（执行通道不支持 && 串联）；武装成功（输出含
     Running timer as unit）后再杀死
   - pkill 模式必须用 `[首字符]` 括号技巧（如 `pkill -9 -f "[m]yapp"`）防自匹配——
     否则 pkill -f 会命中武装命令自身 cmdline 中的进程名

**注入验证**：
1. `ps aux | grep <process>` 确认进程不存在。ps 是瞬时快照——形态A"拉起-杀死"风暴的
   拉起间隙可能抓到进程短暂复现，复现不构成反证（第 3 步 systemctl 的重启
   计数才是贯穿性证据）
2. `ss -tlnp | grep <port>` 确认服务端口不再监听
3. 形态A：`systemctl status <service>` 确认窗口内反复拉起；若触达重启限速进入
   failed（Reached attempt limit），进程保持死亡直到窗口结束，故障现象依然成立
4. **持续性检查（必做）**——判据是"无外部干预下故障机制仍在运转"。本步证明的是
   持续性（机制将运转到窗口结束），不是效果存在——效果已由第 1-3 步证明；
   对持续性命题，机制状态就是直接证据，白盒主证即时且单独充分：
   - **白盒主证（即时，单独充分）**：形态A——kill 循环 unit 仍 active
     （`systemctl status blade-killloop-<service>`），循环 active 即"持续在杀"
     由构造成立；形态B——武装 timer 仍 pending（`systemctl list-timers` 含
     blade-restore-<process-name>.timer），timer 未触发即进程将持续死亡到窗口结束。
     本步即完成，无需佐证窗口
   - **有界佐证（仅当机制不可查时的回退）**：静观短窗口后端口仍不监听即强确认
   - 若窗口内提前恢复，说明故障窗口契约未达成，必须如实报告实际持续时长

**注入恢复**：
1. 等待墙钟/timer 到期自恢复（主保险）：形态A 循环自停后 systemd 完成拉起；
   形态B timer 到期执行启动命令
2. 提前恢复：
   ```bash
   # 形态A：停掉循环，让 systemd 拉起（触达重启限速时先 reset-failed）
   systemctl stop blade-killloop-<service>.service
   systemctl reset-failed <service> 2>/dev/null; systemctl start <service>
   # 形态B：停掉已武装的 timer 再手动拉起（避免 timer 迟到重复拉起）
   systemctl stop blade-restore-<process-name>.timer 2>/dev/null
   systemctl start <service>   # 或执行应用启动命令
   ```

**恢复验证**：
1. `ps aux | grep <process>` 确认进程恢复运行
2. `ss -tlnp | grep <port>` 确认端口恢复监听
3. `systemctl status <service>` 确认 active (running) 且不再反复重启

**基准事实**：
- **根因**：关键进程在窗口内反复被杀死，服务持续中断
- **必现现象**：进程消失/端口不监听贯穿窗口（systemd 托管形态伴随拉起风暴）；窗口结束循环/timer 自停后恢复

注意事项：
- SIGKILL 不可被捕获，进程不做任何清理逻辑；需要优雅退出时改用 SIGTERM
  （`systemctl kill -s SIGTERM` / `pkill -15`）
- 同名 transient unit 重复武装会报 `Unit ... was already loaded`（上次武装失败时
  unit 以 failed 状态残留所致）；重武装前先清理残留：
  `systemctl stop <unit>.service; systemctl reset-failed <unit>.service`
- 严禁使用无时限的反复手动 kill（无墙钟边界，违反故障窗口契约）
- 不使用 blade `process kill`：上游取证为一次性信号且 `--count` 执行器从不读取
  （见故障定位），不满足持续型故障要求
