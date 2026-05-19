---
title: "K8s Core Knowledge"
topics:
  - Pod lifecycle
  - namespaces
  - workloads
  - health checks
  - resource model
  - networking
  - events
  - fault propagation
  - verification layers
fault_types:
  - pod-kill
  - pod-oom
  - node-cpu-stress
  - node-network-delay
summary: "K8s architecture and chaos engineering context: Pod lifecycle, health checks, resource model, networking, fault propagation paths, verification layer overview. Terminology dictionary included."
---

# Kubernetes 基础知识问答（故障演练专用）

本文件以问答形式梳理 Kubernetes 核心概念，帮助故障演练 Agent 理解 K8s 资源模型、状态语义和故障传播机制，从而设计出更精准的注入方案和验证方案。

---

## 一、核心资源与架构

### Q1: Kubernetes 的基本架构是什么样的？控制平面和工作节点各自负责什么？

**A1**: Kubernetes 采用主从（Master-Worker）架构：

- **控制平面（Control Plane）**：负责集群的全局决策和状态管理，包含：
  - **kube-apiserver**：所有组件通信的唯一入口，暴露 REST API
  - **etcd**：分布式键值存储，保存集群所有状态数据
  - **kube-scheduler**：负责将新 Pod 调度到合适的 Node
  - **kube-controller-manager**：运行各种控制器（Deployment Controller、Node Controller、Endpoint Controller 等），维持期望状态
  - **cloud-controller-manager**（可选）：对接云厂商 API

- **工作节点（Worker Node）**：运行实际负载，包含：
  - **kubelet**：接收 apiserver 指令，管理本节点 Pod 生命周期
  - **kube-proxy**：维护节点上的网络规则，实现 Service 负载均衡
  - **容器运行时（containerd/CRI-O）**：真正运行容器

**故障演练意义**：
- 控制平面组件（尤其是 etcd、kube-apiserver）是集群的"大脑"，**严禁对其注入故障**（安全红线 — 详见 chaos-engineering-principles Q9.1）。
- Node 级故障本质上影响的是该节点上的 kubelet 和容器运行时，进而影响该节点上的所有 Pod。
- Pod 状态变化由 kubelet 上报给 apiserver，存在一定的上报延迟（通常几秒）。

---

### Q2: 什么是 Pod？为什么 Pod 是 Kubernetes 的最小调度单位？

**A2**: Pod 是 Kubernetes 中最小的可部署单元，封装了一个或多个容器（通常是一个主容器 + 若干 sidecar 容器）。

Pod 的核心特征：
- **共享网络命名空间**：同一个 Pod 内的所有容器共享 IP 地址和端口空间，通过 `localhost` 互相通信
- **共享存储卷（Volumes）**：Pod 内的容器可以挂载同一个 Volume 实现文件共享
- **生命周期由 Pod 管理**：容器可以重启，但 Pod 的 IP 通常在重建后改变（除非使用 StatefulSet）
- **一次性调度**：Pod 被调度到某个 Node 后不会自动迁移到其他 Node（除非被删除重建）

**故障演练意义**：
- 注入 Pod 级故障时，故障影响范围被限制在该 Pod 内部（网络、磁盘、CPU、内存）
- 如果 Pod 被删除或崩溃，其所属的 Deployment/ReplicaSet 会根据 `spec.replicas` 自动创建新 Pod 来替代
- 多容器 Pod 中，需要确认故障注入到正确的容器（ChaosBlade 的 `--container-names` 参数）

---

### Q3: Deployment、ReplicaSet、Pod 三者之间的关系是什么？

**A3**: 三者是层级控制关系：

```
Deployment (期望状态: replicas=3, image=nginx:v2)
    └── ReplicaSet (由 Deployment 创建和管理，维护 3 个 Pod 副本)
            ├── Pod-1
            ├── Pod-2
            └── Pod-3
```

- **Deployment**：用户直接操作的资源，定义了应用的期望状态（镜像版本、副本数、更新策略）。它通过管理 ReplicaSet 来实现滚动更新和回滚。
- **ReplicaSet**：确保指定数量的 Pod 副本始终运行。当 Pod 被删除或节点故障导致 Pod 丢失时，ReplicaSet 会自动创建新 Pod。
- **Pod**：实际运行容器的载体。

