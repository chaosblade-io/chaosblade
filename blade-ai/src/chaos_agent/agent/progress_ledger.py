"""Progress ledger — a model-maintained, three-layer working note.

The executor keeps a compact structured note AS IT WORKS (via the
``update_progress`` tool), instead of the harness trying to reconstruct what
happened after the fact. That single note serves four consumers:

  * the intent graph, which is context-isolated from execution and otherwise
    goes blind the moment a turn is interrupted (its raison d'être);
  * the executor itself, which re-reads the note each round to stay anchored to
    the original goal rather than re-deriving it (industry calls this
    structured note-taking / scratchpad; see Anthropic context engineering and
    Claude Code's TodoWrite);
  * interruption handling, because a note that is always current in state needs
    no end-of-run summarization step to survive an abort;
  * downstream analysis / audit.

Three layers, each with a different mutability contract:

  * ``anchor``   — frozen at handoff from the FaultSpec. The tool CANNOT rewrite
    it. This is the drift-correction reference: successive edits drift, an
    immutable anchor does not (the failure the ``approved_target`` freeze
    already guards against elsewhere in this codebase).
  * ``state``    — "what is true now". Shallow-overwritten on each update, so it
    stays bounded.
  * ``log``      — "how we got here". Append-only milestones, capped. Each entry
    carries a ``status`` (observed / verified / assumed) because a finding made
    mid-ReAct is usually unverified, and an unverified finding must never be
    mirrored to intent as established fact.

Everything here is pure and side-effect free so the merge semantics can be unit
tested in isolation.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

#: Ledger layer keys.
ANCHOR = "anchor"
STATE = "state"
LOG = "log"

#: Append-only log is bounded: milestones are few, and an unbounded log would
#: reintroduce the context bloat the ledger exists to avoid. Over the cap,
#: :func:`_truncate_log` keeps confirmed milestones plus the most recent rest.
LOG_CAP = 30

#: The state layer is model-written and re-injected into the system prompt EVERY
#: round, so it needs its own ceilings — a runaway ``established_facts`` list
#: would burn context on every turn and reintroduce exactly the pollution the
#: ledger was introduced to avoid. It also sits outside ``messages``, so
#: compaction cannot rescue it.
FACTS_CAP = 15
#: Per-value character ceiling for a single fact / step / log event.
VALUE_CHAR_CAP = 200
#: Hard ceiling on the RENDERED ledger, which is re-injected into the system
#: prompt every round. Sized so even a worst-case CJK ledger (~2 chars/token)
#: stays inside the design budget of <1.5k tokens.
RENDER_CHAR_CAP = 2400


def _clip(text: Any) -> str:
    """Flatten and clip one caller-supplied string value.

    Newlines are collapsed first. Every ledger entry is a single logical line, and
    the rendered ledger goes into the system prompt — a value carrying ``\\n\\n##
    …`` would break out of the ledger's indentation and read as an independent
    prompt section, which is content the MODEL writes. Flattening keeps
    model-authored text inside its own bullet, and the same value is later
    mirrored to the dialogue and written to the task file.
    """
    s = " ".join(str(text).split())
    return s if len(s) <= VALUE_CHAR_CAP else s[:VALUE_CHAR_CAP] + "…"

#: Allowed verification statuses for a log entry. Anything else is coerced to
#: ``assumed`` — the most cautious reading — so a malformed status can never
#: silently upgrade an unverified finding to a fact.
_VALID_STATUS = ("observed", "verified", "assumed")
_DEFAULT_STATUS = "assumed"


def freeze_anchor(fault_spec: Mapping[str, Any] | None, goal: str = "") -> dict:
    """Build a fresh ledger with its anchor frozen from the FaultSpec.

    Called once at pipeline handoff. The returned ledger has an empty state and
    log; the executor fills those as it works.
    """
    anchor: dict[str, Any] = {}
    if goal:
        anchor["goal"] = goal
    if isinstance(fault_spec, Mapping) and fault_spec:
        anchor["fault_spec"] = dict(fault_spec)
    return {ANCHOR: anchor, STATE: {}, LOG: []}


def _normalize_log_entry(entry: Any) -> dict | None:
    """Coerce one caller-supplied log entry into ``{event, status}``.

    Returns ``None`` for entries with no event text (nothing to record).
    """
    if isinstance(entry, Mapping):
        event = str(entry.get("event") or "").strip()
        status = str(entry.get("status") or "").strip().lower()
    else:
        # A bare string is treated as an event with the cautious default status.
        event = str(entry or "").strip()
        status = ""
    if not event:
        return None
    if status not in _VALID_STATUS:
        status = _DEFAULT_STATUS
    return {"event": _clip(event), "status": status}


#: State-layer keys whose value is a LIST of lines. A model recording a single
#: item naturally passes a bare string instead of a one-element list; normalising
#: here (rather than tolerating both shapes at render time) means every consumer
#: — prompt, dialogue mirror, task file — sees one predictable shape.
_LIST_STATE_KEYS = ("established_facts",)


def _bounded_state_value(value: Any, *, as_list: bool = False) -> Any:
    """Bound one state-layer value so the re-injected ledger stays compact.

    Lists (``established_facts``) keep the most recent :data:`FACTS_CAP` entries
    and each entry is clipped; scalars are clipped. Applied at merge time rather
    than at render time so ``state`` itself never grows unbounded — it is
    persisted to the checkpoint and the task file too.

    ``as_list`` wraps a non-list value, so a single fact passed as a bare string
    is still recorded as one fact instead of being silently unrenderable.
    """
    if isinstance(value, str):
        clipped = _clip(value)
        return [clipped] if as_list else clipped
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        items = [_clip(v) for v in value]
        return items[-FACTS_CAP:] if len(items) > FACTS_CAP else items
    if as_list:
        return [] if value is None else [_clip(value)]
    return value


def _maybe_json(value: Any) -> Any:
    """Parse a JSON-encoded string back into the structure it represents.

    Models pass arrays and objects as JSON *strings* — a mis-formatting this
    codebase has already hit on ``request_replan``'s list fields. Without this,
    ``'[{"event": …}]'`` degrades into one garbage log line and
    ``'{"phase": …}'`` is dropped entirely, silently losing the progress the
    model meant to record. Anything that is not JSON is returned unchanged.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return value


