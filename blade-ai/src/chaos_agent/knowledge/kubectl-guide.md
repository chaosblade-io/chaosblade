---
title: "kubectl Practical Guide"
topics:
  - kubectl commands
  - JSONPath queries
  - field selectors
  - events troubleshooting
  - resource inspection
  - debug subcommand
fault_types:
  - all
summary: "kubectl command reference: subcommand overview, JSONPath patterns, JSON output field reference, Events troubleshooting. Verification mapping migrated to fault-verification-strategies.md."
---

# kubectl 完全使用手册（故障演练专用）

本手册面向故障演练 Agent，系统讲解 kubectl 全部可用能力，帮助 Agent 在注入验证（Layer 2）和恢复验证阶段，设计出精确、可执行的 kubectl 验证方案。

---

## 一、Agent 可用的 kubectl 工具总览

Agent 拥有 1 个统一的 `kubectl` 工具，通过 `subcommand` 参数选择子命令：

| subcommand | 用途 | 典型 v_args |
|------|------|----------|
| `get` | 查询任意 K8s 资源（Pod/Node/Deploy/PVC 等），支持 -o json/yaml/wide/name/jsonpath | `"pods -n <ns> -o json"` |
| `describe` | 查看任意资源的详细描述（含 Events） | `"pod <pod> -n <ns>"` |
| `exec` | 在 Pod 容器内执行命令 | `"<pod> -n <ns> -- <command>"` |
| `patch` | 修改 K8s 资源字段（支持 --type=strategic/merge/json） | `"pod <pod> -n <ns> --type=json -p '...'`" |
| `delete` | 删除 K8s 资源（支持 `--force --grace-period=0` 强制删除） | `"pod <pod> -n <ns>"` |
| `logs` | 查看容器日志 | `"<pod> -n <ns> --tail=50"` |
| `top` | 查看实时资源指标（CPU/内存） | `"pod -n <ns>"` |
| `scale` | 改变工作负载副本数 | `"deployment <name> -n <ns> --replicas=0"` |
| `set` | 设置资源字段（image/resources/env/serviceaccount/selector） | `"image deployment/<name> -n <ns> nginx=nginx:broken"` |
| `cordon` | 标记节点为不可调度 | `"<node>"` |
| `uncordon` | 恢复节点可调度 | `"<node>"` |
| `taint` | 管理节点污点 | `"nodes <node> key=value:NoSchedule"` |
| `debug` | 在节点上创建临时调试容器，访问宿主机 /host 文件系统（两步法，必须含 `-- sleep 3600` 保活） | `"node/<node> --image=busybox -- sleep 3600"` |

> ⚠️ **kubectl debug 版本偏差注意**: `kubectl debug node/` 依赖 EphemeralContainers API，该 API 在不同 K8s 版本间存在 breaking changes。当本地 kubectl 版本与集群 API Server 版本差异超过 ±1 个 minor 版本时，`kubectl debug` 可能返回 "NotFound" 错误。此时无法通过 kubectl 工具获取宿主机文件系统访问，只能依赖 API 层面检查（`kubectl describe node` 看 DiskPressure、`kubectl get events` 看磁盘压力事件）。**注意**：`kubectl run` 不在允许的子命令列表中，不可用作备选。

所有工具都支持 `--kubeconfig`、`-n/--namespace`、`-l/--selector`、`-o` 等标准参数，通过 `v_args` 字符串传递。

**注意**：`kubectl run` 子命令不在允许列表中，Agent 无法使用。如需创建临时调试 Pod，只能使用 `kubectl debug`。

**重要原则**：做验证时，优先用 `kubectl(subcommand="get")` 加 `-o json` 获取结构化数据做断言；用 `kubectl(subcommand="describe")` 查看 Events 和 Conditions 做定性判断；用 `kubectl(subcommand="exec")` 进入容器做进程级/文件系统级验证；用 `kubectl(subcommand="top")` 看实时资源指标。

---

## 二、通用参数与查询语法

### 2.1 全局参数（所有命令可用）

