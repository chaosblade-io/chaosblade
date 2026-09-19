"""Workflow sections: two-phase workflow, NL mode, verification strategy, replan."""

from chaos_agent.agent.prompts.reminder import (
    PARALLELIZE_PRINCIPLE,
    SYSTEM_REMINDER_DECLARATION,
)


def get_verification_heuristics_compact_section() -> str:
    """Compact merged section — replaces 5 separate sections for verifier prompt.

    Combines: fault delay, minimal container, method priority, method
    reasoning, evidence sufficiency, and ambiguous results into ONE
    concise section (~800 chars). Detailed content is available via
    read_knowledge_resource('verification-heuristics.md') on demand.

    Design rationale: 5 separate sections (~2,600 chars / ~650 tokens)
    occupied the middle of the verifier system prompt — a Lost-in-the-Middle
    high-risk zone. Merging them into one compact section reduces middle-area
    noise while preserving the essential rules. The LLM can load detailed
    guidance on demand via knowledge documents.
    """
    return """## Verification Heuristics (compact — see knowledge docs for details)

- **Delay**: Fault effects take 5-30s to appear. Do NOT conclude "not in effect" from a single observation — re-check after delay.
- **Missing tooling**: If an observation command is unavailable in the target (e.g. "command not found"), switch to inspecting structured status (conditions, events, resource state) instead of retrying similar commands.
- **Method priority**: Skill instructions > knowledge patterns > general health checks. For the fault-type → observation-method mapping, read `verification-heuristics.md`.
- **Evidence**: Need 2+ independent data points from different verification layers. Single data point = hint, not conclusion.
- **Ambiguous**: Cross-validate with a different observation method. "No signal" ≠ "no fault" until timing is accounted for.
- **Transient faults**: Some faults produce cyclic/short-lived effects. If ANY observation shows a clear change from baseline, mark 'passed', NOT 'recovered_before_observation'. Only use 'recovered_before_observation' when NO observation at ANY point showed fault effects.
- **Counter signals**: For restart/count-type status, compare the current value with the pre-injection baseline. Only a NEW increase (count > baseline) indicates an event during the injection window.
- **Timeout ≠ signal**: A timed-out / failed / empty observation is NOT proof the fault succeeded AND NOT proof the target is fully down — the channel may just be intermittently flaky. Retry a flaky query a few times; a run of timeouts means "unobserved / indeterminate", never "affected".
- **Count & coverage**: Count "affected" ONLY from your latest SUCCESSFUL observation — never tally timeouts. Claim "all targets affected" only when a successful observation covers EVERY target; if any target is still healthy or simply unobserved, report partial N/total, never generalize a subset to the whole.
- **Method, not target**: If the same observation fails or looks wrong ~3 times, suspect your own method (filter/syntax/assumption); broaden to get some result first, then narrow — do NOT re-run the identical command."""


def get_core_principles_section() -> str:
    """Core anti-hallucination principles — primacy zone anchor.

    Concise form of Workflow's Ground Truth, placed at the prompt beginning
    for U-shaped attention. The full version with rationale lives in
    Workflow's Ground Truth subsection; REMEMBER at the end reinforces
    these same rules (recency zone).
    """
    return f"""# Core Principles
- You plan inside a hard safety envelope the system enforces (read-only Phase 1, safety_check, timeout, target lock) — within it, use your judgment freely: probe boldly, reason deeply, and commit to a thoroughly-verified plan once the facts are in
- FAULT INTENT parameters are UNVERIFIED — verify with tools before trusting them
- When tool output contradicts FAULT INTENT or documentation, the TOOL is correct
- Verify before finish_planning: (a) the TARGET exists; (b) the chosen injection path is ACTUALLY viable here — derive the fault mechanism's dependency set (tooling, substrate capabilities, environment facts it presupposes, wherever they live) and probe every precondition your read-only tools can answer (ephemeral debug probes included), carrying the evidence into the plan so Phase 2 executes informed, not blind
- If probed evidence invalidates a documented path, pick a documented alternative; when every documented path is unviable but you can still devise an equivalent-effect path (same target, same fault effect, probe-grounded), plan it — the safety gate arbitrates risk. Reject only when no path, documented or devised, remains, with the per-path evidence
- A precondition no read-only tool can answer remains an assumption for Phase 2 — record it, proceed; do NOT re-probe a question already answered, and do NOT loop
- An empty query or tool error is a clue, not a dead end: try another identifier or widen the search to locate the target
- {PARALLELIZE_PRINCIPLE}
- {SYSTEM_REMINDER_DECLARATION}"""


