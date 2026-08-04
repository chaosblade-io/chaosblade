"""Programmatic injection plan generator.

Produces a complete, human-readable fault injection plan in Markdown
from the structured state that agent_loop has populated. Used by
confirmation_gate's dry_run branch (/plan command).
"""

import re

from langchain_core.messages import AIMessage

from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec
from chaos_agent.agent.state import AgentState
from chaos_agent.utils.fault_type import build_blade_create_args, ensure_min_duration


def generate_injection_plan(state: AgentState) -> str:
    """Build the full fault-injection plan markdown from agent_loop-filled state."""
    spec = read_fault_spec(state) or FaultSpec()
    sections = [
        _section_target(spec),
        _section_inject_command(spec, state),
        _section_baseline_preview(spec),
        _section_verification_strategy(state),
        _section_recovery_strategy(state),
        _section_safety_assessment(state),
        _section_timing(spec),
        _section_reasoning(state),
    ]
    body = "\n\n".join(s for s in sections if s)
    return f"# Fault Injection Plan\n\n{body}\n\n---\nConfirm and run: `/run` | Adjust: `/plan <change request>`"


def _section_target(spec: FaultSpec) -> str:
    lines = ["## Target"]
    if spec.namespace:
        lines.append(f"- Namespace: `{spec.namespace}`")
    if spec.names:
        names_str = ", ".join(f"`{n}`" for n in spec.names)
        lines.append(f"- Names: {names_str}")
    if spec.labels:
        labels_str = ", ".join(f"{k}={v}" for k, v in spec.labels.items())
        lines.append(f"- Labels: `{labels_str}`")
    if spec.scope or spec.blade_target or spec.blade_action:
        lines.append(
            f"- Fault: {spec.scope}-{spec.blade_target} {spec.blade_action}"
        )
    if not any(x for x in [spec.namespace, spec.names, spec.labels, spec.scope]):
        lines.append("- (target information is incomplete)")
    return "\n".join(lines)


def _section_inject_command(spec: FaultSpec, state: AgentState) -> str:
    if not (spec.scope and spec.blade_target and spec.blade_action):
        return ""

    kubeconfig = state.get("kubeconfig") or ""
    names_str = ",".join(spec.names) if spec.names else ""
    labels_str = (
        ",".join(f"{k}={v}" for k, v in spec.labels.items())
        if spec.labels else ""
    )

    args = build_blade_create_args(
        scope=spec.scope,
        target=spec.blade_target,
        action=spec.blade_action,
        namespace=spec.namespace,
        names=names_str,
        labels=labels_str,
        kubeconfig=kubeconfig,
        params=dict(spec.params) if spec.params else None,
        params_flags=list(spec.params_flags) if spec.params_flags else None,
    )

    # Format as human-readable command
    parts = [f"blade create k8s {spec.scope}-{spec.blade_target} {spec.blade_action}"]
    if args.get("namespace"):
        parts.append(f"  --namespace {args['namespace']}")
    if args.get("names"):
        parts.append(f"  --names {args['names']}")
    if args.get("labels"):
        parts.append(f"  --labels {args['labels']}")
    if args.get("flags"):
        for flag_pair in _split_flags(args["flags"]):
            parts.append(f"  {flag_pair}")
    from chaos_agent.transports import is_kubewiz_channel
    if not is_kubewiz_channel() and kubeconfig:
        parts.append(f"  --kubeconfig {kubeconfig}")

    cmd_str = " \\\n".join(parts)
    return f"## Injection Command\n\n```bash\n{cmd_str}\n```"


def _section_baseline_preview(spec: FaultSpec) -> str:
    if not (spec.scope and spec.blade_target):
        return ""

    from chaos_agent.agent.nodes.baseline.baseline_capture import _lookup_baseline_commands
    from chaos_agent.transports import profile_of, resolve_channel_name

    # Same capability profile the runtime baseline_capture node will use, so
    # the preview matches what actually runs (k8s = kubectl, host = shell).
    profile = profile_of(resolve_channel_name())
    commands = _lookup_baseline_commands(
        profile, spec.scope, spec.blade_target, spec.blade_action,
    )
    if not commands:
        return (
            "## Baseline Capture\n\nNo predefined baseline-capture commands for this fault "
            "(an LLM-driven dynamic strategy will be used)."
        )

    lines = ["## Baseline Capture (runs automatically before injection)"]
    for i, cmd in enumerate(commands, 1):
        rendered = _resolve_baseline_template(cmd.command, spec)
        lines.append(f"{i}. `{rendered}` — {cmd.description}")
    return "\n".join(lines)


def _section_verification_strategy(state: AgentState) -> str:
    lines = [
        "## Verification Strategy",
        "",
        "**Layer 1 (automatic)**: `blade status <uid>` confirms the experiment status is Success",
    ]

    skill_case = state.get("skill_case_content") or ""
    l2_content = _extract_section(skill_case, "注入验证")
    if l2_content:
        lines.append("")
        lines.append("**Layer 2 (observed verification)**:")
        for item in l2_content:
            lines.append(f"- {item}")
    else:
        lines.append("")
        lines.append(
            "**Layer 2 (observed verification)**: the LLM will verify the injection's "
            "effect automatically by comparing against the baseline"
        )

    return "\n".join(lines)


