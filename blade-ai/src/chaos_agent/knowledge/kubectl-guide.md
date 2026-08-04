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

# kubectl Complete Reference (for fault drills)

This handbook targets the fault-drill Agent. It systematically covers every kubectl capability the Agent has, so it can design precise, executable kubectl verification plans during injection verification (Layer 2) and the recovery-verification phase.

---

## 1. Overview of the kubectl tooling available to the Agent

The Agent has ONE unified `kubectl` tool; the sub-command is selected via the `subcommand` parameter:

| subcommand | Purpose | Typical v_args |
|------|------|----------|
| `get` | Query any K8s resource (Pod/Node/Deploy/PVC/...), supports -o json/yaml/wide/name/jsonpath | `"pods -n <ns> -o json"` |
| `describe` | Show a resource's detailed description (including Events) | `"pod <pod> -n <ns>"` |
| `exec` | Run a command inside a Pod's container | `"<pod> -n <ns> -- <command>"` |
| `patch` | Modify K8s resource fields (supports --type=strategic/merge/json) | `"pod <pod> -n <ns> --type=json -p '...'`" |
| `delete` | Delete a K8s resource (supports `--force --grace-period=0` for force delete) | `"pod <pod> -n <ns>"` |
| `logs` | View container logs | `"<pod> -n <ns> --tail=50"` |
| `top` | View live resource metrics (CPU/memory) | `"pod -n <ns>"` |
| `scale` | Change a workload's replica count | `"deployment <name> -n <ns> --replicas=0"` |
| `set` | Set resource fields (image/resources/env/serviceaccount/selector) | `"image deployment/<name> -n <ns> nginx=nginx:broken"` |
| `cordon` | Mark a node unschedulable | `"<node>"` |
| `uncordon` | Make a node schedulable again | `"<node>"` |
| `taint` | Manage node taints | `"nodes <node> key=value:NoSchedule"` |
| `label` | Add/remove/change resource labels (`--overwrite` overwrites an existing one; `key-` removes) | `"node <node> workload-affinity=<app> --overwrite"` |
| `annotate` | Add/remove/change resource annotations | `"node <node> <key>=<value> --overwrite"` |
| `drain` | Evict Pods off a node (node-maintenance drill); recover with `uncordon` | `"<node> --ignore-daemonsets --delete-emptydir-data --grace-period=30"` |
| `debug` | Create a temporary debug container on a node to reach the host /host filesystem (two-step; MUST include `-- sleep 3600`) | `"node/<node> --image=busybox -- sleep 3600"` |

> ⚠️ **Two forbidden `drain` flags**: `--force` (deletes bare Pods with no controller — neither `uncordon` nor any controller will recreate them, so the Pod is gone permanently) and `--disable-eviction` (bypasses the eviction API, which means bypassing PodDisruptionBudget — and PDB is exactly the guarantee the drill is meant to verify) are rejected by the guard. If drain fails because of a bare Pod, **no Pod has been evicted at that point** (atomic failure); treat that node as an unsuitable drain target instead of forcing it through with `--force`.
> `--delete-emptydir-data` **IS allowed**: an emptyDir's lifecycle is already tied to its Pod, so evicting the Pod necessarily discards it — that is inherent to Pod deletion. In real clusters a great many Pods mount an emptyDir, and without this flag drain simply refuses to evict.

> ⚠️ **`edit` / `replace` are unavailable**: `edit` needs an interactive editor and `replace` is a whole-object overwrite; neither is in the allowed sub-command list — express the change as a `patch`, or use a dedicated verb such as `label` / `annotate`.

> ⚠️ **Beware kubectl debug version skew**: `kubectl debug node/` depends on the EphemeralContainers API, which has breaking changes across K8s versions. When the local kubectl version differs from the cluster API server by more than ±1 minor version, `kubectl debug` may return a "NotFound" error. In that case host-filesystem access is not obtainable through the kubectl tool, and you must fall back to API-level checks (`kubectl describe node` for DiskPressure, `kubectl get events` for disk-pressure events). **Note**: `kubectl run` is NOT in the allowed sub-command list and cannot be used as a fallback.

Every tool supports the standard flags `--kubeconfig`, `-n/--namespace`, `-l/--selector`, `-o` and so on, passed through the `v_args` string.

**Note**: the `kubectl run` sub-command is not in the allow-list, so the Agent cannot use it. To create a temporary debug Pod, only `kubectl debug` is available.

