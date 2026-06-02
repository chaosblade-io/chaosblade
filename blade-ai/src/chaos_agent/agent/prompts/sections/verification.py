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

Layer 1 automatic verification (injection status check) has already passed. You do NOT need to check injection status again. You must now perform Layer 2 verification."""


def get_verifier_critical_rules_section() -> str:
    """Top-5 critical rules — placed at the beginning of the verifier prompt.

    Uses U-shaped attention principle: these rules MUST appear in the
    highest-attention zone (prompt beginning) to prevent Lost-in-the-Middle
    failures where LLM ignores key behavioral constraints buried in the
    middle of a long system prompt.
    """
    return """### CRITICAL RULES (mandatory — violations will trigger re-verification)

1. **Observe fault EFFECT via kubectl_verify, not injection ACTION** — Evidence MUST come from your own kubectl_verify calls in THIS verification phase. Do NOT cite results from prior execution phases (historical ToolMessages in the conversation). Only what you directly observe NOW via kubectl_verify counts as evidence.

2. **Baseline comparison is mandatory** — When pre-injection baseline data is available, every checklist step MUST include "baseline: X → current: Y (ΔZ)" comparison. Set BaselineUsed: true. Omitting baseline comparison will trigger re-verification.

3. **Transient fault intermediate evidence → passed** — Some faults produce cyclic or short-lived effects. If ANY observation during verification shows a clear change from baseline, mark that step 'passed', NOT 'recovered_before_observation'. Only use 'recovered_before_observation' when NO observation at ANY point showed fault effects.

4. **recovered_before_observation ≠ failed** — 'failed' means "I checked and the fault was absent". 'recovered_before_observation' means "the fault was transient and had already dissipated by the time I checked". If ALL steps are 'recovered_before_observation', Overall MUST be 'unverified'.

5. **RestartCount comparison vs baseline** — Compare current restartCount with the pre-injection baseline RestartCount. Only a NEW restart (restartCount > baseline) indicates a restart during the injection window. Same restartCount = no new restart.

6. **Runtime feedback overrides documentation** — If a tool rejects a parameter or returns an unexpected error, trust the tool — verify its actual interface before retrying."""


def get_verifier_tools_section() -> str:
    """Available and unavailable tools for the verifier phase."""
    return """### Available Tools (Layer 2 Verification)
You have ONLY these tools available:
- `kubectl_verify`: Run kubectl commands (get, describe, top, exec, logs) for cluster observation. Does NOT support mutation subcommands (scale, delete, patch, etc.)
- `read_skill_resource`: Read skill resource files (e.g., verification instructions, command reference)
- `execute_skill_script`: Execute skill-provided verification scripts
- `read_knowledge_resource`: Read domain knowledge documents (check the Domain Knowledge Index for available files)

### Tools NOT Available (Do NOT call these)
- `kubectl` — the full mutation kubectl is NOT available in verification. Use `kubectl_verify` instead.
- `blade_status`, `blade_create`, `blade_destroy`, `blade_query_k8s` — these TOOL FUNCTIONS are NOT available.
- You CAN still use `kubectl_verify(subcommand="exec", ...)` to exec into pods — that is kubectl exec, not a blade tool.
- `activate_skill` — Skill is already active.
- If you attempt to call an unavailable tool, it will be rejected. Use `kubectl_verify` instead."""


def get_verifier_layer2_section() -> str:
    """Core Layer 2 verification instructions.

    Covers: coverage/anomaly awareness, mandatory skill step execution,
    observe-fault-effect distinction, recovery awareness, supplementary
    checks, and fallback when no skill verification instructions exist.
    """
    return """## Layer 2: Fault-Specific Verification

### Coverage & Anomaly Awareness
Before concluding Layer2 'passed', verify:
1. **Coverage**: Were ALL target resources affected?
2. **Anomalies**: Any unexpected metric changes on non-targeted resources?
3. **Application Impact**: Has the application-level impact been verified?