def _section_recovery_strategy(state: AgentState) -> str:
    lines = [
        "## Recovery Strategy",
        "",
        "1. `blade destroy <uid>` — destroy the experiment and remove the injected fault",
    ]

    skill_case = state.get("skill_case_content") or ""
    recovery = _extract_section(skill_case, "注入恢复")
    if recovery:
        for i, item in enumerate(recovery, 2):
            lines.append(f"{i}. {item}")

    verify = _extract_section(skill_case, "恢复验证")
    if verify:
        lines.append("")
        lines.append("**Recovery verification**:")
        for item in verify:
            lines.append(f"- {item}")

    return "\n".join(lines)


def _section_safety_assessment(state: AgentState) -> str:
    safety_status = state.get("safety_status", "pending")
    safety_reason = state.get("safety_reason") or ""
    health_report = state.get("target_health_report") or ""
    conflicts = state.get("conflict_uids") or []
    safety_score = state.get("safety_score") or {}

    lines = ["## Safety Assessment"]
    lines.append(f"- Status: **{safety_status}**")
    if safety_reason:
        lines.append(f"- Note: {safety_reason}")
    if conflicts:
        lines.append(f"- Conflicting experiments: {', '.join(conflicts)}")
    else:
        lines.append("- Conflicting experiments: none")
    if health_report:
        lines.append(f"- Health pre-check: {health_report}")

    # E10 — render multi-dimensional safety score when present.
    if safety_score:
        overall = safety_score.get("overall", 0)
        level = safety_score.get("level", "")
        lines.append(f"- Risk score: **{overall}/100** ({level})")
        for dim in ("blast_radius", "frequency", "time", "topology"):
            d = safety_score.get(dim) or {}
            if d:
                lines.append(
                    f"  - {dim}: {d.get('value', 0)} — {d.get('explanation', '')}"
                )

    return "\n".join(lines)


def _section_timing(spec: FaultSpec) -> str:
    timeout_str = spec.params.get("timeout", "")
    if not timeout_str and spec.scope and spec.blade_target and spec.blade_action:
        timeout_val = ensure_min_duration(
            None, spec.scope, spec.blade_target, spec.blade_action
        )
        timeout_str = str(timeout_val)

    if not timeout_str:
        return ""

    try:
        timeout_s = int(timeout_str)
    except (ValueError, TypeError):
        return f"## Timing\n\n- Injection duration: {timeout_str}"

    minutes = timeout_s // 60
    total_est = minutes + 2  # baseline + verification overhead
    return (
        f"## Timing\n\n"
        f"- Injection duration: {timeout_s}s ({minutes}min)\n"
        f"- Estimated total: ~{total_est}min (including baseline capture + verification)"
    )


def _section_reasoning(state: AgentState) -> str:
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        if getattr(msg, "tool_calls", None):
            continue
        content = getattr(msg, "content", "") or ""
        if content.strip():
            return f"## Planning Rationale\n\n{content.strip()}"
    return ""


# ── Helpers ──


def _extract_section(skill_case: str, section_name: str) -> list[str]:
    """Extract the item list of a named section from ``skill_case_content``.

    ``section_name`` is a heading in the (Chinese) skill-case markdown, so it
    is passed through verbatim as a literal to match against.
    """
    if not skill_case:
        return []
    pattern = rf"\*\*{re.escape(section_name)}\*\*[：:]\s*\n(.*?)(?=\n\*\*|\n---|\Z)"
    match = re.search(pattern, skill_case, re.DOTALL)
    if not match:
        return []
    block = match.group(1).strip()
    items = []
    for line in block.splitlines():
        line = line.strip()
        if line and re.match(r"^\d+\.", line):
            items.append(re.sub(r"^\d+\.\s*", "", line))
        elif line and line.startswith("-"):
            items.append(line.lstrip("- "))
    return items


def _resolve_baseline_template(template: str, spec: FaultSpec) -> str:
    """Resolve template variables in a baseline command string using FaultSpec.

    Operates on the full ``BaselineCommand.command`` string (e.g.
    ``kubectl describe node {node_name}`` or a bare host diagnostic), so the
    ``/plan`` preview shows the same command the runtime will execute.
    """
    v_args = template
    namespace = spec.namespace
    names = list(spec.names)
    node_name = names[0] if names else ""
    pod_name = "" if spec.scope == "node" else (names[0] if names else "")
    # Stay consistent with ``baseline_capture._resolve_templates``:
    # ``{label_selector}`` MUST render as ``-l key=value``, otherwise the command
    # shown in the plan preview is wrong when copy-pasted (kubectl would treat a
    # bare ``key=value`` as a pod name).
    label_selector = (
        "-l " + ",".join(f"{k}={v}" for k, v in spec.labels.items())
        if spec.labels else ""
    )

    if "{namespace}" in v_args:
        v_args = v_args.replace("{namespace}", namespace or "<namespace>")
    if "{node_name}" in v_args:
        v_args = v_args.replace("{node_name}", node_name or "<node>")
    if "{pod_name}" in v_args:
        v_args = v_args.replace("{pod_name}", pod_name or "<pod>")
    if "{label_selector}" in v_args:
        v_args = v_args.replace("{label_selector}", label_selector or "")
    if "{debug_pod}" in v_args:
        v_args = v_args.replace("{debug_pod}", "<debug-pod>")
    return v_args


def _split_flags(flags_str: str) -> list[str]:
    """Split a flags string into ``--key value`` pairs."""
    if not flags_str:
        return []
    parts = flags_str.split()
    result = []
    i = 0
    while i < len(parts):
        if parts[i].startswith("--") and i + 1 < len(parts) and not parts[i + 1].startswith("--"):
            result.append(f"{parts[i]} {parts[i + 1]}")
            i += 2
        else:
            result.append(parts[i])
            i += 1
    return result
