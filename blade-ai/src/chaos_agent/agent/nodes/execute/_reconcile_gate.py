"""Create-reconcile gate: three-state scan for result-uncertain creates.

A create tool call whose transport timed out (or failed with a transient
no-UID error) leaves the executor not knowing whether the fault was
actually created — and a non-idempotent create blindly retried can
materialise a DUPLICATE on the same target. This module owns the STATE
MACHINE half of the gate (blade-create-reconcile-before-retry D6); the
interception half also lives here (wired from execute_loop's
tool-dispatch path):

- The tool layer marks result-uncertain returns with
  ``UNCERTAIN_OUTCOME_MARKER`` (neutral ground, ``tools/markers.py``) —
  the providers layer never touches agent state, so the marker rides the
  return TEXT, the single channel of truth between the two layers. WHICH
  tools arm the gate is declared provider-side
  (``reconcile_create_tool_names``), consumed here through the registry
  union — this module names no tool.
- execute_loop, at the TOP of every iteration (same slot as
  ``check_and_reset_wait_guard``), scans the most recent create
  ToolMessage and applies three-state logic:

  1. uncertain marker → reverse-locate the issuing AIMessage's tool_call
     args, build the four-dimension request fingerprint through the
     registry seam (the provider that owns the create tool decides which
     argument keys form its request identity — for the conflict-query
     consumers this is the same construction safety_check uses), and
     register/overwrite ``create_reconcile`` (blocked_count=0,
     gate_reconciled=false — a re-uncertain retry starts a NEW cycle).
  2. gate marker → keep the flag untouched. The fabricated interception
     feedback says "blocked", NOT "executed" — treating it as an
     execution would clear the flag and self-defeat the gate. The other
     fabricated never-executed answers (truncation neutraliser's "was
     NOT executed", screener rejections) keep the flag armed for the
     same reason.
  3. no marker → build F' the same way and clear ONLY on fingerprint
     match. A different target's successful create must not clear the
     original target's protection: that would leave one resolved create
     plus one still-unknown one with the gate disarmed.

The scan runs before the LLM call, the interception judges the response
after it — same iteration, strictly ordered, so the FIRST blind retry
after a timeout already meets a registered flag.

Carrier vocabulary discipline: every tool NAME, argument key, cluster
query and feedback wording lives provider-side
(``providers/chaosblade/reconcile.py``); this module reaches it only
through the registry seam
(:meth:`FaultProviderRegistry.build_reconcile_fingerprint` /
:meth:`reconcile_hold_feedback` / :meth:`reconcile_batch_held_feedback`
and the ``union_tool_names`` attribute unions) — the phase-11
carrier-import retirement applied to the gate wholesale.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.execute.react_helpers import extract_tool_call_fields
from chaos_agent.tools.markers import (
    GATE_RECONCILE_BLOCKED_MARKER,
    UNCERTAIN_OUTCOME_MARKER,
)
from chaos_agent.tools.request_identity import RequestFingerprint

logger = logging.getLogger(__name__)

# Sentinel returned by the scan when the flag needs NO state update.
# ``None`` is a meaningful result (clear the flag), so absence-of-change
# needs its own identity.
NO_CHANGE = object()


# ---------------------------------------------------------------------------
# Registry seam lookups (all carrier vocabulary stays provider-side)
# ---------------------------------------------------------------------------


def _create_tool_names() -> frozenset[str]:
    """Provider union of create tools whose uncertain outcomes arm the gate."""
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    return FaultProviderRegistry.union_tool_names("reconcile_create_tool_names")


def _reconcile_read_tool_names() -> frozenset[str]:
    """Provider union of read tools whose post-uncertain run reconciles."""
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    return FaultProviderRegistry.union_tool_names("reconcile_read_tool_names")


def _build_fingerprint(tool_name: str, args) -> RequestFingerprint | None:
    """Request identity for a create call, via the registry seam."""
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    return FaultProviderRegistry.build_reconcile_fingerprint(tool_name, args)


def fingerprint_to_state_dict(fp: RequestFingerprint) -> dict:
    """State-serialisable form (AgentState values must survive checkpoints)."""
    return {
        "namespace": fp.namespace,
        "labels": fp.labels,
        "target_names": fp.target_names,
        "scope_target_action": fp.scope_target_action,
    }


def fingerprint_from_state_dict(data) -> RequestFingerprint:
    """Rebuild a fingerprint from its state dict form (drift-tolerant)."""
    d = data if isinstance(data, dict) else {}
    return RequestFingerprint(
        namespace=_as_str(d.get("namespace")),
        labels=_as_str(d.get("labels")),
        target_names=_as_str(d.get("target_names")),
        scope_target_action=_as_str(d.get("scope_target_action")),
    )


def scan_create_reconcile(messages: list, current):
    """Three-state scan of the most recent gate-armed create ToolMessage.

    Parameters
    ----------
    messages : list
        The current message history (LangChain message objects).
    current : dict | None
        The registered ``create_reconcile`` flag, if any.

    Returns
    -------
    ``NO_CHANGE`` — leave the flag as-is (no armed create ever ran, gate
    feedback, mismatched fingerprint, or unrecoverable args).

    ``None`` — CLEAR the flag: a same-fingerprint create really executed
    (success / terminal failure / an allowed-through retry), closing the
    uncertain cycle.

    ``dict`` — REGISTER/OVERWRITE the flag for a fresh uncertain outcome
    (shape: fingerprint / uncertain_call_id / blocked_count /
    gate_reconciled, see AgentState.create_reconcile).
    """
    last_idx = _find_last_create_tool_message(messages)
    if last_idx is None:
        return NO_CHANGE
    tool_msg = messages[last_idx]
    content = _string_content(tool_msg)
    tool_call_id = getattr(tool_msg, "tool_call_id", "") or ""
    tool_name = getattr(tool_msg, "name", "") or ""

    if UNCERTAIN_OUTCOME_MARKER in content:
        args = _find_issuing_tool_call_args(messages, last_idx, tool_call_id)
        if args is None:
            # Cannot reconstruct the request identity (compaction severed
            # the issuing AIMessage, or the id is missing). Registering
            # without a fingerprint would make the gate match nothing;
            # clearing would drop live protection — stand still.
            logger.warning(
                "create_reconcile: uncertain create return without "
                "recoverable tool_call args (id=%r) — flag left unchanged",
                tool_call_id,
            )
            return NO_CHANGE
        fp = _build_fingerprint(tool_name, args)
        if fp is None:
            # No provider claims the create tool's identity — without a
            # fingerprint the gate can match nothing; stand still.
            logger.warning(
                "create_reconcile: no provider fingerprint for uncertain "
                "create (tool=%r, id=%r) — flag left unchanged",
                tool_name,
                tool_call_id,
            )
            return NO_CHANGE
        logger.info(
            "create_reconcile registered for uncertain outcome "
            "(fingerprint=%s, uncertain_call_id=%s)",
            fp.normalized(),
            tool_call_id,
        )
        return {
            "fingerprint": fingerprint_to_state_dict(fp),
            "uncertain_call_id": tool_call_id,
            "blocked_count": 0,
            "gate_reconciled": False,
        }

    if GATE_RECONCILE_BLOCKED_MARKER in content:
        # Fabricated interception feedback: the retry did NOT execute, so
        # this says nothing about the flag's lifecycle — keep it armed.
        return NO_CHANGE

    if _looks_fabricated(tool_msg, content):
        # Other never-executed answers: the truncation neutraliser and the
        # tool screener's rejections. A create that never reached the
        # cluster resolves nothing — keep the flag armed.
        return NO_CHANGE

    # No marker: a real execution (success / terminal failure / a retry
    # that was allowed through). Clear only on fingerprint match.
    if not current:
        return NO_CHANGE
    args = _find_issuing_tool_call_args(messages, last_idx, tool_call_id)
    if args is None:
        # Cannot prove the executed create matches the registered request
        # — keep the protection rather than clear on a guess.
        return NO_CHANGE
    executed_fp = _build_fingerprint(tool_name, args)
    if executed_fp is None:
        return NO_CHANGE
    if executed_fp.matches(
        fingerprint_from_state_dict((current or {}).get("fingerprint"))
    ):
        logger.info(
            "create_reconcile cleared: matching create executed "
            "(fingerprint=%s)",
            executed_fp.normalized(),
        )
        return None
    # A different target's execution — the registered unknown stays armed.
    return NO_CHANGE


# Content signature of handle_truncated_response's fabricated answers:
# the truncation neutraliser answers every parseable call with this
# phrase, and a real create return can never contain it (the provider's
# own returns never claim non-execution).
_TRUNCATED_ANSWER_SIGNATURE = "was NOT executed"


def _looks_fabricated(tool_msg, content: str) -> bool:
    """True for ToolMessages fabricated by OUR guards, never by ToolNode.

    The truncation neutraliser stamps its answers with the "was NOT
    executed" phrase; the tool screener's rejections carry
    ``status="error"`` (real create returns are plain tool returns — the
    provider catches its own exceptions, so ToolNode never marks them
    error). Both shapes mean the call did NOT reach the cluster, so the
    clear path must not mistake them for a real execution. A false
    positive here errs conservative: a fabricated-looking real return
    keeps the flag armed (one extra interception) instead of disarming
    the duplicate protection.
    """
    if _TRUNCATED_ANSWER_SIGNATURE in content:
        return True
    return getattr(tool_msg, "status", None) == "error"


def _find_last_create_tool_message(messages: list):
    """Index of the most recent gate-armed create ToolMessage, or None.

    "Gate-armed create" is the provider union of
    ``reconcile_create_tool_names`` — this module names no tool.
    """
    names = _create_tool_names()
    if not names:
        return None
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if isinstance(msg, ToolMessage) and (
            getattr(msg, "name", "") or ""
        ) in names:
            return idx
    return None


def _string_content(msg) -> str:
    """ToolMessage content as a string (non-string content ⇒ "")."""
    content = getattr(msg, "content", "")
    return content if isinstance(content, str) else ""


def _find_issuing_tool_call_args(messages: list, tool_msg_idx: int, tool_call_id: str):
    """Reverse-locate the tool_call args that produced a ToolMessage.

    The issuing AIMessage always sits BEFORE its ToolMessage in the
    stream, so the scan is bounded to ``messages[:tool_msg_idx]`` — a
    LATER AIMessage can never shadow the real issuer. Matching is by
    tool_call_id (globally unique per call); the dict/object dual shape
    of tool_call entries is handled by ``extract_tool_call_fields``.
    """
    if not tool_call_id:
        return None
    for msg in reversed(messages[:tool_msg_idx]):
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            tc_id = (
                tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            )
            if tc_id == tool_call_id:
                _, args = extract_tool_call_fields(tc)
                return args if isinstance(args, dict) else {}
    return None


def _as_str(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


# ---------------------------------------------------------------------------
# Interception half: judge the freshly-issued batch against the flag
# ---------------------------------------------------------------------------

# Interception cap per uncertain cycle: the 3rd same-fingerprint retry is
# released with a warning rather than held again — a give-up cleanup is a
# worse failure than one more create attempt, and the released execution
# itself closes the cycle via the scan's fingerprint-match clear path.
RECONCILE_BLOCK_LIMIT = 2


def whitelist_reconcile_seen(messages: list, uncertain_call_id: str) -> bool:
    """True when a reconciling read tool's ToolMessage ran AFTER the
    uncertain return.

    ``uncertain_call_id`` locates the uncertain create ToolMessage in
    the stream; only whitelist answers strictly after it count (earlier
    reads predate the unknown outcome and say nothing about it). When the
    locator cannot be found (compaction), scan the WHOLE history — the
    wide-open reading errs toward releasing an honest retry, never toward
    a deadlock.

    The whitelist is the provider union of ``reconcile_read_tool_names``
    (read tools that resolve a gate-armed create — a member may be
    another carrier's read tool: the declaring provider owns the
    judgement, this module names no tool).
    """
    read_names = _reconcile_read_tool_names()
    if not read_names:
        return False
    start = 0
    if uncertain_call_id:
        for idx, msg in enumerate(messages):
            if (
                isinstance(msg, ToolMessage)
                and getattr(msg, "tool_call_id", "") == uncertain_call_id
            ):
                start = idx + 1
                break
    for msg in messages[start:]:
        if isinstance(msg, ToolMessage) and (
            getattr(msg, "name", "") or ""
        ) in read_names:
            return True
    return False


# Degraded fallback when no provider composes hold feedback for a create
# it fingerprinted (a provider-implementation inconsistency): generic
# wording only — it names no carrier tool, so it cannot guide the
# reconciliation, but it keeps the interception honest (held, marked,
# retriable once the cap or a completed probe releases).
_GENERIC_HOLD_FALLBACK = (
    f"{GATE_RECONCILE_BLOCKED_MARKER} [create-reconcile] BLOCKED — this "
    "create was HELD, never executed.\n"
    "Reason: the previous create for this same request returned with an "
    "UNKNOWN outcome; a blind retry can create a DUPLICATE on the target.\n"
    "Fix (is_hard_floor=False — reconcile first, then retry; this is "
    "not a ban): query whether the request above is already in effect, "
    "then re-issue this same create — the gate releases it."
)

# Same-shape fallback for the batch's other held calls: carrier-neutral
# wording (the provider-owned notice names the carrier's reconciliation
# tools; without one, only the never-executed fact is assertable).
_GENERIC_BATCH_HELD_FALLBACK = (
    "Error: tool call `{name}` was NOT executed — a create in this same "
    "batch was held by the create-reconcile gate (result-uncertain retry "
    "protection), so the whole batch was held back. Nothing ran and no "
    "state changed. Reconcile first, then re-issue."
)


async def apply_reconcile_gate(
    response, messages: list, flag, tracker=None,
    kubeconfig: str = "", task_id: str = "",
):
    """Judge the response's create calls against the registered flag.

    Parameters
    ----------
    response : AIMessage
        The freshly-issued LLM response carrying this batch's tool_calls.
    messages : list
        Current message history (for the whitelist release condition).
    flag : dict | None
        The create_reconcile flag — the caller passes the MERGED view
        (this iteration's scan may have registered it in ``result``).
    tracker : optional
        Status tracker for the cap-exceeded warning.
    kubeconfig, task_id : str
        Cluster coordinates for the interception-time probe (consumed
        provider-side through the registry seam, only on the intercept
        path).

    Returns
    -------
    ``None`` — release the batch (no flag / no matching create / a
    release condition met / cap exceeded with a warning emitted here).

    ``(list[ToolMessage], dict)`` — INTERCEPT: fabricated answers for the
    WHOLE batch (gate marker on the matching create, batch-held notice
    on everything else — the whole batch is held so no partial execution
    leaves a misleading trace) plus the flag with ``blocked_count``
    incremented (and ``gate_reconciled`` set when the probe completed)
    for the caller to publish. The fabricated answers use
    ``status="error"``, matching the screener's rejection precedent.
    """
    if not flag:
        return None
    create_names = _create_tool_names()
    tool_calls = getattr(response, "tool_calls", None) or []
    create_entries = []
    for tc in tool_calls:
        name, args = extract_tool_call_fields(tc)
        if name in create_names:
            tc_id = (
                tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            )
            create_entries.append((name, tc_id, args))
    if not create_entries:
        return None
    registered_fp = fingerprint_from_state_dict((flag or {}).get("fingerprint"))
    matched_name = ""
    for name, _tc_id, a in create_entries:
        fp = _build_fingerprint(name, a)
        if fp is not None and fp.matches(registered_fp):
            matched_name = name
            break
    if not matched_name:
        # A different target's create — the gate protects the
        # result-uncertain REQUEST, not every create action.
        return None

    # Release condition 1: the gate's own probe already reconciled.
    if flag.get("gate_reconciled"):
        return None
    # Release condition 2: a whitelist read ran after the uncertain
    # return.
    if whitelist_reconcile_seen(
        messages, str((flag or {}).get("uncertain_call_id") or "")
    ):
        return None

    blocked_count = int((flag or {}).get("blocked_count") or 0)
    if blocked_count >= RECONCILE_BLOCK_LIMIT:
        warning = (
            "create-reconcile gate: block limit reached "
            f"({blocked_count}/{RECONCILE_BLOCK_LIMIT}) — releasing the "
            "same-fingerprint retry WITHOUT reconciliation (deadlock "
            "guard; the released execution itself closes the cycle)"
        )
        logger.warning(warning)
        if tracker is not None:
            try:
                tracker.update(warning[:200], {"gate": "create-reconcile"})
            except Exception:
                logger.debug("create-reconcile warning tracking failed",
                             exc_info=True)
        return None

    # Interception applies — the owning provider probes the cluster for
    # the registered request and composes the hold feedback (probe +
    # wording are carrier judgment material, reached through the registry
    # seam). The probe's outcome decides the feedback text and whether it
    # counts as reconciliation (gate_reconciled).
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    hold_outcome = await FaultProviderRegistry.reconcile_hold_feedback(
        matched_name, registered_fp, blocked_count + 1, RECONCILE_BLOCK_LIMIT,
        kubeconfig=kubeconfig, task_id=task_id,
    )
    if hold_outcome is None:
        # No provider composed the feedback (implementation
        # inconsistency with the fingerprint seam): degrade to the
        # generic hold wording — never silently release a blind retry.
        logger.error(
            "create_reconcile gate: no provider hold feedback for create "
            "tool %r — degrading to generic interception wording",
            matched_name,
        )
        content, gate_reconciled = _GENERIC_HOLD_FALLBACK, False
    else:
        content, gate_reconciled = hold_outcome
    new_flag = {
        **flag,
        "blocked_count": blocked_count + 1,
        "gate_reconciled": gate_reconciled,
    }
    answers = []
    seen_ids: set[str] = set()
    for tc in tool_calls:
        name, args = extract_tool_call_fields(tc)
        tc_id = (
            tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
        )
        if not tc_id or tc_id in seen_ids:
            continue
        seen_ids.add(tc_id)
        fp = _build_fingerprint(name, args)
        if (
            name == matched_name
            and fp is not None
            and fp.matches(registered_fp)
        ):
            msg_content = content
        else:
            notice = FaultProviderRegistry.reconcile_batch_held_feedback(
                matched_name, name,
            )
            msg_content = notice or _GENERIC_BATCH_HELD_FALLBACK.format(
                name=name or "unknown",
            )
        answers.append(ToolMessage(
            content=msg_content,
            name=name or None,
            tool_call_id=tc_id,
            status="error",
        ))
    return answers, new_flag
