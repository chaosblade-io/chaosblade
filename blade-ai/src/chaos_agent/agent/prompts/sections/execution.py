"""Execution sections: tool usage, guidelines, and execution directives."""


def get_tools_section(phase: int = 1) -> str:
    """Tool usage guidelines section.

    Carries ONLY what tool schemas cannot express: cross-tool selection
    priority, where the skill case lives (conversation history), and phase
    discipline. Per-tool syntax/parameters/defaults live in the tool
    docstrings; bound-tool availability is carried by the binding list and
    the unknown-tool error feedback.

    Args:
        phase: 1 = planning (agent_loop), 2 = execution (execute_loop).
            Phase 2 omits skill-resource-reading guidance because the
            skill case content is in the conversation history from Phase 1
            (read_skill_resource ToolMessages), not in the system prompt.
    """
    if phase == 2:
        return """## Tool Usage Guidelines

### Tool Selection Priority
1. **Skill case in conversation history**: The active skill's instructions were read
   in Phase 1 — they are in your conversation history as tool results. Re-read them
   as the STARTING POINT for injection commands. Do NOT call skill-reading tools (not bound here).
2. **Supplementary domain knowledge**: When the skill case is insufficient, read the
   relevant knowledge document for domain context. While a documented path covers the need, do not fabricate commands — but when every documented path has empirically failed, an equivalent-effect method you devise (same target, same fault effect) is legitimate, not improvisation.
3. **Read-only context when useful**: Use read-only queries when they are needed
   to establish information for safe execution. The system owns the post-execution
   verification and recovery lifecycle.
4. **Injection tools**: Use the injection tool specified by the skill case. Before invoking
   any tool, inspect its own help/usage output to confirm the flags and parameters it
   actually supports (runtime interface wins — see Core Principles). If an
   injection attempt reports it already created a residual experiment before failing,
   account for THAT residue before choosing a subsequent action. This is partial-failure
   cleanup; normal post-injection recovery remains framework-controlled.
5. **Injection records are framework state, not your artifacts**: NEVER destroy or clean
   up the record of a SUCCESSFUL injection — it is the recovery handle and evidence for
   the stages after you, and an irreversible fault effect is exactly why it must survive.
   Clean up only artifacts you brought in (debug pods, temp files) and residue from a
   FAILED attempt.

### Parallel Calls
- "Dependent" means one call's arguments require another call's result — not that the calls are about the same thing. A probe that comes back "not found" is itself a usable answer.
- Mutation calls stay sequenced by their receipts

### Avoid Redundancy
- Do not repeat read-only queries that were just answered in a previous tool result"""

    return """## Tool Usage Guidelines

### Tool Selection Priority
1. **Skill references first (after skill activation)**: Use `read_skill_resource` to read skill reference files for accurate, up-to-date injection command syntax and parameters
2. **Knowledge docs for domain context**: Especially BEFORE skill activation or when no skill is active, use `read_knowledge_resource` to read knowledge documents — while a documented path covers the need, do not fabricate commands; an equivalent-effect path you devise after the documented ones are proven broken is legitimate
3. **Plan, don't execute**: Your output is the input to `confirmation_gate`. Capture the intended injection parameters in your plan (via `save_fault_plan`); the executor (Phase 2) will issue the actual call

### Parallel Calls
- "Dependent" means one call's arguments require another call's result — not that the calls are about the same thing. A probe that comes back "not found" is itself a usable answer.

### Avoid Redundancy
- Do not call `activate_skill` more than once in the same Phase 1 session
- Do not repeat read-only queries that were just answered in a previous tool result"""


def get_guidelines_section(
    include_method_switching: bool = True,
    phase: int = 2,
) -> str:
    """Important guidelines section.

    Args:
        include_method_switching: When False, omit the Conflict Check
            subsection — used by Phase 1 (planning) where the LLM cannot
            execute and the rules are not yet relevant. Phase 2 (execute_loop)
            keeps the default ``True`` so the executor sees conflict-check
            constraints.
        phase: 1 = planning, 2 = execution. Differentiates the deviation
            criterion: Phase 1 is read-only, so a documented path is ruled
            out by probed evidence, never by empirical failure; Phase 2
            keeps the empirical-failure wording.
    """
    lines = [
        "## Important Guidelines",
        "",
    ]
    # The runtime-feedback principle intentionally has NO copy here: the
    # executor Core Principles bullets and REMEMBER carry it in the
    # primacy/recency zones (the middle of a prompt is the lowest-attention
    # region, so a third copy added nothing).

    # Shared rule: skill-case methods come first; deviation is licensed by
    # proof that documented paths do not work (probed evidence in read-only
    # Phase 1, empirical failure in Phase 2), arbitrated by the safety guard.
    deviation = (
        "is disproved by probed evidence" if phase == 1
        else "has empirically failed"
    )
    lines.append(
        f"- Skill-case methods come first; deviate only once a documented path {deviation} — an equivalent-effect method (same target, same fault effect) is then legitimate, and the safety guard arbitrates what is dangerous"
    )
    base = "\n".join(lines)

    conflict_check = """### Pre-injection Conflict Check
Conflict checking is performed automatically by the system before you are invoked.
If active experiments were detected, you would have been routed through a confirmation gate.
You do NOT need to run additional conflict checks — focus on executing the fault injection."""

    if include_method_switching:
        return f"{base}\n\n{conflict_check}"
    return base


