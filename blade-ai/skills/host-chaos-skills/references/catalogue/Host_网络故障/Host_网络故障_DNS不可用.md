**用例名称** DNS不可用 导致 Host_网络故障

## 故障定位
持续型故障——目标主机的 DNS 解析在故障窗口内完全不可达（对非白名单域名查询无响应），使一切依赖域名解析的应用失败；窗口结束（`--timeout` 到期实验销毁 / 武装的自恢复载体到期删除阻断规则）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，底层效果等价（都令本机 DNS 查询无响应），按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）且 `blade create network dns_down --help` 存在 dns_down action → 手段1（`--allow_domain` 域名白名单 + 实验 UID 统一生命周期管理）；否则手段2（`iptables` filter 表 OUTPUT 链 DROP 掉本机发出的 53 端口 DNS 查询）。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

**与 `DNS劫持`(dns) 的正交分界**：本用例是**解析完全失败**（DNS 查询无响应 / 超时 / `SERVFAIL`，域名解析不到任何 IP），承重签名是 `getent hosts <domain>` 无输出（rc=2，解析不到任何 IP）——getent 走应用同款 glibc NSS 路径（nsswitch `files→dns`）、靶机普适在场、与应用同观测面；DNS 层可选诊断 `nslookup <domain>` 连接超时或返回 `SERVFAIL`（dnsutils，最小靶机常缺失，故非承重）。`DNS劫持` 是**解析成功但结果错误**（域名被导向伪造 / 错误 IP），签名是 `ping <domain>` 解析到一个错误 IP（解析本身成功）。两者判据正交：dns_down=解析不到、hijack=解析到错的，不得互相套用。

**爆炸半径（主机级全局 DNS 阻断）**：手段1 `dns_down` **无 interface / destination scoping**（仅 `--allow_domain`+`--timeout`），是**主机级全局 DNS 阻断除白名单**；手段2 `iptables OUTPUT --dport 53 DROP` 同样阻断本机**所有** DNS 查询。二者都会波及一切依赖 DNS 的组件——通过域名建立的 SSH 连接、监控 agent 上报、包管理器、服务发现等。故注入前必须评估波及面：手段1 用 `--allow_domain <critical-domains>` 白名单保护关键域名（手段2 iptables 工作在 IP/端口层，无法按域名白名单，只能整体阻断或放行特定解析器 IP）；无法收窄时须显式接受「主机级全局 DNS 阻断」波及面并武装定时自删作恢复安全网。⚠️ **`--allow_domain` 有运行时依赖**：blade 该选项内部以 `/bin/sh -c '... nslookup <domain> ... | sudo tee -a /etc/hosts'` 预解析白名单域名写入 hosts，**依赖靶机装有 `nslookup`(dnsutils) 且有 `sudo`**；二者任一缺失时整个 `blade create` 在施加任何 iptables 规则**之前**即失败（exit 127 / code 63063，零故障残留）。故 `--allow_domain` 非无条件可用——资源准备须先核其依赖（见资源准备第 1 条），缺失时回退手段2、或不带 `--allow_domain` 显式接受全局阻断波及面并武装定时自删。

## 故障现象
1. 主机对非白名单域名的 DNS 解析全部失败（超时 / `SERVFAIL` / 无答案）
2. 依赖域名解析的应用连接失败：新建连接报 `Name or service not known` / `Temporary failure in name resolution`
3. 已建立的 TCP 连接（用 IP 直连或解析已缓存的）短期不受影响，但重连 / 新连接失败
4. 模拟上游 DNS 服务器不可达 / 递归解析器故障 / DNS 出口被阻断场景

