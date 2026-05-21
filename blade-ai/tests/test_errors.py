"""Tests for the error classification hierarchy."""

import pytest

from chaos_agent.errors import (
    ChaosAgentError,
    ErrorSeverity,
    BladeExecutionError,
    BladeTransientError,
    InvalidParameterError,
    KubectlConnectionError,
    LLMContextOverflowError,
    LLMRateLimitError,
    SafetyBlockedError,
    SkillNotFoundError,
    TargetNotFoundError,
    ToolGuardError,
    ToolTimeoutError,
    is_recoverable,
    is_transient,
    should_auto_replan,
)


class TestErrorSeverity:
    """Test ErrorSeverity enum values."""

    def test_severity_values(self):
        assert ErrorSeverity.TRANSIENT.value == "transient"
        assert ErrorSeverity.PERMANENT.value == "permanent"
        assert ErrorSeverity.RECOVERABLE.value == "recoverable"


class TestChaosAgentError:
    """Test base ChaosAgentError class."""

    def test_default_severity_is_permanent(self):
        err = ChaosAgentError("test error")
        assert err.severity == ErrorSeverity.PERMANENT

    def test_default_error_code(self):
        err = ChaosAgentError("test error")
        assert err.error_code == 4001

    def test_custom_error_code(self):
        err = ChaosAgentError("test error", error_code=9999)
        assert err.error_code == 9999

    def test_message_preserved(self):
        err = ChaosAgentError("something went wrong")
        assert err.message == "something went wrong"
        assert str(err) == "something went wrong"

    def test_is_exception(self):
        with pytest.raises(ChaosAgentError):
            raise ChaosAgentError("boom")


class TestTransientErrors:
    """Test transient error types."""

    @pytest.mark.parametrize(
        "error_cls, expected_code",
        [
            (ToolTimeoutError, 4002),
            (KubectlConnectionError, 4003),
            (LLMRateLimitError, 4001),
            (BladeTransientError, 4002),
        ],
    )
    def test_transient_severity(self, error_cls, expected_code):
        err = error_cls("test")
        assert err.severity == ErrorSeverity.TRANSIENT
        assert err.error_code == expected_code

    def test_is_transient_returns_true(self):
        err = ToolTimeoutError("timed out")
        assert is_transient(err) is True

    def test_is_transient_returns_false_for_permanent(self):
        err = BladeExecutionError("failed")
        assert is_transient(err) is False

    def test_is_transient_returns_false_for_plain_exception(self):
        assert is_transient(ValueError("not chaos")) is False


class TestPermanentErrors:
    """Test permanent error types."""

    @pytest.mark.parametrize(
        "error_cls, expected_code",
        [
            (BladeExecutionError, 4002),
            (TargetNotFoundError, 1003),
            (SafetyBlockedError, 3001),
            (SkillNotFoundError, 1002),
            (InvalidParameterError, 1001),
            (ToolGuardError, 4001),
        ],
    )
    def test_permanent_severity(self, error_cls, expected_code):
        err = error_cls("test")
        assert err.severity == ErrorSeverity.PERMANENT
        assert err.error_code == expected_code


class TestRecoverableErrors:
    """Test recoverable error types."""

    def test_context_overflow_is_recoverable(self):
        err = LLMContextOverflowError("overflow")
        assert err.severity == ErrorSeverity.RECOVERABLE
        assert err.error_code == 4001

    def test_is_recoverable_returns_true(self):
        err = LLMContextOverflowError("overflow")
        assert is_recoverable(err) is True

    def test_is_recoverable_returns_false_for_transient(self):
        err = ToolTimeoutError("timed out")
        assert is_recoverable(err) is False

    def test_is_recoverable_returns_false_for_plain_exception(self):
        assert is_recoverable(ValueError("nope")) is False


# ---------------------------------------------------------------------------
# Tests for extract_llm_diagnosis / enrich_failure_reason
# ---------------------------------------------------------------------------

from chaos_agent.errors import (
    _DIAGNOSIS_FALLBACK,
    extract_llm_diagnosis,
    enrich_failure_reason,
)
from langchain_core.messages import AIMessage, HumanMessage


