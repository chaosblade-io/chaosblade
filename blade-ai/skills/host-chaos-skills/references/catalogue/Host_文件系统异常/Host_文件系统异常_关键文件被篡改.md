**用例名称** 关键文件被篡改 导致 Host_文件系统异常

## 故障现象
1. 应用配置文件内容被修改或权限被篡改
2. 应用读取配置失败（Permission denied 或解析错误）
3. 服务行为异常或启动失败

## 资源准备
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标文件路径及当前状态

## 演练步骤
1. 记录目标文件当前状态：`ls -la <filepath>` 和 `md5sum <filepath>`
2. 使用 ChaosBlade 注入文件篡改

方式一：权限篡改（应用无法读取）
```bash
blade create file chmod --filepath <target-file> --mark 000 --timeout <duration>
```

方式二：内容追加（配置文件被注入异常内容）
```bash
blade create file append --filepath <target-file> --content "<malicious-content>" --enable-backup --timeout <duration>
```

方式三：文件移动（配置文件消失）
```bash
blade create file move --filepath <target-file> --target /tmp --timeout <duration>
```

参数说明：
- `--filepath`：目标文件路径（必填）
- `--mark`：权限值如 000（chmod 方式）
- `--content`：追加内容（append 方式）
- `--enable-backup`：启用备份，destroy 时恢复原文件
- `--target`：移动目标目录（move 方式）
- `--timeout`：超时自动恢复（秒）

3. 观察应用对文件变化的反应

## 注入验证
1. `ls -la <filepath>` 确认权限变化（chmod 方式）
2. `cat <filepath>` 确认内容变化（append 方式）
3. `ls <filepath>` 确认文件不存在（move 方式）
4. 观察应用日志中的错误信息

## 注入恢复
```bash
blade destroy <experiment-uid>
```

## 恢复验证
1. `ls -la <filepath>` 确认权限/内容/位置恢复
2. 确认应用读取配置恢复正常

## 基准事实
- **根因**：关键文件被篡改（权限、内容或位置），导致应用无法正常读取
- **必现现象**：文件状态变化；应用报 Permission denied / 解析错误 / 文件不存在

---

## 降级方案（原生命令）

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

注入命令（**先武装定时恢复，再注入**——timer 由宿主机 systemd(PID 1) 管理，到期自动还原）：
```bash
# 方式一：权限篡改 —— 先记录原权限并武装定时还原。
# 各命令独立执行（执行通道不支持 shell 变量与 && 串联）：先用只读 stat 取到
# 原权限数字，再把数值直接写入下面的 timer 命令；武装成功（输出含 Running timer as unit）后再执行 chmod 000
stat -c %a <filepath>
# <recovery-seconds> 取 prompt 下发的 recovery_timer_seconds（= duration + grace，见 SKILL.md 双数窗口契约）
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-filemode \
  chmod <stat取到的原权限数字> <filepath>
chmod 000 <filepath>

# 方式二：内容清空 —— 备份→武装→清空逐条独立执行：
# 备份失败时武装与清空都不执行，避免「无备份却已清空」的不可恢复破坏
# （引号内的 && 属 timer 载荷，到期由目标机 shell 执行，保留原样）
cp <filepath> <filepath>.bak
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-filecontent \
  sh -c 'cp <filepath>.bak <filepath> && rm -f <filepath>.bak'
truncate -s 0 <filepath>

# 方式三：文件移走（同目录，避免跨文件系统）。
# 两条命令分两次独立执行；武装成功（输出含 Running timer as unit）后再执行移走
systemd-run --on-active=<recovery-seconds>s --unit=blade-restore-filemove \
  mv <filepath>.orig <filepath>
mv <filepath> <filepath>.orig
```

恢复命令（timer 到期前可提前手动恢复，同时停掉已武装的 timer）：
```bash
# 方式一恢复：
systemctl stop blade-restore-filemode.timer 2>/dev/null
chmod <original-mode> <filepath>

# 方式二恢复（两条命令独立执行；rm -f 收尾清理演练自建的备份文件）：
systemctl stop blade-restore-filecontent.timer 2>/dev/null
cp <filepath>.bak <filepath>
rm -f <filepath>.bak

# 方式三恢复：
systemctl stop blade-restore-filemove.timer 2>/dev/null
mv <filepath>.orig <filepath>
```

注意事项：
- 操作前必须备份原文件，否则无法恢复
- 自恢复基于 systemd-run transient timer 到期自动执行逆操作，补齐了 ChaosBlade `--timeout` 的自恢复能力；武装与注入分次独立执行，须确认武装成功（输出含 `Running timer as unit`）后再执行篡改操作（等价于 `&&` 串联的失败短路保证）；方式一的原权限必须在武装前用 `stat -c %a` 取真实值固化进 timer，不可事后猜测
- 同名 transient timer 重复武装会报 `Unit blade-restore-*.service was already loaded`（上次武装命令执行失败时 unit 以 failed 状态残留所致）；重武装前先清理残留：`systemctl stop <unit>.service; systemctl reset-failed <unit>.service`（武装命令成功执行过的 unit 无残留，可直接重武装）
- chmod 000 对 root 用户无效（root 可绕过权限检查）
