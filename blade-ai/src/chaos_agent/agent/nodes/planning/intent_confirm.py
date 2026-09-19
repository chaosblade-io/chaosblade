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

import json
import logging

from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage
from langgraph.types import interrupt

from chaos_agent.agent.intent_handoff import (
    is_previous_intent_residue as _is_previous_intent_residue,
    spec_relevance_tokens as _spec_relevance_tokens,
    word_contains as _word_contains,
)
from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.state import AgentState
from chaos_agent.memory.tui_session_store import persist_node_dialogue
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


async def _cancel_task_row(task_id: str) -> None:
    """Reject-path terminal write: stamp the row ``cancelled`` AND clear
    the pending-confirmation flag.

    The flag drop matters as much as the stamp: the row still carries
    ``needs_confirm=1`` from the clarification round, and inference would
    otherwise re-classify it as ``waiting_input`` on the next field-less
    flush — but no card is waiting, the user said no. The clear goes
    through ``upsert`` on purpose: inference runs on the merged record,
    the monotonicity guard keeps "cancelled", and the cleared flag keeps
    later flushes from re-deriving waiting_input.

    Best-effort: a store failure must never turn the user's decision
    into a graph error.
    """
    if not (task_id and task_id.startswith("inject-")):
        return
    try:
        from chaos_agent.persistence.task_store import get_task_store

        store = await get_task_store()
        if store is None:
            return
        await store.update_task_state(task_id, "cancelled")
        await store.upsert(task_id, needs_confirm=0)
    except Exception:
        logger.debug("Failed to cancel task row: %s", task_id, exc_info=True)


async def _revive_task_row(task_id: str) -> None:
    """Approval-path revive of a previously-cancelled row.

    Ids are reused across rejections, so an approval must un-cancel the
    row — the monotonicity guard would otherwise pin "cancelled" for the
    whole run. Deliberately conditional:

    * row missing  → no-op: ``update_task_state`` on a missing row would
      INSERT a bare ``injecting`` row (a fresh ghost — the row is born
      naturally as pending/injecting with evidence on the first sync);
    * ``cancelled`` / ``waiting_input`` → stamp ``injecting`` (a fresh
      confirmation round parked the row at waiting_input — the approval
      moves it into execution);
    * any other state (terminal verdicts) → no-op: never clobber a
      verdict.

    Best-effort, same as the cancel write.
    """
    if not (task_id and task_id.startswith("inject-")):
        return
    try:
        from chaos_agent.persistence.task_store import get_task_store

        store = await get_task_store()
        if store is None:
            return
        row = await store.get(task_id)
        if row and row.get("task_state") in ("cancelled", "waiting_input"):
            await store.update_task_state(task_id, "injecting")
    except Exception:
        logger.debug("Failed to revive task row: %s", task_id, exc_info=True)


# Trim window: how many tail messages survive untouched on commit.
# Picked to mirror the previous ``intent_clarification`` fast-path
# behaviour (last 4) so post-commit Phase 1 LLM context size matches
# the pre-Option-A baseline.
_TRIM_TAIL_KEEP = 4


def _format_intent_summary(fault_intent: dict) -> str:
    """Format fault_intent dict into a human-readable summary."""
    parts = []
    parts.append(f"Fault type: {fault_intent.get('fault_type', 'unknown')}")
    parts.append(f"Scope: {fault_intent.get('scope', 'unknown')}")
    parts.append(f"Target: {fault_intent.get('target', 'unknown')}")
    parts.append(f"Action: {fault_intent.get('action', 'unknown')}")
    parts.append(f"Namespace: {fault_intent.get('namespace', 'unknown')}")
    # Always rendered — every fault injection is bounded in time, and the
    # user must see the applied duration (user-stated or system recommended)
    # before approving.
    parts.append(f"Duration: {fault_intent.get('duration_seconds', 0)}s")
    if fault_intent.get("labels"):
        parts.append(f"Label selector: {fault_intent['labels']}")
    if fault_intent.get("names"):
        parts.append(f"Target resources: {', '.join(fault_intent['names'])}")
    if fault_intent.get("params"):
        params_str = ", ".join(f"{k}={v}" for k, v in fault_intent["params"].items())
        parts.append(f"Parameters: {params_str}")
    if fault_intent.get("user_description"):
        parts.append(f"User description: {fault_intent['user_description']}")
    return "\n".join(parts)


