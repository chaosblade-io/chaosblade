# Chaosblade K8s 命令速查表

本文件提供 Chaosblade 在 Kubernetes 环境下常用故障注入命令的速查，减少执行过程中反复 `-h` 查看帮助。

---

## 通用语法

```bash
blade create k8s <scope>-<target> <action> [flags]
```

- `<scope>`: 作用范围，可选 `pod`、`node`、`container`
- `<target>`: 故障目标，如 `cpu`、`network`、`disk`、`process`、`pod`(仅Pod级别)
- `<action>`: 故障动作，如 `fullload`、`drop`、`dns`、`occupy`、`fill`、`kill`

## Action 对照表（scope-target → 可用 action）

| scope-target | 可用 action | 说明 |
|---|---|---|
| pod-cpu | `fullload` | CPU 满载 |
| pod-mem | `load` | 内存占用 |
| pod-network | `dns`, `drop`, `occupy` | 网络故障 (v1.8.0) |
| pod-disk | `fill`, `burn` | 磁盘填充 / IO 负载 |
| pod-process | `kill`, `stop` | 进程操作 |
| pod-pod | `delete` | Pod 删除 |
| pod-IO | `delay`, `errno` | 文件系统 IO 故障 |
| node-cpu | `fullload` | CPU 满载 |
| node-mem | `load` | 内存占用 |
| node-network | `drop` | 网络故障 (v1.8.0: `delay`/`loss` → `drop`) |
| node-disk | `fill`, `burn` | 磁盘填充 / IO 负载 (**无 fullload**) |
| node-process | `kill`, `stop` | 进程操作 |
| container-cpu | `fullload` | CPU 满载 |
| container-network | `drop` | 网络故障 (v1.8.0: `delay`/`loss` → `drop`) |
| container-process | `kill`, `stop` | 进程操作 |
| container-container | `remove` | 容器删除 |

> **⚠️ 常见错误警示**：
> - `node-disk` **没有** `fullload` action！磁盘 IO 负载使用 `burn`，磁盘空间填充使用 `fill`
> - `pod-disk` 同理，只有 `fill` 和 `burn`，没有 `fullload`
> - `pod-mem` 的 action 是 `load`，不是 `fullload`
> - `node-mem` 的 action 是 `load`，不是 `fullload`
> - 只有 `cpu` target 才有 `fullload` action
> - **注入前务必确认 scope-target-action 组合在上表中存在**，避免因 action 不存在导致注入失败

## 通用 K8s Flags

以下参数适用于所有 `blade create k8s` 命令：

| Flag | 说明 | 示例 |
|------|------|------|
| `--namespace` | 目标命名空间 | `--namespace default` |
| `--labels` | 按标签筛选资源 | `--labels "app=nginx"` |
| `--names` | 按名称指定资源（逗号分隔） | `--names node-1,node-2` |
| `--kubeconfig` | kubeconfig 文件路径 | `--kubeconfig ~/.kube/config` |
| `--evict-count` | 限制影响的资源数量 | `--evict-count 1` |
| `--evict-percent` | 限制影响的资源百分比 | `--evict-percent 50` |
| `--waiting-time` | 等待结果的超时时间 | `--waiting-time 30s` |
| `--timeout` | 实验自动过期时间（秒） | `--timeout 600` |

---

## 一、Pod 级别故障

### 1. Pod CPU 满载

```bash
blade create k8s pod-cpu fullload \
  --namespace <ns> \
  --labels "app=<app>" \
  --cpu-percent <0-100> \
  --cpu-count <核数> \
  --kubeconfig ~/.kube/config
```

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--cpu-percent` | CPU 使用率百分比 | 100 |
| `--cpu-count` | 指定满载的 CPU 核数（不指定则全部） | 全部 |
| `--cpu-list` | 指定满载的 CPU 核编号（如 0,1,2） | - |
| `--timeout` | 自动恢复时间（秒） | - |

### 2. Pod 内存压力

```bash
blade create k8s pod-mem load \
  --namespace <ns> \
  --labels "app=<app>" \
  --mode ram \
  --mem-percent <0-100> \
  --kubeconfig ~/.kube/config
