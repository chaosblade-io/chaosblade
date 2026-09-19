"""Tool-pairing integrity for outbound LLM message sequences.

OpenAI chat/completions enforces a protocol invariant: every ``tool``
role message must respond to a preceding assistant message carrying a
matching ``tool_calls`` entry. Lenient providers (DashScope — measured,
see ``handle_truncated_response`` in nodes/execute/react_helpers.py)
accept violations silently; strict providers (DeepSeek) reject them
with a 400 that aborts the whole turn (chaosblade-io/chaosblade#1344,
failure signature #2).

This module is the SINGLE enforcement point for that invariant. It is
wired into ``ResilientChatOpenAI.ainvoke/invoke`` (agent/resilient_llm.py)
before the retry loop, so every LLM call built by ``make_llm`` — including
calls through ``bind_tools`` bindings, which delegate back to the same
wrapped methods — is sanitized regardless of which graph node, memory
operation, or synthetic-message builder produced the sequence.

Design rules (locked by tests/test_utils/test_message_integrity.py):

* Caller-id collection covers ALL THREE places a tool_call id can live:
  ``tool_calls`` (parsed), ``invalid_tool_calls`` (truncated-stream calls
  keep their id ONLY here), and ``additional_kwargs["tool_calls"]`` (raw
  provider dicts). Missing any one source misclassifies legal pairs as
  orphans.
* Falsy ids never enter the caller-id set — ``invalid_tool_calls`` entries
  can carry ``id=None``, and a bare-value set would let ``None == None``
  forge a pairing between two unrelated messages.
* A ToolMessage is an orphan when its ``tool_call_id`` is non-empty and
  absent from the caller-id set, OR when it is empty (malformed — no
  provider accepts it either way). Orphans are dropped with a WARNING
  carrying the id, a content preview, and the drop count.
* PRECEDENCE is enforced too, not just matching. Dropping orphans only
  covers "has a caller somewhere in the list"; a result sitting BEFORE its
  caller is equally illegal and used to sail straight through (measured:
  an ungated consumer shipping such a state lost nothing but produced a
  sequence a strict provider rejects). ``sanitize_tool_pairing`` therefore
  MOVES a premature result to just after its caller. Moving rather than
  dropping, because the message is real evidence and the reversal is a
  benign, reproducible artefact of how state is merged (below).
* The REVERSE direction — every ``tool_calls`` entry has a following answer —
  is enforced by the sibling ``answer_dangling_tool_calls``, NOT by
  ``sanitize_tool_pairing`` (which decides per ToolMessage and so is blind to a
  caller with no result). It fills each dangling caller with an honest "not
  executed" placeholder built by an INJECTED factory, keeping this module
  langchain-free; see its docstring for the reachable producer and the
  never-fabricate-a-result rule.
* Clean sequences return THE SAME list object (zero-copy) — this runs on
  every LLM call, so the happy path must neither copy the list nor scan it
  twice beyond one O(n) precedence check.

The module also carries the DEDUP half of the same invariant, for synthetic
(agent-fabricated) tool pairs such as the verifier's baseline injection:
``synthetic_pairs_intact`` audits whether a required pair set is present,
unique and correctly ordered, and ``drop_messages_with_tool_call_ids``
clears the damaged remnants before a rebuild. Both halves are needed —
a rebuild that leaves the old survivor in place answers one tool_call
twice, the single violation ``sanitize_tool_pairing`` structurally cannot
see (both copies have a caller, so both look legal).

CONTRACT BOUNDARY — what this module does and does not fix:

It repairs the SEQUENCE BEING SENT. It does not repair ``state["messages"]``,
and for a reversed synthetic pair it cannot: the pairs are built with STABLE
langchain ids so that a rebuild stays idempotent, and ``add_messages``
(langgraph) merges by id — an id already in state is REPLACED IN PLACE at its
old index, a new id is APPENDED. When damage loses the caller but leaves the
result, the rebuild therefore re-appends the caller at the end while the
result is pinned to its old early slot. The state pair stays reversed forever
and the dedup gate rebuilds on every turn (measured over three turns:
``caller@4 result@1`` unchanged, no state growth, sequence shipped each turn
legal). Re-ordering state is not available either: emitting a
``RemoveMessage`` for the pinned id in the same update is a no-op, because the
merge loop does ``ids_to_remove.discard(m.id)`` when a same-id message arrives
later in the same batch.

So a reversed state is a tolerated, non-convergent condition, not a bug in
the gate — callers distinguish it from real damage via
``diagnose_synthetic_pairs`` and log it at INFO. Correctness does not depend
on state order: every path that hands a sequence to a provider goes through
``ainvoke``/``invoke`` (see the astream constraint below), so the repair runs
regardless of which node built the list or whether that node has its own
dedup gate.

CONSTRAINT (astream): this gate only covers ``ainvoke``/``invoke``.
``ResilientChatOpenAI`` deliberately does not wrap ``astream`` (retrying a
partially-yielded stream would duplicate tokens), and the agent never
calls ``llm.astream`` directly — UI token streaming comes from graph-level
``astream_events`` tapping ``on_chat_model_stream`` events emitted inside
``ainvoke``. Any future direct ``llm.astream`` call site MUST add the same
sanitize gate first, or Layer 1 is bypassed entirely.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "sanitize_tool_pairing",
    "answer_dangling_tool_calls",
    "synthetic_pairs_intact",
    "diagnose_synthetic_pairs",
    "log_pair_rebuild",
    "log_pair_absent",
    "apply_synthetic_pair_gate",
    "PAIR_INTACT",
    "PAIR_REORDERED",
    "PAIR_DAMAGED",
    "drop_messages_with_tool_call_ids",
]

# Characters of ToolMessage content echoed into the drop WARNING — enough
# to recognise which tool result was lost without flooding the log.
_PREVIEW_CHARS = 200


def _is_message(obj: Any) -> bool:
    """Duck-type check: langchain messages (and state dicts) expose ``type``.

    No langchain import on purpose — this module stays usable from any
    layer and tolerates dict-shaped messages persisted in state.
    """
    if isinstance(obj, dict):
        return "type" in obj
    return hasattr(obj, "type")


def _msg_attr(msg: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a message object or dict uniformly."""
    if isinstance(msg, dict):
        return msg.get(name, default)
    return getattr(msg, name, default)


