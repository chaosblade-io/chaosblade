"""Transient dispatch-error retry — the transport layer owns it, not the LLM.

Field data (#49 ×23 in a 22-minute window, #50 ×10, #51 ×11): the wiz
dispatch layer intermittently rejects task submission BEFORE the command
ever runs (``No executor available`` / ``heartbeat is stale``). Surfacing
each occurrence to the LLM loop cost a full inference round just to
re-issue the identical command. These tests pin the retry contract:

- transient dispatch error → retried HERE (default 30s/60s backoff)
- flapping form heals → caller gets the SUCCESS, never sees the retries
- retries exhausted and STILL transient (zombie form) → stderr carries the
  exhaustion hint so the model stops re-issuing and reports the channel
- anything else (RBAC 403, protocol errors, success) → zero retries
"""
from unittest.mock import MagicMock, patch

import pytest

from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.guard_feedback import GuardFeedback
from chaos_agent.transports.base import TransportTarget
from chaos_agent.transports.executor import execute_via_transport
from chaos_agent.transports.transient import (
    is_transient_transport_error,
    transient_exhaustion_hint,
    transient_retry_delays,
)

NO_EXECUTOR = "Error: No executor available for cluster: c62735cce"
STALE_HEARTBEAT = "executor 97b841ce heartbeat is stale, skip scheduling"
RBAC_403 = "Error from server (Forbidden): deployments.apps is forbidden"


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset registry before each test to avoid cross-test contamination."""
    from chaos_agent.transports.registry import TransportRegistry
    TransportRegistry._channels = {}
    yield
    TransportRegistry._channels = {}


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Neutralize backoff sleeps — record the delays instead of paying them.

    Patched on the ``asyncio`` module the executor actually awaits, so the
    recorded values are the real policy output (default [30.0, 60.0]).
    """
    import chaos_agent.transports.executor as executor_mod

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(executor_mod.asyncio, "sleep", fake_sleep)
    return slept


def _wiz_target() -> TransportTarget:
    return TransportTarget(
        scope="k8s", kubewiz_cluster_uuid="uuid-1", kubewiz_profile="prof-1"
    )


def _guard() -> MagicMock:
    guard = MagicMock()
    guard.evaluate.return_value = GuardFeedback(allowed=True)
    return guard


class TestClassifier:
    """Signature classifier — the single source shared with P6 (B35)."""

    def test_no_executor_signature(self):
        assert is_transient_transport_error(NO_EXECUTOR)

    def test_stale_heartbeat_signature(self):
        assert is_transient_transport_error(STALE_HEARTBEAT)

    def test_rbac_403_is_not_transient(self):
        """A deterministic rejection can never heal by retrying."""
        assert not is_transient_transport_error(RBAC_403)

    def test_empty_and_none_are_not_transient(self):
        assert not is_transient_transport_error("")
        assert not is_transient_transport_error(None)

    def test_default_delays_bracket_the_probe_window(self):
        """30s → 60s: brackets the live-probed ~45s flapping self-heal."""
        assert transient_retry_delays() == [30.0, 60.0]


