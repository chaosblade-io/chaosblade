---
name: python-app-chaos-skills
description: |
  Python 应用层故障演练专家。当用户需要对 Python 应用做「方法级 / 依赖调用级」故障注入时使用此 skill：让应用内某个中间件调用变慢、抛异常或返回被篡改的值，用于验证应用自身的超时、重试、降级、熔断逻辑是否生效。

  支持的依赖:Redis、MySQL / SQLAlchemy、HTTP 客户端 requests / httpx、gRPC、Kafka;支持的故障动作:delay(延迟)、throwCustomException(抛异常)、returnValue(返回值篡改)。当前 catalogue 内置 7 个完整演练用例:Redis 延迟 / 异常 / 返回值篡改、MySQL 延迟 / 异常、HTTP 延迟 / 异常、gRPC 延迟、Kafka 异常;未覆盖的组合(如 httpx、SQLAlchemy)命令形态与之一致(只换 target / action / matcher),可参照同 action 的最接近用例执行。

  与资源级故障的区别：本 skill 的故障发生在应用进程内部（运行时方法拦截），不改变 CPU/内存/磁盘/网络等系统状态，也不改变任何 Kubernetes 对象。因此系统指标与集群状态在注入期间「看起来正常」是预期行为，验证必须在应用层进行。

  前置条件：目标应用必须已带 ChaosBlade Python Agent 运行（`blade prepare python` 生成 hook 文件，应用再以 `PYTHONPATH=<hook 目录>` 重启）。该前置条件无法在演练过程中补齐（需要重启应用）。

skill_type: fault-injection
---

# Python 应用层故障演练

```
意图识别 ──→ 用例选择 ──→ 意图提交
(哪个依赖/哪种动作)(决策树匹配)  (提交意图，执行交给引擎)
```

## 安全红线

以下规则优先级高于一切操作指令：

1. **不得**用本 skill 处理资源级故障（CPU/内存/磁盘/网络/进程）——那属于 k8s / host skill。本 skill 只处理「应用内某个依赖调用行为异常」。
2. **不得**在未确认前置条件的情况下承诺注入成功。Agent 未在应用进程内运行时，注入命令会失败，且无法通过重试解决（需要重启应用）。
3. **不得**省略 matcher 而对全部调用注入，除非用户明确要求全量。省略 matcher 会影响该客户端的**每一次**调用，爆炸半径远大于预期。
4. **不得**用 `blade revoke` 作为故障恢复手段。恢复故障用 `blade destroy <实验uid>`；`revoke` 会**删除 prepare 生成的 hook 文件**——它不会停止任何故障，却让该目录下的应用在下次重启后失去 Agent，且影响同主机上其他并发演练。

## 前置条件（必须先确认）

应用层故障依赖进程内 Agent，链路是：

```
blade create python ... ──HTTP──> Agent(应用进程内) ──MonkeyPatch──> 被拦截的库方法
```

Agent 进入应用进程分两步,**顺序不能颠倒**(以下均已对 chaosblade 1.9.0-alpha 实测):

1. **生成 hook 文件**:`blade prepare python --port <port> --target-script <应用入口脚本> [--python-path <解释器>]`
   - `--target-script` 是**必填**的,缺失时 CLI 直接报 `required flag(s) "target-script" not set`。
   - 它把 `sitecustomize.py` 写到 **`--target-script` 所在目录**,内容是"把 blade 自带的 `<blade目录>/lib/python` 加入 sys.path 并启动 Agent"。因此**不需要额外 `pip install`**。
   - `--port` 必须**空闲**;端口已被监听时 prepare 会拒绝(`the port has been used by other program`)。
