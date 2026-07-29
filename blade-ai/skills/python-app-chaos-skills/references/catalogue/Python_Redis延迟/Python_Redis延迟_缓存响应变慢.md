**用例名称** 缓存响应变慢 导致 Python_Redis延迟

**故障现象**：
1. 应用中依赖 Redis 的接口响应时间显著上升（每次命中的 Redis 命令额外增加固定延迟）
2. 若应用未设置 Redis 超时或超时过长，请求线程/协程被占用，吞吐下降
3. 上游接口可能出现排队、超时甚至雪崩，暴露缺失降级逻辑的问题

**资源准备**：
1. 已生成 Agent hook:`blade prepare python --port 9526 --target-script <应用入口脚本>`(`--target-script` 必填,hook 文件 `sitecustomize.py` 落在该脚本所在目录;blade 自带 agent 库,无需 pip install;端口须空闲)
2. 目标 Python 应用已以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` **重启**,Agent 才在应用进程内监听;且应用使用 `redis` 客户端库
3. 已记录注入前该接口/该 Redis 命令的耗时基线(用于对比)
4. 确认应用侧可观测:接口耗时指标或应用日志

> prepare 返回 success 与 `blade status --type prepare` 显示 `Running` 都**不代表 Agent 已存活**,只是记录状态。真实状态由注入结果反推:报 `no running python preparation record found` 说明缺 prepare(可当场补做);报 `connect: connection refused` 说明 Agent 不在进程内(需重启应用,演练中无法补做,按前置条件不满足上报)。不要尝试用 curl 等命令探活——它们不在 Agent 可执行白名单内。

**演练步骤**：
1. 记录注入前基线:调用一次依赖 Redis 的接口,记录耗时
2. 对指定 Redis 命令注入延迟:
   ```bash
   blade create python redis delay --time 500 --cmd GET --timeout 600
   ```
   - `--time`:延迟毫秒数(**必填**)
   - `--cmd` / `--key`:收窄影响面,只影响该命令 / 该 key;**省略则影响所有 Redis 命令**
   - `--offset`:可选随机抖动毫秒数
   - `--timeout`:实验自动结束时间(秒)
3. 记录返回的实验 uid,用于后续恢复

**注入验证**：
1. 再次调用依赖 Redis 的接口,确认耗时比基线增加约 `--time` 毫秒
2. 查询实验状态确认生效:
   ```bash
   blade status --uid <uid>
   ```
3. 确认未匹配的命令(如注入 GET 时执行 SET)耗时正常——这是 matcher 生效的证据
4. 若耗时无变化且实验状态正常,说明应用未走到被拦截的调用(或 matcher 不匹配),按 matcher 重新收敛,而不是重复注入

**注入恢复**：
1. 销毁实验：
   ```bash
   blade destroy <uid>
   ```
2. 不要执行 `blade revoke`——它会删除 prepare 生成的 hook 文件,不停止任何故障,却让应用下次重启后失去 Agent,并影响同主机其他演练

**恢复验证**：
1. 再次调用接口，确认耗时回到注入前基线
2. `blade status --uid <uid>` 状态为 Destroyed
3. 确认应用无残留超时/排队现象

**基准事实**：
- **根因**：Agent 在应用进程内拦截 `redis.client.Redis.execute_command`，命中 matcher 时先 `time.sleep(time/1000)` 再执行真实命令，因此延迟叠加在每一次命中调用上
- **必现现象**：命中 matcher 的 Redis 调用耗时增加约 `--time`；系统 CPU/内存/网络与 Kubernetes 对象状态**不变**（进程内注入不改变系统状态，指标正常属预期）
