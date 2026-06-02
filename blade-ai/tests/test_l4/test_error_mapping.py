"""Tests for chaos_agent.l4.error_mapping — exception to L4 error code mapping."""

import pytest

from chaos_agent.l4.error_mapping import (
    _build_step_result_from_error,
    _extract_error,
    map_error_class,
    map_to_agent_error,
)
from chaos_agent.l4.schemas import L4AgentError


class TestMapToAgentError:
    """Test map_to_agent_error() pattern matching."""

    @pytest.mark.parametrize(
        "exc,expected_code",
        [
            (TimeoutError("connection timed out"), "AGENT_TIMEOUT"),
            (RuntimeError("request timeout after 30s"), "AGENT_TIMEOUT"),
            (RuntimeError("pod not found in namespace"), "TARGET_UNREACHABLE"),
            (RuntimeError("resource does not exist"), "TARGET_UNREACHABLE"),
            (PermissionError("permission denied"), "PERMISSION_DENIED"),
            (RuntimeError("forbidden: user not authorized"), "PERMISSION_DENIED"),
            (RuntimeError("unauthorized access"), "PERMISSION_DENIED"),
            (RuntimeError("BladeExecutionError: cmd failed"), "TOOL_ERROR"),
            (RuntimeError("ToolGuardError: blocked"), "TOOL_ERROR"),
            (RuntimeError("verification failed at layer2"), "ASSERT_FAILED"),
            (RuntimeError("assert failed"), "ASSERT_FAILED"),
            (RuntimeError("something completely unknown"), "UNKNOWN"),
            (ValueError("unexpected"), "UNKNOWN"),
        ],
    )
    def test_pattern_matching(self, exc, expected_code):
        err = map_to_agent_error(exc)
        assert err.code == expected_code

    def test_returns_l4_agent_error(self):
        err = map_to_agent_error(RuntimeError("test"))
        assert isinstance(err, L4AgentError)

    def test_message_truncated_to_500(self):
        long_msg = "x" * 1000
        err = map_to_agent_error(RuntimeError(long_msg))
        assert len(err.message) == 500

    def test_recoverable_codes(self):
        timeout_err = map_to_agent_error(TimeoutError("timed out"))
        assert timeout_err.recoverable is True

        tool_err = map_to_agent_error(RuntimeError("blade error"))
        assert tool_err.recoverable is True

        perm_err = map_to_agent_error(PermissionError("forbidden"))
        assert perm_err.recoverable is False

    def test_context_passed_to_details(self):
        ctx = {"task_id": "t-001", "node": "execute_loop"}
        err = map_to_agent_error(RuntimeError("test"), context=ctx)
        assert err.details == ctx

    def test_context_none_yields_empty_details(self):
        err = map_to_agent_error(RuntimeError("test"), context=None)
        assert err.details == {}

    def test_matches_exception_class_name(self):
        """Pattern should also match against the exception type name."""

        class TimeoutException(Exception):
            pass

        err = map_to_agent_error(TimeoutException(""))
        assert err.code == "AGENT_TIMEOUT"


class TestMapErrorClass:
    """Test map_error_class() returns lowercase code."""

    def test_returns_lowercase(self):
        result = map_error_class(TimeoutError("timed out"))
        assert result == "agent_timeout"

    def test_unknown_returns_lowercase(self):
        result = map_error_class(RuntimeError("random error"))
        assert result == "unknown"


class TestExtractError:
    """Test _extract_error() graph state extraction."""

    def test_extracts_error_field(self):
        values = {"error": "connection timed out"}
        err = _extract_error(values, "failed")
        assert err.code == "AGENT_TIMEOUT"
        assert err.details["task_state"] == "failed"

    def test_extracts_error_message_field(self):
        values = {"error_message": "pod not found"}
        err = _extract_error(values, "failed")
        assert err.code == "TARGET_UNREACHABLE"

    def test_rejected_state_with_no_error(self):
        values = {"safety_status": "blocked"}
        err = _extract_error(values, "rejected")
        assert "rejected" in err.message.lower()
        assert "blocked" in err.message

    def test_fallback_when_no_error_info(self):
        values = {}
        err = _extract_error(values, "failed")
        assert err.code == "UNKNOWN"
        assert "task_state=failed" in err.message


class TestBuildStepResultFromError:
    """Test _build_step_result_from_error() object construction."""

    def test_creates_step_result_object(self):
        exc = RuntimeError("blade create failed")
        result = _build_step_result_from_error(exc)
        assert result.status == "failed"
        assert result.step_name == "fault_injection"
        assert "blade create failed" in result.error

    def test_error_truncated(self):
        exc = RuntimeError("x" * 1000)
        result = _build_step_result_from_error(exc)
        assert len(result.error) == 500