```

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--mode` | 内存占用模式：`ram`(物理内存) / `cache` | ram |
| `--mem-percent` | 内存使用率百分比 | - |
| `--reserve` | 保留的内存大小（如 `200m`） | - |
| `--rate` | 内存增长速率（每秒MB） | - |

### 3. Pod 网络延迟

> **⚠️ v1.8.0 不可用**：`pod-network delay` 在 blade v1.8.0 中不存在（仅 `dns`/`drop`/`occupy`）。如需延迟效果，使用 Tier 2 kubectl-native 方案（tc qdisc）。以下为旧版参考：

```bash
blade create k8s pod-network delay \
  --namespace <ns> \
  --labels "app=<app>" \
  --time <毫秒> \
  --interface eth0 \
  --kubeconfig ~/.kube/config
```

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--time` | 延迟时间（毫秒） | - |
| `--offset` | 延迟波动范围（毫秒） | - |
| `--interface` | 网络接口 | eth0 |
| `--local-port` | 本地端口（逗号分隔） | 全部 |
| `--remote-port` | 远程端口（逗号分隔） | 全部 |
| `--destination-ip` | 目标 IP（逗号分隔） | 全部 |
| `--exclude-port` | 排除端口 | - |
| `--exclude-ip` | 排除 IP | - |

### 4. Pod 网络丢包

```bash
blade create k8s pod-network drop \
  --namespace <ns> \
  --labels "app=<app>" \
  --interface eth0 \
  --kubeconfig ~/.kube/config
```

> **⚠️ drop 是全量丢包**（iptables DROP 语义），**不支持 `--percent`**。不带端口/IP 过滤时丢弃该接口的所有流量。

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--interface` | 网络接口 | eth0 |
| `--local-port` | 本地端口 | 全部 |
| `--remote-port` | 远程端口 | 全部 |
| `--destination-ip` | 目标 IP | 全部 |

> **爆炸半径控制提示**：drop 默认丢弃全部流量，**必须**使用 `--local-port` 或 `--remote-port` 或 `--destination-ip` 限制影响范围，避免丢弃 DNS、监控等非目标流量。仅在测试完全网络分区时使用不加过滤的全接口注入。

### 5. Pod DNS 故障

```bash
blade create k8s pod-network dns \
  --namespace <ns> \
  --labels "app=<app>" \
  --domain <域名> \
  --ip <伪造IP> \
  --kubeconfig ~/.kube/config
```

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--domain` | 目标域名 | - |
| `--ip` | 映射的伪造 IP | - |

### 6. Pod 网络包损坏

```bash
blade create k8s pod-network corrupt \
  --namespace <ns> \
  --labels "app=<app>" \
  --percent <0-100> \
  --interface eth0 \
  --kubeconfig ~/.kube/config
```

### 7. Pod 网络包重复

```bash
blade create k8s pod-network duplicate \
  --namespace <ns> \
  --labels "app=<app>" \
  --percent <0-100> \
  --interface eth0 \
  --kubeconfig ~/.kube/config
```

### 8. Pod 磁盘填充

```bash
blade create k8s pod-disk fill \
  --namespace <ns> \
  --labels "app=<app>" \
  --path <目录> \
  --size <大小> \
  --kubeconfig ~/.kube/config
```

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--path` | 填充的目标目录 | / |
| `--size` | 填充大小（如 `1024m`、`10g`） | - |
| `--percent` | 填充到磁盘使用率百分比 | - |
| `--retain-handle` | 保留文件句柄（删除文件后不释放空间） | false |

### 9. Pod 磁盘 IO 读写负载

```bash
blade create k8s pod-disk burn \
  --namespace <ns> \
  --labels "app=<app>" \
  --path <目录> \
  --read \
  --write \
  --kubeconfig ~/.kube/config
```

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--path` | IO 负载目标目录 | / |
| `--read` | 读 IO 负载 | false |
| `--write` | 写 IO 负载 | false |
| `--size` | 每次 IO 块大小（如 `10m`） | - |
| `--count` | IO 次数 | - |

### 10. Pod 文件系统 IO 故障

```bash
blade create k8s pod-IO delay \
  --namespace <ns> \
  --labels "app=<app>" \
  --time <毫秒> \
  --path <目录> \
  --kubeconfig ~/.kube/config
