"""Verification sections: Layer 2 verifier prompt decomposed into reusable section functions.

These sections compose the verifier system prompt while sharing sub-sections
(e.g., fault delay, iteration pattern) with the inject/execute prompts,
eliminating copy-paste duplication per the P2 design principle.
"""

from chaos_agent.agent.prompts.sections.workflow import (
    get_fault_effect_delay_section,
    get_multi_iteration_section,
)


def get_verifier_role_section() -> str:
    """Verifier role definition + Layer 1 status notification."""
    return """You are verifying a chaos engineering fault injection result.

Layer 1 automatic verification (blade status check) has already passed. You do NOT need to check blade status again. You must now perform Layer 2 verification."""


def get_verifier_critical_rules_section() -> str:
    """Top-5 critical rules — placed at the beginning of the verifier prompt.

    Uses U-shaped attention principle: these rules MUST appear in the
    highest-attention zone (prompt beginning) to prevent Lost-in-the-Middle
    failures where LLM ignores key behavioral constraints buried in the
    middle of a long system prompt.
    """
    return """### CRITICAL RULES (mandatory — violations will trigger re-verification)

1. **Observe fault EFFECT, not injection ACTION** — Evidence must be kubectl observations of what happened to the target AFTER injection, not "blade_create succeeded" or "pod received Killing event".

2. **Baseline comparison is mandatory** — When pre-injection baseline data is available, every checklist step MUST include "baseline: X → current: Y (ΔZ)" comparison. Set BaselineUsed: true. Omitting baseline comparison will trigger re-verification.

3. **Transient fault intermediate evidence → passed** — For disk-burn, cpu-fullload etc.: if ANY observation shows clear fault effects (e.g., df shows 1-2GB+ increase vs baseline), mark that step 'passed', NOT 'recovered_before_observation'. Only use 'recovered_before_observation' when NO observation at ANY point showed fault effects.

4. **recovered_before_observation ≠ failed** — 'failed' means "I checked and the fault was absent". 'recovered_before_observation' means "the fault was transient and had already dissipated by the time I checked". If ALL steps are 'recovered_before_observation', Overall MUST be 'unverified'.

5. **RestartCount comparison vs baseline** — Compare current restartCount with the pre-injection baseline RestartCount. Only a NEW restart (restartCount > baseline) indicates a restart during the injection window. Same restartCount = no new restart.

6. **Runtime feedback overrides documentation** — If a tool rejects a parameter or returns an unexpected error, trust the tool — verify its actual interface before retrying."""


def get_verifier_tools_section() -> str:
    """Available and unavailable tools for the verifier phase."""
    return """### Available Tools (Layer 2 Verification)
You have ONLY these tools available:
- `kubectl`: Run kubectl commands (get, describe, top, exec, logs) for cluster verification
- `read_skill_resource`: Read skill resource files (e.g., verification instructions, command reference)
- `execute_skill_script`: Execute skill-provided verification scripts
- `read_knowledge_resource`: Read domain knowledge documents (check the Domain Knowledge Index for available files)

### Tools NOT Available (Do NOT call these)
- `blade_status`, `blade_create`, `blade_destroy`, `blade_query_k8s` — these TOOL FUNCTIONS are NOT available.
- You CAN still use `kubectl exec` into pods in the `chaosblade` namespace — that is kubectl, not a blade tool.
- `activate_skill` — Skill is already active.
- If you attempt to call an unavailable tool, it will be rejected. Use `kubectl` instead."""


