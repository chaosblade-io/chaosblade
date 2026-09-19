"""Planning → execution handoff: strip the planning round's ReAct context.

Positioned on the single edge every plan-to-execution transition shares
(``extract_planning_metadata → planning_handoff → safety_check``), the
node removes the CURRENT planning round's process messages (AI turns,
tool results, in-loop corrective feedback) from the messages channel once
the plan is finalized. The handoff essentials survive: the context
anchors (FAULT INTENT, pre-task probe hints), the finalization turn
itself (the finish_planning caller AIMessage + ``Planning finalized``
ToolMessage, kept as a caller/result pair so downstream pairing gates
never see an orphan), and every message after it. extract_planning_metadata
runs BEFORE this node on purpose — it reverse-scans AIMessage tool_calls
(read_skill_resource / finish_planning args) that the strip would remove.

Selection rule (anchor whitelist, bounded by the attribution epoch):
- Left bound: ``attribution_epoch_index`` — the message count recorded at
  the last replan seam. Every replan path resets the epoch right before
  re-entering agent_loop, so the boundary is exactly the start of the
  CURRENT planning round; messages before it (older finalized markers,
  kickoffs, execution receipts, verification evidence) are the replan's
  INPUT and are never removed. First pass carries no boundary → 0.
- Right bound: the LAST ``Planning finalized`` ToolMessage (same marker
  ``_phase2_kickoff_needed`` keys on — one prefix constant, two readers).
- Inside the range only messages flagged ``context_anchor`` survive;
  everything else is stripped. New message types default to stripped —
  retention must be earned by marking the generation site, so the rule
  stays closed under prompt/protocol evolution.

Audit guarantee (persist-then-remove): every to-be-stripped message is
appended to the task JSON (session store, dedup-key idempotent — the
pre_reason_hook flush may already have written some of them) BEFORE the
RemoveMessage list is returned, and the append is then VERIFIED by
reading the session back: ``append_messages`` is fire-and-forget by
contract (a missing active session logs a warning and returns, a failed
disk write is swallowed on purpose — the Round-5 silent-fail audit), so
exception propagation alone cannot carry the guarantee. Any append
exception, read-back miss, or absent store skips the strip entirely:
archive completeness takes absolute priority over context slimming.

Idempotency is positional, not counter-based: once the range holds only
anchors (or nothing to begin with), re-entry finds no targets and returns
no state change — a checkpoint resume re-runs the node harmlessly. The
``attribution_epoch_index`` itself is never touched: the strip only
removes messages AFTER the boundary, so the index stays numerically
valid. That is the exact invariant the compaction re-base in
memory/hook.py protects from the other side — compaction removes BEFORE
the boundary and re-bases, the handoff removes AFTER and does not; the
two mechanisms never delete from the same side of the boundary.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage

from chaos_agent.agent.node_names import PLANNING_HANDOFF
from chaos_agent.agent.state import AgentState

logger = logging.getLogger(__name__)

# additional_kwargs flag marking a message as a handoff-retention anchor.
# Generation sites (FAULT INTENT in agent_loop, probe hints in
# preplan_probe) set it at write time; the strip treats it as a whitelist
# entry — content prefix matching is deliberately avoided (fragile under
# wording drift, unlike the long-frozen ``Planning finalized`` prefix).
CONTEXT_ANCHOR_FLAG = "context_anchor"


def _find_finalized_idx(messages: list) -> int | None:
    """Reverse-scan for the LAST ``Planning finalized`` ToolMessage index.

    Same scan direction and marker as ``_phase2_kickoff_needed`` so the
    two readers can never disagree about which finalization is newest
    (replan emits a new marker; both re-arm on it).
    """
    # Lazy import: execute_loop sits behind factory/tool wiring that
    # transitively imports this package (same pattern as memory/hook).
    from chaos_agent.agent.nodes.execute.execute_loop import (
        _PLANNING_FINALIZED_PREFIX,
    )

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, ToolMessage):
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if content.startswith(_PLANNING_FINALIZED_PREFIX):
            return i
    return None


def _is_context_anchor(msg) -> bool:
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    return bool(kwargs.get(CONTEXT_ANCHOR_FLAG))


def _epoch_start(state: AgentState) -> int:
    """Left bound of the strip range; 0 when no boundary is recorded."""
    raw = state.get("attribution_epoch_index")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _tool_call_ids(msg) -> set:
    """All tool_call ids a message issues (AIMessage) or answers (ToolMessage)."""
    if isinstance(msg, ToolMessage):
        tc_id = getattr(msg, "tool_call_id", None)
        return {tc_id} if tc_id else set()
    ids: set = set()
    for tc in getattr(msg, "tool_calls", None) or []:
        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if tc_id:
            ids.add(tc_id)
    return ids


def select_strip_targets(
    messages: list, epoch_index: int, finalized_idx: int
) -> list:
    """Anchor-whitelist selection inside the epoch-bounded range.

    The range is ``[epoch_index, finalized_idx)`` — the CURRENT planning
    round only. When the boundary lands at/after the marker (batch
    iteration with a stale durable epoch, or any reconstructed message
    list) the slice is empty and nothing is selected: fail-safe toward
    keeping context, never toward over-stripping. Messages without an id
    cannot receive a RemoveMessage (same guard as the compaction path in
    memory/hook.py) and are left in place.

    The finalized ToolMessage sits OUTSIDE the range (right bound is
    exclusive) and is retained on purpose — ``_phase2_kickoff_needed``
    locates it positionally. Its caller AIMessage (the finish_planning
    turn) is exempted from the strip as well (B44): stripping the caller
    left the retained ToolMessage orphaned, so ``sanitize_tool_pairing``
    dropped it before EVERY downstream LLM call — the "hand the summary
    to execution" intent never actually reached a model, and each
    inject/recover iteration logged a misleading orphan warning (case-32:
    the same orphan was dropped 6 times across two graphs). Sibling
    ToolMessages answering the SAME caller batch's other tool_calls are
    exempted too — a parallel ``finish_planning + probe`` turn would
    otherwise trade one orphan for another.
    """
    if finalized_idx is None or finalized_idx <= 0:
        return []
    if epoch_index >= finalized_idx:
        return []

    # Out-of-range finalized_idx (reconstructed/synthetic lists) has no
    # finalization message to read — same fail-safe as above: behave
    # exactly like the pre-B44 selection, strip by anchor whitelist only.
    finalized_msg = (
        messages[finalized_idx] if finalized_idx < len(messages) else None
    )
    finalized_call_id = (
        getattr(finalized_msg, "tool_call_id", None) or ""
        if finalized_msg is not None
        else ""
    )
    protected_call_ids: set = set()
    if finalized_call_id:
        for m in messages[epoch_index:finalized_idx]:
            if (
                not isinstance(m, AIMessage)
                or finalized_call_id not in _tool_call_ids(m)
            ):
                continue
            # Found the caller batch: protect ALL its tool_call ids so
            # the whole finalization turn stays paired.
            protected_call_ids = _tool_call_ids(m)
            break

    return [
        m
        for m in messages[epoch_index:finalized_idx]
        if getattr(m, "id", None)
        and not _is_context_anchor(m)
        and not (
            protected_call_ids
            and _tool_call_ids(m) & protected_call_ids
        )
    ]


def _archive_contains(store, task_id: str, to_strip: list) -> bool:
    """Read-back check: every to-strip message is now actually archived.

    ``read_session`` rebuilds from the on-disk snapshot + JSONL log, so
    a swallowed write failure reads back as missing — exactly the
    silent path the exception-based guard cannot see. Keys follow the
    same id-first dedup strategy ``append_messages`` itself uses.
    """
    from chaos_agent.memory.session_store import (
        _message_dedup_key,
        _serialize_message_full,
    )

    session = store.read_session(task_id)
    if session is None:
        return False
    archived = {_message_dedup_key(m) for m in session.get("messages") or []}
    archived.update(session.get("_baseline_keys") or set())
    for msg in to_strip:
        key = _message_dedup_key(_serialize_message_full(msg))
        if key not in archived:
            return False
    return True


def _persist_then_strip(state: AgentState, to_strip: list) -> list | None:
    """Archive the to-be-stripped messages first, then build the removals.

    Returns the RemoveMessage list on success, or None when the strip
    must be skipped (no task id / no store / archive append raised /
    read-back verification missed a message) — the caller then leaves
    the messages channel untouched.
    """
    task_id = state.get("task_id", "")
    if not task_id:
        logger.warning(
            "planning_handoff: no task_id — skipping strip "
            "(messages are not archived yet)"
        )
        return None
    from chaos_agent.memory.session_store import get_global_session_store

    store = get_global_session_store()
    if store is None:
        logger.warning(
            "planning_handoff: no session store — skipping strip "
            "(archive completeness takes priority)"
        )
        return None
    try:
        store.append_messages(task_id, to_strip, node_name=PLANNING_HANDOFF)
    except Exception:
        logger.warning(
            "planning_handoff: archive append failed — skipping strip",
            exc_info=True,
        )
        return None
    if not _archive_contains(store, task_id, to_strip):
        # append_messages is fire-and-forget by contract: a missing
        # active session and a swallowed disk-write failure BOTH return
        # normally. The read-back is the only signal that separates a
        # landed archive from a silently lost one (D5's absolute
        # priority: archive completeness over context slimming).
        logger.warning(
            "planning_handoff: archive read-back verification missed "
            "message(s) — skipping strip"
        )
        return None
    return [RemoveMessage(id=m.id) for m in to_strip]


def planning_handoff(state: AgentState) -> dict:
    """Graph node: strip the current planning round's process messages.

    Deterministic, synchronous, no LLM. Returns a state update carrying
    only ``messages`` (RemoveMessage entries) — every other key,
    ``attribution_epoch_index`` included, is deliberately absent so the
    reducer leaves it numerically unchanged.
    """
    messages = state.get("messages") or []
    finalized_idx = _find_finalized_idx(messages)
    if finalized_idx is None:
        # No finalization (planning rejected mid-flight, pure dialogue,
        # or a path that never finalized) — nothing to hand off.
        return {}

    to_strip = select_strip_targets(messages, _epoch_start(state), finalized_idx)
    if not to_strip:
        # Idempotent re-entry: the range already holds only anchors.
        return {}

    removals = _persist_then_strip(state, to_strip)
    if removals is None:
        return {}

    logger.info(
        "planning_handoff: stripped %d planning-round message(s) "
        "(range [%d, %d), %d anchor(s) retained)",
        len(removals),
        _epoch_start(state),
        finalized_idx,
        sum(1 for m in messages[_epoch_start(state):finalized_idx] if _is_context_anchor(m)),
    )
    return {"messages": removals}