#: langchain's ``.type`` is a class-derived literal, and the streaming chunk
#: classes do NOT inherit their base's value — measured, not assumed:
#: ``AIMessage.type == "ai"`` but ``AIMessageChunk.type == "AIMessageChunk"``.
#: Comparing against ``"ai"``/``"tool"`` therefore silently skips chunks,
#: which would drop a legitimate ToolMessage as an orphan (its caller's id was
#: never collected). ``add_messages`` converts chunks before they reach graph
#: state, so this is defence for any call site that hands an aggregated chunk
#: straight to ``ainvoke``.
_CHUNK_TYPE_ALIASES = {
    "AIMessageChunk": "ai",
    "ToolMessageChunk": "tool",
    "HumanMessageChunk": "human",
    "SystemMessageChunk": "system",
    "FunctionMessageChunk": "function",
    "ChatMessageChunk": "chat",
}


def _msg_type(msg: Any) -> str:
    """The langchain role of ``msg``, with chunk classes normalised to their
    base role. Empty string when the message carries no usable ``type``.
    """
    raw = _msg_attr(msg, "type", None)
    if not isinstance(raw, str):
        return ""
    return _CHUNK_TYPE_ALIASES.get(raw, raw)


def _message_tool_call_ids(msg: Any) -> list[str]:
    """Tool-call ids carried by ONE message, from all three id homes.

    Single source of truth for "where can a tool_call id live" — both the
    orphan scan and the synthetic-pair audit go through this, so the two
    can never disagree about what counts as a caller.

    De-duplicated PER MESSAGE, and that is load-bearing: a message parsed
    from a real provider response carries the SAME id in two homes at once
    (``additional_kwargs["tool_calls"]`` keeps the raw dicts while
    ``tool_calls`` holds the parsed entries). Counting occurrences instead of
    messages would report one caller as two and fail the pair audit on every
    genuine response.
    """
    ids: list[str] = []
    seen: set[str] = set()

    def _add(tc_id: Any) -> None:
        if tc_id and tc_id not in seen:
            seen.add(tc_id)
            ids.append(tc_id)

    for tc in _msg_attr(msg, "tool_calls", None) or []:
        _add(tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None))
    for tc in _msg_attr(msg, "invalid_tool_calls", None) or []:
        _add(tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None))
    kwargs = _msg_attr(msg, "additional_kwargs", None) or {}
    for tc in kwargs.get("tool_calls", None) or []:
        if isinstance(tc, dict):
            _add(tc.get("id"))
        else:  # pragma: no cover - raw kwargs entries are dicts
            _add(getattr(tc, "id", None))
    return ids


def _collect_caller_ids(messages: list) -> set[str]:
    """Every tool_call id any message in ``messages`` asks to be answered.

    Union of the three id homes (see module docstring); falsy ids are
    skipped so ``None``/``""`` can never forge a pairing.
    """
    ids: set[str] = set()
    for msg in messages:
        if _msg_type(msg) != "ai":
            continue
        ids.update(_message_tool_call_ids(msg))
    return ids


def _collect_answered_ids(messages: list) -> set[str]:
    """Every tool_call id in ``messages`` that already HAS a ToolMessage answer.

    Mirror of ``_collect_caller_ids``: that asks "which ids does this sequence
    want answered", this asks "which are already answered". Their difference is
    exactly the set of dangling callers ``answer_dangling_tool_calls`` fills.
    Falsy ids are skipped, so an empty/None ``tool_call_id`` can never count as
    an answer — ``sanitize_tool_pairing`` drops such a message as an orphan.
    """
    ids: set[str] = set()
    for msg in messages:
        if _msg_type(msg) != "tool":
            continue
        tc_id = _msg_attr(msg, "tool_call_id", None)
        if tc_id:
            ids.add(tc_id)
    return ids


