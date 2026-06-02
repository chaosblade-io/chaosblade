"""L4 adapter: TestTask ↔ AgentState conversions.

Handles inbound (TestTask → inject graph initial_state) and
outbound (graph final state → TaskResult) transformations.
"""

from __future__ import annotations

import uuid

from chaos_agent.l4.error_mapping import _extract_error
from chaos_agent.l4.schemas import L4TaskResult, L4TestTask


def test_task_to_initial_state(task: L4TestTask) -> dict:
    """Convert L4 TestTask into inject graph initial_state dict."""
    payload = task.payload or {}
    fault_spec_dict = {
        "namespace": payload.get("namespace", "default"),
        "scope": payload.get("fault_scope", "pod"),
        "names": payload.get("target_names", []),
        "labels": payload.get("target_labels", {}),
        "blade_target": payload.get("fault_target", "cpu"),
        "blade_action": payload.get("fault_action", "fullload"),
        "params": payload.get("params", {}),
        "duration_seconds": payload.get("duration", 600),
        "source": "l4_sdk",
        "user_description": task.intent,
    }
    return {
        "task_id": task.task_id,
        "tui_session_id": "",
        "operation": "inject",
        "fault_spec": fault_spec_dict,
        "direct": payload.get("direct", False),
        "needs_confirmation": False,
        "safety_status": "pending",
        "interaction_mode": "l4",  # Avoid CLI auto-reject in confirmation_gate
        "kubeconfig": payload.get("kubeconfig", ""),
        "kube_context": payload.get("kube_context", ""),
        "messages": [],
    }


def state_to_task_result(
    values: dict, task_id: str, trajectory_id: str = ""
) -> L4TaskResult:
    """Extract TaskResult from graph final state.

    Reuses build_status_data() to avoid reinventing field assembly.
    """
    from chaos_agent.agent.state import build_status_data, infer_task_state

    task_state = infer_task_state(values)
    status_data = build_status_data(task_id, values)

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

    return L4TaskResult(
        task_id=task_id,
        status=status,
        trajectory_id=trajectory_id,
        summary=status_data.get("fault_type", "") + " \u00b7 " + task_state,
        error=error,
        extras={
            "blade_uid": values.get("blade_uid", ""),
            "verification": values.get("verification"),
            "safety": values.get("safety_status"),
            "task_state": task_state,
            "phase": status_data.get("phase"),
            "duration_ms": status_data.get("duration_ms"),
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
    from chaos_agent.utils.inject_context import build_inject_context

    inject_msgs = inject_values.get("messages", [])
    inject_context = build_inject_context(inject_msgs)

    return {
        "task_id": f"recover-{inject_task_id}",
        "tui_session_id": inject_values.get("tui_session_id", ""),
        "parent_task_id": inject_task_id,
        "operation": "recover",
        "blade_uid": inject_values.get("blade_uid", ""),
        "skill_name": inject_values.get("skill_name", ""),
        "skill_case_content": inject_values.get("skill_case_content", ""),
        "inject_verification_summary": inject_values.get(
            "inject_verification_summary", ""
        ),
        "inject_context": inject_context,
        "fault_spec": inject_values.get("fault_spec", {}),
        "kubeconfig": inject_values.get("kubeconfig", ""),
        "verifier_loop_count": 0,
        "verification": None,
        "recover_verification": None,
        "messages": [],
    }


def make_trajectory_id(task_id: str) -> str:
    """Generate a trajectory_id. Format: traj-{task_id}-{short_uuid}."""
    short = uuid.uuid4().hex[:8]
    return f"traj-{task_id}-{short}"
