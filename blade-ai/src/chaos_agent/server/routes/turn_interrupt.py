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


def _fmt_target(ns: str, names: list, *, max_shown: int = 4) -> str:
    """Render a target for the auto-approve token.

    - Drops the leading slash when the namespace is empty (node / cluster
      scope) so it reads ``node-a, node-b`` instead of ``/node-a, node-b``.
    - Summarizes long name lists as ``N 个: a, b, c …(+M)`` so a 40-node
      batch doesn't dump every name onto one line.
    """
    if not names:
        return f"{ns}/*" if ns else "*"
    total = len(names)
    if total > max_shown:
        body = f"{total}: {', '.join(names[:max_shown])} …(+{total - max_shown})"
    else:
        body = ", ".join(names)
    return f"{ns}/{body}" if ns else body


def format_auto_approve_info(node: str, payload: dict) -> str:
    """Format interrupt payload for auto-mode display (token, not card)."""
    lines = [f"[Auto-approved: {node}]"]

    if node == "confirmation_gate":
        fi = payload.get("fault_intent") or {}
        ft = fi.get("fault_type", "")
        if ft:
            lines.append(f"Fault: {ft}")
        target = payload.get("target") or {}
        ns = target.get("namespace", "")
        names = target.get("names", [])
        if ns or names:
            lines.append(f"Target: {_fmt_target(ns, names)}")
        params = payload.get("params") or {}
        if params:
            lines.append(f"Params: {', '.join(f'{k}={v}' for k, v in params.items() if v)}")
        safety = payload.get("safety_status", "")
        if safety:
            reason = payload.get("safety_checked_detail") or payload.get("safety_reason") or ""
            lines.append(f"Safety: {safety}" + (f" ({reason})" if reason else ""))
        health = payload.get("target_health_report") or {}
        if health:
            lines.append(f"Health: {health.get('overall', '?')} ({health.get('summary', '')})")
        feas = payload.get("feasibility_report") or {}
        if feas and feas.get("severity"):
            lines.append(f"Feasibility: {feas.get('severity')} ({feas.get('message', '')})")
        score = payload.get("safety_score") or {}
        if score:
            lines.append(f"Safety score: {score.get('overall', '?')}/100 ({score.get('level', '')})")
    elif node == "plan_change_confirm":
        reason = payload.get("reason", "")
        original = payload.get("original") or {}
        proposed = payload.get("proposed") or {}
        if original.get("fault_type"):
            lines.append(f"Original plan: {original['fault_type']}")
        if proposed.get("fault_type"):
            lines.append(f"New plan: {proposed['fault_type']}")
        if reason:
            lines.append(f"Reason: {reason}")
    elif node == "tool_screener":
        reason = payload.get("reason", "")
        agent_reason = payload.get("agent_reason", "")
        original = payload.get("original") or {}
        proposed = payload.get("proposed") or {}
        if original:
            ns = original.get("namespace", "")
            names = original.get("names", [])
            lines.append(f"Approved target: {_fmt_target(ns, names)}")
        if proposed:
            ns = proposed.get("namespace", "")
            names = proposed.get("names", [])
            lines.append(f"Actual target: {_fmt_target(ns, names)}")
        if reason:
            lines.append(f"Drift reason: {reason if len(reason) <= 160 else reason[:159] + '…'}")
        if agent_reason:
            lines.append(f"Agent explanation: {agent_reason}")
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
                # Render hours once the window is measured in them: the default
                # is 6h, and "360 min" reads like a bug rather than a setting.
                mins = int(abs(timeout) // 60)
                span = f"{mins // 60}h" if mins >= 120 else f"{mins} min"
                raise ConfirmTimeout(f"Confirmation timed out ({span})")
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
