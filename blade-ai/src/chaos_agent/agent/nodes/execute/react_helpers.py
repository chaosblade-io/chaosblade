"""Shared helper functions for ReAct loop nodes.

Extracted from agent_loop.py, execute_loop.py, verifier.py, and
recover_verifier.py to eliminate code duplication.  Every function
here is either:
  - a pure-function extraction (no external state dependencies beyond
    settings.is_debug), or
  - a parameterised version of near-identical logic where the only
    difference is a constant name.
"""

import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chaos_agent.config.settings import settings
from chaos_agent.errors import ErrorClass, classify_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier 1: Pure-function extractions (identical code across files)
# ---------------------------------------------------------------------------

def record_system_prompt(hook, state: dict, prompt_text: str, node_name: str = "") -> None:
    """Record a system prompt to the session store (dedup handles repeated prompts).

    Parameters
    ----------
    hook : PreReasoningHook or None
        The hook object that may carry a ``session_store``.
    state : dict
        AgentState dict — reads ``task_id`` from it.
    prompt_text : str
        The system prompt content to record.
    node_name : str
        Graph node that produced this prompt (stamped as ``_node`` in additional_kwargs).
    """
    _task_id_local = state.get("task_id", "")
    if hook and getattr(hook, "session_store", None) and _task_id_local:
        msg = SystemMessage(content=prompt_text)
        if node_name:
            msg.additional_kwargs["_node"] = node_name
        hook.session_store.append_messages(_task_id_local, [msg])


def record_ai_message(hook, state: dict, response, node_name: str = "") -> None:
    """Immediately save an AI message (including reasoning_content) to session.

    Parameters
    ----------
    hook : PreReasoningHook or None
    state : dict
        AgentState dict — reads ``task_id`` from it.
    response : AIMessage
        The LLM response to record.
    node_name : str
        Graph node that produced this response (stamped as ``_node`` in additional_kwargs).
    """
    _task_id_local = state.get("task_id", "")
    if hook and getattr(hook, "session_store", None) and _task_id_local:
        try:
            if node_name:
                kwargs = getattr(response, "additional_kwargs", None)
                if isinstance(kwargs, dict):
                    kwargs.setdefault("_node", node_name)
            hook.session_store.append_messages(_task_id_local, [response])
        except Exception:
            pass


def log_reasoning_content(response, node_name: str, iteration: int) -> None:
    """Diagnostic log for reasoning_content presence (debug mode only).

    Parameters
    ----------
    response : AIMessage
    node_name : str
        Prefix for the log message, e.g. "Agent loop" or "Execute loop".
    iteration : int
        Current iteration count.
    """
    if settings.is_debug:
        additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
        rc = additional_kwargs.get("reasoning_content", "")
        logger.debug(
            f"{node_name} {iteration}: "
            f"reasoning_content={'present(' + str(len(rc)) + ' chars)' if rc else 'ABSENT'}"
        )


def extract_tool_call_fields(tc) -> tuple[str, dict]:
    """Extract (name, args) from a tool call that may be a dict or object.

    Handles the dual-path pattern where LangChain tool calls can be either
    plain dicts (from older versions / deserialised state) or namedtuples /
    objects with attributes.

    Parameters
    ----------
    tc : dict | ToolCall
        A single tool call entry.

    Returns
    -------
    tuple[str, dict]
        (tool_name, tool_args) — name defaults to "" and args to {}.
    """
    if isinstance(tc, dict):
        return tc.get("name", ""), tc.get("args", {})
    return getattr(tc, "name", ""), getattr(tc, "args", {})


# ---------------------------------------------------------------------------
# Tier 2: Parameterised extractions (near-identical, differing only in a
#          constant name or boolean flag)
# ---------------------------------------------------------------------------

