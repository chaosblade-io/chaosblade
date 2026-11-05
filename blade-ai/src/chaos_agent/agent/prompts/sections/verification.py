"""Verification sections: Layer 2 verifier prompt decomposed into reusable section functions.

These sections compose the verifier system prompt while sharing sub-sections
(e.g., fault delay, iteration pattern) with the inject/execute prompts,
eliminating copy-paste duplication per the P2 design principle.
"""

from chaos_agent.agent.prompts.reminder import (
    CONCURRENT_BATCH_FACT,
    LEDGER_BATCH_FACT,
    PARALLELIZE_PRINCIPLE,
    SYSTEM_REMINDER_DECLARATION,
)
from chaos_agent.agent.result.verdict import ChecklistItemStatus


def get_verifier_role_section() -> str:
    """Verifier role definition — tool-agnostic, no Layer 1 assumption."""
    return """You are verifying whether a chaos engineering fault injection produced the expected effect.

Your task: independently observe the current target state and determine if the fault effect is present AND attributable to the injection (not pre-existing)."""


def get_verifier_core_principles_section() -> str:
    """Core verification principles — primacy zone anchor.

    Verify's root cause: an observed effect cannot be attributed to the
    injection without baseline comparison. The effect might be pre-existing
    or caused by something else. Baseline comparison is the primary method
    to establish causation, with healthy-state comparison and cross-validation
    as degradation paths.

    The fourth principle is the CONVERGENCE rule, and it exists because this
    phase's position in the flow defines a finite job: verification sits between
    injection and the archived record, and its product is an evidence chain for
    ONE claim — did the fault take effect on the approved target. That claim
    decomposes into exactly three elements (effect present, attributable to the
    injection, coverage of the target set). Once each element has evidence, the
    burden is discharged; re-sampling an element that already has evidence adds
    no evidentiary weight. Without this rule the phase has no terminal state,
    because "completeness of observation" has none: task-c7c75263 issued the
    same metric query 26 times, and the model's own words for why were "I need
    to change the observation angle to verify more comprehensively" — repeated
    three times, after all three elements were already proven at observation 3.

    Tool-agnostic: no mention of Layer 1 (kubectl native has no Layer 1),
    no concrete tool names. Mirrors Phase 1/2 pattern.
    """
    return f"""# Core Principles
- Evidence MUST come from your own observations in THIS phase — prior phase results (injection action success, planning queries) are NOT evidence
- Baseline comparison is the primary method to prove causation — compare the SAME metric on the SAME resource. When baseline is unavailable, degrade to healthy-state comparison, then cross-validation with BaselineUsed: false
- When a tool returns error, the TOOL is right — verify its actual interface before retrying
- Your product is an evidence chain for ONE claim: did the fault take effect on the approved target — effect present, attributable to the injection, coverage of the target set. When every element has evidence, the burden is discharged and you submit; re-sampling an element that already has evidence adds no proof. Only a MISSING element earns another observation — "another angle exists" is always true and is never a reason to continue, and being unable to observe is itself a conclusion — the conclusion is 'unobserved', never 'absent'
- {PARALLELIZE_PRINCIPLE}
- {LEDGER_BATCH_FACT}
- {CONCURRENT_BATCH_FACT}
- {SYSTEM_REMINDER_DECLARATION}"""


def get_verifier_layer2_section() -> str:
    """Core Layer 2 verification instructions.

    Covers: mandatory skill step execution, observe-fault-effect distinction,
    recovery awareness, supplementary checks, and fallback when no skill
    verification instructions exist.

    Coverage Awareness sub-section removed in the 2026-09-20 verifier
    cleanup (pass-4): its single rule ("Were ALL target resources
    affected?") is carried by Core Principles #4 ("coverage of the target
    set") and the Output contract's Overall 'verified' definition ("across
    the ENTIRE approved target set") — two stronger named carriers made
    this mirror weight without a carrier.
    """
    return """## Fault-Specific Verification

### If Injection Verification Instructions are provided

The skill case defines the required evidence coverage. Use its preferred
methods when they are available. An equivalent observation method is allowed
when it proves the same requirement; record the deviation and why it is
equivalent. Your VERIFICATION_CHECKLIST must cover every required evidence
item, not merely repeat command text.

### Observe Fault Effect, Do NOT Infer From Injection Action

Evidence must be your own observations of what happened to the target AFTER injection, not injection action results.

- Invalid evidence: "pod received Killing event", "the injection action reported success"
- Valid evidence: "Endpoints list is empty", "metrics show CPU at 95%"

An observation failure's evidential weight depends on its cause:
- **Auth-class failure** (401/403/Forbidden/unauthorized): the primary evidence
  channel is unavailable for this run. Evidence gathered through alternative
  channels (logs, events, indirect signals) directly observing the fault
  mechanism is at most "partial" — it supports "verified" only when it itself
  constitutes primary evidence (significant change from baseline).
- **Transient failure** (timeout, connection reset): the failure carries no
  evidential weight once a retry or an equivalent channel succeeds — evidence
  obtained afterwards counts at full strength.

You MAY add supplementary checks after covering required evidence, starting from the mechanism's most direct evidence.
Supplementary checks are additions, NOT replacements.

If a step cannot be executed, mark as "skipped" with reason. NEVER silently omit.

You may conclude any step early if continued attempts are unlikely to yield new information.
When concluding early, you MUST provide:
1. What you tried (commands/methods)
2. What you observed (actual output)
3. Why further attempts would not change the outcome

### Method Deviation Documentation

When you use a DIFFERENT method than specified in a skill case step, document: "Step N: passed — <what you did> (deviation: <why>)".

### If NO Injection Verification Instructions are provided:
Design your own verification plan: identify the observables the fault mechanism is expected to change, then observe them starting from the most direct evidence of the mechanism. Analyze fault context to determine what effects to check for."""