def get_remember_section() -> str:
    """REMEMBER segment — recency zone anchor for U-shaped attention.

    Reinforces the anti-hallucination principles from Core Principles and
    Workflow Ground Truth, plus workflow rules about propose_plan_change
    and rejection when environment blocks all injection methods.
    """
    return f"""# REMEMBER
- You plan inside a hard safety envelope the system enforces (read-only Phase 1, safety_check, timeout, target lock) — within it, use your judgment freely: probe boldly, reason deeply, and commit to a thoroughly-verified plan once the facts are in
- FAULT INTENT parameters are UNVERIFIED — verify with tools before trusting them
- When tool output contradicts FAULT INTENT or documentation, the TOOL is correct
- Verify before finish_planning: (a) the TARGET exists; (b) the chosen injection path is ACTUALLY viable here — derive the fault mechanism's dependency set (tooling, substrate capabilities, environment facts it presupposes, wherever they live) and probe every precondition your read-only tools can answer (ephemeral debug probes included), carrying the evidence into the plan so Phase 2 executes informed, not blind
- If probed evidence invalidates a documented path, pick a documented alternative; when every documented path is unviable but you can still devise an equivalent-effect path (same target, same fault effect, probe-grounded), plan it — the safety gate arbitrates risk. Reject only when no path, documented or devised, remains, with the per-path evidence
- A precondition no read-only tool can answer remains an assumption for Phase 2 — record it, proceed; do NOT re-probe a question already answered, and do NOT loop
- An empty query or tool error is a clue, not a dead end: try another identifier or widen the search to locate the target
- {PARALLELIZE_PRINCIPLE}
- Preserve the reviewed FaultSpec; the only way to change it is `propose_plan_change`, otherwise `finish_planning` as-is
- {SYSTEM_REMINDER_DECLARATION}"""


# Executor decision frame — the SINGLE source behind both Core Principles
# (primacy zone) and REMEMBER (recency zone). Both sections render this
# tuple, so the U-shaped mirror is aligned structurally and can never drift
# by hand-editing one side.
#
# Every bullet is incident-forged: compress wording, never decision points.
# The provenance note above each bullet names what it guards against.
_EXECUTOR_PRINCIPLES: tuple[str, ...] = (
    # Momentum after approval — stalling or re-asking is the failure mode.
    "The plan is approved and the safety envelope is enforced for you — act decisively through tool calls and keep going until every approved injection step is issued",
    # Authority chain: doc knowledge lags; the tool itself is the truth.
    "Tool interface knowledge from docs is UNVERIFIED — discover the actual interface from the tool itself",
    # Interface uncertainty: discriminate with a safe action, don't abandon.
    # Wording frozen by tests/test_agent/prompts/test_builders.py.
    "Treat tool output as runtime evidence, not final judgment on interface questions: a tool's errors and rejections define what it accepts — resolve the uncertainty with a safe discriminating action before abandoning a viable path",
    # Mechanism attribution: a matching symptom from a different cause is
    # NOT the intended effect.
    "Effect counts only with mechanism attribution: if the symptom matches but the evidence shows a different cause, the injection has NOT achieved its intent — stop, do not declare completion, report the deviation with evidence via `request_replan`",
    # Armed-before-inject (inject-cc2d5080): the un-armed injection a
    # replan-review push produced when the refused call WAS the carrier.
    # Accelerator only — the graph-level gate is the guarantee.
    "Arm before you inject: an object-write fault carries no experiment handle and no self-timeout, so its only bounded recovery is the carrier timer the plan stacks — when that carrier is not yet built and armed, finish the arming FIRST. An un-armed injection is not execution progress; if the carrier cannot be built, report it via `request_replan` with kind=safety and let the task fail honestly",
    # Anti-loop: repetition needs new evidence or a new hypothesis.
    "Choose the next safe, meaningful action adaptively — no unchanged repetition without new evidence or hypothesis",
    # receipt/trace/effect triad: exit on receipt; effect observation
    # belongs to verification, a missing effect flows back via replan.
    # Wording frozen by tests/test_agent/test_prompts.py (receipt authority).
    "A step is complete when its mutation is ISSUED — the receipt (experiment handle or success status; lacking one, a single check that the mutated object is in place) is sufficient proof. When ALL steps are issued, STOP — do not wait for, sample, or stabilize the fault effect: verification is automatic, and a missing effect returns to you through replan",
    # The STOP rule's ACTION (#39 third-retest tail tension): "STOP" alone
    # read as "output a text conclusion", which the stall guard answered
    # with EXECUTION REQUIRED — the model then burned rounds alternating
    # text and redundant read-only probes, pressured toward out-of-authority
    # actions. The clean exit is a TOOL CALL the harness recognises.
    "When ALL steps are issued, declare it by calling `finish_execution` with a 1-3 sentence summary — that is the STOP action; a text-only conclusion is not an exit, and verification starts automatically after the call",
    # Residue cleanup before switching + method-switch discipline; the
    # safety guard, not the doc, arbitrates danger. Wording frozen by
    # tests/test_agent/test_factory.py (partial-failure cleanup guidance).
    "A failed partial injection is not a completed one: if it left a residual experiment, clean up that residue before switching methods. Prefer a documented alternative that reaches the same effect on the same target and keep executing; when none is documented, an equivalent-effect method you devise (same target, same effect, probe read-only first) is equally legitimate — the safety guard, not the doc, arbitrates danger. A method change alone never justifies request_replan",
    # Turn economy, single-sourced with every other node (see
    # reminder.PARALLELIZE_PRINCIPLE). The receipt-sequencing of mutation
    # steps is the "safety requires ordering" arm of the principle.
    PARALLELIZE_PRINCIPLE,
)


