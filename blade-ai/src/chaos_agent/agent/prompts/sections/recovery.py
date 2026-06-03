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
- You MUST conclude by CALLING the submit_recover_verification tool (see Output below)"""


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
- `kubectl_verify`: Run kubectl commands (get, describe, top, exec, logs) for cluster observation. Does NOT support mutation subcommands (scale, delete, patch, etc.)
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
    return f"""## Output (MANDATORY — submit via the submit_recover_verification tool)

When ready to conclude (after running kubectl to observe CURRENT post-recovery
state), call `submit_recover_verification`. This tool call IS your verdict —
do NOT also write free-text. Debug pod cleanup is automatic.

If still gathering evidence, call kubectl_verify instead — do NOT call
submit_recover_verification yet. See the tool schema for argument details.
Fallback: if tool calling unavailable, output a plain-text
RECOVERY_VERIFICATION_RESULT block with Layer1 ({layer1_label}), Layer2,
BaselineUsed, Overall, Warnings.

**Primary Evidence of Recovery**:
Primary evidence = the SPECIFIC fault effect is now ABSENT (metric returned
to baseline, artifacts removed, connections restored). NOT generic health
(pod Running, no restarts). Set PrimaryEvidenceObserved: true ONLY when you
directly observed the fault-specific effect being removed.
If PrimaryEvidenceObserved=false, Overall CANNOT be "recovered" — use "partial".

RECOVERY_VERIFICATION_CHECKLIST is mandatory — parsed programmatically."""


def get_recover_critical_rules_reminder_section() -> str:
    """End-of-prompt reminder — repeats critical rules at the tail.

    Uses U-shaped attention principle: recency effect ensures LLM
    attends to rules at the end of the prompt. Concisely repeats
    the same 3 rules from get_recover_critical_rules_section().
    """
    return """## REMINDER — Critical Rules Recap

Before calling submit_recover_verification, verify you followed ALL of these:
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