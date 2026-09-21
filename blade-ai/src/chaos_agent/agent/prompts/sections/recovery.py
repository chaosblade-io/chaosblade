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
  in the inject verifier: Core Principles at BEGINNING (primacy), low-priority
  information in the middle, and — since the 2026-09-20 pass-5 cleanup — the
  machine-parsed Output contract at the END. (The REMEMBER recency mirror was
  removed there: the ReAct loop's message tail owns recency, and its seven
  bullets were restatements of named carriers. Same ruling as the pass-4
  verifier cleanup.)
"""

from chaos_agent.agent.prompts.reminder import (
    PARALLELIZE_PRINCIPLE,
    SYSTEM_REMINDER_DECLARATION,
)
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
    return f"""# Core Principles
- Evidence MUST come from CURRENT post-recovery observations — stale baseline/injection data is NOT evidence
- Recovery = fault effect ABSENT. Prove by comparing CURRENT state to pre-injection BASELINE for the SAME metric on the SAME resource. When baseline is unavailable, confirm healthy state, then cross-validate with BaselineUsed: false
- When a tool returns error, the TOOL is right — verify its actual interface before retrying
- If an observation repeatedly fails or returns the same unexpected result, suspect your METHOD (wrong filter/command/assumption), not the target — switch to structured status (conditions/events/resource state) instead of retrying the same command. If still unobservable, mark the step skipped with the reason; never silently omit
- {PARALLELIZE_PRINCIPLE}
- {SYSTEM_REMINDER_DECLARATION}"""


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
    """Convergence and residual attribution — the recovery judgement contract.

    Single profile-agnostic text: the convergence/attribution contract is
    universal; k8s / host differences (which resources to observe) come from
    the environment_profile target-authority fragment and per-task
    instructions, never from this section.

    Replaces the old timing-only "Recovery Has Delay" protocol, which
    calibrated waits to propagation delay (10-60s) and exited with "a
    re-check after that delay still shows incomplete recovery -> partial".
    That exit rule misjudged the propagation COST of recovery as recovery
    failure whenever convergence outruns the wait budget (recover-d93a4ddf:
    cause revoked instantly, rollout convergence takes minutes; verdict was
    partial although attribution in the model's reasoning was correct).

    Mirrors the injection verifier's fourth principle: the claim decomposes
    into elements, and the burden is discharged once each element has
    evidence — what the judgement needs is attribution of the residual
    deviation, not a longer snapshot wait.

    Tool-agnostic by design (see test_judgement_tool_agnosticism): an
    earlier version named the waiting tool inline, which is the pattern this
    project rejects. What the prompt owes the model is the JUDGEMENT — that
    a transitional reading is not a verdict, that two readings with no
    elapsed time between them are one reading, and how to attribute the
    residual deviation before judging.
    """
    return """### Converging State and Residual Attribution

Recovery is NOT instantaneous. The undo action completes in seconds, but the
system returns to baseline at its own physical tempo (workload rollout,
process or service restart, resource release, metric propagation — readiness
and downstream metrics typically lag the actual state change by tens of
seconds to minutes). A transitional reading is not a verdict — if the first
check shows incomplete recovery, let time elapse before re-checking the SAME
evidence: two readings with no elapsed time between them are one reading,
and prove nothing.

Your product is an evidence chain for ONE claim: **the fault effect has been
removed by the recovery**. It decomposes into exactly three elements; when
every element has evidence, the burden is discharged and you submit:

1. **Cause undone** — the fault's cause is fully revoked (fresh config/state
   observation; the Layer-1 action report alone is NOT evidence).
2. **Residual attribution** — classify every residual deviation from
   baseline: *fault residual* (undo incomplete — evidence of recovery
   FAILURE) or *recovery propagation cost* (the system converging after the
   undo — evidence that recovery IS working).
3. **Coverage restored** — spaced readings show the trajectory improving
   toward baseline. Before concluding, also check: have ALL target resources
   returned? Any unexpected anomalies on non-targets? Is application-level
   recovery verified?

Judgement:
- Cause undone + every residual attributed to recovery propagation +
  trajectory improving → the recovery holds. Convergence still in progress
  is NOT failure: conclude "recovered" and record the converging tail in
  Warnings.
- Residuals attributable to the fault itself, trajectory stalled or
  regressing, or evidence genuinely mixed → partial / unrecovered.
