"""Tests for InterruptHandler - self-contained interrupt handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.tui.interrupt import InterruptHandler


@pytest.mark.asyncio
class TestInterruptHandler:
    async def test_no_console_returns_approved(self):
        """Without a console, handle_interrupt defaults to 'approved'."""
        handler = InterruptHandler()
        info = {"type": "confirmation", "plan_summary": "inject CPU fault"}
        result = await handler.handle_interrupt(info)
        assert result == "approved"

    async def test_confirmation_interrupt(self):
        """Confirmation interrupt calls confirm renderer and returns answer."""
        console = MagicMock()
        handler = InterruptHandler(console=console)
        info = {"type": "confirmation", "plan_summary": "inject CPU fault"}

        with patch(
            "chaos_agent.tui.renderers.confirm.run",
            new_callable=AsyncMock,
            return_value="approved",
        ) as mock_run:
            result = await handler.handle_interrupt(info)
            mock_run.assert_awaited_once_with(console, info)
            assert result == "approved"

    async def test_rejected_confirmation(self):
        """Confirmation interrupt can return 'rejected'."""
        console = MagicMock()
        handler = InterruptHandler(console=console)
        info = {"type": "confirmation", "plan_summary": "inject CPU fault"}

        with patch(
            "chaos_agent.tui.renderers.confirm.run",
            new_callable=AsyncMock,
            return_value="rejected",
        ):
            result = await handler.handle_interrupt(info)
            assert result == "rejected"

    async def test_question_interrupt(self):
        """Question interrupt calls question renderer and returns user text."""
        console = MagicMock()
        handler = InterruptHandler(console=console)
        info = {"type": "question", "content": "What namespace?"}

        with patch(
            "chaos_agent.tui.renderers.question.run",
            new_callable=AsyncMock,
            return_value="cms-demo",
        ) as mock_run:
            result = await handler.handle_interrupt(info)
            mock_run.assert_awaited_once_with(console, info)
            assert result == "cms-demo"

    async def test_intent_confirm_interrupt(self):
        """Intent confirm interrupt calls intent_confirm renderer."""
        from chaos_agent.tui.state import DisplayMode

        console = MagicMock()
        handler = InterruptHandler(console=console)
        info = {"type": "intent_confirm", "fault_intent": {"fault_type": "cpu"}}

        with patch(
            "chaos_agent.tui.renderers.intent_confirm.run",
            new_callable=AsyncMock,
            return_value="approved",
        ) as mock_run:
            result = await handler.handle_interrupt(info)
            # PR-D3 — the handler forwards the active display_mode so the
            # risk meter respects calm/working/dense. Without explicit
            # state, working is the safe default.
            mock_run.assert_awaited_once_with(
                console, info, display_mode=DisplayMode.WORKING
            )
            assert result == "approved"

    async def test_renderer_flush_before_interrupt(self):
        """Renderer state is flushed before showing interrupt panel."""
        console = MagicMock()
        renderer = MagicMock()
        renderer.thinking = MagicMock()
        renderer.streamer = MagicMock()
        renderer.tool_panel = MagicMock()
        renderer.phase_timeline = MagicMock()

        handler = InterruptHandler(console=console, renderer=renderer)
        info = {"type": "confirmation", "plan_summary": "test"}

        with patch(
            "chaos_agent.tui.renderers.confirm.run",
            new_callable=AsyncMock,
            return_value="approved",
        ):
            await handler.handle_interrupt(info)

        renderer.thinking.finalize.assert_called_once()
        renderer.streamer.finalize.assert_called_once()
        renderer.tool_panel.cancel.assert_called_once()
        renderer.phase_timeline.stop.assert_called_once()

    async def test_phase_timeline_restarted_after_interrupt(self):
        """Phase timeline is restarted after user resolves interrupt."""
        console = MagicMock()
        renderer = MagicMock()
        renderer.thinking = MagicMock()
        renderer.streamer = MagicMock()
        renderer.tool_panel = MagicMock()
        renderer.phase_timeline = MagicMock()

        handler = InterruptHandler(console=console, renderer=renderer)
        info = {"type": "confirmation", "plan_summary": "test"}

        with patch(
            "chaos_agent.tui.renderers.confirm.run",
            new_callable=AsyncMock,
            return_value="approved",
        ):
            await handler.handle_interrupt(info)

        renderer.phase_timeline.start.assert_called_once()

    async def test_keyboard_interrupt_returns_rejected(self):
        """KeyboardInterrupt during confirmation returns 'rejected'."""
        console = MagicMock()
        handler = InterruptHandler(console=console)
        info = {"type": "confirmation", "plan_summary": "test"}

        with patch(
            "chaos_agent.tui.renderers.confirm.run",
            new_callable=AsyncMock,
            side_effect=KeyboardInterrupt,
        ):
            result = await handler.handle_interrupt(info)
            assert result == "rejected"

    async def test_keyboard_interrupt_on_question_returns_empty(self):
        """KeyboardInterrupt during question returns empty string."""
        console = MagicMock()
        handler = InterruptHandler(console=console)
        info = {"type": "question", "content": "What namespace?"}

        with patch(
            "chaos_agent.tui.renderers.question.run",
            new_callable=AsyncMock,
            side_effect=KeyboardInterrupt,
        ):
            result = await handler.handle_interrupt(info)
            assert result == ""

    async def test_set_console_after_construction(self):
        """Console can be set lazily after construction."""
        handler = InterruptHandler()
        console = MagicMock()
        handler.set_console(console)
        info = {"type": "confirmation", "plan_summary": "test"}

        with patch(
            "chaos_agent.tui.renderers.confirm.run",
            new_callable=AsyncMock,
            return_value="approved",
        ) as mock_run:
            result = await handler.handle_interrupt(info)
            mock_run.assert_awaited_once_with(console, info)
            assert result == "approved"

    async def test_set_renderer_after_construction(self):
        """Renderer can be set lazily after construction."""
        handler = InterruptHandler()
        renderer = MagicMock()
        renderer.thinking = MagicMock()
        renderer.streamer = MagicMock()
        renderer.tool_panel = MagicMock()
        renderer.phase_timeline = MagicMock()
        handler.set_renderer(renderer)
        assert handler._renderer is renderer