| 参数 | 说明 | 示例 |
|------|------|------|
| `--kubeconfig <path>` | 指定集群凭证 | `--kubeconfig ~/.kube/config` |
| `--context <ctx>` | 指定 kubeconfig 上下文 | `--context prod-cluster` |
| `-n <ns>` / `--namespace <ns>` | 指定命名空间 | `-n default` |
| `--all-namespaces` / `-A` | 全命名空间查询 | `get pods -A` |
| `-l <selector>` / `--selector <selector>` | 标签选择器 | `-l app=nginx,tier=frontend` |
| `--field-selector <expr>` | 字段选择器 | `--field-selector status.phase=Running` |
| `-o <format>` | 输出格式 | `-o json`, `-o yaml`, `-o wide`, `-o name` |
| `--sort-by <jsonpath>` | 按字段排序 | `--sort-by=.status.phase` |

### 2.2 输出格式详解

Agent 必须理解每种输出格式的适用场景：

- **`-o json`**：返回完整的 JSON 对象，包含资源全部字段。适合做程序化断言（如检查 `.status.phase == "Running"`、`.status.containerStatuses[0].restartCount > 0`）。`kubectl(subcommand="get")` 加 `-o json` 获取结构化数据。
- **`-o yaml`**：与 JSON 内容相同，只是格式为 YAML。可读性更好，但解析难度相同。通用 `kubectl` 工具可用。
- **`-o wide`**：在默认表格基础上增加额外列（如 Pod 的 NODE、IP；Node 的 OS-IMAGE、KERNEL-VERSION）。适合快速一览。
- **`-o name`**：仅返回 `<resource>/<name>` 列表，适合批量处理。
- **默认（表格）**：人类可读，但 Agent 解析时容易出错，优先用 JSON。

### 2.3 标签选择器（Label Selector）

标签是 K8s 最核心的筛选机制，故障演练中大量通过标签定位目标 Pod/Node。

```bash
# 等于
-l app=nginx
# 不等
-l app!=nginx
# 多条件与
-l app=nginx,tier=frontend
# 存在某标签
-l 'release'
# 不存在某标签
-l '!release'
# 集合匹配
-l 'app in (nginx, apache)'
-l 'tier notin (backend)'
```

**Agent 验证技巧**：注入前记录目标 Pod 的标签，注入后通过相同标签选择器确认受影响的 Pod 集合是否变化。

### 2.4 字段选择器（Field Selector）

通过资源字段值做筛选，支持的操作符：`=`、`==`、`!=`。

```bash
# 按 Pod 状态筛选
kubectl get pods --field-selector status.phase=Running
kubectl get pods --field-selector status.phase!=Succeeded

# 按 Node 状态筛选
kubectl get nodes --field-selector spec.unschedulable=false

# 常见可用字段
# Pod: metadata.name, metadata.namespace, status.phase, spec.nodeName
# Node: metadata.name, status.phase
# Event: involvedObject.kind, involvedObject.name, type, reason
```

---

## 三、各子命令详解与验证用法

### 3.1 get — 读取资源状态

**核心能力**：获取任意 K8s 资源的当前状态，是验证阶段最常用的命令。

**Agent 可用资源类型**（故障演练高频）：

| 资源类型 | 缩写 | 验证场景 |
|----------|------|----------|
| `pods` | `po` | Pod 状态、重启次数、容器状态 |
| `nodes` | `no` | 节点就绪状态、容量、污点 |
| `deployments` | `deploy` | 副本数、更新策略、可用副本 |
| `replicasets` | `rs` | 期望/实际副本数 |
| `daemonsets` | `ds` | 期望/已调度 Pod 数 |
| `statefulsets` | `sts` | 副本数、分区更新状态 |
| `services` | `svc` | ClusterIP、端口映射、Endpoints |
| `endpoints` | `ep` | 后端 Pod IP 列表（验证服务发现） |
| `events` | `ev` | 事件流（OOMKilled、FailedScheduling、Evicted） |
| `persistentvolumeclaims` | `pvc` | 绑定状态、容量 |
| `persistentvolumes` | `pv` | 可用容量、回收策略 |
| `configmaps` | `cm` | 配置数据 |
| `secrets` | - | Secret 引用状态 |
| `horizontalpodautoscalers` | `hpa` | 当前/目标/最大副本数、指标 |
| `jobs` / `cronjobs` | - | 完成状态 |
| `all` | - | 某命名空间下所有核心资源 |

