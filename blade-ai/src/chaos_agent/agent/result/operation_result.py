"""Canonical result-data builders for inject and recover operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from chaos_agent.agent.spec.fault_spec import (
    FaultSpec,
    fault_type_from_state,
    legacy_params_dict,
    legacy_target_dict,
    read_fault_spec,
)
from chaos_agent.agent.result.operation_outcome import (
    build_verification_simple,
    read_inject_verification,
    read_operation_outcome,
    read_recover_verification,
    read_verification_side_effects,
)
from chaos_agent.agent.result.verdict import RecoverVerdict
from chaos_agent.agent.state import (
    TaskState,
    extract_ui_diagnostics,
    materialize_fault_handle,
    recovery_task_state_from_level,
    strip_side_effects,
    terminal_task_state,
)


def pending_vehicle_teardown(values: Mapping[str, Any]) -> list[str]:
    """Registered-but-uncleaned vehicle artifacts at task end (inject-dfee9d3d).

    Single source for the task-end teardown fact: a long-window recovery
    carrier (deadline after task end) is deadline-protected out of the
    finalize sweep, so its RBAC members live on with no system-side
    trigger after the timer fires. The recover graph's finalize node
    re-runs the same sweep with the deadline passed, so ONE ``blade-ai
    recover`` call collects the whole stack (idempotent,
    ``--ignore-not-found``). Rendered by every terminal builder consumer
    (CLI hint, SSE envelope field, task JSON audit) — implement a new
    terminal display by reading this field, never by re-deriving the
    pending list at another construction site.
    """
    # Lazy import — result layer stays free of agent-node imports at
    # import time (build_inject_context / FaultProviderRegistry precedents).
    from chaos_agent.agent.execution_artifacts import VEHICLE_ARTIFACT_TYPES

    pending: list[str] = []
    for artifact in values.get("execution_artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("type") not in VEHICLE_ARTIFACT_TYPES:
            continue
        if artifact.get("status") == "cleaned":
            continue
        name = str(artifact.get("name") or "")
        ns = str(artifact.get("namespace") or "")
        pending.append(
            f"{artifact.get('type')}:{ns}/{name}" if name else str(artifact.get("type"))
        )
    return pending


def _fault_spec_dict(values: Mapping[str, Any]) -> dict[str, Any]:
    spec = read_fault_spec(dict(values or {}))
    return spec.to_dict() if spec else {}


def build_recovery_handle(values: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a carrier-agnostic recovery handle for a fault.

    Recovery-side view of ``fault_handle``: a fault is undone either via its
    ChaosBlade destroy UID (blade-family carriers) or via reverse operations
    over recorded execution artifacts (native carriers). Returns ``None``
    when neither handle is available.
    """
    state = dict(values or {})
    handle = materialize_fault_handle(state)
    # Lazy import — keep the result layer free of provider-package module
    # imports at import time (matches the build_inject_context precedent).
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    if handle and FaultProviderRegistry.is_experiment_handle(handle):
        # Experiment-carrier UID pinning. Keep the historically pinned
        # consumer shape — kind / value / experiment_uid (the legacy
        # ``experiment_uid`` key stays the permanent external contract).
        # Membership ("is this an experiment handle?") is the owning
        # provider's ``has_experiment_uid`` declaration via the registry,
        # not a ``kind == "experiment_uid"`` string branch, so a future
        # experiment carrier pins automatically (phase-7 T6).
        uid = handle.get("value", "")
        return {"kind": handle.get("kind", ""), "value": uid, "experiment_uid": uid}
    artifacts = list(state.get("execution_artifacts") or [])
    if artifacts:
        return {"kind": "artifact", "artifacts": artifacts}
    if handle:
        return dict(handle)
    return None