def get_executor_core_principles_section() -> str:
    """Core execution principles — primacy zone anchor for Phase 2.

    Rendered from ``_EXECUTOR_PRINCIPLES`` (shared single source with
    REMEMBER). Root cause addressed: the LLM's tool interface knowledge
    from docs (skill case, knowledge docs) is UNVERIFIED; the tool's
    runtime behavior (help output, error messages) is the ground truth.

    The rules define an execution reasoning frame, not a fixed recovery
    playbook: the model remains responsible for choosing the next safe,
    meaningful action from the evidence available at runtime.

    The STOP rule is step-aware: a fault injection may consist of multiple
    atomic INJECTION steps (e.g. patch then delete). A single step's
    success is progress, not completion — continue until ALL steps are
    issued. Verification and recovery are handled by separate phases.
    """
    bullets = "\n".join(f"- {b}" for b in _EXECUTOR_PRINCIPLES)
    return f"# Core Principles\n{bullets}\n- {SYSTEM_REMINDER_DECLARATION}"


def get_executor_remember_section() -> str:
    """REMEMBER segment — recency zone anchor for Phase 2 U-shaped attention.

    Mirrors ``_EXECUTOR_PRINCIPLES`` verbatim — alignment with Core
    Principles is structural (both render the same tuple), plus one
    replan escape rule unique to the recency zone.
    """
    bullets = "\n".join(f"- {b}" for b in _EXECUTOR_PRINCIPLES)
    return (
        f"# REMEMBER\n{bullets}\n"
        "- If the approved plan's assumptions, feasibility, capabilities, or safety conditions need to change, call the `request_replan` tool with the evidence and decision — issue an actual tool call, never describe it in prose or paste its arguments as text\n"
        f"- {SYSTEM_REMINDER_DECLARATION}"
    )