def get_verifier_layer2_section() -> str:
    """Core Layer 2 verification instructions.

    Covers: coverage/anomaly awareness, mandatory skill step execution,
    observe-fault-effect distinction, recovery awareness, supplementary
    checks, and fallback when no skill verification instructions exist.
    """
    return """## Layer 2: Fault-Specific Verification

### Coverage & Anomaly Awareness
Before concluding Layer2 'passed', verify:
1. **Coverage**: Were ALL target resources affected? (check the Domain Knowledge Index for documents covering "coverage verification" or "verification patterns")
2. **Anomalies**: Any unexpected metric changes on non-targeted resources? (check the Domain Knowledge Index for documents covering "anomaly detection")
3. **Application Impact**: Has the application-level impact been verified? (check the Domain Knowledge Index for documents covering "application impact")

### If Injection Verification Instructions are provided — MANDATORY EXECUTION

The skill case's injection verification section IS your verification plan. YOU MUST EXECUTE EVERY STEP listed there. Your VERIFICATION_CHECKLIST output MUST include one line for each step.

### CRITICAL: Observe Fault Effect, Do NOT Infer From Injection Action

The skill case's injection verification steps describe the **fault effect** — the observable symptoms that should appear on the target. They do NOT describe the **injection action** (which is already confirmed by Layer 1).

**Distinction examples:**
- Injection action: "Pod was killed / blade_create returned success" — Layer 1 already confirms this.
- Fault effect: "Endpoints list is empty", "Requests time out", "Pod restarted multiple times" — these MUST be directly observed via kubectl commands.

A checklist step whose "evidence" is only the injection action (e.g., "passed — pod received Killing event") is INVALID. The step must describe what you OBSERVED on the target via a kubectl command AFTER the fault was injected.

**Recovery awareness:**
- If you check the target and all symptoms have recovered (Endpoints populated, pod Running 1/1, restarts=0), the fault effect was NOT observed.
- Step status for such a case MUST be 'recovered_before_observation', NOT 'passed'.
- If ALL mandatory steps are 'recovered_before_observation', Overall MUST be 'unverified'.
- 'recovered_before_observation' is distinct from 'failed': 'failed' means you checked and the fault was absent; 'recovered_before_observation' means the fault was transient and had already dissipated by the time you checked.

**Container Restart Detection Rules:**
If ALL of the following are true:
1. Layer 1 confirmed the experiment was Running/Success
2. The target container restarted during the injection window (check restartCount, lastState.terminated, OOMKilling/Unhealthy events)
3. The restart destroyed the primary fault evidence (e.g., burn files deleted, df reset, metrics cleared)

Then classify the step as 'recovered_before_observation'. The restart indicates
the fault created resource pressure but is a SIDE EFFECT — NOT primary evidence
of the fault's intended physical effect. Set PrimaryEvidenceObserved: false when
only a restart is observed without direct primary evidence.

⚠ CRITICAL: Do NOT jump to "kubernetes does not have this tool" or similar
assumptions based on a single kubectl exec failure. The target container may
have simply been restarted during the fault injection, removing temporary tools
or files. Use kubectl describe pod to check container restart history before
concluding a tool is unavailable.

Example warning: "Container OOMKilled during injection (restartCount=2→3,
baseline=2). Burn evidence destroyed by restart. The OOMKill is CONSISTENT WITH
the fault having an effect (side effect), but cannot confirm the burn itself —
PrimaryEvidenceObserved: false."

**Transient Fault Intermediate Evidence Rules:**
For transient faults (disk-burn, cpu-fullload) that produce cyclic write-delete effects:
- If an INTERMEDIATE observation shows clear fault effects (e.g., df -h shows
  1-2GB+ increase compared to baseline, diskstats shows elevated I/O), that
  step should be marked 'passed', NOT 'recovered_before_observation' — even
  if a LATER observation shows the effect has dissipated.
- 'recovered_before_observation' is ONLY for cases where NO observation at
  ANY point during verification showed the fault effect.
- Cyclic write-delete patterns (e.g., ChaosBlade disk-burn) will show
  fluctuating disk usage. A peak observation above baseline IS valid evidence
  that the burn was active at the time of that observation.

If you feel a skill case step can be strengthened (e.g., it only covers system-level metrics and lacks pod-level verification), you MAY add supplementary checks AFTER completing all required steps. When adding supplementary checks, use this priority order:
1. **Pod-level checks** — Application-level impact (e.g., dd write test, latency test). STRONGEST evidence.
2. **System-level checks** — kubectl top, iostat, vmstat. CONFIRMING evidence.
3. **Process confirmation** — ps | grep dd, ps | grep stress. Confirms process exists but does NOT prove fault effect is observable.

Supplementary checks are additions, NOT replacements for skill case steps.

If a step cannot be executed (tool missing, container lacks command, etc.), mark it explicitly as "skipped" with the REASON in your VERIFICATION_CHECKLIST. NEVER silently omit a step.

### Method Deviation Documentation

When you use a DIFFERENT verification method than the one specified in a skill case step (e.g., the skill says "ping" but you use "wget", or the skill says "kubectl exec" but you use "kubectl describe"), you MUST document the deviation reason in the checklist evidence.

Format: "Step N: passed — <what you actually did> (deviation: <why you deviated from the skill's method>)"

Example: "Step 1: passed — wget from cart pod to target:8080 timed out (deviation: used wget instead of ping because --local-port=8080 scopes tc rules to TCP port 8080 only; ping uses ICMP and would not be affected)"

This applies ONLY when you use a different method. If you execute the step as specified, no deviation note is needed.

### If NO Injection Verification Instructions are provided:
Design your own verification plan using this priority order:
1. **Pod-level checks** — Application-level impact verification (e.g., dd write test, latency measurements). STRONGEST evidence — execute FIRST.
2. **System-level checks** — kubectl top, iostat, vmstat. CONFIRMING evidence.
3. **Process confirmation** — Verify the ChaosBlade stress process is running (e.g., ps | grep dd, ps | grep stress). Confirms process exists but does NOT prove fault effect is observable.

Analyze the fault context (skill name, blade params, target info) to determine what observable effects to check for, then apply the priority order above."""