**Key principle**: when verifying, prefer `kubectl(subcommand="get")` with `-o json` to obtain structured data for assertions; use `kubectl(subcommand="describe")` to inspect Events and Conditions for qualitative judgement; use `kubectl(subcommand="exec")` to enter the container for process-level / filesystem-level verification; use `kubectl(subcommand="top")` for live resource metrics.

---

## 2. Common flags and query syntax

### 2.1 Global flags (available on every command)

| Flag | Description | Example |
|------|------|------|
| `--kubeconfig <path>` | Specify cluster credentials | `--kubeconfig ~/.kube/config` |
| `--context <ctx>` | Specify the kubeconfig context | `--context prod-cluster` |
| `-n <ns>` / `--namespace <ns>` | Specify the namespace | `-n default` |
| `--all-namespaces` / `-A` | Query across all namespaces | `get pods -A` |
| `-l <selector>` / `--selector <selector>` | Label selector | `-l app=nginx,tier=frontend` |
| `--field-selector <expr>` | Field selector | `--field-selector status.phase=Running` |
| `-o <format>` | Output format | `-o json`, `-o yaml`, `-o wide`, `-o name` |
| `--sort-by <jsonpath>` | Sort by a field | `--sort-by=.status.phase` |

### 2.2 Output formats in detail

The Agent MUST understand when each output format applies:

- **`-o json`**: returns the complete JSON object with every field of the resource. Best for programmatic assertions (e.g. checking `.status.phase == "Running"`, `.status.containerStatuses[0].restartCount > 0`). Use `kubectl(subcommand="get")` with `-o json` to obtain structured data.
- **`-o yaml`**: identical content to JSON, just formatted as YAML. More readable, but equally hard to parse. Available on the generic `kubectl` tool.
- **`-o wide`**: adds extra columns to the default table (e.g. NODE and IP for a Pod; OS-IMAGE and KERNEL-VERSION for a Node). Good for a quick overview.
- **`-o name`**: returns only a `<resource>/<name>` list — good for batch processing.
- **default (table)**: human-readable, but error-prone for the Agent to parse; prefer JSON.

### 2.3 Label selectors

Labels are K8s's most central filtering mechanism; fault drills locate target Pods/Nodes through labels extensively.

```bash
# equals
-l app=nginx
# not equals
-l app!=nginx
# multiple conditions (AND)
-l app=nginx,tier=frontend
# label exists
-l 'release'
# label does not exist
-l '!release'
# set matching
-l 'app in (nginx, apache)'
-l 'tier notin (backend)'
```

**Agent verification tip**: record the target Pod's labels before injection, then after injection use the same label selector to confirm whether the affected Pod set changed.

### 2.4 Field selectors

Filter by resource field value; supported operators: `=`, `==`, `!=`.

```bash
# filter by Pod status
kubectl get pods --field-selector status.phase=Running
kubectl get pods --field-selector status.phase!=Succeeded

# filter by Node status
kubectl get nodes --field-selector spec.unschedulable=false

# commonly available fields
# Pod: metadata.name, metadata.namespace, status.phase, spec.nodeName
# Node: metadata.name, status.phase
# Event: involvedObject.kind, involvedObject.name, type, reason
```

---

## 3. Sub-commands in detail, with verification usage

### 3.1 get — read resource state

**Core capability**: fetch the current state of any K8s resource; the most-used command during verification.

**Resource types available to the Agent** (high-frequency in fault drills):

| Resource type | Short name | Verification use |
|----------|------|----------|
| `pods` | `po` | Pod status, restart count, container state |
| `nodes` | `no` | Node readiness, capacity, taints |
| `deployments` | `deploy` | Replica count, update strategy, available replicas |
| `replicasets` | `rs` | Desired / actual replica count |
| `daemonsets` | `ds` | Desired / scheduled Pod count |
| `statefulsets` | `sts` | Replica count, partitioned-update status |
| `services` | `svc` | ClusterIP, port mapping, Endpoints |
| `endpoints` | `ep` | Backend Pod IP list (verifies service discovery) |
| `events` | `ev` | Event stream (OOMKilled, FailedScheduling, Evicted) |
| `persistentvolumeclaims` | `pvc` | Binding status, capacity |
| `persistentvolumes` | `pv` | Available capacity, reclaim policy |
| `configmaps` | `cm` | Configuration data |
| `secrets` | - | Secret reference status |
| `horizontalpodautoscalers` | `hpa` | Current/target/max replica count, metrics |
| `jobs` / `cronjobs` | - | Completion status |
| `all` | - | All core resources in a namespace |

**Verification examples (fault-drill scenarios)**:

