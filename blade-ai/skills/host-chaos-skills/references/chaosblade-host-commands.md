# ChaosBlade 主机级命令参考

> 本文件整理自一台装有 `blade` CLI v1.9.0 的机器上的 `--help` 输出，作为主机故障注入的命令参考。
>
> **这是出处说明，不是对你环境的断言。** 目标机器上的版本可能不同，可用 target / action 也可能
> 不同。命令报「unknown command」之类的错时，先用 `blade create --help` 或
> `blade create <target> --help` 看该版本实际提供什么，再据此调整——不要因为本文件写了就认为一定有，
> 也不要因为本文件没写就认为一定没有。

## 通用参数

所有主机级命令均支持以下通用参数：

| 参数 | 说明 |
|------|------|
| `--timeout <seconds>` | 实验超时自动恢复时间（强烈建议始终设置） |
| `--channel ssh` | 通过 SSH 远程执行 |
| `--ssh-host <IP>` | SSH 目标主机 |
| `--ssh-user <user>` | SSH 用户名 |
| `--ssh-key <path>` | SSH 私钥路径 |
| `--ssh-port <port>` | SSH 端口（默认 22） |

## CPU

### `blade create cpu fullload`

CPU 满载实验。

| 参数 | 说明 |
|------|------|
| `--cpu-percent <0-100>` | CPU 使用率百分比 |
| `--cpu-count <N>` | 指定占用的 CPU 核心数 |
| `--cpu-list <0,1,3>` 或 `<1-3>` | 指定占用的 CPU 核心索引 |
| `--climb-time <seconds>` | 爬坡时间（秒），逐步升至目标负载 |

示例：
```bash
blade create cpu fullload --cpu-percent 80 --timeout 60
blade create cpu fullload --cpu-percent 90 --cpu-count 2 --timeout 120
blade create cpu fullload --cpu-list 0,1 --timeout 60
```

## 内存

### `blade create mem load`

内存压力实验。

| 参数 | 说明 |
|------|------|
| `--mode <ram\|cache>` | 内存占用模式：ram（直接内存）或 cache（缓存） |
| `--mem-percent <0-100>` | 内存使用率百分比 |
| `--reserve <MB>` | 保留内存大小（MB），与 mem-percent 互斥时 mem-percent 优先 |
| `--rate <MB/s>` | 内存占用速率（仅 ram 模式） |
| `--include-buffer-cache` | ram 模式下统计包含 buffer/cache |
| `--avoid-being-killed` | 防止被 OOM Killer 杀死 |

示例：
```bash
blade create mem load --mode ram --mem-percent 80 --timeout 60
blade create mem load --mode cache --mem-percent 70 --timeout 120
blade create mem load --mode ram --mem-percent 90 --avoid-being-killed --timeout 60
```

## 磁盘

### `blade create disk fill`

磁盘空间填充实验。

| 参数 | 说明 |
|------|------|
| `--path <dir>` | 填充目标目录（默认 /） |
| `--size <MB>` | 填充大小（MB） |
| `--percent <0-100>` | 填充至目标路径所在分区的百分比（优先级最高） |
| `--reserve <MB>` | 保留空间（MB），优先级：percent > reserve > size |
| `--retain-handle` | 保留文件句柄（删除文件后仍占用空间） |

示例：
```bash
blade create disk fill --path /home --percent 90 --timeout 120
blade create disk fill --path /data --size 10240 --timeout 60
blade create disk fill --path /var/log --reserve 512 --timeout 120
```

### `blade create disk burn`

磁盘 IO 高负载实验。

| 参数 | 说明 |
|------|------|
| `--path <dir>` | IO 操作目标目录（默认 /） |
| `--read` | 注入读 IO 负载 |
| `--write` | 注入写 IO 负载 |
| `--size <MB>` | 块大小（MB），默认 10 |

示例：
```bash
blade create disk burn --read --write --path /data --timeout 60
blade create disk burn --write --path /home --size 20 --timeout 120
```

## 网络

### `blade create network dns`

DNS 劫持实验。

| 参数 | 说明 |
|------|------|
| `--domain <域名>` | 目标域名（必填） |
| `--ip <IP>` | 劫持到的 IP 地址（必填） |
| `--replace` | 如果域名已有解析是否替换 |