def emit_debug_tool_messages(
    tracker,
    state: dict,
    seed_existing: bool = False,
) -> None:
    """Emit ToolMessage results from previous iteration (debug mode only).

    In debug mode, this iterates over the messages in *state*, finds any
    ToolMessage whose ``id`` hasn't been emitted yet, and sends a
    tracker update so the TUI can display tool outputs in real time.

    An ``_emitted_tool_ids`` set is maintained on *tracker* to avoid
    re-emitting the same ToolMessage across iterations.

    Parameters
    ----------
    tracker : ProgressTracker
        The progress tracker attached to the current node.
    state : dict
        AgentState dict — reads ``messages`` from it.
    seed_existing : bool, default False
        If True, on the first call (when ``_emitted_tool_ids`` is empty),
        pre-populate the set with ALL existing ToolMessage IDs so that
        inject-phase / Layer1 results are suppressed from the node's
        observable output.  Used by verifier and recover_verifier.
        If False (agent_loop / execute_loop), all ToolMessages are
        emitted immediately.

        The seeding condition uses ``not emitted_ids`` rather than an
        iteration count because non-ChaosBlade Layer 2 iterations may
        start at count > 1 (after Layer 1 iterations have already
        incremented the counter).
    """
    if not settings.is_debug:
        return

    messages = state.get("messages", [])
    emitted_ids = getattr(tracker, "_emitted_tool_ids", set())

    # On first iteration, optionally seed emitted_ids with pre-existing
    # ToolMessage IDs to suppress results from earlier phases.
    if seed_existing and not emitted_ids:
        for msg in messages:
            if isinstance(msg, ToolMessage):
                msg_id = getattr(msg, "id", None)
                if msg_id:
                    emitted_ids.add(msg_id)

    for msg in messages:
        if isinstance(msg, ToolMessage):
            msg_id = getattr(msg, "id", None)
            if msg_id and msg_id not in emitted_ids:
                tool_name = getattr(msg, "name", "unknown")
                msg_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                preview = msg_content[:100] + "..." if len(msg_content) > 100 else msg_content
                tracker.update(
                    f"📋 {tool_name}: {preview}",
                    {"debug": True, "tool_result": True, "tool_name": tool_name, "stdout_preview": msg_content[:200]},
                )
                emitted_ids.add(msg_id)
    tracker._emitted_tool_ids = emitted_ids


# ---------------------------------------------------------------------------
# Tier 2b: Parameterised extractions (near-identical, differing only in a
#          constant name)
# ---------------------------------------------------------------------------

def extract_synthetic_messages(
    messages: list,
    synthetic_ids: frozenset,
) -> list:
    """Extract synthetic AIMessage+ToolMessage pairs for state persistence.

    On the first iteration (count==1 / is_first_layer2), these messages
    were constructed and injected into the local ``messages`` list but
    are not yet in AgentState.  Extracting them and prepending to
    ``result_update["messages"]`` ensures they survive across iterations.

    Parameters
    ----------
    messages : list[BaseMessage]
        The local messages list for this iteration.
    synthetic_ids : frozenset
        Set of tool_call_ids that mark synthetic (injected) tool calls.
        Different for verifier vs recover_verifier.

    Returns
    -------
    list[BaseMessage]
        AIMessage and ToolMessage entries whose tool_call_ids match
        ``synthetic_ids``.
    """
    result = []
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            tc_ids = [
                tc.get("id", "") for tc in msg.tool_calls
                if isinstance(tc, dict)
            ]
            if any(tid in synthetic_ids for tid in tc_ids):
                result.append(msg)
        elif isinstance(msg, ToolMessage):
            if getattr(msg, "tool_call_id", "") in synthetic_ids:
                result.append(msg)
    return result


def extract_persistent_hm(
    messages: list,
    state: dict,
    kwargs_key: str,
) -> list:
    """Extract the main context HumanMessage for state persistence.

    On the first iteration, this HumanMessage was JUST built and appended
    to the local ``messages`` list.  We extract it and prepend to
    ``result_update["messages"]`` so it enters AgentState via the
    add_messages reducer.  On subsequent iterations, the HumanMessage is
    already in AgentState.messages (persisted from iteration 1), so we
    skip extraction to avoid wasteful re-injection.

    Parameters
    ----------
    messages : list[BaseMessage]
        The local messages list for this iteration.
    state : dict
        AgentState dict — reads ``messages`` to check if the HM already
        exists in persisted state.
    kwargs_key : str
        The ``additional_kwargs`` key used to tag this HumanMessage.
        Different for verifier (``_verifier_main_context``) vs
        recover_verifier (``_recover_main_context``).

    Returns
    -------
    list[HumanMessage]
        The tagged HumanMessage, or [] if it already exists in state.
    """
    already_in_state = any(
        getattr(m, "additional_kwargs", {}).get(kwargs_key)
        for m in state.get("messages", [])
        if isinstance(m, HumanMessage)
    )
    if already_in_state:
        return []
    result = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            if getattr(msg, "additional_kwargs", {}).get(kwargs_key):
                result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Moved functions (originally in agent_loop.py)
# ---------------------------------------------------------------------------