def _coerce_log_entries(log_append: Any) -> list:
    """Normalize whatever the model passed for ``log_append`` into a list.

    Models do get the type wrong, and the naive reading of a mis-typed value is
    actively harmful here: iterating a bare string yields one log entry PER
    CHARACTER, and iterating a dict yields its KEY names as events — garbage that
    is then re-injected every round and mirrored to the dialogue. So a single
    string is treated as one entry, a single mapping as one entry, and anything
    non-iterable is dropped.
    """
    if log_append is None:
        return []
    log_append = _maybe_json(log_append)
    if isinstance(log_append, (str, bytes)):
        return [log_append]
    if isinstance(log_append, Mapping):
        return [log_append]
    if isinstance(log_append, Sequence):
        return list(log_append)
    return []


def _select_log(entries: list, budget: int) -> list:
    """Choose ``budget`` entries: the newest ones, plus confirmed milestones.

    Two failure modes to avoid, and they pull in opposite directions:

      * plain recency drops the early ``verified`` line that matters most
        ("injected uid=…, fault is live") under a flood of process notes;
      * plain milestone-priority drops what JUST happened, which is what the
        model needs to decide its next action.

    So a fixed share of the budget is reserved for the tail (whatever its
    status), and the rest goes to the most recent ``verified`` entries.
    Chronological order is preserved.
    """
    if budget <= 0 or len(entries) <= budget:
        return list(entries)
    tail_share = max(1, budget // 2)
    keep = {i: entries[i] for i in range(len(entries) - tail_share, len(entries))}
    for i, entry in reversed(list(enumerate(entries))):
        if len(keep) >= budget:
            break
        if i not in keep and isinstance(entry, Mapping) and entry.get("status") == "verified":
            keep[i] = entry
    # Any budget still unspent goes to the next-most-recent entries.
    for i in range(len(entries) - 1, -1, -1):
        if len(keep) >= budget:
            break
        keep.setdefault(i, entries[i])
    return [keep[i] for i in sorted(keep)]


def _truncate_log(entries: list) -> list:
    """Cap stored log length while protecting confirmed milestones.

    See :func:`_select_log` for the selection policy; storage keeps more than the
    prompt renders, so an interruption record and the task-file audit can show a
    fuller trail than a single round needs.
    """
    return _select_log(entries, LOG_CAP)


def merge_progress_ledger(
    current: Mapping[str, Any] | None,
    *,
    state_update: Mapping[str, Any] | None = None,
    log_append: Sequence[Any] | None = None,
) -> dict:
    """Apply one ``update_progress`` delta to the ledger, returning a new dict.

    Contract, per layer:

      * ``anchor`` is preserved verbatim from ``current`` and is NEVER taken
        from the delta — the tool cannot rewrite the goal it is measured against.
      * ``state`` is shallow-overwritten: keys in ``state_update`` replace those
        in the current state; untouched keys are kept.
      * ``log`` appends the (normalized, non-empty) ``log_append`` entries,
        collapses a consecutive duplicate, and caps the result via
        :func:`_truncate_log` (which protects confirmed milestones).

    Never mutates ``current``.
    """
    base = dict(current) if isinstance(current, Mapping) else {}

    anchor = dict(base.get(ANCHOR) or {})

    new_state = dict(base.get(STATE) or {})
    state_update = _maybe_json(state_update)
    if isinstance(state_update, Mapping):
        for key, value in state_update.items():
            name = str(key)
            new_state[name] = _bounded_state_value(
                _maybe_json(value), as_list=name in _LIST_STATE_KEYS,
            )

    new_log = list(base.get(LOG) or [])
    for entry in _coerce_log_entries(log_append):
        normalized = _normalize_log_entry(entry)
        if normalized is None:
            continue
        # Drop a consecutive duplicate. A retried tool call or a replayed
        # checkpoint re-submits the same milestone, and three identical "injected
        # uid=x" lines would both waste the round's context and read as three
        # separate injections. Only ADJACENT repeats are collapsed, so a genuine
        # recurrence later in the drill is still recorded.
        if new_log and new_log[-1] == normalized:
            continue
        new_log.append(normalized)
    if len(new_log) > LOG_CAP:
        new_log = _truncate_log(new_log)

    return {ANCHOR: anchor, STATE: new_state, LOG: new_log}


def render_ledger(
    ledger: Mapping[str, Any] | None, *, log_tail: int = 8, include_anchor: bool = True,
) -> str:
    """Render the ledger as compact text for prompt re-injection or mirroring.

    Renders the anchor verbatim (the drift reference), the current state, and up
    to ``log_tail`` milestones with their verification status — selected the same
    milestone-first way as the storage cap, not by plain recency. An
    empty/missing ledger renders as an empty string so callers can treat it as
    "nothing recorded yet".

    ``include_anchor=False`` omits the anchor block — used when composing a
    combined operation record whose headline already carries the goal, so it is
    not repeated.
    """
    if not isinstance(ledger, Mapping):
        return ""
    anchor = ledger.get(ANCHOR) or {}
    state = ledger.get(STATE) or {}
    log = ledger.get(LOG) or []
    if not (anchor or state or log):
        return ""

    lines: list[str] = []

    if include_anchor and anchor:
        goal = anchor.get("goal")
        spec = anchor.get("fault_spec") or {}
        lines.append("Goal (ANCHOR, immutable):")
        if goal:
            # Flattened for the same reason as every other rendered value: the
            # goal comes from free-form user input and must stay inside its bullet.
            lines.append(f"  {_clip(goal)}")
        if isinstance(spec, Mapping) and spec:
            scope = spec.get("scope", "")
            target = spec.get("blade_target", "")
            action = spec.get("blade_action", "")
            ns = spec.get("namespace", "")
            names = spec.get("names") or []
            desc = f"  {scope}/{target}/{action}"
            if ns or names:
                desc += f" @ {ns}/{', '.join(names) if names else ''}"
            lines.append(desc)

    if state:
        lines.append("Current state:")
        phase = state.get("phase")
        step = state.get("current_step")
        if phase:
            lines.append(f"  phase: {phase}")
        if step:
            lines.append(f"  current step: {step}")
        facts = state.get("established_facts") or []
        # Normalised to a list at merge time; a bare value here can only come
        # from an older persisted ledger, so render it rather than drop it.
        if isinstance(facts, (str, bytes)) or not isinstance(facts, Sequence):
            facts = [facts]
        for fact in facts:
            lines.append(f"  - established: {_clip(fact)}")

    if log:
        # Same selection as the storage cap: the newest entries (what the model
        # must react to now) plus confirmed milestones (what must never be lost).
        recent = _select_log(list(log), log_tail) if log_tail > 0 else list(log)
        lines.append("Progress log (how we got here):")
        for entry in recent:
            if isinstance(entry, Mapping):
                event = entry.get("event", "")
                status = entry.get("status", _DEFAULT_STATUS)
            else:
                event, status = str(entry), _DEFAULT_STATUS
            lines.append(f"  [{status}] {event}")

    text = "\n".join(lines)
    # Final backstop: whatever combination of anchor + facts + log arrives, the
    # rendered ledger is re-injected EVERY round, so it must never blow past a
    # fixed ceiling. The per-value caps above make this unreachable in practice.
    if len(text) > RENDER_CHAR_CAP:
        text = text[:RENDER_CHAR_CAP] + "\n…(ledger truncated)"
    return text


#: Instruction that turns the ledger from a passive record into an active
#: anti-drift anchor: the model is told to check it before acting and to keep it
#: current. This is the mechanism behind Claude Code's TodoWrite — the re-reading
#: and updating is what keeps the agent on the approved goal.
_LEDGER_DIRECTIVE = (
    "Check the progress ledger below before acting: your next action MUST serve "
    "its immutable ANCHOR (the approved goal); if you have drifted, correct "
    "course first. Whenever you establish a new fact or reach a milestone, call "
    "update_progress to keep the ledger current (a finding you have not actually "
    "verified must be marked observed/assumed, never verified)."
)

#: Variant used when the ledger carries no anchor yet — during planning the
#: FaultSpec is still converging, so nothing is frozen (see the planning-phase
#: helper), and during recovery the ledger starts fresh. Pointing the model at an
#: "ANCHOR" that is absent from the rendered ledger would just be confusing.
_LEDGER_DIRECTIVE_NO_ANCHOR = (
    "Read the progress ledger below before acting so you do not re-derive what "
    "is already established. Whenever you establish a new fact or reach a "
    "milestone, call update_progress to keep it current (a finding you have not "
    "actually verified must be marked observed/assumed, never verified)."
)


def build_ledger_prompt_section(ledger: Mapping[str, Any] | None) -> str:
    """Render the ledger as a system-prompt section, or ``""`` if empty.

    Combines the standing directive with the current ledger content. The
    anti-drift wording is used only when an anchor is actually present, so the
    model is never pointed at an ANCHOR the rendered ledger does not contain.
    Returned empty when there is nothing recorded yet.
    """
    body = render_ledger(ledger)
    if not body:
        return ""
    has_anchor = bool(isinstance(ledger, Mapping) and (ledger.get(ANCHOR) or {}))
    directive = _LEDGER_DIRECTIVE if has_anchor else _LEDGER_DIRECTIVE_NO_ANCHOR
    return f"{directive}\n\n{body}"