```bash
# Verify Pod OOM: inspect the container exit code and restart count
kubectl get pod <pod> -n <ns> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.exitCode}'

# Verify high Pod CPU: find which Node the Pod is on
kubectl get pod <pod> -n <ns> -o wide

# Verify Deployment replica mismatch: compare desired / available
kubectl get deployment <name> -n <ns> -o jsonpath='{.status.replicas} {.status.availableReplicas}'

# Verify Service load-balancing anomaly: check whether Endpoints is empty
kubectl get endpoints <svc> -n <ns> -o json

# Verify Node unavailable: check the Ready condition
kubectl get node <node> -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'

# Verify HPA hit its ceiling: current replicas vs max replicas
kubectl get hpa <name> -n <ns> -o jsonpath='{.status.currentReplicas} {.spec.maxReplicas}'

# Verify PVC Pending: check the status
kubectl get pvc <name> -n <ns> -o jsonpath='{.status.phase}'

# List recent abnormal events
kubectl get events -n <ns> --sort-by=.lastTimestamp --field-selector type!=Normal
```

**Common JSONPath expressions**:

```
{.metadata.name}                          # name
{.metadata.namespace}                     # namespace
{.metadata.labels}                        # all labels
{.metadata.labels.app}                    # one label's value
{.spec.nodeName}                          # the Node the Pod is on
{.status.phase}                           # Pod lifecycle phase
{.status.conditions[?(@.type=="Ready")].status}     # Ready condition
{.status.containerStatuses[0].restartCount}         # restart count
{.status.containerStatuses[0].lastState.terminated.reason}   # previous termination reason
{.status.containerStatuses[0].state.waiting.reason}          # waiting reason
{.spec.containers[0].resources.limits.cpu}                   # CPU limit
{.spec.containers[0].resources.limits.memory}                # memory limit
{.status.conditions[?(@.type=="MemoryPressure")].status}    # node memory pressure
```

### 3.2 describe — detailed diagnosis (including Events)

**Core capability**: shows a resource's full state, Conditions and recent Events. Events are a goldmine for fault diagnosis.

**Where the key information lives**:

- **Pod describe**:
  - `Status`: current phase (Running / Pending / CrashLoopBackOff / Terminating)
  - `Conditions`: PodScheduled / Initialized / ContainersReady / Ready
  - `Containers`: State (Running / Waiting / Terminated), Last State, Restart Count, Limits/Requests
  - `Events`: scheduling events, image-pull events, health-check failures, OOMKilled, Evicted

- **Node describe**:
  - `Conditions`: Ready / MemoryPressure / DiskPressure / PIDPressure / NetworkUnavailable
  - `Capacity` / `Allocatable`: CPU, memory, max Pod count
  - `Non-terminated Pods`: every Pod currently running on that node
  - `Events`: node failures, Pod eviction events

- **Deployment describe**:
  - `Replicas`: Desired / Current / Updated / Available
  - `Conditions`: Progressing / Available / ReplicaFailure
  - `Events`: scaling events, rolling-update events

**Verification examples**:

```bash
# Verify Pod image-pull failure: look for ImagePullBackOff / ErrImagePull in Events
kubectl describe pod <pod> -n <ns>

# Verify Node disk pressure: look at DiskPressure under Conditions
kubectl describe node <node>

# Verify Pod eviction: look for Evicted and the eviction reason in Events
kubectl describe pod <pod> -n <ns>
```

### 3.3 top — live resource metrics

**Core capability**: view live CPU (millicores) and memory (bytes/MiB) usage for Pods/Nodes. Requires **metrics-server** to be installed in the cluster.

**Key insight**: `top` shows **actual usage**, not the Limit. In fault drills it is commonly used to verify that a resource-pressure injection took effect.

```bash
# CPU/memory for every Pod in a namespace
kubectl top pod -n <ns>

# a specific Pod
kubectl top pod <pod> -n <ns>

# all nodes
kubectl top node

# a specific node
kubectl top node <node>

# sort by CPU
kubectl top pod -n <ns> --sort-by=cpu

# sort by memory
kubectl top pod -n <ns> --sort-by=memory
```

**Mapping to verification scenarios**:

| Fault type | What to check with top |
|----------|-----------|
| Pod CPU fullload | in `top pod`, the target Pod's CPU is near its Limit or abnormally high |
| Pod memory pressure | in `top pod`, the target Pod's memory is near its Limit |
| High Node CPU | in `top node`, the target Node's CPU utilisation spikes |
| High Node memory | in `top node`, the target Node's memory utilisation spikes |

### 3.4 logs — container logs

**Core capability**: fetch a container's stdout/stderr to verify whether the application layer has noticed the fault (errors, timeouts, connection resets).

