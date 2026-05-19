"""Renderer — dispatches TUIEvent dataclasses to render functions.

A single Renderer owns the ChaosConsole, StreamingPrinter, and a registry
of phase/tool/etc. renderers. Bridge calls `renderer.dispatch(event)`;
the renderer routes to the matching function.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.events import (
    InterruptRequired,
    PhaseChanged,
    TUIEvent,
    TaskError,
    TaskResult,
    ThinkingReceived,
    TokenReceived,
    ToolCompleted,
    ToolStarted,
)
from chaos_agent.tui.inflight import InFlightTracker
from chaos_agent.tui.live_coordinator import LiveCoordinator
from chaos_agent.tui.recording import EventRecorder
from chaos_agent.tui.renderers import interrupted_tasks, messages, result, tool_panel
from chaos_agent.tui.renderers.phase_timeline import PhaseTimelineRenderer, phase_for_node
from chaos_agent.tui.streaming import StreamingPrinter, ThinkingPrinter

logger = logging.getLogger(__name__)

# Type for an async interrupt resolver injected by the app
InterruptCallback = Callable[[InterruptRequired], Awaitable[None]]


class Renderer:
    """Central renderer that dispatches TUIEvents to the console."""

    def __init__(
        self,
        console: ChaosConsole,
        state=None,
        recorder: Optional[EventRecorder] = None,
    ) -> None:
        self.console = console
        # PR-E5 — single tracker shared by ThinkingPrinter (verb hint) and
        # any future footer / multi-channel surface. Construct first so we
        # can hand it to the printers.
        self.inflight = InFlightTracker()
        # PR-E2 — single Live region shared across token / thinking / tool /
        # timeline owners. All four are now wired through the coordinator:
        # the three region owners (token / thinking / tool) take turns in
        # the body slot via ``acquire``/``release``; phase-timeline owns
        # the header slot via ``acquire_header`` so the stepper coexists
        # with whatever body is painting (composite ``Group(header, region)``).
        self.live_coordinator = LiveCoordinator(console)
        self.streamer = StreamingPrinter(console, coordinator=self.live_coordinator)
        self.thinking = ThinkingPrinter(
            console, inflight=self.inflight, coordinator=self.live_coordinator
        )
        self.tool_panel = tool_panel.ToolPanelRenderer(
            console, state=state, coordinator=self.live_coordinator
        )
        self.phase_timeline = PhaseTimelineRenderer(
            console, coordinator=self.live_coordinator
        )
        self._interrupt_cb: Optional[InterruptCallback] = None
        self._task_done_cb: Optional[Callable[[str], None]] = None
        # PR-D5 — held for terminal renderers (result, etc.) that need to
        # honour the active density mode. Optional so the existing test
        # suite (which constructs ``Renderer(console)`` without state) keeps
        # working — falls back to ``DisplayMode.WORKING``.
        self._state = state
        # PR-E1 — passive event tape. None when recording is disabled or
        # the host didn't wire one in (legacy tests / headless paths).
        self._recorder = recorder

    def set_interrupt_handler(self, cb: InterruptCallback) -> None:
        self._interrupt_cb = cb

    def set_task_done_handler(self, cb: Callable[[str], None]) -> None:
        """Called after TaskResult/TaskError so the app can clear injection state."""
        self._task_done_cb = cb

    async def dispatch(self, event: TUIEvent) -> None:
        """Render a single TUIEvent."""
        # Tape every dispatched event before painting so even renderers
        # that crash leave behind a record of what they were given.
        # Recording is best-effort and never raises into the dispatch.
        if self._recorder is not None:
            try:
                self._recorder.record(event)
            except Exception:
                logger.exception("EventRecorder.record raised — disabling recorder")
                try:
                    self._recorder.disable()
                except Exception:
                    pass

        # Update the in-flight tracker before painting so the next
        # ThinkingPrinter render sees the new state. Pure data update;
        # cannot raise unless someone passes a non-TUIEvent.
        try:
            self.inflight.on_event(event)
        except Exception:
            logger.exception("InFlightTracker.on_event raised")

        try:
            if isinstance(event, TokenReceived):
                # PR-E2 reorder: streamer acquires the region BEFORE
                # thinking finalize runs. That way, when thinking
                # currently owns the region, streamer.append rotates
                # ownership without restart (no flicker), and
                # thinking.finalize then sees it's no longer the
                # owner — release becomes a coord-side no-op while
                # local state (tick thread, buffer) still gets cleared.
                self.streamer.append(event.content)
                self.thinking.finalize()

            elif isinstance(event, ThinkingReceived):
                # Buffer thinking tokens into a single Live block; do not
                # finalize the streamer (token vs thinking are separate channels).
                # The node label drives the structure half of the §9.4 header
                # ("意图识别 · 拆解中..." vs a bare "拆解中...").
                self.thinking.append(event.content, event.node)

            elif isinstance(event, ToolStarted):
                # PR-E2 reorder: streamer.finalize runs first because
                # the markdown flush contract requires Live stop ⇒
                # console.print ⇒ optional re-arm-for-header. Then
                # tool_panel.start acquires the region (rotating from
                # thinking if it's still owner, or starting fresh if
                # streamer's release tore the Live down). Finally
                # thinking.finalize cleans up local state — release is
                # a no-op against the coord since tool_panel is owner.
                self.streamer.finalize()
                self.tool_panel.start(event.tool_name)
                self.thinking.finalize()

            elif isinstance(event, ToolCompleted):
                # Symmetric to ToolStarted — see above for the rationale.
                self.streamer.finalize()
                self.tool_panel.complete(event.tool_name, event.content)
                self.thinking.finalize()

            elif isinstance(event, InterruptRequired):
                # All Live blocks must be released before yielding to a prompt.
                self.thinking.finalize()
                self.streamer.finalize()
                self.tool_panel.cancel()
                # Only pause the timeline if it had been started; otherwise
                # leave it dormant — we don't want a confirm card on a chat
                # turn (none today, but be defensive) to spawn a stepper.
                was_active = self.phase_timeline.active
                if was_active:
                    self.phase_timeline.stop()
                if self._interrupt_cb is not None:
                    await self._interrupt_cb(event)
                if was_active:
                    self.phase_timeline.start()

            elif isinstance(event, TaskResult):
                self.thinking.finalize()
                self.streamer.finalize()
                self.tool_panel.cancel()
                self.phase_timeline.stop()
                from chaos_agent.tui.state import DisplayMode
                display_mode = (
                    getattr(self._state, "display_mode", DisplayMode.WORKING)
                    if self._state is not None
                    else DisplayMode.WORKING
                )
                result.render_result(
                    self.console, event.data, event.task_id,
                    display_mode=display_mode,
                )
                if self._recorder is not None:
                    self._recorder.stop()
                if self._task_done_cb is not None:
                    self._task_done_cb("result")

            elif isinstance(event, TaskError):
                self.thinking.finalize()
                self.streamer.finalize()
                self.tool_panel.cancel()
                self.phase_timeline.stop()
                # PR follow-up — route through the suggestion-aware
                # wrapper. It falls back to the plain one-line error
                # when the message doesn't match any known pattern,
                # so generic errors keep the calmer rhythm; only
                # actionable categories grow into the recovery panel.
                messages.render_error_with_suggestions(
                    self.console, event.message, event.task_id
                )
                if self._recorder is not None:
                    self._recorder.stop()
                if self._task_done_cb is not None:
                    self._task_done_cb("error")

            elif isinstance(event, PhaseChanged):
                # Lazy-start the stepper on the first event for a tracked
                # pipeline node. Chat-only turns never reach this branch
                # because intent_clarification isn't in the phase map.
                phase = phase_for_node(event.source)
                if not phase:
                    return
                if not self.phase_timeline.active:
                    self.phase_timeline.start()
                self.phase_timeline.on_phase_event(
                    event.source, "Starting" in (event.message or "")
                )

        except Exception as e:
            logger.exception(f"Renderer dispatch failed: {e}")

    # -- Task lifecycle (owned here, not in a separate controller) -------
    def begin_task(self) -> None:
        """Mark the start of a new agent run.

        The phase stepper is no longer eagerly started here — it will lazy-
        start the first time a tracked pipeline node fires a PhaseChanged
        event. Pure chat / intent-clarification turns therefore don't paint
        the 5-stage stepper.
        """
        return

    def end_task(self) -> None:
        """Tear down any lingering Live blocks at the end of an agent run."""
        self.thinking.finalize()
        self.streamer.finalize()
        self.tool_panel.cancel()
        self.phase_timeline.stop()

    # -- Convenience pass-throughs ---------------------------------------
    def system(self, message: str) -> None:
        self.thinking.finalize()
        self.streamer.finalize()
        messages.render_system(self.console, message)

    def error(self, message: str, task_id: str = "") -> None:
        self.thinking.finalize()
        self.streamer.finalize()
        # Same routing as the TaskError dispatch branch above:
        # actionable patterns get the recovery panel, everything
        # else stays as a calm one-line error.
        messages.render_error_with_suggestions(self.console, message, task_id)

    def interrupted_tasks(self, tasks: list[dict]) -> None:
        self.thinking.finalize()
        self.streamer.finalize()
        interrupted_tasks.render_interrupted_tasks(self.console, tasks)

    def user_echo(self, text: str) -> None:
        messages.render_user(self.console, text)

    def shutdown(self) -> None:
        # PR-E2 contract: every printer that holds a coord slot
        # (region or header) must run its own finalize / cancel BEFORE
        # ``live_coordinator.shutdown()`` — otherwise the printer's
        # local ``_coord_active`` flag would leak into a future
        # session reuse and the next acquire would think it already
        # owns the slot. The order below preserves that contract.
        try:
            self.thinking.finalize()
        except Exception:
            pass
        try:
            self.streamer.finalize()
        except Exception:
            pass
        try:
            self.tool_panel.cancel()
        except Exception:
            pass
        try:
            self.phase_timeline.stop()
        except Exception:
            pass
        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception:
                pass
        # Belt and braces — if a printer skipped its release path on a
        # crash, force the Live region down so the terminal returns to
        # normal scroll mode. Runs LAST so the printer-side cleanup
        # above already cleared each ``_coord_active`` flag.
        try:
            self.live_coordinator.shutdown()
        except Exception:
            pass