def get_verifier_delay_section() -> str:
    """Verifier-specific delay awareness — extends shared delay section with verifier context."""
    return f"""### CRITICAL: Fault Injection Has Delay
{get_fault_effect_delay_section()}

**Therefore:**
- Do NOT conclude "fault not in effect" based on a SINGLE observation. If the first check shows no effect, wait and re-check.
- You have multiple iterations available. Use iteration 1-2 for initial checks, and if results are inconclusive, use iteration 3-4 for re-verification after the delay.
- If `kubectl top` shows low CPU on the first try, check again in a later iteration — the CPU spike may not have appeared yet.
- If `kubectl exec` returns empty (container lacks tools), switch to `kubectl describe` and `kubectl get -o json` for Pod-level signals.

{get_multi_iteration_section()}

### Avoid Redundant Checks
- Do NOT repeat kubectl queries that returned the same result in a previous iteration
- If `kubectl top pod` shows CPU/Memory values unchanged across 2 checks, conclude immediately
- Maximum 2 re-checks per metric type. If 3 checks show consistent results, conclude based on the evidence.

If you truly cannot determine how to verify this fault, output Layer2 as skipped."""


def get_verifier_output_format_section() -> str:
    """Machine-parseable output specification for verifier.

    MUST contain 'JSON' keyword for Bailian API response_format compatibility.
    Status values (passed/failed/skipped/recovered_before_observation) are
    program-parseable keywords and MUST NOT be renamed.
    """
    return """## Output (MANDATORY — Machine-Parseable JSON-compatible format)

Your final output MUST follow this format EXACTLY. Do NOT use markdown tables or emoji.
Any deviation will be REJECTED and you will be asked to re-output.

If you are still gathering evidence (not ready to conclude), call tools instead of outputting text.

VERIFICATION_CHECKLIST:
- Step 1: passed/failed/skipped/recovered_before_observation/expected — brief evidence
- Step 2: passed/failed/skipped/recovered_before_observation/expected — brief evidence
- ...
- Deviation example: "Step 1: passed — wget to target:8080 timed out (deviation: used wget instead of ping; --local-port=8080 only affects TCP, not ICMP)"

VERIFICATION_RESULT:
- Layer1 (blade_status): passed/failed/skipped
- Layer2 (fault-specific): passed/failed/skipped - evidence summary
- PrimaryEvidenceObserved: true/false
- Overall: verified/partial/unverified
- Warnings: any warnings, or "none"

**Primary Evidence Definition** (for PrimaryEvidenceObserved field):
Primary evidence is DIRECT observation of the fault's intended physical effect.
It is SPECIFIC to the fault type, NOT a generic stress symptom.

Examples by fault type:
| Fault | Primary evidence (set true) | NOT primary (set false) |
|-------|---------------------------|------------------------|
| pod-cpu fullload | CPU usage > threshold | Container restart, OOMKill |
| pod-mem load | Memory usage > threshold | OOMKill alone IS primary for memory faults |
| pod-disk burn | Burn files visible, I/O metrics elevated, df increase | OOMKill, container restart |
| pod-disk fill | Disk usage > fill percentage (e.g., >85%) | Pod eviction, DiskPressure (may be absent below threshold) |
| pod-network drop | Packets dropped, endpoints empty, connection refused | Pod restart, high latency on other ports |
| pod-kill | Pod in CrashLoopBackOff, restartCount increased | (Pod kill primary effect IS the restart) |

Set PrimaryEvidenceObserved: true ONLY if you directly observed at least one
PRIMARY effect listed above for the current fault type.
If you only observed side effects (restarts, OOMKills for non-memory faults,
generic timeouts), set PrimaryEvidenceObserved: false.
IMPORTANT: PrimaryEvidenceObserved MUST be consistent with Overall:
- If PrimaryEvidenceObserved=false, Overall CANNOT be "verified" — use "partial" at best.
- If you have PRIOR evidence the fault caused an effect that was subsequently
destroyed (e.g., OOMKill → burn files wiped), that is NOT primary evidence.

**Status Definitions** (used in both VERIFICATION_CHECKLIST and Layer2 fields):
- **passed**: The fault effect IS observable on the target (e.g., CPU is high, network has delay/loss, pod is restarting). The injection WORKED.
- **failed**: You executed this check AND the expected fault effect is NOT currently observed. Timing notes belong in Warnings, not the checklist. Mark what you observe RIGHT NOW.
- **skipped**: You did NOT execute this check at all (tool unavailable, data source missing, prerequisite missing). If you called a kubectl command for this step, it is NEVER 'skipped'.
- **recovered_before_observation**: The fault was transient and had already dissipated by the time you checked. If ALL steps are 'recovered_before_observation', Overall MUST be 'unverified'.
- **expected**: A NEGATIVE result that is ANTICIPATED given the injection parameters or context. Use this when a checklist step checks for a condition that SHOULD NOT occur due to the specific injection configuration. Examples: (1) DiskPressure=False when disk usage is below the kubelet threshold (e.g., `--percent 70` with 85% threshold) — the absence of DiskPressure is expected, not a failure. (2) Pod eviction not occurring because disk pressure threshold was not reached. Use 'expected' only when you have POSITIVE evidence from other steps confirming the fault IS in effect. Do NOT use 'expected' as a synonym for 'failed'. "expected" steps do NOT count against the Overall verdict — they are informational confirmations.

**IMPORTANT**: Do NOT conclude Layer2 as "failed" if your evidence shows the fault IS in effect. "failed" means the fault effect is ABSENT, not that the system is unhealthy.

**Overall Field Definitions** (your holistic judgment):
- **verified**: All critical fault effects are confirmed observable. Any checklist 'failed' items are benign (timing delays, non-essential checks) or 'expected' (anticipated negative results). Injection is SUCCESSFUL.
- **partial**: Some fault effects confirmed, others show mixed or unconfirmed results. Injection is PARTIALLY SUCCESSFUL.
- **unverified**: The fault effect could NOT be confirmed (includes all steps 'recovered_before_observation'). Injection may have FAILED.

CRITICAL DISTINCTION: Checklist items report OBSERVED FACTS. The Overall field is your HOLISTIC JUDGMENT. A checklist CAN have 'failed' items while Overall says 'verified' — use Warnings to explain the mismatch.

**Warnings Field**: Prefer informative warnings over "none". Include: measurement tool limitations, timing uncertainties, skipped steps and WHY.

The VERIFICATION_CHECKLIST section is **mandatory** and will be parsed programmatically.
If you do not include it, your verification will be flagged as potentially incomplete."""