def _preview(content: Any) -> str:
    text = content if isinstance(content, str) else str(content)
    if len(text) > _PREVIEW_CHARS:
        return text[:_PREVIEW_CHARS] + "..."
    return text


def _caller_positions(messages: list) -> dict[str, int]:
    """First index at which each tool_call id is asked for.

    First-wins on purpose: with a duplicated caller the earlier one is the
    position a result must follow to be legal, and moving it there is the
    smaller edit.
    """
    positions: dict[str, int] = {}
    for idx, msg in enumerate(messages):
        if _msg_type(msg) != "ai":
            continue
        for tc_id in _message_tool_call_ids(msg):
            positions.setdefault(tc_id, idx)
    return positions


def _count_premature_results(messages: list, caller_ids: set[str]) -> int:
    """How many ToolMessages appear BEFORE the caller they answer.

    Restricted to ids in ``caller_ids`` so this agrees exactly with what
    ``_repair_precedence`` can act on: a result with no caller anywhere is
    an orphan (already dropped upstream), not a precedence violation, and
    counting it here would build a copy of the list to move nothing.
    """
    seen: set[str] = set()
    premature = 0
    for msg in messages:
        msg_type = _msg_type(msg)
        if msg_type == "ai":
            seen.update(_message_tool_call_ids(msg))
        elif msg_type == "tool":
            tc_id = _msg_attr(msg, "tool_call_id", None) or ""
            if tc_id in caller_ids and tc_id not in seen:
                premature += 1
    return premature


def _repair_precedence(messages: list, caller_ids: set[str]) -> tuple[list, list[str]]:
    """Move every premature ToolMessage to just after its caller.

    The pairing rule has two halves — a matching caller, and that caller
    PRECEDING the result. Dropping orphans enforces the first half only: a
    result whose caller sits later in the list has a match, so it survives,
    and the sequence ships illegal. This enforces the second half.

    Moving is chosen over dropping because the message is real evidence and
    the reversal is a reproducible artefact of state merging, not corruption
    of the content (see CONTRACT BOUNDARY in the module docstring). It is
    also never a perturbation of a legal sequence: no provider-accepted
    conversation contains a result before its call, so the only inputs this
    rewrites are ones that would already have been rejected.

    Single pass with a defer-and-flush: a premature result is held until its
    caller's index is reached, then emitted right after it. Deferred buckets
    are keyed by an AI caller's index, and an AI message is never itself
    deferred, so every bucket is guaranteed to flush inside the loop.

    Returns ``(messages_unchanged, [])`` without copying when the sequence
    already satisfies precedence — the happy path on every LLM call.
    """
    if not _count_premature_results(messages, caller_ids):
        return messages, []

    positions = _caller_positions(messages)
    deferred: dict[int, list] = {}
    out: list = []
    moved: list[str] = []
    for idx, msg in enumerate(messages):
        if _msg_type(msg) == "tool":
            tc_id = _msg_attr(msg, "tool_call_id", None) or ""
            caller_at = positions.get(tc_id)
            if caller_at is not None and caller_at > idx:
                deferred.setdefault(caller_at, []).append(msg)
                moved.append(tc_id)
                continue
        out.append(msg)
        if idx in deferred:
            out.extend(deferred.pop(idx))
    for bucket in deferred.values():  # pragma: no cover - see docstring
        out.extend(bucket)
    return out, moved


