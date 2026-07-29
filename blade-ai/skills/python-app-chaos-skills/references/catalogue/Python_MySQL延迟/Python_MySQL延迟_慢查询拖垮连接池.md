**用例名称** 慢查询拖垮连接池 导致 Python_MySQL延迟

**故障现象**：
1. 应用执行匹配的 SQL 时耗时显著上升（每次命中的查询额外增加固定延迟）
2. 连接被长时间占用，连接池可用连接耗尽，后续请求在获取连接处排队甚至超时
3. 依赖数据库的接口整体变慢，暴露「无查询超时 / 连接池过小 / 缺少降级」的问题

**资源准备**：
1. 已生成 Agent hook:`blade prepare python --port 9526 --target-script <应用入口脚本>`(`--target-script` 必填,hook 文件 `sitecustomize.py` 落在该脚本所在目录;blade 自带 agent 库,无需 pip install;端口须空闲)
2. 目标 Python 应用已以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` **重启**,Agent 才在应用进程内监听;且应用使用 `mysql-connector` 或 `PyMySQL` 客户端(若走 SQLAlchemy ORM 请改用 target=sqlalchemy)
3. 已记录注入前该接口/该查询的耗时基线,以及**连接池使用率基线**
4. 确认应用侧可观测:接口耗时指标、连接池指标或应用日志

> prepare 返回 success 与 `blade status --type prepare` 显示 `Running` 都**不代表 Agent 已存活**,只是记录状态。真实状态由注入结果反推:报 `no running python preparation record found` 说明缺 prepare(可当场补做);报 `connect: connection refused` 说明 Agent 不在进程内(需重启应用,演练中无法补做,按前置条件不满足上报)。不要尝试用 curl 等命令探活——它们不在 Agent 可执行白名单内。

**演练步骤**：
1. 记录注入前基线:调用一次依赖该查询的接口,记录耗时与连接池使用率
2. 对指定 SQL 类型注入延迟:
   ```bash
   blade create python mysql delay --time 3000 --sqltype select --timeout 600
   ```
   - `--time`:延迟毫秒数(**必填**)
   - `--sqltype` / `--sql` / `--database`:收窄影响面,只影响某类 SQL(如 select)、匹配的 SQL 或某个库;**全部省略则影响所有 SQL 执行**(含写操作,爆炸半径显著更大)
   - `--offset`:可选,在 `--time` 之上再叠加 0~offset 毫秒的随机抖动(总延迟为 time ~ time+offset)
   - `--timeout`:实验自动结束时间(秒)
3. 记录返回的实验 uid,用于后续恢复

**注入验证**：
1. 再次调用该接口,确认耗时比基线增加约 `--time` 毫秒
2. 施加并发调用,观察连接池使用率上升、可用连接下降,甚至出现获取连接超时——这是「慢查询拖垮连接池」传导链成立的证据
3. 查询实验状态确认生效:
   ```bash
   blade status --uid <uid>
   ```
4. 确认未匹配的 SQL 类型(如注入 select 时执行 insert)耗时正常——matcher 生效的证据
5. 若耗时无变化且实验状态正常,说明应用未走到被拦截的调用(或 matcher 不匹配),按 matcher 重新收敛,而不是重复注入

**注入恢复**：
1. 销毁实验：
   ```bash
   blade destroy <uid>
   ```
2. 不要执行 `blade revoke`——它会删除 prepare 生成的 hook 文件,不停止任何故障,却让应用下次重启后失去 Agent,并影响同主机其他演练

**恢复验证**：
1. 再次调用接口,确认耗时回到注入前基线
2. `blade status --uid <uid>` 状态为 Destroyed
3. 确认连接池使用率回落、无残留排队请求

**基准事实**：
- **根因**：Agent 在应用进程内拦截 MySQL 游标的执行方法（`mysql.connector.cursor.MySQLCursor.execute` / `pymysql.cursors.Cursor.execute`），命中 matcher 时先 `time.sleep(time/1000)` 再执行真实 SQL，因此延迟叠加在每一次命中调用上，且**连接在睡眠期间持续被占用**
- **必现现象**：命中 matcher 的查询耗时增加约 `--time`；**MySQL 服务端完全健康**（其慢查询日志中看不到这些语句，因为延迟发生在客户端进程内而非服务端执行阶段）；系统指标与 Kubernetes 对象状态**不变**（进程内注入不改变外部状态，数据库指标正常属预期）
