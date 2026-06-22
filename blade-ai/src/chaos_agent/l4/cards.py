"""Interrupt payload → PendingCard adapter.

主图节点通过 ``interrupt(payload)`` 暂停。本模块把 4 类 interrupt payload
统一适配为 ``PendingCard``，由上层（ai-testing-platform / TUI / Server）通过
``runtime.present_card`` 协议消费。

识别策略（按优先级）：
  1. payload 含 ``"type"`` 字段 → 直接 dispatch（intent_confirm / plan_change /
     target_change）
  2. 无 ``type`` 但含 ``safety_status`` + ``plan_summary`` →  confirmation_gate
     (plan_confirm)
  3. 兜底 → 通用 ``unknown`` 卡，details 原样透传
"""

from __future__ import annotations

import logging
import uuid

from chaos_agent.l4.schemas import PendingCard

logger = logging.getLogger(__name__)


def _make_card_id(thread_id: str, card_type: str) -> str:
    """Generate a unique card id scoped to thread + type."""
    return f"{card_type}-{thread_id}-{uuid.uuid4().hex[:8]}"


def _adapt_intent_confirm(payload: dict, thread_id: str) -> PendingCard:
    """payload 形态见 ``intent_confirm.py:202``。

    仅 intent_confirm 卡支持 ``request_modify`` 决策（用户填反馈，平台层
    拆解为「step rejected → clarify(user_feedback)」）。
    """
    fault_intent = payload.get("fault_intent") or {}
    fault_type = fault_intent.get("fault_type") or "unknown"
    namespace = fault_intent.get("namespace") or ""
    confidence = payload.get("intent_confidence")
    round_n = int(payload.get("clarification_round") or 0)

    title_parts = [f"故障意图确认：{fault_type}"]
    if namespace:
        title_parts.append(f"@ {namespace}")
    title = " ".join(title_parts)

    summary = payload.get("summary") or ""
    if confidence is not None:
        summary = f"{summary}\n（识别置信度: {confidence}）" if summary else f"识别置信度: {confidence}"
    if round_n > 0:
        summary = f"{summary}\n（已澄清 {round_n} 轮）" if summary else f"已澄清 {round_n} 轮"

    details = {
        "fault_intent": fault_intent,
        "intent_confidence": confidence,
        "intent_reasoning": payload.get("intent_reasoning") or "",
        "clarification_round": round_n,
        "batch_faults": payload.get("batch_faults"),
    }

    return PendingCard(
        card_type="intent_confirm",
        card_id=_make_card_id(thread_id, "intent_confirm"),
        title=title,
        summary=summary,
        details=details,
        decision_options=["approved", "rejected", "request_modify"],
        thread_id=thread_id,
    )


def _adapt_plan_confirm(payload: dict, thread_id: str) -> PendingCard:
    """payload 形态见 ``confirmation_gate.py:142``——无 ``type`` 字段，靠
    ``safety_status`` + ``plan_summary`` 识别。
    """
    skill = payload.get("skill_name") or "unknown"
    fault_intent = payload.get("fault_intent") or {}
    target = payload.get("target") or ""
    safety_status = payload.get("safety_status") or "unknown"

    title = f"执行前确认：{skill}"
    if isinstance(fault_intent, dict) and fault_intent.get("fault_type"):
        title = f"执行前确认：{fault_intent['fault_type']}"
    summary_lines = [
        f"目标: {target}" if target else "",
        f"安全检查: {safety_status}",
    ]
    if payload.get("safety_reason"):
        summary_lines.append(f"原因: {payload['safety_reason']}")
    summary = "\n".join(line for line in summary_lines if line)

    details = {
        "skill_name": skill,
        "fault_intent": fault_intent,
        "target": target,
        "plan_summary": payload.get("plan_summary") or "",
        "safety_status": safety_status,
        "safety_reason": payload.get("safety_reason"),
        "safety_checked_detail": payload.get("safety_checked_detail"),
        "safety_score": payload.get("safety_score"),
        "params": payload.get("params") or {},
        "target_health_report": payload.get("target_health_report"),
        "conflict_uids": payload.get("conflict_uids") or [],
        "feasibility_report": payload.get("feasibility_report"),
        "pipeline_attempt": payload.get("pipeline_attempt"),
        "is_complex": payload.get("is_complex"),
        "plan_path": payload.get("plan_path"),
    }

    return PendingCard(
        card_type="plan_confirm",
        card_id=_make_card_id(thread_id, "plan_confirm"),
        title=title,
        summary=summary,
        details=details,
        decision_options=["approved", "rejected"],
        thread_id=thread_id,
    )