**验证示例（故障演练场景）**：

```bash
# 验证 Pod OOM：查看容器退出码和重启次数
kubectl get pod <pod> -n <ns> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.exitCode}'

# 验证 Pod CPU 高：查看 Pod 所在 Node
kubectl get pod <pod> -n <ns> -o wide

# 验证 Deployment 副本不一致：对比 desired / available
kubectl get deployment <name> -n <ns> -o jsonpath='{.status.replicas} {.status.availableReplicas}'

# 验证 Service 负载均衡异常：检查 Endpoints 是否为空
kubectl get endpoints <svc> -n <ns> -o json

# 验证 Node 不可用：检查 Ready 条件
kubectl get node <node> -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'

# 验证 HPA 达到上限：查看当前副本 vs 最大副本
kubectl get hpa <name> -n <ns> -o jsonpath='{.status.currentReplicas} {.spec.maxReplicas}'

# 验证 PVC Pending：查看状态
kubectl get pvc <name> -n <ns> -o jsonpath='{.status.phase}'

# 查看最近的异常事件
kubectl get events -n <ns> --sort-by=.lastTimestamp --field-selector type!=Normal
```

**JSONPath 常用表达式**：

```
{.metadata.name}                          # 名称
{.metadata.namespace}                     # 命名空间
{.metadata.labels}                        # 全部标签
{.metadata.labels.app}                    # 某个标签值
{.spec.nodeName}                          # Pod 所在节点
{.status.phase}                           # Pod 生命周期阶段
{.status.conditions[?(@.type=="Ready")].status}     # Ready 条件
{.status.containerStatuses[0].restartCount}         # 重启次数
{.status.containerStatuses[0].lastState.terminated.reason}   # 上次终止原因
{.status.containerStatuses[0].state.waiting.reason}          # 等待原因
{.spec.containers[0].resources.limits.cpu}                   # CPU limit
{.spec.containers[0].resources.limits.memory}                # 内存 limit
{.status.conditions[?(@.type=="MemoryPressure")].status}    # 节点内存压力
```

### 3.2 describe — 详细诊断（含 Events）

**核心能力**：展示资源的完整状态、Conditions、最近 Events。Events 是故障诊断的金矿。

**关键信息位置**：

- **Pod describe**：
  - `Status`：当前阶段（Running / Pending / CrashLoopBackOff / Terminating）
  - `Conditions`：PodScheduled / Initialized / ContainersReady / Ready
  - `Containers`：State（Running / Waiting / Terminated）、Last State、Restart Count、Limits/Requests
  - `Events`：调度事件、镜像拉取事件、健康检查失败、OOMKilled、Evicted

- **Node describe**：
  - `Conditions`：Ready / MemoryPressure / DiskPressure / PIDPressure / NetworkUnavailable
  - `Capacity` / `Allocatable`：CPU、内存、Pod 数量上限
  - `Non-terminated Pods`：当前运行在该节点的所有 Pod
  - `Events`：节点故障、Pod 驱逐事件

- **Deployment describe**：
  - `Replicas`：Desired / Current / Updated / Available
  - `Conditions`：Progressing / Available / ReplicaFailure
  - `Events`：扩缩容事件、滚动更新事件

**验证示例**：

```bash
# 验证 Pod 镜像拉取失败：看 Events 中是否有 ImagePullBackOff / ErrImagePull
kubectl describe pod <pod> -n <ns>

# 验证 Node 磁盘压力：看 Conditions 中 DiskPressure
kubectl describe node <node>

# 验证 Pod 被驱逐：看 Events 中 Evicted 和 eviction 原因
kubectl describe pod <pod> -n <ns>
```

### 3.3 top — 实时资源指标

**核心能力**：查看 Pod/Node 的实时 CPU（millicores）和内存（bytes/MiB）使用。需要集群已安装 **metrics-server**。

**关键认知**：`top` 显示的是**实际使用量**，不是 Limit。故障演练中常用于验证资源压力注入是否生效。

