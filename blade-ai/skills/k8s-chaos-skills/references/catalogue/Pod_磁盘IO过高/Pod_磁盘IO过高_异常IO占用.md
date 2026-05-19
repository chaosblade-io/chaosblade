**用例名称** 异常IO占用 导致 Pod_磁盘IO过高

**故障现象**：
1. Pod 容器内磁盘 IO 使用率持续过高，读写延迟增大
2. 应用响应变慢或发生超时
3. 可能影响同节点上其他 Pod 的磁盘 IO 性能
4. df -h 显示磁盘使用率明显上升（写入路径下临时文件占用）

**资源准备**：
1. 确认目标 Pod 已正常运行
2. 确认目标路径在 Pod 容器内可用且可写（常用：/tmp、/）
3. 建议提前采集 baseline：`kubectl exec <pod> -n <ns> -- df -h` 和 `du -sh /tmp`

**演练步骤**：
1. 定位应用的目标 Pod
2. 使用 ChaosBlade 对目标 Pod 注入磁盘 IO 负载：
   - **读 IO**：使用 `--read` 标志，ChaosBlade 会创建一个 600MB 文件进行持续读取
   - **写 IO**：使用 `--write` 标志，ChaosBlade 会创建 `--size * 100` MB 的文件进行持续写入（默认 size=10MB，即写入 ~1000MB）
   - **读写混合**：同时使用 `--read --write` 标志
   - **注意**：`pod-disk burn` 仅支持 `--size` 参数调节写入量，迭代次数硬编码为 100，**不支持 `--count` 参数**
   - 命令示例：`blade create k8s pod-disk burn --names <pod> --namespace <ns> --path /tmp --size 100 --read --write --timeout 600 --kubeconfig <path>`
3. 观察 Pod 磁盘 IO 变化及应用的响应情况

**注入验证**：
1. **关键差异**：`pod-disk burn` 与 `pod-disk fill` 不同 — burn 创建的 I/O 文件是**临时的**，实验完成（或 destroy）后会自动清理。只能用间接证据验证
2. 进入目标 Pod，使用 `df -h` 查看磁盘使用率，与 baseline 对比：
   - 使用率有 1-2GB+ 增长 → 间接证据 burn 已发生（即使文件已清理）
   - 使用率无明显变化 → 需要进一步检查 ls、Events 和**容器重启记录**
3. 使用 `ls -lah <path>/` 检查目标路径下是否有 burn 残留文件
4. 查看 Pod Events：`kubectl describe pod <pod> -n <ns>` 确认：
   - 无 Evicted/CrashLoopBackOff — 正常
   - **⚠ 容器 OOMKilled 或重启记录（Restart Count > 基线）** → 即使 burn 文件不在、df 无变化，也可能是容器重启销毁了证据
5. **验证结论选择**：
   - 文件还在且 df 显示增加 → `passed`
   - 文件已清理但 df 显示显著增加 → `recovered_before_observation`
   - L1 确认 burn 成功 + 容器近期有 OOMKilled/重启 + df 无变化 → `recovered_before_observation`（证据被容器重启销毁，burn 实际已执行）
   - 文件不在 + df 无变化 + 容器无重启记录 + L1 状态确认 burn 可能未执行 → `failed`

**⚠ OOM Kill 交互场景**（重要）：
- `pod-disk burn` 在内存受限的 Pod 上执行时，I/O 写入产生的内存压力可能触发 OOMKill
- OOMKill 会导致容器重建，burn 临时文件被彻底清除，磁盘使用率回到 baseline
- **验证策略**：检查 `kubectl describe pod` 中的 Restart Count 和 OOMKilling 事件时间是否在注入时间窗口内
- 若注入时间窗口内发生容器重启 → 这不是"burn 未执行"，而是"burn 证据被容器重启销毁" → 应判定为 `recovered_before_observation`

> **注意**：如果注入后容器重启，重启前的 df -h baseline 对比无意义（容器重建后 overlay 是全新的）。应以"L1 确认 burn 成功 + 容器重启时间在注入窗口内"作为判定 `recovered_before_observation` 的充分条件。

**注入恢复**：
1. 销毁 ChaosBlade 磁盘 burn 实验：`blade destroy <uid>`
2. burn 临时文件会在实验销毁时自动清理，无需手动删除

**恢复验证**：
1. 使用 `df -h` 查看 Pod 磁盘使用率，确认恢复到 baseline 水平
2. 确认应用磁盘读写延迟恢复正常
3. 确认 Pod 状态为 Running，无异常 Events

**基准事实**：
- **根因**：应用或系统存在异常进程大量执行磁盘 I/O，导致 Pod 磁盘 IO 负载过高，影响同 Pod 内其他进程及同节点上其他 Pod 的磁盘读写性能
- **必现现象**：Pod 磁盘 IO 使用率升高，应用响应变慢或超时；df -h 显示磁盘使用率明显增加；ls 可见临时文件（实验运行期间）
- **参数限制**：`pod-disk burn` 仅支持 `--size` 调节写入块大小，迭代次数硬编码为 100，**不支持 `--count` 参数**
- **瞬态特性**：burn 创建的 I/O 文件在实验完成后自动清理，属于**瞬态故障**（transient fault），验证时需使用间接证据（df -h baseline 对比），而非要求文件必须仍存在

---
