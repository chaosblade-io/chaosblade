"""Result event construction for CLI / TUI inject streams."""

from __future__ import annotations

import json
import logging

from chaos_agent.agent.state import has_active_fault
from chaos_agent.agent.streaming import StreamEvent
from chaos_agent.models.schemas import build_inject_envelope

logger = logging.getLogger(__name__)


def _vehicle_teardown_hint(pending_vehicles: list[str], task_id: str) -> str:
    """CLI hint text for uncollected task-built vehicles (inject-dfee9d3d).

    Render layer only: the pending list itself is single-sourced in
    ``operation_result.pending_vehicle_teardown`` and rides every result
    envelope as ``vehicle_teardown_pending`` (R13-1) — this renderer reads
    the field, never re-derives it.
    """
    return (
        f"\nVehicle teardown outstanding ({len(pending_vehicles)}): "
        f"{', '.join(pending_vehicles)}. Run `blade-ai recover "
        f"--task-id {task_id}` to collect task-built carrier assets "
        "(idempotent)."
    )


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
    turn_tokens_seen: bool,
    interaction_mode: str,
    snapshot=None,
) -> tuple[list[StreamEvent], bool]:
    """Build result StreamEvents from final graph state values.

    Returns (events_to_yield, should_return_early).

    ``snapshot`` is the graph snapshot the caller already holds (the engine
    is the authority on pause — round-64 F3). Forwarded to
    ``build_inject_data_from_state`` so a stream that ``break``s at the
    confirmation gate (``confirm=True``, no callback) reports
    ``waiting_input`` instead of the fail-closed ``failed`` the values-only
    projection produced. The TUI conversation branch is untouched: its
    pause is the intent graph's, finalized on purpose (round-60 F4''').
    """
    if not values:
        return [StreamEvent(
            type="error",
            content="Graph completed but no state available",
            task_id=task_id,
        )], False

    if interaction_mode == "tui" and not has_active_fault(values):
        events: list[StreamEvent] = []
        from chaos_agent.agent.result.operation_outcome import read_operation_outcome
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

    from chaos_agent.agent.result.operation_result import build_inject_data_from_state
    result_data = build_inject_data_from_state(values, task_id, snapshot=snapshot)

    events: list[StreamEvent] = [StreamEvent(
        type="result",
        content=json.dumps(build_inject_envelope(
            result_data, result_data["task_state"], result_data.get("error", ""),
        ), ensure_ascii=False),
        task_id=task_id,
    )]
    # Task-end teardown hint (inject-dfee9d3d): reads the single-sourced
    # envelope field (R13-1) — the SSE / turn / persisted-JSON consumers
    # carry the same fact automatically via build_inject_data_from_state,
    # so every terminal surface answers this question identically.
    # Non-stream inject returns a plain dict without this builder: its
    # data dict still carries the field, and carrier-doc guidance covers
    # the non-interactive follow-up.
    pending_vehicles = result_data.get("vehicle_teardown_pending") or []
    if pending_vehicles:
        events.append(StreamEvent(
            type="token",
            content=_vehicle_teardown_hint(pending_vehicles, task_id),
            task_id=task_id,
        ))
    return events, False