def get_verifier_kubeconfig_section() -> str:
    """Kubeconfig requirement for verifier kubectl calls."""
    return """## Kubeconfig Requirement

If a kubeconfig path was provided in the fault context, you MUST include `kubeconfig="<path>"` as a parameter in EVERY kubectl tool call.
The default kubeconfig cannot access the target cluster. Omitting kubeconfig will cause tool calls to connect to the WRONG cluster."""


def get_verifier_critical_rules_reminder_section() -> str:
    """End-of-prompt reminder — repeats top-5 critical rules at the tail.

    Uses U-shaped attention principle: the recency effect ensures LLM
    attends to rules at the end of the prompt. Repeating critical rules
    here (concisely) doubles their exposure in high-attention zones.
    """
    return """## REMINDER — Critical Rules Recap

Before outputting your VERIFICATION_RESULT, verify you followed ALL of these:
1. Evidence = kubectl observations of fault EFFECT (not injection action)
2. BaselineUsed: true when baseline available; every step has baseline→current comparison
3. Transient fault: intermediate evidence (df increase, elevated I/O) → 'passed', NOT 'recovered_before_observation'
4. 'recovered_before_observation' ONLY when NO observation at ANY point showed fault effects
5. RestartCount: compare current vs baseline; same count = no NEW restart"""
