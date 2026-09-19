"""blade-ai exception → L4 AgentError mapping.

Maps runtime exceptions to one of 6 structured error codes
that ai-testing-platform understands.
"""

from __future__ import annotations

import re

from chaos_agent.agent.result.operation_outcome import read_operation_outcome
from chaos_agent.l4.schemas import L4AgentError

# Ordered patterns: first match wins
_ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"timed?\s*out|timeout", re.I), "AGENT_TIMEOUT"),
    (re.compile(r"not\s*found|does\s*not\s*exist", re.I), "TARGET_UNREACHABLE"),
    (
        re.compile(r"permission|forbidden|unauthorized|auth", re.I),
        "PERMISSION_DENIED",
    ),
    (re.compile(r"blade.*error|tool.*guard", re.I), "TOOL_ERROR"),
    (re.compile(r"verif.*fail|assert.*fail|layer.?2", re.I), "ASSERT_FAILED"),
]

# Matches an LLM provider rejection on BOTH the class name and the message
# prefix. Both are needed: l4/execution.py's ``except`` path still holds the
# real ``LLMProviderRejectError``, while ``_extract_error`` rebuilds a plain
# ``RuntimeError`` from a state string, where only the message survives.
_LLM_PROVIDER_REJECT = re.compile(
    r"LLMProviderRejectError|LLM provider rejection", re.I
)


def map_to_agent_error(exc: Exception, context: dict | None = None) -> L4AgentError:
    """Map a blade-ai exception to a structured L4AgentError."""
    msg = str(exc)
    exc_type = type(exc).__name__

    # Type-first guard, ahead of the pattern table. When no hint signature
    # matches, the rejection's message echoes up to 300 chars of the raw
    # provider body, and that body routinely contains phrases the table
    # below reads as a DRILL fault:
    #
    #   "The model `qwen-x` does not exist" (404) → `does\s*not\s*exist`
    #       → TARGET_UNREACHABLE, sending the operator to inspect the
    #         cluster when the real cause is the llm_model setting;
    #   "... request timed out" in a 400/422 body  → `timed?\s*out`
    #       → AGENT_TIMEOUT with recoverable=True, which makes the platform
    #         heal-rerun the WHOLE task — re-injecting the fault — for an
    #         error the LLM layer just declared deterministic.
    #
    # (Genuine 408/409 never reach here: they sit in
    # ``_RETRIABLE_4XX_STATUSES`` and stay unwrapped, so they keep the
    # AGENT_TIMEOUT reading that is correct for them.)
    #
    # ``code`` stays UNKNOWN because the 6-value enum is a closed contract
    # with ai-testing-platform (see L4AgentError.code); inventing a 7th code
    # the platform cannot parse would be worse than an honest UNKNOWN. The
    # distinguishing signal rides in ``details`` instead, and recoverable is
    # forced False so no consumer retries a deterministic rejection.
    if _LLM_PROVIDER_REJECT.search(msg) or _LLM_PROVIDER_REJECT.search(exc_type):
        return L4AgentError(
            code="UNKNOWN",
            message=msg[:500],
            recoverable=False,
            details={**(context or {}), "error_kind": "llm_provider_reject"},
        )

    code = "UNKNOWN"
    for pattern, error_code in _ERROR_PATTERNS:
        if pattern.search(msg) or pattern.search(exc_type):
            code = error_code
            break
    return L4AgentError(
        code=code,
        message=msg[:500],
        recoverable=code in ("AGENT_TIMEOUT", "TOOL_ERROR"),
        details=context or {},
    )


def map_error_class(exc: Exception) -> str:
    """Return error classification string for runtime.heal(error_class=...).

    heal() uses this to decide recovery strategy: retry / fallback / escalate.
    """
    err = map_to_agent_error(exc)
    return err.code.lower()


def _extract_error(values: dict, task_state: str) -> L4AgentError:
    """Extract error information from graph final state into L4AgentError."""
    error_msg = read_operation_outcome(values).error or values.get("error_message", "")
    if not error_msg and task_state == "rejected":
        error_msg = (
            f"Task rejected: safety_status={values.get('safety_status', 'unknown')}"
        )
    dummy_exc = (
        RuntimeError(error_msg)
        if error_msg
        else RuntimeError(f"task_state={task_state}")
    )
    return map_to_agent_error(dummy_exc, context={"task_state": task_state})


def _build_step_result_from_error(exc: Exception) -> object:
    """Build a step_result object required by runtime.heal().

    heal(step_result, error_class) needs context about the failed step.
    """
    return type(
        "StepResult",
        (),
        {
            "status": "failed",
            "error": str(exc)[:500],
            "step_name": "fault_injection",
        },
    )()
