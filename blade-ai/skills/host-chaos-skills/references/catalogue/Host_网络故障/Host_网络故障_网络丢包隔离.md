**用例名称** 网络丢包隔离 导致 Host_网络故障

**故障现象**：
1. 主机与匹配过滤条件的 IP/端口之间的数据包被丢弃，受影响方向取决于 `--network-traffic`：
   - `out`（出站）：本机主动发起的连接超时、`ping` 该目标不通
   - `in`（入站）：来自该来源的请求收不到，但**本机主动 ping/curl 出去仍可能正常**（回包方向未被拦截时）
2. TCP 连接超时
3. 依赖该网络路径的服务调用失败

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标 IP/端口及流量方向

**演练步骤**：
1. 记录当前网络连通性基线：`ping <target-ip>` 或 `curl <target-url>`
2. 使用 ChaosBlade 注入网络丢包/隔离

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

**注入验证**：
1. **（主证据，必做）** 确认实验已生效：
   ```bash
   blade status --uid <experiment-uid>
   ```
   状态为 Success/Running 即表示丢包规则已下到主机网络栈。
2. **（只做与本次 `--network-traffic` 匹配的分支）** 另一方向的现象在本次注入下**不可能出现**，直接标记为 `expected` 并跳过：
   - **`out`（出站）**：从本机向目标验证不通
     ```bash
     ping -c 4 <target-ip>
     curl --connect-timeout 5 <target-url>
     ```
     预期丢包率 100% / 连接超时。
   - **`in`（入站）**：**本机 `ping`/`curl` 出去仍通是预期，不是失败**。改为从对端（或另一台主机）向本机发起访问，确认收不到响应；或在本机用 `tcpdump -i any -n host <peer-ip>` 观察到包到达但无响应。
3. 观察应用错误日志中是否出现连接超时/拒绝

> ⚠️ 验证纪律：
> - **严禁为不适用的方向反复更换查询方式找证据**。注入 `in` 时本机主动出站必然可通，查到"通"是**必然**而非失败。
> - 验证必须落在**匹配过滤条件的目标上**。若注入指定了 `--destination-ip`/`--destination-port`/`--source-ip`/`--source-port`/`--string-pattern`，只有匹配的流量被丢弃；用其它地址或端口测试必然连通，**不要换目标反复重试**。
> - 同一事实（如实验是否生效）确认一次即可，不要重复查询。

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `ping <target-ip>` 确认网络恢复
2. 确认应用服务调用恢复正常

**基准事实**：
- **根因**：匹配过滤条件的网络数据包被丢弃（`out` 作用于 OUTPUT、`in` 作用于 INPUT），导致主机与该目标之间通信中断
- **必现现象（与方向无关）**：实验状态为 Success/Running；被拦截方向上匹配过滤条件的流量全部超时
- **随方向变化的现象**：
  - `out`：本机主动访问该目标不通（`ping`/`curl` 超时）
  - `in`：来自该来源的请求收不到；本机主动出站仍正常
- **作用域边界**：仅影响匹配过滤条件（IP/端口/字符串）的流量，其余网络路径不受影响

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

前提条件：具备 root 权限执行 iptables

注入命令：
```bash
# 出站方向丢弃到特定 IP 的所有包
iptables -I OUTPUT -d <target-ip> -j DROP

# 或丢弃到特定端口的包
iptables -I OUTPUT -p tcp --dport <port> -d <target-ip> -j DROP

# 入站方向丢弃来自特定 IP 的包
iptables -I INPUT -s <source-ip> -j DROP
```

恢复命令：
```bash
# 删除对应规则
iptables -D OUTPUT -d <target-ip> -j DROP
iptables -D OUTPUT -p tcp --dport <port> -d <target-ip> -j DROP
iptables -D INPUT -s <source-ip> -j DROP
```

注意事项：
- iptables 规则立即生效，已建立的 TCP 连接可能需要等超时
- 无自动超时恢复，必须手动删除规则
- 注意不要误删其他 iptables 规则
