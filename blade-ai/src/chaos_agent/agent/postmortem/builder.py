"""Pure-function context extraction from AgentState → postmortem input dict.

Reads (does NOT mutate) the post-experiment state and projects the
fields the LLM will need into a JSON-serialisable dict. Keeps the LLM
prompt clean: it gets a single ``context`` argument with predictable
keys instead of having to dig through dozens of state fields.

Decision: extract → render in two stages so the prompt template can
evolve independently of the state shape. Also makes ``generate_postmortem``
trivially testable (mock the context dict, no need for a full state).
"""
from __future__ import annotations

from typing import Any

from chaos_agent.agent.fault_spec import read_fault_spec
from chaos_agent.agent.verdict import FailureCategory

# Failure categories worth a postmortem. The rest (USER_REJECTED,
# SAFETY_REJECTED, PLANNING_TIMEOUT) ran no actual experiment — no
# verifier data, no side-effects, no blade_uid — so an LLM-generated
# report would be padded fluff. Skip them cleanly.
_POSTMORTEM_FAILURE_WHITELIST: frozenset[str] = frozenset({
    FailureCategory.VERIFICATION_FAILED.value,
    FailureCategory.EXECUTION_FAILED.value,
    FailureCategory.REPLAN_EXHAUSTED.value,
})


def should_generate_postmortem(state: dict, settings) -> bool:
    """Decide whether to spend an LLM call on this task.

    Conditions (ALL must hold):
      1. settings.postmortem_enabled is True
      2. Task is an inject (not chat / recover-bridge)
      3. EITHER blade_uid is set (real injection happened, success or
         post-inject failure) OR the failure category is in the
         postmortem whitelist (real experiment, just failed verification
         / execution / replan)

    A return value of ``False`` lets the caller cleanly skip postmortem
    generation without raising / logging — postmortem is opportunistic.
    """
    if not getattr(settings, "postmortem_enabled", False):
        return False

    if state.get("confirmed_intent") not in ("inject",):
        # chat / recover-bridge / unset — nothing to post-mortem
        return False

    if state.get("blade_uid"):
        return True

    failure_detail = state.get("failure_detail") or {}
    category = failure_detail.get("category") if isinstance(failure_detail, dict) else None
    return category in _POSTMORTEM_FAILURE_WHITELIST


def build_postmortem_context(state: dict, *, max_messages: int = 30) -> dict[str, Any]:
    """Project AgentState fields into a flat dict for the LLM prompt.

    Side-effect data is split between two sources by design:
      - ``pre_snapshot``: from ``state.se_snapshot`` (captured by
        se_snapshot_node BEFORE injection). Just counts of pods /
        endpoints so the prompt stays bounded.
      - ``side_effects``: from ``state.verification.side_effects``
        (written by se_detect_node AFTER injection). The diff that
        matters — what changed.

    ``messages`` is tail-truncated to ``max_messages`` with a leading
    "... (N earlier messages elided)" marker when oversized, so a long
    ReAct loop doesn't blow the prompt budget.
    """
    spec = read_fault_spec(state)
    fault_spec_dict = spec.to_dict() if spec else {}

    verification = state.get("verification") or {}
    if not isinstance(verification, dict):
        verification = {}

    side_effects = verification.get("side_effects") or {}
    if not isinstance(side_effects, dict):
        side_effects = {}

    pre_snapshot_dict = state.get("se_snapshot") or {}
    if not isinstance(pre_snapshot_dict, dict):
        pre_snapshot_dict = {}
    # Reduce to counts — the full snapshot can be hundreds of pods worth
    # of dicts; the LLM only needs scale, not per-pod detail.
    pre_snapshot_summary = {
        "pods_count": len(pre_snapshot_dict.get("pods", {})),
        "endpoints_count": len(pre_snapshot_dict.get("endpoints", {})),
    }

    baseline_capture = state.get("baseline_capture")
    if isinstance(baseline_capture, dict):
        baseline_summary = baseline_capture
    else:
        baseline_summary = {}

    safety_score = state.get("safety_score") or {}
    failure_detail = state.get("failure_detail") or {}
    result = state.get("result") or {}

    messages_raw = state.get("messages") or []
    elided = max(0, len(messages_raw) - max_messages)
    messages_tail = messages_raw[-max_messages:] if elided else list(messages_raw)
    messages_summary = [_summarise_message(m) for m in messages_tail]

    return {
        "task_id": state.get("task_id", ""),
        "skill_name": state.get("skill_name", ""),
        "fault_spec": fault_spec_dict,
        "blade_uid": state.get("blade_uid", "") or "",
        "result": {
            "status": result.get("status", "") if isinstance(result, dict) else "",
            "task_state": state.get("task_state", "") or result.get("task_state", "") if isinstance(result, dict) else state.get("task_state", ""),
            "duration_ms": result.get("duration_ms", 0) if isinstance(result, dict) else 0,
        },
        "verification": {
            "level": verification.get("level", ""),
            "layer1": verification.get("layer1", {}),
            "layer2": verification.get("layer2", {}),
        },
        "side_effects": side_effects,
        "pre_snapshot": pre_snapshot_summary,
        "baseline_capture": baseline_summary,
        "safety_score": safety_score,
        "failure_detail": failure_detail,
        "replan_count": state.get("replan_count", 0),
        "replan_context": state.get("replan_context") or "",
        "user_input": state.get("input", "") or (spec.user_description if spec else ""),
        "messages_elided": elided,
        "messages": messages_summary,
        "started_at": state.get("started_at", "") or "",
        "finished_at": state.get("finished_at", "") or "",
    }


def _summarise_message(msg) -> dict[str, Any]:
    """Reduce a langchain message to {type, time, content_preview, tool_calls}.

    Full content is truncated at 500 chars per message — the LLM only
    needs the gist for timeline reconstruction. Tool calls are kept
    structured because the postmortem timeline references them by name.
    """
    msg_type = getattr(msg, "type", "") or msg.__class__.__name__.lower().replace("message", "")
    content = getattr(msg, "content", "") or ""
    if not isinstance(content, str):
        content = str(content)
    preview = content if len(content) <= 500 else content[:500] + "..."

    tool_calls = getattr(msg, "tool_calls", None) or []
    tcs = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        tcs.append({
            "name": tc.get("name", ""),
            "args": tc.get("args", {}),
        })

    # Wall-clock timestamp (Beijing ISO string) injected by _ts_add_messages reducer
    ts_iso = (getattr(msg, "additional_kwargs", None) or {}).get("_ts", "")
    time_str = ts_iso[11:19] if len(ts_iso) >= 19 else ""

    return {
        "type": msg_type,
        "time": time_str,
        "content_preview": preview,
        "tool_calls": tcs,
    }
