"""Execution sections: tool usage, output style, K8s connection, guidelines, and execution directives."""


def get_tools_section(phase: int = 1) -> str:
    """Tool usage guidelines section.

    Args:
        phase: 1 = planning (agent_loop), 2 = execution (execute_loop).
            Phase 2 omits skill-resource-reading guidance because the active
            skill's content is already embedded in the execution prompt and
            the corresponding tools are not bound to the executor.
    """
    if phase == 2:
        return """## Tool Usage Guidelines

### Tool Selection Priority
1. **Skill content is in your prompt**: The active skill's instructions have already been loaded for this phase — do NOT call skill-reading tools (they are not bound here). Re-read the relevant section of your prompt.
2. **Knowledge docs for domain context**: When you need supplementary domain knowledge, use `read_knowledge_resource`. Do NOT guess or improvise blade commands.
3. **Read before write**: Use `kubectl(subcommand="get"/"describe")` for verification, reserve `kubectl(subcommand="exec")` for active checks.
4. **Blade tools for faults**: Use `blade_create` / `blade_status` for fault creation and inspection. `blade_destroy` is NOT bound in this phase — recovery is framework-controlled.

### Parallel Calls
- You MAY make multiple independent kubectl calls in a single turn (e.g., check pods AND nodes simultaneously)
- Do NOT make dependent calls in parallel (e.g., wait for blade_create result before running blade_status)

### Avoid Redundancy
- Do not repeat kubectl queries that were just answered in a previous tool result
- Do not call blade_status repeatedly for the same uid in the same turn"""

    return """## Tool Usage Guidelines

### Tool Selection Priority
1. **Skill references first (after skill activation)**: Use `read_skill_resource` to read skill reference files for accurate, up-to-date command syntax and parameters
2. **Knowledge docs for domain context**: Especially BEFORE skill activation or when no skill is active, use `read_knowledge_resource` to read knowledge documents — do NOT guess or improvise blade commands
3. **Read before write**: Use `kubectl(subcommand="get"/"describe")` for verification — `exec` is Phase 2 only
4. **Plan, don't execute**: Your output is the input to `confirmation_gate`. Capture the intended `blade_create` arguments in your plan (via `save_fault_plan`); the executor (Phase 2) will issue the actual call.

### Resource Lookup Priority
When you need blade command syntax or parameters:
1. **Skill references** (`read_skill_resource`) — contains accurate, up-to-date command reference
2. **Knowledge docs** (`read_knowledge_resource`) — contains supplementary domain knowledge
3. **Never guess** — if unsure, always read the reference first

### Parallel Calls
- You MAY make multiple independent kubectl calls in a single turn (e.g., check pods AND nodes simultaneously)
- Do NOT make dependent calls in parallel (e.g., don't call `kubectl describe` before the matching `kubectl get` returns)

### Avoid Redundancy
- Do not re-activate a skill that is already active (check conversation history)
- Do not repeat kubectl queries that were just answered in a previous tool result"""


def get_output_section() -> str:
    """Communication style section."""
    return """## Communication Style

- **Lead with conclusions**: State the result first, then supporting details
- **Key milestones**: Brief updates at critical points (skill activated, fault injected, verification started)
- **Errors and blockers**: Provide full detail only when something goes wrong or is blocked
- **Avoid verbosity**: Do not narrate your reasoning process unless the user asks
- **Structured results**: Use consistent format for blade results: `blade_uid | status | target | details`"""


def get_k8s_connection_section() -> str:
    """K8s cluster connection section."""
    return """## K8s Cluster Connection
All kubectl and blade tools support optional `kubeconfig`, `context`, and `cluster` parameters.
If the user specifies a kubeconfig path or cluster context, you MUST pass it to every tool call.
For blade_create parameter details (namespace/names handling), see the tool schema."""


def get_guidelines_section(include_method_switching: bool = True) -> str:
    """Important guidelines section.

    Args:
        include_method_switching: When False, omit the Injection Method
            Switching subsection — used by Phase 1 (planning) where the LLM
            cannot execute and the rules are not yet relevant. Phase 2
            (execute_loop) keeps the default ``True`` so the executor sees
            method-switching constraints. The detailed switching catalogue
            also lives in the ``chaosblade-cli`` knowledge doc for on-demand
            recall.
    """
    base = """## Important Guidelines

### Runtime Feedback Priority (CRITICAL)
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
   trust the tool output and adapt your approach

- Follow the skill instructions exactly - do not improvise blade commands
- Capture the blade UID from every create command - it is needed for recovery
- Report results in a structured format including blade_uid, status, and verification details
- If verification fails, do NOT retry injection without user guidance"""

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
    plan: str = "",
    plan_path: str = "",
) -> str:
    """Execution phase directives for Phase 2 (execute_loop).

    Replaces the inline exec_directives list that was previously in
    builders.py, providing better testability and separation.

    Args:
        skill_name: Active skill name (optional).
        structured_params_hint: Pre-defined scope/target/action hint from CLI
            structured params (e.g., "scope=pod, target=cpu, action=fullload").
        plan: Execution plan text.
        plan_path: Path to saved plan file.
    """
    parts = [
        "## EXECUTION PHASE DIRECTIVES",
        "You are now in the execution phase. The plan has been approved.",
        "Follow the skill instructions precisely to inject the fault.",
        "Use blade_create to inject ChaosBlade faults, and kubectl for K8s operations.",
        "",
        "### When blade_create Fails",
        "1. **Fallback — kubectl exec tool pod**:\n"
        "   `kubectl get pods -n chaosblade -l app=otel-c-tool` → find a running pod →\n"
        "   `kubectl exec <pod> -n chaosblade -- blade create k8s <scenario> [flags]`\n"
        "   NOTE: no --kubeconfig inside blade command (pod uses ServiceAccount).\n"
        "   Extract blade_uid from the JSON response.\n"
        "2. **Constraint**: alternative methods MUST come from the skill case's "
        "injection section. Do NOT improvise methods not listed there.\n"
        "3. **If all methods exhausted** → output `[REPLAN]`.\n"
        "4. If no tool pod available → consider kubectl-native alternatives "
        "(scale/cordon/patch/taint).\n\n"
        "Rules:\n"
        "- timeout: auto-applied by tools; pass explicitly only if user specifies custom value\n"
        "- blade_uid: always capture from response for recovery\n"
        "- blast radius: must remain consistent with original plan\n"
        "- report: notify user of any method switch",
        "",
        "### MULTI-STEP INJECTION (kubectl-native)",
        "When the skill case requires multiple kubectl commands:\n"
        "1. Execute each step IN ORDER\n"
        "2. Check result before proceeding to next step\n"
        "3. Continue until ALL steps are done — do NOT conclude after first success",
    ]

    if skill_name:
        parts.append(f"Active skill: {skill_name}")

    if structured_params_hint:
        parts.append("")
        parts.append("### STRUCTURED FAULT PARAMETERS (pre-defined)")
        parts.append("The user has pre-defined the fault parameters. Use these EXACT values for blade_create:")
        parts.append(f"  {structured_params_hint}")
        parts.append("Do NOT override these values. Construct the blade command from these parameters directly.")

    if plan:
        plan_ref = f" (saved at {plan_path})" if plan_path else ""
        parts.append("")
        parts.append(f"### EXECUTION PLAN{plan_ref}")
        parts.append("This task was assessed as complex. Execute step by step:")
        parts.append(f"---\n{plan}\n---")
        parts.append("After completing all injection steps, verify results and report blade_uid for each experiment.")

    return "\n".join(parts)
