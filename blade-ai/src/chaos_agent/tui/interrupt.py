"""InterruptHandler - self-contained interrupt handling for TUI.

Handles confirmation_gate, intent_confirm, and question interrupts by
directly rendering UI and collecting user input. No Future pattern needed.

This eliminates the yield/Future timing deadlock that occurred when the
async generator yielded a "confirm" event and then awaited a Future that
hadn't been created yet on the consumer side.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class InterruptHandler:
    """Self-contained interrupt handler for TUI.

    Directly renders the appropriate UI panel and returns the user's answer.
    No async Future coordination needed — the callback is called inline
    by the runner within the generator's execution context.

    Handles three interrupt types:
    - intent_confirm: intent confirmation gate (approve/reject)
    - confirmation: execution confirmation gate (approve/reject)
    - question: free-text question (user input)
    """

    def __init__(self, console=None, renderer=None, state=None) -> None:
        self._console = console
        self._renderer = renderer
        # PR-D3 — state is read at handle_interrupt time to forward
        # ``display_mode`` into intent_confirm.run() so the risk meter
        # honours the user's calm/working/dense preference. None is OK
        # (renderer falls back to working).
        self._state = state

    def set_console(self, console) -> None:
        """Set the console after construction (for lazy init)."""
        self._console = console

    def set_renderer(self, renderer) -> None:
        """Set the renderer after construction (for lazy init)."""
        self._renderer = renderer

    def set_state(self, state) -> None:
        """Set the state after construction (for lazy init)."""
        self._state = state

    async def handle_interrupt(self, interrupt_info: dict) -> str:
        """Handle an interrupt by rendering UI and returning user's answer.

        Called directly by the runner (inject_stream / converse_stream) when
        the graph pauses at an interrupt point. This runs inline within the
        async generator — no event loop coordination needed.

        Args:
            interrupt_info: Dict with interrupt details.
                intent_confirm: {"type": "intent_confirm", "fault_intent": ..., "summary": ...}
                confirmation: {"type": "confirmation", "plan_summary": ..., "safety_status": ...}
                question: {"type": "question", "content": "..."}

        Returns:
            User response: "approved"/"rejected" for confirmations, free text for questions.
        """
        if not self._console:
            logger.warning("InterruptHandler has no console, defaulting to 'approved'")
            return "approved"

        # Flush any active renderer state before showing interrupt panel
        if self._renderer:
            self._renderer.thinking.finalize()
            self._renderer.streamer.finalize()
            self._renderer.tool_panel.cancel()
            self._renderer.phase_timeline.stop()

        # Render the appropriate UI and collect answer
        interrupt_type = interrupt_info.get("type", "confirmation")
        try:
            if interrupt_type == "intent_confirm":
                from chaos_agent.tui.renderers import (
                    experiment_card as experiment_card_renderer,
                    intent_confirm as intent_confirm_renderer,
                )
                from chaos_agent.tui.state import DisplayMode
                display_mode = (
                    getattr(self._state, "display_mode", DisplayMode.WORKING)
                    if self._state is not None
                    else DisplayMode.WORKING
                )
                answer = await intent_confirm_renderer.run(
                    self._console, interrupt_info, display_mode=display_mode
                )
                # PR-D2 — on approval, paint the experiment card so the
                # phase timeline that follows is framed as a chaos
                # experiment (hypothesis · blast radius · [rollback]).
                # The renderer is a no-op when display_mode is calm or
                # fault_intent is empty — no extra branching needed here.
                if answer == "approved":
                    fault_intent = interrupt_info.get("fault_intent") or {}
                    experiment_card_renderer.render(
                        self._console,
                        fault_intent,
                        display_mode=display_mode,
                        state=self._state,
                    )
            elif interrupt_type == "confirmation":
                from chaos_agent.tui.renderers import confirm as confirm_renderer
                answer = await confirm_renderer.run(self._console, interrupt_info)
            else:
                from chaos_agent.tui.renderers import question as question_renderer
                answer = await question_renderer.run(self._console, interrupt_info)
        except (KeyboardInterrupt, EOFError):
            answer = "rejected" if interrupt_type in ("confirmation", "intent_confirm") else ""

        # Restart phase timeline after user resolves
        if self._renderer:
            self._renderer.phase_timeline.start()

        # Vertical breath: 1 blank line between the interrupt panel
        # and the resumed execution output that follows immediately.
        # Without it, the phase-timeline line and the first token of
        # the resumed stream mash into one visual block.
        if self._console:
            self._console.print("")

        return answer
