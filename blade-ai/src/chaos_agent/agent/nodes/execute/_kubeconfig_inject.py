"""Shared kubeconfig/kubewiz injection utility for execute_loop.

Provides:
- _resolve_kubeconfig: Multi-level fallback kubeconfig resolution from AgentState
- inject_kubeconfig_into_tool_calls: Programmatic kubeconfig injection into LLM tool calls
- _resolve_kubewiz_cluster_uuid / _resolve_kubewiz_profile: kubewiz param resolution
- resolve_transport_target: Construct TransportTarget from AgentState (replaces sync_kubewiz_runtime)
- sync_kubewiz_runtime: Deprecated — syncs per-session kubewiz params into settings
"""

import logging

from langchain_core.messages import AIMessage

from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.transports import TransportTarget, is_kubewiz_channel

logger = logging.getLogger(__name__)


def _resolve_kubeconfig(state: AgentState) -> str:
    """Resolve kubeconfig from state with multi-level fallback.

    Priority: state.kubeconfig > spec.params.kubeconfig > settings.kubeconfig_path
    """
    kc = state.get("kubeconfig", "")
    if kc:
        return kc
    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    spec = read_fault_spec(state)
    if spec:
        kc = spec.params.get("kubeconfig", "")
        if kc:
            return kc
    return settings.kubeconfig_path


def inject_kubeconfig_into_tool_calls(
    response: AIMessage,
    kubeconfig: str,
) -> None:
    """Inject kubeconfig into kubectl/blade tool calls that are missing it.

    This is a programmatic safety net: even if the LLM forgets to include
    kubeconfig in its tool call arguments, this function ensures it is present
    before the ToolNode dispatches the call.

    Mutates response.tool_calls in-place.

    Rules:
    - Only injects into tools whose name starts with "kubectl" or "blade"
    - Only injects when the existing kubeconfig arg is empty/falsy
    - Does NOT override if the LLM already set a kubeconfig value
    - Skips entirely when the provided kubeconfig is empty

    Args:
        response: The LLM's AIMessage response containing tool_calls.
        kubeconfig: The kubeconfig path to inject.
    """
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        return

    # Determine channel via TransportTarget — replaces settings.kube_connection_mode check.
    # kubewiz mode: connection info comes from settings, not kubeconfig param.
    # Clear any kubeconfig the LLM might have passed to avoid conflicts.
    if is_kubewiz_channel():
        for tc in tool_calls:
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            if args.get("kubeconfig"):
                if isinstance(tc, dict):
                    tc["args"]["kubeconfig"] = ""
                else:
                    try:
                        tc.args["kubeconfig"] = ""  # type: ignore[index]
                    except (TypeError, AttributeError):
                        pass
        return

    if not kubeconfig:
        return

    injected_count = 0
    for tc in tool_calls:
        # Handle both dict and namedtuple-style access
        if isinstance(tc, dict):
            name = tc.get("name", "")
            args = tc.get("args", {})
        else:
            name = getattr(tc, "name", "")
            args = getattr(tc, "args", {})

        # Only inject into kubectl/blade tools
        if not (name.startswith("kubectl") or name.startswith("blade")):
            continue

        # Only inject when kubeconfig is missing or empty
        if args.get("kubeconfig", ""):
            continue

        # Inject kubeconfig
        if isinstance(tc, dict):
            tc["args"]["kubeconfig"] = kubeconfig
        else:
            # namedtuple/object style — try dict conversion or attribute set
            if hasattr(tc, "__setitem__"):
                tc["args"]["kubeconfig"] = kubeconfig
            elif hasattr(tc, "args"):
                # ToolCall is a typed dict-like; create a new one
                # This path is unlikely with LangChain but handled defensively
                try:
                    tc.args["kubeconfig"] = kubeconfig  # type: ignore[index]
                except (TypeError, AttributeError):
                    logger.debug(f"Cannot inject kubeconfig into tool_call {name}: immutable args")
                    continue

        injected_count += 1
        logger.debug(f"Injected kubeconfig into tool_call '{name}'")

    if injected_count:
        logger.info(
            f"inject_kubeconfig_into_tool_calls: injected kubeconfig "
            f"into {injected_count} tool call(s)"
        )


_TASK_SCOPED_TOOLS = frozenset({"blade_create", "host_inject", "host_read"})


def inject_task_id_into_tool_calls(response: AIMessage, task_id: str) -> None:
    """Bind audit-bearing execution tools to the graph's current task.

    ``task_id`` is runtime identity, not an LLM planning parameter.  Leaving
    it model-controlled lets an otherwise valid tool call write audit events
    into an unrelated task record, as happened when the model supplied a
    descriptive plan id.  Only tools that actually accept this argument are
    touched; their supplied value is deliberately overwritten.
    """
    if not task_id:
        return

    for tool_call in getattr(response, "tool_calls", None) or []:
        if isinstance(tool_call, dict):
            name = tool_call.get("name", "")
            args = tool_call.get("args", {})
        else:
            name = getattr(tool_call, "name", "")
            args = getattr(tool_call, "args", {})
        if name not in _TASK_SCOPED_TOOLS or not isinstance(args, dict):
            continue
        args["task_id"] = task_id


def _resolve_kubewiz_cluster_uuid(state: AgentState) -> str:
    """Resolve kubewiz cluster UUID: state > settings."""
    uuid = state.get("kubewiz_cluster_uuid", "")
    if uuid:
        return uuid
    return settings.kubewiz_cluster_uuid


def _resolve_kubewiz_profile(state: AgentState) -> str:
    """Resolve kubewiz profile: state > settings."""
    profile = state.get("kubewiz_profile", "")
    if profile:
        return profile
    return settings.kubewiz_profile


def resolve_transport_target(state: AgentState) -> TransportTarget:
    """Construct a TransportTarget from AgentState.

    This is the preferred API for obtaining a TransportTarget — it reads
    per-session overrides from *state* and falls back to *settings* for
    any missing field.  Replaces the old ``sync_kubewiz_runtime`` which
    mutated global settings as a side effect.

    .. note::
        Not yet wired into all graph nodes — 10+ callers still use
        ``sync_kubewiz_runtime(state)`` which mutates settings as a side
        effect.  Once those callers are migrated to pass a TransportTarget
        directly to tools, ``sync_kubewiz_runtime`` can be removed.
    """
    return TransportTarget.from_state(state)


def sync_kubewiz_runtime(state: AgentState) -> None:
    """Sync per-session kubewiz params from state into settings.

    .. deprecated::
        Use ``resolve_transport_target(state)`` instead.  This function
        is retained because tools still call ``TransportTarget.from_state({})``
        which reads from *settings*; syncing ensures per-session values
        are visible.  Once all tools accept a TransportTarget parameter
        this function will be removed.

    Guard: only syncs when the resolved transport channel (from *settings*,
    not from state) is a kubewiz channel.  Checking settings — not state —
    prevents a stale ``kubewiz_cluster_uuid`` in state from accidentally
    switching the channel from kubeconfig to kubewiz in a session that
    was explicitly configured for kubeconfig mode.
    """
    if not is_kubewiz_channel():
        return
    uuid = _resolve_kubewiz_cluster_uuid(state)
    if uuid:
        settings.kubewiz_cluster_uuid = uuid
    profile = _resolve_kubewiz_profile(state)
    if profile:
        settings.kubewiz_profile = profile
