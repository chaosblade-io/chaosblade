"""Result event construction for CLI / TUI inject streams."""

from __future__ import annotations

import json
import logging

from chaos_agent.agent.streaming import StreamEvent
from chaos_agent.models.schemas import build_inject_envelope

logger = logging.getLogger(__name__)


def _extract_visible_reply(values: dict) -> str:
    """Pick a user-visible reply from the latest AIMessage in graph state.

    Used to recover from LLM backends that emit the answer only into
    reasoning_content during streaming (e.g. qwen enable_thinking),
    leaving the user without any token events for this turn.
    """
    if not isinstance(values, dict):
        return ""
    messages = values.get("messages") or []
    for msg in reversed(messages):
        msg_type = getattr(msg, "type", "")
        if msg_type != "ai":
            continue
        content = getattr(msg, "content", "") or ""
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
            content = "".join(parts)
        if isinstance(content, str) and content.strip():
            return content
    return ""


def _build_inject_result_events(
    values: dict | None,
    task_id: str,
    kwargs: dict,
    target_names: list[str],
    turn_tokens_seen: bool,
    interaction_mode: str,
) -> tuple[list[StreamEvent], bool]:
    """Build result StreamEvents from final graph state values.

    Returns (events_to_yield, should_return_early).
    """
    if not values:
        return [StreamEvent(
            type="error",
            content="Graph completed but no state available",
            task_id=task_id,
        )], False

    blade_uid = values.get("blade_uid", "")

    if interaction_mode == "tui" and not blade_uid:
        events: list[StreamEvent] = []
        from chaos_agent.agent.operation_outcome import read_operation_outcome
        error_msg = read_operation_outcome(values).error
        safety_rejected = values.get("safety_status") == "rejected"

        if error_msg or safety_rejected:
            events.append(StreamEvent(
                type="error",
                content=error_msg or values.get("safety_reason") or "Request rejected",
                task_id=task_id,
            ))
        if not turn_tokens_seen:
            synthetic = _extract_visible_reply(values)
            if synthetic:
                events.append(StreamEvent(
                    type="token",
                    content=synthetic,
                    task_id=task_id,
                ))
        events.append(StreamEvent(
            type="conversation_turn",
            content="",
            task_id=task_id,
        ))
        return events, True

    from chaos_agent.server.routes.turn_result import build_inject_data_from_state
    result_data = build_inject_data_from_state(values, task_id)

    return [StreamEvent(
        type="result",
        content=json.dumps(build_inject_envelope(
            result_data, result_data["task_state"], result_data.get("error", ""),
        ), ensure_ascii=False),
        task_id=task_id,
    )], False
