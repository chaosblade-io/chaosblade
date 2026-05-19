**用例名称** Finalizers未清理 导致 Pod_Terminating

**故障现象**：
1. Pod 状态一直停留在 Terminating，无法完成删除
2. Pod 的 metadata 中存在 finalizers 字段，等待外部控制器清理

**资源准备**：
1. 确认应用 A/B 已正常运行
2. 为应用 B 的 Pod 添加 finalizers（通过编辑 Pod 定义或创建带 finalizers 的 Pod）

**演练步骤**：
1. 定位应用 B 的 Pod
2. 编辑 Pod 定义，在 metadata.finalizers 中添加一个自定义标识（模拟外部控制器创建的 Pod）
3. 删除应用 B 的 Pod，触发 Pod 终止流程
4. 观察 Pod 状态变化

**注入验证**：
1. 查看 Pod 状态，确认停留在 Terminating 状态
2. 查看 Pod 详情，确认 metadata 中存在 finalizers 字段
3. 确认没有控制器在处理该 finalizer

**注入恢复**：
1. 使用 kubectl edit 编辑 Pod 定义
2. 手动删除 finalizers 字段中的内容
3. 保存后 Pod 将被自动删除

**恢复验证**：
1. 查看 Pod 状态，确认已从集群中删除
2. 确认 finalizers 字段已清空

**基准事实**：
- **根因**：Pod 的 metadata 中存在 finalizers，表示该资源被删除时需要由创建资源的程序来做删除前的清理，清理完成后需要将标识从 finalizers 中移除才能最终删除资源
- **必现现象**：Pod 状态为 Terminating，metadata.finalizers 不为空
