"""Runtime capability screening for static ToolNodes.

``bind_tools`` is not an enforcement boundary. A restored checkpoint can carry
tool calls produced under a different capability context, and a model that has
seen a tool name in the conversation can emit a call for it even when the tool
was not bound this turn — the ToolNode will happily execute it.

The planning and execute phases already screen at runtime
(``phase1_screener`` / ``tool_screener`` / ``intent_screener``). Three ToolNodes
had no equivalent — ``verifier_tools``, ``recover_verifier_tools`` and
``plan_builder_tools`` — which is how task-46317228 ended up running
``host_read`` during verification on a k8s-profile session.

This wrapper closes them without duplicating the heavyweight target-drift
screeners: it only answers "may this tool run in this environment?", filters the
disallowed calls out of the batch, and returns a ``ToolMessage`` rejection for
each so the model learns why.

Filtering (rather than rejecting the whole batch) is deliberate: the accident's
verification turn called ``kubectl_read`` AND ``host_read`` together, and
failing the legitimate call alongside the bad one would burn a round for no
reason.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from chaos_agent.agent.capabilities import screen_tool_calls, tool_call_field

logger = logging.getLogger(__name__)

# LangGraph decides whether to INJECT ``config`` by comparing the parameter's
# annotation object against ``RunnableConfig`` / ``RunnableConfig | None``. Under
# ``from __future__ import annotations`` the annotation is the *string*
# ``"RunnableConfig | None"``, which never matches: it warns and then silently
# stops passing config — the wrapped ToolNode would run with ``config=None`` and
# lose callback/tracing propagation that it used to get as a direct graph node.
# Setting the annotation explicitly keeps the real object regardless of the
# module's postponed-evaluation setting.
_CONFIG_ANNOTATION = RunnableConfig | None


def with_capability_screen(tool_node: Any, phase: str) -> Callable:
    """Wrap *tool_node* so cross-profile tool calls never reach it.

    Args:
        tool_node: the ``ToolNode`` to protect.
        phase: capability phase key — must be registered in
            ``capabilities.context._PHASE_TO_PROVIDER_PHASE`` (an unregistered
            phase now fails closed, which would block every call).
    """

    # ``config`` must carry a real ``RunnableConfig | None`` annotation object,
    # see ``_CONFIG_ANNOTATION`` — it is re-attached after the definition.
    async def _screened(state: dict, config: RunnableConfig | None = None) -> dict:
        messages = list(state.get("messages") or [])
        last = messages[-1] if messages else None
        calls = list(getattr(last, "tool_calls", None) or [])
        if not calls:
            return await tool_node.ainvoke(state, config)

        # Verdict + fail-closed-on-exception live in ``capabilities``; every
        # screener shares that one implementation.
        allowed, rejected = screen_tool_calls(calls, state, phase)

        if not rejected:
            return await tool_node.ainvoke(state, config)

        for call in rejected:
            logger.warning(
                "capability screen (%s): refused tool %r for this environment",
                phase, tool_call_field(call, "name"),
            )
        # When NOTHING survived, the refusal is systematic (the whole tool
        # surface is wrong for this environment), not a mis-picked tool. Some
        # loops around a screened ToolNode have no iteration bound
        # (``should_continue_plan_builder``), so a model that keeps retrying the
        # same call would spin until the graph recursion limit. Say explicitly
        # that retrying cannot work — the same technique
        # ``_phase1_handle_tool_error`` uses to end a refused Phase-1 turn.
        tail = (
            ""
            if allowed
            else (
                " No tool from this domain is available in the current "
                "environment, so retrying or substituting another one will be "
                "refused as well. Stop calling tools and reply with your "
                "conclusion as plain text."
            )
        )
        refusals = [
            ToolMessage(
                content=(
                    f"Error: {tool_call_field(call, 'name')} is not available for "
                    f"the current environment capability profile. Use only the "
                    f"tools bound for this environment — a tool from another "
                    f"execution domain cannot reach this target.{tail}"
                ),
                tool_call_id=tool_call_field(call, "id"),
                name=tool_call_field(call, "name"),
                # Mark it as an error like ``handle_tool_errors`` does: routers
                # skip error ToolMessages when deciding whether a control tool
                # ran, and an unmarked refusal would be read as a successful
                # result (``router.route_after_verifier_tools``).
                status="error",
            )
            for call in rejected
        ]

        if not allowed:
            return {"messages": refusals}

        # Hand the ToolNode a message carrying ONLY the surviving calls, so the
        # refused ones are never dispatched. The original message stays intact
        # in history; this copy exists solely for dispatch and never enters
        # state (we return the ToolNode's output plus our refusals).
        #
        # ``model_copy`` rather than a hand-built AIMessage: reconstructing the
        # message would silently drop ``additional_kwargs`` /
        # ``response_metadata`` / ``usage_metadata``. ToolNode only reads
        # ``tool_calls`` today, but copying is the version that cannot rot.
        try:
            dispatch_msg = last.model_copy(update={"tool_calls": allowed})
        except Exception:  # non-pydantic message object
            dispatch_msg = AIMessage(
                content=getattr(last, "content", "") or "",
                tool_calls=allowed,
                id=getattr(last, "id", None),
            )
        result = await tool_node.ainvoke(
            {**state, "messages": messages[:-1] + [dispatch_msg]}, config
        )
        if isinstance(result, dict):
            # MERGE rather than rebuild: a ToolNode may return state keys other
            # than ``messages`` (a tool emitting a Command / state update), and
            # returning only ``messages`` would drop them silently.
            merged = dict(result)
            merged["messages"] = list(result.get("messages") or []) + refusals
            return merged
        # Not the documented shape. Returning only the refusals would leave the
        # ALLOWED calls unanswered, which the next model request rejects — so
        # make the surprise loud instead of debugging it from an API error.
        logger.error(
            "capability screen (%s): ToolNode returned %s, not a state dict — "
            "the allowed calls' results cannot be merged with the refusals",
            phase, type(result).__name__,
        )
        return {"messages": refusals}

    _screened.__annotations__["config"] = _CONFIG_ANNOTATION
    return _screened


__all__ = ["with_capability_screen"]
