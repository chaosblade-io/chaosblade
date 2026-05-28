"""Recovery verification sections: Layer 2 recover_verifier prompt decomposed into
reusable section functions, following the same U-shaped architecture pattern as
the inject verifier (see verification.py).

Design rationale (from first-principles audit of task-d0f0f506 recovery):
- The previous inline prompt had core behavioral rules in the MIDDLE of the
  system prompt — a Lost-in-the-Middle high-risk zone where LLMs have the
  lowest compliance rate (Liu et al., 2023, U-shaped attention curve).
- Three observed failures all traced to middle-position rules being ignored:
  1) using stale injection-phase data instead of fresh kubectl commands
  2) treating ls /tmp as primary evidence for pod-disk-burn (cyclic write-delete)
  3) skipping /proc/diskstats check entirely
- This module restructures the prompt using the U-shaped pattern already proven
  in the inject verifier: CRITICAL rules at BEGINNING (primacy) + END (recency),
  with low-priority information in the middle.
"""

from chaos_agent.agent.prompts.sections.knowledge_sections import get_knowledge_summary_section


# ---------------------------------------------------------------------------
# Baseline integrity — compact version for recovery verifier system prompt
# ---------------------------------------------------------------------------

_BASELINE_INTEGRITY_COMPACT = """**Baseline Comparison Rules** (applies to ALL quantitative metrics):
1. IDENTIFY exact resource (partition/device/node), not just "disk" or "CPU"
2. Compare SAME resource only: ✅ "imagefs /dev/vdb: baseline 10% → now 84%"
   ❌ "16% → 84%" (different partitions — INVALID)
3. No baseline → say "No baseline for <resource>. Current: <value>"
4. Value matching injection param = fault is present, not "no change"
"""


# ---------------------------------------------------------------------------
# Section functions (U-shaped composition order)
# ---------------------------------------------------------------------------

def get_recover_role_section() -> str:
    """Recovery verifier role definition.

    Placed at the BEGINNING of the prompt (primacy effect zone).
    """
    return """You are verifying that a chaos engineering fault has been successfully recovered.

**IMPORTANT: You are in Layer 2 (VERIFICATION phase).**
- DO NOT execute recovery actions — that was Layer 1's job
- DO NOT output RECOVERY_EXECUTION_RESULT — that is Layer 1's format
- You MUST output RECOVERY_VERIFICATION_RESULT format below"""


def get_recover_critical_rules_section() -> str:
    """Top-3 critical rules for recovery verification — placed at BEGINNING.

    Uses U-shaped attention principle: these rules MUST appear in the
    highest-attention zone (prompt beginning). Three rules derived from
    observed failures in task-d0f0f506:
    1) LLM used stale injection-phase data → must execute fresh kubectl
    2) LLM treated non-primary evidence as primary → must observe fault effect directly
    3) LLM skipped specific metrics → must observe CURRENT state
    """
    return f"""### CRITICAL RULES (mandatory — violations will trigger re-verification)

1. **Execute kubectl to observe CURRENT (post-recovery) state** — You MUST call at least
   one kubectl command in this Layer 2 iteration to observe the target's CURRENT runtime
   state. Using baseline data (pre-injection) or injection-phase observations as
   "post-recovery" evidence is INVALID — those were captured BEFORE or DURING the fault,
   not AFTER recovery.

2. **Primary evidence = fault effect being REMOVED, not generic health** —
   "df -h shows disk back to baseline", "CPU returned to normal", "I/O metrics recovered" =
   primary evidence. "Pod Running", "no new restarts" = generic health indicators, NOT
   primary evidence (unless pod-kill where restart IS the primary effect).
   Set PrimaryEvidenceObserved: true ONLY if you directly observed the specific fault
   effect being absent. If only generic indicators observed, set PrimaryEvidenceObserved: false.

{_BASELINE_INTEGRITY_COMPACT}"""


def get_recover_tools_section() -> str:
    """Available tools for recovery verification."""
    return """### Available Tools
- `kubectl`: Run kubectl commands for cluster verification
- `read_skill_resource`: Read skill resource files (e.g., recovery verification instructions)
- `read_knowledge_resource`: Read domain knowledge documents (check Domain Knowledge Index)"""


def get_recover_skill_priority_section() -> str:
    """Skill use-case priority and checklist mapping — middle zone.

    Compact version: the detailed instructions_section is built dynamically
    in HumanMessage (per-task), so this section only sets the behavioral
    framework.
    """
    return """### Skill Use-Case Priority (CRITICAL)
If a skill use-case is provided in the instructions, treat it as the PRIMARY AUTHORITY
for recovery verification. Follow its **恢复验证** section exactly. If a step cannot
be executed, note it explicitly — do NOT silently skip skill case verification steps.

### Checklist Step Mapping (CRITICAL)
Map each skill case verification step to a checklist item. If N steps in skill case,
RECOVERY_VERIFICATION_CHECKLIST MUST have at least N items.
Do NOT declare Layer2 as "passed" unless ALL steps are "passed" or "expected".
If some steps are "skipped" or "partial", Layer2 MUST be "partial", not "passed".

If NO recovery verification instructions exist: design your own steps, list them in
RECOVERY_VERIFICATION_CHECKLIST BEFORE executing, then verify via kubectl tools.
If you truly cannot determine how to verify, output Layer2 as skipped."""


