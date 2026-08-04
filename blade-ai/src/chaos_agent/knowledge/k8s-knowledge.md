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
summary: "K8s architecture and chaos engineering context: Pod lifecycle, health checks, resource model, networking, fault propagation paths, verification layer overview. Resource abbreviation table included."
---

# Kubernetes Fundamentals Q&A (for fault drills)

This document walks through core Kubernetes concepts in Q&A form, to help the fault-drill Agent understand the K8s resource model, state semantics and fault-propagation mechanics — and therefore design more precise injection and verification plans.

---

## 1. Core resources and architecture

### Q1: What does the basic Kubernetes architecture look like? What are the control plane and worker nodes each responsible for?

**A1**: Kubernetes uses a master-worker architecture:

- **Control plane**: responsible for cluster-wide decisions and state management. It contains:
  - **kube-apiserver**: the single entry point for all component communication; exposes the REST API
  - **etcd**: distributed key-value store holding all cluster state
  - **kube-scheduler**: assigns new Pods to suitable Nodes
  - **kube-controller-manager**: runs the various controllers (Deployment Controller, Node Controller, Endpoint Controller, ...) that drive actual state toward desired state
  - **cloud-controller-manager** (optional): integrates with the cloud provider's API

- **Worker node**: runs the actual workloads. It contains:
  - **kubelet**: takes instructions from the apiserver and manages the Pod lifecycle on its node
  - **kube-proxy**: maintains the node's network rules, implementing Service load balancing
  - **container runtime (containerd/CRI-O)**: actually runs the containers

**Relevance to fault drills**:
- Control-plane components (especially etcd and kube-apiserver) are the cluster's "brain"; **injecting faults into them is strictly forbidden** (safety red line — see chaos-engineering-principles Q9.1).
- A node-level fault fundamentally affects that node's kubelet and container runtime, and through them every Pod on that node.
- Pod state changes are reported by kubelet to the apiserver, so there is a reporting delay (usually a few seconds).

---

### Q2: What is a Pod, and why is the Pod the smallest schedulable unit in Kubernetes?

**A2**: A Pod is the smallest deployable unit in Kubernetes, wrapping one or more containers (typically one main container plus some sidecars).

A Pod's core characteristics:
- **Shared network namespace**: every container in the same Pod shares the IP address and port space, and they talk to each other over `localhost`
- **Shared volumes**: containers in a Pod can mount the same Volume to share files
- **Lifecycle managed by the Pod**: containers can restart, but a Pod's IP usually changes after recreation (unless a StatefulSet is used)
- **Scheduled once**: after a Pod is scheduled onto a Node it is never migrated to another Node automatically (only by being deleted and recreated)

