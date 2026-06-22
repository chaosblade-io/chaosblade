"""Shared helpers for LLM loop nodes (agent_loop, execute_loop, verifier, etc.).

Extracted to centralize stagnation-related logic: hint construction and
tool filtering.  Each node still owns its own control flow (prompt
building, convergence hints, bind_tools conditions).
"""

from chaos_agent.agent.nodes.react_helpers import summarize_llm_response
from chaos_agent.config.settings import settings


def filter_stagnant_tool(
    tools: list | None,
    stagnant_tool: str | None,
    *,
    preserve: set[str] | None = None,
) -> list:
    """Remove a stagnant tool from the tool list.

    Only removes tool-level stagnation (full tool name match).
    Subcommand-level stagnation (``":" in stagnant_tool``) does NOT
    remove the tool — the node's hint tells the LLM to use other
    subcommands instead.

    ``preserve`` keeps named tools even when they match, used by
    intent_clarification to keep ``submit_fault_intent`` available.
    """
    result = list(tools) if tools else []
    if not stagnant_tool or ":" in stagnant_tool:
        return result
    return [
        t for t in result
        if getattr(t, "name", "") != stagnant_tool
        or (preserve and getattr(t, "name", "") in preserve)
    ]


def build_stagnation_hint(
    stagnant_tool: str,
    *,
    colon_suffix: str = "",
    else_actions: list[str] | None = None,
) -> str:
    """Build an ACTION_STAGNATION hint for a stagnant tool.

    Parameters
    ----------
    stagnant_tool : str
        Tool name, possibly with ``:subcommand`` suffix.
    colon_suffix : str
        Extra text after "with OTHER subcommands" for subcommand-level
        stagnation, e.g. ``"(patch, delete, scale, etc.) to complete
        remaining injection steps"``.
    else_actions : list[str] | None
        Bullet-point alternatives for full tool-level stagnation.
        Falls back to a generic "Use a DIFFERENT tool" if omitted.
    """
    if ":" in stagnant_tool:
        base_tool = stagnant_tool.split(":")[0]
        suffix = f" {colon_suffix}" if colon_suffix else ""
        return (
            f"**ACTION_STAGNATION**: You have called `{stagnant_tool}` "
            f"multiple consecutive times with no progress. "
            f"Stop using this subcommand. You can still use `{base_tool}` "
            f"with OTHER subcommands{suffix}.\n"
            f"Do NOT call `{stagnant_tool}` again."
        )
    if not else_actions:
        else_actions = ["Use a DIFFERENT tool to proceed."]
    actions_str = "\n".join(f"- {a}" for a in else_actions)
    return (
        f"**ACTION_STAGNATION**: You have called `{stagnant_tool}` "
        f"multiple consecutive times with no progress. "
        f"This tool has been temporarily removed. You MUST now either:\n"
        f"{actions_str}\n"
        f"Do NOT attempt to call `{stagnant_tool}` again."
    )


def post_invoke_debug(
    tracker,
    response,
    count: int,
    label: str,
) -> None:
    """Emit debug-level LLM response summary to the progress tracker."""
    if not settings.is_debug:
        return
    debug_info, tool_names = summarize_llm_response(response)
    tracker.update(
        f"{label} {count} LLM:\n{debug_info}",
        {"debug": True, "iteration": count, "tool_calls": tool_names},
    )