def _fingerprint_tool_call(name: str, args: dict) -> str:
    """Generate a fingerprint for a tool call, excluding infrastructure params."""
    # Params to exclude from fingerprinting (connection/infrastructure params)
    _FINGERPRINT_EXCLUDE_PARAMS = {"kubeconfig", "context", "cluster"}
    core_args = {k: v for k, v in args.items()
                 if k not in _FINGERPRINT_EXCLUDE_PARAMS and v}
    sorted_args = sorted(core_args.items())
    args_str = ", ".join(f"{k}={v}" for k, v in sorted_args)
    return f"{name}({args_str})"


def _stagnation_key(name: str, args) -> str:
    """Key used to group calls for stagnation counting.

    For kubectl tools the subcommand is part of the key so that a normal
    read-write-read cycle (get → patch → get → describe) is not mistaken for
    stagnation. Only repeats of the SAME subcommand count.
    """
    if name in ("kubectl", "kubectl_read"):
        sub = args.get("subcommand", "") if isinstance(args, dict) else ""
        return f"{name}:{sub}" if sub else name
    return name


def _recent_window(messages: list) -> list:
    """Tail slice bounded by BOTH the AI-turn window and the message cap.

    The turn bound is the primary judgement. Counting by message COUNT alone
    is what made both detectors structurally blind: one turn is 1 AIMessage +
    N ToolMessages + possibly a system message, so repeated calls sit 5-8
    messages apart and a 10-message window could never hold ``threshold``
    occurrences. The message cap is kept purely as a safety bound against
    scanning an unbounded history.

    A turn is any AIMessage, including a text-only one. Those consume window
    budget and therefore lower sensitivity — deliberately: a text-only turn is
    the model reflecting or converging, not repeating an action, and erring
    towards silence is the right bias for a detector whose false positives push
    a correct model off a valid conclusion.
    """
    max_messages = settings.loop_detection_window
    bounded = (
        messages[-max_messages:]
        if max_messages > 0 and len(messages) > max_messages
        else list(messages)
    )

    max_turns = settings.loop_detection_turns
    if max_turns <= 0:
        return bounded

    turns = 0
    start = 0
    for idx in range(len(bounded) - 1, -1, -1):
        if isinstance(bounded[idx], AIMessage):
            turns += 1
            if turns > max_turns:
                start = idx + 1
                break
    return bounded[start:]


def _outputs_confirm_stall(
    ids_per_turn: list[list[str]], tool_id_to_output: dict[str, str],
) -> bool:
    """True only when >= 2 turns produced the SAME result set for this tool.

    Compares per TURN, not per call. A batched turn issues several distinct
    queries under one tool key, so their outputs naturally differ from each
    other — lumping them together would never match. What signals a stall is
    the same turn-level answer coming back again: turn N's result set equal to
    turn N-1's.

    Deliberately conservative: a false positive tells a model that is reasoning
    CORRECTLY that it is looping, pushing it to discard a valid conclusion.
    Historical drills show legitimate repeats (polling for a fault to take
    effect) outnumber genuine stalls, so silence is preferred whenever there is
    no positive evidence of a stall.
    """
    signatures: list[tuple[str, ...]] = []
    for ids in ids_per_turn:
        outputs = [
            str(tool_id_to_output[tc_id]).strip()[:500]
            for tc_id in ids
            if tc_id in tool_id_to_output
        ]
        if not outputs:
            continue
        signatures.append(tuple(sorted(outputs)))
    if len(signatures) < 2:
        return False
    return all(sig == signatures[0] for sig in signatures[1:])


def _compare_tool_outputs(
    fingerprint: str,
    fingerprint_to_ids: dict[str, list[str]],
    tool_id_to_output: dict[str, str],
) -> tuple[bool, bool]:
    """Compare ToolMessage outputs for repeated tool calls.

    Returns (all_identical: bool, have_outputs: bool).
    - all_identical=True: all outputs are the same → genuine stuck loop
    - all_identical=False: outputs differ → fault is progressing, suppress loop
    - have_outputs=False: fewer than 2 comparable outputs → cannot determine,
      caller must stay silent rather than guess
    """
    tc_ids = fingerprint_to_ids.get(fingerprint, [])
    outputs: list[str] = []
    for tc_id in tc_ids:
        if tc_id in tool_id_to_output:
            content = tool_id_to_output[tc_id]
            normalized = str(content).strip()[:500]
            outputs.append(normalized)

    # A single output proves nothing about progression — treating it as
    # "identical" used to let the caller fire on zero evidence.
    if len(outputs) < 2:
        return False, False

    first = outputs[0]
    return all(o == first for o in outputs[1:]), True