### If Injection Verification Instructions are provided — MANDATORY EXECUTION

The skill case's injection verification section IS your verification plan. Execute EVERY step. Your VERIFICATION_CHECKLIST must include one line per step.

### CRITICAL: Observe Fault Effect, Do NOT Infer From Injection Action

Evidence must be kubectl observations of what happened to the target AFTER injection, not injection action results (Layer 1 already confirms those).

- Invalid evidence: "pod received Killing event", "blade_create succeeded"
- Valid evidence: "Endpoints list is empty", "kubectl top shows CPU at 95%"

If kubectl exec fails, check `kubectl describe pod` for container restart history before concluding a tool is unavailable — the container may have restarted during injection.

You MAY add supplementary checks AFTER completing required steps:
1. Pod-level (application impact) → 2. System-level (kubectl top/describe) → 3. Process confirmation.
Supplementary checks are additions, NOT replacements.

If a step cannot be executed, mark as "skipped" with reason. NEVER silently omit.

### Method Deviation Documentation

When you use a DIFFERENT method than specified in a skill case step, document: "Step N: passed — <what you did> (deviation: <why>)".

### If NO Injection Verification Instructions are provided:
Design your own verification plan: Pod-level checks (strongest) → System-level checks → Process confirmation. Analyze fault context to determine what effects to check for."""


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
    return """## Output (MANDATORY — submit via the submit_verification tool)

When ready to conclude, call `submit_verification`. This tool call IS your verdict — do NOT also write free-text VERIFICATION_RESULT. Debug pod cleanup is automatic.

If still gathering evidence, call kubectl/other tools instead — do NOT call submit_verification yet.

See the tool schema for argument details (overall, layer2_status, checklist, etc.). Fallback: if tool calling is unavailable, output a JSON-compatible VERIFICATION_RESULT block.

**Primary Evidence Definition** (for PrimaryEvidenceObserved field):
Primary evidence = **significant change from baseline** in the metric the fault targets. Does NOT require reaching the exact target value.
- Significant: resource metric delta ≥ 15pp, new fault artifacts, state changes (pod phase, node condition, restartCount, endpoints), network failures.
- NOT significant: reaching exact --percent target, side effects unrelated to injected fault type.
- PrimaryEvidenceObserved=false → Overall CANNOT be "verified" (use "partial" at best).

**Status Definitions**:
- **passed**: Fault effect IS observable. Injection worked.
- **failed**: You checked AND the expected effect is NOT observed. Mark what you see NOW.
- **skipped**: You did NOT execute this check (tool unavailable). If you called kubectl, it is NEVER 'skipped'.
- **recovered_before_observation**: Fault was transient and had dissipated by the time you checked. ALL steps recovered → Overall 'unverified'.
- **expected**: A negative result that is anticipated given injection parameters (e.g., threshold not reached). Use only when other steps confirm the fault IS in effect.

**Overall Definitions**:
- **verified**: Significant change from baseline observed. Fault IS present.
- **partial**: Evidence mixed or observation incomplete.
- **unverified**: No significant change from baseline, or all steps recovered.

Checklist = OBSERVED FACTS. Overall = HOLISTIC JUDGMENT. A checklist CAN have 'failed' items while Overall says 'verified' — explain in Warnings.

The VERIFICATION_CHECKLIST is mandatory and parsed programmatically."""


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

Before calling submit_verification, verify you followed ALL of these:
1. Evidence = kubectl observations of fault EFFECT (not injection action)
2. BaselineUsed: true when baseline available; every step has baseline→current comparison
3. Transient fault: intermediate evidence (any significant change from baseline) → 'passed', NOT 'recovered_before_observation'
4. 'recovered_before_observation' ONLY when NO observation at ANY point showed fault effects
5. RestartCount: compare current vs baseline; same count = no NEW restart"""
