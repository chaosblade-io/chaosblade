---
title: "Fault Verification Strategies"
topics:
  - verification patterns
  - kubectl verification mapping
  - delay handling
  - minimal container workarounds
  - fault-specific checks
  - data interpretation pitfalls
  - coverage-verification
  - anomaly-detection
  - application-impact-verification
fault_types:
  - pod-kill
  - cpu-stress
  - network-delay
  - network-loss
  - dns-fault
  - disk-fill
  - disk-io
  - oom
  - node-disk-fill
  - node-cpu-stress
summary: "Fault-specific verification methodology, kubectl verification mapping by fault type, data interpretation pitfalls, coverage/anomaly/application-impact verification. Includes verification command design principles."
---

# Fault Verification Strategies and Methodology (for the Agent)

> **Purpose**: This document systematically lays out the layered verification model, the design principles for verification methods, verification plans for common fault scenarios, and the Agent's decision logic during the Layer 2 phase. It helps the Agent understand "how to verify a fault took effect" and design precise, executable verification plans.

> **Agent quick-reference index**:
> - **Verification model**: the three-layer model → [Q1](#q1-why-do-we-need-a-three-layer-verification-model-is-one-layer-not-enough); design principles → [Q2](#q2-how-do-you-design-an-effective-layer-2-verification-plan)
> - **Pod-level verification**: CPU fullload → [Q3](#q3-what-is-the-verification-plan-for-pod-cpu-fullload); memory/OOM → [Q4](#q4-what-is-the-verification-plan-for-pod-memory-pressure--oom); network delay → [Q5](#q5-what-is-the-verification-plan-for-pod-network-delay); packet loss → [Q6](#q6-what-is-the-verification-plan-for-pod-packet-loss); DNS fault → [Q7](#q7-what-is-the-verification-plan-for-a-pod-dns-fault); disk filling → [Q8](#q8-what-is-the-verification-plan-for-pod-disk-filling)
> - **Node-level verification**: CPU fullload → [Q9](#q9-what-is-the-verification-plan-for-node-cpu-fullload); disk full → [Q10](#q10-what-is-the-verification-plan-for-a-full-node-disk); high disk IO → [Q11](#q11-what-is-the-verification-plan-for-high-node-disk-io)
> - **Failure handling**: handling verification failure → [Q12](#q12-if-layer-2-verification-fails-what-should-the-agent-do); distinguishing timeouts → [Q13](#q13-how-do-you-tell-verification-failure-apart-from-verification-timeout)
> - **Skill conventions**: writing the verification method → [Q14](#q14-what-should-a-skills-verification-method-section-contain)
> - **Data pitfalls**: common data-interpretation pitfalls → [Q15](#q15-what-are-the-common-data-interpretation-pitfalls-in-layer-2-verification)
> - **Coverage verification**: coverage → [Q16](#q16-how-do-you-verify-that-the-injections-coverage-is-complete); anomalous-metric detection → [Q17](#q17-how-do-you-detect-and-investigate-unexpected-metric-changes); application-impact verification → [Q18](#q18-how-do-you-verify-the-faults-impact-at-the-application-level)

---

## 1. Recap of the layered verification model

### Q1: Why do we need a three-layer verification model? Is one layer not enough?

**A1**: The three-layer model is the core framework of fault verification; see the full treatment in `chaos-engineering-principles.md` Q7-Q8.

A brief comparison:
- **Layer 1 (injection-action verification)**: confirms the ChaosBlade experiment was created → filters out ineffective injections fast
- **Layer 2 (symptom verification)**: uses kubectl to confirm the fault symptom really appeared → sees through blade's status abstraction to the real system state
- **Layer 3 (impact verification)**: lateral comparison to confirm the blast radius is contained → verifies the blast radius

The Agent's strategy: Layer 1 + Layer 2 are mandatory; Layer 3 is optional (depends on the Skill's definition). On verification failure, trigger a rollback (blade destroy).

---

## 2. Design principles for Layer 2 verification

### Q2: How do you design an effective Layer 2 verification plan?

**A2**: An effective Layer 2 plan follows these five principles:

#### 2.1 Be targeted

The verification method MUST target the specific fault type, not a generic "check whether the system is healthy".

**Comparison**:
- ❌ **Generic**: `kubectl get pods -n default` (only checks the Pod exists)
- ✅ **Targeted** (Pod CPU fullload): `kubectl top pod my-pod -n default`, asserting CPU utilisation > 80%

> **🤖 How the Agent implements this**:
> - A Skill's SKILL.md should contain a "verification method" section listing the recommended kubectl commands and expected output explicitly
> - In the Layer 2 phase, the Agent reads that section and produces a verification plan
> - Verification methods differ widely between fault types; a single template cannot be reused

---

#### 2.2 Be quantifiable

The verification result should be a quantifiable metric, not a subjective judgement.

**Comparison**:
- ❌ **Subjective**: "the application got slower"
- ✅ **Quantifiable**: "P99 latency went from 100ms to 3000ms", "CPU utilisation went from 10% to 95%"

> **🤖 How the Agent implements this**:
> - Prefer commands that return numeric output, such as `kubectl top` and `kubectl get -o json`
> - Parse the JSON or table output and extract the key metrics (CPU percentage, memory bytes, latency in ms)
> - Compare the extracted metric against a threshold to decide pass/fail

---

#### 2.3 Cross-check multiple sources

A single data source may be unreliable; combine several and cross-check.

**Example**: verifying Pod OOM
- **Source 1**: `kubectl get pod -o json` → check `exitCode=137`, `reason=OOMKilled`
- **Source 2**: `kubectl describe pod` → check for an OOMKilling event in Events
- **Source 3**: `kubectl top pod` → check whether memory is near the limit
- **Source 4**: `kubectl logs --previous` → check the pre-crash logs for an out-of-memory record

> **🤖 How the Agent implements this**:
> - A Skill should provide several verification commands that form a verification chain
> - The Agent runs them in turn and judges holistically
> - If one source is unavailable (a minimal image with no top command), try the fallback

---

#### 2.4 Respect timing

Verification should run within a reasonable time window after injection; too early or too late both yield wrong conclusions.

**Examples**:
- **Too early**: checking immediately after injection, while the chaos process may still be starting and CPU has not risen yet
- **Too late**: waiting too long, by which point HPA may have scaled out or self-healing rebuilt the Pod, so the symptom is gone

> **🤖 How the Agent implements this**:
> - After a successful injection, wait briefly (**2-5 seconds**) for the fault to take effect
> - Then run Layer 2 verification
> - On failure, retry **1-2 times** (3-5 seconds apart) to rule out a timing issue
> - If it still fails after the retries, declare verification failed and trigger a rollback

---

#### 2.5 Stay rollback-safe

Verification must not introduce new side effects, so a failed verification can always roll back safely.

**Comparison**:
- ❌ **With side effects**: `kubectl exec my-pod -- rm -rf /data/*` (deletes data irrecoverably)
- ✅ **Side-effect free**: `kubectl exec my-pod -- df -h` (read-only; does not change system state)

> **🤖 How the Agent implements this**:
> - Layer 2 verification must use **read-only commands** only (get, describe, top, logs, and query-style commands under exec)
> - If a command with side effects is needed (e.g. cleaning up disk-filler files), run it in the **recovery phase**, not during verification
> - On verification failure, **call** `blade destroy` **immediately** without performing extra cleanup

---

## 3. Verification plans for common fault scenarios

> **Note**: each scenario below provides only the verification methodology (method name, verification goal, pass criteria, failure causes); the concrete commands live in the corresponding skill case's 「注入验证」 section. When there is no skill case, use `kubectl(subcommand="top"/"exec"/"logs")` and consult kubectl-guide.md.

### Q3: What is the verification plan for Pod CPU fullload?

**A3**:

**Layer 2 verification**:

**Method 1: verify CPU utilisation with kubectl top**
Expected: CPU usage close to the limit (e.g. limit=500m, actual 450-500m)

**JSONPath assertion**:
Read the CPU limit, then compare it with the top output

**Method 2: verify the chaos process exists via kubectl exec**
Expected: the output contains a chaos_cpu process with high CPU usage

**Method 3: verify increased latency from application logs**
Expected: the logs contain keywords such as timeout, slow request, high latency

**Layer 3 verification** (optional):
- Lateral comparison: is CPU normal on the Deployment's other Pods? Expected: only the target Pod has high CPU; the others are normal (< 20%)
- Verify whether HPA scaled out. Expected: if currentReplicas < maxReplicas and CPU stays high, currentReplicas should increase

**Possible reasons verification fails**:
- The Pod's CPU limit is too large (e.g. 4 cores) for the chaos process to saturate
- The Pod's node is resource-constrained and the chaos process is throttled
- The application is itself CPU-intensive and already consumes most of the CPU, leaving the chaos process no headroom

> **🤖 The Agent's decision logic**:
> - If `top pod` shows CPU < 50% of the limit, declare verification failed
> - Retry 1-2 times to rule out a timing issue
> - If it still fails, call `blade destroy` to roll back and record the failure reason in the experiment history

---

### Q4: What is the verification plan for Pod memory pressure / OOM?

**A4**:

**Layer 2 verification**:

**Method 1: verify memory utilisation with kubectl top**
Expected: memory usage close to the limit (e.g. limit=512Mi, actual 480-512Mi)

**Method 2: verify OOMKilled via kubectl get pod -o json**
Expected: returns "OOMKilled"; returns 137 (128+9, SIGKILL); restartCount > 0 (the container restarted)

**Method 3: verify Events via kubectl describe**
Expected: Events contain "OOMKilling" or "Memory cgroup out of memory"

**Method 4: verify the pre-crash logs via kubectl logs --previous**
Expected: the logs contain keywords such as "out of memory", "Killed", "signal 9"

**Layer 3 verification** (optional):
- Verify the Deployment recreates the Pod automatically. Expected: availableReplicas dips briefly, then recovers
- Verify the new Pod starts healthily. Expected: the new Pod is Running with READY=1/1

**Possible reasons verification fails**:
- `--mem-size` is set too low to reach the memory limit
- The Pod has no memory limit, so it can consume memory without bound and never triggers OOMKill
- The application degrades gracefully under memory pressure (e.g. shrinking its cache), avoiding OOM

> **🤖 The Agent's decision logic**:
> - If the goal is to verify OOM, check `lastState.terminated.reason == "OOMKilled"`
> - If the goal is to verify memory pressure (OOM not required), check that memory in `top pod` is near the limit
> - Pick the right assertion based on the Skill's stated verification requirement

---

> **⚠️ General notes for network faults** (apply to both Q5 network delay and Q6 packet loss):
> - **localhost is NOT affected by tc rules**: ChaosBlade network injection uses Linux `tc` (traffic control) underneath; `tc` rules act on a network interface (e.g. eth0) but do not affect localhost (127.0.0.1) loopback traffic. Verification MUST use the Pod's ClusterIP or the Service DNS name; testing connectivity via localhost is strictly forbidden
> - **Readiness Probe compatibility**: whether a pod-network fault causes Endpoints removal depends on the target Pod's Readiness Probe type:
>   - `exec` probes: run inside the container over localhost, so they are **unaffected** by tc rules → the Pod stays Ready → Endpoints is **not** removed
>   - `httpGet`/`tcpSocket` probes (on a port within the affected range): **may** fail because of the network fault → the Pod becomes NotReady → Endpoints is removed
>   - Before verifying, confirm the probe type via `kubectl describe pod <pod>` and adjust the expectation accordingly

### Q5: What is the verification plan for Pod network delay?

**A5**:

**Layer 2 verification**:

**Method 1: verify latency with kubectl exec ping**
Expected: the avg in rtt min/avg/max/mdev is close to the injected delay (e.g. 3000ms)

**Caveats**:
- `<target-ip>` should be another in-cluster Pod IP or a Service ClusterIP
- Do NOT ping an external address (e.g. 8.8.8.8), since external network conditions distort the latency
- If the target Pod uses a distroless image it may have no ping command, so another method is needed

**Method 2: verify HTTP latency with kubectl exec curl**
Expected: time_total close to the injected delay

**Method 3: verify timeouts from application logs**
Expected: the logs contain keywords such as "timeout", "i/o timeout", "deadline exceeded", "connection timed out"

**Method 4: verify connection state with kubectl exec ss/netstat**
Expected: connections are visible in ESTABLISHED state, but there may be many retransmissions

**Layer 3 verification** (optional):
- Verify whether downstream calls are affected. Expected: the upstream service's logs show retry, fallback, circuit breaker open records
- Verify the Service's overall error rate. Expected: Endpoints is non-empty (network delay alone does not remove a Pod from Endpoints unless the health check fails)

**Possible reasons verification fails**:
- The injected delay is too small (e.g. 10ms) and is masked by network jitter
- **(see the general network-fault notes above)** localhost is unaffected by tc rules + Readiness Probe compatibility

> **🤖 The Agent's decision logic**:
> - Parse the ping or curl output and extract the latency (in ms)
> - If the latency is < 50% of the injected value, declare verification failed (some error margin is allowed)
> - Retry 1-2 times to rule out network jitter
> - If it still fails, call `blade destroy` to roll back

---

### Q6: What is the verification plan for Pod packet loss?

**A6**:

**Layer 2 verification**:

**Method 1: verify the loss rate with kubectl exec ping**
Expected: the output contains "X% packet loss" where X is close to the injected rate (e.g. 50%)

**Parsing example**:
- Extract "50% packet loss" and compare it with the injected `--percent 50`

**Method 2: verify connection failures with kubectl exec curl**
Expected: some requests fail with errors such as "Connection reset by peer", "Operation timed out"

**Method 3: verify connection resets from application logs**
Expected: the logs contain keywords such as "connection reset", "broken pipe", "no route to host", "retry"

**Layer 3 verification** (optional):
- Verify the retry mechanism works. Expected: the upstream service's logs show "retrying request", "attempt 2/3" records
- Verify whether the circuit breaker trips. Expected: with a high loss rate over a long period, the breaker may open and the logs show "circuit breaker open"

**Possible reasons verification fails**:
- The loss rate is too low (e.g. 5%) and is masked by TCP retransmission, so the application layer never notices
- **(see the general network-fault notes above)** localhost is unaffected by tc rules + Readiness Probe compatibility
- The application has a robust retry mechanism that recovered from the loss automatically

> **🤖 The Agent's decision logic**:
> - Parse the ping output and extract the loss percentage
> - If the loss rate is < 50% of the injected value, declare verification failed
> - Retry 1-2 times to rule out randomness
> - If it still fails, call `blade destroy` to roll back

---

### Q7: What is the verification plan for a Pod DNS fault?

**A7**:

**Layer 2 verification**:

**How ChaosBlade pod-network dns works**: it edits the target Pod's /etc/hosts file, adding a `<forged-ip> <domain> #chaosblade` entry. This means the fault only takes effect when the system resolver (getaddrinfo/gethostbyname) is used.

**Method 1 (preferred): verify the hijack entry via kubectl exec cat /etc/hosts**
Expected: /etc/hosts contains an entry tagged `#chaosblade`, e.g. `1.1.1.1 example.com #chaosblade`

**Method 2 (effect verification): verify the domain resolves to the forged IP via kubectl exec ping/wget/curl**
Expected: ping prints `PING example.com (1.1.1.1)`; wget connects to the forged IP (may return 403 / connection refused)

**Method 3: verify resolution errors from application logs**
Expected: the logs contain keywords such as "Connection refused", "Unknown host", "connection timed out"
Note: this method only works when the target application actually uses the hijacked domain

**Method 4: verify other domains are unaffected**
Expected: `ping <another domain>` resolves to its normal IP, proving the hijack targets only the specified domain

**nslookup/dig do NOT apply to this fault type**:
nslookup and dig query the DNS server directly and bypass /etc/hosts entirely. They therefore always return the real DNS record rather than the hijack entry in /etc/hosts. Using nslookup/dig to verify this kind of DNS hijack yields the WRONG conclusion that "the fault did not take effect".

**Layer 3 verification** (optional):
- Verify DNS caching behaviour. Expected: if the application caches DNS, a second lookup may still succeed (cache not yet expired)
- Verify CoreDNS itself is healthy (it must NOT be affected). Expected: the CoreDNS Pod is Running (proving the fault only affects the target Pod, not cluster DNS)

**Possible reasons verification fails**:
- The application connects by IP rather than by domain, so the DNS fault does not affect it
- The application caches DNS and can still resolve until the cache expires
- The injected `--domain` does not match the domain the application actually uses
- nslookup/dig were used for verification (both bypass /etc/hosts and cannot detect this kind of DNS hijack)

> **The Agent's decision logic**:
> - Prefer `cat /etc/hosts` to confirm the hijack entry (direct evidence), then use `ping` or `wget` to confirm the effect (application-level evidence)
> - If the target application does not depend on the hijacked domain, mark application-impact verification as skipped and advise the user to pick a domain the application actually uses
> - Do NOT use `nslookup` or `dig` to verify a ChaosBlade DNS fault

---

### Q8: What is the verification plan for Pod disk filling?

**A8**:

**Layer 2 verification**:

**Method 1: verify disk utilisation via kubectl exec df**
Expected: Use% close to 100% (e.g. 95-100%)

**Parsing example**:
- Extract "98%" and compare it with the expectation (close to 100%)

**Method 2: verify the filler file exists via kubectl exec ls**
Expected: the output contains a large file (e.g. a 1G chaos_fill_xxx)

**Method 3: verify write failures from application logs**
Expected: the logs contain keywords such as "no space left on device", "write error", "disk full", "ENOSPC"

**Method 4: verify new files cannot be created via kubectl exec touch**
Expected: returns the error "No space left on device"

**Layer 3 verification** (optional):
- Verify whether other Pods on the same node are affected. Expected: only the target Pod's mounted volume is filled; other Pods are unaffected (unless they share the same PV)
- Verify log rotation works. Expected: if the application rotates logs, old log files should be cleaned up, freeing some space

**Possible reasons verification fails**:
- `--size` is set too small to reach the disk's capacity ceiling
- The target path `/data` is not a Volume mounted by the Pod but the container's root filesystem, so filling it may affect the container runtime
- The application has automatic cleanup (log rotation, temp-file cleanup) that offsets the filling

> **🤖 The Agent's decision logic**:
> - Parse the `df -h` output and extract the Use% field
> - If Use% < 90%, declare verification failed
> - Retry 1-2 times to rule out filesystem-statistics lag
> - If it still fails, call `blade destroy` to roll back
> - **Important**: when `--retain=true` (the default), remind the user to clean up the filler files manually after recovery

---

### Q9: What is the verification plan for Node CPU fullload?

**A9**:

**Layer 2 verification**:

**Method 1: verify CPU utilisation via kubectl top node**
Expected: CPU utilisation close to the injected value (e.g. 90%)

**Parsing example**:
- Extract "90%" and compare it with the injected `--cpu-percent 90`

**Method 2: verify Conditions via kubectl describe node**
Expected: Ready=True in Conditions (unless CPU is high enough to affect kubelet)

**Method 3: verify Pods on the same node are affected via kubectl top pod**
Expected: CPU usage of Pods on the same node may rise (because of CPU contention)

**Layer 3 verification** (optional):
- Verify the scheduler avoids that node. Expected: new Pods are not scheduled onto worker-1 (provided other nodes have free capacity)
- Verify whether HPA scales out because of the node's high CPU. Expected: if Pod CPU rises through node contention, HPA may trigger a scale-out

**Possible reasons verification fails**:
- The node has too many CPU cores (e.g. 32) and `--cpu-count` was not specified, so only some cores were affected
- The node's workload is very light, so overall CPU% stays low even with CPU fullload injected
- The injected `--cpu-percent` does not match how it is measured (blade may compute per-core while kubectl top computes across all cores)

> **🤖 The Agent's decision logic**:
> - Parse the `top node` output and extract the CPU% field
> - If CPU% < 70% of the injected value, declare verification failed (node-level verification allows a wider margin)
> - Retry 1-2 times to rule out transient fluctuation
> - If it still fails, call `blade destroy` to roll back

---

### Q10: What is the verification plan for a full node disk?

**A10**:

**Layer 2 verification**:

**Method 1: verify DiskPressure via kubectl describe node**
Expected: DiskPressure=True in Conditions

**Parsing example**:
- Key indicator: DiskPressure=True

**Method 2: verify disk-pressure events via kubectl get events**
Expected: Events contain records such as "NodeHasDiskPressure", "insufficient disk"

**Method 3: verify disk utilisation via kubectl top node (if metrics-server supports it)**
Note: kubectl top usually shows only CPU/memory, not disk. Use describe or df instead

**Alternative: SSH onto the node and run df** (if the Agent has node access)
Expected: Use% close to 100%

**Method 4: verify via an in-cluster tool Pod**
- Find an in-cluster tool Pod (e.g. otel-c-tool) to run ChaosBlade commands and kubectl API checks
- **Note**: otel-c-tool does NOT mount /host, so its `df -h` shows the overlay filesystem and cannot be used to verify host disk usage
- The tool Pod is usable for: ChaosBlade commands (blade status/destroy) and kubectl API checks (describe node, top node)

**Method 5: verify via kubectl debug node/ (the correct way to inspect the host filesystem)**
- `kubectl debug node/<node> --image=busybox -- sleep 3600` creates a temporary debug container with the node's root filesystem mounted automatically at `/host/`
- **Two-step approach (mandatory)**: first create the debug pod (with `-- sleep 3600` to keep it alive), then kubectl exec into it to run commands. A bare busybox exits immediately (Succeeded phase)
- Every host path MUST be prefixed with `/host/` (e.g. `/host/tmp`, `/host/var/log`)
- Do NOT use the `-it` flag (the Agent's execution environment is non-interactive)
- Clean up the debug container after use
- **Host disk / IO / process verification MUST use this method**: otel-c-tool does not mount /host and cannot run `df -h /host`, `iostat` and similar commands
- **Beware version skew**: if `kubectl debug node/` returns a "NotFound" error, the kubectl client and server versions may differ by more than ±1 minor version. Host-filesystem access is then unobtainable through the kubectl tool, so fall back to API-level checks (Method 1: `kubectl describe node` for DiskPressure + Method 2: `kubectl get events` for disk-pressure events). **Note**: `kubectl run` is NOT in the allowed sub-command list and cannot be used as a fallback

**⚠️ The overlay-filesystem trap**:
- `kubectl exec <any Pod> -- df -h` inspects the **container's overlay filesystem**, not the host's
- **otel-c-tool is subject to this too**: it does not mount /host, so its `df -h` still shows the overlay
- Only kubectl debug node/ provides the /host mount and can show real host disk usage

**⚠️ The multi-disk topology trap**:
- K8s distinguishes **nodefs** (the root partition, where kubelet and config files live) from **imagefs** (container-runtime storage: images, container writable layers, logs). They may sit on different physical disks
- When `kubectl describe node`'s allocatable shows BOTH `nodefs` and `imagefs` fields, the two are separate
- DiskPressure can be triggered by **either** filesystem. `df -h /host` only checks nodefs
- When `--path` points at a container overlay path (e.g. `/tmp`, `/var/log`), the filling writes to imagefs. Verifying via `df -h /host` will show no change
- Correct verification: run `df -h` (no path argument) to list every mounted filesystem, identify the partition whose utilisation rose, and match it to the injected `--path`

**Semantics of the `--path` parameter**:
- **CRD mode** (the default, `blade create k8s`): `--path` is a path relative to the container filesystem. `/tmp` and `/var/log` live inside the container overlay and are backed by imagefs; `/var/lib/docker` and `/var/lib/containerd`, if they exist on the host root partition, are backed by nodefs
- **exec-os mode** (running `blade` directly on the host): `--path` is a literal host path. `/tmp` fills the host's `/tmp`, backed by nodefs
- When verifying, reason about which partition the filling acts on from the injection mode and the `--path` value

**Layer 3 verification** (optional):
- Verify whether Pods on the node are affected. Expected: some Pods may be Pending or FailedMount
- Verify the kubelet log. Expected: the log contains records such as "disk pressure", "evicting pods"

**Possible reasons verification fails**:
- `--size` is set too small to reach the disk's capacity ceiling
- The filled path is not the node's root filesystem but some mounted volume, so it does not affect node-level DiskPressure
- The node has a large disk (e.g. 1TB) and filling 10GB is not enough to trigger DiskPressure
- The path filled (via `--path`) corresponds to the imagefs partition, but `df -h /host` only checks nodefs, leading to the wrong conclusion that the filling failed
- kubectl debug failed due to version skew, so host filesystem information could not be obtained

> **🤖 The Agent's decision logic**:
> - Check the DiskPressure condition in `describe node` first
> - If DiskPressure=False but `df -h` shows Use% > 90%, the filling took effect without crossing kubelet's pressure threshold
> - If `df -h /host` shows no change in utilisation, run `df -h` (no path argument) to inspect every partition and confirm whether the filling acted on imagefs
> - Pick the right assertion based on the Skill's stated verification requirement (does it require DiskPressure=True, or merely high disk utilisation)

### Q11: What is the verification plan for high node disk IO?

**A11**: Verifying high node disk IO spans three metric levels:

**Node-level metrics (mandatory)**:

| Metric | Standard command | BusyBox alternative | Pass criteria |
|------|---------|-------------|---------|
| %util | `iostat -xd 1 3` | not supported | close to 100% |
| tps / throughput | as above | `iostat -d -k 1 3` | significantly above baseline |
| %iowait | `iostat -c 1 3` | as above (supported) | significantly elevated (e.g. >10%) |
| dd process | `ps aux \| grep dd` | `ps \| grep dd` | the ChaosBlade dd process exists |

**Pod-level metrics (should verify)**:

| Metric | Command | Pass criteria |
|------|------|---------|
| Write latency | `kubectl exec <pod> -- dd if=/dev/zero of=/tmp/test bs=1M count=100` | duration increases significantly |
| Disk Events | `kubectl describe pod <pod>` | IO-related events appear |

**Verification priority**:
1. `iostat -c 1 3` to confirm %iowait rose (most portable; BusyBox supports it)
2. `ps | grep dd` to confirm the dd process is running (direct evidence)
3. `iostat -d -k 1 3` to confirm the tps/throughput change (incremental data)
4. A Pod-level dd test to confirm the read/write latency impact

**Caveats**:
- BusyBox `iostat` does not support the `-x` flag, so %util cannot be obtained directly
- `iostat -d`'s cumulative row can overflow into integers like 922337203685...; only look at the incremental intervals (from the 2nd row on)
- `iostat` MUST be given an interval and a count (e.g. `-k 1 3`); otherwise it prints only the since-boot cumulative average

> **🤖 The Agent's decision logic**:
> - Try `iostat -c 1 3` first (BusyBox-compatible) to confirm %iowait rose
> - If `iostat -x` reports "unrecognized option", switch to the BusyBox alternative immediately — do not retry `-x`
> - Finding ChaosBlade's dd process via `ps | grep dd` is direct evidence the fault took effect
> - For Pod-level verification, pick a Running Pod on the same node to run the dd write test

---

## 3-B. When to use blade_status vs blade_query_k8s

blade-ai has two tools for querying experiment state, with different purposes:

| Tool | Data source | Return value | Purpose |
|------|---------|--------|------|
| `blade_status` | local CLI side | `Status="Running"/"Destroyed"/"Error"` | confirm the experiment was created/destroyed successfully |
| `blade_query_k8s` | the K8s cluster's CRD | a `statuses[]` array (per-resource detail, affected_count) | confirm which resources were affected; coverage checks |

**Selection rules**:

| Situation | Use | Why |
|------|--------|------|
| Confirm the experiment was created after injection | `blade_status` | a local query suffices; no cluster-side detail needed |
| Confirm the experiment was cleared after destroy | `blade_status` | as above |
| Verify coverage (number of affected resources) | `blade_query_k8s` | returns a `statuses[]` array so `affected_count` can be tallied |
| Diagnose a partial injection failure | `blade_query_k8s` | shows whether each target's status is success/fail |
| Get the concrete list of affected resources | `blade_query_k8s` | `blade_status` returns no per-resource information |

> **💡 Agent usage tips**:
> - Prefer `blade_status` for a fast Layer 1 check
> - Switch to `blade_query_k8s` when coverage data is needed or an injection problem must be diagnosed
> - `blade_query_k8s`'s result contains everything `blade_status` returns, but the response is larger and slower

### Example blade_query_k8s output

```json
{
  "code": 200,
  "success": true,
  "result": {
    "uid": "abc123def456",
    "statuses": [
      {
        "state": "Success",
        "kind": "pod",
        "identifier": "default/node-name/pod-name/container-name/docker"
      },
      {
        "state": "Success",
        "kind": "pod",
        "identifier": "default/node-name2/pod-name2/container-name2/docker"
      }
    ]
  }
}
```

**Reading the key fields**:
- `result.statuses[].state`: each affected resource's state (`Success` / `Error`). On a partial injection failure, some entries are `Error`
- `result.statuses[].kind`: the resource type (`pod` / `node`)
- `result.statuses[].identifier`: formatted as `namespace/node/pod/container/runtime`, which pinpoints the affected resource
- An empty or missing `statuses` array: the CRD may not be ready yet — wait a few seconds and query again

---

## 4. Handling verification failure

### Q12: If Layer 2 verification fails, what should the Agent do?

**A12**: The flow for handling a Layer 2 failure:

> **Analysis + decision**: the failure-cause taxonomy and the retry/rollback decision logic are in `failure-modes.md` Mode 3 (Verification Failure) and `verification-heuristics.md`. What follows covers only the **execution-level procedure**.

**Executing the rollback**:

```bash
blade destroy <uid>
```

- Confirm `blade status --uid <uid>` returns Status="Destroyed" (`blade status` does not support the `--kubeconfig` flag; the Agent passes credentials via an environment variable internally)
- Record the failure reason in the experiment history (Operational Memory)
- Return a clear error message to the user, including:
  - The injected fault type and target
  - The verification-failure detail (e.g. "CPU utilisation was only 15%, expected > 80%")
  - Suggested troubleshooting steps (e.g. "check whether the Pod's CPU limit is set too high")

**Updating the knowledge base**:

- If a new failure mode was discovered (e.g. an image that does not support the chaos process), record it in MEMORY.md
- If the Skill's verification method turned out to be wrong, flag that Skill as needing revision

---

### Q13: How do you tell verification failure apart from verification timeout?

**A13**: 

**Verification failure**:
- The verification command ran successfully, but the output does not match expectations
- Example: `kubectl top pod` returns CPU=10% where > 80% was expected
- **Handling**: roll back immediately and record the failure reason

**Verification timeout**:
- The verification command timed out (e.g. `kubectl exec` unresponsive for over 60 seconds)
- Possible causes: the target Pod is unresponsive, the network is down, kubelet is faulty
- **Handling**:
  - First check `blade status --uid <uid>` to confirm the experiment's state
  - If Status="Running", the problem is likely the verification command itself — try the fallback verification method
  - If Status="Error" or the query times out, chaosblade-operator may be malfunctioning — force a rollback
  - Record the timeout as a diagnostic clue

> **🤖 How the Agent implements this**:
> - Give every verification command a reasonable timeout (e.g. 60 seconds for kubectl exec)
> - Catch the timeout exception and distinguish a command timeout from an experiment anomaly
> - On a command timeout, try a simpler verification method (e.g. `kubectl get` instead of `kubectl exec`)
> - If every verification method times out, treat it as a serious anomaly: force a rollback and raise an alert

---

## 5. Conventions for writing a Skill's verification method

### Q14: What should a Skill's "verification method" section contain?

**A14**: A complete "verification method" section should contain the following:

**1. Layer 1 verification instruction**
```markdown
### Layer 1 verification

Run `blade status --uid <uid>` and confirm Status="Running".
```

**2. The Layer 2 verification command list**
```markdown
### Layer 2 verification

Run the following in order; verification succeeds when at least one of them passes:

**Method 1: verify CPU utilisation via kubectl top**
```bash
kubectl top pod {{target_name}} -n {{namespace}}
```
Expected output: CPU utilisation > 80% of the limit.

**Method 2: verify the chaos process via kubectl exec**
```bash
kubectl exec {{target_name}} -n {{namespace}} -- ps aux | grep chaos
```
Expected output: contains a chaos_cpu process.

**Method 3: verify latency from application logs**
```bash
kubectl logs {{target_name}} -n {{namespace}} --tail=50
```
Expected output: the logs contain keywords such as "timeout", "slow", "latency".
```

**3. Possible reasons verification fails**
```markdown
### Verification troubleshooting

If verification fails, check these possible causes:
1. The Pod's CPU limit is too large (e.g. 4 cores) for the chaos process to saturate
2. The Pod's node is resource-constrained and the chaos process is throttled
3. Not enough time was allowed after injection, so the chaos process has not fully started
```

**4. Layer 3 verification (optional)**
```markdown
### Layer 3 verification (optional)

To verify the blast radius, run:
```bash
kubectl top pod -l app={{app_label}} -n {{namespace}}
```
Expected output: only the target Pod has high CPU; the other replicas are normal.
```

**5. Post-recovery cleanup steps**
```markdown
### Post-recovery cleanup

No extra cleanup is needed after the experiment is destroyed. If the disk was filled with `--retain=true`, delete the filler files manually:
```bash
kubectl exec {{target_name}} -n {{namespace}} -- rm -f /data/chaos_fill_*
```
```

> **🤖 How the Agent uses this**:
> - The LLM reads the "verification method" section and parses out the command list
> - It runs them in turn, parses the output, and judges whether it matches expectations
> - If every method fails, verification is declared failed and a rollback is triggered
> - If at least one method passes, verification is declared successful and the flow continues

### Q15: What are the common data-interpretation pitfalls in Layer 2 verification?

**A15**: Layer 2 verification relies on metric data obtained via kubectl exec. The following pitfalls can cause a wrong verdict:

**1. Cumulative-counter overflow**

Data sources such as BusyBox iostat and /proc/diskstats can show absurdly large values from integer overflow (e.g. tps > 10^8).
Criterion: the value clearly exceeds the physical device's capability → mark it as overflow and do NOT use it as positive evidence.
Correct approach: focus on the incremental intervals (skip the 1st cumulative row) and judge the current state from the increments only.

**2. Expected non-zero, measured zero**

When the skill case expects a metric to rise (iowait rising, CPU utilisation rising, process count increasing)
but the measured value is at or near zero, that is strong counter-evidence that the fault did NOT take effect.
A common mistake: rationalising the zero with speculative explanations ("async IO", "kernel buffering", "scheduler optimisation").
Correct approach: unless there is direct evidence supporting such an explanation (e.g. independent log output proving async IO mode),
accept the zero as counter-evidence rather than rationalising it.

**3. Missing process**

When the fault type depends on a specific process (dd, stress-ng, chaos_*) to produce its effect,
ps/grep not finding that process is direct evidence the fault did not take effect.
Note: ps output may truncate long commands — use `ps aux` or grep the full command name.

**4. Inconsistent sampling interval**

The first output of tools like iostat and top is usually the since-boot cumulative value, which does not represent the current state.
Correct approach: collect at least 2-3 intervals, skip the first, and judge from the later intervals.

> **🤖 How the Agent uses this**:
> - When reading command output, first decide whether the data falls into one of the pitfalls above
> - Mark anomalous data `[ANOMALY]` and do not use it as positive evidence
> - Do not rationalise zero-value / missing-process counter-evidence with speculation unless there is direct corroboration

---

## 5-B. Verification coverage and completeness

### Q16: How do you verify that the injection's coverage is complete?

**A16**: Coverage = actually affected resources / expected target resources. Incomplete coverage means the injection did not reach every intended target.

**Data sources**:
- Layer 1's `blade_query_k8s` returns a `statuses[]` array containing each affected resource's state
- `affected_count` = the length of `statuses[]`, i.e. how many resources were actually affected
- Expected target count = the total number of Pods/Nodes matched by the label selector

**How to determine the expected target count**:
```bash
# Pod level
kubectl get pods -l <label> -n <ns> --no-headers | wc -l
# or read the length of target.names from the fault context
```

**Common reasons coverage falls short**:
1. **ChaosBlade defaults to a single target**: `blade create k8s pod-* --labels <label>` selects only **1** matching resource by default. To cover every match, pass `--effect-count`
2. **Node-level faults**: `blade create k8s node-*` usually affects only the named node and is not subject to this limit
3. **The CRD is not ready yet**: `blade_query_k8s`'s `statuses[]` may be empty for a short window after injection

**Decision rules**:
- Coverage = 100% → normal, nothing extra to do
- Coverage < 100% → report the ratio under VERIFICATION_RESULT → Warnings (e.g. "Coverage: 1/3 pods affected")
- Coverage < 100% but explained by ChaosBlade's known default → report as a Warning; do NOT downgrade the Layer 2 status

**Example**:
```
affected_count=1, target.names=["pod-a", "pod-b", "pod-c"]
→ Warning: "Coverage: 1/3 pods affected. ChaosBlade defaults to single-target unless --effect-count is specified."
```

### Q17: How do you detect and investigate unexpected metric changes?

**A17**: An unexpected metric change is one that does not match the injection's expected effect. For example: after injecting memory pressure, a non-target Pod's memory DROPS; after injecting CPU pressure, the target Pod's CPU does not rise but its disk IO becomes anomalous.

**Detection method**:
1. Compare metrics across **every** target resource matching the label, not just the ones showing the expected effect
2. Record each resource's pre-injection baseline and post-injection metric
3. Identify changes in the opposite direction to expectations (memory expected to rise but actually falling)

**Investigation path**:
```bash
# 1. Check whether the Pod restarted (metrics reset after a restart)
kubectl describe pod <name> -n <ns> | grep -A5 "Restart Count"

# 2. Check recent events
kubectl get events -n <ns> --sort-by='.lastTimestamp' | tail -20

# 3. Check whether HPA scaled out (new Pods carry baseline metrics)
kubectl get hpa -n <ns>

# 4. Confirm the metrics-server sampling interval (15s on most clusters; the binary defaults to 60s)
kubectl top pod <name> -n <ns>  # run twice in a row and see whether the values fluctuate
```

**Common causes**:
| Anomaly | Possible cause | How to verify |
|---------|---------|---------|
| Memory DROPS instead | The Pod restarted (metrics reset to baseline) | check restartCount via `kubectl describe pod` |
| CPU did not rise | The process never started, or already exited | `kubectl exec -- ps aux \| grep stress` |
| Metrics fluctuate up and down | metrics-server sampling interval (15s on most clusters) | check 2-3 times in a row to confirm the trend |
| New Pods appeared | HPA scaled out | `kubectl get hpa` |

**Decision rules**:
- An anomaly MUST be recorded in the Negative Evidence section and must not be rationalised with speculation
- If the anomaly can be proven irrelevant (e.g. the Pod restart is unrelated to the injection), explain why in Negative Evidence
- If the anomaly cannot be explained, it weakens the credibility of the verification conclusion

### Q18: How do you verify the fault's impact at the application level?

**A18**: Application impact means observable degradation of application behaviour caused by the injection (higher latency, higher error rate, lower availability). This is verification's ultimate goal — confirming the fault's "blast radius" really reached the application layer.

**Verification methods** (using the kubectl tool only):

**Method 1: search application logs for anomaly keywords with kubectl logs**
```bash
# search for timeout / error / latency keywords
kubectl logs <pod> -n <ns> --tail=100 | grep -iE "timeout|error|latency|slow|retry|refused"

# compare the error rate before and after injection
kubectl logs <pod> -n <ns> --since=5m | grep -ci "error"
```

**Method 2: issue a test request with kubectl exec**
```bash
# test Service reachability and response time from inside the cluster
kubectl exec <test-pod> -n <ns> -- curl -s -o /dev/null -w "HTTP %{http_code} Time: %{time_total}s\n" http://<service>:<port>/health

# if curl is unavailable, try wget
kubectl exec <test-pod> -n <ns> -- wget -q -O /dev/null --timeout=5 http://<service>:<port>/health
```

**Method 3: check application-level events with kubectl get events**
```bash
kubectl get events -n <ns> --field-selector reason=Unhealthy --sort-by='.lastTimestamp'
```

**Method 4: check Service endpoint changes with kubectl get endpoints**
```bash
# a network fault may change Endpoints
kubectl get endpoints <service> -n <ns>
```

**Fallbacks when the container lacks tooling**:
- No `curl`/`wget` in the container: use `kubectl describe pod` to inspect probe-failure records in Events
- Cannot issue a request from inside the Pod: observe the application's behaviour change indirectly via `kubectl logs`
- The Service has no external endpoint: check for changes via `kubectl get endpoints`

**Decision rules**:
- Application-impact verification steps required by the Skill case **must NOT be skipped**; when they cannot be run, mark them `[SKIPPED]` and state why
- Application-impact verification passes → strengthens the credibility of the Layer 2 conclusion
- Application-impact verification fails (e.g. latency did not rise) → investigate why (did the fault's effect really propagate to the application layer?)
- Application-impact verification skipped (no tooling available) → note it in a Warning; do NOT downgrade the Layer 2 status

---

## 6-A. Fault scenario → kubectl verification mapping

> Migrated from kubectl-guide.md sections 6 and 7; complements the per-fault-type verification plans (Q3-Q11).

### Command combinations for Pod-level faults

| Fault scenario | Recommended command combination |
|----------|-----------------|
| Pod OOM | `get pod -o json` (exitCode=137, reason=OOMKilled) + `describe pod` (OOMKilling in Events) + `top pod` (memory near the limit) |
| High Pod CPU | `top pod` (CPU spike) + `exec -- ps aux` (chaos process present) + `get pod -o wide` (confirm the node) |
| Pod disk full | `exec -- df -h` (disk utilisation near 100%) + `describe pod` (disk-related Events) |
| Pod network delay | `exec -- ping` (latency rose) + `exec -- ss -tlnp` (connection state) + application-log timeouts |
| Pod packet loss | `exec -- ping` (loss rate) + application-log connection resets |
| Pod DNS fault | `exec -- cat /etc/hosts` (confirm the #chaosblade hijack entry) + `exec -- ping <domain>` (resolves to the forged IP; `getent hosts` works on glibc images) + application-log resolve failures. **⚠️ Do NOT use nslookup/dig — they bypass /etc/hosts** |
| Pod image-pull failure | `get pod -o json` (waiting.reason=ImagePullBackOff) + `describe pod` (ErrImagePull in Events) |
| Pod stuck Terminating | `get pod -o json` (deletionTimestamp non-empty, phase not Terminated) + `describe pod` (finalizers / deletion reason) |
| Pod deleted | `get pod -o json` (returns NotFound) or `get pods -l` (the Pod list changed) |

### Command combinations for Node-level faults

| Fault scenario | Recommended command combination |
|----------|-----------------|
| High Node CPU | `top node` (CPU spike) + `describe node` (Conditions normal) |
| High Node memory | `top node` (memory spike) + `describe node` (MemoryPressure=True) + `get events` (NodeHasInsufficientMemory) |
| High Node disk | `describe node` (DiskPressure=True) + `get events` (NodeHasDiskPressure) |
| Node unavailable | `get node -o json` (Ready=False) + `describe node` (kubelet stopped posting, in Events) + the state of that node's Pods |
| High Node disk IO | `exec` onto the node (e.g. via a DaemonSet) to read io stat, or observe Pod startup latency |

### Command combinations for Workload / Service-level faults

| Fault scenario | Recommended command combination |
|----------|-----------------|
| Deployment replica mismatch | `get deployment -o json` (readyReplicas < replicas) + `get pods -l` (some Pods unhealthy) |
| HPA at its ceiling | `get hpa -o json` (currentReplicas == maxReplicas) + `top pod` (CPU/memory triggering the scale-out) |
| DaemonSet not fully scheduled | `get ds -o json` (desiredNumberScheduled != numberReady) + `get pods -l` (some Pending) |
| Service load-balancing anomaly | `get endpoints -o json` (addresses empty) + `get svc -o json` (selector correct) + `get pods -l` (state of the Pods matching the selector) |
| Workload scaled down | `get deployment -o json` (replicas reduced) + `get events` (ScaledDown) |

### Conventions for discovering a Service's targets
When the fault target is a Service, the matching Pods MUST be discovered through the Service's selector; guessing the label selector is forbidden:
- Read the selector: `kubectl get svc <name> -n <ns> -o jsonpath='{.spec.selector}'`
- Find Pods with that selector: `kubectl get pods -n <ns> -l '<selector-key>=<selector-value>'`
- Do NOT infer the label from the Service name (e.g. assuming svc=mysql → app=mysql)

### Design principles for verification commands

1. **Observe before asserting**: read the current state via `get -o json` or `describe` first, then assert in LLM reasoning — never assume the output format.
2. **Cross-check multiple dimensions**: a single metric can mislead; combine status fields + Events + resource metrics + application logs.
3. **Compare against a baseline**: record Pod state, the Events list and resource metrics before injection as the baseline, then compare after injection.
4. **Watch for change, not absolute values**: some metrics fluctuate naturally, so verification should ask "did the expected change occur" (restartCount increased, a new Event appeared, CPU went from 10% to 90%).
5. **Prefer structured JSON output**: use `-o json` for programmatic assertions; use `describe` when a human-readable event description is needed.
6. **Make good use of label selectors**: fetch the state of several Pods of the same application in bulk via `-l app=<app>` rather than querying one at a time.
7. **Mind the time window**: Events and logs both age out, so focus on new events near the injection timestamp using `--since=5m` or sorting by `lastTimestamp`.
8. **Distinguish the container's view from the host's**: `kubectl exec` shows the inside of the container; `kubectl top` and `kubectl get` show the host/cluster view.

---

## 6-B. Recovery-verification strategy

After recovery (`blade destroy`), the fault's effect MUST be verified as fully gone; otherwise residual impact can remain (leftover processes, uncleaned /etc/hosts entries, iptables rules that were never reverted).

### General principles for recovery verification

1. **Recovery verification ≠ inverting the assertion** — it is not simply negating the injection assertions, but confirming the system returned to its **pre-injection baseline**
2. **A baseline comparison is mandatory** — record the key pre-injection metrics (CPU%, memory%, disk utilisation, network latency, ...) and compare after recovery to confirm the return to baseline
3. **Allow a recovery window** — after `blade destroy` runs, the fault's effect can take 5-30s to disappear entirely (metrics-server's sampling interval is 15s on most clusters), so do not verify immediately
4. **Check for residue** — after recovery, always check for leftovers: processes (a stress process that never exited), files (/etc/hosts not cleaned), rules (iptables/tc rules not deleted)

### Recovery-verification method per fault type

| Fault type | Recovery-verification method | Residue check |
|---------|------------|---------|
| Pod CPU fullload | `kubectl top pod` CPU back to baseline + `exec -- ps aux` shows no stress process | does a stress-ng/stress process remain? |
| Pod memory pressure | `kubectl top pod` memory back to baseline + the Pod is not OOMKilled | has `/proc/meminfo` returned to normal? |
| Pod network delay | `exec -- ping -c 3 <target>` latency back to baseline | `tc qdisc show` shows no ChaosBlade tc rule |
| Pod packet loss | `exec -- ping -c 10 <target>` loss rate back to 0% | `tc qdisc show` shows no ChaosBlade tc rule |
| Pod DNS fault | `exec -- cat /etc/hosts` has no `#chaosblade` entry + `ping <domain>` resolves to the real IP | has the `#chaosblade` entry been removed from /etc/hosts? |
| Pod disk filling | `exec -- df -h` utilisation back to baseline | have the temporary filler files been deleted? |
| Node CPU fullload | `kubectl top node` CPU back to baseline + no abnormal Node Conditions | does a stress process remain on the node? |
| Node network fault | inter-node ping latency/loss back to baseline | have the iptables/tc rules been cleaned up? |
| Node disk filling | `kubectl describe node` DiskPressure=False + `df -h` utilisation back to baseline | have the filler files been deleted? |
| Pod deleted | `kubectl get pod <name>` the Pod exists and is Running | — |
| Process killed | `exec -- ps aux \| grep <process>` the process is running again | — |

### Handling recovery-verification failure

When recovery verification finds residual impact:

| Anomaly | Possible cause | How to handle |
|---------|---------|---------|
| CPU has not come down | a stress process remains | `exec -- kill <pid>` to terminate the leftover process manually |
| /etc/hosts not cleaned | blade destroy failed to remove the entry | `exec -- grep -v '#chaosblade' /etc/hosts > /tmp/hosts && mv /tmp/hosts /etc/hosts` (portable; ⚠️ Alpine/BusyBox `sed -i` is not GNU-sed compatible, so do NOT use `sed -i`) |
| tc rules remain | ChaosBlade failed to delete the tc rule | `exec -- tc qdisc del dev eth0 root` to delete it manually |
| Disk not freed | filler files remain | `exec -- rm <path>` to delete the filler files manually |
| The Pod is still Evicted | node resource pressure has not fully cleared | wait for the node's resources to recover, or delete the Pod manually so it is rebuilt |

> **Safety floor**: if the residue persists after manual cleanup, you MUST **escalate to a human operator** (rather than attempting more destructive operations) and record it in Negative Evidence.

---

## 6. Glossary

| Term | Definition |
|------|------|
| Layer 1 verification | Confirming the experiment's state via `blade status` |
| Layer 2 verification | Using kubectl to verify the fault symptom appeared |
| Layer 3 verification | Lateral comparison to verify the blast radius is contained |
| Verification failure | The verification command ran successfully, but the output does not match expectations |
| Verification timeout | The verification command timed out, so no result could be obtained |
| Rollback | Calling `blade destroy` to stop the injection |
| Retry | Waiting a moment after a failure and verifying again |
| Self-healing | Kubernetes's automatic fault-repair mechanisms, which may mask the fault symptom |
