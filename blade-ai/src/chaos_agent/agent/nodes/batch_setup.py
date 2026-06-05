"""batch_setup node — prepare execution environment for the current fault.

Called at the start of each batch loop iteration. Routes to agent_loop
for full per-fault planning (kubectl verify, skill activation, plan
generation), then the standard pipeline takes over.

Responsibilities:
  1. Read faults[current_fault_index] from batch_submit_args
  2. Build FaultSpec for the current fault
  3. Allocate task_id + bootstrap SessionStore entry
  4. Clear messages (RemoveMessage) for LLM context isolation
  5. Add a HumanMessage guiding agent_loop to plan this specific fault
  6. Reset all per-fault state fields to defaults
  7. Sync to TaskStore
"""

from __future__ import annotations

import logging
from uuid import uuid4

from langchain_core.messages import HumanMessage, RemoveMessage

from chaos_agent.agent.fault_spec import SOURCE_TUI, FaultSpec
from chaos_agent.agent.nodes._store_sync import sync_to_store
from chaos_agent.agent.state import AgentState
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

REMOVE_ALL_MESSAGES = "__remove_all__"

# Per-fault fields to reset at each batch iteration.
# skill_name IS reset — agent_loop re-activates per fault so each fault
# gets the correct skill_case_content for its specific scope/target/action.
_BATCH_RESET_FIELDS: dict = {
    "operation": "inject",
    "confirmed_intent": "inject",
    "dry_run": False,
    "skill_name": None,
    "skill_case_content": None,
    "matched_use_case_path": None,
    "safety_status": "pending",
    "safety_reason": None,
    "safety_checked_detail": None,
    "conflict_uids": None,
    "safety_score": None,
    "blast_radius_scope": None,
    "blast_radius_detail": None,
    "target_health_report": None,
    "feasibility_report": None,
    "approved_target": None,
    "plan": None,
    "plan_path": None,
    "is_complex": None,
    "planning_rejected": False,
    "blade_uid": None,
    "injection_method": None,
    "kubectl_exec_pod_name": None,
    "blade_parsed_flags": None,
    "inject_context": None,
    "injection_start_time": None,
    "original_replicas": None,
    "direct": False,
    "force_override": False,
    "verification": None,
    "recover_verification": None,
    "metric_observations": None,
    "inject_layer1_cache": None,
    "recover_layer1_cache": None,
    "inject_verification_summary": None,
    "baseline_data": None,
    "target_metadata": None,
    "evidence_snapshot": None,
    "disk_burn_post_check": None,
    "disk_fill_post_check": None,
    "se_snapshot": None,
    "reverify_count": 0,
    "reverify_gaps": None,
    "cleaned_debug_pods": None,
    "result": None,
    "error": None,
    "failure_reason": None,
    "failure_detail": None,
    "postmortem": None,
    "finished_at": None,
    "agent_loop_count": 0,
    "execute_loop_count": 0,
    "verifier_loop_count": 0,
    "pipeline_started_at": 0.0,
    "transient_retry_count": 0,
    "pipeline_attempt": 0,
    "pipeline_attempts_history": None,
    "replan_requested": False,
    "replan_count": 0,
    "replan_context": None,
    "replan_history": None,
    "screener_route": None,
    "drift_reject_count": 0,
    "plan_change_reject_count": 0,
    "compressed_summary": None,
    "parent_task_id": "",
    "recover_phase": "layer1_recovery",
    "recover_layer1_type": None,
    "layer1_iteration_count": 0,
    "layer2_context_added": False,
    "recover_layer2_first": False,
}


def _normalize_batch_args(state: dict) -> dict:
    """Ensure batch_submit_args exists; wrap single fault_spec if needed."""
    batch_args = state.get("batch_submit_args")
    if batch_args and isinstance(batch_args, dict) and batch_args.get("faults"):
        return batch_args

    spec_dict = state.get("fault_spec") or {}
    return {
        "faults": [{
            "scope": spec_dict.get("scope", ""),
            "target": spec_dict.get("blade_target", ""),
            "action": spec_dict.get("blade_action", ""),
            "namespace": spec_dict.get("namespace", ""),
            "names": list(spec_dict.get("names", [])),
            "labels": dict(spec_dict.get("labels", {})),
            "params": dict(spec_dict.get("params", {})),
        }],
        "execution_order": "serial",
        "interval_seconds": 0,
    }


def _build_agent_prompt(spec: FaultSpec, idx: int, total: int) -> str:
    """Build a HumanMessage guiding agent_loop to plan this specific fault."""
    parts = []
    if total > 1:
        parts.append(f"批量故障注入 ({idx + 1}/{total})")
    parts.append(f"请为以下故障制定注入计划并执行：")
    parts.append(f"- 故障类型: {spec.scope}-{spec.blade_target}-{spec.blade_action}")
    if spec.namespace:
        parts.append(f"- 命名空间: {spec.namespace}")
    if spec.names:
        parts.append(f"- 目标: {', '.join(spec.names)}")
    if spec.params:
        param_str = ", ".join(f"{k}={v}" for k, v in spec.params.items() if v)
        if param_str:
            parts.append(f"- 参数: {param_str}")
    return "\n".join(parts)


async def batch_setup(state: AgentState) -> dict:
    batch_args = _normalize_batch_args(state)
    idx = state.get("current_fault_index", 0)
    faults = batch_args.get("faults", [])

    if idx >= len(faults):
        logger.warning("batch_setup: index %d >= faults count %d", idx, len(faults))
        return {}

    current = faults[idx]
    existing = state.get("fault_spec") or {}

    spec = FaultSpec(
        namespace=current.get("namespace") or existing.get("namespace", ""),
        scope=current.get("scope") or existing.get("scope", ""),
        names=tuple(current.get("names") or existing.get("names", [])),
        labels=dict(current.get("labels") or existing.get("labels", {})),
        blade_target=current.get("target") or existing.get("blade_target", ""),
        blade_action=current.get("action") or existing.get("blade_action", ""),
        params=dict(current.get("params") or existing.get("params", {})),
        params_flags=list(existing.get("params_flags", [])),
        duration_seconds=int(existing.get("duration_seconds", 0)),
        source=SOURCE_TUI,
        user_description=existing.get("user_description", ""),
    )

    new_task_id = f"task-{uuid4().hex[:12]}"
    tui_sid = state.get("tui_session_id", "")

    try:
        from chaos_agent.agent.nodes.intent_clarification import bootstrap_task_session
        from langchain_core.messages import SystemMessage
        desc = f"{spec.scope}-{spec.blade_target} {spec.blade_action}"
        label = f"批量故障 {idx + 1}/{len(faults)}: {desc}" if len(faults) > 1 else desc
        bootstrap_task_session(new_task_id, "inject", tui_sid, SystemMessage(content=label))
    except Exception:
        logger.warning("batch_setup: bootstrap_task_session failed for %s",
                       new_task_id, exc_info=True)

    agent_prompt = _build_agent_prompt(spec, idx, len(faults))

    result = dict(_BATCH_RESET_FIELDS)
    result.update({
        "task_id": new_task_id,
        "fault_spec": spec.to_dict(),
        "created_at": now_iso(),
        "needs_confirmation": True,
        "batch_submit_args": batch_args,
        "messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            HumanMessage(content=agent_prompt),
        ],
    })

    await sync_to_store(state, result)
    return result
