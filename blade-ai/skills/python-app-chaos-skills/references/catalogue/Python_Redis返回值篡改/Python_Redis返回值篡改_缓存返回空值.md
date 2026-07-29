**用例名称** 缓存返回空值 导致 Python_Redis返回值篡改

**故障现象**：
1. 应用读取缓存时拿到被篡改的返回值（如 `None`），而非真实缓存内容——**调用不报错**，因此比异常类故障更隐蔽
2. 若应用把 `None` 当作"缓存未命中"，则全量回源查库，形成缓存穿透
3. 若应用未校验空值就直接使用，可能返回错误业务数据、抛出 `AttributeError`/`TypeError`，甚至把空值写回缓存造成污染扩散

**资源准备**：
1. 已生成 Agent hook:`blade prepare python --port 9526 --target-script <应用入口脚本>`(`--target-script` 必填,hook 文件 `sitecustomize.py` 落在该脚本所在目录;blade 自带 agent 库,无需 pip install;端口须空闲)
2. 目标 Python 应用已以 `PYTHONPATH=<hook 目录>:$PYTHONPATH` **重启**,Agent 才在应用进程内监听;且应用使用 `redis` 客户端库
3. 已记录注入前该接口返回内容的基线,以及数据库 QPS 基线(用于观测穿透)
4. 确认应用侧可观测:接口返回内容、错误率、数据库 QPS

> prepare 返回 success 与 `blade status --type prepare` 显示 `Running` 都**不代表 Agent 已存活**,只是记录状态。真实状态由注入结果反推:报 `no running python preparation record found` 说明缺 prepare(可当场补做);报 `connect: connection refused` 说明 Agent 不在进程内(需重启应用,演练中无法补做,按前置条件不满足上报)。不要尝试用 curl 等命令探活——它们不在 Agent 可执行白名单内。

**安全提示**：本故障返回的是**假数据**且调用不报错,应用可能把假数据写回缓存或落库。注入前必须确认演练环境无真实业务写入,并优先用 `--key` 把影响面收窄到测试 key。

**演练步骤**：
1. 记录注入前基线:调用一次依赖缓存的接口,记录返回内容与数据库 QPS
2. 对指定 Redis 读命令注入空返回值:
   ```bash
   blade create python redis returnValue --return-value null --cmd GET --key chaos:test:* --timeout 600
   ```
   - `--return-value`:取值按以下规则解析——`null`/`none` → `None`;`true`/`false` → 布尔;纯数字 → 整数或浮点;以 `{` 或 `[` 开头 → 解析为 JSON;其余 → 原样字符串
     - **没有 `nil` 这个关键字**:写 `--return-value nil` 会返回三个字符的字符串 `"nil"`,而不是空值
   - `--cmd` / `--key`:收窄影响面。**强烈建议至少指定 `--cmd`**,全部省略会让该客户端每一次 Redis 调用都返回假值(包括写命令的返回值)
   - `--timeout`:实验自动结束时间(秒)
3. 记录返回的实验 uid,用于后续恢复

**注入验证**：
1. 调用依赖缓存的接口,确认拿到的是被篡改的值而非真实缓存内容,且**调用本身没有报错**
2. 判断应用的处理分支并据实记录：
   - 回源查库 → 观察数据库 QPS 上升(缓存穿透成立)
   - 直接使用空值 → 观察是否出现 `AttributeError`/`TypeError` 或错误业务结果
   - 有空值校验并降级 → 这是防护到位的正向证据
3. 查询实验状态确认生效:
   ```bash
   blade status --uid <uid>
   ```
4. 确认未匹配的命令/key 返回值正常——matcher 生效的证据
5. 若返回值无变化且实验状态正常,优先确认应用读的是否为被 matcher 限定的命令与 key,而不是重复注入

**注入恢复**：
1. 销毁实验：
   ```bash
   blade destroy <uid>
   ```
2. 不要执行 `blade revoke`——它会删除 prepare 生成的 hook 文件,不停止任何故障,却让应用下次重启后失去 Agent,并影响同主机其他演练

**恢复验证**：
1. 再次调用接口,确认返回真实缓存内容
2. `blade status --uid <uid>` 状态为 Destroyed
3. 数据库 QPS 回落到基线
4. **检查缓存与数据库中是否残留被写回的假值**:销毁实验只停止篡改,不回滚故障期间已落盘的数据,如有污染需业务侧清理

**基准事实**：
- **根因**：Agent 在应用进程内拦截 `redis.client.Redis.execute_command`，命中 matcher 时**不执行真实命令**而直接返回配置的值（受控中断，RETURN_IMMEDIATELY），因此 Redis 服务端从未收到该命令
- **必现现象**：命中 matcher 的 Redis 调用返回配置值且**不抛异常**；**Redis 服务端完全健康**，其真实数据未被读取也未被修改；系统指标与 Kubernetes 对象状态**不变**（进程内注入不改变外部状态）
- **数据影响**：本故障产生的假数据可能被应用写回缓存或数据库，销毁实验不会回滚这些副作用
