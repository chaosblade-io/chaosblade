---
title: "ChaosBlade CLI Flag Catalogue & Injection Method Switching"
topics:
  - chaosblade flags
  - chaosblade scenario examples
  - injection method switching
  - kubectl exec blade fallback
  - kubectl-native injection
fault_types:
  - pod-cpu
  - pod-memory
  - pod-network
  - pod-disk
  - pod-process
  - pod-pod
  - container-cpu
  - container-memory
  - container-network
  - node-cpu
  - node-memory
  - node-disk
  - node-network
summary: "Long-form examples and flag references for ChaosBlade K8s scenarios across pod / container / node scopes, plus the three-tier injection method switching catalogue (blade_create → kubectl exec into tool pod → kubectl-native scale/cordon/patch/taint)."
---

# ChaosBlade CLI Flag Catalogue & Injection Method Switching

> **When to read this**: When you are constructing a `blade_create`
> command and the active skill case does not list the exact flag you
> need, or when `blade_create` fails and you need to switch injection
> method. The tool docstrings retain only the hard constraints; the
> long-form catalogue lives here.

## Scenario Flag Examples

ChaosBlade K8s scenarios follow the form:

```
blade create k8s <scope>-<target> <action> [flags]
```

### Pod Scope

| Scenario | Example flags |
| --- | --- |
| `pod-cpu fullload` | `--cpu-percent 80` (single CPU) ; `--cpu-count 2 --cpu-percent 100` (pin 2 cores) |
| `pod-memory load` | `--mem-percent 70` ; `--mem-size 512` (MB) ; `--mode cache` (cache vs ram) |
| `pod-network delay` | `--time 3000 --offset 1000 --interface eth0` ; add `--local-port 8080` to scope to a port |
| `pod-network loss` | `--percent 50 --interface eth0` ; `--destination-ip 10.1.2.3` for outbound-only loss |
| `pod-network corrupt` / `duplicate` / `reorder` | similar `--percent` semantics as loss |
| `pod-disk fill` | `--path /tmp --size 1024` (MB). Path is inside the container; check writable mounts first |
| `pod-disk burn` | `--read --write --size 50` for IO contention |
| `pod-process kill` | `--process java` ; `--process-cmd "java -jar"` |
| `pod-pod fail` | drops the pod via the pod controller — verify with restart count |
| `pod-network dns` | `--domain www.example.com --ip 10.0.0.0` (both **required**). Modifies `/etc/hosts` — see [DNS note](#dns-fault-note) below |

### Container Scope

Container scope **requires** either `--container-ids` or
`--container-names` in `flags`:

```
blade create k8s container-cpu fullload \
  --names <pod> --container-names <ctr> --cpu-percent 80
```

### Node Scope

ChaosBlade rejects `--namespace` and `--labels` for node scope — the
`blade_create` tool auto-omits them. Use `--names` to identify the node.

| Scenario | Example flags |
| --- | --- |
| `node-cpu fullload` | `--cpu-percent 80` |
| `node-memory load` | `--mem-percent 70` (node scope accepts ONLY `--mem-percent`, not `--mem-size`) |
| `node-disk fill` | `--path /tmp --size 1024`. Path resolution depends on mount layout — see "Resource Mapping" below. |
| `node-network delay` / `loss` | same network flags as pod scope, but applied at the node interface |
| `node-process kill` | targets host processes — exercise extreme caution |

### Resource Mapping for `node-disk fill`

`--path` typically maps to a partition as follows, but the actual
partition depends on the node's mount configuration — verify with
`df -h` on the live node:

- `/tmp`, `/var/log`, `/var/run`, `/run` → typically imagefs in CRD
  mode (container overlay), **only if** the node has a separate
  imagefs. If nodefs and imagefs share a single partition, these paths
  are on nodefs.
- `/var/lib/docker`, `/var/lib/containerd` → these **are** the
  container runtime storage root. When on a separate disk, they define
  imagefs (not nodefs). When on the root disk, they are on nodefs.
- `/var/lib/kubelet`, `/etc`, `/root`, `/home` → always on nodefs
  (kubelet root dir / host OS paths).

Include the LIKELY target resource in your fault plan's "Expected
Impact" section, but note: "actual partition should be verified with
`df -h` during verification".

## Injection Method Switching

When `blade_create` fails on the host (incompatible blade version,
missing CLI, host firewall, etc.) you have three escalating
alternatives. The skill case's "Injection Method Selection" section is
authoritative for *which* alternatives apply to a given fault — this
doc only describes the *mechanics*.

### Tier 1: kubectl exec into Tool Pod

Preserves `blade_uid` for automatic recovery via `blade_destroy`.

```
1. Find a running tool pod:
   kubectl get pods -n chaosblade -l app=otel-c-tool --kubeconfig=<path>

2. Execute blade inside the pod (default --timeout is auto-injected):
   kubectl exec <pod> -n chaosblade -- \
     blade create k8s <scope>-<target> <action> [flags]

3. Extract blade_uid from the JSON response — it is still valid for
   blade_destroy recovery.
```

Inside the tool pod, blade uses the pod's ServiceAccount — do NOT add
`--kubeconfig` inside the blade command (`v_args`). The `kubectl` tool's
own `kubeconfig` parameter (for connecting to the cluster) should still
be passed via the dedicated `kubeconfig` parameter.

### Tier 2: kubectl-Native Injection

No `blade_uid`; manual rollback required; Layer 2 will verify fault
effect.

| Fault intent | kubectl primitive |
| --- | --- |
| Pod kill | `kubectl delete pod <name> -n <ns> --force --grace-period=0` |
| Pod evict / drain | `kubectl drain <node> --ignore-daemonsets` |
| Node unschedulable | `kubectl cordon <node>` (uncordon to recover) |
| Node taint | `kubectl taint nodes <node> key=value:NoSchedule` |
| Replica zero | `kubectl scale deployment <name> -n <ns> --replicas=0` |
| Probe failure | `kubectl patch ... readinessProbe` (rollback by patch) |

Always document the recovery primitive (e.g. `kubectl uncordon`,
`kubectl scale --replicas=<original>`) in the same response so the user
can roll back manually.

### Tier 2 Verification (kubectl-native)

kubectl-native injections have **no blade_uid** — verification must rely entirely on kubectl observation:

| Fault intent | L1: Confirm injection happened | L2: Confirm fault effect observable | Recovery verification |
| --- | --- | --- | --- |
| Pod kill | `get pod <name>` → NotFound | Remaining pods handle traffic; no service disruption | `get pod` → Pod recreated and Running |
| Pod evict / drain | `get pods -n <ns> -o wide` → Pods removed from node | Node workload redistributed | `uncordon` + `get pods` → Pods rescheduled |
| Node unschedulable | `describe node` → `Unschedulable: true` | New pods cannot be assigned to this node | `uncordon` + `describe node` → `Unschedulable: false` |
| Node taint | `describe node` → taint in Taints list | Pods without toleration evicted/not scheduled | `taint nodes <node> key-` + verify taint removed |
| Replica zero | `get deployment` → `READY 0/0` | Service endpoints empty; traffic fails | `scale --replicas=<original>` + `READY` matches |
| Probe failure | `describe pod` → readiness probe fails | Pod removed from Service Endpoints | `patch` restore + `get endpoints` → IP restored |

> **Key difference from ChaosBlade injections**: kubectl-native faults have no auto-recovery mechanism. Recovery verification is especially critical — the operator must manually execute the recovery primitive and confirm the system returns to baseline.

### Tier 3: Adjust Blade Parameters

Check `blade create k8s <scenario> -h` (run inside the tool pod) for
supported flags in your version. Older blade versions reject
`--namespace` on some k8s subcommands — retry without it.

## When to Output `[REPLAN]`

If all three tiers above are exhausted without success, output
`[REPLAN]` rather than improvising a method that the skill case did not
list. Improvising untested methods violates the safety contract.

<a id="dns-fault-note"></a>

## DNS Fault: Verification Constraints

ChaosBlade `pod-network dns` modifies `/etc/hosts` (adds `#chaosblade` annotated entries). This only affects programs that resolve via the system resolver (`getaddrinfo`/`gethostbyname` → NSS → `/etc/hosts`):

| Tool | Uses /etc/hosts? | Reason |
|------|-----------------|--------|
| `ping`, `curl`, `wget` | ✅ Yes | Resolve via C library → NSS |
| `getent hosts` | ✅ Yes (glibc only) | Resolve via NSS — **not available in Alpine/musl images**; use `cat /etc/hosts` + `ping` instead |
| `nslookup`, `dig`, `host` | ❌ No | Direct DNS query, bypass NSS and /etc/hosts |
| Most business apps (Java/Python/Go) | ✅ Yes | Resolve via C library or equivalent |

**Verification**: Use `cat /etc/hosts` (confirm `#chaosblade` entry) + `ping <domain>` (confirm resolution to forged IP). On glibc images (Debian/Ubuntu/CentOS), `getent hosts <domain>` is also reliable. **Never use `nslookup`/`dig`** — they bypass /etc/hosts and will return the real DNS record, misleading the verifier.
