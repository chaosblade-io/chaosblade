"""Tests for the CLI orphan-row signal guard (runner.py).

Covers the SIGTERM cancel guard and the interrupted-row terminal write
introduced to close the CLI-direct orphan gap: a SIGTERM'd / Ctrl-C'd
inject run used to leave its TaskStore row at the last mid-graph upsert
("injecting"), a zombie no later writer can fix.
"""

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.cli.runner import (
    _install_sigterm_cancel_guard,
    _write_signal_interrupted_row,
)

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "chaos_agent" / "cli" / "runner.py"
)


class TestWriteSignalInterruptedRow:
    @pytest.mark.asyncio
    async def test_writes_cancelled_through_shared_taxonomy(self):
        """user_cancel classifies to "cancelled" via abort_row_word — the
        same single source the server abort paths use."""
        with patch(
            "chaos_agent.server.routes.stream_abort.write_aborted_task_row",
            new_callable=AsyncMock,
        ) as mock_write:
            await _write_signal_interrupted_row("inject-abc123")
        mock_write.assert_awaited_once_with("inject-abc123", "cancelled")

    @pytest.mark.asyncio
    async def test_propagates_store_error(self):
        """The helper is a thin pass-through: fail-soft is the runner's
        except-branch responsibility (its try/except logs the LOUD
        warning), so store errors must propagate to it unchanged."""
        with patch(
            "chaos_agent.server.routes.stream_abort.write_aborted_task_row",
            new=AsyncMock(side_effect=RuntimeError("store down")),
        ):
            with pytest.raises(RuntimeError, match="store down"):
                await _write_signal_interrupted_row("inject-abc123")


@pytest.mark.skipif(
    not hasattr(signal, "SIGTERM") or os.name == "nt",
    reason="add_signal_handler needs a Unix main-thread event loop",
)
class TestSigtermCancelGuard:
    @pytest.mark.asyncio
    async def test_sigterm_cancels_driving_task(self):
        """SIGTERM must cancel the driving task (guard installed inside
        it), not kill the process — the except branch around the graph
        run turns that cancel into the terminal row write."""
        cleanup = None
        interrupted = asyncio.Event()

        async def victim():
            nonlocal cleanup
            cleanup = _install_sigterm_cancel_guard()
            assert cleanup is not None  # fail BEFORE sending the signal
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                interrupted.set()
                raise

        task = asyncio.create_task(victim())
        try:
            await asyncio.sleep(0.05)  # let the guard install
            assert cleanup is not None
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(interrupted.wait(), timeout=5)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if cleanup is not None:
                cleanup()

    @pytest.mark.asyncio
    async def test_cleanup_removes_handler(self):
        cleanup = _install_sigterm_cancel_guard()
        assert cleanup is not None
        cleanup()
        # No handler left: removing again reports False.
        assert asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM) is False

    @pytest.mark.asyncio
    async def test_degrades_to_none_when_unsupported(self):
        """Non-main-thread / unsupported platform: degrade to the
        pre-guard behavior (None), never fail the run."""
        mock_loop = MagicMock()
        mock_loop.add_signal_handler.side_effect = NotImplementedError
        with patch(
            "chaos_agent.cli.runner.asyncio.get_running_loop",
            return_value=mock_loop,
        ):
            assert _install_sigterm_cancel_guard() is None


class TestRunnerWiringContract:
    """Source-level wiring guard: every CLI graph-run entry must install
    the guard and route interrupt exceptions through the shared write —
    a new entry point (or an accidental revert) fails here."""

    def test_inject_entries_wired(self):
        src = _RUNNER_PATH.read_text(encoding="utf-8")
        # The assignment-call form excludes the helper's own def line;
        # the write helper never calls itself, so task_id call sites are
        # exactly the wired methods.
        assert src.count("= _install_sigterm_cancel_guard()") == 3, (
            "expected exactly 3 CLI entries wired with the SIGTERM guard "
            "(inject_stream / inject / resume_stream)"
        )
        assert src.count("await _write_signal_interrupted_row(task_id)") == 3, (
            "expected exactly 3 interrupt-except branches writing the "
            "terminal row"
        )
