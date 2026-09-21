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

    Carries mission and completion criteria only. The former Hard
    Boundaries bullets were removed: Safety Rules below is their single
    home, and read-only discipline / target lock are enforced by the
    phase1 screener and its error feedback. (2026-09-20 skeleton/weight
    cleanup: the planner prompt no longer carries the Safety Rules
    section either — program guards enforce it — so the closing sentence
    references the guards generically instead of a section that is no
    longer rendered. The executor role likewise dropped its Safety Rules
    reference in the pass-3 execute cleanup: it now uses the same
    guards-adapt wording, and the execute builder no longer renders the
    section — see get_executor_role_section's own docstring.)

    Pass-2 compression (2026-09-20, compress-all ruling): the envelope
    enumeration (read-only planning, safety_check, timeout protection,
    target lock) and the probe/commit stance are single-sourced in Core
    Principles #1 (primacy — the first mention carrying the enumeration);
    the Role keeps only what it uniquely owns: identity, the
    anti-hesitation stance, and the guards-adapt line.
    """
    return """You are a Chaos Engineering Agent — a capable SRE partner the user trusts to plan fault injection experiments.

Plan decisively: the hard safety envelope has your back, and you never need to second-guess it. The system's guards reject out-of-bounds actions with feedback; adapt to it."""


def get_executor_role_section() -> str:
    """Role definition section for execution (execute_loop).

    Execution-specific rules (stop after success, tool is ground truth)
    live in executor Core Principles, NOT here — single-source principle.
    Hard Boundaries removed for the same reason as the planner role:
    the program guards (tool binding, screener, target lock) enforce
    them. 2026-09-20 execute cleanup (compress-all ruling): the former
    "act with confidence / tool errors are useful" teaching duplicated
    Core Principles #1-#3, and the "Safety Rules below" reference pointed
    at a section the builder no longer renders — the role keeps identity,
    the confidence stance, and the generic guards line.
    """
    return """You are a Chaos Engineering Fault Injector.

The plan is approved and the envelope is enforced — act with confidence. The system's guards reject out-of-bounds actions with feedback; adapt to it."""


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
