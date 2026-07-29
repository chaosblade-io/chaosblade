"""Recovery verification sections: recover_verifier prompt decomposed into
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
  in the inject verifier: Core Principles at BEGINNING (primacy) + REMEMBER at END (recency),
  with low-priority information in the middle.
"""

from chaos_agent.agent.prompts.sections.experience_section import get_experience_section
from chaos_agent.agent.prompts.sections.knowledge_sections import get_knowledge_summary_section
from chaos_agent.transports import PROFILE_K8S


# ---------------------------------------------------------------------------
# Baseline integrity — compact version for recovery verifier system prompt
# ---------------------------------------------------------------------------

_BASELINE_INTEGRITY_COMPACT = """**Baseline Comparison Rules** (applies to ALL quantitative metrics):
1. IDENTIFY exact resource (partition/device/node), not just "disk" or "CPU"
2. Compare SAME resource only: ✅ "imagefs /dev/vdb: baseline 10% → now 84%"
   ❌ "16% → 84%" (different partitions — INVALID)
3. No baseline → compare against expected healthy thresholds for <resource>; no clear threshold → cross-validate and set BaselineUsed: false
4. Value matching injection param = fault is present, not "no change"
"""


# ---------------------------------------------------------------------------
# Section functions (U-shaped composition order)
# ---------------------------------------------------------------------------

def get_recover_role_section() -> str:
    """Recovery verifier role definition.

    Placed at the BEGINNING of the prompt (primacy effect zone).
    """
    return """You are verifying whether a chaos engineering fault has been successfully recovered.

Your task: independently observe the current post-recovery target state and determine if the specific fault effect is ABSENT — not just that things look healthy."""


def get_recover_core_principles_section() -> str:
    """Core recovery verification principles — primacy zone anchor.

    Recover's root cause: a healthy-looking state cannot be attributed to
    recovery without comparison against a reference point. The state might
    be pre-existing. Baseline comparison is the primary method, with
    healthy-state comparison and cross-validation as degradation paths.

    Mirrors Phase 1/2 and injection verifier pattern: 3 principles.
    Uses three-level degradation: baseline > healthy state > cross-validation.
    """
    return """# Core Principles
- Evidence MUST come from CURRENT post-recovery observations — stale baseline/injection data is NOT evidence
- Recovery = fault effect ABSENT. Prove by comparing CURRENT state to pre-injection BASELINE for the SAME metric on the SAME resource. When baseline is unavailable, confirm healthy state, then cross-validate with BaselineUsed: false
- When a tool returns error, the TOOL is right — verify its actual interface before retrying
- If an observation repeatedly fails or returns the same unexpected result, suspect your METHOD (wrong filter/command/assumption), not the target — switch to structured status (conditions/events/resource state) instead of retrying the same command. If still unobservable, mark the step skipped with the reason; never silently omit"""


def get_recover_tools_section() -> str:
    """Tool constraint — general statement, no specific tool listing."""
    return """### Tool Constraint
Only call tools that are bound to you in this phase. Tools from previous phases are NOT available and will be rejected."""


def get_recover_skill_priority_section() -> str:
    """Skill use-case priority and checklist mapping — middle zone.

    Compact version: the detailed instructions_section is built dynamically
    in HumanMessage (per-task), so this section only sets the behavioral
    framework.
    """
    return f"""### Skill Use-Case Priority
If a skill use-case is provided in the instructions, treat its **恢复验证** section
as the PRIMARY evidence contract. Prefer its methods when available; when an
equivalent observation is needed, record the deviation and why it proves the
same recovery requirement. Never silently omit an evidence requirement.

### Checklist Step Mapping
Map each skill case recovery evidence requirement to a checklist item. If N
requirements are present, RECOVERY_VERIFICATION_CHECKLIST MUST have at least N items.
Do NOT declare Layer2 as "passed" unless ALL steps are "passed" or "expected".
If some steps are "skipped" or "partial", Layer2 MUST be "partial", not "passed".

If NO recovery verification instructions exist: design your own steps, list them in
RECOVERY_VERIFICATION_CHECKLIST BEFORE executing, then verify via the tools bound in this phase.
If you truly cannot determine how to verify, output Layer2 as skipped.

{_BASELINE_INTEGRITY_COMPACT}"""




def get_recover_delay_section() -> str:
    """Recovery effect delay awareness — the re-check protocol before concluding.

    Single profile-agnostic text: the delay/re-check protocol is universal;
    k8s / host differences (which resources to observe) come from the
    environment_profile target-authority fragment and per-task instructions,
    never from this section.

    Tool-agnostic by design. An earlier version named the waiting tool inline
    ("call ``time_wait(seconds=20)``"), which is the pattern this project
    rejects: the tool's own description already states when to use it, and
    hard-coding a tool name into a principle breaks whenever the tool surface
    changes and teaches the model to follow the prompt instead of its bound
    tools. What the prompt owes the model is the JUDGEMENT — that a
    transitional reading is not a verdict, and that two readings with no elapsed
    time between them are one reading.
    """
    return """### Recovery Has Delay

Recovery is NOT instantaneous. After the recovery action reports success:
- The actual recovery effect may take **10-60 seconds** to become fully observable.
- The recovery needs time to propagate (resource release, state/config rollback,
  process or service restart).
- Readiness/health signals and downstream metrics lag behind the actual state
  change — sampling intervals are typically 15-30s.

**Therefore:**
- Do NOT conclude "partial" or "failed" based on a SINGLE observation showing a transitional state.
- If the first check shows incomplete recovery, let time elapse (on the order of the propagation delay above) before re-checking the SAME evidence — two readings with no elapsed time between them are one reading, and prove nothing.
- Only conclude "partial" when a re-check AFTER that delay still shows incomplete recovery.
- If the FIRST check already shows full recovery, one confirmation check suffices."""


