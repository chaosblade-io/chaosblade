**用例名称** DNS劫持 导致 Host_网络故障

## 故障定位
持续型故障——目标主机对特定域名的解析在故障窗口内被劫持到错误 IP，使依赖该域名的应用访问失败或被导向错误服务；窗口结束（`--timeout` 到期 / 武装的自恢复载体到期）即自动恢复。**手段1（ChaosBlade）**与**手段2（原生命令）**是并列的注入手段，都让目标域名的解析结果偏离真实 IP，按环境能力选用：主机装有 ChaosBlade（`blade version` 可用）→ 手段1（`blade create network dns`，注入层为 `/etc/hosts`、按域名精确劫持，`--replace` 控制是否覆盖已有本地解析记录，实验 UID 统一生命周期管理）；否则手段2（借 `iptables` nat 表把本机发出的 DNS 查询 DNAT 重定向到指定解析器，覆盖面比改 `/etc/hosts` 更广——绕过 hosts 的应用同样受影响）。`duration_seconds` 是必填的故障窗口契约，未给定时先向用户确认。

## 故障现象
1. 域名解析返回错误 IP
2. 应用访问特定域名失败或被导向错误服务
3. 网络连接超时或返回非预期响应

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标域名及劫持目标 IP

## 演练步骤
1. 记录当前 DNS 解析基线——与注入验证判据**同口径**走 hosts-honoring 解析路径：`getent hosts <domain>`（或 `ping -c1 <domain>` 读解析到的真实 IP）。不用 `nslookup`/`dig` 作基线：它们绕过 `/etc/hosts` 只查 DNS 服务器，与本 case 手段1 注入层（改 `/etc/hosts`）及 `ping` 判据错配，且 dnsutils 非最小靶机默认装（缺失即 exit 127）；仅手段2（iptables DNAT 53 层重定向 DNS 查询本身）下 nslookup 才与观测面一致
2. 注入 DNS 劫持 —— **手段1（ChaosBlade）**（主机无 blade 时改用文末**手段2（原生命令）**）

```bash
blade create network dns --domain <target-domain> --ip <redirect-ip> --timeout <duration>
```

参数说明：
- `--domain`：目标域名（必填）
- `--ip`：劫持到的 IP 地址（必填）
- `--replace`：可选，域名已有解析时是否替换
- `--timeout`：超时自动恢复（秒）

3. 观察域名解析结果及应用连通性变化

## 注入验证
1. `ping <domain>` 确认解析到错误 IP（ping 走完整 resolver、/etc/hosts 优先）。⚠️ 不要用 nslookup 作判据——ChaosBlade network dns 的注入层是 /etc/hosts（与 k8s pod-network dns 同源，`--replace` 即「已有本地解析记录是否覆盖」语义），nslookup 只查 DNS 服务器、不读 hosts，验不出劫持；仅手段2（iptables DNAT 53 层，DNS 查询本身被重定向到伪造解析器）下 nslookup 才有效
2. 观察应用访问该域名时是否超时或返回错误

## 注入恢复
**手段1（ChaosBlade）**——销毁实验即恢复（`--timeout` 到期亦自动恢复）：
```bash
blade destroy <experiment-uid>
```

**手段2（原生命令）**——见文末「手段2（原生命令）」块的恢复命令（先停武装的定时器 `systemctl stop blade-restore-dnsnat.timer`，再按注入的 `-A` 参数逐字精确 `-D` 删除 DNAT 规则，本机 DNS 查询随即恢复直连原解析器）

## 恢复验证
1. `ping <domain>` 确认解析回到注入前基线 IP（nslookup 不读 /etc/hosts，注入期间与恢复后返回相同结果，不能作恢复判据）
2. 确认应用访问该域名恢复正常

## 基准事实
- **根因**：DNS 解析被劫持，域名指向错误 IP，导致应用无法正常访问目标服务
- **必现现象**：域名解析结果为非预期 IP；依赖该域名的服务调用失败或超时

---

**手段2（原生命令）**

> 与手段1（ChaosBlade）并列的注入手段，借 `iptables` nat 表把本机发出的 DNS 查询 DNAT 重定向到指定解析器（覆盖面比改 `/etc/hosts` 更广——绕过 hosts 的应用同样受影响）；主机未装 ChaosBlade 时选用本手段。

前提条件：具备 root 权限，且主机 iptables 支持 nat 表

注入命令（**先武装定时恢复，再注入**；到期自动删除 DNAT 规则，补齐自恢复能力）：
```bash
# 1) 先武装定时还原（定时器由宿主机 systemd(PID 1) 管理），再注入规则。
#    三条命令分次独立执行（执行通道不支持 && 串联）；武装成功（输出含 Running timer as unit）后再执行注入
# <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-dnsnat sh -c \
  'iptables -t nat -D OUTPUT -p udp --dport 53 -j DNAT --to-destination <redirect-dns-ip>:53; \
   iptables -t nat -D OUTPUT -p tcp --dport 53 -j DNAT --to-destination <redirect-dns-ip>:53'
# 2) 在网络层把本机发出的 DNS 查询重定向到伪造的解析器。
# 比改 /etc/hosts 覆盖面更广：绕过 hosts 的应用（自带 DNS 缓存/直连解析器的）同样受影响。
iptables -t nat -A OUTPUT -p udp --dport 53 -j DNAT --to-destination <redirect-dns-ip>:53
iptables -t nat -A OUTPUT -p tcp --dport 53 -j DNAT --to-destination <redirect-dns-ip>:53
```

恢复命令（提前恢复；先停武装的定时器再手动删除规则）：
```bash
systemctl stop blade-restore-dnsnat.timer 2>/dev/null
# -D 与注入的 -A 参数逐字对应，是精确逆操作
iptables -t nat -D OUTPUT -p udp --dport 53 -j DNAT --to-destination <redirect-dns-ip>:53
iptables -t nat -D OUTPUT -p tcp --dport 53 -j DNAT --to-destination <redirect-dns-ip>:53
```

> 若确实要走 /etc/hosts 路线：`cp /etc/hosts /etc/hosts.bak` 备份可以执行，但追加记录需要
> shell 重定向（`echo ... >> /etc/hosts`），Agent 不执行，需人工完成；恢复用
> `cp /etc/hosts.bak /etc/hosts`。

注意事项：
- /etc/hosts 修改仅影响本机解析，不影响其他机器
- 某些应用有独立 DNS 缓存，修改 hosts 后可能需要重启应用才生效
- 自恢复基于注入前武装的 systemd-run transient timer（到期自动删除 DNAT 规则）；提前恢复仍用上方手动命令
- 同名 transient timer 重复武装会报 `Unit blade-restore-dnsnat.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-restore-dnsnat.service; systemctl reset-failed blade-restore-dnsnat.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
