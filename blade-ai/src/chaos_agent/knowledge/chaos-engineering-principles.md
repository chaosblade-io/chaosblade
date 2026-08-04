---
title: "Chaos Engineering Principles"
topics:
  - chaos engineering theory
  - steady state hypothesis
  - blast radius
  - experiment design
  - safety red lines
  - fault injection methodology
  - three-layer verification model
fault_types:
  - all
summary: "Authoritative source for chaos engineering principles: steady state hypothesis, blast radius control, experiment lifecycle, safety red lines, three-layer verification model. Q&A format."
---

# Chaos Engineering Principles and Fault Injection Fundamentals (for the Agent)

> **Purpose**: This document systematically lays out the core ideas of chaos engineering, the basic principles of fault injection, why fault drills matter, and the methodology the Agent should follow when designing injection and verification plans. It helps the Agent understand the "why" behind the "what", so it can make sounder decisions.

> **Agent quick-reference index**:
> - **Core concepts**: what chaos engineering is → [Q1](#q1-what-is-chaos-engineering-how-does-it-differ-from-traditional-testing); why do fault injection → [Q2](#q2-why-do-fault-injection-what-is-its-core-value)
> - **Design principles**: the seven principles → [Q3](#q3-what-are-the-seven-principles-of-chaos-engineering); blast-radius control → [Q5](#q5-what-is-blast-radius-and-how-do-you-control-it)
> - **Fault taxonomy**: infrastructure / Pod / application layer → [Q4](#q4-what-types-of-fault-injection-are-there-and-what-are-the-typical-scenarios-for-each)
> - **Experiment flow**: the five lifecycle phases → [Q6](#q6-what-phases-does-a-complete-fault-injection-experiment-contain); the three-layer verification model → [Q7](#q7-authoritative-source-what-are-layer-1--layer-2--layer-3-verification-and-how-do-they-relate)
> - **Safety red lines**: the forbidden-operations list → [Q9](#q9-safety-red-lines-which-operations-are-absolutely-forbidden-and-why)
> - **Use cases**: typical applications → [Q10](#q10-what-are-the-typical-real-world-applications-of-fault-injection)
> - **The Agent's role**: strengths and limits → [Q11](#q11-what-advantages-does-the-agent-have-over-traditional-fault-injection-tools-such-as-using-chaosblade-directly) / [Q12](#q12-what-are-the-agents-limitations-when-should-the-agent-not-be-used)

---

## 1. The nature and goals of chaos engineering

### Q1: What is chaos engineering? How does it differ from traditional testing?

**A1**: Chaos engineering is the discipline of experimenting on a distributed system in order to build confidence in the system's ability to withstand turbulent conditions, by deliberately injecting faults.

**The core differences**:

| Dimension | Traditional testing | Chaos engineering |
|------|---------|---------|
| **Goal** | Verify the system works as specified | Discover unknown weaknesses under abnormal conditions |
| **Method** | deterministic input → deterministic output | random/controlled perturbation → observe system behaviour |
| **Scope** | unit, integration and end-to-end tests | real traffic in production or near-production |
| **Assumption** | "we know every possible failure mode" | "we do not know how the system will fail" |
| **Mindset** | prove the system is correct | falsify the system's resilience assumptions |

> **💡 Agent usage tips**:
> - Cite this section when the user asks "why do chaos engineering" or "how does chaos engineering differ from testing"
> - Key insight: **chaos engineering is not about "breaking the system", it is about "improving resilience through controlled experiments"**. Every experiment should have an explicit hypothesis, a bounded scope, observable metrics and a rollback plan.

---

### Q2: Why do fault injection? What is its core value?

**A2**: The core value of fault injection shows up at four levels:

**1. Verifying that high-availability design is real**
- Many systems claim to "support automatic failover" or to "have elastic scaling", yet have never been verified under real fault conditions
- Fault injection exposes hidden problems: misconfiguration, missing dependencies, unreasonable timeout settings
- **Example**: a Deployment sets `replicas=3`, but if anti-affinity is misconfigured all three Pods may sit on one node, so a node failure takes the service down completely

**2. Discovering how cascading failures propagate**
- In a distributed system, one component's failure can spread to others through retries, timeouts, connection-pool exhaustion and similar mechanisms
- Fault injection helps map the "fault propagation graph", identifying single points of failure and fragile links
- **Example**: database latency rises → application query timeouts → connection pool exhausted → new requests rejected → upstream service trips its circuit breaker → degraded user experience

**3. Training the team's incident response**
- A fault drill is a fire drill: it lets SRE and development teams practise diagnosis and recovery in a low-pressure setting
- Repeated drills build muscle memory and shorten MTTR (Mean Time To Recovery)
- **Example**: after a Pod is OOMKilled, can the team locate the problem quickly with `kubectl logs --previous`? Is there an automated alert?

**4. Building confidence in the system**
- After thorough fault drills, the team is more confident about how the system will behave in production
- That confidence is not blind; it is a rational judgement grounded in a large body of experimental data
- **Example**: "our latency tolerance for this service is 500ms, verified across 100+ network-delay injection experiments"

> **💡 Agent usage tips**:
> - When the user questions "why run fault drills", explain the value along these four levels
> - When designing an experiment plan, state which value level this experiment addresses (e.g. "this experiment verifies that HPA autoscaling actually works")

---

### Q3: What are the seven principles of chaos engineering?

**A3**: Per the book *Chaos Engineering*, the discipline follows these principles:

**1. Build a hypothesis around steady-state behaviour**
- Before injecting, define explicitly what the system's "normal state" looks like
- **Example**: "under normal conditions the API service's P99 latency is < 200ms and its error rate < 0.1%"
- **Agent action**: when designing an experiment, first determine which steady-state metrics to monitor

**2. Vary real-world events**
- Faults should simulate problems that can actually happen in the real environment, not invented scenarios
- Common fault types: server crash, network delay, disk full, dependency unavailable, misconfiguration
- **Agent action**: prioritise high-frequency, high-impact fault types for drills

**3. Run experiments in production**
- Only production has real traffic, real load and real dependencies
- If drilling directly in production is impossible, at least use a staging environment that closely mirrors production
- **Agent action**: respect the safety boundary strictly; never inject into system namespaces or business-critical paths

**4. Automate experiments to run continuously**
- Running drills by hand is slow and error-prone; experiments should be automated
- Automated experiments can run frequently and catch regressions early
- **Agent action**: the Agent IS the automation vehicle — it should be able to design, execute and verify experiments autonomously

**5. Minimise blast radius**
- An experiment's impact should be as small as possible: start from a single Pod and expand gradually
- Use a canary strategy: inject into a few instances first, confirm safety, then widen the scope
- **Agent action**: safety checks (namespace blacklist, conflict detection, blast-radius assessment) MUST run before executing an injection

**6. Monitor system state in real time**
- Key metrics must be monitored live during the experiment, and it must be stopped the moment abnormal impact is detected
- Metrics should cover business (QPS, error rate), system (CPU, memory, network) and application (latency, throughput) dimensions
- **Agent action**: enter the verification phase immediately after injection to confirm the fault took effect as expected

**7. Analyse the results and improve the system**
- After every experiment, summarise: did the hypothesis hold? what problems were found? how do we improve?
- Document the results to build the organisation's knowledge base
- **Agent action**: persist experiment history to Operational Memory for later tasks to reference

> **💡 Agent usage tips**:
> - These seven principles are the Agent's **highest-order guidance** for designing experiments
> - When generating an experiment plan, check each principle in turn (especially #5 "minimise blast radius" and #6 "monitor in real time")
> - If the user asks for something that violates a principle (e.g. injecting into kube-system), refuse and explain why

---

## 2. Fault taxonomy and where each type applies

### Q4: What types of fault injection are there, and what are the typical scenarios for each?

**A4**: Grouped by the layer at which the fault occurs:

#### 4.1 Infrastructure-layer faults (Node level)

| Fault type | Description | Typical scenario | Verification focus |
|----------|------|---------|---------|
| **Node CPU fullload** | The node's CPU utilisation reaches 100% | Verify the scheduler avoids heavily loaded nodes; verify whether other Pods on the node are affected | `top node` shows the CPU spike; new Pods are not scheduled onto that node |
| **Node memory pressure** | The node's memory utilisation nears its ceiling | Verify kubelet triggers Pod eviction; verify the node enters MemoryPressure | `describe node` shows MemoryPressure=True; some Pods are Evicted |
| **Node disk full** | The node's disk utilisation reaches 100% | Verify log-write failures, container start failures, image-pull failures | `describe node` shows DiskPressure=True; Pods are Pending or FailedMount |
| **Node network cut** | The node loses cluster network connectivity | Verify the Node NotReady state; verify the Pod drift mechanism | `get nodes` shows the node as NotReady; Pods are rescheduled |
| **Node down** | The node is completely unavailable | Verify how the control plane detects the node failure; verify how workloads migrate | The node disappears from `get nodes`; Pods are rebuilt on other nodes |

#### 4.2 Container / Pod-layer faults

| Fault type | Description | Typical scenario | Verification focus |
|----------|------|---------|---------|
| **Pod CPU fullload** | A process in the Pod consumes a lot of CPU | Verify HPA scales up; verify the application's response latency increases | `top pod` shows CPU near the limit; the application's P99 latency rises |
| **Pod memory leak / OOM** | The Pod's memory grows until it is OOMKilled | Verify the memory limit is set sensibly; verify whether data is lost after the restart | `get pod -o json` shows exitCode=137; `restartCount` increases |
| **Pod kill** | Force-delete the Pod | Verify the Deployment rebuilds it automatically; verify the service is only briefly unavailable | A new Pod starts within 5-10s of the deletion; Endpoints is briefly empty |
| **Pod network delay** | A fixed delay is added to the Pod's ingress/egress traffic | Verify the application's timeout settings are sensible; verify whether downstream calls are affected | `exec -- ping` latency rises; the application log shows timeouts |
| **Pod packet loss** | The Pod's network packets are dropped at random | Verify the retry mechanism works; verify connection-reset handling | `exec -- ping` loss rate rises; the application log shows connection reset |
| **Pod DNS fault** | Name resolution inside the Pod is hijacked | Verify whether service discovery is affected; verify the DNS caching behaviour | `cat /etc/hosts` shows the #chaosblade entry; `ping <domain>` resolves to the forged IP; the application log shows resolve failed (⚠️ do NOT use nslookup/dig) |
| **Pod disk fill** | The volume mounted by the Pod is filled up | Verify the application's write-failure handling; verify log rotation | `exec -- df -h` shows the disk full; the application log shows no space left |
| **Pod process kill** | Kill a specific process inside the Pod | Verify the process supervision mechanism; verify the application's restart logic | The target process disappears from `exec -- ps aux`; the process is restarted |

#### 4.3 Application-layer faults

| Fault type | Description | Typical scenario | Verification focus |
|----------|------|---------|---------|
| **HTTP delay / error injection** | Intercept HTTP requests at the application layer and inject a delay or return an error code | Verify the circuit breaker trips; verify the fallback strategy works | The client receives 5xx errors or increased latency; the circuit breaker opens |
| **JVM GC pause** | Trigger a Full GC in a Java application, causing a STW (Stop-The-World) | Verify the JVM application's response jitter; verify the timeout settings | The application stops responding for tens of seconds; P99 latency spikes |
| **DB connection-pool exhaustion** | Simulate slow database queries filling the connection pool | Verify new requests are rejected; verify connection-pool monitoring alerts | The application log shows connection pool exhausted |

#### 4.4 ChaosBlade action reference

When the Agent builds a ChaosBlade command, `action` determines the fault behaviour. Common actions and the scenarios they map to:

| Action | Meaning | Applicable target | Typical parameters |
|--------|------|------------|---------|
| `fullload` | CPU fullload | cpu | `--cpu-percent` (default 100) |
| `load` | proportional load (memory) | mem | `--mode ram`, `--mem-percent` |
| `delay` | network delay injection | network | `--time` (ms), `--offset` (ms) |
| `loss` | packet loss | network | `--percent` |
| `duplicate` | packet duplication | network | `--percent` |
| `corrupt` | packet corruption | network | `--percent` |
| `fill` | disk filling | disk | `--path`, `--size`/`--percent` |
| `burn` | disk IO burn | disk | `--read`/`--write` |
| `kill` | process kill | process | `--process`, `--signal` |
| `stop` | process suspend | process | `--process` |

> **💡 Agent usage tips**:
> - **What the current skill catalogue covers**: mainly **infrastructure-layer** and **Pod-layer** faults (realised via ChaosBlade)
> - **Unsupported fault types**: application-layer faults usually require intrusive code changes or a service mesh (e.g. Istio Fault Injection) and are not supported yet
> - **Selection principle**: choose the fault type based on the user's intent and the target resource type
>   - If the user says "make this Pod slow" → choose `pod-network-delay`
>   - If the user says "test HPA" → choose `pod-cpu-fullload`
>   - If the user says "verify Pod self-healing" → choose `pod-kill`

---

### Q5: What is blast radius, and how do you control it?

**A5**: Blast radius is the size of the area a fault injection affects. **Controlling blast radius is the core of chaos-engineering safety.**

**The four dimensions of blast radius**:

| Dimension | Order from smallest to largest | How to control it |
|------|--------------|---------|
| **Resource granularity** | Container → Pod → ReplicaSet → Deployment → Namespace → Cluster | Start from a single Pod and widen gradually |
| **Port granularity** | specific port (--local-port) → all ports | For network faults, use --local-port to bound the impact so non-target traffic such as DNS and monitoring is unaffected |
| **Traffic share** | 1% → 5% → 10% → 50% → 100% | Bound the number of targets with a label selector |
| **Duration** | 10s → 30s → 60s → 600s | Set a short duration first and extend once it is confirmed safe |
| **Fault intensity** | mild delay (100ms) → moderate (1s) → severe (5s) → total outage | Tune the fault parameters via params |

**The Agent's safety-check flow** (MUST run in this order):

```
1. Namespace blacklist check
   ↓ if namespace in [kube-system, kube-public, istio-system, ...] → reject

2. Target existence verification
   ↓ kubectl get <resource> <name> -n <namespace>
   ↓ if it does not exist or its status != Running → reject

3. Conflict detection
   ↓ blade status checks for active experiments
   ↓ if there is a conflict → warn and require user confirmation

4. Blast-radius assessment
   ↓ count the matching targets
   ↓ if > threshold (e.g. 10 Pods) → mark as warning and require manual confirmation

5. Manual confirmation gate (when the user passed --confirm)
   ↓ generate the experiment plan, then pause for the user to approve/reject
```

**Best practice**:
- For a service's first fault drill, **pick a single Pod** and set duration to **30-60 seconds**
- Monitor the key metrics (error rate, latency, CPU/memory) during the observation window, and only widen the scope after confirming there is no abnormal impact
- For business-critical targets, **always enable the manual confirmation gate** (`--confirm`)

> **💡 Agent usage tips**:
> - When generating an experiment plan, state the blast radius explicitly (e.g. "blast radius of this experiment: 1 Pod, duration 60 seconds")
> - If the user asks for a large-scale injection ("inject into every Pod"), warn about the risk and recommend starting small
> - Blast-radius formula: `matching targets = kubectl get pods -l <selector> -n <namespace> --no-headers | wc -l`

---

## 3. The fault-injection lifecycle

### Q6: What phases does a complete fault-injection experiment contain?

**A6**: A standard fault-injection experiment has five phases:

```
Preparation → Injection → Observation → Recovery → Analysis
```

#### 6.1 Preparation

**Goal**: make sure the experiment environment is ready, and define the hypothesis and success criteria.

**Key activities**:
- Determine the experiment's goal: which hypothesis is being tested? (e.g. "when Pod CPU is at fullload, HPA should scale up within 2 minutes")
- Pick the target resource: which Deployment/Pod/Node?
- Define the steady-state baseline: what are the normal metrics before the experiment? (e.g. P99 latency 100ms, error rate 0.01%)
- Set up monitoring alerts: configure alert thresholds on the key metrics so the experiment is stopped the moment they are breached
- Prepare a rollback plan: if the experiment gets out of hand, how do we recover quickly?

> **🤖 The Agent's responsibilities**:
> 1. Load Operational Memory and check for historical conflicting experiments
> 2. Verify via `kubectl get` that the target resource exists and is healthy
> 3. Generate the experiment plan: fault type, target, parameters, expected duration
> 4. Trigger the manual confirmation gate (`ask_human`) when required

---

#### 6.2 Injection

**Goal**: execute the injection and confirm the action succeeded.

**Key activities**:
- Call ChaosBlade to run the injection command (e.g. `blade create pod cpu fullload`)
- Capture the experiment UID (needed for recovery later)
- Confirm via `blade status` that the experiment status is Running
- Record the injection timestamp for later metric analysis

> **🤖 The Agent's responsibilities**:
> 1. Activate the corresponding Skill and read its injection instructions
> 2. Build and run the blade command (via the `blade_create` tool)
> 3. **Layer 1 verification**: confirm the experiment was created via `blade_status`
> 4. Save `task_id` and `blade_uid` into AgentState for later recovery

---

#### 6.3 Observation

**Goal**: verify the fault took effect as expected, and observe whether the system behaves per the hypothesis.

**Key activities**:
- **Layer 1 verification**: confirm the ChaosBlade experiment is running
  ```bash
  blade status --uid <uid>
  # expected: { "code": 200, "result": { "status": "Running", "uid": "<uid>" } }
  ```

> **Note**: `blade status` in v1.8.0 does NOT support the `--kubeconfig` flag; the Agent passes cluster credentials internally via the `KUBECONFIG` environment variable.

- **Layer 2 verification**: use kubectl to verify the fault symptom appeared
  - Pod CPU fullload → `kubectl top pod my-pod -n default` shows CPU near the limit
  - Pod network delay → `kubectl exec my-pod -n default -- ping -c 3 <target>` shows increased latency

- **Layer 3 verification** (optional): compare laterally to confirm the blast radius is contained
  - Only the target Pod is affected; the Deployment's other Pods are healthy
  - The Service's overall error rate has not risen significantly

> **🤖 The Agent's responsibilities**:
> 1. Read the "verification method" section of the Skill to get the recommended kubectl verification commands
> 2. Run the verification commands, parse the output and judge whether it matches expectations
> 3. If verification fails (e.g. CPU did not rise after injection), **trigger a rollback automatically** and record the failure reason
> 4. If verification passes, mark the experiment as active and enter the waiting period (duration countdown)

---

#### 6.4 Recovery

**Goal**: stop the injection and confirm the system returned to normal.

**Key activities**:
- Call `blade destroy <uid>` to destroy the experiment
- Confirm via `blade status --uid <uid>` that the status became Destroyed
- Use kubectl to verify the fault symptom is gone
  - `kubectl top pod` shows CPU back to normal levels
  - `kubectl exec -- ping` shows latency back to normal
- Confirm the steady state is restored (Pod Ready=True, Endpoints non-empty)

> **🤖 The Agent's responsibilities**:
> 1. Accept the user's recover request (by task_id, or in force mode)
> 2. Restore AgentState from the Checkpointer and get blade_uid
> 3. **Layer 1 recovery verification**: confirm the experiment is still Running (guards against double recovery)
> 4. Call `blade destroy` to perform the recovery
> 5. **Layer 2 recovery verification**: confirm the fault symptom is gone
> 6. Update the task state to recovered and persist the experiment history to Operational Memory

**Standard recovery commands**:
```bash
# destroy the experiment
blade destroy <uid>

# confirm the experiment is destroyed
blade status --uid <uid>
# expected: { "code": 200, "result": { "status": "Destroyed", "uid": "<uid>" } }
```

> **Recovering non-ChaosBlade faults**: the following fault types are NOT recovered by `blade destroy`; they need the inverse kubectl operation:
>
> | How it was injected | Recovery command |
> |---------|---------|
> | `kubectl scale --replicas=0` | `kubectl scale --replicas=<original value>` |
> | `kubectl cordon <node>` | `kubectl uncordon <node>` |
> | `kubectl patch pvc` (wrong SC) | `kubectl patch pvc` back to the correct storageClassName |
> | `kubectl taint` | `kubectl taint <node> <key>-` |

---

#### 6.5 Analysis

**Goal**: analyse the results, distil the lessons learned, and update the knowledge base.

**Key activities**:
- Collect the metric data from the experiment window (timeline, durations, execution stats)
- Compare system state before and after to assess the impact
- Answer the hypothesis: did it hold? what problems were found?
- Record the results in Operational Memory for later tasks to reference
- If a new fault mode or verification method was discovered, update MEMORY.md

> **🤖 The Agent's responsibilities**:
> 1. Emit the four metric dimensions via `blade-ai metric --task-id <id>`
> 2. Save the experiment digest (goal, constraints, progress, decisions, next steps) to Session Memory
> 3. Save the experiment history record (task_id, fault_type, targets, state, timestamps) to `experiments.jsonl`
> 4. If anything unexpected happened during the experiment (verification failure, safety interception), record it in MEMORY.md as operational experience

---

## 4. The layered fault-verification model

### Q7: [AUTHORITATIVE SOURCE] What are Layer 1 / Layer 2 / Layer 3 verification, and how do they relate?

**A7**: Fault verification uses a three-layer model, going deeper layer by layer:

| Layer | Name | What it verifies | Method | Reliability |
|------|------|---------|---------|--------|
| **Layer 1** | Injection-action verification | Was the ChaosBlade experiment created/destroyed successfully | `blade status --uid <uid>` | High (queries the experiment status directly) |
| **Layer 2** | Symptom verification | Did the system exhibit the expected fault symptom | `kubectl top/exec/describe/logs` + metric assertions | Medium-high (depends on parsing kubectl output) |
| **Layer 3** | Impact verification | Is the fault's blast radius contained and as expected | Lateral comparison + business-metric monitoring | Medium (needs cross-checking across dimensions) |

**How they relate**:
- **Layer 1 is a necessary condition**: if Layer 1 fails (the experiment was not created), skip Layer 2/3 and roll back immediately
- **Layer 2 is a sufficient condition**: passing Layer 2 means the fault really took effect, but not that the impact is as expected
- **Layer 3 is enhanced verification**: used for complex scenarios, e.g. verifying cascading failures or assessing business impact

**The Agent's verification strategy**:
- **Injection verification**: Layer 1 + Layer 2 are mandatory. Layer 3 is optional, depending on whether the Skill defines lateral-comparison rules
- **Recovery verification**: Layer 1 (confirm the experiment was destroyed) + Layer 2 (confirm the symptom is gone) are mandatory
- **Handling verification failure**: if Layer 2 fails (e.g. CPU did not rise after injection), the Agent MUST **automatically trigger** `blade destroy` to roll back, and record the failure reason in the experiment history

**A complete verification example (Pod CPU fullload)**:

```
┌─────────────────────────────────────────────┐
│ Layer 1: injection-action verification      │
├─────────────────────────────────────────────┤
│ command: blade status --uid abc123          │
│ output:  { "code": 200, "result": {         │
│           "status": "Running",               │
│           "uid": "abc123"                    │
│         } }                                  │
│ verdict: experiment created ✓               │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ Layer 2: symptom verification               │
├─────────────────────────────────────────────┤
│ command: kubectl top pod my-pod -n default  │
│ output:  NAME    CPU(cores)  MEMORY(bytes)  │
│          my-pod  480m        256Mi          │
│ assert:  CPU 480m / Limit 500m = 96%        │
│          96% > 80% threshold → pass ✓       │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ Layer 3: impact verification (optional)     │
├─────────────────────────────────────────────┤
│ command: kubectl top pod -l app=my-app -n d │
│ output:  my-pod-1: 480m (96%)               │
│          my-pod-2: 50m  (10%)               │
│          my-pod-3: 60m  (12%)               │
│ verdict: only the target Pod is affected    │
└─────────────────────────────────────────────┘
```

> **💡 Agent usage tips**:
> - Layer 1 verification MUST use the `blade_status` tool — do not hand-assemble the command
> - Layer 2's commands and assertion logic should be read from the Skill's "verification method" section
> - If Layer 2 fails, retry 1-2 times first (3-5s apart) to rule out a timing issue; if it still fails, trigger a rollback

---

### Q8: Why does Layer 2 verification need an LLM? Why not just a rule engine?

**A8**: Four reasons Layer 2 verification needs an LLM:

**1. The diversity of verification logic**
- Verification methods differ widely between fault types:
  - Pod CPU fullload: inspect `top pod` output
  - Pod OOM: inspect exitCode and reason in `get pod -o json`
  - Pod network delay: run `ping` inside the Pod and parse the latency
  - Node disk full: inspect the Conditions in `describe node`
- A rule engine would need hard-coded verification logic per fault type, which does not scale
- **LLM advantage**: it can read the Skill's "verification method" section and generate the verification steps dynamically

**2. Output is not deterministic**
- kubectl output is structured (JSON), but the field meanings require semantic understanding
- **Example**: `status.containerStatuses[0].state.waiting.reason` may be `ImagePullBackOff`, `CrashLoopBackOff` or `ContainerCreating`, each meaning something different
- **LLM advantage**: it can judge from context whether the current state matches expectations

**3. Fusing multiple data sources**
- Complete verification may require combining several sources:
  - live metrics from `kubectl top`
  - Events from `kubectl describe`
  - application logs from `kubectl logs`
  - in-container state from `kubectl exec`
- **LLM advantage**: it can synthesise all of this into a more accurate judgement

**4. Handling abnormal situations**
- When the result does not match expectations, the LLM can analyse the possible causes:
  - wrong injection parameters?
  - target resource missing?
  - fault masked by a self-healing mechanism?
  - verification command itself failed?
- **Rule-engine limitation**: covering every abnormal case is very hard

> **🤖 How the Agent implements this**:
> 1. A Skill's SKILL.md contains a "verification method" section describing the recommended kubectl commands and expected output
> 2. In the Layer 2 phase, the LLM reads that section and produces a verification plan
> 3. The LLM calls the kubectl tool, runs the commands, parses the output and judges pass/fail
> 4. On failure, the LLM decides whether to retry, adjust parameters, or roll back

---

## 5. Fault-injection safety red lines

### Q9: [SAFETY RED LINES] Which operations are absolutely forbidden, and why?

> **Positioning note**: `skills/k8s-chaos-skills/SKILL.md` is the **authoritative source** for the safety red lines (user-maintained, loaded by the Agent every session via `activate_skill`, and containing executable operating procedures). This document provides the **design rationale behind** those red lines (developer-maintained, explaining the engineering considerations behind each one), helping the Agent understand the "why" and not just the "what". When the two conflict, SKILL.md wins.

**A9**: The following are the **safety red lines** of fault injection; the Agent MUST obey them strictly:

#### 9.1 Namespaces that must never be injected into (blacklist)

| Namespace | Reason | Consequence |
|----------|------|------|
| `kube-system` | Contains control-plane components (etcd, apiserver, scheduler, controller-manager, CoreDNS) | The cluster may become completely unusable and unrecoverable |
| `kube-public` | System-reserved namespace | Cluster-wide configuration may be affected |
| `istio-system` / `linkerd` and other service-mesh namespaces | Contains the service-mesh control plane | Inter-service communication across the whole cluster may break |
| `monitoring` / `logging` and other observability namespaces | Contains Prometheus, Grafana, ELK and similar monitoring components | Monitoring may fail, leaving the fault's impact unobservable |

> **🤖 How the Agent implements this**:
> - Before running any blade/kubectl command, ToolGuard checks the target resource's namespace
> - If the namespace is on the blacklist, it **refuses to execute** and returns a safety-interception error
> - The blacklist is configurable via the `BLADE_AI_SAFETY_BLACKLIST_NS` environment variable

---

#### 9.2 No destructive experiments on a StatefulSet (when there is no backup)

**Reason**:
- A StatefulSet usually runs stateful applications (databases, message queues, caches)
- Destructive experiments (Pod kill, disk filling) can lose or corrupt data
- Even when the PVC is retained, data consistency may still be broken

**Exception**:
- If the user explicitly confirms a backup exists AND the experiment's purpose is to verify the backup-restore procedure, it may proceed
- **Agent action**: during confirmation, require evidence that the backup exists (e.g. the backup job's status)

---

#### 9.3 No large-scale injection during production peak hours

**Reason**:
- Traffic pressure is high at peak and the system's headroom is small, so an injection can trigger an avalanche
- Even with a small blast radius by design, cascading effects can still reach a wide range of services

**Best practice**:
- Run experiments during off-peak hours (e.g. 02:00-04:00)
- For a service's first experiment, **pick a single Pod** with duration ≤ 60 seconds
- Monitor the key business metrics throughout, and stop immediately on any anomaly

---

#### 9.4 Never inject multiple interdependent faults at once

**Reason**:
- Injecting into the database and the application at the same time, for example, makes the problem hard to localise
- Stacked faults can produce unpredictable cascading effects

**Best practice**:
- **One experiment injects exactly one fault type**
- To verify a compound-fault scenario, split it into several experiments and record the result of each

---

#### 9.5 Never run an experiment without monitoring alerts configured

**Reason**:
- Without monitoring alerts, an experiment going out of control cannot be detected in time
- The fault's impact may widen and even reach production business traffic

**Best practice**:
- Before the experiment, confirm alerts are configured on the key metrics (QPS, error rate, latency, CPU, memory)
- Set stricter experiment-window alerts than the normal thresholds (e.g. if the normal error rate is < 0.1%, set the experiment-window alert threshold to 0.5%)

> **💡 Agent usage tips**:
> - When generating an experiment plan, check all five safety red lines above
> - If the user asks for something that violates a red line, **refuse** and explain why
> - A safety-interception error message should state clearly which red line was violated and how to fix it

---

## 6. Typical applications of fault injection

### Q10: What are the typical real-world applications of fault injection?

**A10**: 

#### 10.1 Verifying a high-availability architecture

**Scenario**: a microservice runs 3 replicas and claims "a single Pod failure does not affect service availability".

**Experiment design**:
- **Injection**: kill one Pod at random (`blade create pod kill`)
- **Verification**:
  - Layer 1: confirm the experiment was created
  - Layer 2: confirm the Pod was deleted and the Deployment created a replacement automatically
  - Layer 3: confirm the Service Endpoints is briefly empty during the deletion but recovers within 5 seconds; the service's overall error rate stays < 0.1%

**Expected result**: a brief wobble (< 5 seconds), but the service stays available overall

**Problems this may uncover**:
- If Endpoints takes > 30 seconds to recover, the health-check configuration is unreasonable (periodSeconds too long)
- If the error rate exceeds 1%, the client's retry mechanism is not working or the timeout is too short

---

#### 10.2 Verifying HPA autoscaling

**Scenario**: a service has HPA configured and is expected to scale out when CPU > 70%.

**Experiment design**:
- **Injection**: inject CPU fullload into one of its Pods (`blade create pod cpu fullload`)
- **Verification**:
  - Layer 1: confirm the experiment was created
  - Layer 2: `kubectl top pod` shows the target Pod's CPU near its limit
  - Layer 3: `kubectl get hpa` shows currentReplicas increasing; `kubectl get deployment` shows replicas increasing

**Expected result**: HPA scales out within 2-5 minutes and the new Pods share the load

**Problems this may uncover**:
- If HPA does not scale out, metrics-server may be missing or misconfigured
- If CPU stays high after scaling out, maxReplicas may be set too low

---

#### 10.3 Verifying timeout and retry behaviour

**Scenario**: service A calls service B with a 3-second timeout and 3 retries configured.

**Experiment design**:
- **Injection**: inject a 5-second network delay into service B's Pod (`blade create pod network delay --time 5000`)
- **Verification**:
  - Layer 1: confirm the experiment was created
  - Layer 2: run `ping` inside service B's Pod and confirm latency rose to ~5 seconds
  - Layer 3: service A's logs show timeout and retry records; service A's overall error rate stays < 1% (because the retries succeed)

**Expected result**: some of service A's requests time out but eventually succeed via retries, keeping the overall error rate contained

**Problems this may uncover**:
- If the error rate exceeds 10%, there are too few retries or the timeout is too short
- If service A's connection pool is exhausted, the pool size needs tuning or a circuit breaker is needed

---

#### 10.4 Verifying that monitoring alerts actually work

**Scenario**: a Pod OOMKilled alert is configured, but has never been verified to actually fire.

**Experiment design**:
- **Injection**: inject memory pressure into a Pod (`blade create pod mem load`) so its memory usage approaches the limit
- **Verification**:
  - Layer 1: confirm the experiment was created
  - Layer 2: `kubectl top pod` shows memory near the limit; `kubectl get pod -o json` shows the container was OOMKilled (exitCode 137)
  - Layer 3: confirm the alerting system sent a notification within 1 minute, and that the SRE received it and responded

**Expected result**: the alert fires promptly and the SRE can locate the problem quickly

**Problems this may uncover**:
- If the alert never fires, the alert rule is misconfigured or metric collection is broken
- If the alert is > 5 minutes late, the alert evaluation window needs tuning

---

#### 10.5 Verifying the disaster-recovery procedure

**Scenario**: after the database primary fails, the standby should take over automatically.

**Experiment design**:
- **Injection**: kill the database primary's Pod (`blade create pod kill`)
- **Verification**:
  - Layer 1: confirm the experiment was created
  - Layer 2: confirm the primary's Pod was deleted and the StatefulSet created a new Pod
  - Layer 3: confirm the standby was promoted to primary, application connections switched to the new primary automatically, and data consistency was not compromised

**Expected result**: the database completes failover within 30-60 seconds; the application errors briefly, then recovers

**Problems this may uncover**:
- If failover takes > 5 minutes, the election algorithm or heartbeat detection needs tuning
- If data is inconsistent, the replication mechanism needs strengthening or backups need to be more frequent

---

## 7. The Agent's role in fault injection

### Q11: What advantages does the Agent have over traditional fault-injection tools (such as using ChaosBlade directly)?

**A11**: 

**1. Intelligent fault design**
- Traditional tools: the user must pick the fault type, target and parameters by hand, and it is easy to get wrong
- Agent: understands the user's intent from natural language and picks a suitable fault type and parameters automatically
  - **Example**: the user says "make this Pod's network slow"; the Agent picks `pod-network-delay` and infers a reasonable delay from context

**2. Automated verification flow**
- Traditional tools: the user must run kubectl commands by hand to check whether the fault took effect
- Agent: reads the verification method from the Skill automatically, runs Layer 1 + Layer 2 verification, and judges pass/fail

**3. Built-in safety protection**
- Traditional tools: the user may make a mistake and inject into a system namespace, or inject twice
- Agent: built-in safety checks (namespace blacklist, conflict detection, blast-radius assessment) intercept dangerous operations automatically

**4. Accumulating and reusing experiment history**
- Traditional tools: every experiment is isolated and the experience is never captured
- Agent: persists experiment history to Operational Memory, so later tasks can draw on past experience and avoid repeating mistakes

**5. A natural-language interface**
- Traditional tools: require memorising complex blade command parameters
- Agent: supports natural-language descriptions (the `--nl` parameter), lowering the barrier to entry

**6. Real-time status tracking**
- Traditional tools: the user has to query the experiment status manually
- Agent: pushes execution status live via SSE or stderr, so the user always knows the progress

---

### Q12: What are the Agent's limitations? When should the Agent NOT be used?

**A12**: 

**Limitations**:

1. **Dependent on the LLM's reasoning ability**
   - If the LLM misreads the user's intent, it may pick an unsuitable fault type
   - **Mitigation**: offer a structured-parameter mode (`--fault-type` etc.) that bypasses LLM reasoning

2. **Verification logic depends on Skill quality**
   - If a Skill's "verification method" section is inaccurate, Layer 2 verification may fail
   - **Mitigation**: review and refine Skill content regularly

3. **Cannot handle complex cross-service fault scenarios**
   - The Agent currently targets single-resource (Pod/Node) fault injection
   - Verifying cascading failures across several services still needs a human-designed experiment flow
   - **Future extension**: support multi-step experiment orchestration

4. **Limited support for minimal images**
   - If the target Pod uses a distroless or scratch image, `kubectl exec` may not be able to run verification commands
   - **Mitigation**: have the Skill provide fallback verification (e.g. via `kubectl top` or application logs)

**When NOT to use the Agent**:

1. **Emergency fault recovery**
   - If production is already broken and needs immediate recovery, calling `blade destroy` directly is faster
   - The Agent's ReAct loop and verification flow add latency

2. **Highly customised fault scenarios**
   - Injecting a custom fault (modifying application code, returning a specific HTTP error code) is not supported today
   - Use a purpose-built fault-injection framework instead (Chaos Mesh, LitmusChaos)

3. **Large-scale parallel experiments**
   - Injecting into hundreds of Pods at once is inefficient with the Agent's serial execution model
   - Use a batch script or a dedicated chaos-engineering platform instead

---

## 8. Glossary

| Term | Definition |
|------|------|
| Chaos Engineering | The discipline of experimenting on a distributed system, deliberately injecting faults to build confidence in its ability to withstand turbulent conditions |
| Fault Injection | The act of deliberately introducing a fault into a system to verify its resilience and fault tolerance |
| Blast Radius | The size of the area a fault injection affects; should be kept as small as possible |
| Steady State | The system's behaviour under normal conditions; the control baseline for a fault experiment |
| Hypothesis | A prediction of how the system will behave under the fault, e.g. "when Pod CPU is at fullload, HPA will scale out" |
| Self-healing | Kubernetes's ability to repair faults automatically: Pod recreation, node eviction, Endpoint updates |
| Cascading Failure | One component's failure propagating through dependencies to others, causing widespread failure |
| MTTR | Mean Time To Recovery — how fast the system recovers from a fault |
| RTO | Recovery Time Objective — the maximum downtime the business tolerates |
| RPO | Recovery Point Objective — the maximum data loss the business tolerates |
| Layer 1 verification | Injection verification — confirms the ChaosBlade experiment was created/destroyed successfully |
| Layer 2 verification | Phenomenon verification — confirms the system exhibited the expected fault symptom |
| Layer 3 verification | Impact verification — confirms the fault's blast radius is contained |
| Skill | The Agent's skill module, defining a fault type's injection instructions, verification method and preconditions |
| Operational Memory | The Agent's operational memory, storing experiment history and operational experience |
