**用例名称** 网络丢包隔离 导致 Host_网络故障

## 故障定位
持续型故障——iptables DROP 规则下到主机网络栈后持续丢包，贯穿整个故障窗口；窗口结束（`--timeout` 到期实验销毁 / 武装的自恢复载体到期 `iptables -D` 删除规则）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都是让匹配过滤条件的流量被丢弃、主机与该目标间通信中断），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（实验 UID 统一生命周期管理）；否则手段2。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 主机与匹配过滤条件的 IP/端口之间的数据包被丢弃，受影响方向取决于 `--network-traffic`：
   - `out`（出站）：本机主动发起的连接超时、`ping` 该目标不通
   - `in`（入站）：来自该来源的请求收不到，但**本机主动 ping/curl 出去仍可能正常**（回包方向未被拦截时）
2. TCP 连接超时
3. 依赖该网络路径的服务调用失败

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标 IP/端口及流量方向

## 演练步骤
1. 记录当前网络连通性基线：`ping <target-ip>` 或 `curl <target-url>`
2. 注入网络丢包/隔离 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create network drop --destination-ip <target-ip> --network-traffic out --timeout <duration>
```

参数说明：
- `--source-ip`：可选，源 IP 过滤
- `--destination-ip`：可选，目的 IP 过滤
- `--source-port`：可选，源端口过滤
- `--destination-port`：可选，目的端口过滤（支持逗号分隔多端口）
- `--network-traffic`：流量方向（in 入站 / out 出站）
- `--string-pattern`：可选，匹配包含特定字符串的数据包
- `--timeout`：超时自动恢复（秒）

3. 观察网络连通性及应用状态

## 注入验证
1. （诊断，仅当下述效果证据未出现时执行）确认实验已生效：
   ```bash
   blade status --uid <experiment-uid>
   ```
   状态为 Success/Running 即表示丢包规则已下到主机网络栈——这是机制回执，为机制代言不为结果代言，用于定位失败层（规则未下发 vs 下发未生效），不构成效果证据
2. **（主证据，必做）**（只做与本次 `--network-traffic` 匹配的分支）另一方向的现象在本次注入下**不可能出现**，直接标记为 `expected` 并跳过：
   - **`out`（出站）**：从本机向目标验证不通
     ```bash
     ping -c 4 <target-ip>
     curl --connect-timeout 5 <target-url>
     ```
     预期丢包率 100% / 连接超时。
   - **`in`（入站）**：**本机 `ping`/`curl` 出去仍通是预期，不是失败**。改为从对端（或另一台主机）向本机发起访问，确认收不到响应；或在本机用 `tcpdump -i any -n host <peer-ip>` 观察到包到达但无响应（`tcpdump` 工具面非普适——最小镜像默认不装、缺失即 `which` 空 / `exit 127`；缺失时走对端访问路径即可，不得反复重试）。
3. 观察应用错误日志中是否出现连接超时/拒绝

> ⚠️ 验证纪律：
> - **严禁为不适用的方向反复更换查询方式找证据**。注入 `in` 时本机主动出站必然可通，查到"通"是**必然**而非失败。
> - 验证必须落在**匹配过滤条件的目标上**。若注入指定了 `--destination-ip`/`--destination-port`/`--source-ip`/`--source-port`/`--string-pattern`，只有匹配的流量被丢弃；用其它地址或端口测试必然连通，**不要换目标反复重试**。
> - 同一事实（如实验是否生效）确认一次即可，不要重复查询。

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的定时器——Tier-1 `systemctl stop` / Tier-2 `pkill` 括号法——再 `iptables -D` 删除注入的 DROP 规则）

## 恢复验证
1. `ping <target-ip>` 确认网络恢复
2. 确认应用服务调用恢复正常

## 基准事实
- **根因**：匹配过滤条件的网络数据包被丢弃（`out` 作用于 OUTPUT、`in` 作用于 INPUT），导致主机与该目标之间通信中断
- **必现现象（与方向无关）**：实验状态为 Success/Running；被拦截方向上匹配过滤条件的流量全部超时
- **随方向变化的现象**：
  - `out`：本机主动访问该目标不通（`ping`/`curl` 超时）
  - `in`：来自该来源的请求收不到；本机主动出站仍正常
- **作用域边界**：仅影响匹配过滤条件（IP/端口/字符串）的流量，其余网络路径不受影响

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（`iptables -I … -j DROP` 丢弃匹配过滤条件的流量）；主机未装 ChaosBlade 时选用本手段。

前提条件：具备 root 权限执行 `iptables`；自恢复载体需 `systemd-run`（Tier-1，主机有 systemd）或 `setsid`+`sh`+`sleep`（Tier-2，主机无 systemd），二者皆不可用则 fail-closed（见标准件文档 §七）。

注入命令（**先武装定时恢复，再注入**——武装载体按主机是否有 systemd 分层选型〔探测门 → Tier 选择 → 武装〕，构造细节/纪律/取证约束见 `references/carrier/recovery-carrier.md`〔host 域分层自恢复载体单一权威，本 case 只引用不内嵌〕；`<recovery-seconds>` 一律取 prompt 下发的 `recovery_timer_seconds`〔= duration + grace，见 SKILL.md 双数窗口契约〕，Tier-1 的 `--on-active` 与 Tier-2 的 `sleep` 同源取此值，禁自算缓冲）：
```bash
# 0) 只读探测 systemd 是否在场（任一信号为真即判在场，degraded 仍算在场，不因 degraded 降级）：
systemctl is-system-running          # 只读；bus 可达即 systemd 在场
# 或只读判据： [ -d /run/systemd/system ]
#   → 在场选 Tier-1（systemd-run transient timer）；不在场（两信号皆否，如最小容器 / 非 systemd init 裸机）
#     选 Tier-2（setsid detached-timer）；两 Tier 前置皆不可用（systemd 不在场 且 setsid/sh/sleep 任一缺失）
#     → fail-closed：显式报告「本环境无可用自治恢复载体」及缺失项，禁止注入无安全网的自定时故障（见标准件文档 §七）。
#   下面给出 Tier-1 / Tier-2 两种武装形态，按探测结果二选一；武装成功后（Tier-1 输出含 "Running timer as unit"、
#   Tier-2 即刻返回且 ps 见 PPID=1 detached 会话）再执行注入。各命令独立执行（执行通道不支持在工具调用层 && 串联）。
#   逆命令 iptables -D 的过滤参数必须与注入 iptables -I 逐字对应（同 chain、同 -d/-s/--dport、同 -j），才是精确逆操作；
#   下面以「出站丢弃到特定 IP」为例，若注入的是端口过滤或入站规则，武装的逆命令须改成对应那条 -D（见块尾备选形态）。