def sanitize_tool_pairing(messages: list) -> list:
    """Make ``messages`` satisfy the pairing rule before it is sent.

    Two forward passes for the drop half (they cannot be merged — caller-id
    collection must complete before orphan decisions, or a caller appearing
    AFTER its ToolMessage in the scan order would be misjudged): collect all
    caller ids, then filter tool messages against that set. A third pass
    repairs precedence (``_repair_precedence``), which is what stops a
    reversed-but-matched pair from shipping.

    Returns the ORIGINAL list object when nothing is dropped or moved.
    """
    if not messages or not isinstance(messages, list):
        return messages

    caller_ids = _collect_caller_ids(messages)

    kept: list = []
    dropped: list[tuple[str, str]] = []  # (tool_call_id or "<empty>", preview)
    for msg in messages:
        if _is_message(msg) and _msg_type(msg) == "tool":
            tc_id = _msg_attr(msg, "tool_call_id", None) or ""
            if not tc_id:
                dropped.append(("<empty>", _preview(_msg_attr(msg, "content", ""))))
                continue
            if tc_id not in caller_ids:
                dropped.append((tc_id, _preview(_msg_attr(msg, "content", ""))))
                continue
        kept.append(msg)

    out = messages if not dropped else kept
    out, moved = _repair_precedence(out, caller_ids)

    if not dropped and not moved:
        return messages

    for tc_id, preview in dropped:
        reason = "empty tool_call_id" if tc_id == "<empty>" else f"no caller with id {tc_id!r}"
        logger.warning(
            "Dropping orphan ToolMessage before LLM call (%s): %s",
            reason,
            preview,
        )
    if dropped:
        logger.warning(
            "sanitize_tool_pairing dropped %d orphan ToolMessage(s); "
            "this means an upstream message operation produced an unpaired "
            "tool result — check memory compaction / synthetic-message "
            "builders for the source.",
            len(dropped),
        )
    if moved:
        logger.warning(
            "sanitize_tool_pairing moved %d ToolMessage(s) that PRECEDED their "
            "own tool_call (ids %s) to just after it. Nothing was lost, but "
            "the sequence reached this gate protocol-illegal — a reversed "
            "synthetic pair left in state by an id-based merge is the known "
            "benign source (see message_integrity CONTRACT BOUNDARY); any "
            "other source means a message operation split a pair.",
            len(moved),
            sorted(set(moved)),
        )
    if not out:
        # Every message was an orphan. The request still goes out (this gate
        # sanitises, it does not veto), but no provider accepts an empty
        # message list, so the turn is already lost — and the 400 that comes
        # back describes the SYMPTOM, not this cause. Log the cause at ERROR
        # so the two can be correlated after the fact.
        logger.error(
            "sanitize_tool_pairing emptied the ENTIRE message list (%d "
            "message(s), all unpaired tool results). The provider will reject "
            "the request with a 400 about an empty/invalid message array; the "
            "real fault is local conversation state, not the provider. "
            "Dropped: %s",
            len(dropped),
            [tc_id for tc_id, _ in dropped],
        )
    return out


def answer_dangling_tool_calls(messages: list, answer_factory: Callable[[str], Any]) -> list:
    """Give every UNANSWERED tool_call an honest placeholder answer.

    The REVERSE half of the pairing protocol, and the sibling of
    ``sanitize_tool_pairing`` (which enforces the forward half: every result
    has a preceding caller). A strict provider rejects a ``tool_calls`` entry
    with no following ``tool`` message just as it rejects an orphan result
    (module docstring, failure signature #2), but ``sanitize_tool_pairing`` is
    one-directional — it decides per ToolMessage and so is structurally blind
    to a caller that has no result.

    The known reachable producer is the execute-loop short-circuit: when the
    router sends the run to the verifier on budget / wall-clock / error, it
    skips ``phase2_tools``, so the model's last ``tool_calls`` batch is never
    answered. ``execute_loop._answer_replan_tool_calls`` already fills the SAME
    gap for the replan exit — at the source, persisted, with honest "Not
    executed: ..." wording; this is the generic send-side backstop that also
    covers that exit's siblings plus any other producer (a truncated-stream
    call ``handle_truncated_response`` skipped, a synthetic fragment whose
    result half was lost).

    ``answer_factory`` builds the placeholder for ONE dangling ``tool_call_id``.
    It is INJECTED because this module is deliberately langchain-free (see the
    imports); the caller (``agent/resilient_llm.py``) supplies a real
    ``ToolMessage``. The placeholder MUST be an honest "not executed" note and
    MUST NOT fabricate a tool result — this agent's evidence chain is its
    product, so a call that never ran is answered as never having run.

    Returns the ORIGINAL list object when nothing dangles (zero-copy: this runs
    on every LLM call, and a provider-accepted conversation has every caller
    answered, so the happy path adds nothing and cannot perturb a legal
    sequence). Each answer is inserted IMMEDIATELY AFTER its caller — precisely,
    after the caller's contiguous run of real answers, so a multi-call message
    keeps its existing sibling results in order and the placeholder joins them
    adjacently. When ONE caller is missing SEVERAL answers (a parallel-tool turn
    short-circuited wholesale), they are emitted in the caller's ORIGINAL
    tool_call order — not sorted by id — so the answers line up with the
    questions the model asked (matching ``_answer_replan_tool_calls``).

    That adjacency is NOT cosmetic. On the reachable path the dangling caller is
    the last message in ``state``, but the verifier / recover-verifier append
    their synthetic baseline pair and a Human instruction AFTER it before they
    send (``_build_layer2_messages``), so appending the answer at the very END
    of the send list would strand it past a Human message and leave the caller
    immediately followed by another assistant — a shape a strict provider
    rejects just as it rejects the dangling caller itself. Inserting after the
    caller keeps each tool response adjacent to the ``tool_calls`` it answers,
    which is what the protocol requires, and is a no-op difference when the
    caller genuinely is last.
    """
    if not messages or not isinstance(messages, list):
        return messages

    caller_ids = _collect_caller_ids(messages)
    if not caller_ids:
        return messages
    dangling = caller_ids - _collect_answered_ids(messages)
    if not dangling:
        return messages

    # Bucket each dangling id under the index of the AIMessage that asks for it
    # (first-wins, the same position map ``_repair_precedence`` uses; every
    # dangling id has an entry because ``_caller_positions`` and
    # ``_collect_caller_ids`` scan the same "ai" messages via
    # ``_message_tool_call_ids``). Callers are visited in message order, and
    # WITHIN one caller the answers are emitted in the caller's ORIGINAL
    # tool_call order (see the walk below) — NOT sorted by id — so a multi-call
    # turn short-circuited wholesale lines its answers up with the questions the
    # model actually asked. That order is fixed by the message, so it is
    # deterministic without a sort, and it matches the in-repo precedent
    # ``execute_loop._answer_replan_tool_calls`` (which also preserves order).
    positions = _caller_positions(messages)
    dangling_by_caller: dict[int, set[str]] = {}
    for tc_id in dangling:
        caller_at = positions.get(tc_id)
        if caller_at is None:  # pragma: no cover - dangling ⊆ caller_ids
            continue
        dangling_by_caller.setdefault(caller_at, set()).add(tc_id)

    out: list = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        out.append(msg)
        i += 1
        caller_at = i - 1
        if caller_at in dangling_by_caller:
            # Emit the caller's own contiguous real answers first (a multi-call
            # message may have some already), then insert the missing ones right
            # after them — adjacent to the caller, never at the list's end.
            while i < n and _msg_type(messages[i]) == "tool":
                out.append(messages[i])
                i += 1
            # Answer the missing siblings in the order the model emitted them:
            # _message_tool_call_ids is order-stable and dedups, so this is
            # deterministic and emits each id at most once (a duplicated id
            # stays first-wins via dangling_by_caller).
            missing = dangling_by_caller[caller_at]
            for tc_id in _message_tool_call_ids(messages[caller_at]):
                if tc_id in missing:
                    out.append(answer_factory(tc_id))
    ordered = sorted(dangling)
    logger.warning(
        "answer_dangling_tool_calls inserted %d honest 'not executed' answer(s) "
        "for tool_call(s) that reached the send gate with no result (ids %s). "
        "A strict provider rejects an unanswered tool_call, so each is answered "
        "here — inserted right after its caller, NOT appended at the end (the "
        "verifier appends its baseline pair and a Human instruction after the "
        "dangling caller, so an end-append would land past them) — rather than "
        "shipped dangling. The usual source is the execute-loop short-circuit "
        "(budget / wall-clock / error routed to the verifier before "
        "phase2_tools ran); check why the loop exited with a pending batch.",
        len(ordered),
        ordered,
    )
    return out