def _build_handoff_summary(fault_intent: dict, dialogue_round: int) -> SystemMessage:
    """Build the ``[Intent Clarification Summary]`` SystemMessage that
    marks the boundary between intent dialogue and inject execution.

    Format and content are kept identical to the pre-Option-A summary
    that ``intent_clarification`` used to produce — downstream consumers
    (``session_store._split_at_handoff`` and the handoff extraction in
    ``cli/runner.py``'s create-session blocks — the create_session call
    sites of ``inject_stream`` and ``run``) match on the
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
    while preserving operation summaries and ``[Compressed History]``
    markers.

    Operation summaries record previous inject/batch/recover results —
    the LLM needs them to answer "what happened last time?" across
    multiple tasks in the same session. Compressed history summaries are
    the output of PreReasoningHook's LLM compaction and must survive
    trimming for the same reason.

    Interruption records are preserved for a stronger reason still: they are the
    only place the dialogue learns that an operation stopped part-way and that a
    fault may still be live. Dropping one would silently lose that warning.
    """
    if len(messages) <= _TRIM_TAIL_KEEP:
        return []
    _PRESERVE_PREFIXES = (
        "[Task Summary]",
        "[Task Interrupted]",
        "[Batch Summary]",
        "[Recover Summary]",
        "[Compressed History]",
    )
    remove_list: list[RemoveMessage] = []
    for msg in messages[:-_TRIM_TAIL_KEEP]:
        content = getattr(msg, "content", "") or ""
        if any(content.startswith(p) for p in _PRESERVE_PREFIXES):
            continue
        msg_id = getattr(msg, "id", None)
        if msg_id:
            remove_list.append(RemoveMessage(id=msg_id))
    return remove_list


# ── Probe snapshot harvest (tier1-speedup) ─────────────────────────

#: Read-only observation tools whose ToolMessage results the fallback
#: extractor scans. ``kubectl_read`` covers get/describe/top/logs and
#: read-only exec (``ps aux``); ``host_read`` covers host-side observation.
#: ``blade_help`` / ``blade_status`` produce no target facts and are
#: deliberately excluded — scanning them would only add noise.
_PROBE_SOURCE_TOOLS = ("kubectl_read", "host_read")

#: Whole-snapshot ceiling. Slightly under the ledger's FACTS_CAP so the
#: snapshot section stays in the same context-weight class as the ledger
#: section it complements.
_SNAPSHOT_MAX_FACTS = 12

#: Per-fact character ceiling — identical to the ledger's VALUE_CHAR_CAP
#: so a snapshot fact is never fatter than the ledger fact it mirrors.
_SNAPSHOT_FACT_CHAR_CAP = 200


def _maybe_json(value):
    """Best-effort decode of a JSON-encoded string into its structure.

    Models do pass dicts/lists as JSON *strings* in tool_call args (the
    same mis-formatting ``progress_ledger._maybe_json`` guards against).
    Anything that is not JSON-shaped is returned unchanged.
    """
    if isinstance(value, str) and value.lstrip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _latest_established_facts(messages: list) -> list:
    """Return the established_facts list from the LAST update_progress
    tool_call that carried one, in the model's own order.

    The ledger's state layer is shallow-overwrite: the last call carrying
    ``established_facts`` defines the ledger's final view. The snapshot
    must agree with that view exactly — the ``[FAULT INTENT]`` section
    declares itself same-source as the ledger section — so facts from
    earlier calls (which the model may have deliberately rewritten to
    drop wrong entries) are NOT merged back in.
    """
    for msg in reversed(messages):
        if getattr(msg, "type", "") != "ai":
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc.get("name") != "update_progress":
                continue
            args = _maybe_json(tc.get("args"))
            if not isinstance(args, dict):
                continue
            state_update = _maybe_json(args.get("state_update"))
            if not isinstance(state_update, dict):
                continue
            facts = state_update.get("established_facts")
            if isinstance(facts, str):
                facts = [facts]
            if isinstance(facts, list):
                cleaned = [f.strip() for f in facts if isinstance(f, str) and f.strip()]
                if cleaned:
                    return cleaned
    return []