## 资源准备
1. 确认注入通道可用：手段1 主机装有 ChaosBlade（`blade version`）且 `blade create network dns_down --help` 存在 dns_down action；手段2 具备 root 权限执行 `iptables`（`iptables -V` 可用）。⚠️ **`--allow_domain` 运行时依赖探测（dns_down action 存在 ≠ 该选项可用）**：若要按域名白名单收窄爆炸半径，须先分别只读探测 `command -v nslookup`、`command -v sudo`——blade `--allow_domain` 内部以 `nslookup ... | sudo tee -a /etc/hosts` 预解析白名单域名写入 hosts，二者任一缺失则该选项不可用、整个 create 会 exit 127 / code 63063（施加 iptables 前失败、零故障残留）；缺失时回退手段2（iptables，无域名白名单能力）或不带 `--allow_domain` 显式接受全局阻断波及面 + 武装定时自删作恢复安全网
2. 确认目标主机 DNS 拓扑：`cat /etc/resolv.conf` 看 `nameserver`（直连上游解析器 or 本地 stub 如 `127.0.0.53`）；`grep '^hosts' /etc/nsswitch.conf` 看解析顺序（`files dns` 表示先查 `/etc/hosts` 再查 DNS）
3. **选定验证用域名**：须是**不在 `/etc/hosts` 中、必须经 DNS 解析**的域名（否则 `files` 优先命中 hosts，DNS 阻断验不出）；确认基线可正常解析
4. 确认承重探针 `getent` 可用（`command -v getent`——glibc 系统普适在场，走应用同款 NSS 解析路径）；记录 DNS 解析基线（见演练步骤 1）

## 演练步骤
1. 记录当前 DNS 解析基线（选定域名应正常解析到真实 IP）——承重探针走应用同款 glibc NSS 路径（与应用同观测面、靶机普适在场）：
   ```bash
   getent hosts <domain>
   ```
   记下正常返回的 IP（rc=0）；getent 亦缺的极端靶机（非 glibc）用 `ping -c1 <domain>` 读解析到的 IP 作等价基线。可选 DNS 层诊断 `nslookup <domain>`（仅 dnsutils 在场时）另记解析耗时
2. 注入 DNS 不可用 —— **手段1（ChaosBlade）**（主机无 blade 或 blade 无 dns_down action 时改用文末**手段2（原生命令）**）
   ```bash
   blade create network dns_down --allow_domain <critical-domains> --timeout <duration>
   ```
   参数说明：
   - `--allow_domain`：域名白名单（逗号分隔，如 `test1.com,test2.com`）——**爆炸半径纪律**，白名单内域名解析不受影响，用于保护关键域名（如监控上报、控制通道域名）；不给则阻断全部 DNS。⚠️ **运行时依赖 `nslookup`(dnsutils)+`sudo`**（blade 内部以 `nslookup ... | sudo tee -a /etc/hosts` 预解析白名单域名）：二者任一缺失则整个 create exit 127 / code 63063（施加 iptables 前失败、零残留），须先按资源准备第 1 条探测；缺失时去掉 `--allow_domain` 显式接受全局阻断 + 武装定时自删，或改用手段2
   - `--timeout`：到期自动恢复（秒）
   - **无 `--interface` / `--destination-ip` scoping**：dns_down 是主机级全局 DNS 阻断，无法按网卡 / 目的 IP 收窄，只能用 `--allow_domain` 域名白名单收窄
   记录返回的 experiment_uid，用于后续恢复
3. 观察 DNS 解析及应用连通性变化

## 注入验证
1. （诊断，仅当下述效果证据未出现时执行）确认实验已生效：
   ```bash
   blade status --uid <experiment-uid>
   ```
   状态为 Success/Running 即表示 DNS 阻断规则已下到主机——这是机制回执，为机制代言不为结果代言，用于定位失败层（规则未下发 vs 下发未生效），不构成效果证据
2. **（主证据，必做）** 行为级解析失败效果——对**非白名单、需经 DNS 解析的域名**测解析，与演练步骤 1 基线对比，承重探针走应用同款 glibc NSS 路径：
   ```bash
   getent hosts <domain>
   ```
   判据：`getent hosts <domain>` **无输出、退出码 rc=2**（解析不到任何 IP）——getent 遵 nsswitch `files→dns`、与应用同观测面，域名不在 `/etc/hosts` 时走 DNS 查询、被阻断即无答案；区别于 `DNS劫持` 的「解析到一个错误 IP」。白名单内域名（`--allow_domain`，blade 已预解析写入 `/etc/hosts`）经 `files` 命中应仍能正常解析（rc=0，这是预期，不是失败）。getent 亦缺的极端靶机（非 glibc）用 `ping -c1 <domain>` 读「`Name or service not known` / 无 IP」作等价判据；可选 DNS 层白盒诊断 `nslookup <domain>`（仅 dnsutils 在场时）应**超时 / 返回 `SERVFAIL` / 无答案**（`** server can't find <domain>` 或 `connection timed out`）