示例：
```bash
blade create network dns --domain payment-api.svc.internal --ip 10.96.0.253 --timeout 60
```

### `blade create network drop`

网络丢包/隔离实验。

| 参数 | 说明 |
|------|------|
| `--source-ip <IP>` | 源 IP 过滤 |
| `--destination-ip <IP>` | 目的 IP 过滤 |
| `--source-port <port>` | 源端口过滤 |
| `--destination-port <port>` | 目的端口过滤（支持逗号分隔多端口） |
| `--network-traffic <in\|out>` | 流量方向 |
| `--string-pattern <string>` | 包含指定字符串的包 |

示例：
```bash
blade create network drop --destination-ip 10.0.0.5 --network-traffic out --timeout 60
blade create network drop --destination-port 3306 --network-traffic out --timeout 120
blade create network drop --string-pattern "mysql-primary.db.internal" --network-traffic out --timeout 60
```

### `blade create network occupy`

端口占用实验。

| 参数 | 说明 |
|------|------|
| `--port <端口>` | 目标端口（必填） |
| `--force` | 强制杀死正在使用该端口的进程 |

示例：
```bash
blade create network occupy --port 8080 --force --timeout 60
```

## 进程

### `blade create process kill`

杀死进程实验。

| 参数 | 说明 |
|------|------|
| `--process <name>` | 进程名（包含该关键词的进程） |
| `--process-cmd <cmd>` | 进程命令名 |
| `--pid <pid>` | 进程 PID |
| `--local-port <port>` | 按本地端口匹配进程 |
| `--signal <9\|15>` | 杀死信号（默认 9） |
| `--count <N>` | 限制杀死次数，0 为无限 |
| `--exclude-process <name>` | 排除的进程 |
| `--ignore-not-found` | 进程不存在时不报错 |

示例：
```bash
blade create process kill --process nginx --signal 9 --timeout 60
blade create process kill --local-port 8080 --signal 15 --timeout 60
```

### `blade create process stop`

进程假死/挂起实验（SIGSTOP）。

| 参数 | 说明 |
|------|------|
| `--process <name>` | 进程名 |
| `--process-cmd <cmd>` | 进程命令名 |
| `--pid <pid>` | 进程 PID |
| `--local-port <port>` | 按本地端口匹配 |
| `--exclude-process <name>` | 排除的进程 |
| `--ignore-not-found` | 进程不存在时不报错 |

示例：
```bash
blade create process stop --process java --timeout 30
blade create process stop --local-port 3306 --timeout 60
```

### `blade create process load`

进程数飙升实验（创建大量进程）。

| 参数 | 说明 |
|------|------|
| `--count <N>` | 创建的进程数（正整数，0 或不设为无限） |
| `--user <username>` | 以指定用户身份创建进程 |

示例：
```bash
blade create process load --count 500 --timeout 60
blade create process load --count 1000 --user nobody --timeout 120
```

## 文件系统

### `blade create file append`

文件内容追加实验。

| 参数 | 说明 |
|------|------|
| `--filepath <path>` | 目标文件路径（必填） |
| `--content <text>` | 追加内容（必填） |
| `--count <N>` | 追加次数（默认 1） |
| `--interval <seconds>` | 追加间隔（秒） |
| `--enable-base64` | 内容使用 base64 编码 |
| `--enable-backup` | 启用备份（destroy 时恢复原文件） |

示例：
```bash
blade create file append --filepath /var/log/app.log --content "ERROR OOM" --count 10000 --interval 1 --timeout 60
```

### `blade create file chmod`

文件权限篡改实验。

| 参数 | 说明 |
|------|------|
| `--filepath <path>` | 目标文件路径（必填） |
| `--mark <mode>` | 权限值如 000、777（必填） |

示例：
```bash
blade create file chmod --filepath /etc/nginx/nginx.conf --mark 000 --timeout 60
```

### `blade create file delete`

文件删除实验。

| 参数 | 说明 |
|------|------|
| `--filepath <path>` | 目标文件路径（必填） |
| `--force` | 强制删除（不可恢复） |