**故障演练意义**：
- 删除一个 Pod 不会导致服务不可用，因为 ReplicaSet 会立即创建新 Pod（除非同时设置了 `terminationGracePeriod=0` 和强制删除）
- 验证 Pod 删除故障时，应观察 Deployment 的 `availableReplicas` 是否短暂下降后恢复
- 如果注入导致 Pod 持续 CrashLoopBackOff，ReplicaSet 会不断尝试重建 Pod，但 Deployment 的 `readyReplicas` 会持续低于 `replicas`

---

### Q4: StatefulSet 和 Deployment 有什么区别？为什么 StatefulSet 的故障演练需要更谨慎？

**A4**: StatefulSet 用于管理有状态应用（如数据库、消息队列），与 Deployment 的关键区别：

| 特性 | Deployment | StatefulSet |
|------|-----------|-------------|
| Pod 命名 | 随机哈希后缀 | 有序序号（如 web-0, web-1, web-2） |
| 网络身份 | 每次重建 IP 变化 | 通过 Headless Service 保持稳定的网络标识 |
| 存储 | 通常使用临时存储或共享存储 | 每个 Pod 绑定独立的 PVC，Pod 重建后重新挂载原 PVC |
| 创建/删除顺序 | 并行 | 严格有序（创建按 0→N，删除按 N→0） |
| 扩缩容 | 并行 | 按序号逐个进行 |

**故障演练意义**：
- StatefulSet 的 PVC 在 Pod 删除后不会删除，数据会保留。但如果故障注入导致数据损坏，恢复后可能影响数据一致性。
- **安全红线**："无备份不对 StatefulSet 做破坏性实验" — 详见 chaos-engineering-principles Q9.2
- StatefulSet Pod 重建后仍挂载原 PVC，所以磁盘填充类故障在恢复后，如果填充的文件未被清理，问题可能持续存在。

---

### Q5: DaemonSet 的作用是什么？它的故障特点是什么？

**A5**: DaemonSet 确保集群中每个（或指定的一批）Node 上都运行一个 Pod 副本。典型用途：日志收集（Fluentd/Fluent Bit）、监控采集（Prometheus Node Exporter）、网络代理（Calico、Cilium）。

**故障演练意义**：
- DaemonSet 的调度逻辑与 Deployment 不同：它按节点调度，而不是按副本数调度
- 验证 DaemonSet 故障时，应检查 `status.desiredNumberScheduled` 是否等于 `status.numberReady`
- 如果某 Node 上的 DaemonSet Pod 故障，该节点会失去日志采集或监控能力，但业务 Pod 通常不受影响（除非网络类 DaemonSet）
- DaemonSet Pod 通常需要特殊权限（hostNetwork、hostPath），注入时需确认安全边界

---

### Q6: Service 和 Endpoints 是如何工作的？负载均衡异常通常发生在哪里？

**A6**: Service 是 Kubernetes 的抽象层，为一组 Pod 提供统一的访问入口：

- **Service**：定义了访问策略（ClusterIP、NodePort、LoadBalancer、ExternalName）和选择器（selector）
- **Endpoints**：由 Endpoint Controller 自动维护，包含了所有匹配 Service selector 且处于 Ready 状态的 Pod 的 IP:Port 列表
- **kube-proxy**：在每个 Node 上监听 Endpoints 变化，更新 iptables/IPVS 规则，实现流量转发

**故障演练意义**：
- Service 负载均衡异常通常表现为：Endpoints 的 `addresses` 列表为空或缺少某些 Pod IP
- 造成 Endpoints 为空的原因：
  1. Pod 未通过 Readiness Probe（`ready=False`）
  2. Pod 标签与 Service selector 不匹配
  3. Pod 被删除或处于 Terminating 状态
  4. 网络故障导致 Pod IP 不可达
- 验证负载均衡异常时，应同时检查 `kubectl get svc`（selector 是否正确）、`kubectl get endpoints`（后端列表）、`kubectl get pods -l <selector>`（Pod 状态）

---

### Q7: HPA（Horizontal Pod Autoscaler）的工作原理是什么？如何验证 HPA 达到上限？

**A7**: HPA 根据指标自动调整 Deployment/StatefulSet 的副本数：

```
metrics-server 采集指标 → HPA Controller 计算期望副本数 → 修改 Deployment replicas
```

