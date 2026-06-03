"""Programmatic injection plan generator.

Produces a complete, human-readable fault injection plan in Markdown
from the structured state that agent_loop has populated. Used by
confirmation_gate's dry_run branch (/plan command).
"""

import re

from langchain_core.messages import AIMessage

from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec
from chaos_agent.agent.state import AgentState
from chaos_agent.utils.fault_type import build_blade_create_args, ensure_min_duration


def generate_injection_plan(state: AgentState) -> str:
    """从 agent_loop 填充后的 state 生成完整故障注入计划 markdown。"""
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
    return f"# 故障注入计划\n\n{body}\n\n---\n确认执行: `/run` | 调整: `/plan <修改建议>`"


def _section_target(spec: FaultSpec) -> str:
    lines = ["## 目标"]
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
        lines.append("- (目标信息未完整收集)")
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
    if kubeconfig:
        parts.append(f"  --kubeconfig {kubeconfig}")

    cmd_str = " \\\n".join(parts)
    return f"## 注入命令\n\n```bash\n{cmd_str}\n```"


def _section_baseline_preview(spec: FaultSpec) -> str:
    if not (spec.scope and spec.blade_target):
        return ""

    from chaos_agent.agent.nodes.baseline_capture import _lookup_baseline_commands

    commands = _lookup_baseline_commands(
        spec.scope, spec.blade_target, spec.blade_action
    )
    if not commands:
        return "## 基线采集\n\n注入前无预定义基线采集命令（将使用 LLM 动态策略）。"

    lines = ["## 基线采集（注入前自动执行）"]
    for i, cmd in enumerate(commands, 1):
        v_args = _resolve_baseline_template(cmd.v_args_template, spec)
        lines.append(f"{i}. `kubectl {cmd.subcommand} {v_args}` — {cmd.description}")
    return "\n".join(lines)


def _section_verification_strategy(state: AgentState) -> str:
    lines = [
        "## 验证策略",
        "",
        "**Layer 1（自动）**：`blade status <uid>` 确认实验状态为 Success",
    ]

    skill_case = state.get("skill_case_content") or ""
    l2_content = _extract_section(skill_case, "注入验证")
    if l2_content:
        lines.append("")
        lines.append("**Layer 2（观测验证）**：")
        for item in l2_content:
            lines.append(f"- {item}")
    else:
        lines.append("")
        lines.append("**Layer 2（观测验证）**：LLM 将基于基线对比自动验证注入效果")

    return "\n".join(lines)


def _section_recovery_strategy(state: AgentState) -> str:
    lines = [
        "## 恢复策略",
        "",
        "1. `blade destroy <uid>` — 销毁实验，移除故障注入",
    ]

    skill_case = state.get("skill_case_content") or ""
    recovery = _extract_section(skill_case, "注入恢复")
    if recovery:
        for i, item in enumerate(recovery, 2):
            lines.append(f"{i}. {item}")

    verify = _extract_section(skill_case, "恢复验证")
    if verify:
        lines.append("")
        lines.append("**恢复验证**：")
        for item in verify:
            lines.append(f"- {item}")

    return "\n".join(lines)


def _section_safety_assessment(state: AgentState) -> str:
    safety_status = state.get("safety_status", "pending")
    safety_reason = state.get("safety_reason") or ""
    health_report = state.get("target_health_report") or ""
    conflicts = state.get("conflict_uids") or []
    safety_score = state.get("safety_score") or {}

    lines = ["## 安全评估"]
    lines.append(f"- 状态: **{safety_status}**")
    if safety_reason:
        lines.append(f"- 说明: {safety_reason}")
    if conflicts:
        lines.append(f"- 冲突实验: {', '.join(conflicts)}")
    else:
        lines.append("- 冲突实验: 无")
    if health_report:
        lines.append(f"- 健康预检: {health_report}")

    # E10 — render multi-dimensional safety score when present.
    if safety_score:
        overall = safety_score.get("overall", 0)
        level = safety_score.get("level", "")
        lines.append(f"- 风险评分: **{overall}/100** ({level})")
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
        return f"## 时间\n\n- 注入持续: {timeout_str}"

    minutes = timeout_s // 60
    total_est = minutes + 2  # baseline + verification overhead
    return (
        f"## 时间\n\n"
        f"- 注入持续: {timeout_s}s ({minutes}min)\n"
        f"- 预计总耗时: ~{total_est}min（含基线采集 + 验证）"
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
            return f"## 规划推理\n\n{content.strip()}"
    return ""


# ── Helpers ──


def _extract_section(skill_case: str, section_name: str) -> list[str]:
    """从 skill_case_content 提取指定 section 的条目列表。"""
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
    """Resolve template variables in baseline v_args using FaultSpec data."""
    v_args = template
    namespace = spec.namespace
    names = list(spec.names)
    node_name = names[0] if names else ""
    pod_name = "" if spec.scope == "node" else (names[0] if names else "")
    label_selector = (
        ",".join(f"{k}={v}" for k, v in spec.labels.items())
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
    """将 flags 字符串拆分为 --key value 对。"""
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