```bash
# 查看某命名空间所有 Pod 的 CPU/内存
kubectl top pod -n <ns>

# 查看特定 Pod
kubectl top pod <pod> -n <ns>

# 查看所有节点
kubectl top node

# 查看特定节点
kubectl top node <node>

# 按 CPU 排序
kubectl top pod -n <ns> --sort-by=cpu

# 按内存排序
kubectl top pod -n <ns> --sort-by=memory
```

**验证场景对照**：

| 故障类型 | top 验证点 |
|----------|-----------|
| Pod CPU 满载 | `top pod` 中目标 Pod CPU 接近 Limit 或异常高 |
| Pod 内存压力 | `top pod` 中目标 Pod 内存接近 Limit |
| Node CPU 高 | `top node` 中目标 Node CPU 使用率飙升 |
| Node 内存高 | `top node` 中目标 Node 内存使用率飙升 |

### 3.4 logs — 容器日志

**核心能力**：获取容器 stdout/stderr，用于验证应用层是否已感知故障（报错、超时、连接重置）。

```bash
# 当前日志
kubectl logs <pod> -n <ns>

# 最后 N 行
kubectl logs <pod> -n <ns> --tail=100

# 带时间戳
kubectl logs <pod> -n <ns> --timestamps

# 多容器 Pod 指定容器
kubectl logs <pod> -n <ns> -c <container>

# 查看之前崩溃容器的日志（关键！）
kubectl logs <pod> -n <ns> --previous

# 实时跟踪（注入时观察）
kubectl logs <pod> -n <ns> -f --tail=50

# 查看指定时间范围内日志
kubectl logs <pod> -n <ns> --since=5m
```

**故障日志关键词对照**：

| 故障现象 | 日志中可能出现的信号 |
|----------|---------------------|
| OOMKill | `Killed`, `OOM`, `out of memory`, `signal 9` |
| CPU 满载导致延迟 | `timeout`, `deadline exceeded`, `slow`, `latency` |
| 网络延迟 | `timeout`, `i/o timeout`, `connection timed out` |
| 网络丢包 | `connection reset`, `broken pipe`, `no route to host` |
| DNS 故障 | `lookup failed`, `no such host`, `resolve` |
| 磁盘满 | `no space left`, `write error`, `disk full` |
| 进程被杀 | `signal 9`, `signal 15`, `terminated` |
| 镜像拉取失败 | `ImagePullBackOff`, `ErrImagePull`, `not found` |

### 3.5 exec — 容器内执行

**核心能力**：在运行中的容器内执行命令，做进程级、文件系统级、网络级的深入验证。

> ⚠️ **重要限制**: `kubectl exec` **不支持 `-l/--selector`**（exec 只能连接单个 Pod 的单个容器，无法按标签批量执行）。需先用 `kubectl get pods -l <selector>` 获取具体 Pod 名称，再 exec 到该 Pod。

```bash
# 查看容器内进程（确认 chaos 进程是否存在）
kubectl exec <pod> -n <ns> -- ps aux

# 查看容器内 CPU 占用最高的进程
kubectl exec <pod> -n <ns> -- top -b -n 1

# 查看容器内存信息
kubectl exec <pod> -n <ns> -- cat /proc/meminfo

# 查看容器磁盘使用
kubectl exec <pod> -n <ns> -- df -h

# 查看网络连接状态
kubectl exec <pod> -n <ns> -- ss -tlnp
kubectl exec <pod> -n <ns> -- netstat -tlnp

# 测试网络连通性（验证网络故障）
kubectl exec <pod> -n <ns> -- ping -c 3 <target-ip>
kubectl exec <pod> -n <ns> -- curl -v --connect-timeout 5 <target-url>

# 查看文件是否存在（验证磁盘填充）
kubectl exec <pod> -n <ns> -- ls -la /data/

# 查看容器内特定文件内容
kubectl exec <pod> -n <ns> -- cat /etc/resolv.conf
```

**重要注意**：
- 部分精简镜像（如 distroless、scratch）没有 shell、ps、top、curl 等工具，exec 会失败。此时换用 `kubectl top` 或 `kubectl get pod -o json` 做验证。
- exec 命令运行在容器命名空间内，看到的是容器视角，不是宿主机视角。

### 3.6 delete — 删除资源

**核心能力**：删除资源。在故障演练中主要用于清理测试资源，**非必要不使用**。