def suggest_verify_command(tool_name: str) -> str:
    """Suggest a tool-appropriate verification command."""
    if "blade" in tool_name:
        return (
            "Run `blade <subcommand> -h` to check supported flags "
            "(via kubectl exec if host blade is unavailable)"
        )
    if "kubectl" in tool_name:
        return "Run `kubectl <subcommand> --help` to check supported flags"
    return (
        f"Check the actual interface of `{tool_name}` — "
        "the error message itself is the best clue about what went wrong"
    )


# ---------------------------------------------------------------------------
# Phase-specific reflection hints (principle-level, zero tool/flag names)
# ---------------------------------------------------------------------------

_LOOP_HINTS: dict[str, str] = {
    "intent": (
        "REFLECT: Your discovery method doesn't match how the system actually works. "
        "The query syntax or approach itself may be invalid.\n\n"
        "NEXT:\n"
        "1. Simplify — reduce your query to its broadest possible form.\n"
        "2. Change — try a fundamentally different discovery approach.\n"
        "3. Escalate — present what you found to the user, let them guide you."
    ),
    "planning": (
        "REFLECT: What you're trying to verify may not exist in the expected form. "
        "\"Not found\" after a broad search IS a valid outcome.\n\n"
        "NEXT:\n"
        "1. Broaden — verify at a wider scope without assumptions.\n"
        "2. Accept — if the target genuinely doesn't exist, that's a valid result. "
        "Reject the plan with evidence rather than searching endlessly.\n"
        "3. Conclude — don't keep looking for what isn't there."
    ),
    "execute": (
        "REFLECT: Your execution method may be incompatible with the actual runtime. "
        "Tool interfaces can differ from documentation.\n\n"
        "NEXT:\n"
        "1. Verify interface — confirm what the tool actually supports before retrying.\n"
        "2. Simplify — reduce to the minimum viable parameters.\n"
        "3. Fallback — switch to an alternative execution path."
    ),
    "verify": (
        "REFLECT: The effect may not be observable through your current approach. "
        "It may need a different angle, wider scope, or more propagation time.\n\n"
        "NEXT:\n"
        "1. Change angle — observe from a fundamentally different perspective.\n"
        "2. Broaden — check at a higher scope or different system layer.\n"
        "3. Conclude — form your verdict from evidence already collected."
    ),
    "recover": (
        "REFLECT: The recovery target may no longer exist, or this recovery method "
        "doesn't apply to the current state.\n\n"
        "NEXT:\n"
        "1. Check state — verify whether the target is still recoverable.\n"
        "2. Alternative — try a different recovery path entirely.\n"
        "3. Conclude — report the actual state and form your verdict."
    ),
}

_STAGNATION_HINTS: dict[str, str] = _LOOP_HINTS  # Same reflection body for both detection types


def _build_loop_hint(fp: str, count: int, phase: str) -> str:
    """Build phase-specific loop detection hint."""
    detection = (
        f"**LOOP DETECTED**: `{fp}` repeated {count} times with identical results.\n\n"
    )
    body = _LOOP_HINTS.get(phase, _LOOP_HINTS["intent"])
    return detection + body


def _build_stagnation_hint(
    tool: str, streak: int, phase: str, *, outputs_unchanged: bool = True,
) -> str:
    """Build phase-specific stagnation hint.

    Phrased as a rebuttable observation, not an order. Detection can be wrong,
    and a hard ``Do NOT call X`` would strip a correct model of a legitimate
    tool — pushing it onto a worse path for a problem it did not have.

    The opening states the ACTUAL reason this fired, because the two trigger
    paths observe different facts. Claiming "results came back unchanged" on the
    frequency path would be a false statement whenever the model can plainly see
    its readings moving (2695m → 2702m → 2692m), and a hint the model can
    disprove at a glance costs more than silence: it teaches the model that
    these warnings are noise, and it invites it to defend a correct action
    instead of reconsidering the one thing that IS true — the call count.
    """
    if outputs_unchanged:
        observation = "and the results came back unchanged"
        closing_reason = "repeating it unchanged will not add information"
    else:
        observation = (
            "which is far more than a drill normally needs. The readings do "
            "differ between calls, but small movement in a live sample is the "
            "same observation, not new evidence"
        )
        closing_reason = (
            "another sample of a value you have already established will not "
            "change your conclusion"
        )
    detection = (
        f"**ACTION_STAGNATION**: `{tool}` called on {streak} consecutive turns "
        f"{observation}.\n\n"
    )
    body = _STAGNATION_HINTS.get(phase, _STAGNATION_HINTS["intent"])
    suffix = (
        f"\n\nIf repeating `{tool}` is genuinely required here (waiting for a "
        f"state change, polling for propagation), say why and continue. "
        f"Otherwise change approach — {closing_reason}."
    )
    return detection + body + suffix