HPA 支持五种指标类型（`autoscaling/v2` API）：

| 类型 | 数据来源 API 组 | 说明 | 数据提供者 |
|------|----------------|------|-----------|
| **Resource** | metrics.k8s.io | Pod 的 CPU/Memory 利用率 | metrics-server |
| **ContainerResource** | metrics.k8s.io | 特定容器的资源指标（v1.30 stable） | metrics-server |
| **Pods** | custom.metrics.k8s.io | 每个 Pod 的自定义指标平均值 | Prometheus Adapter 等 |
| **Object** | custom.metrics.k8s.io | 描述其他 K8s 对象的指标 | Prometheus Adapter 等 |
| **External** | external.metrics.k8s.io | 与 K8s 无关的外部指标（如消息队列长度） | Prometheus Adapter 等 |

> 故障演练中最常见的是 Resource 类型的 CPU/Memory 指标（由 metrics-server 提供）。但如果集群部署了 Prometheus Adapter，HPA 也可能基于自定义指标（如 QPS、延迟）触发扩缩容。

HPA 的关键字段：
- `spec.minReplicas` / `spec.maxReplicas`：副本数上下限
- `spec.metrics`：触发扩缩容的指标（Resource CPU/Memory、Pods、Object、External）
- `status.currentReplicas`：当前副本数
- `status.desiredReplicas`：期望副本数

**故障演练意义**：
- 注入 CPU 满载故障后，如果 HPA 已配置，Pod CPU 升高会触发 HPA 扩容
- 当 `currentReplicas == maxReplicas` 且 CPU 仍然超过阈值时，表示 HPA 已达上限，无法继续扩容
- 验证 HPA 上限故障时：
  1. `kubectl get hpa -o json` 确认 `currentReplicas == spec.maxReplicas`
  2. `kubectl top pod` 确认 CPU 仍高于目标值
  3. `kubectl get deployment` 确认副本数不再增加

---

### Q8: Namespace 的作用是什么？为什么故障演练要在隔离 Namespace 中进行？

**A8**: Namespace 是 Kubernetes 中的逻辑隔离边界：

- 同一 Namespace 内的资源名称必须唯一，不同 Namespace 可以重名
- RBAC（基于角色的访问控制）通常按 Namespace 划分权限
- 资源配额（ResourceQuota）和限制范围（LimitRange）在 Namespace 级别生效
- 网络策略（NetworkPolicy）通常按 Namespace 定义

**故障演练意义**：
- **隔离原则**：必须在隔离的测试 Namespace 中演练，严禁在 `kube-system`、`kube-public` 等系统 Namespace 中注入 — 详见 chaos-engineering-principles Q9.1
- Namespace 级别的故障（如删除 Namespace 内所有 Pod）影响范围可控
- 验证时通过 `-n <ns>` 限定查询范围，避免被其他 Namespace 的噪声干扰

---

### Q9: ConfigMap 和 Secret 是什么？它们的故障场景有哪些？

**A9**: ConfigMap 和 Secret 用于将配置数据注入到 Pod 中：

- **ConfigMap**：存储非敏感的配置数据（配置文件、环境变量值、命令行参数）
- **Secret**：存储敏感数据（密码、Token、TLS 证书），数据在 etcd 中 base64 编码存储

注入方式：
- 环境变量：`envFrom` / `env.valueFrom`
- 文件挂载：`volumeMounts` 挂载为文件
- 命令行参数：`$(ENV_NAME)` 引用

**故障演练意义**：
- 当前技能目录主要关注运行时资源故障（CPU、内存、网络、磁盘），ConfigMap/Secret 故障属于配置层故障
- 如果未来扩展配置故障场景，可能的验证方式：
  - 修改 ConfigMap 后观察应用是否热加载（大多数应用不会自动重载 ConfigMap 挂载的配置）
  - 删除 Secret 后观察依赖该 Secret 的 Pod 启动失败（`FailedMount` Event）
- ConfigMap/Secret 更新后，已运行的 Pod 不会自动感知变化，需要滚动更新或重启 Pod

---

### Q10: PersistentVolume（PV）和 PersistentVolumeClaim（PVC）的关系是什么？

**A10**: PV 和 PVC 解耦了存储的供给和使用：

