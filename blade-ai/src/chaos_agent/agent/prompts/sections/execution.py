"""Execution sections: tool usage, guidelines, and execution directives."""


def get_tools_section(phase: int = 1) -> str:
    """Tool usage guidelines section.

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
   as a STARTING POINT for injection commands. Do NOT call skill-reading tools (not bound here).
2. **Knowledge docs for domain context**: When you need supplementary domain knowledge, use `read_knowledge_resource`. Do NOT guess or improvise blade commands.
3. **Pre-injection checks only**: Use `kubectl(subcommand="get"/"describe")` ONLY to confirm the target exists before injection. Do NOT use kubectl to verify injection effects — that is handled automatically after execution.
4. **Blade tools for faults**: Use `blade_create` to inject faults. Call `blade_help`
   first to verify available flags — skill docs may be outdated. Only call tools that
   are bound to you in this phase — recovery is framework-controlled.

### Parallel Calls
- You MAY make multiple independent kubectl calls in a single turn (e.g., check pods AND nodes simultaneously)
- Do NOT make dependent calls in parallel

### Avoid Redundancy
- Do not repeat kubectl queries that were just answered in a previous tool result"""

    return """## Tool Usage Guidelines

### Tool Selection Priority
1. **Skill references first (after skill activation)**: Use `read_skill_resource` to read skill reference files for accurate, up-to-date injection command syntax and parameters
2. **Knowledge docs for domain context**: Especially BEFORE skill activation or when no skill is active, use `read_knowledge_resource` to read knowledge documents — do NOT guess or improvise injection commands
3. **Read before write**: Use cluster query tools for verification — mutation tools are Phase 2 only
4. **Plan, don't execute**: Your output is the input to `confirmation_gate`. Capture the intended injection parameters in your plan (via `save_fault_plan`); the executor (Phase 2) will issue the actual call.

### Timeout Protection
Every fault injection experiment MUST have timeout protection to prevent
indefinite residue. The default timeout is applied automatically by the
injection tool. Pass a custom value only if the user specifies one.

### Parallel Calls
- You MAY make multiple independent cluster query calls in a single turn (e.g., check pods AND nodes simultaneously)
- Do NOT make dependent calls in parallel

### Avoid Redundancy
- Do not re-activate a skill that is already active (check conversation history)
- Do not repeat cluster queries that were just answered in a previous tool result"""


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
        phase: 1 = planning (omit Runtime Feedback Priority — already covered
            by Workflow's Ground Truth section). 2 = execution (full version
            with Runtime Feedback Priority, since the executor deals with tool
            errors directly).
    """
    runtime_feedback = """### Runtime Feedback Priority
Your knowledge about tool interfaces comes from documentation, which may be
outdated or wrong. The tool's actual behavior at runtime is always the ground truth.

When ANY tool returns an error or unexpected result:
1. DO NOT assume the documentation is correct and the tool is wrong
2. The tool is RIGHT — adapt your approach to match what the tool actually does
3. Before retrying a failed command, verify the tool's actual interface
   (e.g. run the tool's help/usage command) to see what it really supports
4. If a parameter/flag/subcommand was rejected, it does NOT exist in the
   current tool version — do NOT retry it, regardless of what documentation says
5. If the tool's output contradicts skill instructions or knowledge docs,
   trust the tool output and adapt your approach"""

    lines = [
        "## Important Guidelines",
        "",
    ]
    # Phase 1: Ground Truth in Workflow already covers this principle.
    # Phase 2: still needs it because executor deals with tool errors directly.
    if phase == 2:
        lines.append(runtime_feedback)
        lines.append("")

    # Shared rule: both phases must follow skill instructions
    lines.append(
        "- Follow the skill instructions exactly — do not improvise injection commands"
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
        "### Injection Failure Escalation",
        "1. Try the standard injection tool based on skill case instructions",
        "2. If it fails, READ the error — the error tells you what's actually wrong",
        "3. Adapt parameters based on the error and retry",
        "4. If adaptation fails, try alternative methods from the skill case",
        "5. If all methods fail, output `[REPLAN]`",
        "Do NOT improvise methods not listed in the skill case.",
        "",
        "### Multi-Step Execution",
        "When the skill case requires multiple injection steps:",
        "- Execute each step IN ORDER",
        "- Check result before proceeding to next step",
        "- Continue until ALL steps are done — do NOT conclude after first success",
        "",
        "### Parameter Priority",
        "When the skill case template uses a default value and the user specified",
        "a different value, the USER'S value takes priority.",
    ]

    if skill_name:
        parts.append(f"\nActive skill: {skill_name}")

    if structured_params_hint:
        parts.append("")
        parts.append("### STRUCTURED FAULT PARAMETERS (pre-defined)")
        parts.append("The user has pre-defined the fault parameters. Use these EXACT values:")
        parts.append(f"  {structured_params_hint}")
        parts.append("Do NOT override these values.")

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
        parts.append("This task was assessed as complex. Execute step by step:")
        parts.append(f"---\n{plan}\n---")

    return "\n".join(parts)
