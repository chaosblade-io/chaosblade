"""EventBridge — converts Agent StreamEvents into TUIEvent dataclasses
and dispatches them to the Renderer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from chaos_agent.agent.streaming import StreamEvent
from chaos_agent.tui.events import (
    InterruptRequired,
    PhaseChanged,
    TaskError,
    TaskResult,
    ThinkingReceived,
    TokenReceived,
    ToolCompleted,
    ToolStarted,
    TUIEvent,
)

logger = logging.getLogger(__name__)


class EventBridge:
    """Converts StreamEvents into TUIEvents and forwards them to the Renderer.

    The renderer is the only stdout sink. Bridge is a pure mapping layer.
    """

    def __init__(self, renderer) -> None:
        self._renderer = renderer

    async def process_stream_event(self, event: StreamEvent) -> None:
        """Convert a StreamEvent and dispatch the matching TUIEvent."""
        tui_event = self._convert_stream_event(event)
        if tui_event is not None and self._renderer is not None:
            await self._renderer.dispatch(tui_event)

    def _convert_stream_event(self, event: StreamEvent) -> Optional[TUIEvent]:
        """Pure mapping: StreamEvent → TUIEvent (or None to drop)."""
        event_type = event.type

        if event_type == "token":
            return TokenReceived(content=event.content, node=event.node)

        elif event_type == "thinking":
            return ThinkingReceived(content=event.content, node=event.node)

        elif event_type == "tool_start":
            return ToolStarted(tool_name=event.tool_name, node=event.node)

        elif event_type == "tool_end":
            return ToolCompleted(
                tool_name=event.tool_name,
                content=event.content,
                node=event.node,
            )

        elif event_type == "confirm":
            return InterruptRequired(
                interrupt_info=_parse_interrupt_content(event.content),
                task_id=event.task_id,
            )

        elif event_type == "result":
            return TaskResult(
                data=_safe_json_parse(event.content) if event.content else {},
                task_id=event.task_id,
            )

        elif event_type == "error":
            return TaskError(message=event.content, task_id=event.task_id)

        elif event_type == "node_start":
            return PhaseChanged(
                phase=event.node,
                source=event.node,
                message=f"Starting {event.node}",
            )

        elif event_type == "node_end":
            return PhaseChanged(
                phase=event.node,
                source=event.node,
                message=f"Completed {event.node}",
            )

        return None

    async def consume_status(
        self, queue: asyncio.Queue, done: asyncio.Event
    ) -> None:
        """Consume StatusEvents from a queue and forward as PhaseChanged."""
        while not done.is_set():
            try:
                status_event = queue.get_nowait()
                tui_event = PhaseChanged(
                    phase=getattr(status_event, "phase", ""),
                    source=getattr(status_event, "source", ""),
                    message=getattr(status_event, "message", ""),
                )
                if self._renderer is not None:
                    await self._renderer.dispatch(tui_event)
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"Error consuming status event: {e}")


def _parse_interrupt_content(content) -> dict:
    """Parse interrupt content into a dict, falling back to confirmation."""
    if not content:
        return {"type": "confirmation"}

    parsed = _safe_json_parse(content)
    if isinstance(parsed, dict) and "type" in parsed:
        return parsed

    return {"type": "confirmation", "plan_summary": content}


def _safe_json_parse(content):
    try:
        import json
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