- **PV**：集群中的存储资源（由管理员或 StorageClass 动态供给），代表一块实际的存储（NFS、云盘、本地盘等）
- **PVC**：用户（Pod）对存储的申请，声明需要的容量和访问模式
- **StorageClass**：定义动态供给的存储模板（如 SSD、HDD、网络存储）

绑定流程：
```
Pod 引用 PVC → PVC 匹配/创建 PV → PV 绑定到 PVC → 存储挂载到 Pod
```

**故障演练意义**：
- PVC Pending 是最常见的存储故障：可能原因包括没有匹配的 PV、StorageClass 不存在、配额不足
- 验证 PVC Pending：`kubectl get pvc -o json` 查看 `status.phase == "Pending"`
- 节点磁盘故障可能影响本地 PV 的可用性
- Storage 相关的 Events（`FailedMount`、`FailedAttachVolume`）是诊断存储故障的关键

---

## 二、Pod 生命周期与状态语义

### Q11: Pod 的完整生命周期阶段（phase）有哪些？每个阶段代表什么？

**A11**: Pod 的 `status.phase` 有五个值：

| Phase | 含义 | 故障演练关联 |
|-------|------|-------------|
| **Pending** | Pod 已被 K8s 接受，但有一个或多个容器尚未创建。通常是因为镜像拉取中、卷挂载中、或调度失败（资源不足、污点不匹配） | Pending 故障的核心验证点 |
| **Running** | Pod 已绑定到 Node，至少一个容器正在运行，或正在启动/重启 | 正常运行状态，但 Running 不代表 Ready |
| **Succeeded** | 所有容器正常终止（exitCode=0），且不会重启（如 Job） | 通常不用于长期运行的服务 |
| **Failed** | 所有容器终止，且至少一个容器非正常退出（exitCode≠0） | 进程被杀、应用崩溃后的状态 |
| **Unknown** | 无法获取 Pod 状态（通常是节点与 apiserver 通信中断） | Node 故障的间接表现 |

**重要区分**：
- `phase=Running` 只表示容器在运行，不代表应用健康
- 应用是否可用要看 `status.conditions` 中的 `Ready=True` 和 Readiness Probe 结果

---

### Q12: Container 的状态（Container State）有哪几种？如何解读？

**A12**: 每个容器有三种可能的状态：

| State | 含义 | 典型原因 |
|-------|------|----------|
| **Waiting** | 容器尚未运行 | `ContainerCreating`（正在创建）、`ImagePullBackOff`（镜像拉取失败）、`CrashLoopBackOff`（反复崩溃）、`PodInitializing`（init 容器执行中） |
| **Running** | 容器正在运行 | 正常运行 |
| **Terminated** | 容器已终止 | `Completed`（正常完成）、`OOMKilled`（被 OOM 杀死，exitCode=137）、`Error`（异常退出）、`ContainerCannotRun`（容器无法运行） |

> **注意**: `Evicted` 不是 Container 级别状态，而是 **Pod 级别状态**(`status.reason=Evicted`)。Evicted Pod 的 `status.phase=Failed`，但其容器级 `terminated.reason` 通常为 `Error` 或 `OOMKilled`，而不是 `Evicted`。

**关键字段**：
- `restartCount`：容器重启次数。故障注入导致容器崩溃时，该值会增加
- `lastState.terminated`：上次终止的原因和退出码
- `ready`：容器是否通过了 Readiness Probe

**退出码速查**：
- 0：正常退出
- 1：通用错误
- 137 (128+9)：收到 SIGKILL（通常是 OOMKilled 或强制终止）
- 143 (128+15)：收到 SIGTERM（优雅终止）

---

### Q13: Pod 的 Conditions 有哪些？它们与 Pod 可用性的关系是什么？

**A13**: Pod 的 `status.conditions` 包含四个条件：

| Condition | 含义 |
|-----------|------|
| **PodScheduled** | Pod 已被调度到某个 Node |
| **Initialized** | 所有 init 容器已执行完毕 |
| **ContainersReady** | 所有容器已通过 Readiness Probe（或没有配置 Probe） |
| **Ready** | Pod 可以接收 Service 流量（等于 ContainersReady + 没有删除中） |

