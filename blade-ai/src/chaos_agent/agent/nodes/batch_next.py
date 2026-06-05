"""batch_next node — collect current fault result and advance index.

Called after save_memory in the batch loop. Responsibilities:
  1. Append current fault's result to batch_results
  2. Emit batch_fault_result custom event for real-time ResultCard
  3. Increment current_fault_index
  4. Optional interval sleep between serial faults
"""

from __future__ import annotations

import asyncio
import json
import logging

from langchain_core.callbacks import adispatch_custom_event

from chaos_agent.agent.fault_spec import read_fault_spec
from chaos_agent.agent.state import AgentState, infer_task_state

logger = logging.getLogger(__name__)


async def batch_next(state: AgentState) -> dict:
    idx = state.get("current_fault_index", 0)
    batch_args = state.get("batch_submit_args") or {}
    faults = batch_args.get("faults", [])

    spec = read_fault_spec(state)
    task_state = infer_task_state(dict(state))

    from chaos_agent.agent.fault_spec import legacy_target_dict
    from chaos_agent.agent.state import extract_ui_diagnostics, strip_side_effects

    verification = state.get("verification")
    entry = {
        "task_id": state.get("task_id", ""),
        "blade_uid": state.get("blade_uid"),
        "task_state": task_state,
        "fault_type": (spec.fault_type if spec and spec.fault_type
                       else state.get("skill_name", "")),
        "error": state.get("error"),
        "target": legacy_target_dict(dict(state)),
        "verification": strip_side_effects(verification),
        "side_effects": verification.get("side_effects") if isinstance(verification, dict) else None,
        "postmortem": state.get("postmortem"),
        "duration_ms": 0,
        **extract_ui_diagnostics(dict(state)),
    }

    results = list(state.get("batch_results") or [])
    results.append(entry)
    new_index = idx + 1

    # Emit per-fault result as a custom event so turn.py's streaming
    # loop can forward it to the TUI as a ResultCard immediately.
    try:
        await adispatch_custom_event("batch_fault_result", {
            "fault_index": idx,
            "fault_total": len(faults),
            "result": entry,
        })
    except Exception:
        logger.debug("batch_next: failed to dispatch batch_fault_result", exc_info=True)

    interval = int(batch_args.get("interval_seconds", 0))
    if interval > 0 and new_index < len(faults):
        logger.info("batch_next: sleeping %ds before fault %d/%d", interval, new_index + 1, len(faults))
        await asyncio.sleep(interval)

    return {
        "current_fault_index": new_index,
        "batch_results": results,
    }