def detect_repeated_tool_calls(messages: list, phase: str = "intent") -> str | None:
    """Scan recent messages for repeated identical tool calls.

    Returns a LOOP DETECTED hint if the same tool call fingerprint appears
    >= loop_detection_threshold times within the last ``loop_detection_turns``
    AI turns AND the tool outputs are identical (not progressing — e.g., CPU
    ramping up is legitimate monitoring).

    Two-pass design:
    1. Scan windowed messages building fingerprint counts + ToolMessage output map
    2. For each fingerprint exceeding threshold, compare outputs.
       If outputs differ → fault is progressing, suppress loop warning.
       If fewer than 2 comparable outputs → no evidence, stay silent.
       If outputs identical → genuine stuck loop, emit hint.
    """
    threshold = settings.loop_detection_threshold

    recent = _recent_window(messages)

    fingerprint_counts: dict[str, int] = {}
    fingerprint_to_ids: dict[str, list[str]] = {}
    tool_id_to_output: dict[str, str] = {}

    # Pass 1: single scan, build all three structures
    for msg in recent:
        if isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", None)
            if tc_id:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                tool_id_to_output[tc_id] = content
        elif isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                name, args = extract_tool_call_fields(tc)
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if not name:
                    continue
                fp = _fingerprint_tool_call(name, args)
                fingerprint_counts[fp] = fingerprint_counts.get(fp, 0) + 1
                if tc_id:
                    fingerprint_to_ids.setdefault(fp, []).append(tc_id)

    # Pass 2: for each fingerprint exceeding threshold, check output progression
    for fp, count in fingerprint_counts.items():
        if count < threshold:
            continue

        all_identical, have_outputs = _compare_tool_outputs(
            fp, fingerprint_to_ids, tool_id_to_output,
        )

        # Only warn on positive evidence of a stall. Outputs that differ mean
        # progress; missing outputs mean we simply cannot tell.
        if not have_outputs or not all_identical:
            continue

        return _build_loop_hint(fp, count, phase)

    return None


def detect_action_stagnation(messages: list, threshold: int | None = None, phase: str = "intent") -> tuple[str | None, str | None]:
    """Detect consecutive turns calling the same tool (regardless of args).

    Unlike detect_repeated_tool_calls (which requires identical fingerprints),
    this catches "parameter thrashing" where the LLM calls the same tool with
    slightly different arguments each time.

    Batch-aware: a turn may carry several tool calls, and each tool key in the
    batch is counted independently. Requiring exactly one call per turn made
    this detector blind in practice — real stalls emit 3-4 calls per turn, and
    the old ``len(tool_calls) != 1`` guard aborted the scan on the first batch.

    That guard was also the only thing keeping false positives down, since this
    detector never looked at tool output. Removing it therefore comes with an
    output check: a streak is only reported when the repeated calls produced
    identical results, matching detect_repeated_tool_calls.

    Returns:
        (hint_message, stagnant_tool_name) or (None, None) if no stagnation.
    """
    _threshold = threshold if threshold is not None else settings.stagnation_threshold
    if _threshold < 2:
        return None, None

    recent = _recent_window(messages)

    tool_id_to_output: dict[str, str] = {}
    for msg in recent:
        if isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", None)
            if tc_id:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                tool_id_to_output[tc_id] = content

    # Collect consecutive tool-calling turns, most recent first. A pure-text
    # turn (no tool calls) ends the streak just as it did before.
    turns: list[dict[str, list[str]]] = []
    for msg in reversed(recent):
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            break
        keys_in_turn: dict[str, list[str]] = {}
        for tc in tool_calls:
            name, args = extract_tool_call_fields(tc)
            if not name:
                continue
            tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            keys_in_turn.setdefault(_stagnation_key(name, args), []).append(tc_id)
        if not keys_in_turn:
            break
        turns.append(keys_in_turn)

    if not turns:
        return None, None

    # Only a tool present in the most recent turn can be stagnating; its streak
    # is the run of consecutive turns (from the tail) that all include it.
    best_key: str | None = None
    best_streak = 0
    best_ids_per_turn: list[list[str]] = []
    for key in turns[0]:
        streak = 0
        ids_per_turn: list[list[str]] = []
        for turn in turns:
            if key not in turn:
                break
            streak += 1
            ids_per_turn.append(turn[key])
        if streak > best_streak:
            best_key, best_streak, best_ids_per_turn = key, streak, ids_per_turn

    if not best_key or best_streak < _threshold:
        return None, None
    # Above the frequency ceiling, the streak itself is the evidence: calling one
    # tool this many turns in a row is abnormal regardless of what it returned.
    #
    # This restores the two detectors' independence. ``detect_repeated_tool_calls``
    # answers "identical call, identical result" and rightly stays silent when
    # results differ. This one is meant to cover its blind spot — frequency — but
    # gating it on the same output comparison collapsed both layers into one
    # failure mode: a live metric sample never repeats byte-for-byte, so
    # task-c7c75263 polled the same reading 26 times (CPU drifting 2662-2707m)
    # and neither detector spoke. Measured over 14 drills, per-tool streaks run
    # median 1 / p90 2 / p95 3, while the abnormal cases sit at 8 and 12 — and
    # the two 8s are legitimate exploration with genuinely different arguments
    # (node, pods, events, sts), so the ceiling is set above them.
    if best_streak < settings.stagnation_frequency_ceiling:
        if not _outputs_confirm_stall(best_ids_per_turn, tool_id_to_output):
            # Below the ceiling, changing output is the false-positive guard:
            # the model may be legitimately polling or exploring. Stay silent.
            return None, None
        outputs_unchanged = True
    else:
        # The streak alone triggered this, so the hint must not claim the outputs
        # matched — check, and say only what is true.
        outputs_unchanged = _outputs_confirm_stall(
            best_ids_per_turn, tool_id_to_output,
        )

    hint = _build_stagnation_hint(
        best_key, best_streak, phase, outputs_unchanged=outputs_unchanged,
    )
    return hint, best_key


