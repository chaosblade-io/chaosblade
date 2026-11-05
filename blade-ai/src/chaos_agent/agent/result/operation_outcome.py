"""Accessors for operation verification and outcome fields.

The physical AgentState still keeps inject and recover verification in
separate legacy-compatible fields:

* ``verification`` for injection
* ``recover_verification`` for recovery

This module is the semantic boundary for reading/writing those fields so
result builders and terminal nodes do not accidentally cross the two lanes.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from chaos_agent.agent.result.verdict import (
    INJECT_VERDICT_VALUES,
    RECOVER_VERDICT_VALUES,
    InjectVerdict,
    Layer1Result,
    RecoverVerdict,
    layer1_to_dict,
)

logger = logging.getLogger(__name__)


_MISSING = object()


@dataclass(frozen=True)
class OperationOutcome:
    """UI/API-facing operation outcome extracted from graph state."""

    result: dict[str, Any] | None
    error: str
    failure_reason: str
    failure_detail: dict[str, Any] | None
    postmortem: Any | None
    issue_report: Any | None
    finished_at: str


def _copy_dict(value: Any) -> dict[str, Any] | None:
    return deepcopy(value) if isinstance(value, dict) else None


def _copy_optional(value: Any) -> Any:
    return deepcopy(value) if value is not None else None


def _gate_verification_level(
    verification: dict[str, Any] | None,
    closed_set: frozenset[str],
    clamp_to: str,
    domain: str,
) -> dict[str, Any] | None:
    """Read-side level closed-set gate (B76 round-15 D5).

    A verification read back from state/persistence with a level OUTSIDE
    its closed set (legacy fossil word, foreign writer) is clamped to the
    domain's fail-closed word and warned — mirroring the write-path clamps
    (inject finalize → "unverified", recover parse → "unrecovered") so
    both sides of the boundary enforce the same vocabulary. A MISSING
    level key passes through unchanged (schema tolerance for legacy
    states); the underlying state dict is NEVER rewritten — the gate
    operates on the defensive copy ``_copy_dict`` already made.
    """
    if not isinstance(verification, dict) or "level" not in verification:
        return verification
    level = verification["level"]
    if isinstance(level, str) and level in closed_set:
        return verification
    logger.warning(
        "%s verification level %r outside closed set; clamped to %r "
        "(read-side gate, underlying state untouched)",
        domain,
        level,
        clamp_to,
    )
    verification["level"] = clamp_to
    return verification


def read_inject_verification(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the injection verification result from state.

    The level passes a read-side closed-set gate (round-15 D5): an
    out-of-set word (e.g. a legacy fossil) clamps to "unverified" — the
    same word the inject finalize write-clamp uses.
    """

    verification = _copy_dict(state.get("verification"))
    return _gate_verification_level(
        verification,
        INJECT_VERDICT_VALUES,
        InjectVerdict.UNVERIFIED.value,
        "inject",
    )