**故障演练意义**：
- 注入故障后，如果容器健康检查失败，`ContainersReady` 和 `Ready` 会变为 `False`
- `Ready=False` 的 Pod 会从 Service Endpoints 中被移除，导致流量不再发送到该 Pod
- 验证网络/进程类故障时，应同时检查 `Ready` 条件和 Endpoints 变化

---

### Q14: 什么是 Terminating 状态？Pod 为什么会卡在 Terminating？

**A14**: 当 Pod 被删除（`kubectl delete pod` 或 Deployment 缩容）时，Pod 进入 Terminating 状态：

1. apiserver 将 Pod 的 `deletionTimestamp` 设置为当前时间 + `terminationGracePeriodSeconds`（默认 30 秒）
2. kubelet 向容器发送 SIGTERM（信号 15）
3. 容器在宽限期内优雅退出
4. 如果容器未退出，kubelet 发送 SIGKILL（信号 9）强制终止
5. 清理资源（网络、存储卷）

**卡在 Terminating 的常见原因**：
- Pod 内有进程未响应 SIGTERM（如没有正确处理信号的应用）
- Finalizer 未完成（某些控制器在 Pod 删除前需要执行清理逻辑）
- 存储卷无法卸载（如 NFS 服务器不可达、挂载点被占用）
- kubelet 或容器运行时故障，无法执行删除操作

**故障演练意义**：
- Terminating 卡住是一个独立的故障场景
- 验证时检查：`metadata.deletionTimestamp` 非空且已过去较长时间，但 Pod 仍然存在
- 强制删除：`kubectl delete pod <pod> --force --grace-period=0`（绕过优雅终止，直接删除 etcd 记录）

---

## 三、健康检查与自愈机制

### Q15: Liveness Probe、Readiness Probe、Startup Probe 的区别是什么？

**A15**: 三种探针分别用于不同的健康检查目的：

| 探针 | 用途 | 失败后果 | 适用场景 |
|------|------|----------|----------|
| **Liveness Probe** | 检查容器是否还活着 | kubelet 杀死容器并重启 | 检测死锁、无限循环等应用僵死状态 |
| **Readiness Probe** | 检查容器是否已准备好接收流量 | Pod 从 Service Endpoints 中移除 | 检测应用启动中、依赖服务未就绪 |
| **Startup Probe** | 检查应用是否已完成启动 | 禁用其他探针，防止启动阶段被误杀 | 启动时间很长的应用（如 JVM） |

**探针类型**：
- `httpGet`：发送 HTTP 请求，检查状态码
- `tcpSocket`：尝试 TCP 连接
- `exec`：在容器内执行命令，检查退出码
- `grpc`：gRPC 健康检查（较新）

**故障演练意义**：
- 网络延迟/丢包故障可能导致 Liveness Probe 失败，触发容器重启（`restartCount` 增加）
- CPU 满载可能导致探针超时（如果 `timeoutSeconds` 设置较短），引发不必要的重启
- 验证故障时，应检查 `describe pod` 中的探针失败 Events（`Unhealthy`）
- 如果探针配置不合理（如 `periodSeconds` 太短、`failureThreshold` 太小），故障的影响会被放大

---

### Q16: Kubernetes 的自愈机制有哪些？它们如何影响故障演练的观察？

**A16**: Kubernetes 内置了多种自愈机制：

| 机制 | 触发条件 | 行为 | 对故障演练的影响 |
|------|----------|------|-----------------|
| **容器重启** | Liveness Probe 失败或容器异常退出 | kubelet 根据 `restartPolicy`（Always/OnFailure/Never）决定是否重启容器 | Pod 级故障可能导致容器反复重启，观察 `restartCount` |
| **Pod 重建** | Deployment/ReplicaSet 检测到 Pod 数量不足 | 创建新 Pod 替代丢失的 Pod | Node 故障或 Pod 删除后，新 Pod 会在其他 Node 上重建 |
| **重新调度** | Pod 处于 Pending 状态 | scheduler 尝试将 Pod 调度到可用 Node | 资源不足导致的 Pending 故障，释放资源后会自动恢复 |
| **Node 驱逐** | Node 进入 NotReady 或资源压力状态 | Controller 将该节点上的 Pod 标记为删除，在其他节点重建 | Node 故障后，Pod 会自动漂移 |
| **Endpoint 更新** | Pod Ready 状态变化 | Endpoint Controller 更新 Service 后端列表 | Ready=False 的 Pod 自动从流量中摘除 |