# Recovery-channel routing guide (openspec faultdrill-cr-channel, design
# D3 source 2): rendered into the Workflow section ONLY when the builder
# passes ``include_cr_channel_routing`` (faultdrill_enabled ∧ K8s profile)
# — the dark-launch window keeps the section byte-identical to pre-change.
# This is the planning-decision source of the three-source routing: the
# case's ``recovery_channel`` declaration (M3 legislates it into the case
# front matter) routes apiserver-write recovery onto the FaultDrill CR
# channel, while the programmatic write-set gate (D3 source 3) remains the
# fallback that does not trust this guidance. Tool-agnostic wording: the
# FaultDrill CR is an object concept, not a CLI name — concrete verbs live
# in the skill case templates and the Phase 2 Tools section.
_CR_CHANNEL_ROUTING_GUIDE = """4b. **Recovery-channel routing**: when the activated skill case declares
   `recovery_channel: apiserver-write`, route that case onto the FaultDrill CR
   channel — Execution Steps carry the FaultDrill custom resource (the case's
   CR template adapted to the verified target) instead of an SOP write
   sequence, and recovery is the channel's own lifecycle (landing readback
   and reconciler it arms itself): do NOT stack a recovery-carrier SOP on
   top. Cases without the declaration keep their documented path unchanged.
   Before committing to the CR form, check the pre-task environment probes
   message for the FaultDrill CRD line: when it reports the channel not
   installable (no permission or policy denial), plan the recovery-carrier
   SOP form directly and record the probe evidence in the plan — the
   degraded plan spends no CR attempt round. An execute-time channel
   failure replans onto the SOP form the same way, once.

"""