class TestExtractLlmDiagnosis:
    """Test extract_llm_diagnosis helper."""

    def test_finds_last_ai_message(self):
        msgs = [
            HumanMessage(content="inject memory fault"),
            AIMessage(content="Early analysis of the situation"),
            AIMessage(content="Target node cn-hongkong lacks ChaosBlade Agent"),
        ]
        result = extract_llm_diagnosis(msgs)
        assert "Target node cn-hongkong lacks ChaosBlade Agent" in result

    def test_empty_messages_returns_fallback(self):
        assert extract_llm_diagnosis([]) == _DIAGNOSIS_FALLBACK

    def test_skips_tool_only_ai_messages(self):
        tool_msg = AIMessage(
            content="",
            tool_calls=[{"name": "blade_create", "args": {}, "id": "1"}],
        )
        msgs = [tool_msg]
        assert extract_llm_diagnosis(msgs) == _DIAGNOSIS_FALLBACK

    def test_truncation(self):
        long_text = "A" * 600
        msgs = [AIMessage(content=long_text)]
        result = extract_llm_diagnosis(msgs, max_length=100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")

    def test_reasoning_content_fallback(self):
        msg = AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "Deep reasoning about the failure root cause here"},
        )
        result = extract_llm_diagnosis([msg])
        assert "Deep reasoning about the failure root cause" in result

    def test_skips_short_content(self):
        msgs = [AIMessage(content="ok")]
        assert extract_llm_diagnosis(msgs) == _DIAGNOSIS_FALLBACK

    def test_prefers_content_over_reasoning(self):
        msg = AIMessage(
            content="Content diagnosis is the primary source of truth",
            additional_kwargs={"reasoning_content": "reasoning backup"},
        )
        result = extract_llm_diagnosis([msg])
        assert "Content diagnosis" in result


class TestEnrichFailureReason:
    """Test enrich_failure_reason helper."""

    def test_appends_diagnosis(self):
        msgs = [AIMessage(content="Node missing ChaosBlade Agent, injection blocked")]
        result = enrich_failure_reason("verification_failed: Layer1=skipped", msgs)
        assert result.startswith("verification_failed: Layer1=skipped")
        assert "| llm_analysis:" in result
        assert "Node missing ChaosBlade Agent" in result

    def test_fallback_when_no_diagnosis(self):
        result = enrich_failure_reason("verification_failed: Layer1=skipped", [])
        assert "| llm_analysis:" in result
        assert _DIAGNOSIS_FALLBACK in result


class TestShouldAutoReplan:
    """Test should_auto_replan pattern matching."""

    def test_unknown_flag_triggers_replan(self):
        """'unknown flag: --namespace' should trigger auto-replan."""
        assert should_auto_replan("Error: blade create failed (exit 1): unknown flag: --namespace") is True

    def test_unknown_flag_case_insensitive(self):
        """Pattern matching should be case-insensitive."""
        assert should_auto_replan("Unknown Flag: --foo") is True

    def test_unsupported_flag_triggers_replan(self):
        """'unsupported flag' was already a replanable pattern."""
        assert should_auto_replan("Error: unsupported flag: --bar") is True

    def test_resource_not_found_triggers_replan(self):
        assert should_auto_replan("Error: resource not found: pods \"mysql\"") is True

    def test_permission_denied_no_replan(self):
        """Permission errors should NOT trigger replan."""
        assert should_auto_replan("Error: permission denied") is False

    def test_timeout_no_replan(self):
        """Timeout errors should NOT trigger replan."""
        assert should_auto_replan("Error: timeout waiting for blade status") is False

    def test_unknown_flag_dominates_when_combined_with_timeout(self):
        """Patch B layered classifier: USER_CONFIG (unknown flag) ranks
        above INFRA_TRANSIENT (timeout) so a mixed string is treated
        as REPLAN-able. Rationale: an "unknown flag" is a planning
        bug LLM can fix on replan; a co-occurring "timeout" is just
        ambient network noise that doesn't change what action is
        correct. Real-world co-occurrence of both signatures in one
        error string is rare enough that biasing toward the more
        specific signal is safer than the legacy "timeout always
        wins" behaviour. See ``ErrorClass`` / ``classify_error`` in
        ``chaos_agent.errors`` for the full rule order."""
        assert should_auto_replan("unknown flag: --namespace, timeout exceeded") is True
