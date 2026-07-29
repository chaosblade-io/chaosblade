**用例名称** 微服务调用超时 导致 Python_gRPC延迟

**故障现象**：
1. 应用发起的 gRPC 一元调用耗时显著上升（每次命中的调用额外增加固定延迟）
2. 若调用未设置 deadline 或 deadline 大于注入延迟，调用线程/协程被长时间占用，并发能力下降
3. 若设置了 deadline 且小于注入延迟，客户端抛出 `DEADLINE_EXCEEDED`，暴露上游是否有降级与重试保护

**资源准备**：
1. 已生成 Agent hook:`blade prepare python --port 9526 --target-script <应用入口脚本>`(`--target-script` 必填,hook 文件 `sitecustomize.py` 落在该脚本所在目录;blade 自带 agent 库,无需 pip install;端口须空闲)
2. 目标 Python 应用已以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` **重启**,Agent 才在应用进程内监听;且应用使用 `grpc` 客户端发起**一元(unary-unary)调用**
3. 已记录注入前该 gRPC 方法的耗时基线
4. 确认应用侧可观测:调用耗时指标或应用日志

> prepare 返回 success 与 `blade status --type prepare` 显示 `Running` 都**不代表 Agent 已存活**,只是记录状态。真实状态由注入结果反推:报 `no running python preparation record found` 说明缺 prepare(可当场补做);报 `connect: connection refused` 说明 Agent 不在进程内(需重启应用,演练中无法补做,按前置条件不满足上报)。不要尝试用 curl 等命令探活——它们不在 Agent 可执行白名单内。

**演练步骤**：
1. 记录注入前基线:触发一次该 gRPC 调用,记录耗时
2. 对指定 gRPC 方法注入延迟:
   ```bash
   blade create python grpc delay --time 2000 --service UserService --method /pkg.UserService/GetUser --timeout 600
   ```
   - `--time`:延迟毫秒数(**必填**)
   - `--service`:gRPC 服务名;`--method`:方法全路径(形如 `/pkg.Svc/Method`)
   - 两者**全部省略则影响该应用发出的所有一元 gRPC 调用**
   - `--offset`:可选,在 `--time` 之上再叠加 0~offset 毫秒的随机抖动(总延迟为 time ~ time+offset)
   - `--timeout`:实验自动结束时间(秒)
3. 记录返回的实验 uid,用于后续恢复

**注入验证**：
1. 再次触发该调用,确认耗时比基线增加约 `--time` 毫秒
2. 查询实验状态确认生效:
   ```bash
   blade status --uid <uid>
   ```
3. 若调用配置的 deadline 小于注入延迟,预期看到 `DEADLINE_EXCEEDED`——这是 deadline 生效的正向证据
4. 确认未匹配的方法调用耗时正常——matcher 生效的证据
5. 若耗时无变化且实验状态正常,优先排查两点:调用是否为**一元调用**(流式调用不在拦截点内),以及 `--method` 是否写成了完整路径形式;而不是重复注入

**注入恢复**：
1. 销毁实验：
   ```bash
   blade destroy <uid>
   ```
2. 不要执行 `blade revoke`——它会删除 prepare 生成的 hook 文件,不停止任何故障,却让应用下次重启后失去 Agent,并影响同主机其他演练

**恢复验证**：
1. 再次触发调用,确认耗时回到注入前基线
2. `blade status --uid <uid>` 状态为 Destroyed
3. 确认应用无残留超时与重试堆积

**基准事实**：
- **根因**：Agent 在应用进程内拦截 `grpc._channel._UnaryUnaryMultiCallable.__call__`，命中 matcher 时先 `time.sleep(time/1000)` 再发起真实调用，因此延迟叠加在每一次命中调用上
- **适用范围限制**：拦截点是**一元-一元**调用，流式调用（stream-stream / server-stream 等）不受该拦截点影响
- **必现现象**：命中 matcher 的 gRPC 调用耗时增加约 `--time`；**被调服务端完全健康**，其耗时指标不变；系统指标与 Kubernetes 对象状态**不变**（进程内注入不改变外部状态，服务端指标正常属预期）