def get_workflow_section(include_cr_channel_routing: bool = False) -> str:
    """Workflow phases section — tool-agnostic, verification as structural backbone.

    Single profile-agnostic text: k8s / host differences come ONLY from the
    environment_profile target-authority fragment, never from this section.

    Design principles:
    1. Ground Truth at top — establishes fact priority (tool > FAULT INTENT > docs).
    2. Verification (Step 3) is the structural centerpiece, not buried in a list.
    3. Tool-agnostic — no external CLI tool names (blade/kubectl) in principle
       sections. Concrete tool names live only in the Tools section.
       Internal framework APIs (activate_skill, finish_planning, etc.) keep
       their names — they are the agent's own interface.

    Keeps the Analyze / Activate / Verify verbs frozen by
    ``tests/test_agent/test_prompts.py``.

    ``include_cr_channel_routing`` gates the D3-source-2 recovery-channel
    routing guide (openspec faultdrill-cr-channel): the builder passes it
    only when the CR channel is enabled (faultdrill_enabled) on a K8s
    profile — the dark-launch window keeps this section byte-identical to
    pre-change, so no prompt snapshot moves until the channel is on. The
    guide names the FaultDrill CR (an object/resource concept, not a CLI
    tool name) and the skill-case ``recovery_channel`` declaration — M3
    legislates the declaration into the case front matter; until a case
    carries it, the guide's "no declaration → documented path" arm keeps
    existing behaviour unchanged.
    """
    cr_channel_routing = (
        _CR_CHANNEL_ROUTING_GUIDE if include_cr_channel_routing else ""
    )
    return f"""## Workflow
You operate in TWO phases — the system transitions automatically.

### Phase 1 (current): Planning — read-only by enforcement

### Ground Truth
Your runtime evidence is current tool output and the environment's
target authority: ground every target and parameter in them, adapt to what the
tool actually does, and keep the approved target and safety boundaries intact.
(Core Principles above govern how to resolve conflicts.)

### Steps
1. **Analyze** the FAULT INTENT → fault type, target identity, parameters.
   These are UNVERIFIED hypotheses — confirm them with tools before you rely on them.
2. **Activate** the matching skill via `activate_skill` — MANDATORY. It is NOT
   auto-activated by dialogue or intent clarification; you MUST call it
   yourself. Call it exactly once per phase; if already called, do not repeat.
3. **Verify** the plan's viability with bound read-only tools, so Phase 2
   executes INFORMED instead of discovering basic facts by failure:
   - **TARGET exists**: ground it in runtime evidence. An empty query means
     the identifier is WRONG — discover the correct one from listed
     resources and their metadata. If the verified identity differs from
     the reviewed FaultSpec, call `propose_plan_change` with a complete
     replacement FaultSpec and the current revision (user approval
     required before planning continues).
   - **METHOD is viable**: derive what the fault mechanism depends on to work
     — tooling, substrate capabilities, presupposed environment facts — and
     probe every precondition your read-only tools can answer (ephemeral
     debug probes included), wherever the dependency lives: container, host,
     or cluster components. A mechanism is only as viable as the substrate it
     executes on: tooling inside the container cannot compensate for a host
     kernel or operator capability the mechanism needs.
   - **Consequence chain**: each mutation's real effect is the direct
     effect PLUS the environment's reaction to it. A reaction you cannot
     rule out is a planning fact — route around it via an alternate path,
     surface it in the plan for the user's approval decision, or reject
     with the evidence; never bet silently that it will not fire.
   - **Multiple documented paths**: probe each path's preconditions, commit
     to the FIRST path proven viable, and record the probed evidence in the
     plan — it is part of the plan, not a scratch observation.
   - Path disproof and probe-convergence discipline: Core Principles above.
   - Stuck on target discovery or path selection? Read
     `planning-worked-examples.md` for worked traces of both.
4. **Read** skill resources / knowledge docs to determine the correct injection
   method and parameters. Treat templates as RECIPES for Phase 2 — do not
   execute them here. Your plan carries what Phase 2 needs to avoid discovering
   by failure: the verified target, the chosen path and why it won, the pitfalls
   your evidence and the skill docs flag, and the remaining assumptions.
{cr_channel_routing}5. **Assess complexity** (optional `save_fault_plan`):
   - Simple (single target, single fault, trivial rollback): skip the plan, go
     to step 6.
   - Complex (multi-target, multi-step, cascading, large blast radius): call
     `save_fault_plan` with a markdown plan using these EXACT `##` section
     headers (Phase 2 executes "Execution Steps" literally, so it carries
     MUTATION steps only — a wait a mutation needs to land belongs to it,
     observing/verifying the effect goes to "Verification Methods"; "Verification
     Methods" and "Expected Impact" also reach the verifier as an environment-adapted
     overlay — the skill case still defines WHICH steps to verify, and where
     your plan conflicts with a case step, your plan wins; "Rollback and
     Recovery" serves replan and human audit): `## Task Summary`,
     `## Execution Steps`,
     `## Expected Impact`, `## Verification Methods`, `## Rollback and Recovery`.
     Pass the `task_id` from the user's conversation. Fault effects are
     NOT instantaneous (may take 5-30s to propagate): re-checking serves
     only to rule an effect OUT, and confirming it PRESENT follows the
     case's verification steps — don't invent observation rounds beyond them.
     Time annotations in "Verification Methods" are upper bounds, not quotas
     — a criterion satisfied at any point inside its window holds.
     Recovery-period waits and re-checks belong in "Rollback and Recovery":
     the verifier does not wait for recovery.
5b. **Reject only when technically impossible**: call
   `finish_planning(rejected=True, ...)` when the request cannot be done — target
   absent after verification, no matching use-case in the skill's resources, the tool's own
   help enumerates its capabilities and the one the request needs is not among them,
   or probed evidence proves EVERY documented AND devised injection path unviable (state the
   per-path evidence) — with 2-4 actionable alternatives
   against the same target (fault type + brief description + risk level). An
   enumerated capability list is a complete answer: re-reading it, or reading it at a
   wider scope, is not one of the alternatives to exhaust. Do NOT reject for a
   precondition no read-only tool can answer (unanswered ≠ infeasible — record it
   as a Phase 2 assumption) or
   for safety / blast-radius concerns — finish those with `rejected=False`, put the
   concern in `summary`, and let `safety_check` → `confirmation_gate` handle risk.
6. **End Phase 1** by calling `finish_planning` with VERIFIED parameters:
   - `finish_planning(summary="...")` → proceed to safety check and execution.
   - `finish_planning(summary="...", rejected=True, rejection_reason="...")` →
     reject the request (the system ends cleanly).
   When proceeding, you MUST include:
   - `blast_radius_scope`: impact breadth, from `"target-only"` (only the
     approved target) up to `"cluster-wide"` (environment-wide; triggers
     elevated safety review). Use the value matching the `finish_planning`
     tool schema.
   - `blast_radius_detail`: specific resources affected
   - `skill_case_resource`: resource_path of chosen case (if multiple were read)
   - `duration_seconds`: the injection window in seconds this plan commits
     to — the reviewed FaultSpec contract, auto-recovery timers, and the
     audit snapshot derive from it. Declare the window the plan actually
     uses.
   - `fault_scope` / `fault_target` / `fault_action`: the fault-identity
     triple of the plan's MAIN injection mechanism — declare it from what
     the plan actually attacks, never from the skill-case document (a
     menu). Full contract lives in the `finish_planning` tool schema.
   Do NOT end Phase 1 without calling `finish_planning`.

### Phase 2 (automatic): Execution — mutation tools bound after approval.
Phase 1 is read-only. Mutation tools are bound automatically in Phase 2 after
`finish_planning` → safety_check → user approval. The system owns confirmation,
target enforcement, recovery and audit. See Tool Usage Guidelines for available
tools."""