# Checklist status DEFINITIONS, keyed by every ChecklistItemStatus member.
#
# B76 round-14 single-sourced the vocabulary VALUE LISTS (tool schema,
# _ITEM_STATUS_PROSE, JSON reminder all derive from the enum). It never
# single-sourced the DEFINITIONS — those stayed hand-copied prose in
# get_verifier_output_format_section and silently drifted to 5-of-7:
# `partial` and `not_applicable` were exposed to the LLM as enum values but
# taught NO meaning. The gap is load-bearing: with no authoritative owner for
# "a conditional/diagnostic step whose trigger precondition did not fire, so
# it was correctly NOT run", the model invents a definition and flip-flops
# between skipped / not_applicable / expected for structurally identical steps
# (2026-10-29 python Case #5 inject-e0b3109a: step3 `blade status` → skipped,
# step5 matcher-recheck → not_applicable, same "precondition false" shape,
# ~12 reasoning lines burned on a label that never changes the verdict; Case #2
# had labelled BOTH steps not_applicable — cross-case inconsistent).
#
# Rendering iterates the enum and indexes this map, so adding a member without
# a definition raises KeyError at import — B76's "teaching cannot silently miss
# a legislated word" guarantee, extended from the WORD to its MEANING.
# Context-INVARIANT checklist status semantics, shared by BOTH the inject and
# the recover verifier faces. `not_applicable` means the same thing regardless
# of which side is judging — a step that does not apply to the actual
# situation — so it is defined ONCE here and imported by recovery.py; a second
# hand-copy would drift (the very failure this fix removes).
STATUS_NOT_APPLICABLE_DEFINITION = (
    "The step does not apply to the actual situation and was correctly NOT "
    "executed — either its target does not exist in this environment, OR "
    "it is a conditional/diagnostic step whose trigger precondition did "
    "not fire (e.g. \"run only if the effect is absent\", but the effect "
    "WAS observed). Correctly not running it is NOT a coverage gap. MUST "
    "carry a reason."
)

_CHECKLIST_STATUS_DEFINITIONS: dict[ChecklistItemStatus, str] = {
    ChecklistItemStatus.PASSED: (
        "Fault effect IS observable and attributable to the injection."
    ),
    ChecklistItemStatus.PARTIAL: (
        "You ran the check but THIS step's evidence is incomplete or mixed — "
        "the effect is only partly confirmed. Counts as a non-passed step."
    ),
    ChecklistItemStatus.FAILED: (
        "You checked AND the expected effect is NOT observed. Mark what you "
        "see NOW."
    ),
    ChecklistItemStatus.SKIPPED: (
        "The step DOES apply and should have been run, but you could NOT "
        "execute it (tool unavailable / no read-only channel) — a genuine "
        "coverage gap. If you actually ran an observation command it is NEVER "
        "'skipped'. A conditional step that correctly did not trigger is "
        "'not_applicable', NOT 'skipped'."
    ),
    ChecklistItemStatus.RECOVERED_BEFORE_OBSERVATION: (
        "Fault was transient and had dissipated by the time you checked. ALL "
        "steps recovered → Overall 'unverified'."
    ),
    ChecklistItemStatus.EXPECTED: (
        "A negative result you OBSERVED that is anticipated given injection "
        "parameters (e.g., threshold not reached). Use only when other steps "
        "confirm the fault IS in effect."
    ),
    ChecklistItemStatus.NOT_APPLICABLE: STATUS_NOT_APPLICABLE_DEFINITION,
}

_CHECKLIST_STATUS_DEFINITIONS_BLOCK = "\n".join(
    f"- **{member.value}**: {_CHECKLIST_STATUS_DEFINITIONS[member]}"
    for member in ChecklistItemStatus
)