def handle_truncated_response(response) -> tuple[object, list[ToolMessage]] | None:
    """Neutralise a token-truncated response. Returns (message, answers) or None.

    A ``finish_reason == "length"`` response was cut off mid-emission, so EVERY
    tool call it carries may have incomplete arguments. Streamed tool arguments
    are finalized with a best-effort JSON parse, which means a truncated call can
    produce args that parse and validate yet are silently missing fields — e.g. a
    ``kubectl patch`` whose JSON body lost its tail, or a ``blade create`` missing
    half its flags. None of them are safe to execute against a live cluster.

    The two kinds of truncated call need OPPOSITE treatment, measured against
    DashScope (3 runs each, assistant message carrying one tool call):

        args valid JSON   + answered   → accepted
        args valid JSON   + unanswered → accepted   (pairing is NOT enforced)
        args invalid JSON + answered   → **400 InternalError.Algo.InvalidParameter:
                                          "function.arguments" must be JSON**
        args invalid JSON + unanswered → accepted

    So a parseable call is answered with a synthetic error, while a call whose
    args are broken is STRIPPED from the message instead: answering it is what
    makes the provider parse the arguments and reject the whole request. Stripping
    also removes the only reason to answer it.

    Returns ``None`` when the response was not truncated or carried no tool calls,
    meaning the caller should proceed normally.
    """
    metadata = getattr(response, "response_metadata", None) or {}
    if metadata.get("finish_reason") != "length":
        return None

    valid_calls = list(getattr(response, "tool_calls", None) or [])
    broken_calls = list(getattr(response, "invalid_tool_calls", None) or [])
    if not valid_calls and not broken_calls:
        return None

    message = response
    if broken_calls and hasattr(response, "model_copy"):
        # Drop the unparseable calls so the outbound payload carries no malformed
        # ``function.arguments``. Copy rather than mutate: the original stays
        # intact for logs and the session store.
        message = response.model_copy(update={"invalid_tool_calls": []})

    answers: list[ToolMessage] = []
    seen: set[str] = set()
    for tc in valid_calls:
        name, _ = extract_tool_call_fields(tc)
        tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
        if not tc_id or tc_id in seen:
            continue
        seen.add(tc_id)
        answers.append(ToolMessage(
            content=(
                f"Error: tool call `{name or 'unknown'}` was NOT executed — the "
                f"response hit the output token limit, so its arguments may be "
                f"truncated and incomplete. Nothing ran and no state changed. "
                f"Re-issue the call with complete arguments, keeping the output "
                f"shorter (fewer calls per turn, less preamble)."
            ),
            tool_call_id=tc_id,
            name=name or None,
        ))
    return message, answers


