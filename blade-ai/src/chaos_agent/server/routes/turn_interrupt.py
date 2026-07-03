"""Interrupt extraction and confirmation helpers for the /turn endpoint."""

from __future__ import annotations

import asyncio
import json

_CONFIRM_KEEPALIVE_INTERVAL_S = 25
_KEEPALIVE_FRAME = ": keepalive\n\n"


def extract_pending_interrupt(graph_state) -> tuple[str, dict] | None:
    """Pull the first unresolved interrupt from a paused graph state.

    Returns ``(node_name, payload_dict)`` or ``None``.
    """
    if not graph_state or not graph_state.tasks:
        return None
    for task in graph_state.tasks:
        interrupts = getattr(task, "interrupts", None) or ()
        for it in interrupts:
            value = getattr(it, "value", None)
            if value is None:
                continue
            node = getattr(task, "name", "") or ""
            if isinstance(value, dict):
                return (node, value)
            return (node, {"value": value})
    return None


def content_from_interrupt_payload(payload: dict) -> str:
    """Pick a human-readable string for the ``content`` field of a confirm event."""
    return (
        payload.get("summary")
        or payload.get("plan_summary")
        or payload.get("question")
        or json.dumps(payload, ensure_ascii=False, indent=2)
    )


def format_auto_approve_info(node: str, payload: dict) -> str:
    """Format interrupt payload for auto-mode display (token, not card)."""
    lines = [f"[Auto-approved: {node}]"]

    if node == "confirmation_gate":
        fi = payload.get("fault_intent") or {}
        ft = fi.get("fault_type", "")
        if ft:
            lines.append(f"故障: {ft}")
        target = payload.get("target") or {}
        ns = target.get("namespace", "")
        names = target.get("names", [])
        if ns or names:
            lines.append(f"目标: {ns}/{', '.join(names) if names else '*'}")
        params = payload.get("params") or {}
        if params:
            lines.append(f"参数: {', '.join(f'{k}={v}' for k, v in params.items() if v)}")
        safety = payload.get("safety_status", "")
        if safety:
            reason = payload.get("safety_checked_detail") or payload.get("safety_reason") or ""
            lines.append(f"安全: {safety}" + (f" ({reason})" if reason else ""))
        health = payload.get("target_health_report") or {}
        if health:
            lines.append(f"健康: {health.get('overall', '?')} ({health.get('summary', '')})")
        feas = payload.get("feasibility_report") or {}
        if feas and feas.get("severity"):
            lines.append(f"可行性: {feas.get('severity')} ({feas.get('message', '')})")
        score = payload.get("safety_score") or {}
        if score:
            lines.append(f"安全评分: {score.get('overall', '?')}/100 ({score.get('level', '')})")
    elif node == "plan_change_confirm":
        reason = payload.get("reason", "")
        original = payload.get("original") or {}
        proposed = payload.get("proposed") or {}
        if original.get("fault_type"):
            lines.append(f"原方案: {original['fault_type']}")
        if proposed.get("fault_type"):
            lines.append(f"新方案: {proposed['fault_type']}")
        if reason:
            lines.append(f"原因: {reason}")
    elif node == "tool_screener":
        reason = payload.get("reason", "")
        agent_reason = payload.get("agent_reason", "")
        original = payload.get("original") or {}
        proposed = payload.get("proposed") or {}
        if original:
            ns = original.get("namespace", "")
            names = original.get("names", [])
            lines.append(f"批准目标: {ns}/{', '.join(names) if names else '*'}")
        if proposed:
            ns = proposed.get("namespace", "")
            names = proposed.get("names", [])
            lines.append(f"实际目标: {ns}/{', '.join(names) if names else '*'}")
        if reason:
            lines.append(f"偏移原因: {reason}")
        if agent_reason:
            lines.append(f"Agent 解释: {agent_reason}")
    else:
        content = content_from_interrupt_payload(payload)
        if content:
            lines.append(content)

    return "\n".join(lines)


def normalise_answer(answer: str) -> str:
    """Normalise a free-text confirmation answer to ``"approved"``/``"rejected"``."""
    return (
        "approved"
        if answer.strip().lower() in ("approved", "yes", "y", "ok")
        else "rejected"
    )


class ConfirmTimeout(Exception):
    """Raised when confirmation wait exceeds the deadline."""


async def wait_for_confirmation(
    store,
    turn_id: str,
    timeout: float,
    keepalive_interval: float = _CONFIRM_KEEPALIVE_INTERVAL_S,
):
    """Async generator: yields keepalive SSE comments in real-time,
    then yields the user answer as the final frame.

    Yields:
        str: Either _KEEPALIVE_FRAME (SSE comment) or the user's answer (final).
    Raises:
        ConfirmTimeout: if deadline expires before user responds.
    """
    fut = store.register_interrupt(turn_id)
    deadline = asyncio.get_event_loop().time() + timeout
    try:
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ConfirmTimeout(
                    f"Confirmation timed out ({int(timeout // 60)} min)"
                )
            slice_s = min(keepalive_interval, remaining)
            try:
                answer = await asyncio.wait_for(
                    asyncio.shield(fut), timeout=slice_s,
                )
                yield answer  # final frame: user's answer
                return
            except asyncio.TimeoutError:
                yield _KEEPALIVE_FRAME  # real-time keepalive
    finally:
        # Unified cleanup: timeout / disconnect / normal completion
        if not fut.done():
            store.cancel_interrupt(turn_id)
