**用例名称** 域名解析异常 导致 Pod_网络DNS故障

**故障现象**：
1. 目标 Pod 内特定域名解析返回伪造 IP 地址
2. 应用连接到错误的服务地址，出现 Connection refused 或连接超时
3. 仅影响被注入的域名，其他域名解析正常，网络连通性未受影响

**资源准备**：
1. 确认目标应用 A 已正常运行
2. 确认目标应用依赖外部域名解析（如访问远程 API、数据库域名等）

**演练步骤**：
1. 定位应用 A 的 Pod 作为故障注入目标
2. 使用 chaosblade 对目标 Pod 注入 DNS 故障，将特定域名解析到伪造 IP
3. 观察目标 Pod 的域名解析结果和应用行为变化

**注入验证**：
1. 通过 kubectl exec 在目标 Pod 内执行 `cat /etc/hosts`，确认包含 ChaosBlade 注入的域名劫持条目（格式：`<forged-ip> <domain> #chaosblade`）
2. 通过 kubectl exec 在目标 Pod 内执行 `ping -c 1 <domain>` 或 `wget/curl <domain>`，确认域名解析到注入的伪造 IP（ping 输出中 `PING <domain> (<forged-ip>)` 显示伪造 IP）
3. 验证其他域名解析不受影响：`ping -c 1 <其他域名>` 应解析到正常 IP（而非伪造 IP）
4. 查看目标 Pod 的应用日志，确认出现 Connection refused、Unknown host、连接超时等异常。**注意**：若目标应用不依赖被劫持域名，此步骤应标记为 skipped（无法验证应用层影响），并建议用户选择应用实际使用的域名重新注入
5. **重要**：`nslookup` 和 `dig` 直接查询 DNS 服务器，绕过 /etc/hosts 文件，因此**无法检测**此类型 DNS 劫持的故障效果。不要使用 nslookup/dig 作为验证工具

**注入恢复**：
1. 销毁 chaosblade DNS 注入实验

**恢复验证**：
1. 再次执行 `cat /etc/hosts`，确认 ChaosBlade 的域名劫持条目已被移除
2. 执行 `ping -c 1 <domain>`，确认域名解析恢复为正确 IP
3. 查看应用日志，确认连接恢复正常

**基准事实**：
- **根因**：DNS 解析被篡改，特定域名指向伪造 IP，导致应用连接到错误地址
- **必现现象**：被注入域名解析返回伪造 IP，应用出现连接异常；其他域名和网络不受影响

**与 delay/loss 的本质区别**：
- DNS 故障影响的是域名解析结果（应用层），而非网络连通性（网络层）
- delay 增加网络延迟，loss 导致丢包，两者都影响网络传输；DNS 故障则改变解析结果，传输本身可能正常

---
