"""CLI status event formatting and printing."""

from __future__ import annotations

import asyncio
import logging

from chaos_agent.config.settings import settings
from chaos_agent.observability.status_tracker import (
    elided_preview,
    StatusCategory,
    StatusEvent,
    StatusPhase,
)

logger = logging.getLogger(__name__)

_PHASE_COLORS = {
    StatusPhase.STARTED: "\033[36m",     # cyan
    StatusPhase.RUNNING: "\033[33m",     # yellow
    StatusPhase.COMPLETED: "\033[32m",   # green
    StatusPhase.FAILED: "\033[31m",      # red
}
_PHASE_ICONS = {
    StatusPhase.STARTED: "►",
    StatusPhase.RUNNING: "●",
    StatusPhase.COMPLETED: "✓",
    StatusPhase.FAILED: "✗",
}
_RESET = "\033[0m"


def format_status_event(event: StatusEvent) -> str:
    """Format a status event for CLI display.

    Visibility rules:
    - Non-debug mode: only show SYSTEM events (e.g., final results).
    - Debug mode: show all events (NODE, TOOL, LLM, SYSTEM) including
      tool output previews and LLM reasoning summaries.
    """
    if event.detail.get("debug") and not settings.is_debug:
        return ""

    if not settings.is_debug and event.category in (StatusCategory.NODE, StatusCategory.TOOL):
        return ""

    color = _PHASE_COLORS.get(event.phase, "")
    icon = _PHASE_ICONS.get(event.phase, "·")
    duration = _format_duration(event)

    if "\n" in event.message:
        header, rest = event.message.split("\n", 1)
        indented_rest = rest.replace("\n", "\n      ")
        line = f"  {color}{icon} [{event.source}] {header}{duration}{_RESET}\n      {indented_rest}"
    else:
        line = f"  {color}{icon} [{event.source}] {event.message}{duration}{_RESET}"

    stdout_preview = event.detail.get("stdout_preview", "")
    if stdout_preview:
        # Failure previews keep BOTH ends (elided_preview): the causal line
        # sits at different ends for different tools, and the producer may
        # itself have elided the middle — re-eliding the combined string
        # still preserves its two anchors (head of the head, tail of the
        # tail) while the marker makes every cut visible (#31: a head-only
        # cut kept the kubectl warning banner and hid "Error from server
        # (NotFound)"). Success previews keep the head.
        _failed = (
            event.phase == StatusPhase.FAILED
            or event.detail.get("exit_code") not in (None, 0)
        )
        if _failed:
            preview_text = elided_preview(stdout_preview, 100, 200)
        else:
            preview_text = stdout_preview[:200]
            if len(stdout_preview) > 200:
                preview_text += "..."
        indented_preview = preview_text.replace("\n", "\n      ")
        line += f"\n      → output: {indented_preview}"

    if settings.is_debug and event.detail.get("debug") and event.detail:
        import json
        detail = {
            k: v for k, v in event.detail.items()
            # ``segments`` is rendered inline by _format_duration; repeating
            # the same numbers as raw JSON is noise, not information.
            if k not in ("debug", "tool_calls", "stdout_preview", "segments")
        }
        if detail:
            detail_str = json.dumps(detail, ensure_ascii=False)
            line += f"\n    → detail: {detail_str}"

    return line


def _format_duration(event: StatusEvent) -> str:
    """Render elapsed time so the number states what it measures.

    ``duration_ms`` on a NODE event is the whole span from ``tracker.start``
    to this event, while the message beside it usually names one phase. A bare
    ``(71751ms)`` after ``Iteration 1 LLM response:`` therefore reads as
    "the model took 71.7s" — which is how inject-6ebf341c's turn 1 came to be
    audited as model latency when the model was only part of it. When the
    producer marked sub-spans, print them: the parts sum to the total (the
    tracker appends the un-instrumented remainder as ``other``), so the reader
    can see both the whole and where it went.
    """
    segments = event.detail.get("segments") or []
    if segments:
        parts = " ".join(
            f"{s.get('label')}={float(s.get('ms') or 0):.0f}ms" for s in segments
        )
        return f" (node {event.duration_ms:.0f}ms: {parts})"
    return f" ({event.duration_ms:.0f}ms)" if event.duration_ms > 0 else ""


async def _status_printer(queue: asyncio.Queue[StatusEvent], done_event: asyncio.Event):
    """Background task that reads status events and prints them to stderr."""
    while not done_event.is_set():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
            import sys
            formatted = format_status_event(event)
            if formatted:
                sys.stderr.write(formatted + "\n")
                sys.stderr.flush()
        except asyncio.TimeoutError:
            continue
        except Exception:
            break
