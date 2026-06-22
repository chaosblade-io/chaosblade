"""blade-ai exception → L4 AgentError mapping.

Maps runtime exceptions to one of 6 structured error codes
that ai-testing-platform understands.
"""

from __future__ import annotations

import re

from chaos_agent.agent.operation_outcome import read_operation_outcome
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


def map_to_agent_error(exc: Exception, context: dict | None = None) -> L4AgentError:
    """Map a blade-ai exception to a structured L4AgentError."""
    msg = str(exc)
    exc_type = type(exc).__name__
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