2. **重启应用加载 hook**:应用以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` 启动,Agent 才真正在应用进程内监听。
   - 仅靠"hook 文件在应用当前目录"**不够**,实测必须在 `PYTHONPATH` 上。
   - 该步骤**需要重启应用**,演练过程中无法补做。
   - 替代路径:应用代码内显式 `ChaosBladeAgent(port=...).start()`(同样需要重启)。

**关键认知**:`blade prepare python` 返回 `success` **不代表 Agent 已在运行**;`blade status --type prepare` 显示 `Running` 也只是**记录状态**,不是存活状态。真实存活只能由注入结果反推(见下表)。

**按 CLI 报错区分两种失败**(补救手段完全不同):

| CLI 报错 | 含义 | 补救 |
|---|---|---|
| `no running python preparation record found` | 没有 prepare 记录 | 执行一次 prepare,**可在演练中补做**,然后重试注入 |
| `connect: connection refused` / `python agent is not running` | 有记录但 Agent 没在进程内 | **需重启应用**,演练中无法补做 —— 按前置条件不满足上报,不要重试 |

3. **命令执行位置**:blade **只能连它自己所在机器的 agent**(CLI 没有指定 agent 主机的参数,`prepare python` 的 `--python-path`/`--target-script` 描述的都是本机路径),因此注入命令必须**落在目标应用所在那台机器**。
   - 本类故障的 profile 是 `host`,框架会在 scope 与通道 profile 不一致时**直接收走全部工具**(fail-closed)。所以对话/ReAct 路径下只有**主机寻址的通道**(ssh / kubewiz_host)能进行本类演练;`kubeconfig` 与 `kubewiz_k8s`(profile 均为 k8s)会在你看到注入工具之前就被拒。
   - 因此你能调到注入工具,就说明通道已经是主机寻址的。此时若仍注入不成功,原因在**agent 前置条件**,而不是通道选错。
   - 通道由运行时配置自动决定,无需向用户询问。
4. **多条 prepare 记录会互相遮蔽**:实测存在多条 `Running` 记录时,`blade create python` 取到的是**最早**那条的端口,而不是最新的。若刚 prepare 了新端口却注入到旧端口,先 `blade status --type prepare --status Running` 检查并 revoke 陈旧记录。

## 意图识别

从用户描述中提取两个维度：

| 维度 | 取值 | 用户表述示例 |
|---|---|---|
| target（哪个依赖） | redis / mysql / sqlalchemy / http / httpx / grpc / kafka | "Redis 变慢"、"数据库查询报错"、"调用下游超时" |
| action（哪种异常） | delay / throwCustomException / returnValue | "变慢/延迟"、"报错/抛异常/连不上"、"返回脏数据/空值" |

matcher（收窄影响面)按 target 决定：

| target | matcher | 含义 |
|---|---|---|
| redis | cmd, key | 只影响某个命令（GET/SET）或某个 key |
| mysql / sqlalchemy | sql, sqltype, database | 只影响某类 SQL(select/insert)、某个库或匹配的 SQL |
| http | url, method, host | 只影响某个 URL / 方法 / 目标域名 |
| httpx | url, method, host, path | 同上，另可按 path 收窄 |
| grpc | service, method | 只影响某个 gRPC 服务 / 方法 |
| kafka | topic, operation | 只影响某个 topic / 生产或消费操作 |

## 用例选择

用例清单在 `references/catalogue/` 下，按 `Python_<依赖><故障>` 组织。已覆盖的组合：

| target | delay | throwCustomException | returnValue |
|---|---|---|---|
| redis | ✅ 缓存响应变慢 | ✅ 缓存不可用 | ✅ 缓存返回空值 |
| mysql | ✅ 慢查询拖垮连接池 | ✅ 数据库查询抛异常 | — |
| http | ✅ 下游接口响应变慢 | ✅ 下游接口连接失败 | — |
| grpc | ✅ 微服务调用超时 | — | — |
| kafka | — | ✅ 消息生产失败 | — |
| httpx / sqlalchemy | — | — | — |

选择规则：

1. 先按 target 定目录（如 `Python_Redis延迟`）
2. 再按 action 定文件
3. 读取用例的「演练步骤 / 注入验证 / 注入恢复」并据此提交意图

**检索结果必须校验 action（重要）**：检索在某个 target 下找不到对应 action 的用例时，会退化返回该 target 下**其他 action** 的用例（例如查 `mysql` + `returnValue` 会返回延迟与异常的用例）。这类用例的注入参数与**验证方法都不适用**——延迟用例验证"耗时上升"，异常用例验证"抛出异常"，都无法验证返回值被篡改。

因此读到用例后先核对标题中的动作与你的意图是否一致：

- 一致 → 按用例执行。
- 不一致，或该 target 完全没有用例（httpx / sqlalchemy）→ **只借用它的前置条件、恢复方式与"进程内注入不改变系统状态"的验证原则**，注入参数按「意图识别」一节的 matcher 表自行构造，验证方法按本文「验证原则」一节对应 action 选取。不要照搬不符动作的演练步骤。
- `httpx` 可参照 `http` 用例（多一个 `--path` matcher）；`sqlalchemy` 可参照 `mysql` 用例（matcher 相同，`--database` 匹配的是驱动/scheme）。

## 验证原则（关键）

本类故障**不改变**系统与集群状态,所以：

- `kubectl get/describe`、CPU/内存/磁盘/网络指标在注入期间**正常是预期的**,不能据此判断注入失败。
- 验证必须落在应用层,且与 action 对应：
  - `delay` → 被拦截调用的耗时上升约等于配置的 time（与注入前基线对比）
  - `throwCustomException` → 被拦截调用抛出配置的异常类型（应用日志 / 错误率可见）
  - `returnValue` → 被拦截调用返回配置的值而非真实值
- 未匹配 matcher 的调用保持正常,这是设计使然,不是注入不完整。
