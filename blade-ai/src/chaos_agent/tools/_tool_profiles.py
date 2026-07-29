"""Declared capability profile per LLM-facing tool.

One table, read by the tools themselves when they call
``execute_via_transport(expect_profile=...)``. It exists because the tools layer
cannot import ``agent.providers`` (providers import tools — that would be
circular), yet the profile a tool needs is already declared once by its owning
provider's ``matches_channel``.

The two are kept from drifting by
``tests/test_transports/test_registry_completeness.py``, which asserts every
provider-owned tool appears here with exactly the profile its provider accepts.
Adding a tool to a provider without registering it here therefore fails in CI
rather than silently losing the profile gate at runtime.
"""

from __future__ import annotations

from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

# Tool name -> capability profile its commands require.
#
# ONLY tools whose owning provider accepts exactly ONE profile belong here. A
# provider that works on both (``ChaosbladeProvider``: "ChaosBlade operates on
# both cluster and bare-host targets" — ``blade create cpu load`` runs on a
# bare host just as well) has no single expected profile, so its tools must NOT
# be listed: doing so would REFUSE a legitimate host-channel blade injection.
#
# ``host_*`` run a bare shell on ONE machine → host-addressing channel required.
# ``kubectl*`` need cluster access → k8s channel required.
# The Python agent family is host-profile: ``blade`` can only reach an agent on
# its own machine, so the command must land on the application's host.
#
# ``blade_create`` / ``blade_destroy`` / ``blade_status`` / ``blade_help`` /
# ``blade_query_k8s`` are deliberately ABSENT — see above. Their safety comes
# from the capability gate (which resolves the fault scope's profile) plus
# ToolGuard, not from a transport profile assertion.
TOOL_PROFILE: dict[str, str] = {
    "host_read": PROFILE_HOST,
    "host_inject": PROFILE_HOST,
    "kubectl": PROFILE_K8S,
    "kubectl_read": PROFILE_K8S,
    "blade_python_create": PROFILE_HOST,
    "blade_python_prepare": PROFILE_HOST,
    "blade_python_revoke": PROFILE_HOST,
}


def profile_for_tool(tool_name: str) -> str:
    """Return the declared profile for *tool_name* (empty when unregistered).

    Empty means "no profile gate" — used by non-provider tools (file/knowledge
    readers, web search) that never touch a transport channel.
    """
    return TOOL_PROFILE.get(tool_name, "")


__all__ = ["TOOL_PROFILE", "profile_for_tool"]