def build_inject_data_from_state(
    values: Mapping[str, Any],
    task_id: str,
    *,
    elapsed_ms: int = 0,
) -> dict[str, Any]:
    """Build the result-card data dict for an inject graph state."""

    state_values = dict(values or {})
    verification = read_inject_verification(state_values)
    # ``terminal_task_state`` (not ``infer_task_state``): every caller of this
    # builder is a terminal point, and without a verdict the run is failed —
    # see that helper for why a creation handle is not evidence of effect.
    task_state = terminal_task_state(state_values)

    outcome = read_operation_outcome(state_values)

    diagnostics: dict[str, Any] = {}
    try:
        diagnostics = extract_ui_diagnostics(state_values) or {}
    except Exception:
        pass

    # Durable inject facts for recover context reconstruction.  Persisting
    # them here (finalize time) makes recover independent of message-scan
    # heuristics that compaction can silently degrade (durable-first, scan
    # as fallback).  inject_context is pre-built from the FINAL message list
    # so later reads never re-derive it from a truncated/compacted history.
    inject_context = ""
    try:
        from chaos_agent.utils.inject_context import build_inject_context

        inject_context = build_inject_context(list(state_values.get("messages") or []))
    except Exception:
        inject_context = ""

    experiment_uid_out = state_values.get("experiment_uid") or ""
    return {
        "task_id": task_id,
        "task_state": task_state,
        "fault_type": fault_type_from_state(state_values),
        "experiment_uid": experiment_uid_out,
        # Attribution facts persisted for recover hydration (R4): a native
        # fault has no UID, so the method + materialized handle are the only
        # durable identity that survives across a process restart. TaskSnapshot
        # feeds them back into the recover initial state.
        "injection_method": state_values.get("injection_method"),
        "fault_handle": materialize_fault_handle(state_values),
        "recovery_handle": build_recovery_handle(state_values),
        "duration_ms": elapsed_ms,
        "fault_spec": _fault_spec_dict(state_values),
        "target": legacy_target_dict(state_values),
        "params": legacy_params_dict(state_values),
        "execution_artifacts": list(state_values.get("execution_artifacts") or []),
        "verification": strip_side_effects(verification),
        "side_effects": read_verification_side_effects(verification),
        "blast_radius_detail": str(state_values.get("blast_radius_detail") or ""),
        "inject_context": inject_context,
        "postmortem": outcome.postmortem,
        "issue_report": outcome.issue_report,
        "error": outcome.error,
        # Task-end teardown fact, single-sourced (inject-dfee9d3d, R13-1):
        # every terminal builder consumer (CLI hint, SSE envelope, task
        # JSON audit) reads this field instead of re-deriving the pending
        # list — "did this task leave uncollected vehicles" must have ONE
        # answer, not per-construction-site copies.
        "vehicle_teardown_pending": pending_vehicle_teardown(state_values),
        # Write-set boundary exit payload (unattended widened contract):
        # manifest entries verbatim + interactive re-run guidance,
        # machine-readable for pipeline consumption. Absent for every
        # other outcome.
        **({"write_set_boundary": state_values["write_set_boundary"]}
           if state_values.get("write_set_boundary") else {}),
        **diagnostics,
    }