#: Every required id has exactly one caller and one result, caller first.
PAIR_INTACT = "intact"
#: Presence and multiplicity are correct but a result precedes its caller.
#: Repaired on the way out by ``sanitize_tool_pairing``; see CONTRACT BOUNDARY
#: in the module docstring for why state can stay in this shape permanently.
PAIR_REORDERED = "reordered"
#: A required id is missing a half, or a half is duplicated. Real damage —
#: the injected evidence is gone or a tool_call would be answered twice.
PAIR_DAMAGED = "damaged"


def diagnose_synthetic_pairs(messages: list, required_ids) -> str:
    """Three-state audit of a synthetic pair set: why it is not intact.

    ``synthetic_pairs_intact`` answers the rebuild question, but a caller that
    only knows "not intact" cannot tell real damage from the benign,
    non-convergent reversal described in CONTRACT BOUNDARY — and logging both
    at WARNING turns a known artefact into permanent alarm noise. This splits
    them:

    * ``PAIR_DAMAGED`` — a half is missing or duplicated. Worth a WARNING:
      something upstream lost or copied a message.
    * ``PAIR_REORDERED`` — both halves present exactly once, but the result
      comes first. Worth INFO at most: the send-side gate moves it back, so
      the only cost is a rebuild and slightly worse prompt placement.
    * ``PAIR_INTACT`` — nothing to do.

    Damage is checked across ALL ids before order, so a state that is both
    missing a pair and reversing another reports ``PAIR_DAMAGED``.
    """
    if not required_ids:
        return PAIR_INTACT

    callers: dict[str, list[int]] = {tc_id: [] for tc_id in required_ids}
    results: dict[str, list[int]] = {tc_id: [] for tc_id in required_ids}
    for idx, msg in enumerate(messages or []):
        if not _is_message(msg):
            continue
        msg_type = _msg_type(msg)
        if msg_type == "ai":
            for tc_id in _message_tool_call_ids(msg):
                if tc_id in callers:
                    callers[tc_id].append(idx)
        elif msg_type == "tool":
            tc_id = _msg_attr(msg, "tool_call_id", None) or ""
            if tc_id in results:
                results[tc_id].append(idx)

    reordered = False
    for tc_id in required_ids:
        if len(callers[tc_id]) != 1 or len(results[tc_id]) != 1:
            return PAIR_DAMAGED
        if callers[tc_id][0] >= results[tc_id][0]:
            reordered = True
    return PAIR_REORDERED if reordered else PAIR_INTACT


