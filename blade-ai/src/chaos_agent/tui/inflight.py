"""In-flight tracker — event-driven view of "what's running right now".

The thinking spinner used to randomly sample a verb every 10 seconds,
which felt alive but wasn't *truthful* — it'd say "比对工具" mid-paint
even if no tool had been called yet. This module flips the relationship:
the spinner asks the tracker what's actually happening and renders that.

Three states cover the reality of a chaos-agent turn:

  - **thinking**   — LLM is producing reasoning tokens, no tools running
  - **streaming**  — LLM is producing the final answer
  - **tool(s)**    — one or more tool calls are in flight (counter > 0)

Tools today are sequential, so the counter pegs at 0 or 1; PR-E7 will
introduce parallel tool calls, at which point the counter becomes load-
bearing for the multi-channel view.
"""

from __future__ import annotations

from typing import Optional

from chaos_agent.tui.events import (
    PhaseChanged,
    TaskError,
    TaskResult,
    ThinkingReceived,
    TokenReceived,
    ToolCompleted,
    ToolStarted,
    TUIEvent,
)


class InFlightTracker:
    """Maintain a small state machine driven by ``Renderer.dispatch``.

    Wire ``tracker.on_event(ev)`` into the dispatch path BEFORE the
    actual rendering — readers see a consistent picture by the time the
    paint happens. The tracker holds no console reference; it's a pure
    data structure.
    """

    def __init__(self) -> None:
        self._tool_count: int = 0
        self._current_tool: str = ""
        self._current_phase: str = ""
        self._is_streaming: bool = False
        self._is_thinking: bool = False

    # -- Mutation ---------------------------------------------------------

    def on_event(self, event: TUIEvent) -> None:
        """Fold a single TUIEvent into the tracker state."""
        if isinstance(event, ToolStarted):
            self._tool_count += 1
            self._current_tool = event.tool_name
            # A tool starting means the LLM stopped streaming/thinking
            # and yielded to a function call — clear those flags.
            self._is_streaming = False
            self._is_thinking = False
        elif isinstance(event, ToolCompleted):
            if self._tool_count > 0:
                self._tool_count -= 1
            if self._tool_count == 0:
                self._current_tool = ""
        elif isinstance(event, TokenReceived):
            self._is_streaming = True
            self._is_thinking = False
        elif isinstance(event, ThinkingReceived):
            self._is_thinking = True
            self._is_streaming = False
        elif isinstance(event, PhaseChanged):
            # Only "Starting <node>" updates the phase; the corresponding
            # "Completed" doesn't, so the label survives until the next
            # phase begins. Matches the stepper's convention.
            if "Starting" in (event.message or ""):
                self._current_phase = event.source
        elif isinstance(event, (TaskResult, TaskError)):
            self.reset()

    def reset(self) -> None:
        self._tool_count = 0
        self._current_tool = ""
        self._current_phase = ""
        self._is_streaming = False
        self._is_thinking = False

    # -- Read-only view ---------------------------------------------------

    @property
    def tool_count(self) -> int:
        return self._tool_count

    @property
    def current_tool(self) -> str:
        return self._current_tool

    @property
    def current_phase(self) -> str:
        return self._current_phase

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @property
    def is_thinking(self) -> bool:
        return self._is_thinking

    def verb_hint(self) -> Optional[str]:
        """Suggest a thinking-spinner verb that reflects current state.

        Returns ``None`` when nothing concrete is in flight, in which
        case the caller falls back to its existing random verb pool —
        the truthful answer to "what are you doing?" is then *literally*
        "thinking", and a random verb fits that mood.
        """
        if self._tool_count > 1:
            return f"\u5e76\u53d1\u8c03\u7528 {self._tool_count} \u9879\u5de5\u5177"  # 并发调用 N 项工具
        if self._tool_count == 1:
            tool = self._current_tool or "\u5de5\u5177"  # 工具
            return f"\u8c03\u7528 {tool}"  # 调用 <tool>
        if self._is_streaming:
            return "\u751f\u6210\u56de\u590d"  # 生成回复
        return None