```

| Action | 说明 |
|--------|------|
| `delay` | 文件系统 IO 延迟 |
| `errno` | 文件系统 IO 返回错误 |

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--time` | IO 延迟（毫秒），仅 `delay` | - |
| `--errno` | 返回的错误码，仅 `errno` | 28 (ENOSPC) |
| `--path` | 影响的文件路径 | - |
| `--methods` | 拦截的系统调用（如 read,write） | 全部 |
| `--percent` | 故障生效百分比 | 100 |

### 11. Pod 进程操作

```bash
blade create k8s pod-process kill \
  --namespace <ns> \
  --labels "app=<app>" \
  --process <进程名> \
  --kubeconfig ~/.kube/config
```

| Action | 说明 |
|--------|------|
| `kill` | 杀掉进程 |
| `stop` | 挂起进程（SIGSTOP） |

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--process` | 进程名 | - |
| `--process-cmd` | 进程命令行关键词 | - |
| `--signal` | 发送的信号（仅 kill） | 9 |
| `--count` | 限制影响的进程数 | - |

### 12. Pod 删除

```bash
blade create k8s pod-pod delete \
  --namespace <ns> \
  --labels "app=<app>" \
  --kubeconfig ~/.kube/config
```

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--labels` | 标签筛选 | - |
| `--names` | 指定 Pod 名称 | - |
| `--evict-count` | 删除的 Pod 数量 | - |
| `--evict-percent` | 删除的 Pod 百分比 | - |

---

## 二、Node 级别故障

### 1. 节点 CPU 满载

```bash
blade create k8s node-cpu fullload \
  --names <节点名> \
  --cpu-percent <0-100> \
  --kubeconfig ~/.kube/config
```

Flags 与 Pod CPU 满载一致（`--cpu-percent`、`--cpu-count`、`--cpu-list`）。

### 2. 节点内存压力

```bash
blade create k8s node-mem load \
  --names <节点名> \
  --mode ram \
  --mem-percent <0-100> \
  --kubeconfig ~/.kube/config
```

Flags 与 Pod 内存压力一致（`--mode`、`--mem-percent`、`--reserve`、`--rate`）。

### 3. 节点网络延迟

> **⚠️ v1.8.0 不可用**：`node-network delay` 在 blade v1.8.0 中不存在。以下为旧版参考：

```bash
blade create k8s node-network delay \
  --names <节点名> \
  --time <毫秒> \
  --interface eth0 \
  --kubeconfig ~/.kube/config
```

### 4. 节点网络丢包

```bash
blade create k8s node-network drop \
  --names <节点名> \
  --interface eth0 \
  --kubeconfig ~/.kube/config
```

> **⚠️ 同 Pod drop：全量丢包，不支持 `--percent`。** 用 `--local-port`/`--destination-ip` 缩小范围。

Node 网络 Flags 与 Pod 网络一致（`--interface`、`--local-port`、`--remote-port`、`--destination-ip` 等）。

### 5. 节点磁盘填充

```bash
blade create k8s node-disk fill \
  --names <节点名> \
  --path <目录> \
  --size <大小> \
  --kubeconfig ~/.kube/config
```

| Flag | 说明 | 示例 |
|------|------|------|
| `--path` | 填充目标目录 | `/var/lib/docker` |
| `--size` | 填充大小 | `50g` |
| `--percent` | 填充到磁盘使用率 | `95` |

### 6. 节点磁盘 IO 负载

```bash
blade create k8s node-disk burn \
  --names <节点名> \
  --read \
  --write \
  --path <目录> \
  --kubeconfig ~/.kube/config
```

| Flag | 说明 | 默认值 |
|------|------|--------|
| `--read` | 启用读 IO 负载 | false |
| `--write` | 启用写 IO 负载 | false |
| `--path` | IO 负载目标目录 | / |
| `--size` | 每次 IO 块大小（如 `10m`） | - |
| `--count` | IO 次数 | - |

> **建议**：生产环境务必指定 `--path` 为非根分区目录（如 `/data`），避免影响根分区系统文件。

### 7. 节点进程操作

```bash
blade create k8s node-process kill \
  --names <节点名> \
  --process <进程名> \
  --kubeconfig ~/.kube/config
```

Actions 和 Flags 与 Pod 进程操作一致。

---

