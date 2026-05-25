"""Public response schemas: JSONEnvelope, ResponseStatus, ResponseCode.

Shared across CLI (runner), agent, and server — do NOT depend on
server-specific modules here.
"""

import uuid
from enum import IntEnum
from typing import Optional

from pydantic import BaseModel, Field

from chaos_agent.utils.time import now_iso


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ResponseStatus(str):
    """High-level outcome indicator for every API/CLI response."""

    SUCCESS = "success"
    FAIL = "fail"


class ResponseCode(IntEnum):
    """Numeric error codes used across all responses.

    0       = success
    1xxx    = client / validation errors
    2xxx    = not-found / lookup errors
    3xxx    = (reserved)
    4xxx    = operation / recovery errors
    5xxx    = internal / runtime errors
    """

    # Success
    OK = 0

    # Client / validation (1xxx)
    INVALID_ACTION = 1001
    # Validation failure for body / query / path params on the new
    # control-plane endpoints (Phase 3a /config /memory /compact).
    # Distinct from INVALID_ACTION which the legacy injection routes
    # already use for action-string rejection.
    INVALID_PARAMS = 1002

    # Not-found (2xxx)
    TASK_NOT_FOUND = 2001

    # Permission / rejected (3xxx)
    SAFETY_REJECTED = 3001
    USER_REJECTED = 3002

    # Operation / recovery (4xxx)
    RECOVERY_FAILED = 4001
    INJECTION_FAILED = 4002

    # Internal / runtime (5xxx)
    NO_BLADE_UID = 5000
    SERVER_SHUTTING_DOWN = 5001
    # Backend booted but the LLM-bound agent graph isn't built yet —
    # essential config (api_key / model / base_url) is still missing.
    # The TUI BootRunner reads this code to redirect the user into
    # the wizard instead of surfacing the underlying OpenAIError.
    NEEDS_SETUP = 5002
    # Generic internal failure — disk write, lock contention, etc.
    # Used by /config / /memory / /compact handlers when the client
    # bears no fault but the operation still couldn't complete.
    INTERNAL_ERROR = 5099


# ---------------------------------------------------------------------------
# JSONEnvelope
# ---------------------------------------------------------------------------


def _new_request_id() -> str:
    """Generate a new request ID."""
    return str(uuid.uuid4())


def _new_timestamp() -> str:
    """Generate a new ISO 8601 Beijing-time timestamp."""
    return now_iso()


class JSONEnvelope(BaseModel):
    """Unified JSON output envelope for all responses.

    Fields:
      - status: "success" or "fail" — high-level outcome indicator
      - code: numeric error code (0 = success, non-zero = specific error)
      - message: human-readable message (error reason, hint, etc.)
      - data: response payload (None for error responses)
      - request_id: unique request identifier (auto-generated if empty)
      - timestamp: ISO 8601 timestamp (auto-generated)
    """

    status: str = ResponseStatus.SUCCESS
    code: int = ResponseCode.OK
    message: str = "success"
    data: Optional[dict] = None
    request_id: str = ""
    timestamp: str = Field(default_factory=_new_timestamp)

    @classmethod
    def ok(cls, data=None, message: str = "success", code: int = ResponseCode.OK, request_id: str = "") -> dict:
        """Build a success envelope dict.

        request_id is auto-generated when not provided.
        """
        return cls(
            status=ResponseStatus.SUCCESS,
            code=code,
            message=message,
            data=data,
            request_id=request_id or _new_request_id(),
        ).model_dump(mode="json")

    @classmethod
    def fail(cls, code: int, message: str, request_id: str = "", data=None) -> dict:
        """Build a failure envelope dict.

        request_id is auto-generated when not provided.
        """
        return cls(
            status=ResponseStatus.FAIL,
            code=code,
            message=message,
            data=data,
            request_id=request_id or _new_request_id(),
        ).model_dump(mode="json")


def build_inject_envelope(
    inject_data: dict,
    task_state: str,
    failure_reason: str = "",
) -> dict:
    """Build inject result envelope with correct success/fail semantics.

    Uses JSONEnvelope.fail() when task_state indicates failure,
    JSONEnvelope.ok() otherwise. The inject_data is always included
    in the data field for diagnostic purposes.
    """
    if task_state == "failed":
        code = ResponseCode.INJECTION_FAILED
        if failure_reason:
            fr_lower = failure_reason.lower()
            if "safety_rejected" in fr_lower:
                code = ResponseCode.SAFETY_REJECTED
            elif "user_rejected" in fr_lower:
                code = ResponseCode.USER_REJECTED
            elif "execution_timeout" in fr_lower:
                code = ResponseCode.NO_BLADE_UID
        message = failure_reason[:200] if failure_reason else "Injection failed"
        return JSONEnvelope.fail(code=code, message=message, data=inject_data)
    return JSONEnvelope.ok(data=inject_data)