```bash
# current logs
kubectl logs <pod> -n <ns>

# last N lines
kubectl logs <pod> -n <ns> --tail=100

# with timestamps
kubectl logs <pod> -n <ns> --timestamps

# pick a container in a multi-container Pod
kubectl logs <pod> -n <ns> -c <container>

# logs from the PREVIOUSLY crashed container (critical!)
kubectl logs <pod> -n <ns> --previous

# follow live (observe during injection)
kubectl logs <pod> -n <ns> -f --tail=50

# logs within a time window
kubectl logs <pod> -n <ns> --since=5m
```

**Fault-to-log-keyword mapping**:

| Fault symptom | Signals that may appear in logs |
|----------|---------------------|
| OOMKill | `Killed`, `OOM`, `out of memory`, `signal 9` |
| Latency from CPU fullload | `timeout`, `deadline exceeded`, `slow`, `latency` |
| Network delay | `timeout`, `i/o timeout`, `connection timed out` |
| Network packet loss | `connection reset`, `broken pipe`, `no route to host` |
| DNS fault | `lookup failed`, `no such host`, `resolve` |
| Disk full | `no space left`, `write error`, `disk full` |
| Process killed | `signal 9`, `signal 15`, `terminated` |
| Image pull failure | `ImagePullBackOff`, `ErrImagePull`, `not found` |

### 3.5 exec — run commands inside a container

**Core capability**: run a command inside a running container for deep process-level, filesystem-level and network-level verification.

> ⚠️ **Important limitation**: `kubectl exec` **does NOT support `-l/--selector`** (exec can only attach to a single container of a single Pod; it cannot fan out by label). First get the concrete Pod name with `kubectl get pods -l <selector>`, then exec into that Pod.

```bash
# list processes inside the container (confirm whether the chaos process exists)
kubectl exec <pod> -n <ns> -- ps aux

# the top CPU-consuming processes inside the container
kubectl exec <pod> -n <ns> -- top -b -n 1

# container memory information
kubectl exec <pod> -n <ns> -- cat /proc/meminfo

# container disk usage
kubectl exec <pod> -n <ns> -- df -h

# network connection state
kubectl exec <pod> -n <ns> -- ss -tlnp
kubectl exec <pod> -n <ns> -- netstat -tlnp

# test connectivity (verifies a network fault)
kubectl exec <pod> -n <ns> -- ping -c 3 <target-ip>
kubectl exec <pod> -n <ns> -- curl -v --connect-timeout 5 <target-url>

# check whether a file exists (verifies disk filling)
kubectl exec <pod> -n <ns> -- ls -la /data/

# read a specific file inside the container
kubectl exec <pod> -n <ns> -- cat /etc/resolv.conf
```

**Important caveats**:
- Some minimal images (distroless, scratch) have no shell, ps, top or curl, so exec fails. In that case verify via `kubectl top` or `kubectl get pod -o json` instead.
- exec runs inside the container's namespaces, so it shows the container's view, not the host's.

### 3.6 delete — delete resources

**Core capability**: delete a resource. In fault drills this is mainly for cleaning up test resources — **do not use unless necessary**.

```bash
# delete a specific Pod (its ReplicaSet/Deployment will recreate it)
kubectl delete pod <pod> -n <ns>

# delete a batch of Pods by label
kubectl delete pod -n <ns> -l app=nginx

# force delete (use when stuck in Terminating)
kubectl delete pod <pod> -n <ns> --force --grace-period=0
```

---

## 4. Events deep dive: the golden data source for fault diagnosis

K8s Events are the most-overlooked yet highest-value information source in fault verification. Events record every important state change in the cluster.

### 4.1 Common ways to query Events

```bash
# recent events in a namespace (sorted by time)
kubectl get events -n <ns> --sort-by=.lastTimestamp

# abnormal events only (exclude Normal)
kubectl get events -n <ns> --field-selector type!=Normal --sort-by=.lastTimestamp

# events related to a specific Pod
kubectl get events -n <ns> --field-selector involvedObject.name=<pod>

# events related to a specific Node
kubectl get events --field-selector involvedObject.kind=Node,involvedObject.name=<node>

# events with a specific reason
kubectl get events -n <ns> --field-selector reason=FailedScheduling
```

### 4.2 Fault-scenario to Event mapping

When designing a verification plan, the Agent should look in Events for the signal matching the fault type:

