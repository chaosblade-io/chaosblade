"""intent_confirm node — intent confirmation gate before agent_loop.

Two-layer confirmation defense:
  Layer 1 (this node): Confirms the LLM's understanding of the user's fault
  injection intent before proceeding to planning/execution.
  Layer 2 (confirmation_gate): Confirms the generated plan before actual execution.

Uses LangGraph interrupt() to pause the graph. The TUI renders a summary panel
and collects Y/N from the user. Resume with Command(resume="approved"|"rejected").

If rejected, the graph ends (returns to TUI REPL). The user can continue
the conversation in the next invocation to refine their intent.
"""

from __future__ import annotations

import logging

from langchain_core.messages import RemoveMessage, SystemMessage
from langgraph.types import interrupt

from chaos_agent.agent.fault_spec import read_fault_spec
from chaos_agent.agent.state import AgentState
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory

logger = logging.getLogger(__name__)


# Trim window: how many tail messages survive untouched on commit.
# Picked to mirror the previous ``intent_clarification`` fast-path
# behaviour (last 4) so post-commit Phase 1 LLM context size matches
# the pre-Option-A baseline.
_TRIM_TAIL_KEEP = 4


def _format_intent_summary(fault_intent: dict) -> str:
    """Format fault_intent dict into a human-readable summary."""
    parts = []
    parts.append(f"故障类型: {fault_intent.get('fault_type', '未知')}")
    parts.append(f"范围: {fault_intent.get('scope', '未知')}")
    parts.append(f"目标: {fault_intent.get('target', '未知')}")
    parts.append(f"动作: {fault_intent.get('action', '未知')}")
    parts.append(f"命名空间: {fault_intent.get('namespace', '未知')}")
    if fault_intent.get("labels"):
        parts.append(f"标签选择器: {fault_intent['labels']}")
    if fault_intent.get("names"):
        parts.append(f"目标资源: {', '.join(fault_intent['names'])}")
    if fault_intent.get("params"):
        params_str = ", ".join(f"{k}={v}" for k, v in fault_intent["params"].items())
        parts.append(f"参数: {params_str}")
    if fault_intent.get("user_description"):
        parts.append(f"用户描述: {fault_intent['user_description']}")
    return "\n".join(parts)


def _build_handoff_summary(fault_intent: dict, dialogue_round: int) -> SystemMessage:
    """Build the ``[Intent Clarification Summary]`` SystemMessage that
    marks the boundary between intent dialogue and inject execution.

    Format and content are kept identical to the pre-Option-A summary
    that ``intent_clarification`` used to produce — downstream consumers
    (``session_store._split_at_handoff``, ``cli/runner.py`` handoff
    detection at ~478, ``cli/runner.py`` at ~919) match on the
    ``[Intent Clarification Summary]`` content prefix, so producing the
    same string from a different node is a transparent move.
    """
    return SystemMessage(content=(
        f"[Intent Clarification Summary]\n"
        f"Dialogue rounds: {dialogue_round}\n"
        f"Confirmed intent: inject\n"
        f"Fault: {fault_intent.get('fault_type', 'unknown')} → "
        f"{fault_intent.get('scope', '')}/{fault_intent.get('target', '')}/"
        f"{fault_intent.get('action', '')} @ {fault_intent.get('namespace', '')}"
    ))


def _build_trim_remove_list(messages: list) -> list[RemoveMessage]:
    """Build the RemoveMessage list that drops old dialogue messages
    while preserving ``[Task Summary]`` and ``[Compressed History]``
    markers.

    Task summaries record previous inject/recover results — the LLM
    needs them to answer "what happened last time?" across multiple
    tasks in the same session. Compressed history summaries are the
    output of PreReasoningHook's LLM compaction and must survive
    trimming for the same reason.
    """
    if len(messages) <= _TRIM_TAIL_KEEP:
        return []
    _PRESERVE_PREFIXES = ("[Task Summary]", "[Compressed History]")
    remove_list: list[RemoveMessage] = []
    for msg in messages[:-_TRIM_TAIL_KEEP]:
        content = getattr(msg, "content", "") or ""
        if any(content.startswith(p) for p in _PRESERVE_PREFIXES):
            continue
        msg_id = getattr(msg, "id", None)
        if msg_id:
            remove_list.append(RemoveMessage(id=msg_id))
    return remove_list


def _commit_inject_handoff(state: AgentState, fault_intent: dict) -> dict:
    """Run the inject pipeline handoff and produce the state delta.

    Dual-graph model: ``handoff_summary`` is read by the Runner to
    seed Pipeline Graph messages. bootstrap_task_session is called
    by the Runner layer, not here.
    """
    messages = state.get("messages", [])
    dialogue_round = int(state.get("dialogue_round") or 0)
    summary_msg = _build_handoff_summary(fault_intent, dialogue_round)
    remove_list = _build_trim_remove_list(messages)

    return {
        "messages": remove_list,
        "handoff_summary": summary_msg.content,
    }