```bash
# 删除特定 Pod（会触发 ReplicaSet/Deployment 重新创建）
kubectl delete pod <pod> -n <ns>

# 按标签删除一批 Pod
kubectl delete pod -n <ns> -l app=nginx

# 强制删除（Terminating 卡住时使用）
kubectl delete pod <pod> -n <ns> --force --grace-period=0
```

---

## 四、Events 专项：故障诊断的黄金数据源

K8s Events 是故障验证中最容易被忽视但价值最高的信息源。Events 记录了集群中所有重要状态变化。

### 4.1 查询 Events 的常用方式

```bash
# 查看某命名空间最近事件（按时间排序）
kubectl get events -n <ns> --sort-by=.lastTimestamp

# 只看异常事件（排除 Normal）
kubectl get events -n <ns> --field-selector type!=Normal --sort-by=.lastTimestamp

# 查看与特定 Pod 相关的事件
kubectl get events -n <ns> --field-selector involvedObject.name=<pod>

# 查看与特定 Node 相关的事件
kubectl get events --field-selector involvedObject.kind=Node,involvedObject.name=<node>

# 查看特定原因的事件
kubectl get events -n <ns> --field-selector reason=FailedScheduling
```

### 4.2 故障场景与 Event 对照表

Agent 在设计验证方案时，应根据故障类型去 Events 中寻找对应信号：

| 故障场景 | 可能出现的 Event Reason | Event Message 关键词 |
|----------|------------------------|---------------------|
| Pod Pending（资源不足） | `FailedScheduling` | `Insufficient cpu`, `Insufficient memory` |
| Pod OOMKilled | `OOMKilling` | `Memory cgroup out of memory` |
| Pod 镜像拉取失败 | `Failed` | `ErrImagePull`, `ImagePullBackOff` |
| Node 内存压力 | `NodeHasInsufficientMemory` | `insufficient memory` |
| Node 磁盘压力 | `NodeHasDiskPressure` | `disk pressure` |
| Node NotReady | `NodeNotReady` | `Kubelet stopped posting` |
| Pod 被驱逐 | `Evicted` | `The node was low on resource` |
| 健康检查失败 | `Unhealthy` | `Liveness probe failed`, `Readiness probe failed` |
| 容器启动失败 | `BackOff` | `CrashLoopBackOff` |
| 挂载失败 | `FailedMount` | `Unable to mount`, `volume not found` |

---

## 五、JSON 输出关键字段速查

当使用 `kubectl get ... -o json` 时，以下字段是故障验证中最常需要断言的：

### 5.1 Pod 关键字段

```json
{
  "metadata": {
    "name": "...",
    "namespace": "...",
    "labels": { "app": "...", "version": "..." },
    "deletionTimestamp": "..."   // 非空表示正在 Terminating
  },
  "spec": {
    "nodeName": "...",            // Pod 所在节点
    "containers": [{
      "name": "...",
      "image": "...",
      "resources": {
        "limits": { "cpu": "...", "memory": "..." },
        "requests": { "cpu": "...", "memory": "..." }
      }
    }]
  },
  "status": {
    "phase": "Running|Pending|Succeeded|Failed|Unknown",
    "conditions": [
      { "type": "PodScheduled", "status": "True" },
      { "type": "Initialized", "status": "True" },
      { "type": "Ready", "status": "True|False" },
      { "type": "ContainersReady", "status": "True|False" }
    ],
    "containerStatuses": [{
      "name": "...",
      "state": {
        "running": { "startedAt": "..." },
        "waiting": { "reason": "ImagePullBackOff|CrashLoopBackOff|ContainerCreating", "message": "..." },
        "terminated": { "exitCode": 137, "reason": "OOMKilled|Error|Completed", "finishedAt": "..." }
      },
      "lastState": {
        "terminated": { "exitCode": 137, "reason": "OOMKilled" }
      },
      "restartCount": 5,
      "ready": true
    }],
    "reason": "...",   // 如 Evicted
    "message": "..."   // 如 The node was low on resource: memory
  }
}
```