def build_unknown_inject_data(
    task_id: str,
    *,
    task_state: str = "unknown",
    experiment_uid: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Build a minimal complete result-card data dict when graph state is absent."""

    return {
        "task_id": task_id,
        "task_state": task_state,
        "fault_type": "",
        "experiment_uid": experiment_uid or "",
        "injection_method": None,
        "fault_handle": None,
        "duration_ms": 0,
        "fault_spec": {},
        "target": {},
        "params": {},
        "execution_artifacts": [],
        "verification": None,
        "side_effects": None,
        "blast_radius_detail": "",
        "inject_context": "",
        "postmortem": None,
        "issue_report": None,
        "error": error or "",
    }


def _state_with_fault_spec(
    values: Mapping[str, Any] | None,
    fault_spec: FaultSpec | Mapping[str, Any] | None,
) -> dict[str, Any]:
    state = dict(values or {})
    if isinstance(fault_spec, FaultSpec):
        state["fault_spec"] = fault_spec.to_dict()
    elif isinstance(fault_spec, Mapping):
        state["fault_spec"] = dict(fault_spec)
    return state


def build_inject_status_data_from_state(
    values: Mapping[str, Any] | None,
    task_id: str,
    *,
    result: str,
    error: str = "",
    experiment_uid: str | None = None,
    fault_spec: FaultSpec | Mapping[str, Any] | None = None,
    include_experiment_uid: bool = True,
) -> dict[str, Any]:
    """Build the legacy pending/failed inject status data shape."""

    state = _state_with_fault_spec(values, fault_spec)
    data: dict[str, Any] = {
        "task_id": task_id,
        "result": result,
        "fault_type": fault_type_from_state(state),
        "targets": target_list_from_state(state),
    }
    if include_experiment_uid:
        experiment_uid_out = (
            experiment_uid
            if experiment_uid is not None
            else state.get("experiment_uid")
        ) or ""
        data["experiment_uid"] = experiment_uid_out
    if error:
        data["error"] = error
    return data


def recover_task_state_from_values(values: Mapping[str, Any]) -> str:
    """Return the recover lifecycle state from a recover graph state.

    Single-sourced through :func:`recovery_task_state_from_level`
    (round-15 D3): this used to be implementation B of three parallel
    truth-table copies — it read only the result mirror and lost the
    non-ChaosBlade L1-skipped override. The verification dict is
    authoritative for the level (D4); the result mirror stays the
    fallback for legacy states persisted before the verification dict.
    """

    outcome = read_operation_outcome(values)
    result = outcome.result or {}
    if not isinstance(result, Mapping):
        result = {}

    verification = read_recover_verification(values)
    verification = verification if isinstance(verification, Mapping) else {}
    layer1 = verification.get("layer1")
    layer1_status = layer1.get("status", "") if isinstance(layer1, Mapping) else ""
    is_recovered = bool(result.get("recovered", False))
    level = verification.get("level") or result.get(
        "recovery_level",
        "recovered" if is_recovered else "failed",
    )
    return recovery_task_state_from_level(
        level,
        recovered=is_recovered,
        layer1_status=layer1_status,
    )


# Legacy CLI ``result`` label per terminal recover task_state. "failed" is
# the legacy label word — round-14 removed it from the RecoverVerdict
# domain (step-level failure lives in Layer1/Layer2 status); the CLI label
# contract predates that and keeps the fossil word.
_RECOVER_LABEL_BY_TASK_STATE = {
    TaskState.RECOVERED.value: RecoverVerdict.RECOVERED.value,
    TaskState.PARTIAL_RECOVERED.value: RecoverVerdict.PARTIAL.value,
    TaskState.UNVERIFIED.value: RecoverVerdict.UNVERIFIED.value,
    TaskState.FAILED.value: "failed",
}


def recover_result_label_from_values(values: Mapping[str, Any]) -> str:
    """Return the legacy CLI ``result`` label for a recover graph state."""

    task_state = recover_task_state_from_values(values)
    return _RECOVER_LABEL_BY_TASK_STATE.get(task_state, "failed")


def build_recover_data_from_state(
    recover_values: Mapping[str, Any],
    recover_task_id: str,
    inject_state_values: Mapping[str, Any],
    *,
    elapsed_ms: int = 0,
) -> dict[str, Any]:
    """Build the result-card data dict for a recover graph state."""

    recover_state = dict(recover_values or {})
    inject_state = dict(inject_state_values or {})
    return {
        "task_id": recover_task_id,
        "operation": "recover",
        "task_state": recover_task_state_from_values(recover_state),
        "fault_type": fault_type_from_state(inject_state),
        "experiment_uid": inject_state.get("experiment_uid") or "",
        "recovery_handle": build_recovery_handle(inject_state),
        "duration_ms": elapsed_ms,
        "fault_spec": _fault_spec_dict(inject_state),
        "target": legacy_target_dict(inject_state),
        "params": legacy_params_dict(inject_state),
        "verification": strip_side_effects(read_recover_verification(recover_state)),
    }


def target_list_from_state(values: Mapping[str, Any]) -> list[dict[str, str]]:
    """Project a state target into the CLI legacy ``targets`` list shape."""

    target = legacy_target_dict(dict(values or {}))
    names = target.get("names") or []
    namespace = target.get("namespace", "") or ""
    if not namespace:
        namespace = legacy_params_dict(dict(values or {})).get("namespace", "") or ""
    return [{"name": str(name), "namespace": str(namespace)} for name in names]


def build_recover_cli_data_from_state(
    recover_values: Mapping[str, Any],
    inject_task_id: str,
    inject_state_values: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the legacy local-CLI recover response data shape."""

    recover_state = dict(recover_values or {})
    inject_state = dict(inject_state_values or {})
    outcome = read_operation_outcome(recover_state)
    data = {
        "task_id": inject_task_id,
        "result": recover_result_label_from_values(recover_state),
        "experiment_uid": inject_state.get("experiment_uid") or "",
        "targets": target_list_from_state(inject_state),
        "verification": build_verification_simple(
            read_recover_verification(recover_state)
        ),
    }
    if outcome.error:
        data["error"] = outcome.error
    return data


def build_recover_cli_failure_data_from_state(
    inject_task_id: str,
    inject_state_values: Mapping[str, Any],
    *,
    experiment_uid: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Build the legacy local-CLI recover failure data shape."""

    inject_state = dict(inject_state_values or {})
    if experiment_uid and not inject_state.get("experiment_uid"):
        inject_state["experiment_uid"] = experiment_uid
    data = build_recover_cli_data_from_state(
        {"result": {"recovered": False}, "error": error or ""},
        inject_task_id,
        inject_state,
    )
    if error:
        data["error"] = error
    return data
