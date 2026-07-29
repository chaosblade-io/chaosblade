**用例名称** 关键文件被篡改 导致 Host_文件系统异常

**故障现象**：
1. 应用配置文件内容被修改或权限被篡改
2. 应用读取配置失败（Permission denied 或解析错误）
3. 服务行为异常或启动失败

**资源准备**：
1. 确认目标主机上 ChaosBlade 已安装（`blade version`）
2. 确认目标文件路径及当前状态

**演练步骤**：
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

**注入验证**：
1. `ls -la <filepath>` 确认权限变化（chmod 方式）
2. `cat <filepath>` 确认内容变化（append 方式）
3. `ls <filepath>` 确认文件不存在（move 方式）
4. 观察应用日志中的错误信息

**注入恢复**：
```bash
blade destroy <experiment-uid>
```

**恢复验证**：
1. `ls -la <filepath>` 确认权限/内容/位置恢复
2. 确认应用读取配置恢复正常

**基准事实**：
- **根因**：关键文件被篡改（权限、内容或位置），导致应用无法正常读取
- **必现现象**：文件状态变化；应用报 Permission denied / 解析错误 / 文件不存在

---

**降级方案（原生命令）**

> 当 ChaosBlade 不可用时，可使用以下原生命令实现等效故障注入。

注入命令：
```bash
# 方式一：权限篡改
chmod 000 <filepath>

# 方式二：内容清空（先备份）—— 配置文件被清空同样触发解析失败/服务异常
cp <filepath> <filepath>.bak
truncate -s 0 <filepath>

# 方式三：文件移走（同目录，避免跨文件系统）
mv <filepath> <filepath>.chaos_bak
```

恢复命令：
```bash
# 方式一恢复：
chmod <original-mode> <filepath>

# 方式二恢复：
cp <filepath>.bak <filepath>

# 方式三恢复：
mv <filepath>.chaos_bak <filepath>
```

注意事项：
- 操作前必须备份原文件，否则无法恢复
- 无自动超时恢复机制
- chmod 000 对 root 用户无效（root 可绕过权限检查）