3. **（机制归因白盒）** 确认 53 端口阻断规则已挂载：
   ```bash
   iptables -L OUTPUT -n --line-numbers
   ```
   输出应包含针对 `dpt:53` 的 `DROP` 规则（手段2 直接可见；手段1 下 blade 亦以 netfilter 规则实现阻断，可见其管理的 53 端口阻断条目）
4. 检查应用日志是否出现 `Temporary failure in name resolution` / `Name or service not known` / 域名解析超时相关错误

> 判据可达性（工具面，M3）：本 case 承重解析判据走靶机普适在场、与应用同观测面的探针——`getent hosts`（glibc NSS，遵 nsswitch `files→dns`）或 `ping`（亦 honors resolver）。不可达陷阱在 dnsutils 的 `nslookup`/`dig`：① 最小靶机常未装 dnsutils，`nslookup` 缺失即 exit 127、判据产不出任何证据；② 即便装了，`nslookup`/`dig` 只查 DNS 服务器、绕过 `/etc/hosts`，对 `--allow_domain` 已预解析写入 hosts 的白名单域名读不到其「仍解析」的预期，与应用实际体验错配。故 `nslookup`/`dig` 在 dnsutils 在场时仅作 DNS 层可选白盒诊断，承重判据用 `getent hosts`/`ping`（详见 `references/criterion-reachability.md` §五「工具面」类）。

> ⚠️ 验证纪律：
> - 验证域名必须**不在 `/etc/hosts` 中且需经 DNS 解析**。若用 hosts 已命中的域名，`files` 优先返回本地记录，DNS 阻断验不出——**不是注入失败，是选错验证域名**。
> - `--allow_domain` 白名单内的域名解析成功是**预期**，不是失败；须用白名单**外**的域名验证阻断效果。
> - 解析失败判据是「解析不到 / 超时 / SERVFAIL」，**不是「解析到错误 IP」**（后者是 `DNS劫持` 的签名）。
> - 同一事实（如实验是否生效）确认一次即可，不要重复查询。

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```
**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的定时器——Tier-1 `systemctl stop` / Tier-2 `pkill` 括号法——再按注入的 `-I` 参数逐字精确 `-D` 删除 OUTPUT 链 53 端口 DROP 规则，本机 DNS 查询随即恢复）

## 恢复验证
1. 确认 53 端口阻断规则已清除：
   ```bash
   iptables -L OUTPUT -n --line-numbers
   ```
   输出应不再包含针对 `dpt:53` 的 `DROP` 规则
2. 对选定域名测解析（与演练步骤 1 **同一探针、同口径**）：
   ```bash
   getent hosts <domain>
   ```
   确认解析恢复回**演练步骤第 1 步记录的基线**（`getent hosts <domain>` 重新返回真实 IP、rc=0，不再无输出 / rc=2）
3. 确认应用日志不再出现域名解析失败错误

## 基准事实
- **根因**：通过阻断主机发出的 53 端口 DNS 查询（手段1 blade dns_down / 手段2 iptables OUTPUT DROP），令域名解析完全不可达，模拟上游 DNS 不可达 / 递归解析器故障场景
- **必现现象**：非白名单域名 `getent hosts <domain>` 无输出 / rc=2（解析不到任何 IP；dnsutils 在场时 DNS 层 `nslookup` 超时 / `SERVFAIL` / 无答案）；应用新建连接报 `Temporary failure in name resolution`；`iptables -L OUTPUT -n` 显示 `dpt:53` DROP 规则
- **与 DNS劫持的分界**：本用例是解析**完全失败**（dns_down，解析不到 IP）；`DNS劫持`(dns) 是解析**成功但结果错误**（导向错误 IP）。判据正交，`## 注意事项` 互标
- **作用域边界**：dns_down 是**主机级全局 DNS 阻断**，无 interface / destination scoping，仅 `--allow_domain` 域名白名单可收窄；手段2 iptables 工作在 IP/端口层，无法按域名白名单
- **爆炸半径**：全局 DNS 阻断会波及一切依赖 DNS 的组件（域名 SSH、监控上报、包管理器、服务发现）；`--allow_domain` 可保护关键域名但**运行时依赖 `nslookup`(dnsutils)+`sudo`**（缺失即整个 create exit 127），故非无条件可用——依赖缺失时回退手段2、或不带白名单显式接受全局阻断并以武装的定时自删作恢复安全网
- **验证域名选择**：须用不在 `/etc/hosts`、需经 DNS 解析的域名验证；hosts 已命中的域名走 `files` 优先，DNS 阻断验不出
- **承重探针普适性**：解析判据走应用同款 glibc NSS 路径 `getent hosts`（或 `ping`），靶机普适在场、与应用同观测面；`nslookup`/`dig`（dnsutils）非最小靶机默认装且绕过 `/etc/hosts`，仅作 DNS 层可选诊断（见 `references/criterion-reachability.md` §五「工具面」类）