# Tier-1（systemd 在场）：
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-drop \
  iptables -D OUTPUT -d <target-ip> -j DROP
# Tier-2（systemd 不在场；逆命令已在外层 sh -c 内，直接写、禁再嵌套 sh -c——payload 禁嵌套 interpreter）：
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds> && iptables -D OUTPUT -d <target-ip> -j DROP'
# 武装成功后再注入丢包规则：
iptables -I OUTPUT -d <target-ip> -j DROP

# 其它过滤形态（武装的逆命令须相应改成对应那条 -D，过滤参数逐字对应）：
# 丢弃到特定端口的包： iptables -I OUTPUT -p tcp --dport <port> -d <target-ip> -j DROP
# 入站丢弃来自特定 IP： iptables -I INPUT -s <source-ip> -j DROP
```

恢复命令（timer 到期前可提前手动恢复，同时停掉已武装的定时器——按武装时选用的 Tier 停对应定时器）：
```bash
# 提前恢复：删除本次注入的 DROP 规则（过滤参数与注入 -I 逐字对应）
#   Tier-1：systemctl stop blade-restore-drop.timer 2>/dev/null
#   Tier-2：pkill -f 'sleep <recovery-seconds>' 用括号法防自匹配（如 pkill -f 'sleep 42[0]'，见标准件文档 §五）
iptables -D OUTPUT -d <target-ip> -j DROP
```

注意事项：
- iptables 规则立即生效，已建立的 TCP 连接可能需要等超时
- 自恢复基于注入前武装的**分层自恢复载体**（Tier-1 主机有 systemd 时 systemd-run transient timer / Tier-2 主机无 systemd 时 setsid detached-timer；选型探测门、武装/重武装纪律、payload 文法〔Tier-2 禁嵌套 interpreter，逆命令直接写在外层 sh -c 内〕、fail-closed 边界、Tier-2 取证约束见 `references/carrier/recovery-carrier.md`），到期自动 `iptables -D` 删除注入的 DROP 规则，补齐了 ChaosBlade `--timeout` 的自恢复能力；提前恢复仍用上方手动命令
- 逆命令 `iptables -D` 须与注入 `iptables -I` 参数逐字对应才是精确逆操作；删除已不存在的规则会报错但无副作用（幂等容忍，见标准件文档 §六）——定时器自治 fire 与框架提前兜底双执行时，第二次 -D 报 `Bad rule (does not exist)` 属预期，不得据此判恢复失败
- Tier-2 payload 守 discard-sink 不变量、**不写任何取证文件**，故无载体侧 fire 日志属预期取舍（见标准件文档 §八）；fire 是否发生由框架侧只读观测靶标状态差承担（如 `ping <target-ip>` 由不通恢复为通），不得据此误判武装失败
- 武装与注入分两次独立执行，须确认武装成功后再执行注入（等价于 `&&` 串联的失败短路保证）
- 注意不要误删其他 iptables 规则——逆命令只删本次注入登记的那条（过滤参数逐字对应）
- Tier-1 同名 transient timer 重复武装会报 `Unit blade-restore-drop.service was already loaded`（上次武装失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-restore-drop.service; systemctl reset-failed blade-restore-drop.service`（武装命令成功执行过的 unit 无残留，可直接重武装）。Tier-2 重武装先 `pkill -f 'sleep <recovery-seconds>'`（括号法）终止旧会话再重新 setsid（见标准件文档 §五）