## 三、Container 级别故障

Container 级别命令在 Pod 内指定特定容器执行故障注入，适用于多容器 Pod（如有 sidecar 的场景）。

### 通用 Container Flag

| Flag | 说明 |
|------|------|
| `--container-names` | 目标容器名 |

### 1. 容器 CPU 满载

```bash
blade create k8s container-cpu fullload \
  --namespace <ns> \
  --labels "app=<app>" \
  --container-names <容器名> \
  --cpu-percent <0-100> \
  --kubeconfig ~/.kube/config
```

### 2. 容器网络延迟

> **⚠️ v1.8.0 不可用**：`container-network delay` 在 blade v1.8.0 中不存在。以下为旧版参考：

```bash
blade create k8s container-network delay \
  --namespace <ns> \
  --labels "app=<app>" \
  --container-names <容器名> \
  --time <毫秒> \
  --interface eth0 \
  --kubeconfig ~/.kube/config
```

### 3. 容器进程操作

```bash
blade create k8s container-process kill \
  --namespace <ns> \
  --labels "app=<app>" \
  --container-names <容器名> \
  --process <进程名> \
  --kubeconfig ~/.kube/config
```

### 4. 容器删除

```bash
blade create k8s container-container remove \
  --namespace <ns> \
  --labels "app=<app>" \
  --container-names <容器名> \
  --kubeconfig ~/.kube/config
```

---

## 四、实验管理命令

### 查看实验状态

```bash
# 查看所有进行中的实验
blade status --type create

# 查看指定实验详情
blade status <UID>
```

### 销毁实验

```bash
# 销毁指定实验
blade destroy <UID>

# 批量销毁所有实验（紧急回滚用）
blade status --type create | grep "^UID" | awk '{print $2}' | xargs -I {} blade destroy {}
```

### 版本与帮助

```bash
# 查看版本
blade version

# 查看所有支持的实验场景
blade create -h

# 查看某个场景的详细帮助
blade create k8s pod-network drop -h
```

---

## 五、常见演练场景速查

按照 catalogue 中使用频率最高的故障场景，汇总对应的 blade 命令：

### 资源压力类

| 故障场景 | 命令 |
|---------|------|
| Pod CPU 满载 | `blade create k8s pod-cpu fullload --cpu-percent 80 --namespace <ns> --labels "app=<app>"` |
| Pod 内存压力 | `blade create k8s pod-mem load --mode ram --mem-percent 90 --namespace <ns> --labels "app=<app>"` |
| 节点 CPU 满载 | `blade create k8s node-cpu fullload --cpu-percent 90 --names <node>` |
| 节点内存压力 | `blade create k8s node-mem load --mode ram --mem-percent 95 --names <node>` |

### 网络故障类

| 故障场景 | 命令 |
|---------|------|
| Pod 网络丢包(全量) | `blade create k8s pod-network drop --interface eth0 --namespace <ns> --labels "app=<app>"` |
| Pod 网络丢包(指定端口) | `blade create k8s pod-network drop --local-port 3306 --namespace <ns> --labels "app=<app>"` |
| Pod DNS 故障 | `blade create k8s pod-network dns --domain example.com --ip 1.1.1.1 --namespace <ns> --labels "app=<app>"` |
| 节点网络丢包 | `blade create k8s node-network drop --interface eth0 --names <node>` |

### 磁盘故障类

| 故障场景 | 命令 |
|---------|------|
| Pod 磁盘填充 | `blade create k8s pod-disk fill --path /data --size 10g --namespace <ns> --labels "app=<app>"` |
| 节点磁盘填充 | `blade create k8s node-disk fill --path /var/lib/docker --percent 95 --names <node>` |
| 节点磁盘 IO 负载 | `blade create k8s node-disk burn --read --write --path /data --names <node>` |

### 进程与 Pod 操作类

| 故障场景 | 命令 |
|---------|------|
| 杀 Pod 内进程 | `blade create k8s pod-process kill --process <name> --namespace <ns> --labels "app=<app>"` |
| 挂起 Pod 内进程 | `blade create k8s pod-process stop --process <name> --namespace <ns> --labels "app=<app>"` |
| 删除 Pod | `blade create k8s pod-pod delete --namespace <ns> --labels "app=<app>" --evict-count 1` |
| 杀节点进程 | `blade create k8s node-process kill --process <name> --names <node>` |