def _fallback_rows(messages: list, names: list, param_values: list) -> list:
    """Deterministic row extraction from read-only tool results.

    The fallback exists for the case the model forgot to self-record:
    it scans ToolMessage results of whitelisted read-only tools and keeps
    only rows that name the target — a spec name or a param value as a
    whole token — plus ``Restart Policy:`` lines from tool calls that
    targeted a spec name. Generic tool output never leaks in. Rows are
    returned in message order (oldest first); the caller fills the
    remaining snapshot budget from the newest end.
    """
    # tool_call_id → args, so a describe's Restart Policy line can be tied
    # to a call that actually named the target.
    call_args_by_id: dict = {}
    for msg in messages:
        if getattr(msg, "type", "") != "ai":
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc.get("id"):
                call_args_by_id[tc["id"]] = tc.get("args") or {}

    rows: list = []
    for msg in messages:
        if getattr(msg, "type", "") != "tool":
            continue
        tool_name = getattr(msg, "name", "") or ""
        if tool_name not in _PROBE_SOURCE_TOOLS:
            continue
        content = getattr(msg, "content", None)
        if not isinstance(content, str):
            continue
        args_text = json.dumps(
            call_args_by_id.get(getattr(msg, "tool_call_id", ""), {}),
            ensure_ascii=False,
        )
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("---") or len(line) > 500:
                continue
            hit = _first_mentioned(line, names)
            if hit:
                # resource row: get/describe/-o wide line naming the target
                rows.append({"fact": line, "source_tool": tool_name, "key": hit})
                continue
            hit = _first_mentioned(line, param_values)
            if hit:
                # ps aux / process row naming the target process
                rows.append({"fact": line, "source_tool": tool_name, "key": hit})
                continue
            low = line.lower()
            if low.startswith("restart policy:") and any(n in args_text for n in names):
                rows.append({"fact": line, "source_tool": tool_name, "key": "restartpolicy"})
    return rows


def _first_mentioned(text: str, candidates: list) -> str:
    """First candidate occurring in ``text`` as a whole word.

    Word-boundary matching (not bare substring) keeps the "complete
    string match" discipline — ``drill-target`` must not match
    ``drill-target-2`` — while still catching the forms real tool output
    uses: ``pod/drill-target``, ``nginx: master``, ``/usr/sbin/nginx``.
    """
    for c in candidates:
        if _word_contains(text, c):
            return c
    return ""


def _harvest_probe_snapshot(messages: list, spec, *, now: str | None = None) -> dict | None:
    """Dual-source harvest of what intent clarification established about
    the target environment, frozen just before the clarification history
    is trimmed away.

    Primary source — the model's own ``update_progress`` records: every
    established_fact with the model's wording, taken from the LAST call
    that carried the list (same-source as the ledger's final view).
    Cross-intent guard: the intent graph keeps its messages across
    rejected/abandoned intents (deliberately — a continued conversation
    iterates on established context), so that last record may belong to a
    PREVIOUS intent about a DIFFERENT target. The ledger keeps such
    continuity on purpose, but this snapshot section claims "Established
    while clarifying the intent" — a record naming none of THIS intent's
    tokens is previous-intent residue with a fabricated probed_at. The
    whole batch is dropped in that case; the spec-driven fallback stays
    clean either way.

    Fallback — deterministic keyword rows from whitelisted read-only tool
    results (spec names / param values as whole tokens, Restart Policy
    lines). When both sources carry the same information, the self-recorded
    entry wins: the fallback row is dropped if any recorded fact already
    mentions its key token, so the 12-entry budget is not spent twice on
    one fact.

    ``probed_at`` is the harvest moment for every entry. LangChain message
    objects carry no timestamps (and langchain_openai does not surface the
    API's ``created``), so per-message times do not exist inside the graph;
    the snapshot's consumption semantics — "age since the handoff
    baseline" — is exactly what one shared timestamp provides.

    Caps: ≤ 12 entries total (self-recorded kept first, overflow drops the
    oldest = list head, mirroring the ledger's keep-tail rolling), ≤ 200
    chars per fact. Any failure degrades to ``None`` — a missing snapshot
    is the pre-change behaviour and must never block the handoff.
    """
    try:
        recorded = _latest_established_facts(messages)
        names = [n for n in (getattr(spec, "names", ()) or ()) if isinstance(n, str) and n.strip()]
        params = getattr(spec, "params", None) or {}
        param_values = [
            v.strip() for v in params.values()
            if isinstance(v, str) and len(v.strip()) >= 3 and not v.strip().isdigit()
        ]
        # Cross-intent staleness guard (see docstring): relevance tokens are
        # this intent's names / param values / namespace. Whole-BATCH check
        # via ``_is_previous_intent_residue`` — a causal-insight fact need
        # not name the target itself as long as its batch does.
        if _is_previous_intent_residue(recorded, _spec_relevance_tokens(spec)):
            recorded = []

        recorded_blob = "\n".join(recorded)
        fallback = []
        for row in _fallback_rows(messages, names, param_values):
            key = row["key"]
            if not key:
                continue
            if key == "restartpolicy":
                policy_value = row["fact"].split(":", 1)[-1].strip()
                low = recorded_blob.lower()
                if "restartpolicy" in low or (policy_value and policy_value.lower() in low):
                    continue  # self-recorded already covers it
            elif _word_contains(recorded_blob, key):
                continue  # self-recorded already mentions this target token
            fallback.append(row)

        ts = now or now_iso()
        facts = [
            {
                "fact": f[:_SNAPSHOT_FACT_CHAR_CAP],
                "source_tool": "update_progress",
                "probed_at": ts,
            }
            # Keep the TAIL: the ledger's own rolling keeps the most recent
            # FACTS_CAP entries, and the snapshot must drop the same (oldest,
            # head) entries the ledger would.
            for f in recorded[-_SNAPSHOT_MAX_FACTS:]
        ]
        budget = _SNAPSHOT_MAX_FACTS - len(facts)
        for row in reversed(fallback):  # newest tool results first
            if budget <= 0:
                break
            facts.append({
                "fact": row["fact"][:_SNAPSHOT_FACT_CHAR_CAP],
                "source_tool": row["source_tool"],
                "probed_at": ts,
            })
            budget -= 1
        return {"facts": facts} if facts else None
    except Exception:
        logger.debug("probe snapshot harvest failed; continuing without snapshot", exc_info=True)
        return None


