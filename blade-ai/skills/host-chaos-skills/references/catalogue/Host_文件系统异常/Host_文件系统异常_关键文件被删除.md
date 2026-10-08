**用例名称** 关键文件被删除 导致 Host_文件系统异常

## 故障现象
1. 关键文件（配置文件/数据文件/日志文件）被意外删除
2. 应用启动失败或运行异常
3. 数据丢失

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标文件路径：`ls -la <filepath>`
3. 确认该文件有备份或可重新生成

## 演练步骤
1. 记录目标文件信息：`ls -la <filepath>` 和 `md5sum <filepath>`
2. 使用 ChaosBlade 注入文件删除

```bash
blade create file delete --filepath <target-file> --timeout <duration>
```

参数说明：
- `--filepath`：目标文件路径（必填）
- `--force`：可选，强制删除（不可恢复，演练中不建议使用）
- `--timeout`：超时自动恢复（秒），非 force 模式下 destroy 可恢复文件

3. 观察应用对文件缺失的反应

## 注入验证
1. `ls <filepath>` 确认文件不存在
2. 观察应用日志中的 "No such file or directory" 错误
3. 确认应用行为变化（如无法启动、功能异常）

## 注入恢复
```bash
blade destroy <experiment-uid>
```

> 注意：非 --force 模式下，destroy 会恢复被删除的文件

## 恢复验证
1. `ls -la <filepath>` 确认文件恢复
2. `md5sum <filepath>` 确认内容完整性
3. 确认应用恢复正常运行

## 基准事实
- **根因**：关键文件被意外删除（人为误操作、恶意攻击等），导致应用无法正常运行
- **必现现象**：文件不存在；应用报 No such file or directory；服务异常或无法启动

---

## 降级方案（原生命令）

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

注入命令（**先武装定时恢复，再注入**）：
```bash
# 先武装定时移回（timer 由宿主机 systemd(PID 1) 管理，到期自动还原），
# 再移走文件。移走而非删除：原文件即备份，同文件系统内为原子操作，不存在
# 「备份成功但删除失败」或「备份失败却已删除」的中间态。
# 两条命令分两次独立执行（执行通道不支持 && 串联）；武装成功（输出含 Running timer as unit）后再执行移走
# <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-filedel \
  mv <filepath>.orig <filepath>
mv <filepath> <filepath>.orig
```

恢复命令（timer 到期前可提前手动恢复）：
```bash
# 提前恢复：移回原路径，不留残留备份文件（同时停掉已武装的 timer）
systemctl stop blade-restore-filedel.timer 2>/dev/null
mv <filepath>.orig <filepath>
```

注意事项：
- 操作前必须备份，否则数据不可恢复
- 自恢复基于 systemd-run transient timer 到期自动 `mv` 回原路径，补齐了 ChaosBlade `--timeout` 的自恢复能力；武装与移走分两次独立执行，须确认武装成功（输出含 `Running timer as unit`）后再执行移走（等价于 && 串联的失败短路保证）
- 演练环境不要对无法重建的唯一数据文件做此操作
- 同名 transient timer 重复武装会报 `Unit blade-restore-filedel.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop blade-restore-filedel.service; systemctl reset-failed blade-restore-filedel.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
