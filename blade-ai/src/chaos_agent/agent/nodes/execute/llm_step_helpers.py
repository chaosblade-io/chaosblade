"""Shared helpers for LLM loop nodes (agent_loop, execute_loop, verifier, etc.).

Extracted to centralize stagnation-related logic: hint construction and
tool filtering.  Each node still owns its own control flow (prompt
building, convergence hints, bind_tools conditions).
"""

import re

from langchain_core.messages import HumanMessage

from chaos_agent.agent.nodes.execute.react_helpers import summarize_llm_response
from chaos_agent.config.settings import settings

# Marker embedded in a persisted corrective hint so its repeat count can be read
# back out of history. Kept in the text (not only the id) because the count is
# what the model must SEE; the id only controls whether the reducer overwrites.
_REPEAT_RE = re.compile(r"<!--hint-repeat:(\d+)-->")


def _hint_id(kind: str, key: str) -> str:
    """Stable id for a corrective hint of *kind* about *key*.

    ``add_messages`` replaces a message whose id already exists, so reusing this
    id keeps exactly ONE copy in history no matter how many turns re-trigger it.
    """
    return f"hint:{kind}:{key}" if key else f"hint:{kind}"


def count_prior_hints(history: list | None, kind: str, key: str) -> int:
    """How many times this corrective hint has already been recorded.

    Reads the counter out of the persisted MESSAGES. This is the fallback path
    only — see :func:`resolve_hint_count`. It cannot be the primary source
    because compaction removes the messages it summarises, and a hint is recorded
    at its first occurrence, i.e. early enough to be summarised away in any drill
    that outgrows ``reserve_tokens``.
    """
    if not history:
        return 0
    prefix = _hint_id(kind, key)
    best = 0
    for msg in history:
        msg_id = getattr(msg, "id", None) or ""
        if not (msg_id == prefix or msg_id.startswith(f"{prefix}#")):
            continue
        content = getattr(msg, "content", "")
        found = _REPEAT_RE.search(content if isinstance(content, str) else "")
        if found:
            best = max(best, int(found.group(1)))
        else:
            best = max(best, 1)
    return best


def hint_count_key(kind: str, key: str) -> str:
    """State-dict key for the repeat count of one corrective hint."""
    return f"{kind}:{key}" if key else kind


def resolve_hint_count(
    counts: dict | None, history: list | None, kind: str, key: str,
) -> int:
    """Prior occurrence count, preferring state over message archaeology.

    ``counts`` (``state["hint_repeat_counts"]``) is authoritative: compaction
    rewrites ``messages`` but leaves other state fields untouched, so this is the
    only carrier that survives a long drill. The message scan remains as a
    fallback for checkpoints written before the field existed — without it, a
    restored older run would restart counting at 1.
    """
    from_state = 0
    if counts:
        try:
            from_state = int(counts.get(hint_count_key(kind, key), 0) or 0)
        except (TypeError, ValueError):
            from_state = 0
    # Take the larger of the two: state is authoritative, but a mid-upgrade
    # history may hold a higher number than a freshly-introduced empty dict.
    return max(from_state, count_prior_hints(history, kind, key))


def persist_corrective_hint(
    injections: list,
    history: list | None,
    kind: str,
    key: str,
    text: str,
    *,
    escalate_after: int | None = None,
    counts: dict | None = None,
    counts_out: dict | None = None,
) -> HumanMessage:
    """Record a corrective hint in history and return the message to inject.

    A hint that only reaches the LLM through a node-local message copy is gone
    the moment the turn ends: the node returns just its response, so the next
    iteration re-derives the hint from scratch and the model never learns that it
    has been told before. task-e9ee12d6 is the measured case — the stagnation
    hint fired from turn 11 onward and the model still issued the same
    ``kubectl_read top`` call 31 more times, because each turn it was reading a
    first-time notice.

    Appending to *injections* (which the node folds into ``result["messages"]``)
    puts the notice in history, so every later turn sees the record of the
    mistake. Two accumulation modes:

    * default — a stable id, so ``add_messages`` OVERWRITES the previous copy.
      History holds one entry whose text carries the running count.
    * beyond ``escalate_after`` — a unique id per occurrence, so entries pile up.
      Reserved for the point where the overwrite mode has demonstrably failed to
      change behaviour: at that point the visible weight of many notices is the
      signal, and a weak model does not have to interpret a counter to feel it.

    ``counts`` is ``state["hint_repeat_counts"]`` and is where the number really
    lives; the updated value is written into ``counts_out`` for the node to fold
    into its state update. The messages are for the model to SEE the mistake and
    may legitimately be summarised away by compaction — the count must not be,
    which is why it is state and not message archaeology.

    The returned message is what the caller appends to its local list for the
    CURRENT turn — the tail position carries the attention weight, while the
    persisted copy stays where it was first recorded and serves as the record.
    """
    prior = resolve_hint_count(counts, history, kind, key)
    count = prior + 1
    if counts_out is not None:
        counts_out[hint_count_key(kind, key)] = count

    body = text.rstrip()
    if count > 1:
        body += (
            f"\n\n(This is reminder #{count} of this kind. The previous "
            f"{prior} did not change the outcome — repeating the same action "
            f"again will not either. Choose a different action, or conclude.)"
        )
    # The marker is written literally rather than derived from ``_REPEAT_RE``, so
    # a future change to the pattern cannot silently produce an unparseable stamp.
    stamped = f"{body}\n<!--hint-repeat:{count}-->"

    escalated = escalate_after is not None and count > escalate_after
    msg_id = f"{_hint_id(kind, key)}#{count}" if escalated else _hint_id(kind, key)
    injections.append(HumanMessage(content=stamped, id=msg_id))
    # The turn-local copy carries no id: it must not collide with the persisted
    # one under ``add_messages`` when a node folds both into the same update.
    return HumanMessage(content=stamped)