def _commit_inject_handoff(state: AgentState, fault_intent: dict, spec=None) -> dict:
    """Run the inject pipeline handoff and produce the state delta.

    Dual-graph model: ``handoff_summary`` is read by the Runner to
    seed Pipeline Graph messages. bootstrap_task_session is called
    by the Runner layer, not here.
    """
    messages = state.get("messages", [])
    dialogue_round = int(state.get("dialogue_round") or 0)
    summary_msg = _build_handoff_summary(fault_intent, dialogue_round)
    # Harvest the probe snapshot BEFORE the trim removes the clarification
    # history the harvester reads — this is the last moment those tool
    # results and self-recorded facts still exist in ``messages``. Written
    # unconditionally: ``None`` overwrites any snapshot left by a previous
    # fault in the same session (the field is durable, so a stale snapshot
    # would otherwise leak into this fault's plan context).
    probe_snapshot = _harvest_probe_snapshot(
        messages, spec if spec is not None else read_fault_spec(state),
    )
    remove_list = _build_trim_remove_list(messages)

    # Cross-intent guard for the LEDGER handoff (same residue class the
    # snapshot guard above drops): the ledger's ``established_facts`` are
    # the LAST update_progress batch — the very list the snapshot's
    # primary source checks. When that batch belongs to a PREVIOUS intent
    # about a different target (rejected/abandoned, then the user changed
    # targets and the new clarification never re-recorded), the snapshot
    # drops it — but the ledger bridge would otherwise copy it verbatim
    # into the pipeline, where the plan's system prompt renders it under
    # "do not re-derive what is already established" and the
    # seed-anchor-and-preserve branch then carries it through
    # execute/verify/recover. Hand off None in that case (pre-change
    # baseline); the IntentState copy is wiped by the dispatch clear
    # anyway, so nothing session-scoped is lost.
    ledger = state.get("progress_ledger")
    if isinstance(ledger, dict) and ledger:
        _st = ledger.get("state")
        _facts = _st.get("established_facts") if isinstance(_st, dict) else None
        if _is_previous_intent_residue(
            list(_facts or []),
            _spec_relevance_tokens(spec if spec is not None else read_fault_spec(state)),
        ):
            ledger = None

    return {
        "messages": remove_list,
        "handoff_summary": summary_msg.content,
        "probe_snapshot": probe_snapshot,
        "progress_ledger": ledger,
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

    # Persisted or external state can bypass the dialogue tool schema. Do not
    # render an approval gate for an incomplete or unregistered fault domain.
    if spec is None or not spec.is_complete:
        logger.warning("Rejecting incomplete fault intent before confirmation: %r", fault_intent)
        refusal = AIMessage(
            content="The fault intent has a missing or unsupported scope, so it did not enter execution confirmation; fix it and resubmit."
        )
        # Write it ourselves: the next ``intent_clarification`` turn rebuilds the
        # persist list from scratch and only back-fills ToolMessages from history,
        # so an AIMessage another node left in state is never picked up.
        persist_node_dialogue(state.get("tui_session_id", ""), [refusal])
        # Same terminal write as an explicit user rejection — the row
        # may already carry fault_spec evidence (written during
        # clarification), so it would otherwise project "injecting".
        await _cancel_task_row(task_id)
        return {
            "confirmed_intent": None,
            "intent_reasoning": "fault intent is incomplete or uses an unsupported scope",
            "messages": [refusal],
        }

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
                "Dry-Run: skipping intent confirmation, moving to plan generation",
                {"dry_run": True, "fault_intent": fault_intent},
            )
            tracker.complete("Dry-Run: bypassed Layer-1 confirm")
        logger.info("intent_confirm bypassed for dry_run task %s", task_id)
        # Dry-Run enters the pipeline too — same revive as approval (the
        # id may carry a "cancelled" from an earlier rejection).
        await _revive_task_row(task_id)
        # Dry-Run mirrors the approved path: ``/plan <NL>`` runs the
        # full inject pipeline as a preview, so the downstream
        # agent_loop / safety_check stages need the same clean
        # ``[Intent Clarification Summary]`` handoff and trimmed
        # message list they would see on a real approval. Skipping
        # this would leave Phase 1 reading the verbose clarification
        # dialogue and produce a different plan preview than the
        # post-Option-A approved flow.
        return _commit_inject_handoff(state, fault_intent, spec)

    if tracker:
        tracker.start(
            StatusCategory.NODE,
            "intent_confirm",
            "Waiting for the user to confirm the fault-injection intent",
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
    #   · ``clarification_round`` — how many user turns were spent
    #                                clarifying the intent before submission:
    #                                every fresh turn after the opening
    #                                counts one; a turn that only replayed an
    #                                already-reviewed contract is refunded,
    #                                while a turn whose submission bootstrapped
    #                                the contract (no review pre-existed it)
    #                                keeps its count (0 = one-shot
    #                                convergence). The TUI renders this field
    #                                only when N>0.
    batch_args = state.get("batch_submit_args")
    if batch_args and isinstance(batch_args, dict) and batch_args.get("faults"):
        batch_faults = batch_args["faults"]
        batch_lines = [f"Batch fault injection: {len(batch_faults)} fault(s) (serial execution)"]
        for i, f in enumerate(batch_faults, 1):
            item_spec = read_fault_spec({"fault_spec": f}) if isinstance(f, dict) else None
            if item_spec is not None and not item_spec.fault_target:
                item_spec = None
            scope = item_spec.scope if item_spec else f.get("scope", "")
            target = item_spec.fault_target if item_spec else f.get("target", "")
            action = item_spec.fault_action if item_spec else f.get("action", "")
            namespace = item_spec.namespace if item_spec else f.get("namespace", "")
            names = list(item_spec.names) if item_spec else f.get("names", [])
            duration = item_spec.duration_seconds if item_spec else f.get("duration_seconds", 0)
            batch_lines.append(
                f"  {i}. {scope}-{target}-{action} "
                f"@ {namespace}/{', '.join(names) or '*'} ({duration}s)"
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
        "fault_revision": spec.revision,
    }

    # Interrupt: TUI renders the summary and collects Y/N
    decision = interrupt(confirmation_info)

    if decision == "approved":
        if tracker:
            tracker.complete("User confirmed the intent, moving to the execution stage")
        logger.info("Intent confirmed by user: %s", fault_intent.get("fault_type"))
        # Revive in case this id was cancelled by an earlier rejection on
        # the same conversation (id reuse contract) — the monotonicity
        # guard would otherwise pin "cancelled" for the whole run.
        await _revive_task_row(task_id)
        # Option A handoff: the trim + bootstrap side effects used to
        # fire from ``intent_clarification`` the moment intent
        # converged, which meant the working messages list shrank even
        # when the user later rejected at this gate. Moving them to
        # the approved branch keeps the full clarification dialogue
        # alive across rejections (so a continued conversation can
        # refine, not restart) and stops orphan task files from being
        # created for rejected intents.
        return _commit_inject_handoff(state, fault_intent, spec)
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
            tracker.complete("User rejected the intent, returning to conversation")
        logger.info("Intent rejected by user, returning to conversation")
        # Terminal write for the TaskStore row: the id stays reserved for
        # reuse, but its sqlite row must stop reporting "injecting".
        # Direct column write; the monotonicity guard keeps later
        # field-less flushes from regressing it.
        await _cancel_task_row(task_id)
        # The reviewed FaultSpec remains available for the next turn. It is
        # replaced only by a new explicit proposal, never merged from prose.
        return {
            "confirmed_intent": None,
        }
