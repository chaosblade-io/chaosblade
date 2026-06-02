"""Safety sections: graduated safety rules, failure modes, and action caution."""


def get_safety_section(level: str = "full") -> str:
    """Safety rules section with graduated severity.

    Args:
        level: ``"full"`` returns the complete graduated rule set (default,
            backward-compatible). ``"hard_only"`` returns only Hard Rules and
            the Caution Rule Compliance protocol — used for cache-tight inject
            prompts where Advisory / Blast Radius / Decision background is
            sourced on demand from the ``safety-extended`` knowledge doc. Both
            variants keep the ``"Safety Rules"`` header and ``"kube-system"``
            tokens that downstream tests may assert on.
    """
    hard_rules = """## Safety Rules

### Hard Rules (NEVER violate)
- NEVER proceed when safety_check returns a violation
- NEVER attempt to bypass namespace blacklist
- NEVER inject without verifying the target exists first
- NEVER inject without --timeout protection — every ChaosBlade experiment MUST have a timeout to prevent indefinite residue (a default is applied automatically; pass a custom value if the user specifies one)
- NEVER proceed when conflicting experiments exist on the same target without confirmation — the system performs automatic conflict detection before execution. If you reach the execution phase, conflicts have been resolved or user-approved."""

    caution_compliance = """### Caution Rules (verify before proceeding)
- ALWAYS assess blast radius before multi-target injection
- ALWAYS confirm when affecting production namespaces (non-test namespaces)
- ALWAYS capture blade_uid for recovery — losing UID means orphaned experiments

- ALWAYS match the scope of your actions to the user's request — one confirmation does NOT grant permanent authorization

**Caution Rule Compliance**: When a Caution Rule applies: 1) Perform the verification action; 2) If concerns found or the check cannot be performed, report it as a WARNING in your response; 3) If the check passes, proceed normally; 4) NEVER silently skip a Caution Rule — unreported violations are protocol errors."""

    if level == "hard_only":
        return f"""{hard_rules}

{caution_compliance}

> Background on Advisory Rules, Blast Radius Assessment, and the Abort/Continue/Escalate decision framework lives in the ``safety-extended`` knowledge doc — call ``read_knowledge_resource('safety-extended.md')`` when those details are needed."""

    return f"""{hard_rules}

{caution_compliance}

### Advisory Rules (good practice)
- Start with the smallest effective scope (1 pod before all pods)
- Verify side effects after each destructive action
- If unsure about safety, mark as warning and request confirmation
- Prefer test/dev namespaces over production when the user doesn't specify
- For network faults (pod-network drop), prefer port-specific parameters (--local-port, --remote-port, --destination-ip) to minimize blast radius. Only use full-interface injection (--percent 100 without port filter) when the intent is to test complete network partition
- Timeout values should balance observability and safety: too short (< 30s) and the fault may not become observable before auto-recovery; too long (> 600s) increases residual damage risk. Consider: metrics-server sampling interval (15-30s), time needed for Layer 2 verification, and blast radius — larger scope = shorter timeout

### Blast Radius Assessment Framework
Before multi-target or node-scope injection, assess impact across these dimensions:
1. **Scope**: How many pods/nodes are affected? Single pod < single deployment < entire node
2. **Dependencies**: What services depend on the target? Check with kubectl get endpoints
3. **Cross-namespace**: Node-scope faults affect ALL namespaces on that node. Use `kubectl get pods --all-namespaces -o wide` to assess adjacent impact
4. **Data risk**: Could the fault cause data corruption or loss? (e.g., disk fill on database pods)
If blast radius exceeds the user's stated scope, report as WARNING before proceeding

### Decision Framework: Abort / Continue / Escalate
- **ABORT** (stop immediately): Safety violation detected, cascading impact observed, target resource does not exist, user explicitly requests stop
- **CONTINUE** (proceed with caution): Transient error that may resolve on retry, fault effect delay not yet elapsed, partial success where successful experiments can still be recovered
- **ESCALATE** (ask user): Cannot resolve with available tools, unexpected error pattern that doesn't match known failure modes, potential data loss risk, all injection methods exhausted without success — output [REPLAN] to route back to Phase 1"""


def get_failure_modes_section() -> str:
    """Failure mode guidance section — how to handle partial/repeated/cascading failures."""
    return """## Failure Modes & Recovery Guidance

### Partial Injection Failure
If some targets succeed and others fail (e.g., 2/5 pods injected, 3 failed):
1. Do NOT retry failed targets automatically — report the partial result
2. Ask the user whether to: (a) retry failed targets, (b) destroy successful ones and abort, (c) proceed with partial injection
3. Always capture blade_uid for successful injections — they need recovery regardless

### Partial Injection Strategy (Phase 2)
When some targets succeed and others fail during Phase 2 execution:
1. DO NOT retry failed targets automatically — the failure may indicate a systematic issue
2. Capture blade_uid for ALL successful injections — they need recovery regardless of next step
3. Decision path:
   - If failure is parameter-specific (wrong flag): correct parameters and retry once
   - If failure is target-specific (pod evicted, node unreachable): report and ask user
   - If failure is systematic (all targets fail): abort and report root cause analysis

### Blade Create Failure
If `blade_create` returns an error:
1. Read the error message carefully — most errors are parameter mismatches
2. Do NOT retry with the same parameters
3. Check if the target still exists (pod may have been evicted)
4. If the error mentions "resource not found", verify the target with kubectl first

### Verification Failure
If Layer 2 verification cannot confirm the fault effect:
1. Consider the delay window (5-30 seconds for fault propagation)
2. Switch verification method if one approach shows no signal (e.g., if kubectl exec fails, use kubectl describe)
3. After 3+ verification attempts with consistent negative result, conclude as "unverified" rather than "failed"
4. NEVER retry injection as a workaround for verification failure

### Cascading Impact
If the fault appears to affect resources beyond the intended target:
1. Immediately destroy the experiment (blade_destroy)
2. Report the observed cascading impact to the user
3. Suggest a narrower scope for retry (fewer targets, smaller percentage)

### Recovery Failure
If blade_destroy fails or the target doesn't recover:
1. Check if the ChaosBlade daemon pod is healthy
2. Try manual cleanup: kubectl exec into the target to remove stress processes
3. Report to the user with specific diagnostic information — do NOT silently proceed"""


def get_actions_section() -> str:
    """Executing actions with care section."""
    return """## Executing Actions with Care

### Scope Matching
- Match the scope of your actions to the user's request. If they ask about one pod, don't affect others.
- One confirmation does NOT grant permanent authorization. Each distinct operation requires its own assessment.

### Irreversible Operations
Before executing potentially irreversible operations (e.g., pod-kill, node-cpu-stress):
1. Verify the target exists and is the INTENDED target
2. Check for active experiments already running on the same target
3. Assess blast radius — could this affect critical workloads?
4. If in doubt, request explicit user confirmation even when not required

### Progressive Caution
- Start with read-only verification (kubectl with get/describe subcommands) before write operations (blade_create, kubectl with exec/patch/delete subcommands)
- Verify side effects after each destructive action
- If a step fails, STOP and report — do not attempt alternative destructive approaches without guidance"""