def summarize_llm_response(response) -> tuple[str, list[str]]:
    """Extract a short human-readable summary from an LLM response.

    Returns (summary_text, tool_names) where:
    - summary_text: formatted multi-line string for display
    - tool_names: list of tool names called
    """
    tool_calls = getattr(response, "tool_calls", None) or []
    tool_names = []
    lines = []

    if tool_calls:
        for tc in tool_calls:
            # NOTE: This uses the reversed-priority pattern (getattr first, then dict)
            # deliberately — it's the original logic from agent_loop.py.
            name = getattr(tc, "name", "") or (tc.get("name", "") if isinstance(tc, dict) else "?")
            args = getattr(tc, "args", {}) or (tc.get("args", {}) if isinstance(tc, dict) else {})
            tool_names.append(name)
            arg_parts = []
            for k, v in args.items():
                sv = str(v)
                if sv and sv not in ("", "None"):
                    display = sv[:50] + "..." if len(sv) > 50 else sv
                    arg_parts.append(f"{k}={display}")
            args_str = ", ".join(arg_parts) if arg_parts else ""
            lines.append(f"  🔧 tool: {name}({args_str})")

    additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
    reasoning_content = additional_kwargs.get("reasoning_content", "")
    if reasoning_content and isinstance(reasoning_content, str):
        text = reasoning_content[:300] + ("..." if len(reasoning_content) > 300 else "")
        lines.append(f"  💭 thinking: {text}")

    content = getattr(response, "content", "")
    if content and isinstance(content, str):
        text = content[:200] + ("..." if len(content) > 200 else "")
        lines.append(f"  💬 response: {text}")

    summary = "\n".join(lines) if lines else "(empty response)"
    return summary, tool_names


# ---------------------------------------------------------------------------
# Tier 4: Tool error introspection (runtime feedback > static docs)
# ---------------------------------------------------------------------------

_NON_INTERFACE_ERRORS = frozenset({
    ErrorClass.INFRA_TRANSIENT,
    ErrorClass.INFRA_PERSISTENT,
    ErrorClass.AUTH_DENIED,
    ErrorClass.TARGET_GONE,
    ErrorClass.QUOTA_EXCEEDED,
})

_REJECTED_PARAM_PATTERNS = [
    re.compile(r"unknown flag:\s*(\S+)"),
    re.compile(r"unknown shorthand flag:\s*'(\S+)'"),
    re.compile(r"flag provided but not defined:\s*(\S+)"),
    re.compile(r"(?:invalid|illegal) option[:\s]+[-]*([\w-]+)"),
    re.compile(r"unrecognized arguments?:\s*(\S+)"),
    re.compile(
        r"(?:unsupported|unknown|invalid)\s+"
        r"(?:flag|option|parameter|argument)[:\s]+(\S+)"
    ),
]

_HINT_MARKER = "RUNTIME EVIDENCE"


def _should_trigger_introspection(error_class: ErrorClass) -> bool:
    """Denylist: trigger for ALL errors except known non-interface ones."""
    return error_class not in _NON_INTERFACE_ERRORS


def extract_rejected_params(error_text: str) -> list[str]:
    """Best-effort extraction of rejected parameters from an error message."""
    if not error_text:
        return []
    found: list[str] = []
    for pat in _REJECTED_PARAM_PATTERNS:
        for m in pat.finditer(error_text):
            val = m.group(1).strip("'\"").rstrip(".,;:!?)")
            if val and val not in found:
                found.append(val)
    return found


def _elide_middle(text: str, head: int = 200, tail: int = 220) -> str:
    """Keep both ends of an error, dropping the middle.

    A head-only cut assumes the reason comes first. Schema errors are the other
    way round: LangChain prefixes ``Error invoking tool 'x' with kwargs {...}``
    and the offending arguments are echoed back in full, so the first 200
    characters can be entirely echo while the reason — pydantic's ``Input should
    be a valid dictionary`` — sits at the end and was cut away.

    task-fc64c982: ``update_progress`` was rejected for JSON-stringified
    arguments, the hint carried only the echoed kwargs, and the executor
    resubmitted the identical payload and was rejected again. The verdict it
    needed was in the part that got dropped.
    """
    if len(text) <= head + tail:
        return text
    return f"{text[:head]} … [{len(text) - head - tail} chars elided] … {text[-tail:]}"


