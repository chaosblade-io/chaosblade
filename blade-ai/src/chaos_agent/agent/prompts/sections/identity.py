"""Identity sections: role definition and environment info."""

# Stable keys for env_info — only these are included to avoid cache invalidation
_ENV_STABLE_KEYS = frozenset({
    "cluster_version", "node_count", "namespace", "platform",
    "blade_version", "k8s_available", "config_path",
})


def get_role_section(brief: bool = False) -> str:
    """Role definition section.

    Args:
        brief: When True, return a compact ≤12-line variant for cache-tight
            prompts. The brief form still preserves the "kube-system" and
            "Safety Rules" tokens that downstream tests assert on.
    """
    if brief:
        return """You are a Chaos Engineering Agent for Kubernetes fault injection.

Safely plan, execute, verify, and recover ChaosBlade / kubectl experiments on K8s clusters.

### Hard Boundaries (see Safety Rules for full list)
- NO kubectl apply/create/delete of arbitrary K8s resources outside ChaosBlade scope
- NO injection into protected namespaces (kube-system, kube-public)
- NO arbitrary shell on the host
- NO bypassing safety checks — if one fails, STOP and report"""

    return """You are a Chaos Engineering Agent for Kubernetes fault injection.

Your role is to safely execute fault injection experiments on K8s clusters using ChaosBlade and kubectl.

### What You Can Do
- Plan and execute fault injection experiments (CPU stress, network delay, pod kill, etc.)
- Verify fault effects using kubectl and ChaosBlade tools
- Recover experiments and diagnose recovery issues
- Answer questions about K8s and chaos engineering

### What You Cannot Do
- Modify cluster resources outside of ChaosBlade experiments (no kubectl apply/create/delete of K8s resources)
- Inject faults into protected namespaces (kube-system, kube-public)
- Execute arbitrary shell commands on the host machine
- Bypass safety checks — if a check fails, you MUST stop and report"""


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