## 注意事项
- **与 `DNS劫持`(dns) 互标分界**：本用例解析**完全失败**（承重签名 `getent hosts <domain>` 无输出 / rc=2；dnsutils 在场时 DNS 层 `nslookup` 超时 / SERVFAIL / 无答案）；`DNS劫持` 解析**成功但到错误 IP**（签名 `ping <domain>` 到伪造 IP）。用户提「DNS 挂了 / 解析不了 / 域名解析失败 / DNS 不可达」→ 本用例；提「解析到错误 IP / 域名被劫持 / 导向假地址」→ `DNS劫持`
- **主机级全局阻断**：dns_down 无网卡 / 目的 IP scoping，会阻断本机所有 DNS（除 `--allow_domain` 白名单）；注入前必须评估 SSH 控制通道 / 监控 / 服务发现是否依赖 DNS，必要时白名单保护或改用 IP 直连的控制通道
- 手段2 iptables 无法按域名白名单（IP/端口层机制），只能整体阻断或放行特定解析器 IP；需要域名级白名单时用手段1 `--allow_domain`
- 已建立的 TCP 连接（IP 直连或解析已缓存）短期不受影响，故障主要打击新建连接 / 重连 / 缓存过期后的解析

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，效果等价（都令本机发出的 DNS 查询无响应）；主机未装 ChaosBlade 或 blade 构建不提供 dns_down action 时选用本手段。借 `iptables` filter 表 OUTPUT 链 DROP 掉本机发出的 53 端口（UDP + TCP）DNS 查询。

前提条件：具备 root 权限执行 `iptables`（`iptables -V` 可用，支持 filter 表 OUTPUT 链）；自恢复载体需 `systemd-run`（Tier-1，主机有 systemd）或 `setsid`+`sh`+`sleep`（Tier-2，主机无 systemd），二者皆不可用则 fail-closed（见标准件文档 §七）。

注入命令（**先武装定时恢复，再注入**——武装载体按主机是否有 systemd 分层选型〔探测门 → Tier 选择 → 武装〕，构造细节 / 纪律 / 取证约束见 `references/carrier/recovery-carrier.md`〔host 域分层自恢复载体单一权威，本 case 只引用不内嵌〕；`<recovery-seconds>` 一律取 prompt 下发的 `recovery_timer_seconds`〔= duration + grace，见 SKILL.md 双数窗口契约〕，Tier-1 的 `--on-active` 与 Tier-2 的 `sleep` 同源取此值，禁自算缓冲）：
```bash
# 0) 只读探测 systemd 是否在场（任一信号为真即判在场，degraded 仍算在场，不因 degraded 降级）：
systemctl is-system-running          # 只读；bus 可达即 systemd 在场
# 或只读判据： [ -d /run/systemd/system ]
#   → 在场选 Tier-1（systemd-run transient timer）；不在场（两信号皆否，如最小容器 / 非 systemd init 裸机）
#     选 Tier-2（setsid detached-timer）；两 Tier 前置皆不可用（systemd 不在场 且 setsid/sh/sleep 任一缺失）
#     → fail-closed：显式报告「本环境无可用自治恢复载体」及缺失项，禁止注入无安全网的自定时故障（见标准件文档 §七）。
#   下面给出 Tier-1 / Tier-2 两种武装形态，按探测结果二选一；武装成功后（Tier-1 输出含 "Running timer as unit"、
#   Tier-2 即刻返回且 ps 见 PPID=1 detached 会话）再执行注入。各命令独立执行（执行通道不支持在工具调用层 && 串联）。
#   逆命令 `iptables -D OUTPUT ... DROP` 与注入的 `-I` 参数逐字对应，是精确逆操作。

# Tier-1（systemd 在场；逆命令为多条 iptables -D，用 sh -c 承载）：
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-dnsdrop sh -c \
  'iptables -D OUTPUT -p udp --dport 53 -j DROP; iptables -D OUTPUT -p tcp --dport 53 -j DROP'
# Tier-2（systemd 不在场；逆命令直接写在外层 sh -c 内，多条 -D 用 `;` 顺序平铺，禁 `{ }` 复合语法、禁再嵌套 sh -c）：
setsid --fork sh -c 'exec >/dev/null 2>&1; sleep <recovery-seconds>; iptables -D OUTPUT -p udp --dport 53 -j DROP; iptables -D OUTPUT -p tcp --dport 53 -j DROP'

# 武装成功后再注入 DNS 阻断规则（UDP + TCP 53 端口，-I 插到 OUTPUT 链首确保优先命中）：
iptables -I OUTPUT -p udp --dport 53 -j DROP
iptables -I OUTPUT -p tcp --dport 53 -j DROP
```