def synthetic_pairs_intact(messages: list, required_ids) -> bool:
    """True when EVERY id in ``required_ids`` has exactly one well-formed pair.

    Well-formed = exactly one AI caller bearing the id, exactly one
    ToolMessage answering it, and the caller PRECEDES the result.

    This is the dedup gate for SYNTHETIC (agent-fabricated) tool pairs —
    the verifier's baseline injection and the recover loop's counterpart.
    Probing only "is a ToolMessage with this id already present?" (the shape
    both call sites used before) is blind to four damaged states, and each
    costs something different:

    * caller missing, result surviving → the result ships orphaned and the
      send-side gate above must drop it, so the injected evidence is
      silently LOST (and a strict provider 400s wherever that gate is
      bypassed);
    * result missing, caller surviving → a tool_call ships with no answer,
      which OpenAI-compatible APIs reject as well;
    * duplicated → one tool_call answered twice. ``sanitize_tool_pairing``
      CANNOT catch this one: both copies have a caller, so both look legal;
    * reversed → the result does not respond to a PRECEDING call. This one is
      no longer fatal on the send path (``_repair_precedence`` moves it), but
      it still costs a rebuild and puts the evidence wherever state left it
      instead of right before the question — so the gate keeps rebuilding.

    Any of the four means "not intact", and the caller rebuilds the full set.
    Use ``diagnose_synthetic_pairs`` when the log level should differ between
    the last case and the first three.
    """
    return diagnose_synthetic_pairs(messages, required_ids) == PAIR_INTACT


def log_pair_rebuild(diagnosis: str, phase: str, ids, rebuilt: bool = True) -> None:
    """Log a synthetic-pair rebuild at the level its CAUSE deserves.

    Shared by the two gates (verify / recover_verify) so the level mapping
    lives next to the vocabulary it depends on and cannot drift between them.

    ``rebuilt=False`` covers the case where the builder returned nothing to
    inject BUT the sequence still holds a fragment of the pair set — real
    damage this turn cannot repair. No rebuild happened, and a log line
    claiming one would send whoever reads it looking in the wrong place. A
    turn holding no fragment at all is not damage (nothing was ever injected,
    so nothing was lost) and logs elsewhere — ``log_pair_absent`` when the
    builder was empty, ``log_pair_first_injection`` when it rebuilt — see
    the discriminator in ``apply_synthetic_pair_gate``.

    The DAMAGED branch also qualifies its legality claim by ``rebuilt``, for
    the same reason: only a rebuild makes the pair set whole in both
    directions. Downstream, ``sanitize_tool_pairing`` clears an orphan result
    and ``answer_dangling_tool_calls`` answers an unanswered caller, but
    NEITHER clears a DUPLICATED answer — so an unrepaired duplicate is the one
    fragment that still ships protocol-illegal.
    """
    outcome = "rebuilt the pair set" if rebuilt else "NO rebuild (builder returned nothing)"
    if diagnosis == PAIR_DAMAGED:
        # The legality claim is conditional because only the rebuilt branch
        # earns the unqualified form. A rebuild makes the pair set whole, so
        # the sequence reads legal in both directions. Left unrepaired, an
        # orphan RESULT is cleared downstream by ``sanitize_tool_pairing`` and
        # an unanswered CALLER is answered by ``answer_dangling_tool_calls`` —
        # but a DUPLICATED answer is cleared by NEITHER (both copies have a
        # caller, so both look legal). Printing "legal either way" on this
        # branch would promise a repair nobody performs, and would wave the
        # reader past that one fragment which can still reach a strict provider.
        legality = (
            "the outgoing sequence is legal either way"
            if rebuilt
            else "the fragments ship unrepaired; downstream clears an orphan "
                 "result and answers an unanswered caller, but not a "
                 "duplicated one"
        )
        logger.warning(
            "Synthetic tool pair(s) DAMAGED in the %s gate (ids %s): a half is "
            "missing or duplicated, so evidence was lost or a tool_call would "
            "be answered twice. %s; %s. "
            "Look upstream — memory compaction or a synthetic-message builder.",
            phase, sorted(ids), outcome, legality,
        )
    elif diagnosis == PAIR_REORDERED:
        logger.info(
            "Synthetic tool pair(s) REVERSED in the %s gate (ids %s): result "
            "before caller. %s. Benign, and expected to repeat on every turn "
            "without converging — ``add_messages`` pins the surviving result to "
            "its old index while the rebuilt caller is appended, so state never "
            "re-orders (see CONTRACT BOUNDARY in this module). The outgoing "
            "sequence is legal either way.",
            phase, sorted(ids), outcome,
        )


