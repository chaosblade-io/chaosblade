"""CLI status event formatting and printing."""

from __future__ import annotations

import asyncio
import logging

from chaos_agent.config.settings import settings
from chaos_agent.observability.status_tracker import (
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
    duration = f" ({event.duration_ms:.0f}ms)" if event.duration_ms > 0 else ""

    if "\n" in event.message:
        header, rest = event.message.split("\n", 1)
        indented_rest = rest.replace("\n", "\n      ")
        line = f"  {color}{icon} [{event.source}] {header}{duration}{_RESET}\n      {indented_rest}"
    else:
        line = f"  {color}{icon} [{event.source}] {event.message}{duration}{_RESET}"

    stdout_preview = event.detail.get("stdout_preview", "")
    if stdout_preview:
        preview_text = stdout_preview[:200]
        if len(stdout_preview) > 200:
            preview_text += "..."
        indented_preview = preview_text.replace("\n", "\n      ")
        line += f"\n      → output: {indented_preview}"

    if settings.is_debug and event.detail.get("debug") and event.detail:
        import json
        detail = {k: v for k, v in event.detail.items() if k not in ("debug", "tool_calls", "stdout_preview")}
        if detail:
            detail_str = json.dumps(detail, ensure_ascii=False)
            line += f"\n    → detail: {detail_str}"

    return line


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
