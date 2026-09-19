**用例名称** DNS劫持 导致 Host_网络故障

**故障现象**：
1. 域名解析返回错误 IP
2. 应用访问特定域名失败或被导向错误服务
3. 网络连接超时或返回非预期响应

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标域名及劫持目标 IP

**演练步骤**：
1. 记录当前 DNS 解析基线：`nslookup <domain>` 或 `dig <domain>`
2. 使用 ChaosBlade 注入 DNS 劫持

```bash
blade create network dns --domain <target-domain> --ip <redirect-ip> --timeout <duration>
```

参数说明：
- `--domain`：目标域名（必填）
- `--ip`：劫持到的 IP 地址（必填）
- `--replace`：可选，域名已有解析时是否替换
- `--timeout`：超时自动恢复（秒）

3. 观察域名解析结果及应用连通性变化

**注入验证**：
1. `ping <domain>` 确认解析到错误 IP（ping 走完整 resolver、/etc/hosts 优先）。⚠️ 不要用 nslookup 作判据——ChaosBlade network dns 的注入层是 /etc/hosts（与 k8s pod-network dns 同源，`--replace` 即「已有本地解析记录是否覆盖」语义），nslookup 只查 DNS 服务器、不读 hosts，验不出劫持；仅降级方案（iptables DNAT 53 层，DNS 查询本身被重定向到伪造解析器）下 nslookup 才有效
2. 观察应用访问该域名时是否超时或返回错误

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `ping <domain>` 确认解析回到注入前基线 IP（nslookup 不读 /etc/hosts，注入期间与恢复后返回相同结果，不能作恢复判据）
2. 确认应用访问该域名恢复正常

**基准事实**：
- **根因**：DNS 解析被劫持，域名指向错误 IP，导致应用无法正常访问目标服务
- **必现现象**：域名解析结果为非预期 IP；依赖该域名的服务调用失败或超时

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

前提条件：具备 root 权限，且主机 iptables 支持 nat 表

注入命令（**先武装定时恢复，再注入**；到期自动删除 DNAT 规则，补齐自恢复能力）：
```bash
# 1) 先武装定时还原（定时器由宿主机 systemd(PID 1) 管理），再注入规则。
#    三条命令分次独立执行（执行通道不支持 && 串联）；武装成功（输出含 Running timer as unit）后再执行注入
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
