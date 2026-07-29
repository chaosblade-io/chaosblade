**用例名称** 消息生产失败 导致 Python_Kafka异常

**故障现象**：
1. 被拦截的 Kafka 调用直接抛出异常，真实操作从未执行。受影响的一侧取决于 `--operation`：
   - `produce`（本用例默认）：`KafkaProducer.send` 抛异常，消息未进入 Kafka；**消费侧不受影响**
   - `consume`：`KafkaConsumer.poll` 抛异常，消费位点停滞；**生产侧不受影响，topic 消息量仍在增长**
   - `--topic` 与 `--operation` 全部省略：生产与消费**两侧都受影响**，爆炸半径显著更大
2. 若应用未捕获异常，业务流程中断、接口返回 5xx；若捕获但无补偿，消息静默丢失
3. 暴露「消息发送失败后是否有本地暂存/重投/告警」的问题——这类丢失往往在故障恢复后才被发现

**资源准备**：
1. 已生成 Agent hook:`blade prepare python --port 9526 --target-script <应用入口脚本>`(`--target-script` 必填,hook 文件 `sitecustomize.py` 落在该脚本所在目录;blade 自带 agent 库,无需 pip install;端口须空闲)
2. 目标 Python 应用已以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` **重启**,Agent 才在应用进程内监听;且应用使用 `kafka-python` 客户端
3. 已记录注入前该 topic 的生产速率/成功率基线,以及下游消费位点
4. 确认应用日志可见异常堆栈

> prepare 返回 success 与 `blade status --type prepare` 显示 `Running` 都**不代表 Agent 已存活**,只是记录状态。真实状态由注入结果反推:报 `no running python preparation record found` 说明缺 prepare(可当场补做);报 `connect: connection refused` 说明 Agent 不在进程内(需重启应用,演练中无法补做,按前置条件不满足上报)。不要尝试用 curl 等命令探活——它们不在 Agent 可执行白名单内。

**演练步骤**：
1. 记录注入前基线:触发一次消息生产,确认消息可被下游消费
2. 对指定 topic 的生产操作注入异常:
   ```bash
   blade create python kafka throwCustomException --exception kafka.errors.KafkaTimeoutError --exception-message "chaos drill: kafka produce failed" --topic order-events --operation produce --timeout 600
   ```
   - `--exception`:异常类名。支持内置名(`TimeoutError`/`ConnectionError` 等)或全限定路径(如 `kafka.errors.KafkaTimeoutError`)
   - `--exception-message`:异常消息,含空格时必须加引号
   - `--operation`:取值仅 `produce`(拦截 `KafkaProducer.send`)或 `consume`(拦截 `KafkaConsumer.poll`)
   - `--topic`:限定 topic;**topic 与 operation 全部省略则生产与消费两侧都受影响**,爆炸半径显著更大
   - `--timeout`:实验自动结束时间(秒)
3. 记录返回的实验 uid,用于后续恢复

**注入验证**：
1. **（主证据，必做）** 触发**与本次 `--operation` 匹配的那一侧调用**（`produce` → 生产一条消息；`consume` → 触发一次拉取），确认抛出配置的异常类型与消息：
   ```
   kafka.errors.KafkaTimeoutError: chaos drill: kafka produce failed
   ```
   - 若日志中看到的是 `RuntimeError` 而非配置的类型,说明该异常类名**无法在应用进程内导入**,Agent 已静默降级为 `RuntimeError`。此时应用「按 Kafka 异常类型决定是否重投」的逻辑不会被真正验证,应改用应用确实能导入的类名重注
2. **（只做与本次 `--operation` 匹配的分支）** 其余分支的现象在本次注入下**不可能出现**，直接标记为 `expected` 并跳过：
   - **`produce`**：a) Kafka 侧确认该 topic 消息量停止增长、下游消费位点不再推进——这是消息确实未发出的证据；b) 确认**消费侧仍正常**——这是 operation matcher 生效的证据
   - **`consume`**：a) 确认消费位点停滞、下游处理中断；b) 确认**生产侧仍正常写入，topic 消息量仍在增长**——这是 matcher 生效的证据，**不是失败**
   - **省略 topic 与 operation（双侧）**：两侧都应抛异常；此时**不存在"另一侧仍正常"的对照证据，不要去找**
3. 查询实验状态确认生效:
   ```bash
   blade status --uid <uid>
   ```
4. 重点检查:应用是否记录了失败消息以便补偿。**若无补偿机制,这些消息在恢复后不会自动补发**,需在演练结论中显式指出

> ⚠️ 验证纪律：
> - **严禁为不适用的一侧反复找证据**。注入 `produce` 时消费必然正常、注入 `consume` 时生产必然正常，观察到"正常"是 matcher 生效的**证据**，不是失败。
> - 本故障是**进程内受控中断**：Kafka 集群指标、Broker 状态、分区状态、系统指标与 Kubernetes 对象状态**全部正常且不变**，这是预期——不要去 Kafka 集群侧找异常。
> - 若注入限定了 `--topic`，**其它 topic 收发正常是预期**；验证必须落在被限定的那个 topic 上。
> - 同一事实（如实验是否生效）确认一次即可，不要重复查询。

**注入恢复**：
1. 销毁实验：
   ```bash
   blade destroy <uid>
   ```
2. 不要执行 `blade revoke`——它会删除 prepare 生成的 hook 文件,不停止任何故障,却让应用下次重启后失去 Agent,并影响同主机其他演练

**恢复验证**：
1. 再次触发消息生产,确认成功写入且下游可消费
2. `blade status --uid <uid>` 状态为 Destroyed
3. 确认应用日志不再出现注入的异常;**核对故障期间的消息是否有缺口**,如有则需业务侧补偿

**基准事实**：
- **根因**：Agent 在应用进程内拦截 `kafka.KafkaProducer.send`（`--operation produce`）或 `kafka.KafkaConsumer.poll`（`--operation consume`），命中 matcher 时**不执行真实操作**而直接抛出配置的异常（受控中断），因此 Kafka 集群从未收到该请求
- **必现现象**：命中 matcher 的生产调用抛出配置异常且消息不入队；**Kafka 集群完全健康**（Broker 指标、分区状态正常）；系统指标与 Kubernetes 对象状态**不变**（进程内注入不改变外部状态，Kafka 指标正常属预期）
- **数据影响**：与延迟类故障不同，本故障会造成**真实的消息缺失**，恢复实验并不会自动补回，需按业务补偿流程处理
