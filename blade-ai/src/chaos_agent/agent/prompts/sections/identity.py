"""Identity sections: role definition and environment info."""

# Stable keys for env_info — only these are included to avoid cache invalidation
_ENV_STABLE_KEYS = frozenset({
    "cluster_version", "node_count", "namespace", "platform",
    "blade_version", "k8s_available", "config_path",
})


def get_role_section() -> str:
    """Role definition section for planning (agent_loop).

    Tool-agnostic: no concrete tool names (ChaosBlade, kubectl) — only
    abstract terms (fault injection, mutations). Phase 1 is read-only
    (planning), so the role says "plan", not "execute/verify/recover".
    """
    return """You are a Chaos Engineering Agent for Kubernetes fault injection.

Safely plan fault injection experiments on K8s clusters.

### Hard Boundaries (see Safety Rules for full list)
- NO arbitrary mutations outside of skill-case-defined injection methods
- NO arbitrary shell on the host
- NO bypassing safety checks — if one fails, STOP and report"""


def get_executor_role_section() -> str:
    """Role definition section for execution (execute_loop).

    Execution-specific rules (stop after success, tool is ground truth)
    live in executor Core Principles and REMEMBER (U-shaped attention),
    NOT here — single-source principle.
    """
    return """You are a Chaos Engineering Fault Injector for Kubernetes.

You are in the EXECUTION PHASE — the plan has been approved.
You MUST call tools to inject the fault NOW. Do NOT just output text.
Tool errors are expected — they are how you discover the actual interface.

### Hard Boundaries (see Safety Rules for full list)
- NO arbitrary mutations outside of skill-case-defined injection methods
- NO arbitrary shell on the host
- NO bypassing safety checks — if one fails, STOP and report"""


def get_env_section(env_info: dict) -> str:
    """Generate environment info section.

    Only includes stable cluster metadata to avoid cache invalidation
    (borrowed from OpenCLAW's dynamic clock removal pattern).

    Args:
        env_info: Dict of environment key-value pairs.

    Returns:
        Formatted environment section string.
    """
    filtered = {k: v for k, v in env_info.items() if k in _ENV_STABLE_KEYS}
    lines = ["## Environment"]
    for k, v in filtered.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)
