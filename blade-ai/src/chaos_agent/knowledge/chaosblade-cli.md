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
phases:
  - execute
  - recover
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
| `pod-network delay` | **Build-dependent** — probe with `blade create k8s pod-network -h` first. Where available (netem builds): `--time 3000 --offset 1000 --interface eth0` ; absent builds need the Tier 2 tc qdisc substitute |
| `pod-network drop` | `--destination-ip 10.1.2.3` ; `--source-port 3306` ; `--network-traffic out` for direction. **Does NOT support `--percent` or `--interface`** (iptables DROP semantics: all matching traffic is dropped) |
| `pod-network corrupt` / `duplicate` / `reorder` | **Build-dependent** — probe with `blade create k8s pod-network -h`: netem builds expose them, trimmed builds expose only `dns` / `drop` / `occupy`. Verified reorder flags (netem build): `--interface eth0 --percent 50 --correlation 50` (**all three required**) + optional `--gap 2 --time 20 --timeout <s>`; underlying rule is `tc netem delay <time>ms reorder <percent>% <correlation>% gap <gap>` |
| `pod-disk fill` | `--path /tmp --size 1024` (MB). Path is inside the container; check writable mounts first |
| `pod-disk burn` | `--read --write --size 50` for IO contention |
| `pod-process kill` | `--process java` ; `--process-cmd "java -jar"` |
| `pod-pod fail` | drops the pod via the pod controller — verify with restart count |
| `pod-network dns` | `--domain www.example.com --ip 10.0.0.0` (both **required**). Modifies `/etc/hosts` — see [DNS note](#dns-fault-note) below |
| `pod-network occupy` | `--port 8080` — occupies the given port |

> **⚠️ `pod-network` sub-command set is BUILD-dependent, not version-dependent** — always probe the actual binary with `blade create k8s pod-network -h` before choosing a path.
> Empirically verified on two live clusters: an upstream v1.8.0 build (Git Tag `v1.8.0`) exposed the full netem family (`delay` / `loss` / `corrupt` / `duplicate` / `reorder`) besides `dns` / `drop` / `occupy`; a trimmed v1.8.5 distribution build (Git Tag `blade-ai-v0.1.1`) exposed only `dns` / `drop` / `occupy`.
> Where the netem actions exist, use them directly (they manage their own timeout auto-recovery); where they are absent, fall back to the Tier 2 kubectl-native approach (tc qdisc). `drop` everywhere is iptables DROP — **no `--percent`, no `--interface`**, drops all matching traffic.

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
| `node-network drop` | `--interface` flags same as pod scope, applied at node interface. **Does NOT support `--percent`** (drops all matching traffic). (v1.8.0: `delay`/`loss` unavailable) |
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

Host-side `blade_create` (`blade create k8s ...`) creates an experiment
CR for EVERY scope — including node — and blocks until the operator
reconciles it. The node experiment *runs* inside the tool pod, but the
host CLI still needs the operator to get it there. Therefore: if the
operator is unhealthy (not ready / ImagePullBackOff / not deployed per
preflight or probe), the host path CANNOT succeed — skip it and start
with Tier 1 directly.

Otherwise, when `blade_create` fails on the host (incompatible blade
version, missing CLI, host firewall, etc.) you have three escalating
alternatives. The skill case's "Injection Method Selection" section is
authoritative for *which* alternatives apply to a given fault — this
doc only describes the *mechanics*.

### Tier 1: kubectl exec into Tool Pod

Preserves `blade_uid` for automatic recovery via `blade_destroy`.

```
1. Find a running tool pod ACROSS ALL NAMESPACES (the namespace is
   deployment-specific — it may be `chaosblade`, `default`, or else):
   kubectl get pods -A -l app=otel-c-tool -o wide --kubeconfig=<path>
   (fallback label: app=chaosblade-tool)

2. Execute blade inside the pod (default --timeout is auto-injected),
   using the NAMESPACE you discovered in step 1:
   kubectl exec <pod> -n <tool-pod-namespace> -- \
     blade create k8s <scope>-<target> <action> [flags]

3. Extract blade_uid from the JSON response — recovery must destroy this
   experiment through the SAME in-cluster channel (see "Recovery of
   Tier-1 experiments" below).
```

Inside the tool pod, blade uses the pod's ServiceAccount — do NOT add
`--kubeconfig` inside the blade command (`v_args`). The `kubectl` tool's
own `kubeconfig` parameter (for connecting to the cluster) should still
be passed via the dedicated `kubeconfig` parameter.

#### Recovery of Tier-1 (kubectl exec) experiments

An experiment created via Tier 1 lives in the cluster (CRD) and the host
`blade_destroy` tool cannot reach it — destroy it through the same
in-cluster channel used for injection:

```
1. Find a running tool pod ACROSS ALL NAMESPACES (same discovery as
   injection step 1; the namespace is deployment-specific — never assume):
   kubectl get pods -A -l app=otel-c-tool -o wide --kubeconfig=<path>
   (fallback label: app=chaosblade-tool)

2. Destroy the experiment inside the pod, using the NAMESPACE from step 1:
   kubectl exec <pod> -n <tool-pod-namespace> -- blade destroy <uid> \
     --kubeconfig=<path>

3. Confirm the output reports success.
```

The tool pod used during injection may have rotated (DaemonSet) — always
re-discover a currently Running pod before destroying.

#### CRD experiment status checks (UID dual mapping)

The `blade_uid` of a cluster-created experiment is the CRD resource name.
Inside a tool pod, `blade status <uid>` searches the LOCAL experiment
database and returns 'record not found' for CRD-created experiments —
treating that as "destroyed" is a false conclusion. Check the CRD instead:

| Intent | Command |
| --- | --- |
| Query CRD status via API server | `blade query k8s create <uid>` (inside the tool pod) |
| Check the CRD directly | `kubectl get/describe chaosblade <uid>` |

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

## When to Request Replan

If all three tiers above are exhausted without success but you can devise an
equivalent-effect method (same target, same fault effect, probe the environment
read-only first), that is legitimate within the safety envelope — the safety
guard arbitrates what is dangerous. Only when no such path remains, output
the structured replan request.

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

<a id="python-app-faults"></a>

## Python Application Faults: Matcher & Flag Reference

Reference for `blade_python_create` (`blade create python <target>
<action> [matchers] [flags]`). The tool docstring keeps the hard
constraints; this section carries the full syntax.

### Matchers (which calls to affect; omit = ALL calls of that client)

| target | matcher flags |
|---|---|
| `redis` | `cmd` (e.g. GET), `key` |
| `mysql` / `sqlalchemy` | `sql`, `sqltype` (e.g. select), `database` |
| `http` | `url`, `method`, `host` |
| `httpx` | `url`, `method`, `host`, `path` |
| `grpc` | `service`, `method` |
| `kafka` | `topic`, `operation` |

Matchers not valid for the chosen target are ignored by the tool.

### Action flags

| action | flags |
|---|---|
| `delay` | `--time 500` (REQUIRED, ms); optional `--offset 100` adds random 0..offset ms jitter |
| `throwCustomException` | `--exception ConnectionError --exception-message 'chaos test'`. Accepts a builtin name or a qualified path (`redis.exceptions.ConnectionError`). **An unresolvable name SILENTLY degrades to RuntimeError** — verify the exception TYPE the app actually saw, not just that it failed. |
| `returnValue` | `--return-value null`. Conversion: `"null"`/`"none"` → None, `"true"`/`"false"` → bool, digits → int/float, leading `{` or `[` → parsed JSON, anything else → literal string. **There is NO `"nil"` keyword** — it returns the 3-char string `"nil"`. |
