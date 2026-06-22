"""AgentState construction helpers.

Entry points should describe their caller context and fault intent, while this
module owns the exact initial-state shape passed to LangGraph.  Keeping that
contract in one place prevents CLI, HTTP, TUI, and L4 from drifting as
AgentState grows.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.utils.time import now_iso


def _fault_spec_to_dict(fault_spec: FaultSpec | dict | None) -> dict | None:
    if isinstance(fault_spec, FaultSpec):
        return fault_spec.to_dict()
    if isinstance(fault_spec, dict):
        return deepcopy(fault_spec)
    return None


def build_inject_initial_state(
    *,
    task_id: str,
    fault_spec: FaultSpec | dict | None,
    tui_session_id: str = "",
    confirmed_intent: str | None = None,
    needs_confirmation: bool = False,
    interaction_mode: str = "cli",
    kubeconfig: str | None = "",
    kube_context: str | None = "",
    kubewiz_cluster_uuid: str | None = "",
    kubewiz_profile: str | None = "",
    direct: bool = False,
    dry_run: bool = False,
    messages: list | None = None,
    batch_submit_args: dict | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the initial AgentState for an inject Pipeline Graph run."""

    state: dict[str, Any] = {
        "task_id": task_id,
        "tui_session_id": tui_session_id or "",
        "operation": "inject",
        "fault_spec": _fault_spec_to_dict(fault_spec),
        "needs_confirmation": bool(needs_confirmation),
        "safety_status": "pending",
        "kubeconfig": kubeconfig or "",
        "kube_context": kube_context or "",
        "kubewiz_cluster_uuid": kubewiz_cluster_uuid or "",
        "kubewiz_profile": kubewiz_profile or "",
        "created_at": created_at or now_iso(),
        "direct": bool(direct),
        "interaction_mode": interaction_mode,
        "dry_run": bool(dry_run),
    }

    if confirmed_intent is not None:
        state["confirmed_intent"] = confirmed_intent
    if messages is not None:
        state["messages"] = list(messages)
    if batch_submit_args is not None:
        state["batch_submit_args"] = deepcopy(batch_submit_args)

    return state


__all__ = [
    "build_inject_initial_state",
]