async def intent_confirm(state: AgentState) -> dict:
    """Pause and ask user to confirm their fault injection intent.

    Presents a structured summary of the parsed fault intent and waits
    for user approval before routing to agent_loop.

    Resume with Command(resume="approved") to proceed, or
    Command(resume="rejected") to abort (graph ends, back to TUI REPL).
    """
    task_id = state.get("task_id", "")
    # Single source of truth — the fault_spec written by
    # intent_clarification. Projected through ``to_intent_dict()`` for
    # the helpers below (render / handoff) which still take the
    # legacy dict shape; the spec itself stays in state.
    spec = read_fault_spec(state)
    fault_intent = spec.to_intent_dict() if spec else {}
    intent_confidence = float(state.get("intent_confidence") or 0.0)

    tracker = get_tracker(task_id) if task_id else None
    # Phase 3c.2 — Dry-Run short-circuit. ``/plan <NL>`` runs the
    # whole planning pipeline (agent_loop → safety_check →
    # confirmation_gate) so the user sees a real "what would happen"
    # summary, but the user-facing intent gate is the wrong place to
    # prompt for approval — the user already opted into "preview only"
    # by typing /plan. Without this skip the user would have to click
    # Y on a Layer-1 confirm card before the plan even materialises.
    # ``confirmation_gate`` already understands dry_run and emits the
    # final preview AIMessage, so falling straight through to
    # agent_loop here is what the rest of the graph expects.
    if state.get("dry_run"):
        if tracker:
            tracker.start(
                StatusCategory.NODE,
                "intent_confirm",
                "Dry-Run: 跳过意图确认，进入计划生成",
                {"dry_run": True, "fault_intent": fault_intent},
            )
            tracker.complete("Dry-Run: bypassed Layer-1 confirm")
        logger.info("intent_confirm bypassed for dry_run task %s", task_id)
        # Dry-Run mirrors the approved path: ``/plan <NL>`` runs the
        # full inject pipeline as a preview, so the downstream
        # agent_loop / safety_check stages need the same clean
        # ``[Intent Clarification Summary]`` handoff and trimmed
        # message list they would see on a real approval. Skipping
        # this would leave Phase 1 reading the verbose clarification
        # dialogue and produce a different plan preview than the
        # post-Option-A approved flow.
        return _commit_inject_handoff(state, fault_intent)

    if tracker:
        tracker.start(
            StatusCategory.NODE,
            "intent_confirm",
            "等待用户确认故障注入意图",
            {"fault_intent": fault_intent, "intent_confidence": intent_confidence},
        )

    # Build confirmation payload for TUI rendering.
    #
    # Extra fields beyond the original 4-key payload (Layer 1 v3 audit
    # trail — visible only when relevant):
    #   · ``intent_reasoning``     — LLM's own explanation of why it
    #                                classified this fault_type. UI
    #                                surfaces it on low-confidence
    #                                turns so the user can audit
    #                                "why did the agent pick this?".
    #   · ``clarification_round`` — N>0 means we've already asked the
    #                                user once for clarification; UI
    #                                can show "round N of N" so the
    #                                user knows we're iterating.
    batch_args = state.get("batch_submit_args")
    if batch_args and isinstance(batch_args, dict) and batch_args.get("faults"):
        batch_faults = batch_args["faults"]
        batch_lines = [f"批量故障注入: {len(batch_faults)} 个故障 (串行执行)"]
        for i, f in enumerate(batch_faults, 1):
            batch_lines.append(
                f"  {i}. {f.get('scope','')}-{f.get('target','')}-{f.get('action','')} "
                f"@ {f.get('namespace','')}/{', '.join(f.get('names', [])) or '*'}"
            )
        summary = "\n".join(batch_lines)
    else:
        summary = _format_intent_summary(fault_intent)
    confirmation_info = {
        "type": "intent_confirm",
        "fault_intent": fault_intent,
        "summary": summary,
        "intent_confidence": intent_confidence,
        "intent_reasoning": state.get("intent_reasoning") or "",
        "clarification_round": int(state.get("clarification_round") or 0),
        "batch_faults": batch_args.get("faults") if batch_args else None,
    }

    # Interrupt: TUI renders the summary and collects Y/N
    decision = interrupt(confirmation_info)

    if decision == "approved":
        if tracker:
            tracker.complete("用户确认意图，进入执行阶段")
        logger.info("Intent confirmed by user: %s", fault_intent.get("fault_type"))
        # Option A handoff: the trim + bootstrap side effects used to
        # fire from ``intent_clarification`` the moment intent
        # converged, which meant the working messages list shrank even
        # when the user later rejected at this gate. Moving them to
        # the approved branch keeps the full clarification dialogue
        # alive across rejections (so a continued conversation can
        # refine, not restart) and stops orphan task files from being
        # created for rejected intents.
        return _commit_inject_handoff(state, fault_intent)
    else:
        # User rejected — clear confirmed_intent so router routes to END.
        # Notably we do NOT touch ``messages`` here: the full
        # clarification dialogue stays in working memory so the user's
        # next turn can iterate on the already-established context
        # instead of forcing the agent to re-collect baseline facts.
        # ``task_id`` also stays as ``task-<hex>`` (allocated by
        # ``intent_clarification``) — ``bootstrap_task_session`` is
        # idempotent on re-entry (``store.has_active`` guard) so a
        # subsequent approval reuses the same id without re-creating
        # the on-disk file.
        if tracker:
            tracker.complete("用户拒绝意图，返回对话")
        logger.info("Intent rejected by user, returning to conversation")
        # Do NOT clear ``fault_spec`` on reject — the user often wants
        # to refine the intent in the next turn (change namespace,
        # add a label selector, ...). Keeping the spec lets the next
        # ``intent_clarification`` run continue merging on top of
        # what was already captured. Only ``confirmed_intent`` is
        # reset so the router takes the END path.
        return {
            "confirmed_intent": None,
        }
