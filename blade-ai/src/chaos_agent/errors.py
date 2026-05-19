"""Error classification hierarchy for Blade AI.

Errors are classified by severity:
- TRANSIENT: Temporary failures that can be retried (network timeout, API rate limit)
- PERMANENT: Unrecoverable failures that should fail immediately (invalid target, safety block)
- RECOVERABLE: Failures that can be handled by degradation (context overflow triggers compaction)
"""

from enum import Enum


class ErrorSeverity(Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    RECOVERABLE = "recoverable"
    REPLANABLE = "replanable"


class ChaosAgentError(Exception):
    """Base error with severity classification."""

    severity: ErrorSeverity = ErrorSeverity.PERMANENT
    error_code: int = 4001

    def __init__(self, message: str = "", *, error_code: int | None = None):
        self.message = message
        if error_code is not None:
            self.error_code = error_code
        super().__init__(message)


# --- Transient Errors (可重试) ---


class ToolTimeoutError(ChaosAgentError):
    """Tool execution timed out - transient, can retry."""

    severity = ErrorSeverity.TRANSIENT
    error_code = 4002


class KubectlConnectionError(ChaosAgentError):
    """kubectl connection failed - transient, can retry."""

    severity = ErrorSeverity.TRANSIENT
    error_code = 4003


class LLMRateLimitError(ChaosAgentError):
    """LLM API rate limited - transient, can retry."""

    severity = ErrorSeverity.TRANSIENT
    error_code = 4001


class BladeTransientError(ChaosAgentError):
    """Blade transient failure - can retry."""

    severity = ErrorSeverity.TRANSIENT
    error_code = 4002


# --- Permanent Errors (立即失败) ---


class BladeExecutionError(ChaosAgentError):
    """Blade execution failed permanently."""

    severity = ErrorSeverity.PERMANENT
    error_code = 4002


class TargetNotFoundError(ChaosAgentError):
    """Target K8s resource does not exist."""

    severity = ErrorSeverity.PERMANENT
    error_code = 1003


class SafetyBlockedError(ChaosAgentError):
    """Safety check blocked the operation."""

    severity = ErrorSeverity.PERMANENT
    error_code = 3001


class SkillNotFoundError(ChaosAgentError):
    """Requested skill not found in registry."""

    severity = ErrorSeverity.PERMANENT
    error_code = 1002


class InvalidParameterError(ChaosAgentError):
    """Invalid or missing parameters."""

    severity = ErrorSeverity.PERMANENT
    error_code = 1001


class ToolGuardError(ChaosAgentError):
    """Tool guard blocked the command."""

    severity = ErrorSeverity.PERMANENT
    error_code = 4001


class ScriptExecutionError(ChaosAgentError):
    """Script execution validation failed (path traversal, bad extension, etc)."""

    severity = ErrorSeverity.PERMANENT
    error_code = 4004


class ScriptTimeoutError(ChaosAgentError):
    """Script execution timed out - transient, can retry."""

    severity = ErrorSeverity.TRANSIENT
    error_code = 4005


# --- Recoverable Errors (降级后重试) ---


class LLMContextOverflowError(ChaosAgentError):
    """Context window overflow - recoverable by compaction."""

    severity = ErrorSeverity.RECOVERABLE
    error_code = 4001


def is_transient(error: Exception) -> bool:
    """Check if an error is transient (should be retried)."""
    return isinstance(error, ChaosAgentError) and error.severity == ErrorSeverity.TRANSIENT


def is_recoverable(error: Exception) -> bool:
    """Check if an error is recoverable (can retry after degradation)."""
    return isinstance(error, ChaosAgentError) and error.severity == ErrorSeverity.RECOVERABLE


def is_replanable(error: Exception) -> bool:
    """Check if an error should trigger replan to Phase 1."""
    return isinstance(error, ChaosAgentError) and error.severity == ErrorSeverity.REPLANABLE


# ---------------------------------------------------------------------------
# Auto-replan pattern matching (for router use, independent of exception types)
# ---------------------------------------------------------------------------

_REPLANABLE_PATTERNS = [
    "resource not found", "target not found", "not found",
    "invalid parameter", "flag mismatch", "unsupported flag",
    "unknown flag",  # blade CLI version incompatibility (e.g., "unknown flag: --namespace")
    "no matches for kind", "no resources found",
]

_NON_REPLANABLE_PATTERNS = [
    "permission denied", "forbidden", "unauthorized",
    "timeout", "connection refused", "deadline exceeded",
]


class FailureReason(Enum):
    """Categorized failure reason for task result.

    Only set when the task result is "failed". Not set on success.
    """

    # --- Planning phase ---
    PLANNING_TIMEOUT = "planning_timeout"        # Agent loop max iterations exceeded
    SAFETY_REJECTED = "safety_rejected"          # Safety check blocked the operation
    USER_REJECTED = "user_rejected"              # User rejected the confirmation

    # --- Execution phase ---
    PREREQUISITE_FAILED = "prerequisite_failed"    # Pre-condition not met (e.g., no DaemonSet pod)
    EXECUTION_FAILED = "execution_failed"        # Execute loop error (non-replanable)
    EXECUTION_TIMEOUT = "execution_timeout"      # Execute loop max iterations exceeded
    REPLAN_EXHAUSTED = "replan_exhausted"        # Replan count exhausted, still failing

    # --- Verification phase ---
    VERIFICATION_FAILED = "verification_failed"  # Post-injection verification failed

    # --- Recovery phase ---
    RECOVERY_FAILED = "recovery_failed"          # Fault recovery failed
    RECOVERY_VERIFICATION_TIMEOUT = "recovery_verification_timeout"  # Recover verifier max iterations

    # --- System ---
    INTERNAL_ERROR = "internal_error"            # Unhandled exception


# ---------------------------------------------------------------------------
# LLM diagnosis extraction for failure_reason enrichment
# ---------------------------------------------------------------------------

_DIAGNOSIS_FALLBACK = "未能从 Agent 推理记录中提取失败根因分析"


def extract_llm_diagnosis(messages: list, max_length: int = 500) -> str:
    """Extract the last meaningful AI text from messages as failure diagnosis.

    Scans *messages* in reverse to find the most recent AIMessage with
    substantive text content (>= 20 chars, not just tool-call stubs).
    Falls back to ``reasoning_content`` when ``content`` is empty (thinking
    mode).  Returns ``_DIAGNOSIS_FALLBACK`` if nothing qualifies.
    """
    for msg in reversed(messages):
        if not (hasattr(msg, "type") and msg.type == "ai"):
            continue
        # Skip messages that are purely tool calls with no text
        if hasattr(msg, "tool_calls") and msg.tool_calls and not getattr(msg, "content", ""):
            continue

        text = ""
        content = getattr(msg, "content", "")
        if isinstance(content, str) and len(content) >= 20:
            text = content
        else:
            # Thinking-mode fallback: reasoning_content
            additional = getattr(msg, "additional_kwargs", {}) or {}
            rc = additional.get("reasoning_content", "")
            if isinstance(rc, str) and len(rc) >= 20:
                text = rc

        if text:
            if len(text) > max_length:
                return text[:max_length] + "..."
            return text

    return _DIAGNOSIS_FALLBACK


def enrich_failure_reason(
    base_reason: str, messages: list, max_length: int = 500
) -> str:
    """Append LLM diagnosis to a templated failure_reason string."""
    diagnosis = extract_llm_diagnosis(messages, max_length)
    return f"{base_reason} | llm_analysis: {diagnosis}"


def should_auto_replan(error_message: str) -> bool:
    """Pattern-match blade/kubectl error messages to determine replan eligibility.

    Returns True if the error message suggests a problem that Phase 1 could
    re-investigate with its richer tool set (e.g., target disappeared,
    parameter mismatch).
    """
    msg_lower = error_message.lower()
    if any(p in msg_lower for p in _NON_REPLANABLE_PATTERNS):
        return False
    return any(p in msg_lower for p in _REPLANABLE_PATTERNS)