def persist_replaceable_hint(
    injections: list, kind: str, key: str, text: str,
) -> HumanMessage:
    """Persist a hint whose CONTENT is superseded each turn, keeping one copy.

    For notices that restate a changing fact rather than count occurrences — the
    iteration-budget tiers ("iteration 12 of 15", "FINAL ITERATION") are the
    case this exists for. They were turn-local, so the model saw the countdown
    once and the next turn had no idea how much budget was left.

    Appending them with a stable id makes ``add_messages`` REPLACE the previous
    copy, which is what makes persisting them safe: a plain append would leave
    "iteration 3 of 15" sitting in history for turn 12 to read and conclude it
    has plenty of budget. One entry, always the current number.

    Unlike :func:`persist_corrective_hint` this adds no running count — the text
    already carries the number that matters, and these notices are not a record
    of ignored warnings.
    """
    stamped = text.rstrip()
    injections.append(HumanMessage(content=stamped, id=_hint_id(kind, key)))
    # Turn-local copy carries no id, so the reducer cannot collapse the two when
    # a node folds both into the same update.
    return HumanMessage(content=stamped)


def filter_stagnant_tool(
    tools: list | None,
    stagnant_tool: str | None,
    *,
    preserve: set[str] | None = None,
) -> list:
    """Remove a stagnant tool from the tool list.

    Only removes tool-level stagnation (full tool name match).
    Subcommand-level stagnation (``":" in stagnant_tool``) does NOT
    remove the tool — the node's hint tells the LLM to use other
    subcommands instead.

    ``preserve`` keeps named tools even when they match, used by
    intent_clarification to keep ``submit_fault_intent`` available.
    """
    result = list(tools) if tools else []
    if not stagnant_tool or ":" in stagnant_tool:
        return result
    return [
        t for t in result
        if getattr(t, "name", "") != stagnant_tool
        or (preserve and getattr(t, "name", "") in preserve)
    ]


def build_stagnation_hint(
    stagnant_tool: str,
    *,
    colon_suffix: str = "",
    else_actions: list[str] | None = None,
) -> str:
    """Build an ACTION_STAGNATION hint for a stagnant tool.

    Parameters
    ----------
    stagnant_tool : str
        Tool name, possibly with ``:subcommand`` suffix.
    colon_suffix : str
        Extra text after "with OTHER subcommands" for subcommand-level
        stagnation, e.g. ``"(patch, delete, scale, etc.) to complete
        remaining injection steps"``.
    else_actions : list[str] | None
        Bullet-point alternatives for full tool-level stagnation.
        Falls back to a generic "Use a DIFFERENT tool" if omitted.

    The two branches close differently on purpose. Tool-level stagnation really
    does remove the tool (``filter_stagnant_tool``), so stating that is a fact
    the model cannot act against. Subcommand-level stagnation removes nothing —
    a flat prohibition there is one the model can disprove on its next turn,
    which teaches it to discount these warnings and invites it to argue instead
    of reconsider. That branch therefore states the observation and leaves a
    documented way to continue.
    """
    if ":" in stagnant_tool:
        base_tool = stagnant_tool.split(":")[0]
        suffix = f" {colon_suffix}" if colon_suffix else ""
        return (
            f"**ACTION_STAGNATION**: You have called `{stagnant_tool}` "
            f"multiple consecutive times with no progress. "
            f"You can still use `{base_tool}` "
            f"with OTHER subcommands{suffix}.\n"
            f"Before the next call, reason it through explicitly: what do you "
            f"already know from the results so far, what is still genuinely "
            f"unknown, and what would THIS call add that the last one did not? "
            f"If you cannot answer the third question, the call adds nothing.\n"
            f"If repeating `{stagnant_tool}` is genuinely required here, say why "
            f"and continue. Otherwise change approach — repeating it unchanged "
            f"will not add information."
        )
    if not else_actions:
        else_actions = ["Use a DIFFERENT tool to proceed."]
    actions_str = "\n".join(f"- {a}" for a in else_actions)
    return (
        f"**ACTION_STAGNATION**: You have called `{stagnant_tool}` "
        f"multiple consecutive times with no progress. "
        f"This tool has been temporarily removed. You MUST now either:\n"
        f"{actions_str}\n"
        f"Reason the choice through before acting: what do you already know from "
        f"the results so far, what is still genuinely unknown, and which of the "
        f"options above would actually resolve it? Being unable to observe "
        f"further is itself a valid conclusion.\n"
        f"Do NOT attempt to call `{stagnant_tool}` again."
    )


def post_invoke_debug(
    tracker,
    response,
    count: int,
    label: str,
) -> None:
    """Emit debug-level LLM response summary to the progress tracker."""
    if not settings.is_debug:
        return
    debug_info, tool_names = summarize_llm_response(response)
    tracker.update(
        f"{label} {count} LLM:\n{debug_info}",
        {"debug": True, "iteration": count, "tool_calls": tool_names},
    )
