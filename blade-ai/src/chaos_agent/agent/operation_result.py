"""Canonical result-data builders for inject and recover operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from chaos_agent.agent.fault_spec import (
    FaultSpec,
    fault_type_from_state,
    legacy_params_dict,
    legacy_target_dict,
)
from chaos_agent.agent.operation_outcome import (
    build_verification_simple,
    read_inject_verification,
    read_operation_outcome,
    read_recover_verification,
    read_verification_side_effects,
)
from chaos_agent.agent.state import (
    extract_ui_diagnostics,
    infer_task_state,
    strip_side_effects,
)


def build_inject_data_from_state(
    values: Mapping[str, Any],
    task_id: str,
    *,
    elapsed_ms: int = 0,
) -> dict[str, Any]:
    """Build the result-card data dict for an inject graph state."""

    state_values = dict(values or {})
    task_state = infer_task_state(state_values)
    if task_state == "injecting":
        task_state = "injected" if state_values.get("blade_uid") else "failed"

    verification = read_inject_verification(state_values)
    outcome = read_operation_outcome(state_values)

    diagnostics: dict[str, Any] = {}
    try:
        diagnostics = extract_ui_diagnostics(state_values) or {}
    except Exception:
        pass

    return {
        "task_id": task_id,
        "task_state": task_state,
        "fault_type": fault_type_from_state(state_values),
        "blade_uid": state_values.get("blade_uid", "") or "",
        "duration_ms": elapsed_ms,
        "target": legacy_target_dict(state_values),
        "params": legacy_params_dict(state_values),
        "verification": strip_side_effects(verification),
        "side_effects": read_verification_side_effects(verification),
        "postmortem": outcome.postmortem,
        "error": outcome.error,
        **diagnostics,
    }


def build_unknown_inject_data(
    task_id: str,
    *,
    task_state: str = "unknown",
    blade_uid: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Build a minimal complete result-card data dict when graph state is absent."""

    return {
        "task_id": task_id,
        "task_state": task_state,
        "fault_type": "",
        "blade_uid": blade_uid or "",
        "duration_ms": 0,
        "target": {},
        "params": {},
        "verification": None,
        "side_effects": None,
        "postmortem": None,
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
    blade_uid: str | None = None,
    fault_spec: FaultSpec | Mapping[str, Any] | None = None,
    include_blade_uid: bool = True,
) -> dict[str, Any]:
    """Build the legacy pending/failed inject status data shape."""

    state = _state_with_fault_spec(values, fault_spec)
    data: dict[str, Any] = {
        "task_id": task_id,
        "result": result,
        "fault_type": fault_type_from_state(state),
        "targets": target_list_from_state(state),
    }
    if include_blade_uid:
        data["blade_uid"] = (
            blade_uid if blade_uid is not None else state.get("blade_uid", "")
        ) or ""
    if error:
        data["error"] = error
    return data


def recover_task_state_from_values(values: Mapping[str, Any]) -> str:
    """Return the recover lifecycle state from a recover graph state."""

    outcome = read_operation_outcome(values)
    result = outcome.result or {}
    if not isinstance(result, Mapping):
        result = {}

    is_recovered = bool(result.get("recovered", False))
    recovery_level = result.get(
        "recovery_level",
        "recovered" if is_recovered else "failed",
    )
    if not is_recovered:
        return "failed"
    if recovery_level == "partial":
        return "partial_recovered"
    return "recovered"


def recover_result_label_from_values(values: Mapping[str, Any]) -> str:
    """Return the legacy CLI ``result`` label for a recover graph state."""

    outcome = read_operation_outcome(values)
    result = outcome.result or {}
    if not isinstance(result, Mapping):
        return "failed"
    if not result.get("recovered", False):
        return "failed"
    return str(result.get("recovery_level") or "recovered")


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
        "blade_uid": inject_state.get("blade_uid", "") or "",
        "duration_ms": elapsed_ms,
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
        "blade_uid": inject_state.get("blade_uid", "") or "",
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
    blade_uid: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Build the legacy local-CLI recover failure data shape."""

    inject_state = dict(inject_state_values or {})
    if blade_uid and not inject_state.get("blade_uid"):
        inject_state["blade_uid"] = blade_uid
    data = build_recover_cli_data_from_state(
        {"result": {"recovered": False}, "error": error or ""},
        inject_task_id,
        inject_state,
    )
    if error:
        data["error"] = error
    return data