**Relevance to fault drills**:
- When injecting a Pod-level fault, the blast radius is confined inside that Pod (network, disk, CPU, memory)
- If a Pod is deleted or crashes, its owning Deployment/ReplicaSet automatically creates a replacement according to `spec.replicas`
- In a multi-container Pod you must confirm the fault is injected into the right container (ChaosBlade's `--container-names` flag)

---

### Q3: What is the relationship between Deployment, ReplicaSet and Pod?

**A3**: The three form a hierarchy of control:

```
Deployment (desired state: replicas=3, image=nginx:v2)
    └── ReplicaSet (created and managed by the Deployment; maintains 3 Pod replicas)
            ├── Pod-1
            ├── Pod-2
            └── Pod-3
```

- **Deployment**: the resource the user operates on directly; it declares the application's desired state (image version, replica count, update strategy). It achieves rolling updates and rollbacks by managing ReplicaSets.
- **ReplicaSet**: guarantees that the specified number of Pod replicas is always running. When a Pod is deleted, or a node failure loses a Pod, the ReplicaSet creates a new one automatically.
- **Pod**: the carrier that actually runs containers.

**Relevance to fault drills**:
- Deleting one Pod does not make the service unavailable, because the ReplicaSet immediately creates a new Pod (unless `terminationGracePeriod=0` plus a force delete are used together)
- When verifying a Pod-deletion fault, watch whether the Deployment's `availableReplicas` dips briefly and then recovers
- If the injection drives a Pod into persistent CrashLoopBackOff, the ReplicaSet keeps trying to rebuild it, but the Deployment's `readyReplicas` stays below `replicas`

---

### Q4: How do StatefulSet and Deployment differ, and why do StatefulSet fault drills need more caution?

**A4**: StatefulSet manages stateful applications (databases, message queues). The key differences from Deployment:

| Aspect | Deployment | StatefulSet |
|------|-----------|-------------|
| Pod naming | random hash suffix | ordered index (e.g. web-0, web-1, web-2) |
| Network identity | IP changes on every recreation | stable network identity via a Headless Service |
| Storage | usually ephemeral or shared storage | each Pod is bound to its own PVC and remounts the original PVC after recreation |
| Create/delete order | parallel | strictly ordered (create 0→N, delete N→0) |
| Scaling | parallel | one ordinal at a time |

**Relevance to fault drills**:
- A StatefulSet's PVC is not deleted when the Pod is, so data is retained. But if the injection corrupts data, consistency may be affected even after recovery.
- **Safety red line**: "no destructive experiments on a StatefulSet without a backup" — see chaos-engineering-principles Q9.2
- A StatefulSet Pod remounts its original PVC after recreation, so for disk-filling faults, if the filler files were not cleaned up, the problem can persist past recovery.

---

### Q5: What is a DaemonSet for, and what are its fault characteristics?

**A5**: A DaemonSet ensures one Pod replica runs on every Node (or on a specified subset). Typical uses: log collection (Fluentd/Fluent Bit), monitoring agents (Prometheus Node Exporter), network agents (Calico, Cilium).

**Relevance to fault drills**:
- A DaemonSet's scheduling logic differs from a Deployment's: it schedules per node, not per replica count
- When verifying a DaemonSet fault, check whether `status.desiredNumberScheduled` equals `status.numberReady`
- If the DaemonSet Pod on some Node fails, that node loses log collection or monitoring, but business Pods are usually unaffected (unless it is a networking DaemonSet)
- DaemonSet Pods usually need special privileges (hostNetwork, hostPath), so confirm the safety boundary before injecting

---

### Q6: How do Service and Endpoints work, and where do load-balancing anomalies usually occur?

**A6**: Service is Kubernetes's abstraction layer providing a single access point for a group of Pods:

- **Service**: defines the access policy (ClusterIP, NodePort, LoadBalancer, ExternalName) and the selector
- **Endpoints**: maintained automatically by the Endpoint Controller; contains the IP:Port list of every Pod that matches the Service selector AND is Ready
- **kube-proxy**: watches Endpoints changes on every Node and updates iptables/IPVS rules to forward traffic

**Relevance to fault drills**:
- A Service load-balancing anomaly usually shows up as: the Endpoints `addresses` list is empty or missing some Pod IPs
- Reasons Endpoints can be empty:
  1. The Pod fails its Readiness Probe (`ready=False`)
  2. The Pod's labels do not match the Service selector
  3. The Pod was deleted or is Terminating
  4. A network fault makes the Pod IP unreachable
- When verifying a load-balancing anomaly, check all of `kubectl get svc` (is the selector right), `kubectl get endpoints` (the backend list) and `kubectl get pods -l <selector>` (Pod state)

---

### Q7: How does HPA (Horizontal Pod Autoscaler) work, and how do you verify that HPA hit its ceiling?

**A7**: HPA adjusts a Deployment's/StatefulSet's replica count automatically based on metrics:

```
metrics-server collects metrics → HPA Controller computes the desired replica count → updates Deployment replicas
```

HPA supports five metric types (`autoscaling/v2` API):

| Type | Source API group | Description | Data provider |
|------|----------------|------|-----------|
| **Resource** | metrics.k8s.io | Pod CPU/Memory utilisation | metrics-server |
| **ContainerResource** | metrics.k8s.io | resource metrics for a specific container (stable in v1.30) | metrics-server |
| **Pods** | custom.metrics.k8s.io | average of a custom metric across Pods | Prometheus Adapter, etc. |
| **Object** | custom.metrics.k8s.io | a metric describing some other K8s object | Prometheus Adapter, etc. |
| **External** | external.metrics.k8s.io | external metrics unrelated to K8s (e.g. queue depth) | Prometheus Adapter, etc. |

> In fault drills the Resource type (CPU/Memory, provided by metrics-server) is by far the most common. But if the cluster runs Prometheus Adapter, HPA may also scale on custom metrics such as QPS or latency.

HPA's key fields:
- `spec.minReplicas` / `spec.maxReplicas`: replica-count floor and ceiling
- `spec.metrics`: the metrics that trigger scaling (Resource CPU/Memory, Pods, Object, External)
- `status.currentReplicas`: current replica count
- `status.desiredReplicas`: desired replica count

**Relevance to fault drills**:
- After injecting a CPU-fullload fault, if HPA is configured, the rising Pod CPU triggers a scale-up
- When `currentReplicas == maxReplicas` and CPU is still above the threshold, HPA has hit its ceiling and cannot scale further
- To verify an HPA-ceiling fault:
  1. `kubectl get hpa -o json` confirms `currentReplicas == spec.maxReplicas`
  2. `kubectl top pod` confirms CPU is still above target
  3. `kubectl get deployment` confirms the replica count stops growing

---

### Q8: What is a Namespace for, and why must fault drills run in an isolated Namespace?

**A8**: A Namespace is a logical isolation boundary in Kubernetes:

- Resource names must be unique within a Namespace; different Namespaces may reuse names
- RBAC permissions are usually partitioned per Namespace
- ResourceQuota and LimitRange take effect at Namespace level
- NetworkPolicy is usually defined per Namespace

**Relevance to fault drills**:
- **Isolation principle**: drills MUST run in an isolated test Namespace; injecting into system Namespaces such as `kube-system` or `kube-public` is strictly forbidden — see chaos-engineering-principles Q9.1
- A Namespace-level fault (e.g. deleting every Pod in the Namespace) has a controllable blast radius
- Scope queries with `-n <ns>` during verification so noise from other Namespaces does not interfere

---

### Q9: What are ConfigMap and Secret, and what are their fault scenarios?

**A9**: ConfigMap and Secret inject configuration data into Pods:

- **ConfigMap**: stores non-sensitive configuration data (config files, environment-variable values, command-line arguments)
- **Secret**: stores sensitive data (passwords, tokens, TLS certificates), base64-encoded in etcd

How they are consumed:
- Environment variables: `envFrom` / `env.valueFrom`
- File mounts: mounted as files via `volumeMounts`
- Command-line arguments: referenced as `$(ENV_NAME)`

**Relevance to fault drills**:
- The current skill catalogue focuses on runtime resource faults (CPU, memory, network, disk); ConfigMap/Secret faults belong to the configuration layer
- If configuration-fault scenarios are added later, possible verification approaches:
  - Modify a ConfigMap and observe whether the application hot-reloads (most applications do NOT reload mounted ConfigMap config automatically)
  - Delete a Secret and observe that Pods depending on it fail to start (`FailedMount` Event)
- After a ConfigMap/Secret update, already-running Pods do not notice the change; a rolling update or Pod restart is required

---

### Q10: What is the relationship between PersistentVolume (PV) and PersistentVolumeClaim (PVC)?

**A10**: PV and PVC decouple storage provisioning from storage consumption:

- **PV**: a storage resource in the cluster (provisioned by an administrator or dynamically by a StorageClass), representing a real piece of storage (NFS, cloud disk, local disk, ...)
- **PVC**: a Pod's request for storage, declaring the capacity and access mode it needs
- **StorageClass**: the template for dynamic provisioning (SSD, HDD, network storage, ...)

Binding flow:
```
Pod references a PVC → PVC matches/creates a PV → PV binds to the PVC → storage is mounted into the Pod
```

**Relevance to fault drills**:
- PVC Pending is the most common storage fault: possible causes include no matching PV, a missing StorageClass, or insufficient quota
- Verify PVC Pending with `kubectl get pvc -o json` and check `status.phase == "Pending"`
- A node disk failure can affect the availability of a local PV
- Storage-related Events (`FailedMount`, `FailedAttachVolume`) are key to diagnosing storage faults

---

## 2. Pod lifecycle and state semantics

### Q11: What are the Pod lifecycle phases, and what does each mean?

**A11**: A Pod's `status.phase` has five values:

| Phase | Meaning | Relevance to fault drills |
|-------|------|-------------|
| **Pending** | The Pod has been accepted by K8s but one or more containers have not been created yet. Usually because an image is being pulled, a volume is being mounted, or scheduling failed (insufficient resources, taint mismatch) | The central assertion point for Pending faults |
| **Running** | The Pod is bound to a Node and at least one container is running, starting or restarting | The normal running state — but Running does NOT mean Ready |
| **Succeeded** | All containers terminated normally (exitCode=0) and will not restart (e.g. a Job) | Rarely relevant to long-running services |
| **Failed** | All containers terminated and at least one exited abnormally (exitCode≠0) | The state after a process kill or application crash |
| **Unknown** | The Pod's state cannot be obtained (usually the node lost contact with the apiserver) | An indirect symptom of a Node fault |

**Important distinction**:
- `phase=Running` only means the container is running; it does NOT mean the application is healthy
- Whether the application is usable depends on `Ready=True` in `status.conditions` and the Readiness Probe result

---

### Q12: What container states exist, and how do you read them?

**A12**: Every container has three possible states:

| State | Meaning | Typical causes |
|-------|------|----------|
| **Waiting** | The container is not running yet | `ContainerCreating` (being created), `ImagePullBackOff` (image pull failed), `CrashLoopBackOff` (crashing repeatedly), `PodInitializing` (init containers still running) |
| **Running** | The container is running | normal operation |
| **Terminated** | The container has terminated | `Completed` (finished normally), `OOMKilled` (killed by OOM, exitCode=137), `Error` (abnormal exit), `ContainerCannotRun` (the container cannot run) |

> **Note**: `Evicted` is NOT a container-level state; it is a **Pod-level** state (`status.reason=Evicted`). An evicted Pod has `status.phase=Failed`, but its container-level `terminated.reason` is usually `Error` or `OOMKilled`, not `Evicted`.

**Key fields**:
- `restartCount`: how many times the container restarted. It increases when an injection crashes the container
- `lastState.terminated`: the reason and exit code of the previous termination
- `ready`: whether the container passed its Readiness Probe

**Exit-code cheat sheet**:
- 0: normal exit
- 1: generic error
- 137 (128+9): received SIGKILL (usually OOMKilled or a force termination)
- 143 (128+15): received SIGTERM (graceful termination)

---

### Q13: What Pod Conditions are there, and how do they relate to Pod availability?

**A13**: A Pod's `status.conditions` contains four conditions:

| Condition | Meaning |
|-----------|------|
| **PodScheduled** | The Pod has been scheduled onto a Node |
| **Initialized** | All init containers have completed |
| **ContainersReady** | All containers passed their Readiness Probe (or have no Probe configured) |
| **Ready** | The Pod can receive Service traffic (equals ContainersReady AND not being deleted) |

**Relevance to fault drills**:
- After injection, if the container health check fails, `ContainersReady` and `Ready` flip to `False`
- A Pod with `Ready=False` is removed from the Service Endpoints, so traffic stops going to it
- When verifying network/process faults, check both the `Ready` condition and the Endpoints change

---

### Q14: What is the Terminating state, and why do Pods get stuck in it?

**A14**: When a Pod is deleted (`kubectl delete pod`, or a Deployment scale-down) it enters Terminating:

1. The apiserver sets the Pod's `deletionTimestamp` to now + `terminationGracePeriodSeconds` (30s by default)
2. kubelet sends SIGTERM (signal 15) to the containers
3. The containers exit gracefully within the grace period
4. If a container has not exited, kubelet sends SIGKILL (signal 9) to force it
5. Resources (network, volumes) are cleaned up

**Common reasons for getting stuck in Terminating**:
- A process in the Pod does not respond to SIGTERM (an application that does not handle the signal properly)
- A Finalizer has not completed (some controllers must run cleanup logic before the Pod is deleted)
- A volume cannot be unmounted (NFS server unreachable, mount point busy)
- kubelet or the container runtime is faulty and cannot perform the deletion

**Relevance to fault drills**:
- Being stuck in Terminating is a fault scenario in its own right
- To verify: `metadata.deletionTimestamp` is non-empty and well in the past, yet the Pod still exists
- Force delete: `kubectl delete pod <pod> --force --grace-period=0` (bypasses graceful termination and removes the etcd record directly)

---

## 3. Health checks and self-healing

### Q15: How do Liveness Probe, Readiness Probe and Startup Probe differ?

**A15**: The three probes serve different health-check purposes:

| Probe | Purpose | Consequence of failure | Where it applies |
|------|------|----------|----------|
| **Liveness Probe** | Is the container still alive? | kubelet kills and restarts the container | Detecting deadlocks, infinite loops and other hung-application states |
| **Readiness Probe** | Is the container ready to receive traffic? | The Pod is removed from the Service Endpoints | Detecting "still starting up" or "a dependency is not ready" |
| **Startup Probe** | Has the application finished starting? | Disables the other probes so the startup phase is not killed by mistake | Applications with long startup times (e.g. JVM) |

**Probe types**:
- `httpGet`: send an HTTP request and check the status code
- `tcpSocket`: attempt a TCP connection
- `exec`: run a command inside the container and check its exit code
- `grpc`: gRPC health check (newer)

**Relevance to fault drills**:
- Network delay/loss faults can make the Liveness Probe fail and trigger a container restart (`restartCount` increases)
- CPU fullload can make a probe time out (if `timeoutSeconds` is short), causing unnecessary restarts
- When verifying, check the probe-failure Events (`Unhealthy`) in `describe pod`
- If the probe configuration is unreasonable (`periodSeconds` too short, `failureThreshold` too small), the fault's impact is amplified

---

### Q16: What self-healing mechanisms does Kubernetes have, and how do they affect what a drill observes?

**A16**: Kubernetes has several built-in self-healing mechanisms:

| Mechanism | Trigger | Behaviour | Impact on fault drills |
|------|----------|------|-----------------|
| **Container restart** | Liveness Probe failure or abnormal container exit | kubelet decides whether to restart based on `restartPolicy` (Always/OnFailure/Never) | A Pod-level fault may cause repeated restarts — watch `restartCount` |
| **Pod recreation** | The Deployment/ReplicaSet notices too few Pods | Creates a new Pod to replace the lost one | After a Node fault or Pod deletion, the new Pod is rebuilt on another Node |
| **Rescheduling** | A Pod is Pending | The scheduler tries to place it on an available Node | A Pending fault caused by insufficient resources recovers automatically once resources free up |
| **Node eviction** | A Node goes NotReady or under resource pressure | The controller marks that node's Pods for deletion and rebuilds them elsewhere | After a Node fault, Pods drift automatically |
| **Endpoint update** | A Pod's Ready status changes | The Endpoint Controller updates the Service backend list | A Pod with Ready=False is pulled out of traffic automatically |

**Relevance to fault drills**:
- A verification plan MUST account for the self-healing time window. For example, a new Pod starts within 5-10s of a Pod deletion, so verification has to catch that window
- Some faults (e.g. CPU fullload) that trigger Liveness Probe failures cause the container to restart repeatedly — the observed symptom is then "repeated restarts", not "high CPU"
- Self-healing can mask a fault's real impact; the Agent must see through the self-healing layer to the underlying problem via Events, logs and metrics together

---

## 4. Resource model and scheduling

### Q17: What is the difference between Request and Limit, and what role do they play in fault drills?

**A17**: Request and Limit are the two dimensions of a container's resource declaration:

| Dimension | Meaning | Effect |
|------|------|------|
| **Request** | The amount of resource the container is guaranteed to get | The scheduler uses Request to decide which Node the Pod lands on (a Node's Allocatable must be >= the sum of all its Pods' Requests) |
| **Limit** | The maximum amount the container may use | If the container exceeds the Limit, CPU is throttled and memory triggers OOMKilled |

**Example**:
```yaml
resources:
  requests:
    cpu: "100m"      # 0.1 core
    memory: "128Mi"  # 128 MB
  limits:
    cpu: "500m"      # 0.5 core
    memory: "256Mi"  # 256 MB
```

**Relevance to fault drills**:
- **Verifying Pod CPU fullload**: in `top pod`, CPU usage approaches the Limit (not the Request). ChaosBlade's `pod-cpu fullload` drives the Pod's CPU toward its Limit.
- **Verifying Pod memory pressure**: when memory usage nears the Limit, the container is OOMKilled (exitCode 137). With no Limit set, the Pod can consume memory until the node runs out.
- **Pending fault (insufficient resources)**: when the sum of all Pods' CPU/Memory Requests on a node exceeds the node's Allocatable, a new Pod stays Pending.

---

### Q18: What are node Taints and Tolerations, and how do they affect scheduling?

**A18**: A Taint is a "repelling label" on a node; a Toleration is a Pod's declaration that it can tolerate one:

- If a node has a Taint and a Pod has no matching Toleration, the Pod cannot be scheduled there
- Even for a Pod already running on the node, certain Taints (e.g. `NoExecute`) cause it to be evicted

Common system Taints:
- `node.kubernetes.io/not-ready`: node not ready
- `node.kubernetes.io/unreachable`: node unreachable
- `node.kubernetes.io/disk-pressure`: disk pressure
- `node.kubernetes.io/memory-pressure`: memory pressure
- `node.kubernetes.io/pid-pressure`: PID pressure
- `node.kubernetes.io/network-unavailable`: network unavailable

**Relevance to fault drills**:
- Node-level faults (disk full, high memory) make kubelet add the corresponding Taint automatically, which then evicts the node's Pods
- When verifying a Node fault, watch the node's Taint changes and the Pod eviction Events
- Some DaemonSets (e.g. monitoring agents) carry Tolerations for these Taints so they keep running even on a faulted node

---

### Q19: What are Affinity and Anti-Affinity?

**A19**: Affinity controls which nodes a Pod prefers, or which Pods it prefers to sit with:

- **NodeAffinity**: the Pod prefers nodes carrying specific labels (e.g. `disktype=ssd`)
- **PodAffinity**: the Pod prefers to be scheduled onto the same node as Pods carrying specific labels
- **PodAntiAffinity**: the Pod prefers to be spread away from Pods carrying specific labels (e.g. "Pods of the same Deployment should not land on the same node")

**Relevance to fault drills**:
- Anti-affinity affects where a Pod is rescheduled on recreation. For example, after a Node fault, if anti-affinity prevents other nodes from accepting the rebuilt Pod, the Pod stays Pending
- When verifying high-availability scenarios, check whether anti-affinity is in effect (the Pod count within a single failure domain)

---

## 5. Networking model

### Q20: What are the core principles of the Kubernetes networking model?

**A20**: The Kubernetes networking model requires:

1. **Every Pod has its own IP address** (Pod IPs are routable within the cluster)
2. **Pods can communicate directly** with no NAT (whether or not they are on the same Node)
3. **Node agents (kubelet, kube-proxy) can reach every Pod**

The implementation is the CNI plugin's job (Calico, Cilium, Flannel, Weave, ...); different CNIs have different network topologies and fault behaviour.

**Relevance to fault drills**:
- Pod network faults (delay, loss, DNS) are realised by the CNI, but ChaosBlade's network injection operates inside the Pod's network namespace, independent of the underlying CNI
- A Service's ClusterIP is a virtual IP; whether it is reachable depends on the kube-proxy mode:
  - **iptables mode**: the ClusterIP is not bound to any network interface and exists only in iptables NAT rules, so **a Pod cannot ping the ClusterIP** (ICMP does not hit the NAT rules) — but the service is reachable over TCP/UDP via `ClusterIP:Port`
  - **IPVS mode**: the ClusterIP is bound to the `kube-ipvs0` dummy interface, so **a Pod CAN ping the ClusterIP** (though the echo comes from the local node and is not forwarded to a backend Pod); TCP/UDP connections still go through IPVS load balancing
  - **Recommendation**: in either mode, verify service availability with `curl ClusterIP:Port` or the Pod DNS name, not by pinging the ClusterIP
- When verifying a network fault, test connectivity to a concrete Pod IP from inside a Pod, not connectivity to a ClusterIP

---

### Q21: How does DNS work in Kubernetes?

**A21**: Cluster DNS (usually CoreDNS) provides in-cluster service discovery:

- **Service DNS**: `<service>.<namespace>.svc.cluster.local` → ClusterIP
- **Pod DNS** (must be enabled): `<pod-ip>.<namespace>.pod.cluster.local`
- **Headless Service**: DNS returns the backend Pod IP list directly (used by StatefulSet)

CoreDNS runs in the cluster as a Deployment/DaemonSet, usually in the `kube-system` namespace.

**Relevance to fault drills**:
- DNS fault injection (ChaosBlade `pod-network dns`) modifies the Pod's `/etc/hosts` file (adding domain-IP entries marked with a `#chaosblade` comment), NOT `/etc/resolv.conf`
- To verify a DNS fault, use `cat /etc/hosts` (confirm the #chaosblade entry) and `ping <domain>` (confirm it resolves to the forged IP; on glibc images `getent hosts <domain>` also works, but Alpine/musl images have no `getent`). **Do NOT use `nslookup`/`dig`** — they bypass /etc/hosts and cannot detect ChaosBlade DNS hijacking
- CoreDNS itself lives in `kube-system`, so **injecting faults into it is strictly forbidden** (safety red line)

---

## 6. Events and logs

### Q22: What is the lifecycle and reliability of Kubernetes Events?

**A22**: Events are an important diagnostic source in Kubernetes, with these characteristics:

- Events are stored in etcd and retained for **1 hour** by default (governed by the event aggregator; tunable via `--event-ttl`)
- Events of the same kind are aggregated (the `count` field is the number of occurrences)
- An Event's `type` is either `Normal` or `Warning`
- An Event's `source.component` indicates its origin (e.g. `default-scheduler`, `kubelet`, `replicaset-controller`)

**Relevance to fault drills**:
- During verification, look first at new events around the injection timestamp; older events may already have been purged
- `kubectl get events --field-selector type!=Normal` is an effective way to surface anomalies quickly
- Some faults (e.g. OOMKilled) leave traces in Pod Events, Node Events AND system logs simultaneously; cross-checking multiple sources is more reliable

---

### Q23: How are container logs stored and retrieved?

**A23**: Container logs are managed by the container runtime (containerd/CRI-O):

- By default a container's stdout/stderr is written to the node's filesystem (usually under `/var/log/containers/` or `/var/log/pods/`)
- `kubectl logs` actually reads those log files from the node via kubelet
- Logs are not auto-purged by default, but may be cleaned up when the node's disk fills
- Production clusters usually run a log-collection DaemonSet (Fluentd/Fluent Bit) shipping logs to central storage (ELK, Loki)

**Relevance to fault drills**:
- `kubectl logs --previous` retrieves the final logs of an already-crashed container, which is essential for diagnosing OOM and CrashLoopBackOff
- If the node's disk is full, container logs may fail to be written, so `kubectl logs` returns empty or errors
- In a multi-container Pod, name the container with `-c <container>` to get its logs

---

## 7. Fault propagation and impact analysis

### Q24: How does a single Pod fault typically propagate through the whole system?

**A24**: The propagation path of a Pod fault (from the K8s point of view):

```
Pod fault
    ├── container exits / health check fails
    │       └── Pod Ready=False
    │               └── removed from the Service Endpoints
    │                       └── traffic stops going to that Pod
    │                               └── if the remaining Pods cannot carry the load → service degradation / timeouts
    ├── Pod deleted / node failure
    │       └── ReplicaSet creates a new Pod
    │               └── the new Pod takes time to start (cold-start latency)
    │                       └── service capacity is reduced during startup
    ├── CPU/memory resource pressure
    │       └── other Pods on the same node are affected (CPU throttling, memory contention, even OOMKill)
    │               └── cascading failure
    └── network fault (delay / loss / DNS)
            └── application-layer timeouts, retries, circuit breakers trip
                    └── dependent services are affected (the fault spreads)
```

**Relevance to fault drills**:
- When designing a verification plan, the Agent should not only verify "did the fault take effect" but also "is the fault's impact as expected"
- For example, after injecting Pod CPU fullload, beyond the CPU metric also verify that Pod's response latency, its health-check status, whether it was removed from Endpoints, and the load shift on the application's other Pods

---

### Q25: How do you distinguish "the injection took effect" from "the fault caused the expected impact"?

**A25**: This is the core question of Layer 2 verification; see the full three-layer verification model in `chaos-engineering-principles.md` Q7-Q8.

A brief comparison:

| Verification layer | Meaning | Example |
|----------|------|------|
| **Did the fault take effect** | Was the ChaosBlade experiment created successfully, and was the target resource modified | `blade_status` returns success; a chaos process appears inside the Pod |
| **Did the expected symptom appear** | Did the system state exhibit the symptom described in the fault scenario | Pod memory approaches its Limit; the application slows down |
| **Is the impact as expected** | Is the fault's blast radius within the controllable range, with no unintended spread | Only the target Pod is affected; other Pods on the same node are healthy |

---

## 8. Resource abbreviations

The short names accepted by `kubectl` for the resources referenced throughout this document:

| Resource | Short name |
|------|------|
| Pod (the smallest schedulable unit) | po |
| Node | no |
| Namespace | ns |
| Deployment | deploy |
| ReplicaSet | rs |
| DaemonSet | ds |
| StatefulSet | sts |
| Service | svc |
| Endpoints | ep |
| ConfigMap | cm |
| Secret | - |
| PersistentVolume | pv |
| PersistentVolumeClaim | pvc |
| HorizontalPodAutoscaler | hpa |
| Event | ev |
| Container | - |
| Image | - |
| Label | - |
| Selector | - |
| Taint | - |
| Toleration | - |
| Affinity | - |
| Probe | - |
| ResourceQuota | quota |
| LimitRange | limits |
| Ingress (L7 routing) | ing |
| NetworkPolicy | netpol |