def get_execution_directives_section(
    skill_name: str = "",
    structured_params_hint: str = "",
    user_params_hint: str = "",
    plan: str = "",
    plan_path: str = "",
) -> str:
    """Execution phase directives for Phase 2 (execute_loop).

    Tool-agnostic execution principles. Specific tool operation steps
    (blade_help syntax, kubectl exec fallback) live in knowledge docs,
    not here — per the abstraction layering design principle.

    Args:
        skill_name: Active skill name (optional).
        structured_params_hint: Pre-defined scope/target/action hint from CLI
            structured params (e.g., "scope=pod, target=cpu, action=fullload").
        user_params_hint: JSON-serialised user-provided fault parameters.
        plan: Execution plan text.
        plan_path: Path to saved plan file.
    """
    parts = [
        "## EXECUTION PHASE DIRECTIVES",
        "The plan has been approved.",
        "",
        "### Execution Orchestration",
        "The approved plan is a contract, not a hypothesis: its commands were",
        "constructed and validated during planning — execute them as written",
        "(see Execution Discipline below). Use only capabilities within the",
        "approved scope — do not fabricate tool interfaces or expand the approved",
        "target or safety boundaries. When the plan itself needs a different",
        "assumption, target, or safety decision, use the Replan Mechanism below;",
        "when a documented method fails but the approved goal remains reachable",
        "another way, choosing an equivalent-effect alternative within the",
        "approved scope is yours to make.",
        "",
        "### Execution Discipline",
        "A tool error corrected by the tool's own feedback is a legitimate path,",
        "not a plan failure; the same method failing twice means replan, not a",
        "third attempt.",
    ]

    if plan:
        # Plan-conditioned discipline clauses (tier1-speedup unit 3): with a
        # frozen plan, the executor's job is faithful execution, not
        # re-derivation — the pre-change "current hypothesis, not a script"
        # wording licensed re-thinking every step from scratch and burned
        # the first-round reasoning budget on re-deriving already-frozen
        # commands (task inject-8b757abb: 286s before the first tool call).
        parts.extend([
            "Execute the plan's '## Execution Steps' in their written order.",
            "Before your FIRST mutation step, spend exactly one read-only call to",
            "confirm the target still exists (skip it when that step is itself",
            "read-only). Do NOT re-derive command texts, re-evaluate quoting or",
            "escaping, or reorder steps — the plan froze them.",
        ])

    parts.extend([
        "",
        "### Multi-Step Execution",
        "The approved mutation steps live in the plan's '## Execution Steps' section.",
        "Run them through tool calls (never prose), using each step's receipt to",
        "decide whether the next step still applies. A step that observes or",
        "verifies the effect is verification work — skip it. A wait or pause",
        "the plan declares as a step is executed as written, not skipped.",
        "Do not add effect observations after an issued step either; a later",
        "phase verifies the effect, and watching for it here only consumes the",
        "window. When the LAST mutation step is issued, state what was issued",
        "and through which path, then STOP — the system owns post-execution",
        "verification and recovery.",
        "",
        "### Parameter Priority",
        "When conflicting sources specify a parameter value, follow this hierarchy",
        "(highest → lowest):",
        "1. Tool runtime behavior — if a tool rejects a value, adapt to its actual interface",
        "2. User-specified parameters — user intent takes priority over template defaults",
        "3. Pre-defined structured parameters — use as specified unless a tool error proves invalid",
        "4. Skill case template defaults",
    ])

    if skill_name:
        parts.append(f"\nActive skill: {skill_name}")

    if structured_params_hint:
        parts.append("")
        parts.append("### STRUCTURED FAULT PARAMETERS (pre-defined)")
        parts.append("The user has pre-defined the fault parameters. Use these EXACT values:")
        parts.append(f"  {structured_params_hint}")
        parts.append("Do NOT override these values — UNLESS the tool returns an error")
        parts.append("proving a value is invalid for the current tool version.")
        parts.append("In that case, adapt to the tool's actual interface (see Parameter Priority).")

    if user_params_hint:
        parts.append("")
        parts.append("### USER-SPECIFIED PARAMETERS")
        parts.append("The user provided these fault-specific parameters:")
        parts.append(f"  {user_params_hint}")
        parts.append("These user parameters always take priority over template defaults.")

    if plan:
        plan_ref = f" (saved at {plan_path})" if plan_path else ""
        parts.append("")
        parts.append(f"### EXECUTION PLAN{plan_ref}")
        parts.append("This task was assessed as complex. Execute ONLY the mutation steps below.")
        parts.append(f"---\n{_execution_steps_only(plan)}\n---")

    return "\n".join(parts)


def _execution_steps_only(plan: str) -> str:
    """Slice the plan down to its '## Execution Steps' section.

    Structural isolation instead of a textual ban: the full plan also carries
    'Verification Methods' / 'Expected Impact' / 'Rollback' sections whose
    numeric criteria (sample counts, thresholds, intervals) the executor
    otherwise adopts as its own EXIT criteria and keeps observing the fault
    effect to satisfy (task inject-9bf2dddd: 571s of post-injection watching).
    Plans without the header fall back to the full text so non-standard
    plans lose nothing.
    """
    lines = plan.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## execution steps"):
            start = i
            break
    if start is None:
        return plan
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("## "):
            end = j
            break
    section = "\n".join(lines[start:end]).strip()
    return section or plan