class TestTransientRetry:
    """Retry behaviour of ``execute_via_transport`` on the wiz channel."""

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_flapping_two_failures_then_success(
        self, mock_run, mock_guard, _fast_sleep
    ):
        """Two transient dispatch rejections, third attempt succeeds → the
        caller receives the SUCCESS (the retries never surface to the LLM)."""
        mock_guard.return_value = _guard()
        # wiz CLI itself fails non-zero with the dispatch error on stderr;
        # parse_wiz_output keeps that stderr verbatim (protocol.py).
        mock_run.side_effect = [
            CommandResult(1, "", NO_EXECUTOR),
            CommandResult(1, "", NO_EXECUTOR),
            CommandResult(0, "exit_code: 0\npod/nginx Running", ""),
        ]

        result = await execute_via_transport(
            ["kubectl", "get", "pods"], _wiz_target(), skip_guard=True
        )

        assert result.exit_code == 0
        assert result.stdout == "pod/nginx Running"
        assert mock_run.call_count == 3
        assert _fast_sleep == [30.0, 60.0]
        assert transient_exhaustion_hint() not in (result.stderr or "")

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_stale_heartbeat_signature_also_retried(
        self, mock_run, mock_guard, _fast_sleep
    ):
        """The second probed signature (zombie-leaning flapping form) takes
        the same retry path — the classifier is the only dispatch filter."""
        mock_guard.return_value = _guard()
        mock_run.side_effect = [
            CommandResult(1, "", STALE_HEARTBEAT),
            CommandResult(0, "exit_code: 0\nok", ""),
        ]

        result = await execute_via_transport(
            ["kubectl", "get", "pods"], _wiz_target(), skip_guard=True
        )

        assert result.exit_code == 0
        assert mock_run.call_count == 2
        assert _fast_sleep == [30.0]

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_exhausted_still_transient_appends_zombie_hint(
        self, mock_run, mock_guard, _fast_sleep
    ):
        """Retries exhausted and STILL transient = zombie form (dead executor,
        lingering registration). The final stderr must carry the exhaustion
        hint so the model knows unchanged retries are wasted."""
        mock_guard.return_value = _guard()
        mock_run.return_value = CommandResult(1, "", NO_EXECUTOR)

        result = await execute_via_transport(
            ["kubectl", "get", "pods"], _wiz_target(), skip_guard=True
        )

        assert result.exit_code == 1
        assert mock_run.call_count == 3  # 1 + 2 retries
        assert _fast_sleep == [30.0, 60.0]
        assert result.stderr.startswith(NO_EXECUTOR)
        assert "zombie executor registration" in result.stderr
        assert "report the channel" in result.stderr

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_rbac_403_never_retried(
        self, mock_run, mock_guard, _fast_sleep
    ):
        """A deterministic rejection is surfaced immediately — zero retries,
        zero backoff, no hint (retrying can never change a 403)."""
        mock_guard.return_value = _guard()
        mock_run.return_value = CommandResult(1, "", RBAC_403)

        result = await execute_via_transport(
            ["kubectl", "patch", "deploy", "x"], _wiz_target(), skip_guard=True
        )

        assert result.exit_code == 1
        assert result.stderr == RBAC_403
        assert mock_run.call_count == 1
        assert _fast_sleep == []

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_exit_zero_not_retried_even_with_transient_wording(
        self, mock_run, mock_guard, _fast_sleep
    ):
        """Success short-circuits the retry check BEFORE the signature test —
        a warning-shaped stderr on a green result is not a dispatch error."""
        mock_guard.return_value = _guard()
        mock_run.return_value = CommandResult(
            0, "exit_code: 0\nok", NO_EXECUTOR
        )

        result = await execute_via_transport(
            ["kubectl", "get", "pods"], _wiz_target(), skip_guard=True
        )

        assert result.exit_code == 0
        assert mock_run.call_count == 1
        assert _fast_sleep == []

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_retry_disabled_by_config(
        self, mock_run, mock_guard, _fast_sleep, monkeypatch
    ):
        """``transport_transient_retry_max=0`` restores the pre-P3 behaviour:
        single attempt, no hint — an explicit operator opt-out."""
        from chaos_agent.config.settings import settings

        monkeypatch.setattr(settings, "transport_transient_retry_max", 0)
        mock_guard.return_value = _guard()
        mock_run.return_value = CommandResult(1, "", NO_EXECUTOR)

        result = await execute_via_transport(
            ["kubectl", "get", "pods"], _wiz_target(), skip_guard=True
        )

        assert result.exit_code == 1
        assert result.stderr == NO_EXECUTOR
        assert mock_run.call_count == 1
        assert _fast_sleep == []

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_audit_records_final_result_once(
        self, mock_run, mock_guard, _fast_sleep
    ):
        """Guard + audit stay OUTSIDE the retry loop: one audit record, and
        it carries the FINAL result (with the exhaustion hint when the
        transient persisted) — not an intermediate attempt."""
        mock_guard.return_value = _guard()
        mock_run.return_value = CommandResult(1, "", NO_EXECUTOR)

        result = await execute_via_transport(
            ["kubectl", "get", "pods"], _wiz_target(),
            skip_guard=True, audit=True,
        )

        assert mock_run.call_count == 3
        mock_guard.return_value.audit_log.assert_called_once()
        audited_result = mock_guard.return_value.audit_log.call_args[0][1]
        assert audited_result is result
        assert "zombie executor registration" in audited_result.stderr

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_hint_absent_when_last_attempt_succeeds(
        self, mock_run, mock_guard, _fast_sleep
    ):
        """The exhaustion hint is keyed on the FINAL result's stderr — a
        late success (even after one transient failure) is a clean success."""
        mock_guard.return_value = _guard()
        mock_run.side_effect = [
            CommandResult(1, "", NO_EXECUTOR),
            CommandResult(0, "exit_code: 0\nok", ""),
        ]

        result = await execute_via_transport(
            ["kubectl", "get", "pods"], _wiz_target(), skip_guard=True
        )

        assert result.exit_code == 0
        assert result.stderr == ""
        assert transient_exhaustion_hint() not in result.stderr