def verification_floor_from_layer1(
    state: Mapping[str, Any],
    layer1: Any,
    *,
    layer2_details: str | None = None,
    extra_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic injection-verification floor derived from Layer 1 alone.

    Single source for the "injection landed (or did not) but the semantic
    Layer 2 verdict never completed" outcome. Two consumers share it:

    * the no-LLM ``verifier()`` node — its whole verdict IS this floor; and
    * ``terminal_task_state`` — a run that ENDED without a verdict (e.g. an
      LLM ``httpx.ReadTimeout`` mid verifier loop) but left durable Layer 1
      evidence in the checkpoint, so the terminal word is derived from what
      was actually MEASURED, never from whether the LLM's final emission
      survived the stream.

    The floor is CONSERVATIVE by construction: it yields only ``partial``
    (Layer 1 confirms the experiment ran) or ``unverified`` (it does not).
    It NEVER claims ``verified`` — confirming the fault EFFECT is observable
    is Layer 2's (the LLM's) job and this function refuses to impersonate
    it. So a crashed-with-evidence run reads exactly like a run that
    honestly concluded ``partial``, and no interruption can upgrade it to a
    stronger claim than the durable evidence supports.

    ``layer2_details`` / ``extra_warnings`` let a caller (the crash path)
    name the interruption without forking the derivation; the defaults
    reproduce the no-LLM verifier's wording byte-for-byte.
    """
    # Lazy import: _verifier_shared imports agent.state at module level, and
    # agent.state imports this module — a module-level edge here would close
    # an import cycle. verdict symbols carry no such edge (imported above).
    from chaos_agent.agent.nodes.verify._verifier_shared import (
        _compute_baseline_confidence,
    )

    is_non_chaosblade = layer1.status == "skipped"
    is_expired = bool(getattr(layer1, "expired", False))
    if is_non_chaosblade:
        level = InjectVerdict.UNVERIFIED.value
    elif is_expired:
        level = InjectVerdict.PARTIAL.value
    else:
        level = (
            InjectVerdict.PARTIAL.value
            if layer1.is_passed()
            else InjectVerdict.UNVERIFIED.value
        )

    if is_expired:
        l2_status = "recovered_before_observation"
        default_details = (
            "Fault expired before Layer 2 observation — "
            "recovered_before_observation (no LLM available for specific verification)"
        )
    else:
        l2_status = "skipped"
        default_details = "No LLM available for specific verification"

    if layer1.is_passed():
        warnings = [
            "Layer 2 (fault-specific) verification was skipped. "
            "Only Layer 1 (programmatic) verification was performed."
        ]
    elif is_expired:
        warnings = [
            "Fault experiment record expired (Destroyed/Revoked) before or during "
            "verification, so live fault effects could not be observed by Layer 1. "
            "Layer 2 was unavailable (no LLM) to judge cluster-level evidence."
        ]
    elif is_non_chaosblade:
        warnings = [
            "Native fault: Layer 1 not applicable, Layer 2 skipped (no LLM). "
            "Fault injection could NOT be verified — the fault may not have been injected."
        ]
    else:
        warnings = []
    if extra_warnings:
        warnings = warnings + list(extra_warnings)

    return {
        "level": level,
        "layer1": layer1_to_dict(layer1),
        "layer2": {
            "status": l2_status,
            "details": (
                default_details if layer2_details is None else layer2_details
            ),
        },
        "baseline_confidence": _compute_baseline_confidence(state),
        "warnings": warnings,
    }


def synthesize_inject_verification_floor(
    state: Mapping[str, Any],
    *,
    layer2_details: str | None = None,
    extra_warnings: list[str] | None = None,
) -> dict[str, Any] | None:
    """The durable-state verification floor, or ``None`` when no Layer 1 exists.

    ``None`` means the checkpoint carries no ``inject_layer1_cache`` — the run
    ended before any verifier iteration ran Layer 1, so there is NO durable
    evidence and the caller's fail-closed word must stand (no evidence, no
    upgrade). Otherwise restores the cached Layer 1 and returns
    :func:`verification_floor_from_layer1`.
    """
    cache = state.get("inject_layer1_cache")
    if not cache:
        return None
    try:
        layer1 = Layer1Result.model_validate(cache)
    except Exception:  # noqa: BLE001 — a corrupt cache is "no evidence"
        return None
    return verification_floor_from_layer1(
        state,
        layer1,
        layer2_details=layer2_details,
        extra_warnings=extra_warnings,
    )


def synthesize_recover_verification_floor(
    state: Mapping[str, Any],
    *,
    layer2_details: str | None = None,
) -> dict[str, Any] | None:
    """The durable-state RECOVER verification floor, or ``None``.

    Recover-side twin of :func:`synthesize_inject_verification_floor`
    (a82984a3 root cure, recover arm). ``None`` means the checkpoint carries
    no durable ``recover_layer1_cache`` showing the destroy LANDED, so the
    caller's fail-closed word ("failed") must stand — no evidence, no upgrade.

    Deliberately NARROWER than the inject floor. The inject floor yields a
    word for ANY cached Layer 1 and lets ``infer_task_state`` map it. The
    recover floor is gated on ``layer1.is_passed()`` because
    ``recovery_task_state_from_level`` maps ``level == "unverified"`` →
    UNVERIFIED *unconditionally* (before any layer1 check): producing a floor
    for a FAILED / ERROR / in-progress destroy would therefore under-report a
    KNOWN deterministic failure as mere ignorance. Only a confirmed-landed
    destroy (blade_destroy succeeded, experiment record gone) earns the honest
    floor word.

    The floor word is ``unverified`` — the recovery ACTION landed (Layer 1
    confirms the experiment is destroyed) but the semantic return-to-baseline
    (Layer 2, the LLM's job) never completed. It NEVER claims ``recovered``:
    that word requires ``recovered=True``, a Layer-2/provider signal this
    floor refuses to impersonate — the same over-claim boundary the inject
    floor holds (it never claims ``verified``).
    """
    cache = state.get("recover_layer1_cache")
    if not cache:
        return None
    try:
        layer1 = Layer1Result.model_validate(cache)
    except Exception:  # noqa: BLE001 — a corrupt cache is "no evidence"
        return None
    if not layer1.is_passed():
        # Destroy did not land (failed/error/in-progress/skipped) — no durable
        # positive evidence, so the fail-closed word stands. Returning a floor
        # here would let recovery_task_state_from_level's unconditional
        # "unverified" branch mask a known failure.
        return None

    # Lazy import: _verifier_shared imports agent.state at module level, and
    # agent.state imports this module — a module-level edge here would close
    # an import cycle (same rationale as verification_floor_from_layer1).
    from chaos_agent.agent.nodes.verify._verifier_shared import (
        _compute_baseline_confidence,
    )

    return {
        "level": RecoverVerdict.UNVERIFIED.value,
        "layer1": layer1_to_dict(layer1),
        "layer2": {
            "status": "skipped",
            "details": (
                "Recovery action (destroy) confirmed by Layer 1, but the run "
                "ended before Layer 2 semantic verification completed."
                if layer2_details is None
                else layer2_details
            ),
        },
        "baseline_confidence": _compute_baseline_confidence(state),
        "warnings": [
            "Layer 1 confirms the experiment was destroyed, but Layer 2 "
            "(semantic return-to-baseline) never completed — the run ended "
            "mid-adjudication. Recovery is UNVERIFIED, not confirmed, and not "
            "failed: the destroy landed."
        ],
    }


def read_recover_verification(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the recovery verification result from state.

    The level passes a read-side closed-set gate (round-15 D5): an
    out-of-set word (e.g. the legacy "failed" fossil — its historical
    "still failed" reading is what the counter-evidence word
    "unrecovered" now spells) clamps to "unrecovered" — the same word
    the recover parse write-clamp uses. The pre-gate coincidence (every
    downstream consumer treats "failed" ∉ success sets, so the fossil
    accidentally behaved like "unrecovered") is now an explicit
    contract instead of luck.
    """

    verification = _copy_dict(state.get("recover_verification"))
    return _gate_verification_level(
        verification,
        RECOVER_VERDICT_VALUES,
        RecoverVerdict.UNRECOVERED.value,
        "recover",
    )


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
        from chaos_agent.agent.result.verdict import FailureDetail

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
        issue_report=_copy_optional(state.get("issue_report")),
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