def get_recover_kubeconfig_section() -> str:
    """Kubeconfig requirement for recovery verifier."""
    return """## Kubeconfig Requirement

If a kubeconfig path was provided, you MUST include `kubeconfig="<path>"` as a
parameter in EVERY kubectl tool call. Omitting kubeconfig will connect to the
WRONG cluster."""


def get_recover_output_format_section(*, layer1_label: str = "blade_destroy") -> str:
    """Machine-parseable output specification for recovery verification.

    Args:
        layer1_label: "blade_destroy" for ChaosBlade, "recovery execution" for non-CB.
    """
    return f"""## Output (MANDATORY — Machine-Parseable format)

Your final output MUST follow this format EXACTLY. No markdown tables or emoji.
If still gathering evidence, call tools instead of outputting text.

RECOVERY_VERIFICATION_CHECKLIST:
- Step 1: passed/failed/skipped/partial/expected — brief evidence (baseline→current Δ%)
- Step 2: ...

RECOVERY_VERIFICATION_RESULT:
- Layer1 ({layer1_label}): passed
- Layer2 (fault-specific): passed/failed/partial/skipped — evidence summary
- PrimaryEvidenceObserved: true/false
- BaselineUsed: true/false
- Overall: recovered/partial/unrecovered
- Warnings: text or "none"

Status: passed | failed | skipped | partial | expected(negative=anticipated)
Overall: recovered(all pass) | partial(some skip) | unrecovered(still present)

**Primary Evidence Definition** (for PrimaryEvidenceObserved field):
Primary evidence of RECOVERY = DIRECT observation that the fault's specific
physical effect has been REMOVED. It is fault-type-specific, NOT generic health.

Examples by fault type — observe the LEFT column to set PrimaryEvidenceObserved=true:
| Fault              | Primary evidence of REMOVAL (set true)                           | NOT primary (set false)                          |
|--------------------|------------------------------------------------------------------|--------------------------------------------------|
| pod-cpu fullload   | CPU usage returned to baseline (kubectl top)                     | Pod Running alone, no new restart                |
| pod-mem load       | Memory usage returned to baseline (kubectl top)                  | Pod Running alone; absence of new OOMKill alone  |
| pod-disk burn      | Burn files removed; I/O back to baseline; df back to baseline    | Pod Running, no new restart                      |
| pod-disk fill      | Disk usage below original threshold (df -h on imagefs/rootfs)    | DiskPressure cleared alone                       |
| pod-network drop   | Packets flowing; endpoints populated; connection succeeds        | Pod Running, no new restart                      |
| pod-kill           | Pod Ready 1/1 AND restartCount stable since recovery action      | Pod still in CrashLoopBackOff                    |

Set PrimaryEvidenceObserved: true ONLY if you directly observed at least one
PRIMARY recovery indicator listed above for the current fault type.
If only generic indicators (pod Running, no events) observed, set false.
IMPORTANT: PrimaryEvidenceObserved MUST be consistent with Overall:
- If PrimaryEvidenceObserved=false, Overall CANNOT be "recovered" — use "partial" at best.

RECOVERY_VERIFICATION_CHECKLIST is mandatory — parsed programmatically."""


def get_recover_critical_rules_reminder_section() -> str:
    """End-of-prompt reminder — repeats critical rules at the tail.

    Uses U-shaped attention principle: recency effect ensures LLM
    attends to rules at the end of the prompt. Concisely repeats
    the same 3 rules from get_recover_critical_rules_section().
    """
    return """## REMINDER — Critical Rules Recap

Before outputting RECOVERY_VERIFICATION_RESULT, verify you followed ALL of these:
1. You executed kubectl commands to observe CURRENT (post-recovery) state — NOT stale baseline/injection data
2. PrimaryEvidenceObserved reflects DIRECT observation of fault effect being absent — NOT generic health
3. Baseline comparison identifies the EXACT resource (partition/device/node), not just 'disk'"""


# ---------------------------------------------------------------------------
# Builder: compose all sections into a complete system prompt
# ---------------------------------------------------------------------------

def build_recover_verifier_system_prompt(*, is_chaosblade: bool = True) -> str:
    """Build the recovery verifier system prompt using U-shaped composition.

    Follows the same architecture pattern as build_verifier_prompt():
    CRITICAL rules at BEGINNING (primacy) + END (recency), with
    low-priority information in the middle.

    Args:
        is_chaosblade: If True, Layer 1 label is "blade_destroy";
                       if False, Layer 1 label is "recovery execution".
    """
    layer1_label = "blade_destroy" if is_chaosblade else "recovery execution"

    parts = [
        # U-shaped attention: CRITICAL rules at BEGINNING (primacy effect)
        get_recover_role_section(),
        get_recover_critical_rules_section(),
        # Middle zone: low-priority / on-demand information
        get_knowledge_summary_section(),
        get_recover_tools_section(),
        get_recover_skill_priority_section(),
        get_recover_kubeconfig_section(),
        # U-shaped attention: output format + CRITICAL rules at END (recency effect)
        get_recover_output_format_section(layer1_label=layer1_label),
        get_recover_critical_rules_reminder_section(),
    ]
    prompt = "\n\n".join(p for p in parts if p)
    return prompt