- You may keep observing while budget allows, but the burden is discharged
  once all three elements have evidence — full convergence is NOT required,
  and a clean-attribution tail must never be recorded as partial."""


def get_recover_output_format_section(*, layer1_label: str = "deterministic destroy") -> str:
    """Machine-parseable output specification for recovery verification.

    Args:
        layer1_label: "deterministic destroy" for the carrier's programmatic
            experiment destroy, "recovery execution" for the LLM-driven path.
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
If PrimaryEvidenceObserved=false, Overall CANNOT be "recovered" — use "partial" at best,
or "unverified" when the observation channel itself was unavailable.

**Overall Definitions**:
- **recovered**: The specific fault effect is ABSENT. The system is at
  baseline, or every residual deviation is attributed to recovery
  propagation with the trajectory improving toward baseline.
- **partial**: Residual deviations attributable to the fault itself, or
  evidence genuinely mixed. A converging recovery tail is NOT partial.
- **unverified**: You could NOT observe the post-recovery state (observation
  tools failed on auth or were unavailable) — this is NOT evidence that the
  fault persists, so it is distinct from unrecovered.
- **unrecovered**: Fault effect is STILL present despite recovery attempt.

**Per-Step Status Definitions**:
- **passed**: Fault effect is absent for this metric (back to baseline or within healthy range).
- **failed**: Fault effect is still present for this metric.
- **skipped**: Could not execute this check (tool unavailable).
- **partial**: Inconclusive — some indicators show recovery, others do not.

Checklist = OBSERVED FACTS. Overall = HOLISTIC JUDGMENT. A checklist CAN
hold 'partial' items (e.g. convergence still in progress) while Overall says
'recovered' — explain the attribution in Warnings.

RECOVERY_VERIFICATION_CHECKLIST is mandatory — parsed programmatically."""


# ---------------------------------------------------------------------------
# Builder: compose all sections into a complete system prompt
# ---------------------------------------------------------------------------

def build_recover_verifier_system_prompt(
    *, layer1_label: str = "deterministic destroy", profile: str = PROFILE_K8S,
    ledger_section: str = "",
) -> str:
    """Build the recovery verifier system prompt using U-shaped composition.

    Follows the same architecture pattern as build_verifier_prompt():
    Core Principles at BEGINNING (primacy); the prompt CLOSES on the
    machine-parsed Output contract (the REMEMBER recency mirror was
    removed in the 2026-09-20 pass-5 cleanup — the ReAct loop's message
    tail owns recency, and every bullet was a restatement of a named
    carrier above).

    Args:
        layer1_label: Label for the Layer-1 line — "deterministic destroy"
            for a carrier's programmatic experiment destroy, "recovery
            execution" for an LLM-driven (kubectl-exec / non-experiment-
            carrier) recovery. Computed by the caller from the resolved
            backend's deterministic-Layer-1 flag.
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
        # Middle zone. No "Tool Constraint" section: the bound-tool list in
        # the tool schema plus the unknown-tool error feedback already carry
        # that fact.
        get_experience_section() or "",
        get_knowledge_summary_section(phase="recover"),
        get_recover_delay_section(),
        environment_fragment,
        get_recover_skill_priority_section(),
        get_recover_output_format_section(layer1_label=layer1_label),
        # The progress ledger NO LONGER rides this head (context-cache-prefix-
        # stability Unit A task 2.5): its per-round rewrite was an early volatile
        # byte that re-billed the whole cached suffix on every recover round. It
        # now rides the message tail (appended + persisted in
        # _recover_verifier_loop) so this [system] head stays byte-stable across
        # rounds. ``ledger_section`` is retained as an accepted-but-ignored kwarg
        # for in-flight callers.
        # REMEMBER (recency zone) removed in the 2026-09-20 pass-5 cleanup:
        # all seven bullets were compressed restatements of named carriers
        # (Core Principles head, Output contract, Residual-Attribution
        # judgement, PARALLELIZE second render). The recover verifier is a
        # ReAct loop — recency rides the message tail (tool results +
        # conditional reminders), so the prompt now closes on the
        # machine-parsed Output contract, same shape as the pass-4
        # verifier.
    ]
    prompt = "\n\n".join(p for p in parts if p)
    return prompt