**故障演练意义**：
- 设计验证方案时，必须考虑自愈机制的时间窗口。例如 Pod 删除后 5-10 秒内新 Pod 就会启动，验证要抓住这个窗口
- 某些故障（如 CPU 满载）如果触发了 Liveness Probe 失败，容器会不断重启，这时观察到的现象是 "反复重启" 而不是 "CPU 高"
- 自愈可能掩盖故障的真实影响，Agent 需要通过 Events、日志、指标等多维度穿透自愈层看到底层问题

---

## 四、资源模型与调度

### Q17: Request 和 Limit 的区别是什么？它们在故障演练中起什么作用？

**A17**: Request 和 Limit 是容器资源声明的两个维度：

| 维度 | 含义 | 作用 |
|------|------|------|
| **Request** | 容器保证能获得的资源量 | scheduler 根据 Request 决定 Pod 调度到哪个 Node（Node 的 Allocatable 必须 >= 所有 Pod 的 Request 之和） |
| **Limit** | 容器最多能使用的资源量 | 如果容器使用超过 Limit，CPU 会被节流（throttle），内存会被 OOMKilled |

**示例**：
```yaml
resources:
  requests:
    cpu: "100m"      # 0.1 核
    memory: "128Mi"  # 128 MB
  limits:
    cpu: "500m"      # 0.5 核
    memory: "256Mi"  # 256 MB
```

**故障演练意义**：
- **Pod CPU 满载验证**：`top pod` 中 CPU 使用量接近 Limit（而不是 Request）。ChaosBlade 的 `pod-cpu fullload` 会让 Pod 的 CPU 使用接近 Limit。
- **Pod 内存压力验证**：当内存使用接近 Limit 时，容器会被 OOMKilled（exitCode 137）。如果没有设置 Limit，Pod 可能使用到节点内存耗尽。
- **Pending 故障（资源不足）**：当节点上所有 Pod 的 CPU/Memory Request 之和超过节点的 Allocatable 时，新 Pod 会处于 Pending 状态。

---

### Q18: 什么是节点污点（Taint）和容忍（Toleration）？它们如何影响调度？

**A18**: Taint 是节点上的"排斥标签"，Toleration 是 Pod 上的"容忍声明"：

- 如果节点有 Taint，而 Pod 没有对应的 Toleration，Pod 不能被调度到该节点
- 即使 Pod 已运行在该节点，某些 Taint（如 `NoExecute`）也会导致 Pod 被驱逐

常见系统 Taint：
- `node.kubernetes.io/not-ready`：节点未就绪
- `node.kubernetes.io/unreachable`：节点不可达
- `node.kubernetes.io/disk-pressure`：磁盘压力
- `node.kubernetes.io/memory-pressure`：内存压力
- `node.kubernetes.io/pid-pressure`：PID 压力
- `node.kubernetes.io/network-unavailable`：网络不可用

**故障演练意义**：
- Node 级故障（如磁盘满、内存高）会导致 kubelet 自动给节点添加对应 Taint，进而驱逐节点上的 Pod
- 验证 Node 故障时，应观察节点 Taint 变化和 Pod 驱逐 Events
- 某些 DaemonSet（如监控采集器）会带有这些 Taint 的 Toleration，确保即使节点故障也能继续运行

---

### Q19: 什么是亲和性（Affinity）和反亲和性（Anti-Affinity）？

**A19**: 亲和性控制 Pod 倾向于调度到哪些节点或与哪些 Pod 共存：

- **NodeAffinity**：Pod 倾向于调度到满足特定标签的节点（如 `disktype=ssd`）
- **PodAffinity**：Pod 倾向于与满足特定标签的 Pod 调度到同一节点
- **PodAntiAffinity**：Pod 倾向于与满足特定标签的 Pod 分散到不同节点（如 "同一个 Deployment 的 Pod 不要调度到同一节点"）

**故障演练意义**：
- 反亲和性配置会影响 Pod 重建时的调度位置。例如 Node 故障后，如果其他节点因反亲和性限制无法接受重建的 Pod，Pod 会处于 Pending 状态
- 验证高可用场景时，应检查反亲和性是否生效（同一故障域内的 Pod 数量）

