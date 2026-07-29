**用例名称** 数据库查询抛异常 导致 Python_MySQL异常

**故障现象**：
1. 应用执行匹配的 SQL 时抛出数据库异常（如连接错误），而非返回结果
2. 依赖该查询的接口返回 5xx 或降级响应，错误率上升
3. 若应用缺少异常捕获/重试/降级，故障沿调用链向上扩散

**资源准备**：
1. 已生成 Agent hook:`blade prepare python --port 9526 --target-script <应用入口脚本>`(`--target-script` 必填,hook 文件 `sitecustomize.py` 落在该脚本所在目录;blade 自带 agent 库,无需 pip install;端口须空闲)
2. 目标 Python 应用已以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` **重启**,Agent 才在应用进程内监听;且使用 `mysql-connector` 或 `PyMySQL` 客户端
3. 已记录注入前该接口的成功率/错误率基线
4. 确认应用日志可见异常堆栈

> prepare 返回 success 与 `blade status --type prepare` 显示 `Running` 都**不代表 Agent 已存活**,只是记录状态。真实状态由注入结果反推:报 `no running python preparation record found` 说明缺 prepare(可当场补做);报 `connect: connection refused` 说明 Agent 不在进程内(需重启应用,演练中无法补做,按前置条件不满足上报)。不要尝试用 curl 等命令探活——它们不在 Agent 可执行白名单内。

**演练步骤**：
1. 记录注入前基线:调用一次依赖该 SQL 的接口,确认正常返回
2. 对指定 SQL 类型注入异常:
   ```bash
   blade create python mysql throwCustomException \
     --exception ConnectionError \
     --exception-message "chaos drill: mysql unavailable" \
     --sqltype select \
     --timeout 600
   ```
   - `--exception`:异常类名,支持内置名(`ConnectionError`/`TimeoutError`)或全限定路径(如 `pymysql.err.OperationalError`)
   - `--exception-message`:含空格时必须加引号
   - `--sqltype` / `--database` / `--sql`:收窄影响面;**全部省略则影响所有 SQL 执行**
   - `--timeout`:实验自动结束时间(秒)
3. 记录返回的实验 uid,用于后续恢复

**注入验证**：
1. 调用依赖该 SQL 的接口,确认返回错误而非正常结果
2. 查看应用日志确认抛出的正是配置的异常类型与消息:
   ```
   ConnectionError: chaos drill: mysql unavailable
   ```
3. 查询实验状态确认生效:
   ```bash
   blade status --uid <uid>
   ```
4. 确认未匹配的 SQL 类型(如注入 select 时执行 insert)仍正常——matcher 生效的证据

**注入恢复**：
1. 销毁实验：
   ```bash
   blade destroy <uid>
   ```
2. 不要执行 `blade revoke`——它会删除 prepare 生成的 hook 文件,不停止任何故障,却让应用下次重启后失去 Agent,并影响同主机其他演练

**恢复验证**：
1. 再次调用接口，确认恢复正常返回
2. `blade status --uid <uid>` 状态为 Destroyed
3. 确认应用日志不再出现注入的异常

**基准事实**：
- **根因**：Agent 在应用进程内拦截 MySQL 游标的执行方法，命中 matcher 时不执行真实 SQL 而直接抛出配置的异常（受控中断），因此数据库本身完全健康
- **必现现象**：命中 matcher 的查询抛出配置异常；MySQL 服务端连接数/负载与 Kubernetes 对象状态**不变**（进程内注入不改变外部状态，数据库指标正常属预期）