| Fault scenario | Possible Event Reason | Event Message keywords |
|----------|------------------------|---------------------|
| Pod Pending (insufficient resources) | `FailedScheduling` | `Insufficient cpu`, `Insufficient memory` |
| Pod OOMKilled | `OOMKilling` | `Memory cgroup out of memory` |
| Pod image-pull failure | `Failed` | `ErrImagePull`, `ImagePullBackOff` |
| Node memory pressure | `NodeHasInsufficientMemory` | `insufficient memory` |
| Node disk pressure | `NodeHasDiskPressure` | `disk pressure` |
| Node NotReady | `NodeNotReady` | `Kubelet stopped posting` |
| Pod evicted | `Evicted` | `The node was low on resource` |
| Health-check failure | `Unhealthy` | `Liveness probe failed`, `Readiness probe failed` |
| Container start failure | `BackOff` | `CrashLoopBackOff` |
| Mount failure | `FailedMount` | `Unable to mount`, `volume not found` |

---

## 5. JSON output: key-field quick reference

When using `kubectl get ... -o json`, these are the fields most often asserted on during fault verification:

### 5.1 Pod key fields

```json
{
  "metadata": {
    "name": "...",
    "namespace": "...",
    "labels": { "app": "...", "version": "..." },
    "deletionTimestamp": "..."   // non-empty means it is Terminating
  },
  "spec": {
    "nodeName": "...",            // the Node the Pod is on
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
    "reason": "...",   // e.g. Evicted
    "message": "..."   // e.g. The node was low on resource: memory
  }
}
```

**Key assertion points**:
- `status.phase`: Running = healthy, Pending = being scheduled / insufficient resources, Failed = failed
- `status.containerStatuses[].restartCount`: >0 means there were restarts; may increase after injection
- `status.containerStatuses[].state.waiting.reason`: `ImagePullBackOff`, `CrashLoopBackOff`
- `status.containerStatuses[].state.terminated.reason`: `OOMKilled` (exitCode 137), `Error`
- `status.conditions[]`: `Ready=False` means the Pod is not ready
- `metadata.deletionTimestamp`: non-empty means the Pod is Terminating

### 5.2 Node key fields

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

**Key assertion points**:
- `conditions[?(@.type=="Ready")].status`: `True` = node healthy, `False` = node unavailable
- `conditions[?(@.type=="MemoryPressure")].status`: `True` = node under memory pressure
- `conditions[?(@.type=="DiskPressure")].status`: `True` = node under disk pressure

### 5.3 Deployment key fields

```json
{
  "metadata": { "name": "..." },
  "spec": {
    "replicas": 3,
    "strategy": { "type": "RollingUpdate" }
  },
  "status": {
    "replicas": 3,          // total replicas
    "updatedReplicas": 3,   // updated replicas
    "readyReplicas": 2,     // ready replicas (< spec.replicas indicates a problem)
    "availableReplicas": 2, // available replicas
    "unavailableReplicas": 1,
    "conditions": [
      { "type": "Available", "status": "True" },
      { "type": "Progressing", "status": "True" }
    ]
  }
}
```

**Key assertion points**:
- `status.readyReplicas < spec.replicas`: some Pods are not ready (possibly caused by the injection)
- `status.availableReplicas < spec.replicas`: some Pods are unavailable
- `conditions`: inspect the Available / Progressing status

### 5.4 Service / Endpoints key fields

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

Endpoints:
```json
{
  "metadata": { "name": "..." },
  "subsets": [{
    "addresses": [{ "ip": "10.244.1.5" }],   // backend Pod IPs
    "notReadyAddresses": [{ "ip": "10.244.1.6" }],  // not-ready backends
    "ports": [{ "port": 8080 }]
  }]
}
```

**Key assertion points**:
- `subsets[].addresses` empty: the Service has no usable backend (verifies a load-balancing anomaly)
- `subsets[].notReadyAddresses` non-empty: some backends are not ready but are still retained

### 5.5 Event key fields

```json
{
  "type": "Warning|Normal",
  "reason": "FailedScheduling",
  "message": "0/3 nodes are available: insufficient memory",
  "involvedObject": { "kind": "Pod", "name": "..." },
  "count": 5,              // occurrence count
  "firstTimestamp": "...",
  "lastTimestamp": "...",
  "source": { "component": "default-scheduler" }
}
```

---

## 6. Related documents

- Fault scenario → kubectl verification command combinations: see `fault-verification-strategies.md` (Q3-Q11 give complete plans per fault type)
- Verification-command design principles (targeted / quantifiable / cross-sourced / time-ordered / reversible): see `fault-verification-strategies.md` Q2
- Compact sub-command recipes and a JSONPath cheat sheet (for the LLM to pull on demand): see `kubectl-recipes.md`