def get_verifier_output_format_section() -> str:
    """Machine-parseable output specification for verifier.

    MUST contain 'JSON' keyword for Bailian API response_format compatibility.
    Status values (passed/failed/skipped/recovered_before_observation) are
    program-parseable keywords and MUST NOT be renamed.
    """
    return """## Output (MANDATORY — submit via the submit_verification tool)

When ready to conclude, call `submit_verification`. This tool call IS your verdict — do NOT also write free-text VERIFICATION_RESULT. Debug pod cleanup is automatic.

If still gathering evidence, call your observation tools instead — do NOT call submit_verification yet.

### Text Response Role
Your text responses are brief progress updates (1-3 sentences per observation). The complete baseline comparison belongs in submit_verification's checklist — NOT in your text output. Do NOT produce tables, templates, or formatted reports in text — structure your evidence ONLY inside the submit_verification call.

See the tool schema for argument details (overall, layer2_status, checklist, etc.). Fallback: if tool calling is unavailable, output a JSON-compatible VERIFICATION_RESULT block.

**Numeric claims (metric_claims field)**: when your verdict rests on a numeric baseline→post change, record each in `metric_claims` as `{metric, baseline, post}`, keyed to the EXACT observed metric name (`updatedReplicas`, not `replicas`; `Disk usage (nodefs)`, not `disk usage`). The timeline cross-check reads this structured binding and compares your declared numbers against the observed metric by exact key — it does NOT parse your prose. A claim bound to the wrong key (or left only in free text) cannot be cross-checked, so the program treats it as unverified-by-measurement rather than confirmed.

**Free-form exec numbers (kind/source fields)**: a number read from output the structured extractor cannot parse into the timeline (`tc -s qdisc`, `ping`, `ss`, `/proc/net/*`, `iptables -L`) is unreachable by exact key — so add two OPTIONAL fields. `kind`/`source` describe the provenance of the **`post` value only** — the number you read from the cited observation. Set `"kind":"raw"` when that `post` value is copied VERBATIM from one observation, plus `"source":"<a distinctive fragment of that command>"` (e.g. `"tc -s qdisc"`). The cross-check separately harvests the numbers in each observation's raw output: a declared `post` value appearing there AS A STANDALONE NUMBER is CORROBORATED (an extractor coverage gap, NOT a hallucination — no downgrade); digits occurring only inside a longer token (an experiment UID, a timestamp) corroborate nothing and just warn; a `post` value you marked `raw`+`source` that appears in NO observation output is FABRICATED and downgrades the verdict. The `baseline` field is NOT part of this anchor: a baseline is inherently read in an EARLIER phase (the pre-injection snapshot), which the fabrication check never inspects — so a baseline that no longer appears in any live observation is never FABRICATED and needs no `source`. Mark any computed / aggregated / ratio value (packet-loss %, a delta, a rate) `"kind":"derived"` — those legitimately never appear verbatim and are exempt. Omitting `kind` is treated as derived (never flagged), so declare `raw` ONLY for a `post` number you literally copied.

**Primary Evidence Definition** (for PrimaryEvidenceObserved field):
Primary evidence = **significant change from baseline OR significant deviation from expected healthy state** in the metric the fault targets. Does NOT require reaching the exact target value.
- Significant: resource metric delta ≥ 15pp, new fault artifacts, or a state change of the target consistent with the fault mechanism.
- NOT significant: reaching the exact injected numeric target, side effects unrelated to injected fault type.
- PrimaryEvidenceObserved=false → Overall CANNOT be "verified" (use "partial" at best).

**Status Definitions**:
""" + _CHECKLIST_STATUS_DEFINITIONS_BLOCK + """

**Overall Definitions**:
- **verified**: The fault effect (significant change from baseline / deviation from healthy state) is confirmed across the ENTIRE approved target set — the whole target, not a subset.
- **partial**: The effect is REAL but INCOMPLETE — confirmed on only some members of the approved target set (state coverage as N/total), or evidence mixed / observation incomplete.
- **unverified**: No significant change from baseline and no deviation from healthy state anywhere, or all steps recovered.

Checklist = OBSERVED FACTS. Overall = HOLISTIC JUDGMENT. The program enforces status consistency STRUCTURALLY, by reading your status enum only — it never re-derives absence/benign semantics from your evidence text. layer2_status='passed' while any item is 'failed' is a self-contradiction and is DOWNGRADED to 'partial'. So when a step's negative result is NOT a real failure (effect absent / benign / expected-negative / dissipated before you could observe it), declare it 'expected' | 'partial' | 'recovered_before_observation' — reserve 'failed' for a genuine unmet effect.

The VERIFICATION_CHECKLIST is mandatory and parsed programmatically."""