def log_pair_absent(phase: str, ids) -> None:
    """Log a turn that injected no synthetic pair because there was nothing to inject.

    INFO, and deliberately NOT the DAMAGED warning ``log_pair_rebuild`` emits:
    the sequence holds no fragment of the pair set either, so nothing was lost
    and nothing shipped unpaired — the builder legitimately returned nothing.

    The measured shape that lands here is a predicate disagreement, not a
    malfunction. ``_is_observation_success`` counts an observation with
    ``exit_code == 0`` and EMPTY stdout as a success (it rejects only a
    non-zero exit or a kubectl error marker inside the text), while both
    baseline builders skip an observation with no stdout — so a baseline whose
    every successful observation was output-less passes the node's
    ``success_count > 0`` precondition and then produces no ``obs_lines``, and
    the builder returns ``[]``. Neither predicate is wrong for its own purpose
    and the shipped sequence is legal either way.

    Because it is benign it must not read as an alarm: this turn repeats on
    EVERY iteration without converging (nothing is injected, so the next turn
    diagnoses identically), and a WARNING here would both cry wolf and point
    the reader at memory compaction. If baseline evidence WAS expected on such
    a turn, the place to look is ``baseline_capture``'s observations.
    """
    logger.info(
        "No synthetic tool pair injected in the %s gate this turn (ids %s): the "
        "builder returned nothing and the sequence holds no fragment of the "
        "pair set, so there was no evidence to inject and nothing was lost. "
        "Expected when no baseline observation carries usable stdout. If "
        "baseline evidence WAS expected here, check baseline_capture's "
        "observations rather than memory compaction.",
        phase,
        sorted(ids),
    )


def log_pair_first_injection(phase: str, ids) -> None:
    """Log a rebuild that found NOTHING to clear — the pair's first injection.

    INFO, and deliberately NOT the DAMAGED warning ``log_pair_rebuild`` emits:
    the sequence held no fragment of the pair set, so this is not a repair
    after damage but the pair's FIRST appearance this cycle. That is the
    normal first-turn path — cycle start, or the first turn after a replan
    seam removed the previous cycle's pair (change
    ``stale-baseline-pair-seam-cleanup``) — and a WARNING here would fire on
    every healthy task's first verify turn while pointing the reader at
    memory compaction, training them to ignore the very vocabulary that
    flags real damage.
    """
    logger.info(
        "Synthetic tool pair(s) injected for the first time in the %s gate "
        "(ids %s): the sequence held no fragment of the pair set, so this "
        "was the cycle's first injection, not a repair after damage.",
        phase,
        sorted(ids),
    )


def drop_messages_with_tool_call_ids(messages: list, ids) -> list:
    """Remove every message bearing a tool_call id in ``ids``.

    The other half of the rebuild: clearing the damaged remnants BEFORE
    appending a fresh pair set. Skipping this leaves the survivor of a
    broken pair in the sequence next to its replacement, answering one
    tool_call twice — a duplicate ``sanitize_tool_pairing`` cannot detect,
    because both copies have a caller.

    An AI message is dropped when ANY of its tool_call ids is in ``ids``.
    Synthetic callers are single-call by construction; a message mixing
    synthetic and real tool_calls would take its real results down with it,
    so builders MUST NOT mix the two in one message.

    Returns the ORIGINAL list object when nothing matched (zero-copy).
    """
    if not messages or not isinstance(messages, list) or not ids:
        return messages

    id_set = set(ids)
    kept: list = []
    dropped = 0
    for msg in messages:
        if _is_message(msg):
            msg_type = _msg_type(msg)
            if msg_type == "tool":
                if (_msg_attr(msg, "tool_call_id", None) or "") in id_set:
                    dropped += 1
                    continue
            elif msg_type == "ai":
                if any(tc_id in id_set for tc_id in _message_tool_call_ids(msg)):
                    dropped += 1
                    continue
        kept.append(msg)

    if not dropped:
        return messages

    # INFO, not WARNING. Dropping the stale fragments IS this function doing
    # its job, and once a reversed pair is pinned in state the rebuild — hence
    # this drop — repeats on EVERY turn without anything being wrong (see
    # CONTRACT BOUNDARY). Severity belongs to the caller: it is the only place
    # that knows why the rebuild happened, and it logs WARNING for
    # PAIR_DAMAGED and INFO for PAIR_REORDERED.
    logger.info(
        "Dropped %d stale synthetic message(s) (tool_call ids %s) before "
        "rebuilding the complete pair set.",
        dropped,
        sorted(id_set),
    )
    return kept