---

## 六、Operator YAML 模板

当使用 Operator 方式（`kubectl apply`）执行持续化演练时，使用以下模板：

```yaml
apiVersion: chaosblade.io/v1alpha1
kind: ChaosBlade
metadata:
  name: <experiment-name>
spec:
  experiments:
    - scope: <pod|node|container>
      target: <cpu|network|disk|process|pod|container>
      action: <action>
      desc: "<实验描述>"
      matchers:
        - name: namespace
          value: ["<namespace>"]
        - name: labels
          value: ["app=<app-name>"]
        # node 级别使用 names 而非 labels
        # - name: names
        #   value: ["<node-name>"]
      flags:
        - name: <flag-name>
          value: "<flag-value>"
```

**Operator YAML 示例 - Pod CPU 满载：**

```yaml
apiVersion: chaosblade.io/v1alpha1
kind: ChaosBlade
metadata:
  name: pod-cpu-fullload
spec:
  experiments:
    - scope: pod
      target: cpu
      action: fullload
      desc: "Pod CPU 满载 80%"
      matchers:
        - name: namespace
          value: ["default"]
        - name: labels
          value: ["app=nginx"]
      flags:
        - name: cpu-percent
          value: "80"
```

**Operator YAML 示例 - Pod 网络延迟：**

```yaml
apiVersion: chaosblade.io/v1alpha1
kind: ChaosBlade
metadata:
  name: pod-network-delay
spec:
  experiments:
    - scope: pod
      target: network
      action: delay
      desc: "Pod 网络延迟 3s"
      matchers:
        - name: namespace
          value: ["default"]
        - name: labels
          value: ["app=nginx"]
      flags:
        - name: time
          value: "3000"
        - name: interface
          value: "eth0"
```

**Operator 管理命令：**

```bash
# 创建实验
kubectl apply -f experiment.yaml

# 查看实验状态
kubectl get chaosblade <experiment-name> -o yaml

# 销毁实验
kubectl delete chaosblade <experiment-name>

# 销毁所有实验
kubectl delete chaosblade --all
```

---

## 七、kubectl exec 注入路径

当宿主机 blade CLI 不可用（未安装或版本不兼容，如报 `unknown flag` 错误）时，可通过 kubectl exec 在集群内的 tool Pod 中执行 blade 命令。

### 前提条件
- 集群中部署了 chaosblade-tool DaemonSet（如 otel-c-tool）
- tool Pod 的 ServiceAccount 有操作目标资源的权限

### 执行步骤

1. 发现可用的 tool Pod：
   ```bash
   kubectl get pods -n chaosblade -l app=otel-c-tool --kubeconfig=<path>
   ```
2. 选择 STATUS=Running 的 Pod（任意一个均可，实验是集群级别的）
3. 执行 blade 命令：
   ```bash
   kubectl exec <pod> -n chaosblade -- blade create k8s <scenario> [flags] --kubeconfig=<path>
   ```
4. 从 JSON 输出中提取 blade_uid 用于后续恢复

### 示例：Pod 网络丢包

```bash
# 1. 发现 tool Pod
kubectl get pods -n chaosblade -l app=otel-c-tool --kubeconfig=/path/to/config
# 2. 通过 tool Pod 执行 blade create
kubectl exec otel-c-tool-xxxxx -n chaosblade -- \
  blade create k8s pod-network drop \
  --namespace cms-demo \
  --labels "app=myapp" \
  --interface eth0 \
  --kubeconfig=/path/to/config
# 3. 从输出中提取 blade_uid
# {"code":200,"success":true,"result":"abc123"}
# 4. 恢复时同样通过 tool Pod 执行 blade destroy
kubectl exec otel-c-tool-xxxxx -n chaosblade -- \
  blade destroy abc123 --kubeconfig=/path/to/config
```

### 注意事项
- 不同 tool Pod 可能运行在不同节点上，但 blade 命令通过 API Server 执行，任何健康 Pod 都可操作
- tool Pod 可能因 DaemonSet 轮换而重启，恢复时需重新查找 Running 的 Pod
- kubectl exec 中的 blade 命令参数与本地 blade CLI 完全一致
