**用例名称** 下游接口响应变慢 导致 Python_HTTP延迟

**故障现象**：
1. 应用通过 `requests` 调用下游服务的接口响应时间显著上升（每次命中的请求额外增加固定延迟）
2. 若应用未设置 `timeout` 或超时值大于注入延迟，请求线程被长时间占用，连接池耗尽、吞吐下降
3. 延迟沿调用链向上传播，上游接口出现排队、超时甚至雪崩，暴露缺失超时/降级配置的问题

**资源准备**：
1. 已生成 Agent hook:`blade prepare python --port 9526 --target-script <应用入口脚本>`(`--target-script` 必填,hook 文件 `sitecustomize.py` 落在该脚本所在目录;blade 自带 agent 库,无需 pip install;端口须空闲)
2. 目标 Python 应用已以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` **重启**,Agent 才在应用进程内监听;且应用使用 `requests` 客户端库(异步 `httpx` 请改用 target=httpx)
3. 已记录注入前该接口的耗时基线(用于对比)
4. 确认应用侧可观测:接口耗时指标或应用日志

> prepare 返回 success 与 `blade status --type prepare` 显示 `Running` 都**不代表 Agent 已存活**,只是记录状态。真实状态由注入结果反推:报 `no running python preparation record found` 说明缺 prepare(可当场补做);报 `connect: connection refused` 说明 Agent 不在进程内(需重启应用,演练中无法补做,按前置条件不满足上报)。不要尝试用 curl 等命令探活——它们不在 Agent 可执行白名单内。

**演练步骤**：
1. 记录注入前基线:调用一次依赖该下游的接口,记录耗时
2. 对指定下游请求注入延迟:
   ```bash
   blade create python http delay --time 2000 --url /api/users --method GET --timeout 600
   ```
   - `--time`:延迟毫秒数(**必填**)
   - `--url` / `--method` / `--host`:收窄影响面,只影响匹配的请求;**全部省略则影响该应用发出的所有 requests 请求**
   - `--offset`:可选,在 `--time` 之上再叠加 0~offset 毫秒的随机抖动(总延迟为 time ~ time+offset)
   - `--timeout`:实验自动结束时间(秒)
3. 记录返回的实验 uid,用于后续恢复

**注入验证**：
1. 再次调用该接口,确认耗时比基线增加约 `--time` 毫秒
2. 查询实验状态确认生效:
   ```bash
   blade status --uid <uid>
   ```
3. 若应用配置的 `timeout` 小于注入延迟,预期看到应用侧抛出 `requests.exceptions.Timeout` 或走降级分支——这本身就是超时配置生效的正向证据
4. 确认未匹配的请求(如注入 `/api/users` 时调用 `/api/orders`)耗时正常——这是 matcher 生效的证据
5. 若耗时无变化且实验状态正常,说明应用未走到被拦截的调用(或 matcher 不匹配,注意 `--url` 是按请求 URL 匹配),按 matcher 重新收敛,而不是重复注入

**注入恢复**：
1. 销毁实验：
   ```bash
   blade destroy <uid>
   ```
2. 不要执行 `blade revoke`——它会删除 prepare 生成的 hook 文件,不停止任何故障,却让应用下次重启后失去 Agent,并影响同主机其他演练

**恢复验证**：
1. 再次调用接口,确认耗时回到注入前基线
2. `blade status --uid <uid>` 状态为 Destroyed
3. 确认应用无残留超时/排队现象,连接池恢复正常

**基准事实**：
- **根因**：Agent 在应用进程内拦截 `requests.adapters.HTTPAdapter.send`，命中 matcher 时先 `time.sleep(time/1000)` 再发出真实请求，因此延迟叠加在每一次命中调用上
- **必现现象**：命中 matcher 的 HTTP 调用耗时增加约 `--time`；**下游服务本身完全健康**，其服务端指标、CPU/内存/网络与 Kubernetes 对象状态**不变**（进程内注入不改变外部状态，下游指标正常属预期）