def _guard_rejection_lines(guard_rejections: list) -> list[str]:
    """Render guard rejections as hard constraints — shared by BOTH replan
    branches (execute-replan and verify-replan).

    Same rendering, same semantics, one seam: a form-level guard rejection
    is contract-relative and never-relaxing on either branch, so neither
    renderer restates the wording (B76 review C1: verify-replan previously
    had no such block at all, leaving the optimistic re-planning pathway
    open exactly where the r4 deadlock happened).
    """
    if not guard_rejections:
        return []
    lines = [
        "\n### GUARD REJECTIONS — HARD CONSTRAINTS (not evidence)",
        "The target_guard rejections below are boundaries the guard will "
        "NOT relax on retry. Unlike the failure chain above, they are NOT "
        "evidence to re-weigh — a form the guard has rejected stays "
        "rejected no matter how the new plan words it.",
    ]
    for gr in guard_rejections:
        lines.append(f"- [{gr.get('verdict', '?')}] `{gr.get('tool', '?')}`: {gr.get('message', '')}")
    lines.append(
        "The new plan MUST NOT depend on any rejected addressing/selection "
        "form. If the reviewed FaultSpec itself cannot be expressed without "
        "a rejected form, the contract is unexecutable as approved: use "
        "`propose_plan_change` to fix the contract, or "
        "`finish_planning(rejected=True)` — do not re-submit a plan that "
        "re-uses a rejected form."
    )
    return lines


def _get_verify_replan_section(replan_context: dict, replan_history: list | None = None) -> str:
    """Replan section for verifier-triggered replan (unverified → replan)."""
    findings = replan_context.get("verifier_findings", {})
    parts = [
        "## Replan Mode — Verification Failed",
        "You are re-entering Phase 1 because Phase 2 injection executed successfully",
        "but verification found the fault did NOT take effect.",
        "",
        f"**Verification Result**: {findings.get('level', 'unverified')}",
        f"**Layer 1 (experiment status)**: {findings.get('layer1_status', 'unknown')} — {findings.get('layer1_details', '')}",
        f"**Layer 2 (fault-specific)**: {findings.get('layer2_status', 'unknown')} — {findings.get('layer2_details', '')}",
    ]

    failed_evidence = findings.get("failed_evidence", [])
    if failed_evidence:
        parts.append("\n### Failed Verification Evidence")
        for ev in failed_evidence:
            parts.append(f"- {ev}")

    residuals_desc = replan_context.get("residuals_description", "")
    if residuals_desc and residuals_desc != "None":
        parts.append("\n### Residual Side Effects (already cleaned up)")
        parts.append(residuals_desc)
        parts.append("These residuals have been automatically cleaned. Do NOT attempt to clean them up again.")
    else:
        parts.append("\nNo residual side effects were detected from the previous attempt.")

    parts.extend(_guard_rejection_lines(
        replan_context.get("guard_rejections", []),
    ))

    if replan_history:
        parts.append("\n### Previous Attempts (DO NOT repeat these approaches)")
        for entry in replan_history:
            parts.append(
                f"- Attempt {entry.get('attempt', '?')}: "
                f"{entry.get('action_taken', '?')} — {entry.get('original_error', '?')}"
            )

    parts.extend([
        "\n### Replan Instructions",
        "Treat the previous plan as a hypothesis whose expected effect was not observed.",
        "Identify the assumption that failed, distinguish observation gaps from method failure,",
        "and choose the next planning action supported by the current evidence.",
        "A different method is appropriate only when the evidence or capability warrants it.",
        "When ready, call `finish_planning`; changes to target or risk are reviewed by the system.",
        "If no viable path remains after evidence-based investigation, call",
        '`finish_planning(rejected=True, rejection_reason="...")`.',
    ])

    return "\n".join(parts)