class TestTransientRetryAuditTrail:
    """O1: the retry count rides the FINAL audit entry (one guard pass, one
    audit record by design) — channel-blip frequency stays groupable on the
    audit trail instead of under-counting."""

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_retry_count_rides_final_audit_entry(
        self, mock_run, mock_guard, _fast_sleep
    ):
        """Two transient failures healed on the third attempt → ONE audit
        entry whose transient_retries=2 (the mid-loop warnings are log-only
        and leave no other record)."""
        guard = _guard()
        mock_guard.return_value = guard
        mock_run.side_effect = [
            CommandResult(1, "", NO_EXECUTOR),
            CommandResult(1, "", NO_EXECUTOR),
            CommandResult(0, "exit_code: 0\nok", ""),
        ]

        result = await execute_via_transport(
            ["kubectl", "get", "pods"], _wiz_target(),
            skip_guard=True, audit=True,
        )

        assert result.exit_code == 0
        assert guard.audit_log.call_count == 1
        assert guard.audit_log.call_args.kwargs.get(
            "transient_retries"
        ) == 2

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_zero_retries_default_entry_unchanged(
        self, mock_run, mock_guard, _fast_sleep
    ):
        """No retry → the executor still passes the count (0) and the
        guard's entry writer omits the key for zero (shape unchanged)."""
        guard = _guard()
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, "exit_code: 0\nok", "")

        await execute_via_transport(
            ["kubectl", "get", "pods"], _wiz_target(),
            skip_guard=True, audit=True,
        )

        assert guard.audit_log.call_count == 1
        assert guard.audit_log.call_args.kwargs.get(
            "transient_retries"
        ) == 0

    def test_guard_entry_writer_omits_zero_and_keeps_shape(self):
        """The writer itself: transient_retries=0 → no key; N → the key."""
        import json as _json
        import logging

        from chaos_agent.tools.guard import ToolGuard

        guard = ToolGuard.__new__(ToolGuard)  # no __init__ state needed
        result = CommandResult(0, "ok", "")
        entries: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                entries.append(record.getMessage())

        handler = _Capture()
        logger = logging.getLogger("chaos_agent.tools.guard")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            guard.audit_log(["kubectl", "get"], result, "t-1")
            guard.audit_log(
                ["kubectl", "get"], result, "t-2", transient_retries=2,
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        clean = _json.loads(entries[0])
        retried = _json.loads(entries[1])
        assert "transient_retries" not in clean
        assert retried["transient_retries"] == 2
