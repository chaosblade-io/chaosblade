"""Shared kubeconfig/kubewiz injection utility for execute_loop.

Provides:
- _resolve_kubeconfig: Multi-level fallback kubeconfig resolution from AgentState
  (round-32: canonical home moved to chaos_agent.agent.kubeconfig — the
  graph-wide single source the settlement primitives resolve through too;
  this private name stays as the execute-side alias for its callers)
- inject_kubeconfig_into_tool_calls: Programmatic kubeconfig injection into LLM tool calls
- resolve_transport_target: Construct TransportTarget from AgentState (re-export of the
  canonical ``chaos_agent.transports.registry`` seam — the single authority that
  replaced the retired ``sync_kubewiz_runtime`` settings side-effect)
"""

import logging

from langchain_core.messages import AIMessage

from chaos_agent.agent.state import AgentState
from chaos_agent.transports import is_kubewiz_channel
from chaos_agent.transports import (
    resolve_transport_target as resolve_transport_target,  # re-export (D2)
)

# ``resolve_transport_target`` (the single authoritative transport-target seam)
# canonically lives in ``chaos_agent.transports.registry`` so tools, graph nodes
# and the registry all resolve through one import with no layering inversion.
# It is re-exported here so existing execute-side callers keep their import
# path unchanged (ground-truth-single-authority D2).

logger = logging.getLogger(__name__)


def _resolve_kubeconfig(state: AgentState) -> str:
    """Resolve kubeconfig from state with multi-level fallback.

    Priority: state.kubeconfig > spec.params.kubeconfig > settings.kubeconfig_path

    Round-32: delegates to the canonical resolver
    :func:`chaos_agent.agent.kubeconfig.resolve_kubeconfig` (promoted out
    of this execute-private module so the settlement primitives — the
    registry's sweep — can share the exact contract without a layering
    inversion); this name remains so existing callers are untouched.
    """
    from chaos_agent.agent.kubeconfig import resolve_kubeconfig

    return resolve_kubeconfig(state)


def inject_kubeconfig_into_tool_calls(
    response: AIMessage,
    kubeconfig: str,
) -> None:
    """Inject kubeconfig into cluster-facing tool calls that are missing it.

    This is a programmatic safety net: even if the LLM forgets to include
    kubeconfig in its tool call arguments, this function ensures it is present
    before the ToolNode dispatches the call.

    Mutates response.tool_calls in-place.

    Rules:
    - Only injects into tools that declare a ``kubeconfig`` parameter (the
      provider-union ``kubeconfig_scoped_tool_names``)
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

    # Phase-7 T2: precise enumeration over the provider-declared union —
    # every tool whose signature declares ``kubeconfig`` — replacing the old
    # ``startswith("kubectl")/("blade")`` prefix match, which also swept in
    # tools that never take the parameter (blade_help, blade_python_*).
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    kubeconfig_scoped = FaultProviderRegistry.union_tool_names(
        "kubeconfig_scoped_tool_names"
    )

    injected_count = 0
    for tc in tool_calls:
        # Handle both dict and namedtuple-style access
        if isinstance(tc, dict):
            name = tc.get("name", "")
            args = tc.get("args", {})
        else:
            name = getattr(tc, "name", "")
            args = getattr(tc, "args", {})

        # Only inject into tools that declare a kubeconfig parameter
        if name not in kubeconfig_scoped:
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

    # Phase-7 T2: the binding set is the provider-declared union of tools
    # whose signature declares ``task_id``, replacing the hardcoded
    # ``_TASK_SCOPED_TOOLS`` literal — which had drifted: all three
    # blade_python_* tools declare ``task_id`` but were never bound.
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    audit_scoped = FaultProviderRegistry.union_tool_names("audit_scoped_tool_names")

    for tool_call in getattr(response, "tool_calls", None) or []:
        if isinstance(tool_call, dict):
            name = tool_call.get("name", "")
            args = tool_call.get("args", {})
        else:
            name = getattr(tool_call, "name", "")
            args = getattr(tool_call, "args", {})
        if name not in audit_scoped or not isinstance(args, dict):
            continue
        args["task_id"] = task_id

