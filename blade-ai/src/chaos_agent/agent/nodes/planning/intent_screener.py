"""Runtime guard for transport-specific read-only Intent discovery tools."""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.capabilities import screen_tool_calls, tool_call_field

INTENT_SCREENER_PASS = "pass"
INTENT_SCREENER_RETRY = "retry"

# NOTE: a ``plan_builder_screener`` used to live here, sharing a parameterised
# ``_screen_provider_tool_calls`` helper with this one. It was never wired into
# ``build_pipeline_graph``, so ``plan_builder_tools`` ran unscreened while a
# passing unit test suggested otherwise. That gap is now closed by
# ``nodes._capability_screen.with_capability_screen(..., "plan")``, which wraps
# the ToolNode itself and can therefore filter PER CALL instead of discarding the
# whole batch. With the second caller gone the helper was inlined here: its
# ``phase`` / ``discovery`` parameters had a combination (neither set) that
# silently refused every call, and speculative generality is what produced the
# unwired duplicate in the first place.


def intent_screener(state: dict) -> dict:
    """Reject stale/crafted provider calls that do not match current transport.

    Uses the DISCOVERY rule (transport only, provisional ``fault_spec.scope``
    deliberately ignored): intent must be able to recognise every registered
    fault family regardless of the connected environment, while its read-only
    probe still has to match the environment it inspects.

    Whole-batch: one disallowed call sends the turn back to intent_clarification.
    Kept as-is deliberately — the per-call form requires screening INSIDE the
    ToolNode (which reads the latest AIMessage, so appending rejections upstream
    cannot hide a call from it), and no cross-profile mixed batch has ever been
    observed in this phase.
    """
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"intent_screener_route": INTENT_SCREENER_PASS}

    # Verdict + fail-closed-on-exception come from ``capabilities`` — one
    # implementation shared with every other screener. The allowed half is
    # discarded on purpose: rejecting is whole-batch here (see above).
    _, rejected_calls = screen_tool_calls(last.tool_calls, state, discovery=True)
    if not rejected_calls:
        return {"intent_screener_route": INTENT_SCREENER_PASS}

    return {
        "messages": [
            ToolMessage(
                content=(
                    "Error: this read-only discovery tool is unavailable for the "
                    "current environment. Select a tool bound to the active "
                    "transport."
                ),
                name=tool_call_field(call, "name"),
                tool_call_id=tool_call_field(call, "id"),
                status="error",
            )
            for call in rejected_calls
        ],
        "intent_screener_route": INTENT_SCREENER_RETRY,
    }
