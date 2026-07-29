**用例名称** 缓存不可用 导致 Python_Redis异常

**故障现象**：
1. 应用执行匹配的 Redis 命令时抛出连接异常，而非返回缓存值
2. 若应用未捕获异常，依赖缓存的接口直接返回 5xx；若捕获后回源，则请求全量穿透到数据库
3. 缓存穿透导致数据库负载骤升，暴露「缓存不可用时缺少熔断/限流/本地兜底」的问题

**资源准备**：
1. 已生成 Agent hook:`blade prepare python --port 9526 --target-script <应用入口脚本>`(`--target-script` 必填,hook 文件 `sitecustomize.py` 落在该脚本所在目录;blade 自带 agent 库,无需 pip install;端口须空闲)
2. 目标 Python 应用已以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` **重启**,Agent 才在应用进程内监听;且应用使用 `redis` 客户端库
3. 已记录注入前该接口的成功率基线,以及**数据库的 QPS/负载基线**(用于观测穿透)
4. 确认应用日志可见异常堆栈

> prepare 返回 success 与 `blade status --type prepare` 显示 `Running` 都**不代表 Agent 已存活**,只是记录状态。真实状态由注入结果反推:报 `no running python preparation record found` 说明缺 prepare(可当场补做);报 `connect: connection refused` 说明 Agent 不在进程内(需重启应用,演练中无法补做,按前置条件不满足上报)。不要尝试用 curl 等命令探活——它们不在 Agent 可执行白名单内。

**演练步骤**：
1. 记录注入前基线:调用一次依赖缓存的接口,确认命中缓存且正常返回;记录数据库 QPS
2. 对指定 Redis 命令注入异常:
   ```bash
   blade create python redis throwCustomException --exception redis.exceptions.ConnectionError --exception-message "chaos drill: redis unavailable" --cmd GET --timeout 600
   ```
   - `--exception`:异常类名。支持内置名(`ConnectionError`/`TimeoutError` 等)或全限定路径(如 `redis.exceptions.ConnectionError`)
   - `--exception-message`:异常消息,含空格时必须加引号
   - `--cmd` / `--key`:收窄影响面,只影响该命令 / 该 key;**全部省略则影响所有 Redis 命令**(含写命令,爆炸半径显著更大)
   - `--timeout`:实验自动结束时间(秒)
3. 记录返回的实验 uid,用于后续恢复

**注入验证**：
1. 调用依赖缓存的接口,确认读缓存失败(返回错误或走回源分支)
2. 查看应用日志确认抛出的正是配置的异常类型与消息:
   ```
   redis.exceptions.ConnectionError: chaos drill: redis unavailable
   ```
   - 若日志中看到的是 `RuntimeError` 而非配置的类型,说明该异常类名**无法在应用进程内导入**,Agent 已静默降级为 `RuntimeError`。此时应用「按 Redis 异常类型判断是否回源」的逻辑不会被真正验证,应改用应用确实能导入的类名重注
3. 观察数据库侧 QPS/负载是否随之上升——这是缓存穿透真实发生的证据;若数据库无变化,说明应用有本地兜底或直接返回错误,需据实记录而非判定注入失败
4. 查询实验状态确认生效:
   ```bash
   blade status --uid <uid>
   ```
5. 确认未匹配的命令(如注入 GET 时执行 SET)仍正常——matcher 生效的证据

**注入恢复**：
1. 销毁实验：
   ```bash
   blade destroy <uid>
   ```
2. 不要执行 `blade revoke`——它会删除 prepare 生成的 hook 文件,不停止任何故障,却让应用下次重启后失去 Agent,并影响同主机其他演练

**恢复验证**：
1. 再次调用接口,确认恢复命中缓存并正常返回
2. `blade status --uid <uid>` 状态为 Destroyed
3. 确认数据库 QPS/负载回落到基线,应用日志不再出现注入的异常

**基准事实**：
- **根因**：Agent 在应用进程内拦截 `redis.client.Redis.execute_command`，命中 matcher 时**不执行真实命令**而直接抛出配置的异常（受控中断），因此 Redis 服务端从未收到该命令
- **必现现象**：命中 matcher 的 Redis 调用抛出配置异常；**Redis 服务端完全健康**（连接数/内存/命中率正常，且其慢日志与命令统计中看不到这些命令）；系统指标与 Kubernetes 对象状态**不变**（进程内注入不改变外部状态，Redis 指标正常属预期）