def _adapt_plan_change(payload: dict, thread_id: str) -> PendingCard:
    """payload 形态见 ``plan_change_confirm.py:69``。"""
    original = payload.get("original") or {}
    proposed = payload.get("proposed") or {}
    title = (
        f"计划变更确认：{original.get('fault_type') or '?'} "
        f"→ {proposed.get('fault_type') or '?'}"
    )
    summary = payload.get("reason") or "Agent 提议变更故障类型，请确认。"

    details = {
        "reason": payload.get("reason") or "",
        "original": original,
        "proposed": proposed,
    }

    return PendingCard(
        card_type="plan_change",
        card_id=_make_card_id(thread_id, "plan_change"),
        title=title,
        summary=summary,
        details=details,
        decision_options=["approved", "rejected"],
        thread_id=thread_id,
    )


def _adapt_tool_drift(payload: dict, thread_id: str) -> PendingCard:
    """payload 形态见 ``tool_screener.py:221``（``type=target_change``）。"""
    summary = payload.get("summary") or "Agent 工具调用偏离原批准目标。"
    title = "工具调用偏移确认"

    details = {
        "reason": payload.get("reason") or "",
        "agent_reason": payload.get("agent_reason") or "",
        "original": payload.get("original") or {},
        "proposed": payload.get("proposed") or {},
        "tool_calls": payload.get("tool_calls") or [],
    }

    return PendingCard(
        card_type="tool_drift",
        card_id=_make_card_id(thread_id, "tool_drift"),
        title=title,
        summary=summary,
        details=details,
        decision_options=["approved", "rejected"],
        thread_id=thread_id,
    )


def _adapt_unknown(payload: dict, thread_id: str) -> PendingCard:
    """Fallback：未知 interrupt payload 形态，原样透传 details 供上层兜底。"""
    title = "未知人工确认请求"
    summary = "SDK 收到未识别的 interrupt payload，已透传详情。"
    return PendingCard(
        card_type="unknown",
        card_id=_make_card_id(thread_id, "unknown"),
        title=title,
        summary=summary,
        details={"raw_payload": dict(payload) if isinstance(payload, dict) else {"value": payload}},
        decision_options=["approved", "rejected"],
        thread_id=thread_id,
    )


def interrupt_to_card(payload: object, thread_id: str) -> PendingCard:
    """Adapt a graph ``interrupt(payload)`` value to ``PendingCard``.

    Args:
        payload: 原始 interrupt 值（通常是 dict，但兜底支持任意类型）
        thread_id: LangGraph thread_id（用于关联 resume 操作）

    Returns:
        PendingCard 对象，``card_id`` 全局唯一。
    """
    if not isinstance(payload, dict):
        logger.warning("interrupt_to_card: non-dict payload type=%s", type(payload).__name__)
        return _adapt_unknown(payload if isinstance(payload, dict) else {}, thread_id)

    ptype = payload.get("type")
    if ptype == "intent_confirm":
        return _adapt_intent_confirm(payload, thread_id)
    if ptype == "plan_change":
        return _adapt_plan_change(payload, thread_id)
    if ptype == "target_change":
        return _adapt_tool_drift(payload, thread_id)

    # confirmation_gate payload 没有 ``type`` 字段；用 safety_status +
    # plan_summary 双因子识别（避免误判）
    if "safety_status" in payload and "plan_summary" in payload:
        return _adapt_plan_confirm(payload, thread_id)

    logger.warning(
        "interrupt_to_card: unrecognized payload, keys=%s",
        list(payload.keys())[:10],
    )
    return _adapt_unknown(payload, thread_id)
