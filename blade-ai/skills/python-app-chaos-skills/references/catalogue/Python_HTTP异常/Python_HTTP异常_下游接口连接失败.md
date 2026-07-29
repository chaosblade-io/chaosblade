**用例名称** 下游接口连接失败 导致 Python_HTTP异常

**故障现象**：
1. 应用通过 `requests` 调用下游服务时直接抛出连接异常，而非收到响应
2. 依赖该下游的接口返回 5xx 或降级响应，错误率上升
3. 若应用缺少异常捕获/重试/熔断，故障沿调用链向上扩散；若重试无退避，反而放大请求量

**资源准备**：
1. 已生成 Agent hook:`blade prepare python --port 9526 --target-script <应用入口脚本>`(`--target-script` 必填,hook 文件 `sitecustomize.py` 落在该脚本所在目录;blade 自带 agent 库,无需 pip install;端口须空闲)
2. 目标 Python 应用已以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` **重启**,Agent 才在应用进程内监听;且应用使用 `requests` 客户端库
3. 已记录注入前该接口的成功率/错误率基线
4. 确认应用日志可见异常堆栈,且可观测下游调用的重试次数

> prepare 返回 success 与 `blade status --type prepare` 显示 `Running` 都**不代表 Agent 已存活**,只是记录状态。真实状态由注入结果反推:报 `no running python preparation record found` 说明缺 prepare(可当场补做);报 `connect: connection refused` 说明 Agent 不在进程内(需重启应用,演练中无法补做,按前置条件不满足上报)。不要尝试用 curl 等命令探活——它们不在 Agent 可执行白名单内。

**演练步骤**：
1. 记录注入前基线:调用一次依赖该下游的接口,确认正常返回
2. 对指定下游请求注入异常:
   ```bash
   blade create python http throwCustomException --exception requests.exceptions.ConnectionError --exception-message "chaos drill: downstream unreachable" --url /api/users --timeout 600
   ```
   - `--exception`:异常类名。支持内置名(`ConnectionError`/`TimeoutError`/`RuntimeError` 等)或全限定路径(如 `requests.exceptions.ConnectionError`)
   - `--exception-message`:异常消息,含空格时必须加引号
   - `--url` / `--method` / `--host`:收窄影响面;**全部省略则影响该应用发出的所有 requests 请求**
   - `--timeout`:实验自动结束时间(秒)
3. 记录返回的实验 uid,用于后续恢复

**注入验证**：
1. 调用依赖该下游的接口,确认返回错误或降级响应而非正常结果
2. 查看应用日志确认抛出的正是配置的异常类型与消息:
   ```
   requests.exceptions.ConnectionError: chaos drill: downstream unreachable
   ```
   - 若日志中看到的是 `RuntimeError` 而非配置的类型,说明该异常类名**无法在应用进程内导入**,Agent 已静默降级为 `RuntimeError`。故障仍然生效,但应用的按异常类型分支的逻辑不会被真正验证——应改用应用确实能导入的类名重注
3. 查询实验状态确认生效:
   ```bash
   blade status --uid <uid>
   ```
4. 确认未匹配的请求(如注入 `/api/users` 时调用 `/api/orders`)仍正常——matcher 生效的证据
5. 观察重试行为:确认重试次数有上限且带退避,而非无限重试放大流量

**注入恢复**：
1. 销毁实验：
   ```bash
   blade destroy <uid>
   ```
2. 不要执行 `blade revoke`——它会删除 prepare 生成的 hook 文件,不停止任何故障,却让应用下次重启后失去 Agent,并影响同主机其他演练

**恢复验证**：
1. 再次调用接口,确认恢复正常返回
2. `blade status --uid <uid>` 状态为 Destroyed
3. 确认应用日志不再出现注入的异常,熔断器(若有)回到闭合状态

**基准事实**：
- **根因**：Agent 在应用进程内拦截 `requests.adapters.HTTPAdapter.send`，命中 matcher 时**不发出真实请求**而直接抛出配置的异常（受控中断），因此下游服务从未收到该请求
- **必现现象**：命中 matcher 的 HTTP 调用抛出配置异常；**下游服务完全健康**且其访问日志中看不到这些请求；系统指标与 Kubernetes 对象状态**不变**（进程内注入不改变外部状态，下游指标正常属预期）