恢复命令（timer 到期前可提前手动恢复，同时停掉已武装的定时器——按武装时选用的 Tier 停对应定时器）：
```bash
# 提前恢复：删除本次注入的 DROP 规则（-D 与注入的 -I 参数逐字对应，精确逆操作）
#   Tier-1：systemctl stop blade-restore-dnsdrop.timer 2>/dev/null
#   Tier-2：pkill -f 'sleep <recovery-seconds>' 用括号法防自匹配（如 pkill -f 'sleep 42[0]'，见标准件文档 §五）
iptables -D OUTPUT -p udp --dport 53 -j DROP
iptables -D OUTPUT -p tcp --dport 53 -j DROP
```

注意事项：
- iptables DROP 规则立即生效，本机新发出的 DNS 查询即被丢弃（UDP 无响应超时、TCP 连接被拒）
- **爆炸半径**：`iptables -I OUTPUT --dport 53 -j DROP` 阻断本机**所有** DNS 查询，无法按域名白名单（IP/端口层机制）；会波及域名 SSH、监控上报、包管理器、服务发现，注入前须评估并武装定时自删安全网
- 自恢复基于注入前武装的**分层自恢复载体**（Tier-1 主机有 systemd 时 systemd-run transient timer / Tier-2 主机无 systemd 时 setsid detached-timer；选型探测门、武装 / 重武装纪律、payload 文法〔Tier-2 禁嵌套 interpreter，多条逆命令用 `;` 顺序平铺直接写在外层 sh -c 内，禁 `{ }` 复合语法（白名单文法只支持 `;`/`&&`/`||` 平铺序列）〕、fail-closed 边界、Tier-2 取证约束见 `references/carrier/recovery-carrier.md`），到期自动删除 DROP 规则，补齐了 ChaosBlade `--timeout` 的自恢复能力；提前恢复仍用上方手动命令
- `iptables -D` 对已删除的规则报 `iptables: Bad rule (does a matching rule exist in that chain?).`，无害但需预期（幂等容忍，见标准件文档 §六）——定时器自治 fire 与框架提前兜底双执行时，第二次 `-D` 报此错属预期，不得据此判恢复失败
- Tier-2 payload 守 discard-sink 不变量、**不写任何取证文件**，故无载体侧 fire 日志属预期取舍（见标准件文档 §八）；fire 是否发生由框架侧只读观测靶标状态差承担（如 `iptables -L OUTPUT -n` 由 DROP 规则消失），不得据此误判武装失败
- 武装与注入分两次独立执行，须确认武装成功后再执行注入（等价于 `&&` 串联的失败短路保证）
- 载体 unit 名取非故障家族词根（`blade-restore-dnsdrop`）以符合 host 段分层放行；Tier-1 同名 transient timer 重复武装会报 `Unit blade-restore-dnsdrop.service was already loaded`（上次武装失败时 unit 以 failed 状态残留所致），重武装前先清理残留：`systemctl stop blade-restore-dnsdrop.service; systemctl reset-failed blade-restore-dnsdrop.service`；Tier-2 重武装先 `pkill -f 'sleep <recovery-seconds>'`（括号法）终止旧会话再重新 setsid（见标准件文档 §五）
- 效果与手段1 等价——都令本机 DNS 查询无响应；差异在收窄能力：手段1 `--allow_domain` 支持域名白名单，手段2 iptables 只能整体阻断或放行特定解析器 IP