---

## 五、网络模型

### Q20: Kubernetes 的网络模型核心原则是什么？

**A20**: Kubernetes 网络模型要求：

1. **每个 Pod 有独立的 IP 地址**（Pod IP 在集群内可路由）
2. **Pod 之间可以直接通信**，无需 NAT（无论是否在同一 Node）
3. **Node 上的 Agent（kubelet、kube-proxy）可以与所有 Pod 通信**

实现方式由 CNI 插件负责（Calico、Cilium、Flannel、Weave 等），不同 CNI 的网络拓扑和故障表现不同。

**故障演练意义**：
- Pod 网络故障（延迟、丢包、DNS）由 CNI 实现，但 ChaosBlade 的网络注入是在 Pod 的网络命名空间内操作，与底层 CNI 无关
- Service 的 ClusterIP 是虚拟 IP，其可达性取决于 kube-proxy 模式：
  - **iptables 模式**：ClusterIP 不绑定任何网络接口，仅存在于 iptables NAT 规则中，**Pod 内无法 ping 通 ClusterIP**（ICMP 不命中 NAT 规则），但可通过 `ClusterIP:Port` TCP/UDP 连接访问服务
  - **IPVS 模式**：ClusterIP 绑定到 `kube-ipvs0` dummy 接口，**Pod 内可以 ping 通 ClusterIP**（但 ping 回显来自节点本机，不转发到后端 Pod），TCP/UDP 连接仍经 IPVS 负载均衡
  - **验证建议**：无论哪种模式，都应通过 `curl ClusterIP:Port` 或 Pod DNS 名称验证服务可用性，而非 ping ClusterIP
- 验证网络故障时，应在 Pod 内测试对具体 Pod IP 的连通性，而不是测试对 ClusterIP 的连通性

---

### Q21: DNS 在 Kubernetes 中是如何工作的？

**A21**: Kubernetes 集群 DNS（通常是 CoreDNS）提供集群内的服务发现：

- **Service DNS**：`<service>.<namespace>.svc.cluster.local` → ClusterIP
- **Pod DNS**（需开启）：`<pod-ip>.<namespace>.pod.cluster.local`
- **Headless Service**：DNS 直接返回后端 Pod IP 列表（用于 StatefulSet）

CoreDNS 以 Deployment/DaemonSet 形式运行在集群中，通常位于 `kube-system` 命名空间。

**故障演练意义**：
- DNS 故障注入（ChaosBlade `pod-network dns`）会修改 Pod 内的 `/etc/hosts` 文件（添加 `#chaosblade` 注释的域名-IP 条目），而非修改 `/etc/resolv.conf`
- 验证 DNS 故障时，应使用 `cat /etc/hosts`（确认 #chaosblade 条目）和 `ping <domain>`（确认解析到伪造 IP；glibc 镜像也可用 `getent hosts <domain>`，但 Alpine/musl 镜像不含 `getent`），**不要用 `nslookup`/`dig`**（它们绕过 /etc/hosts，无法检测 ChaosBlade DNS 劫持）
- CoreDNS 本身位于 `kube-system`，**严禁对其注入故障**（安全红线）

---

## 六、Events 与日志

### Q22: Kubernetes Events 的生命周期和可靠性如何？

**A22**: Events 是 Kubernetes 中重要的诊断信息源，但有以下特点：

- Events 存储在 etcd 中，默认保留 **1 小时**（由事件聚合器控制，可通过 `--event-ttl` 调整）
- 同一类型的事件会被聚合（`count` 字段表示发生次数）
- Events 的 `type` 分为 `Normal`（正常）和 `Warning`（警告）
- Events 的 `source.component` 表明事件来源（如 `default-scheduler`、`kubelet`、`replicaset-controller`）

**故障演练意义**：
- 验证时应优先查看注入时间点附近的新事件，旧事件可能已被清理
- `kubectl get events --field-selector type!=Normal` 是快速发现异常的有效手段
- 某些故障（如 OOMKilled）会在 Pod Events、Node Events 和系统日志中同时留下痕迹，多源交叉验证更可靠

---

### Q23: 容器日志是如何存储和获取的？

**A23**: 容器日志由容器运行时（containerd/CRI-O）管理：

