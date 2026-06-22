"""Error classification hierarchy for Blade AI.

Errors are classified by severity:
- TRANSIENT: Temporary failures that can be retried (network timeout, API rate limit)
- PERMANENT: Unrecoverable failures that should fail immediately (invalid target, safety block)
- RECOVERABLE: Failures that can be handled by degradation (context overflow triggers compaction)
"""

from enum import Enum
from typing import NamedTuple


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


# ---------------------------------------------------------------------------
# LLM diagnosis extraction (used by state_helpers.fail_state)
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

        content = getattr(msg, "content", "")

        # When AIMessage has tool_calls but no text content, try extracting
        # diagnosis from tool_call args (e.g. finish_planning rejection_reason)
        if hasattr(msg, "tool_calls") and msg.tool_calls and not content:
            for tc in msg.tool_calls:
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                if not isinstance(args, dict):
                    continue
                diagnosis = (
                    args.get("rejection_reason")
                    or args.get("reason")
                    or args.get("analysis")
                    or args.get("explanation")
                    or ""
                )
                if isinstance(diagnosis, str) and len(diagnosis) >= 20:
                    if len(diagnosis) > max_length:
                        return diagnosis[:max_length] + "..."
                    return diagnosis
            continue  # tool_calls had nothing useful, keep scanning

        text = ""
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




def should_auto_replan(error_message: str) -> bool:
    """Legacy boolean classifier — replanable / not replanable.

    Implemented on top of :func:`classify_error` so the new richer
    classification is the source of truth. Kept for callers that
    haven't migrated to the new ``ErrorAction`` API.
    """
    return classify_error(error_message).action == ErrorAction.REPLAN


# ---------------------------------------------------------------------------
# Hierarchical error classification — single source of truth for "what
# should the router do with this error". The legacy ``_REPLANABLE_PATTERNS``
# / ``_NON_REPLANABLE_PATTERNS`` lists above are preserved as-is for any
# direct readers, but ``classify_error`` is the recommended API.
# ---------------------------------------------------------------------------


class ErrorClass(Enum):
    """Hierarchical error categorisation for the inject pipeline.

    Each class maps deterministically to an :class:`ErrorAction` via
    :data:`_CLASS_TO_ACTION`, so adding a new pattern only requires
    editing one table — the router itself stays generic.
    """

    INFRA_PERSISTENT = "infra_persistent"
    """Infrastructure problem retry can't fix.

    Examples: node ``DiskPressure``, RBAC denial, kubeconfig token
    expired. Operator action required offline.
    """

    INFRA_TRANSIENT = "infra_transient"
    """Infrastructure blip — same call may succeed seconds later.

    Examples: ``timeout``, ``connection refused``, ``bad file
    descriptor`` (kubeconfig handle), ``no such host``.
    """

    INTERFACE_MISMATCH = "interface_mismatch"
    """Tool rejected the command syntax — the caller's model of the tool
    interface is wrong.

    Examples: ``unknown flag``, ``unknown command``, ``flag provided but
    not defined``.  Distinguished from :attr:`USER_CONFIG` because the
    rejected parameter/subcommand does NOT exist in the tool and must
    never be retried; ``USER_CONFIG`` means the parameter exists but the
    value or usage is wrong.
    """

    USER_CONFIG = "user_config"
    """Bad LLM-generated args; replan with Phase 1's richer tools fixes.

    Examples: ``invalid parameter``, ``invalid argument``, regex mismatch.
    """

    TARGET_GONE = "target_gone"
    """Target object disappeared since planning; replan w/ new target.

    Examples: ``resource not found``, ``no matches for kind``.
    """

    QUOTA_EXCEEDED = "quota_exceeded"
    """Cluster-level resource limit hit; not auto-recoverable.

    Examples: ``ResourceQuota exceeded``, ``limit exceeded``.
    """

    AUTH_DENIED = "auth_denied"
    """Authentication / authorisation failure; needs operator fix.

    Examples: ``permission denied``, ``forbidden``, ``unauthorized``,
    ``x509 certificate expired``.
    """

    UNKNOWN = "unknown"
    """No pattern matched — conservative default to ``END_FAILED``.

    Choosing END_FAILED over CONTINUE is intentional: the previous
    binary classifier returned False on unknown errors which the
    router translated as "keep going", producing the user-reported
    "loop forever" symptom on infrastructure failures we hadn't
    catalogued yet.
    """


class ErrorAction(Enum):
    """Canonical router action for a classified error."""

    REPLAN = "replan"
    """Phase 1 with new context can probably fix this."""

    SHORT_RETRY = "retry"
    """Same call may succeed shortly — let the LLM re-issue it.

    Subject to a separate retry counter (``settings.max_transient_retry``)
    so genuinely persistent transients still terminate.
    """

    END_FAILED = "end"
    """Stop the graph and report failure."""

    ASK_USER = "ask_user"
    """Need a human decision (auth, quota). Routes to ``reject`` node."""