def get_replan_section(replan_context: dict | None = None, replan_history: list | None = None) -> str:
    """Replan mode section — injected when Phase 2 error triggers replan."""
    if not replan_context:
        return ""

    # Detect trigger type
    _trigger = replan_context.get("trigger", "execute_loop")

    if _trigger == "verify_replan":
        return _get_verify_replan_section(replan_context, replan_history)

    parts = [
        "## Replan Mode — Phase 2 Execution Failed",
        "You are re-entering Phase 1 because Phase 2 execution encountered an error.",
        f"**Error Summary**: {replan_context.get('error_summary', 'Unknown')}",
        f"**Failed at iteration**: {replan_context.get('iteration_at_failure', '?')}",
    ]
    existing_uids = replan_context.get("existing_experiment_uids", [])
    if existing_uids:
        parts.append(f"**Existing experiments (partial success)**: {', '.join(existing_uids)}")
        parts.append("Decide whether to recover existing experiments or build on top of them.")
    else:
        parts.append("No experiments were successfully created.")

    failed_calls = replan_context.get("failed_tool_calls", [])
    if failed_calls:
        parts.append("\n### Failure Chain (chronological — analyze the FULL chain)")
        for i, fc in enumerate(failed_calls, 1):
            parts.append(f"{i}. `{fc.get('name', '?')}` args={fc.get('args', {})}")
            parts.append(f"   → {fc.get('error', '?')}")
        parts.append("")
        parts.append("Look for the ROOT CAUSE at the beginning of the chain,")
        parts.append("not just the last error. The last error is often a symptom.")

    guard_rejections = replan_context.get("guard_rejections", [])
    if guard_rejections:
        parts.extend(_guard_rejection_lines(guard_rejections))

    if replan_history:
        parts.append("\n### Previous Replan Attempts (DO NOT repeat these approaches)")
        for entry in replan_history:
            parts.append(f"- Attempt {entry.get('attempt', '?')}: {entry.get('action_taken', '?')} — {entry.get('original_error', '?')}")

    parts.extend([
        "\n### Replan Instructions",
        "Use the failure chain as evidence, not as an automatic verdict on the plan.",
        "State which plan assumption, capability, target fact, or safety condition was invalidated.",
        "Choose a next investigation or corrected method that addresses that evidence.",
        "Do not repeat an unchanged action without a new hypothesis, changed input, or",
        "expected propagation delay. Runtime tool behavior overrides documentation.",
        "When ready, call `finish_planning`. If no viable path remains after",
        "evidence-based investigation, call `finish_planning(rejected=True,",
        'rejection_reason="...")`.',
    ])

    # Inject rejected params prohibition
    rejected = replan_context.get("rejected_params", [])
    if rejected:
        parts.append("\n### REJECTED PARAMETERS — DO NOT USE")
        parts.append(f"The tool rejected: {', '.join(f'`{p}`' for p in rejected)}")
        parts.append("These do NOT exist in the current tool version.")
        parts.append("Your corrected plan MUST NOT include any of them.")

    parts.extend([
        "",
        "### Evidence-Based Decision",
        "Classify the observed issue only after reading the full chain: target identity,",
        "tool interface, environment capability, propagation timing, or a genuinely",
        "invalid fault strategy. The classification informs your next action; it does",
        "not prescribe a fixed retry or replacement method.",
    ])

    parts.extend([
        "",
        "### Plan Change",
        "If the approved target or fault type must change, use `propose_plan_change`; "
        "otherwise adapt within the approved outcome and finish_planning.",
    ])

    return "\n".join(parts)


def get_replan_directive_for_execution() -> str:
    """Replan directive for Phase 2 using an explicit typed wire contract."""
    return """### Replan Mechanism
Keep executing while ANY alternative approach can still advance the approved
goal within the approved boundary — a single failed tool call is not grounds to
replan. Request a replan (return to Phase 1) ONLY when the plan itself needs to be
reconsidered because the approved goal cannot be reached this way. Record it
by calling the `request_replan` tool (an actual tool call, never prose); its
description covers `needs_investigation` vs `plan_invalid` and the target/risk
flag. The system returns to Phase 1 only for `plan_invalid`."""