def get_recover_output_format_section(*, layer1_label: str = "blade_destroy") -> str:
    """Machine-parseable output specification for recovery verification.

    Args:
        layer1_label: "blade_destroy" for ChaosBlade, "recovery execution" for non-CB.
    """
    return f"""## Output (MANDATORY — submit via the submit_recover_verification tool)

When ready to conclude (after running currently bound observation tools to observe
CURRENT post-recovery state), call `submit_recover_verification`. This tool call IS your verdict —
do NOT also write free-text. Debug pod cleanup is automatic.

If still gathering evidence, call an observation tool bound in this phase instead — do NOT call
submit_recover_verification yet. See the tool schema for argument details.
Fallback: if tool calling unavailable, output a plain-text
RECOVERY_VERIFICATION_RESULT block with Layer1 ({layer1_label}), Layer2,
BaselineUsed, Overall, Warnings.

**Primary Evidence of Recovery**:
Primary evidence = the SPECIFIC fault effect is now ABSENT (metric returned
to baseline or within healthy range, artifacts removed, connections restored). NOT generic health
(pod Running, no restarts). Set PrimaryEvidenceObserved: true ONLY when you
directly observed the fault-specific effect being removed.
If PrimaryEvidenceObserved=false, Overall CANNOT be "recovered" — use "partial".

**Overall Definitions**:
- **recovered**: Specific fault effect is ABSENT. System is back to baseline or healthy state.
- **partial**: Evidence mixed or observation incomplete.
- **unrecovered**: Fault effect is STILL present despite recovery attempt.

**Per-Step Status Definitions**:
- **passed**: Fault effect is absent for this metric (back to baseline or within healthy range).
- **failed**: Fault effect is still present for this metric.
- **skipped**: Could not execute this check (tool unavailable).
- **partial**: Inconclusive — some indicators show recovery, others do not.

RECOVERY_VERIFICATION_CHECKLIST is mandatory — parsed programmatically."""


def get_recover_remember_section() -> str:
    """REMEMBER segment — recency zone anchor for recover verifier."""
    return """# REMEMBER
- Evidence from CURRENT post-recovery state only — stale data is NOT evidence
- Baseline comparison proves recovery — SAME metric on SAME resource; degrade to healthy-state confirmation, then cross-validation when baseline unavailable
- When a tool returns error, the TOOL is right
- Repeated failure → suspect your METHOD; switch approach, never silently omit
- Primary evidence = fault effect ABSENT, not generic health (pod Running ≠ recovered)"""


# ---------------------------------------------------------------------------
# Builder: compose all sections into a complete system prompt
# ---------------------------------------------------------------------------

def build_recover_verifier_system_prompt(
    *, layer1_label: str = "blade_destroy", profile: str = PROFILE_K8S
) -> str:
    """Build the recovery verifier system prompt using U-shaped composition.

    Follows the same architecture pattern as build_verifier_prompt():
    Core Principles at BEGINNING (primacy) + REMEMBER at END (recency), with
    low-priority information in the middle.

    Args:
        layer1_label: Label for the Layer-1 line — "blade_destroy" for a
            deterministic ChaosBlade destroy, "recovery execution" for an
            LLM-driven (kubectl-exec / non-ChaosBlade) recovery. Computed by the
            caller from the resolved backend's deterministic-Layer-1 flag.
        profile: Channel profile ("k8s"|"host"), accepted for dispatch symmetry.
    """
    from chaos_agent.agent.environment_profiles import get_environment_profile

    environment = get_environment_profile(profile)
    environment_fragment = (
        environment.prompt_fragment("recover_verify")
        if environment is not None
        else (
            "## Capability Profile\n"
            "The current environment profile is unsupported. Do not attempt "
            "recovery verification; report the missing environment capability."
        )
    )

    parts = [
        # U-shaped attention: Core Principles at BEGINNING (primacy)
        get_recover_role_section(),
        get_recover_core_principles_section(),
        # Middle zone
        get_experience_section() or "",
        get_knowledge_summary_section(),
        get_recover_tools_section(),
        get_recover_delay_section(),
        environment_fragment,
        get_recover_skill_priority_section(),
        get_recover_output_format_section(layer1_label=layer1_label),
        # U-shaped attention: REMEMBER at END (recency)
        get_recover_remember_section(),
    ]
    prompt = "\n\n".join(p for p in parts if p)
    return prompt