示例：
```bash
blade create file delete --filepath /tmp/test.dat --timeout 60
```

### `blade create file load`

文件句柄耗尽实验。

| 参数 | 说明 |
|------|------|
| `--filepath <path>` | 目标文件路径（必填） |
| `--count <N>` | 打开次数（0 或不设为无限） |
| `--force` | 强制达到句柄上限（不可自动恢复） |

示例：
```bash
blade create file load --filepath /var/log/app.log --count 50000 --timeout 60
```

### `blade create file move`

文件移动实验。

| 参数 | 说明 |
|------|------|
| `--filepath <path>` | 源文件路径（必填） |
| `--target <dir>` | 目标目录（必填） |
| `--force` | 覆盖目标文件 |
| `--auto-create-dir` | 自动创建不存在的目录 |

示例：
```bash
blade create file move --filepath /etc/app/config.yaml --target /tmp --timeout 60
```

## 脚本

### `blade create script delay`

脚本函数延迟注入。

| 参数 | 说明 |
|------|------|
| `--file <path>` | 脚本文件路径（必填） |
| `--function-name <name>` | 目标函数名（必填） |
| `--time <ms>` | 延迟时间（毫秒，必填） |

示例：
```bash
blade create script delay --file /opt/app/start.sh --function-name main --time 10000 --timeout 120
```

### `blade create script exit`

脚本函数异常退出注入。

| 参数 | 说明 |
|------|------|
| `--file <path>` | 脚本文件路径（必填） |
| `--function-name <name>` | 目标函数名（必填） |
| `--exit-code <code>` | 退出码 |
| `--exit-message <msg>` | 退出消息 |

示例：
```bash
blade create script exit --file /opt/app/deploy.sh --function-name deploy --exit-code 1 --exit-message "deploy failed" --timeout 60
```

## 时间

### `blade create time travel`

系统时间偏移实验。

| 参数 | 说明 |
|------|------|
| `--offset <duration>` | 时间偏移量（如 5m30s、-2h30m） |
| `--disableNtp <true\|false>` | 是否禁用 NTP（默认 true） |

示例：
```bash
blade create time travel --offset 5m --timeout 120
blade create time travel --offset -2h30m --timeout 300
blade create time travel --offset 1h --disableNtp false --timeout 120
```

## 系统服务

### `blade create systemd stop`

停止 Systemd 服务。

| 参数 | 说明 |
|------|------|
| `--service <name>` | 服务名（必填） |
| `--ignore-not-found` | 服务不存在时不报错 |

示例：
```bash
blade create systemd stop --service nginx --timeout 120
blade create systemd stop --service docker --timeout 300
```

## 系统调用

### `blade create strace delay`

系统调用延迟注入。

| 参数 | 说明 |
|------|------|
| `--pid <pid>` | 目标进程 PID（必填） |
| `--syscall-name <name>` | 目标系统调用名（必填） |
| `--time <duration>` | 延迟时间（支持 s/ms/us/ns 单位，必填） |
| `--delay-loc <enter\|exit>` | 延迟位置：系统调用执行前或执行后（必填） |
| `--first <N>` | 仅注入前 N 次调用 |
| `--end <N>` | 仅注入最后 N 次调用 |
| `--step <N>` | 间隔 N 次注入一次 |

示例：
```bash
blade create strace delay --pid 1234 --syscall-name read --time 100ms --delay-loc enter --timeout 60
blade create strace delay --pid 5678 --syscall-name write --time 500ms --delay-loc exit --first 10 --timeout 60
```

### `blade create strace error`

系统调用返回值篡改。

| 参数 | 说明 |
|------|------|
| `--pid <pid>` | 目标进程 PID（必填） |
| `--syscall-name <name>` | 目标系统调用名（必填） |
| `--return-value <value>` | 篡改后的返回值（必填） |
| `--first <N>` | 仅注入前 N 次调用 |
| `--end <N>` | 仅注入最后 N 次调用 |
| `--step <N>` | 间隔 N 次注入一次 |

示例：
```bash
blade create strace error --pid 1234 --syscall-name open --return-value -1 --timeout 60
blade create strace error --pid 5678 --syscall-name mmap --return-value -12 --first 5 --timeout 60
```
