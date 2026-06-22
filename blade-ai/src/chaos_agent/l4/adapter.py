"""L4 adapter: TestTask ↔ AgentState conversions.

Handles inbound (TestTask → inject graph initial_state) and
outbound (graph final state → TaskResult) transformations.
"""

from __future__ import annotations

import uuid

from chaos_agent.agent.state_builders import build_inject_initial_state
from chaos_agent.l4.error_mapping import _extract_error
from chaos_agent.l4.schemas import L4TaskResult, L4TestTask


def test_task_to_initial_state(task: L4TestTask) -> dict:
    """Convert L4 TestTask into inject graph initial_state dict.

    Reads fault parameters from ``payload["fault_intent"]``.
    This is produced by the platform's ``run_chaos_inject`` via
    ``FaultSpec.to_intent_dict()``.

    Fail-closed: required fields (target / action / scope / namespace) must
    be non-empty; otherwise raise ``ValueError`` so the caller (platform
    ``run_chaos_inject`` tool) returns a clear MISSING_REQUIRED_ARGS error
    instead of silently launching the inject pipeline with an empty
    fault_spec (which previously caused the agent_loop to spin in a
    "tell me what fault you want" ReAct loop until recursion_limit).
    """
    payload = task.payload or {}
    fi = payload.get("fault_intent")
    if not isinstance(fi, dict):
        raise ValueError(
            "L4 adapter: payload must include payload['fault_intent'] "
            "(dict with scope/target/action/namespace). "
            f"Got payload keys={list(payload.keys())}."
        )

    blade_target = fi.get("target", "")
    blade_action = fi.get("action", "")
    scope = fi.get("scope", "")
    namespace = fi.get("namespace", "")

    _missing = [
        name for name, val in (
            ("target", blade_target),
            ("action", blade_action),
            ("scope", scope),
            ("namespace", namespace),
        ) if not val
    ]
    if _missing:
        raise ValueError(
            "L4 adapter: fault_intent missing required field(s): "
            f"{_missing}. Got fault_intent keys={list(fi.keys())}. "
            "Required = target / action / scope / namespace (per "
            "FaultSpec.to_intent_dict())."
        )

    fault_spec_dict = {
        "namespace": namespace,
        "scope": scope,
        "names": fi.get("names", []),
        "labels": fi.get("labels", {}),
        "blade_target": blade_target,
        "blade_action": blade_action,
        "params": fi.get("params", {}),
        "duration_seconds": fi.get("duration", 600),
        "source": "l4_sdk",
        "user_description": fi.get("user_description") or task.intent,
    }
    # L4 SDK skips intent_clarification / batch_setup, so it must still
    # explicitly mark the operation as an inject for save_memory/postmortem.
    return build_inject_initial_state(
        task_id=task.task_id,
        fault_spec=fault_spec_dict,
        confirmed_intent="inject",
        direct=payload.get("direct", False),
        needs_confirmation=False,
        interaction_mode="l4",  # Avoid CLI auto-reject in confirmation_gate
        kubeconfig=payload.get("kubeconfig", ""),
        kube_context=payload.get("kube_context", ""),
        messages=[],
    )


def state_to_task_result(
    values: dict, task_id: str, trajectory_id: str = ""
) -> L4TaskResult:
    """Extract TaskResult from graph final state.

    Reuses build_status_data() to avoid reinventing field assembly.
    """
    from chaos_agent.agent.state import build_status_data, infer_task_state

    task_state = infer_task_state(values)
    status_data = build_status_data(task_id, values)

    # 透出 LLM token 消耗：从 observability tracer 取 trace 汇总，让平台
    # 大盘可以记录每次混沌实验的 token 用量。L4TaskResult 没有专属字段，
    # 借 extras 字典传出（dict 形态，平台侧用 .get 读）。
    token_usage_dict: dict | None = None
    try:
        from chaos_agent.observability.tracer import _traces
        _trace = _traces.get(task_id)
        if _trace is not None:
            _ti = int(getattr(_trace, "total_token_input", 0) or 0)
            _to = int(getattr(_trace, "total_token_output", 0) or 0)
            _calls = int(getattr(_trace, "total_llm_calls", 0) or 0)
            if _ti or _to or _calls:
                token_usage_dict = {
                    "prompt_tokens": _ti,
                    "completion_tokens": _to,
                    "total_tokens": _ti + _to,
                    "call_count": _calls or 1,
                }
    except Exception:
        token_usage_dict = None

    status_map = {
        "injected": "passed",
        "recovered": "passed",
        "partial_recovered": "degraded",
        "failed": "failed",
        "rejected": "failed",
        "injecting": "degraded",
        "recovering": "degraded",
        "completed": "passed",
    }
    status = status_map.get(task_state, "failed")

    error = None
    if status == "failed":
        error = _extract_error(values, task_state)

    from chaos_agent.agent.operation_outcome import (
        read_inject_verification,
        read_operation_outcome,
    )

    verification = (
        status_data["verification"]
        if "verification" in status_data
        else read_inject_verification(values)
    )
    outcome = read_operation_outcome(values)

    return L4TaskResult(
        task_id=task_id,
        status=status,
        trajectory_id=trajectory_id,
        summary=status_data.get("fault_type", "") + " \u00b7 " + task_state,
        error=error,
        extras={
            "blade_uid": status_data.get("blade_uid") or values.get("blade_uid", ""),
            "verification": verification,
            "safety": values.get("safety_status"),
            "task_state": task_state,
            "phase": status_data.get("phase"),
            "duration_ms": status_data.get("duration_ms"),
            # Surface the LLM-generated postmortem (path/markdown/summary)
            # written by save_memory node so SDK callers can render it.
            "postmortem": outcome.postmortem,
            "token_usage": token_usage_dict,
            **{
                k: v
                for k, v in status_data.items()
                if k not in ("task_id", "stage", "status")
            },
        },
    )


def build_recover_initial_state(inject_values: dict, inject_task_id: str) -> dict:
    """Build recover graph initial_state from inject graph final state.

    Mirrors server/routes/recover.py: reads inject checkpoint
    but does NOT copy full state (prevents causal chain illusion).
    """
    from chaos_agent.agent.recovery_state import build_recover_initial_from_checkpoint

    return build_recover_initial_from_checkpoint(
        inject_values,
        inject_task_id,
        record_task_id=f"recover-{inject_task_id}",
    )


def make_trajectory_id(task_id: str) -> str:
    """Generate a trajectory_id. Format: traj-{task_id}-{short_uuid}."""
    short = uuid.uuid4().hex[:8]
    return f"traj-{task_id}-{short}"