**关键验证点**：
- `status.phase`：Running=正常，Pending=调度中/资源不足，Failed=失败
- `status.containerStatuses[].restartCount`：>0 表示有重启，故障注入后可能增加
- `status.containerStatuses[].state.waiting.reason`：`ImagePullBackOff`、`CrashLoopBackOff`
- `status.containerStatuses[].state.terminated.reason`：`OOMKilled`（exitCode 137）、`Error`
- `status.conditions[]`：`Ready=False` 表示 Pod 未就绪
- `metadata.deletionTimestamp`：非空表示 Pod 正在 Terminating

### 5.2 Node 关键字段

```json
{
  "metadata": { "name": "..." },
  "status": {
    "conditions": [
      { "type": "Ready", "status": "True|False", "reason": "..." },
      { "type": "MemoryPressure", "status": "False|True" },
      { "type": "DiskPressure", "status": "False|True" },
      { "type": "PIDPressure", "status": "False|True" },
      { "type": "NetworkUnavailable", "status": "False|True" }
    ],
    "capacity": { "cpu": "8", "memory": "32761208Ki", "pods": "110" },
    "allocatable": { "cpu": "7600m", "memory": "29761208Ki", "pods": "110" },
    "nodeInfo": {
      "osImage": "Ubuntu 22.04",
      "kernelVersion": "5.15.0",
      "containerRuntimeVersion": "containerd://1.6.0"
    }
  }
}
```

**关键验证点**：
- `conditions[?(@.type=="Ready")].status`：`True`=节点正常，`False`=节点不可用
- `conditions[?(@.type=="MemoryPressure")].status`：`True`=节点内存压力过大
- `conditions[?(@.type=="DiskPressure")].status`：`True`=节点磁盘压力过大

### 5.3 Deployment 关键字段

```json
{
  "metadata": { "name": "..." },
  "spec": {
    "replicas": 3,
    "strategy": { "type": "RollingUpdate" }
  },
  "status": {
    "replicas": 3,          // 总副本数
    "updatedReplicas": 3,   // 已更新副本数
    "readyReplicas": 2,     // 就绪副本数（< spec.replicas 表示异常）
    "availableReplicas": 2, // 可用副本数
    "unavailableReplicas": 1,
    "conditions": [
      { "type": "Available", "status": "True" },
      { "type": "Progressing", "status": "True" }
    ]
  }
}
```

**关键验证点**：
- `status.readyReplicas < spec.replicas`：部分 Pod 未就绪（可能是故障注入导致）
- `status.availableReplicas < spec.replicas`：部分 Pod 不可用
- `conditions`：查看 Available / Progressing 状态

### 5.4 Service / Endpoints 关键字段

```json
{
  "metadata": { "name": "..." },
  "spec": {
    "clusterIP": "10.96.0.1",
    "ports": [{ "port": 80, "targetPort": 8080 }],
    "selector": { "app": "nginx" }
  }
}
```

Endpoints：
```json
{
  "metadata": { "name": "..." },
  "subsets": [{
    "addresses": [{ "ip": "10.244.1.5" }],   // 后端 Pod IP
    "notReadyAddresses": [{ "ip": "10.244.1.6" }],  // 未就绪的后端
    "ports": [{ "port": 8080 }]
  }]
}
```

**关键验证点**：
- `subsets[].addresses` 为空：Service 没有可用后端（验证负载均衡异常）
- `subsets[].notReadyAddresses` 非空：部分后端未就绪但仍被保留

### 5.5 Event 关键字段

```json
{
  "type": "Warning|Normal",
  "reason": "FailedScheduling",
  "message": "0/3 nodes are available: insufficient memory",
  "involvedObject": { "kind": "Pod", "name": "..." },
  "count": 5,              // 发生次数
  "firstTimestamp": "...",
  "lastTimestamp": "...",
  "source": { "component": "default-scheduler" }
}
```

---

## 六、相关文档

- 故障场景 → kubectl 验证命令组合：见 `fault-verification-strategies.md`（Q3-Q11 按故障类型给出完整方案）
- 验证命令设计原则（针对性 / 可量化 / 多源交叉 / 时序 / 可回滚）：见 `fault-verification-strategies.md` Q2
- 紧凑的子命令配方与 JSONPath 速查（用于 LLM 按需取用）：见 `kubectl-recipes.md`