def _count_pair_fragments(messages: list, required_ids) -> int:
    """How many messages carry ANY id in ``required_ids`` — caller half or result half.

    The gate's discriminator between "damage the builder could not repair" and
    "the builder legitimately had nothing to inject": zero fragments means the
    pair set was never injected, so a builder returning ``[]`` lost nothing;
    one or more means the sequence holds a half-present or duplicated set that
    the builder was supposed to replace and could not.

    Reads the same three id homes as every other scan in this module (via
    ``_message_tool_call_ids``), so it can never disagree with
    ``diagnose_synthetic_pairs`` about what counts as a caller — a divergence
    there would make the discriminator disagree with the diagnosis it is
    explaining.
    """
    id_set = set(required_ids)
    count = 0
    for msg in messages or []:
        if not _is_message(msg):
            continue
        msg_type = _msg_type(msg)
        if msg_type == "tool":
            if (_msg_attr(msg, "tool_call_id", None) or "") in id_set:
                count += 1
        elif msg_type == "ai":
            if any(tc_id in id_set for tc_id in _message_tool_call_ids(msg)):
                count += 1
    return count


def apply_synthetic_pair_gate(
    messages: list,
    required_ids,
    build_fresh: Callable[[], list],
    *,
    phase: str,
) -> list:
    """The synthetic-pair dedup gate, in one place: diagnose → drop → rebuild.

    Shared by the verify and recover_verify gates. They used to be two inline
    copies buried in 600-line async functions that call the LLM, which is why
    neither was reachable by a unit test and the recover side had no coverage
    of this logic at all — the copies could also drift apart silently. Both
    problems are structural, so the fix is structural: the gate lives here,
    next to the three-state vocabulary and the log-level mapping it depends on,
    and the nodes just call it.

    ``build_fresh`` is a zero-arg CALLABLE, not a prebuilt list: the intact
    path is the common case and must not pay for the builder, which on the
    verify side formats the entire baseline evidence blob.

    A builder returning ``[]`` is a legitimate outcome, not a failure — every
    baseline builder returns it when no observation carries usable stdout — so
    that branch splits on whether the sequence holds any fragment of the pair
    set (``_count_pair_fragments``): a fragment means real damage this turn
    cannot repair (WARNING), no fragment means nothing was ever injected and
    nothing was lost (INFO). Collapsing the two into one WARNING made a benign
    turn indistinguishable from a lost-evidence turn, and because the benign
    one never converges it repeated on every iteration.

    The REBUILT branch discriminates on the same count: zero fragments means
    the pair is being injected for the first time this cycle (cycle start, or
    the first turn after a replan seam removed the previous cycle's pair),
    which is the normal path and logs INFO via ``log_pair_first_injection``;
    fragments present means the rebuild is a repair after damage and keeps
    the cause-deserving level of ``log_pair_rebuild``. The count is taken
    BEFORE the builder call — a pure scan — so the intact path above still
    pays nothing for the builder.

    Returns the list to ship. On the intact path that is ``messages`` itself,
    so the caller keeps appending to the object it already holds; on the
    rebuild path it is a NEW list, because clearing stale fragments must not
    touch the caller's object. Callers pass a copy of ``state["messages"]``,
    never the state list itself — this gate repairs the outgoing sequence and
    deliberately does not repair state (see CONTRACT BOUNDARY above).

    Whatever it returns is a sequence the send-side gate still has to clear:
    this fixes the SYNTHETIC pair set, not real tool traffic.
    """
    diagnosis = diagnose_synthetic_pairs(messages, required_ids)
    if diagnosis == PAIR_INTACT:
        return messages

    # Count fragments BEFORE the builder call: the scan is pure and free, and
    # the count discriminates the rebuilt branch too (first injection vs
    # repair after damage) — see log_pair_first_injection.
    fragments = _count_pair_fragments(messages, required_ids)

    fresh = build_fresh() or []
    if fresh:
        messages = [
            *drop_messages_with_tool_call_ids(messages, required_ids),
            *fresh,
        ]
        if fragments:
            log_pair_rebuild(diagnosis, phase, required_ids, rebuilt=True)
        else:
            log_pair_first_injection(phase, required_ids)
        return messages

    # Nothing to inject. TWO situations share this branch and only one of them
    # is damage, so tell them apart by whether the sequence holds any fragment
    # of the pair set at all:
    #   * fragment present — real damage this turn cannot repair, so the
    #     WARNING stands; the send-side gate still keeps the outgoing sequence
    #     legal by dropping the orphan.
    #   * no fragment — the pair set was never injected, so nothing was lost
    #     and the builder legitimately had no evidence. Logging DAMAGED here
    #     is a false alarm, and a costly one: ``rebuilt=False`` changes
    #     nothing, so the next turn diagnoses identically and the WARNING
    #     repeats on EVERY iteration without ever converging, all while
    #     pointing the reader at memory compaction. See log_pair_absent for
    #     the measured predicate disagreement that reaches it.
    if _count_pair_fragments(messages, required_ids):
        log_pair_rebuild(diagnosis, phase, required_ids, rebuilt=False)
    else:
        log_pair_absent(phase, required_ids)
    return messages
