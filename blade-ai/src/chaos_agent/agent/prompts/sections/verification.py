"""Verification sections: Layer 2 verifier prompt decomposed into reusable section functions.

These sections compose the verifier system prompt while sharing sub-sections
(e.g., fault delay, iteration pattern) with the inject/execute prompts,
eliminating copy-paste duplication per the P2 design principle.
"""


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
    return """# Core Principles
- Evidence MUST come from your own observations in THIS phase — prior phase results (injection action success, planning queries) are NOT evidence
- Baseline comparison is the primary method to prove causation — compare the SAME metric on the SAME resource. When baseline is unavailable, degrade to healthy-state comparison, then cross-validation with BaselineUsed: false
- When a tool returns error, the TOOL is right — verify its actual interface before retrying
- Your product is an evidence chain for ONE claim: did the fault take effect on the approved target — effect present, attributable to the injection, coverage of the target set. When every element has evidence, the burden is discharged and you submit; re-sampling an element that already has evidence adds no proof. Only a MISSING element earns another observation — "another angle exists" is always true and is never a reason to continue, and being unable to observe is itself a conclusion"""


def get_verifier_tools_section() -> str:
    """Tool constraint — general statement, no specific tool listing."""
    return """### Tool Constraint
Only call tools that are bound to you in this phase. Tools from previous phases are NOT available and will be rejected."""


def get_verifier_layer2_section() -> str:
    """Core Layer 2 verification instructions.

    Covers: coverage/anomaly awareness, mandatory skill step execution,
    observe-fault-effect distinction, recovery awareness, supplementary
    checks, and fallback when no skill verification instructions exist.
    """
    return """## Fault-Specific Verification

### Coverage & Anomaly Awareness
Before concluding verification 'passed', verify:
1. **Coverage**: Were ALL target resources affected?
2. **Anomalies**: Any unexpected metric changes on non-targeted resources?
3. **Application Impact**: Has the application-level impact been verified?

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

If an observation command fails, use the current environment's resource-level
or alternative observation capability before concluding the evidence is unavailable.

You MAY add supplementary checks after covering required evidence:
1. Application-level impact → 2. System-level metrics or conditions → 3. Process or resource confirmation.
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
Design your own verification plan: Pod-level checks (strongest) → System-level checks → Process confirmation. Analyze fault context to determine what effects to check for."""


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

**Primary Evidence Definition** (for PrimaryEvidenceObserved field):
Primary evidence = **significant change from baseline OR significant deviation from expected healthy state** in the metric the fault targets. Does NOT require reaching the exact target value.
- Significant: resource metric delta ≥ 15pp, new fault artifacts, state changes (pod phase, node condition, restartCount, endpoints), network failures.
- NOT significant: reaching exact --percent target, side effects unrelated to injected fault type.
- PrimaryEvidenceObserved=false → Overall CANNOT be "verified" (use "partial" at best).

**Status Definitions**:
- **passed**: Fault effect IS observable and attributable to the injection.
- **failed**: You checked AND the expected effect is NOT observed. Mark what you see NOW.
- **skipped**: You did NOT execute this check (tool unavailable). If you actually ran an observation command, it is NEVER 'skipped'.
- **recovered_before_observation**: Fault was transient and had dissipated by the time you checked. ALL steps recovered → Overall 'unverified'.
- **expected**: A negative result that is anticipated given injection parameters (e.g., threshold not reached). Use only when other steps confirm the fault IS in effect.

**Overall Definitions**:
- **verified**: The fault effect (significant change from baseline / deviation from healthy state) is confirmed across the ENTIRE approved target set — the whole target, not a subset.
- **partial**: The effect is REAL but INCOMPLETE — confirmed on only some members of the approved target set (state coverage as N/total), or evidence mixed / observation incomplete.
- **unverified**: No significant change from baseline and no deviation from healthy state anywhere, or all steps recovered.

Checklist = OBSERVED FACTS. Overall = HOLISTIC JUDGMENT. A checklist CAN have 'failed' items while Overall says 'verified' — explain in Warnings.

The VERIFICATION_CHECKLIST is mandatory and parsed programmatically."""



def get_verifier_remember_section() -> str:
    """REMEMBER segment — recency zone anchor for U-shaped attention.

    Mirrors the 4 Core Principles + 1 tactical reminder (baseline execution).
    The convergence line is restated here deliberately: the failure it prevents
    (sampling a proven element over and over) happens LATE in the phase, when
    the primacy-zone copy is furthest away.
    """
    return """# REMEMBER
- Evidence from THIS phase only — prior phase results are NOT evidence
- Baseline comparison proves causation — SAME metric on SAME resource; degrade to healthy-state comparison, then cross-validation when baseline unavailable
- When a tool returns error, the TOOL is right
- Every step uses the strongest available reference: baseline > healthy state > cross-validation
- Submit once effect, attribution and coverage each have evidence — repeating a proven element adds no proof, and completeness of observation is not the goal
- Text responses = brief progress updates; tables and structured comparison go ONLY into submit_verification"""
