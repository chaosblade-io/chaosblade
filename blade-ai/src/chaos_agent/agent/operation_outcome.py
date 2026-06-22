"""Accessors for operation verification and outcome fields.

The physical AgentState still keeps inject and recover verification in
separate legacy-compatible fields:

* ``verification`` for injection
* ``recover_verification`` for recovery

This module is the semantic boundary for reading/writing those fields so
result builders and terminal nodes do not accidentally cross the two lanes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping


_MISSING = object()


@dataclass(frozen=True)
class OperationOutcome:
    """UI/API-facing operation outcome extracted from graph state."""

    result: dict[str, Any] | None
    error: str
    failure_reason: str
    failure_detail: dict[str, Any] | None
    postmortem: Any | None
    finished_at: str


def _copy_dict(value: Any) -> dict[str, Any] | None:
    return deepcopy(value) if isinstance(value, dict) else None


def _copy_optional(value: Any) -> Any:
    return deepcopy(value) if value is not None else None


def read_inject_verification(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the injection verification result from state."""

    return _copy_dict(state.get("verification"))


def read_recover_verification(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the recovery verification result from state."""

    return _copy_dict(state.get("recover_verification"))


def read_verification_side_effects(verification: Mapping[str, Any] | None) -> Any | None:
    """Return side-effect evidence embedded in a verification dict."""

    if not isinstance(verification, Mapping):
        return None
    return _copy_optional(verification.get("side_effects"))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def build_verification_simple(verification: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Flatten a verification dict into the compact API/memory projection."""

    if not isinstance(verification, Mapping) or not verification:
        return None

    layer1 = _as_mapping(verification.get("layer1"))
    layer2 = _as_mapping(verification.get("layer2"))
    result: dict[str, Any] = {
        "level": verification.get("level", "unknown"),
        "layer1": {
            "status": layer1.get("status", "unknown"),
        },
        "layer2": {
            "status": layer2.get("status", "unknown"),
        },
        "baseline_confidence": verification.get("baseline_confidence", "none"),
        "baseline_used": verification.get("baseline_used"),
    }

    warnings = verification.get("warnings")
    if warnings:
        result["warnings"] = _copy_optional(warnings)

    checklist = verification.get("checklist", {})
    items = checklist.get("items", []) if isinstance(checklist, Mapping) else []
    if items:
        result["evidence"] = [
            {
                "step": it.get("step"),
                "status": it.get("status"),
                "detail": it.get("evidence", ""),
            }
            for it in items
            if isinstance(it, Mapping)
        ]

    layer2_details = layer2.get("details", "")
    if layer2_details:
        result["evidence_summary"] = layer2_details

    return result


def read_failure_reason(state: Mapping[str, Any]) -> str:
    """Read or derive the canonical failure reason string."""

    reason = state.get("failure_reason") or ""
    if reason:
        return str(reason)

    detail = state.get("failure_detail")
    if not isinstance(detail, dict):
        return ""

    try:
        from chaos_agent.agent.verdict import FailureDetail

        return FailureDetail.model_validate(detail).to_reason_string()
    except Exception:
        return str(detail.get("category") or "")


def read_merged_error(state: Mapping[str, Any]) -> str:
    """Return the backward-compatible error string shown to old consumers."""

    return read_failure_reason(state) or str(state.get("error") or "")


def read_operation_outcome(state: Mapping[str, Any]) -> OperationOutcome:
    """Extract common terminal outcome fields from state."""

    failure_detail = state.get("failure_detail")
    return OperationOutcome(
        result=_copy_dict(state.get("result")),
        error=read_merged_error(state),
        failure_reason=read_failure_reason(state),
        failure_detail=_copy_dict(failure_detail),
        postmortem=_copy_optional(state.get("postmortem")),
        finished_at=str(state.get("finished_at") or ""),
    )


def write_inject_verification(
    result_update: Mapping[str, Any] | None = None,
    *,
    result: Any = _MISSING,
    verification: Any = _MISSING,
    inject_verification_summary: Any = _MISSING,
    finished_at: Any = _MISSING,
) -> dict[str, Any]:
    """Return an update dict with injection verification fields written."""

    update = dict(result_update or {})
    if result is not _MISSING:
        update["result"] = _copy_optional(result)
    if verification is not _MISSING:
        update["verification"] = _copy_optional(verification)
    if inject_verification_summary is not _MISSING:
        update["inject_verification_summary"] = inject_verification_summary
    if finished_at is not _MISSING:
        update["finished_at"] = finished_at
    return update


def write_recover_verification(
    result_update: Mapping[str, Any] | None = None,
    *,
    result: Any = _MISSING,
    verification: Any = _MISSING,
    finished_at: Any = _MISSING,
) -> dict[str, Any]:
    """Return an update dict with recovery verification fields written."""

    update = dict(result_update or {})
    if result is not _MISSING:
        update["result"] = _copy_optional(result)
    if verification is not _MISSING:
        update["recover_verification"] = _copy_optional(verification)
    if finished_at is not _MISSING:
        update["finished_at"] = finished_at
    return update