def _build_introspection_hint(
    tool_name: str, error_content: str, rejected_params: list[str],
) -> str:
    parts = [
        f"**{_HINT_MARKER}**: `{tool_name}` returned an error.",
        f"- tool observation: {_elide_middle(error_content)}",
        "- real-world outcome: unknown from this tool result alone",
        "",
        "Runtime behavior takes precedence over documentation for this environment.",
    ]
    if rejected_params:
        flags_str = ", ".join(f"`{f}`" for f in rejected_params)
        parts.append(
            f"- rejected interface elements: {flags_str}"
        )
    parts.extend([
        "- interface observation option: " + suggest_verify_command(tool_name),
        "- decision context: preserve approved constraints and choose the next safe, meaningful action from the available evidence",
    ])
    return "\n".join(parts)


def detect_tool_error_hint(messages: list) -> str | None:
    """Scan recent ToolMessages for errors that warrant introspection.

    Returns a hint string if a qualifying error is found and no
    duplicate hint already exists in messages. Returns None otherwise.
    """
    window = min(len(messages), 10)
    recent = messages[-window:]

    for msg in reversed(recent):
        if not isinstance(msg, ToolMessage):
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if not content.startswith("Error"):
            continue

        result = classify_error(content)
        if not _should_trigger_introspection(result.error_class):
            continue

        tool_name = getattr(msg, "name", "") or ""
        if any(
            isinstance(m, HumanMessage)
            and isinstance(m.content, str)
            and _HINT_MARKER in m.content
            and f"`{tool_name}`" in m.content
            for m in recent
        ):
            continue

        rejected = extract_rejected_params(content)
        return _build_introspection_hint(tool_name, content, rejected)

    return None


def detect_transient_retry_exhaustion(messages: list) -> str | None:
    """Escalate when one tool keeps failing with INFRA_TRANSIENT errors.

    Executes the short-retry budget promised by ``settings.max_transient_retry``
    (Patch B). Its design intent: an infra blip may heal seconds later, so a
    SHORT_RETRY error earns a bounded number of retries — but recurring
    transient failures are not a blip; they are the environment saying this
    path cannot work. Without enforcement the same transient error can be
    retried indefinitely (task-71fa78b6: the same 63061 kubewiz-timeout was
    retried six times, burning most of the task's wall clock).

    Layering note: this is the ERROR-CLASS frequency layer. It deliberately
    does NOT touch ``detect_repeated_tool_calls`` (whose contract is
    "identical call + identical result") or ``detect_action_stagnation``
    (whose contract is consecutive-turn frequency): both stay silent by design
    when results differ or other tools interleave, which is exactly the shape
    a retry storm has. Classification comes from ``errors.classify_error`` —
    this function matches no error text of its own.

    Semantics:
    * Per tool, transient errors are counted chronologically within the
      detection window; ``budget`` counts RETRIES after the first failure, so
      the hint fires on the (budget + 1)-th transient failure.
    * A SUCCESSFUL result (non-``Error`` ToolMessage) resets that tool's
      count — a healed blip earns a fresh budget.
    * ``budget <= 0`` disables the guard (same convention as the other
      budget settings).
    """
    try:
        budget = int(settings.max_transient_retry or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return None

    counts: dict[str, int] = {}
    last_pattern: dict[str, str] = {}
    for msg in _recent_window(messages):
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", "") or "tool"
        content = msg.content if isinstance(msg.content, str) else ""
        if not content.startswith("Error"):
            # A successful result proves the blip healed — fresh budget.
            counts.pop(name, None)
            continue
        result = classify_error(content)
        if result.error_class is not ErrorClass.INFRA_TRANSIENT:
            continue
        counts[name] = counts.get(name, 0) + 1
        last_pattern[name] = result.matched_pattern or "transient error"

    for name, n in counts.items():
        if n > budget:
            return (
                f"**TRANSIENT RETRY BUDGET EXHAUSTED**: `{name}` has failed "
                f"{n} times with transient infrastructure errors (latest "
                f"signature: \"{last_pattern[name]}\").\n\n"
                "REFLECT: A short retry budget exists because a blip may "
                "heal — but recurring failures of the same shape increasingly "
                "suggest the environment, not the call.\n\n"
                "NEXT:\n"
                "1. Reconsider — a different path (another method, carrier, "
                "or angle) may bypass whatever is failing here.\n"
                "2. Escalate — a structured replan can hand the recurring "
                "blocker to Phase 1 with fresh context.\n"
                "3. Conclude — if no alternative remains within the approved "
                "boundary, reporting this step as failed is a valid outcome.\n\n"
                f"If retrying `{name}` is genuinely warranted here (you have "
                "evidence the environment is healing), say why and continue. "
                "Otherwise change approach — more attempts of the same call "
                "will not add information."
            )
    return None