_CLASS_TO_ACTION: dict[ErrorClass, ErrorAction] = {
    ErrorClass.INTERFACE_MISMATCH: ErrorAction.REPLAN,
    ErrorClass.USER_CONFIG: ErrorAction.REPLAN,
    ErrorClass.TARGET_GONE: ErrorAction.REPLAN,
    ErrorClass.INFRA_TRANSIENT: ErrorAction.SHORT_RETRY,
    ErrorClass.INFRA_PERSISTENT: ErrorAction.END_FAILED,
    ErrorClass.QUOTA_EXCEEDED: ErrorAction.ASK_USER,
    ErrorClass.AUTH_DENIED: ErrorAction.ASK_USER,
    ErrorClass.UNKNOWN: ErrorAction.END_FAILED,
}


# Pattern lists — order matters within the outer list (first match wins
# across classes). Patterns are matched case-insensitively as substrings.
_CLASSIFY_RULES: list[tuple[ErrorClass, list[str]]] = [
    # Auth must come first — "x509: certificate expired" contains
    # ``timeout``-like words in some forms; classifying as auth is safer.
    (
        ErrorClass.AUTH_DENIED,
        [
            "permission denied",
            "forbidden",
            "unauthorized",
            "x509",
            "certificate has expired",
            "certificate verify failed",
            "tls handshake failure",
        ],
    ),
    (
        ErrorClass.QUOTA_EXCEEDED,
        [
            "exceeded quota",
            "resourcequota",
            "limit exceeded",
            "exceeded the resource limit",
        ],
    ),
    (
        ErrorClass.TARGET_GONE,
        [
            "resource not found",
            "target not found",
            "not found",
            "no matches for kind",
            "no resources found",
        ],
    ),
    (
        ErrorClass.INTERFACE_MISMATCH,
        [
            "unknown flag",
            "unknown command",
            "unknown shorthand flag",
            "flag provided but not defined",
        ],
    ),
    (
        ErrorClass.USER_CONFIG,
        [
            "invalid parameter",
            "flag mismatch",
            "unsupported flag",
            "invalid argument",
            "validation error",
        ],
    ),
    (
        ErrorClass.INFRA_PERSISTENT,
        # Node / system pressure conditions persist; retrying the same
        # call accomplishes nothing. Kubernetes "Evicted" is here for
        # the same reason — the pod isn't coming back without a node fix.
        [
            "diskpressure",
            "memorypressure",
            "networkunavailable",
            "pidpressure",
            "node not ready",
            "evicted",
        ],
    ),
    (
        ErrorClass.INFRA_TRANSIENT,
        # ``bad file descriptor`` is the kubeconfig-handle reuse bug
        # that motivated this whole patch. ``i/o timeout`` covers the
        # generic golang client behaviour. Add new transient signatures
        # here as they're observed in production logs.
        [
            "timeout",
            "deadline exceeded",
            "connection refused",
            "connection reset",
            "bad file descriptor",
            "no such host",
            "i/o timeout",
            "tls handshake timeout",
            "server gave http response to https client",
            "broken pipe",
            "eof",
        ],
    ),
]


class ClassificationResult(NamedTuple):
    """Outcome of :func:`classify_error`.

    Attributes:
        error_class: Bucketed category of the error.
        action: Recommended router action.
        matched_pattern: The substring that triggered the match
            (``None`` for ``UNKNOWN``). Useful for telemetry / logs.
    """

    error_class: ErrorClass
    action: ErrorAction
    matched_pattern: str | None


def classify_error(error_message: str | None) -> ClassificationResult:
    """Classify an error message into ``(ErrorClass, ErrorAction, pattern)``.

    The classification is deterministic: the first matching pattern in
    :data:`_CLASSIFY_RULES` wins. Empty / ``None`` input → ``UNKNOWN``.

    Returns a :class:`ClassificationResult` so callers can log the
    structured class while routing on the action. Never raises.
    """
    if not error_message:
        return ClassificationResult(
            ErrorClass.UNKNOWN, _CLASS_TO_ACTION[ErrorClass.UNKNOWN], None
        )
    msg_lower = error_message.lower()
    for ec, patterns in _CLASSIFY_RULES:
        for p in patterns:
            if p in msg_lower:
                return ClassificationResult(ec, _CLASS_TO_ACTION[ec], p)
    return ClassificationResult(
        ErrorClass.UNKNOWN, _CLASS_TO_ACTION[ErrorClass.UNKNOWN], None
    )