- 默认情况下，容器的 stdout/stderr 被写入节点的文件系统（通常位于 `/var/log/containers/` 或 `/var/log/pods/`）
- `kubectl logs` 实际上是通过 kubelet 从节点上读取这些日志文件
- 日志默认不会自动清理，但节点磁盘满时可能被清理
- 生产环境通常部署日志采集 DaemonSet（Fluentd/Fluent Bit）将日志发送到集中存储（ELK、Loki）

**故障演练意义**：
- `kubectl logs --previous` 可以获取已崩溃容器的最后日志，这对诊断 OOM、CrashLoopBackOff 至关重要
- 如果节点磁盘满，容器日志可能无法写入，此时 `kubectl logs` 返回空或报错
- 多容器 Pod 中需要通过 `-c <container>` 指定容器名获取对应日志

---

## 七、故障传播与影响分析

### Q24: 一个 Pod 故障通常会如何传播到整个系统？

**A24**: Pod 故障的传播路径（K8s 视角）：

```
Pod 故障
    ├── 容器退出 / 健康检查失败
    │       └── Pod Ready=False
    │               └── 从 Service Endpoints 移除
    │                       └── 流量不再发送到该 Pod
    │                               └── 如果剩余 Pod 不足以承载流量 → 服务降级/超时
    ├── Pod 被删除 / 节点故障
    │       └── ReplicaSet 创建新 Pod
    │               └── 新 Pod 启动需要时间（冷启动延迟）
    │                       └── 启动期间服务容量下降
    ├── CPU/内存资源压力
    │       └── 同节点其他 Pod 受影响（CPU throttle、内存竞争、甚至 OOMKill）
    │               └── 级联故障
    └── 网络故障（延迟/丢包/DNS）
            └── 应用层超时、重试、熔断触发
                    └── 依赖服务受影响（故障扩散）
```

**故障演练意义**：
- Agent 设计验证方案时，不应只验证"故障是否生效"，还应验证"故障的影响是否符合预期"
- 例如注入 Pod CPU 满载后，除了验证 CPU 指标，还应验证该 Pod 的响应延迟、健康检查状态、是否从 Endpoints 移除、同应用其他 Pod 的负载变化

---

### Q25: 如何区分"故障注入生效"和"故障造成了预期影响"？

**A25**: 这是 Layer 2 验证的核心问题，详见 `chaos-engineering-principles.md` Q7-Q8 中三层验证模型的完整阐述。

简要对照：

| 验证层次 | 含义 | 示例 |
|----------|------|------|
| **故障是否生效** | ChaosBlade 实验是否成功创建，目标资源是否被修改 | `blade_status` 返回成功；Pod 内出现 chaos 进程 |
| **是否出现预期现象** | 系统状态是否出现了故障场景描述中的现象 | Pod 内存接近 Limit；应用响应变慢 |
| **影响是否符合预期** | 故障的影响范围是否在可控范围内，没有意外扩散 | 只有目标 Pod 受影响，同节点其他 Pod 正常 |

---

## 八、常用术语中英对照

| 英文 | 中文 | 缩写 |
|------|------|------|
| Pod |  Pod（最小调度单元） | po |
| Node | 节点 | no |
| Namespace | 命名空间 | ns |
| Deployment | 部署 | deploy |
| ReplicaSet | 副本集 | rs |
| DaemonSet | 守护进程集 | ds |
| StatefulSet | 有状态集 | sts |
| Service | 服务 | svc |
| Endpoints | 端点 | ep |
| ConfigMap | 配置映射 | cm |
| Secret | 密钥 | - |
| PersistentVolume | 持久卷 | pv |
| PersistentVolumeClaim | 持久卷声明 | pvc |
| HorizontalPodAutoscaler | 水平 Pod 自动扩缩容器 | hpa |
| Event | 事件 | ev |
| Container | 容器 | - |
| Image | 镜像 | - |
| Label | 标签 | - |
| Selector | 选择器 | - |
| Taint | 污点 | - |
| Toleration | 容忍 | - |
| Affinity | 亲和性 | - |
| Probe | 探针 | - |
| ResourceQuota | 资源配额 | quota |
| LimitRange | 限制范围 | limits |
| Ingress | 入口（七层路由） | ing |
| NetworkPolicy | 网络策略 | netpol |
