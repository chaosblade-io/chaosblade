**用例名称** 存储后端无响应 导致 Node_磁盘IO挂起

**适用说明**：ChaosBlade 的 disk 实验只有 `burn`（IO 压力）和 `fill`（空间占用），
**没有 io_hang**（IO 请求迟迟不返回）。本用例通过 `scripts/inject_io_hang.py` 用内核原生能力补齐该场景，
不使用 `blade create`，因此**没有 blade_uid**，恢复只能走脚本的 `--action recover`。

**故障现象**：
1. 应用的 write / fsync 长时间不返回，进程卡在 D 状态（不可中断睡眠）
2. `iostat` 的 `%util` 可能并不高 —— IO 是「卡住」而非「打满」，这是与 `node-disk burn` 的关键区别
3. 容器日志停止输出、健康检查超时、Pod 逐步进入 NotReady
4. 依赖该路径的数据库/中间件触发主备切换或写超时告警

**资源准备**：
1. 确认应用 A 已正常运行，并确认它实际写入哪个路径（`kubectl exec <pod> -- df -h`）
2. 确认监控可观测 Pod 就绪状态、应用写延迟与 `iowait`
3. 确认目标节点上有 `fsfreeze`（util-linux）；若使用 dm-delay 模式还需 `losetup` / `dmsetup` / `mkfs.ext4`
4. 确认执行 kubeconfig 对应的账号可以创建特权 Pod（脚本需要 privileged + hostPID 进入宿主机命名空间）

**模式选择**：

| 模式 | 作用范围 | 适用场景 | 风险 |
|------|---------|---------|------|
| `fsfreeze`（默认） | `--path` 所在的**整个文件系统** | 让运行中的应用真正卡在 write/fsync 上 | 该文件系统上所有进程一起卡住；**严禁用于根文件系统** |
| `dm-delay` | 仅脚本新建的挂载点 | 验证挂载点级别的存储后端无响应，爆炸半径可控 | 需要应用把数据卷指向该挂载点才能观察到影响 |

**演练步骤**：
1. 定位运行应用 A 的节点，记为 `<节点名>`
2. **路径校验（必须）**：确认 `--path` 落在**独立挂载点**上，而不是根分区：
   - `kubectl debug node/<节点名> --image=busybox -- sleep 3600`，再 `kubectl exec -it <debug-pod> -- df -h`
   - 用 `findmnt --target <路径>` 确认该路径所属挂载点
   - **禁止冻结 `/`**：根文件系统承载 kubelet 与容器运行时，冻结后节点无法恢复。脚本默认拒绝该操作
   - 推荐目标：应用的 PV 挂载点、独立数据盘（如 `/data`）、独立 imagefs
3. 注入（默认 fsfreeze 模式，300 秒后看门狗自动恢复）：
   ```
   execute_skill_script(
       skill_name="k8s-chaos-skills",
       script_name="inject_io_hang.py",
       params="--node <节点名> --path <已校验的独立挂载点> --timeout 300 --kubeconfig <路径>",
       timeout=300
   )
   ```
   dm-delay 模式（爆炸半径更小，`--path` 为存放 loop 背景文件的目录），把 `params` 换成：
   ```
   --node <节点名> --path /data --mode dm-delay --delay-ms 600000 --size-mb 512 \
     --timeout 300 --kubeconfig <路径>
   ```
4. 观察应用 A 的写入行为、Pod 就绪状态与相关告警

**注入验证**：
1. 脚本返回 `"status": "success"`，并记录 `scenario_id`（恢复与状态查询都要用到）
2. 写探针确认 IO 已挂起（同样经 `execute_skill_script`，`params` 为）：
   ```
   --action status --node <节点名> --path <同注入> --mode <同注入> --kubeconfig <路径>
   ```
   期望 `write_probe.hanging == true`（`probe_exit=124`，即 `dd` 被 `timeout` 打断）
3. 确认应用侧现象：
   - `kubectl exec <pod> -- sh -c 'timeout 5 dd if=/dev/zero of=<路径>/probe bs=4k count=1 conv=fsync; echo $?'` 返回非 0
   - `kubectl get pod <pod>` 观察就绪状态变化；`kubectl logs <pod> --tail=20` 观察日志停止推进
   - 进入 debug pod 执行 `ps -eo stat,pid,comm | grep '^D'`，确认应用进程处于 D 状态
4. **与 burn 的区分**：`iostat -xd 1 3` 的 `%util` 不一定升高，`iostat -c 1 3` 的 `%iowait` 会升高。
   仅凭 `%util` 判断会误判为「注入失败」

**注入恢复**：
1. 执行脚本恢复（`params` 为）：
   ```
   --action recover --node <节点名> --path <同注入> --mode <同注入> --kubeconfig <路径>
   ```
2. 若脚本不可用，可手动恢复（通过 debug pod 进入宿主机）：
   - fsfreeze 模式：`nsenter -t 1 -m -- fsfreeze -u <挂载点>`
   - dm-delay 模式：执行 `/run/chaos-io-hang/<scenario_id>.recover.sh`
3. `--timeout` 看门狗是最后一道保险：到点会自动执行同一个恢复脚本。**不要**用 `--timeout 0`
   关闭看门狗，除非演练全程有人值守

**恢复验证**：
1. `--action status` 返回 `write_probe.hanging == false`（`probe_exit=0`）
2. 应用 A 的写入耗时回到基线（用注入验证里的 `dd` 探针对比）
3. Pod 恢复 Ready，日志继续推进，D 状态进程消失
4. dm-delay 模式额外确认残留已清理：`dmsetup ls | grep chaos-io-hang` 为空、
   `losetup -a | grep chaos-io-hang` 为空、背景文件 `.<scenario_id>.img` 已删除

**基准事实**：
- **根因**：底层存储后端（云盘、网络存储、多路径链路）无响应，块层 IO 请求长期不完成，
  上层应用阻塞在不可中断的 IO 等待中
- **必现现象**：目标文件系统写 IO 与 fsync 阻塞；应用进程进入 D 状态；`%iowait` 升高而
  `%util` 不一定升高；依赖该路径的应用健康检查超时

**安全红线补充**：
- 只在隔离命名空间 / 测试集群执行；脚本会在目标节点创建 privileged + hostPID Pod
- 冻结的是**整个文件系统**，同一挂载点上的其它 Pod 会一起受影响，注入前必须确认共用情况
- 严禁对承载 etcd、kube-apiserver、kubelet、容器运行时的文件系统注入
- 始终保留 `--timeout` 看门狗；注入后第一件事是记录 `scenario_id`
