"""Tests for chaos_agent.l4.error_mapping — exception to L4 error code mapping."""

import httpx
import openai
import pytest

from chaos_agent.agent.resilient_llm import _provider_reject_error
from chaos_agent.l4.error_mapping import (
    _build_step_result_from_error,
    _extract_error,
    map_error_class,
    map_to_agent_error,
)
from chaos_agent.l4.schemas import L4AgentError


def _api_error(message: str, status: int) -> openai.APIStatusError:
    """A real openai 4xx. ``status_code`` comes from the httpx.Response, not
    from the exception class, so the status has to be passed explicitly.
    """
    response = httpx.Response(
        status_code=status,
        request=httpx.Request("POST", "http://localhost:1/v1/chat/completions"),
    )
    return openai.APIStatusError(
        message, response=response, body={"error": {"message": message}}
    )


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


class TestLLMProviderRejectIsNotMisreadAsADrillFault:
    """A provider rejection is an agent-side config/protocol fault. The raw
    provider body rides in its message, and that body routinely contains
    phrases the pattern table reads as a TARGET or TIMEOUT failure — which
    would send the platform's operator (or its heal loop) after the wrong
    thing. Built with the production ``_provider_reject_error`` so the
    message under test is the one the LLM layer actually emits.
    """

    def test_model_not_found_404_is_not_target_unreachable(self):
        """The regression that motivated the guard: ``does not exist`` in a
        404 body used to read as TARGET_UNREACHABLE, pointing the operator at
        the cluster when the real cause is the ``llm_model`` setting."""
        exc = _provider_reject_error(
            _api_error("Error code: 404 - The model `qwen-max-x` does not exist", 404)
        )
        err = map_to_agent_error(exc)
        assert err.code == "UNKNOWN"
        assert err.code != "TARGET_UNREACHABLE"
        assert err.recoverable is False
        assert err.details["error_kind"] == "llm_provider_reject"

    def test_timeout_wording_in_body_is_not_recoverable_agent_timeout(self):
        """``AGENT_TIMEOUT`` is the one code with ``recoverable=True`` besides
        TOOL_ERROR, so a timeout phrase in a 400 body used to invite the
        platform to heal-rerun the whole task — re-injecting the fault — for
        an error the LLM layer had just declared deterministic."""
        exc = _provider_reject_error(
            _api_error("Error code: 400 - upstream request timed out while proxying", 400)
        )
        err = map_to_agent_error(exc)
        assert err.code == "UNKNOWN"
        assert err.recoverable is False
        assert map_error_class(exc) == "unknown"

    def test_matched_signature_hint_also_guards(self):
        """When a signature matches, the hint REPLACES the raw body — which
        already narrows the misread surface on this path. The guard must still
        hold on the class name alone, since ``_extract_error`` and other
        string consumers only ever see the message.
        """
        exc = _provider_reject_error(
            _api_error(
                "Error code: 400 - Messages with role 'tool' must be a response "
                "to a preceding message with 'tool_calls'",
                400,
            )
        )
        # Signature hit ⇒ the misleading provider phrase never reaches the
        # L4 pattern table; only the mapped hint does.
        assert "tool_calls" not in str(exc)
        assert "tool_call/ToolMessage pairing" in str(exc)
        # Guard still fires — on the class name this time.
        err = map_to_agent_error(exc)
        assert err.code == "UNKNOWN"
        assert err.details["error_kind"] == "llm_provider_reject"

    def test_state_string_path_where_the_type_is_lost(self):
        """``_extract_error`` rebuilds a plain RuntimeError from a state
        string, so ``exc_type`` is gone. The message prefix must carry the
        guard on its own — this is the path a graph-terminal LLM failure
        actually takes."""
        original = _provider_reject_error(
            _api_error("Error code: 404 - The model `qwen-max-x` does not exist", 404)
        )
        # What session_finalize._format_error writes into the envelope/state.
        as_state_string = f"{type(original).__name__}: {original}"
        err = map_to_agent_error(RuntimeError(as_state_string))
        assert err.code == "UNKNOWN"
        assert err.recoverable is False
        assert err.details["error_kind"] == "llm_provider_reject"

    def test_bare_message_prefix_without_class_name(self):
        """Some state writers store ``str(exc)`` only, no class name."""
        original = _provider_reject_error(
            _api_error("Error code: 404 - model does not exist", 404)
        )
        err = map_to_agent_error(RuntimeError(str(original)))
        assert err.details.get("error_kind") == "llm_provider_reject"
        assert err.code == "UNKNOWN"

    def test_context_is_preserved_alongside_error_kind(self):
        ctx = {"task_id": "t-001", "task_state": "failed"}
        exc = _provider_reject_error(_api_error("Error code: 404 - not found", 404))
        err = map_to_agent_error(exc, context=ctx)
        assert err.details == {**ctx, "error_kind": "llm_provider_reject"}

    def test_context_overflow_is_not_a_heal_rerun_signal(self):
        """The one rejection that READS as recoverable (design.md D5a): the
        remedy really is "make the context smaller", so ``recoverable=True``
        looks tempting. It is wrong — at this layer ``recoverable`` means the
        platform heal-reruns the whole task, which re-injects the fault and
        then faces the same bloated checkpoint, so it collects the same 400.
        Compaction is an operator action (/compact) or a config fix
        (settings.model_budgets), not a task rerun.

        The sibling tests above cover 404 / timeout / pairing wordings; this is
        the signature they never exercised, and it is the one whose hint
        explicitly offers a recovery.
        """
        exc = _provider_reject_error(
            _api_error(
                "Error code: 400 - This model's maximum context length is "
                "131072 tokens",
                400,
            )
        )
        # The hint that makes this case tempting is really there.
        assert "context window" in str(exc)
        assert "/compact" in str(exc)

        err = map_to_agent_error(exc)
        assert err.recoverable is False
        assert err.code == "UNKNOWN"
        assert err.details["error_kind"] == "llm_provider_reject"
        assert map_error_class(exc) == "unknown"

    def test_code_enum_stays_closed(self):
        """The 6-value code enum is a contract with ai-testing-platform — the
        guard must classify inside it, never extend it."""
        exc = _provider_reject_error(_api_error("Error code: 404 - not found", 404))
        allowed = {
            "AGENT_TIMEOUT", "TARGET_UNREACHABLE", "PERMISSION_DENIED",
            "TOOL_ERROR", "ASSERT_FAILED", "UNKNOWN",
        }
        assert map_to_agent_error(exc).code in allowed

    def test_genuine_drill_faults_are_untouched(self):
        """The guard is narrow: real target/timeout failures still classify
        exactly as before, including the recoverable flag the heal loop needs."""
        assert map_to_agent_error(RuntimeError("pod not found")).code == "TARGET_UNREACHABLE"
        timeout = map_to_agent_error(TimeoutError("connection timed out"))
        assert timeout.code == "AGENT_TIMEOUT"
        assert timeout.recoverable is True
        assert "error_kind" not in timeout.details


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
