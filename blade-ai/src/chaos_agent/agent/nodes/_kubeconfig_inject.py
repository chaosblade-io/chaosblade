"""Shared kubeconfig injection utility for execute_loop.

Provides:
- _resolve_kubeconfig: Multi-level fallback kubeconfig resolution from AgentState
- inject_kubeconfig_into_tool_calls: Programmatic kubeconfig injection into LLM tool calls
"""

import logging

from langchain_core.messages import AIMessage

from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)


def _resolve_kubeconfig(state: AgentState) -> str:
    """Resolve kubeconfig from state with multi-level fallback.

    Priority: state.kubeconfig > state.params.kubeconfig > settings.kubeconfig_path
    """
    kc = state.get("kubeconfig", "")
    if kc:
        return kc
    params = state.get("params") or {}
    kc = params.get("kubeconfig", "")
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
    if not kubeconfig:
        return

    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
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
