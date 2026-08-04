"""Tool screener: gate ``execute_loop`` tool_calls against the approved target.

Slotted between ``execute_loop`` (the LLM node) and ``phase2_tools``
(the LangGraph ``ToolNode``). For every tool_call in the most recent
AIMessage:

  1. Classify the call into an ``EffectiveTarget`` via
     ``chaos_agent.agent.target_guard.infer_effective_target``.
  2. Compare against the snapshot in ``state.approved_target`` via
     ``target_drift_guard``.
  3. Aggregate verdicts and choose one of three routes:

     - ``pass``  — all calls allowed; ToolNode executes normally.
     - ``interrupt`` — at least one call drifted; pause graph via
                       interrupt() for human confirmation. Approve
                       corrects fault_spec + approved_target and passes;
                       reject retries (LLM gets one chance to
                       self-correct before hard termination).
     - ``retry`` — at least one call was BANNED/UNKNOWN; fabricate
                   ToolMessage rejections so the LLM sees the failure
                   and tries again next iteration. Route back to
                   ``execute_loop``.

Two operating modes governed by ``settings.target_guard_enforcing``:

  - **Enforcing** (default in production after grey rollout): the
    above logic runs as described. Rejections actually block tools.
  - **Log-only** (default before grey rollout finishes): the verdict
    is computed and logged at WARNING level for any non-ALLOW result,
    but the call is allowed to proceed to phase2_tools. Used to
    surface false-positives in production traffic before flipping
    enforcement on.

The screener emits a fabricated ToolMessage for EVERY tool_call in the
AIMessage when any one is rejected. LangChain's ToolNode would normally
do this matching; bypassing ToolNode means we have to satisfy the
"every tool_call needs a corresponding ToolMessage" invariant ourselves,
otherwise the next LLM iteration sees a malformed conversation.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.capabilities import explain_tool_refusal, tool_call_allowed
from chaos_agent.agent.execution_artifacts import is_vehicle_name
from chaos_agent.agent.nodes.execute.llm_step_helpers import hint_count_key
from chaos_agent.agent.nodes.execute.react_helpers import _stagnation_key
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.target_guard import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardDecision,
    GuardVerdict,
    approved_from_dict,
    freeze_approved_target_from_spec,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.carriers import (
    LIVE_DISCOVERY_RETRYABLE_REASONS,
    CarrierResolution,
    discover_unregistered_carrier,
    effective_target_from_registered_carrier,
    is_host_carrier_call,
    registered_carrier_is_current,
)
from chaos_agent.agent.target_guard.classifier import (
    SCOPE_ESCAPE,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
)
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.config.settings import settings
from chaos_agent.tools.guard_gateway import decision_to_feedback, get_guard_gateway

logger = logging.getLogger(__name__)


_FAILED_CREATE_UID_RE = re.compile(
    r'(?:UID:\s*|"uid"\s*:\s*")([a-fA-F0-9][a-fA-F0-9-]{7,})'
)


def _blade_uids_created_by_current_task(messages: list) -> set[str]:
    """Return experiment UIDs proven by this task's blade_create results."""
    from chaos_agent.utils.blade_uid import extract_blade_uid

    uids: set[str] = set()
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        if (getattr(message, "name", "") or "") != "blade_create":
            continue
        content = message.content if isinstance(message.content, str) else ""
        uid = extract_blade_uid(content)
        if uid:
            uids.add(uid)
        # Terminal create failures deliberately do not count as active UIDs in
        # extract_blade_uid, but their CRDs still need cleanup.
        uids.update(match.group(1) for match in _FAILED_CREATE_UID_RE.finditer(content))
    return uids


async def _discover_vehicle_pods(
    state: AgentState, candidates: list[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Live-discover which ``candidates`` are injection tool pods.

    Reuses the SAME label-selector discovery the baseline / conflict checks
    use (``discover_tool_pods_cluster_wide``): an all-namespace lookup of
    the ChaosBlade tooling labels, verified against the LIVE cluster. Pod
    identity is thus a cluster fact, not a naming convention — deployments
    that rename or relocate the tool DaemonSet are still recognised as long
    as they carry the tooling labels, and site-specific tool pods are
    covered by task-side registration (``kubectl_exec_pod_name``).

    Returns ``(positives, misses)``. A failed probe caches its candidates
    as misses: re-probing every screener iteration under an active network
    fault would ride the very API path the fault is severing; the fail-
    closed outcome (the call still reaches drift review) is safe.
    """
    from chaos_agent.agent.nodes.execute._injection_detection import (
        discover_tool_pods_cluster_wide,
    )

    kubeconfig = str(state.get("kubeconfig") or "")
    try:
        pods = await discover_tool_pods_cluster_wide(
            kubeconfig, str(state.get("task_id") or ""),
        )
    except Exception:
        logger.warning(
            "target_guard: vehicle live-discovery failed; candidates %s "
            "keep identity review (fail closed)", candidates,
        )
        return frozenset(), frozenset(candidates)
    discovered = {name for name, _ns in pods}
    positives = frozenset(n for n in candidates if n in discovered)
    misses = frozenset(n for n in candidates if n not in discovered)
    if positives:
        logger.info(
            "target_guard: live discovery confirmed injection vehicle(s) %s",
            sorted(positives),
        )
    return positives, misses


def _identity_matches_approved(
    effective: EffectiveTarget, approved: ApprovedTarget | None,
) -> bool:
    """Cheap structural match between an effective target and the approval.

    True means identity drift cannot fire for this call, so the vehicle
    block (probe + exemption) may be skipped entirely. That matters beyond
    efficiency: the discovery probe rides the in-band API path, which an
    injected network fault may already be severing — no probe may fire for
    an exec into the approved target itself. False negatives are harmless:
    at worst one extra fail-closed, cached probe.
    """
    if approved is None or not effective.names:
        return False
    if (effective.namespace or "default") != (approved.namespace or "default"):
        return False
    if approved.is_namespace_wide:
        return True
    known = (
        set(approved.names)
        | set(approved.resolved_names)
        | set(approved.owner_names)
    )
    return bool(known) and all(n in known for n in effective.names)


def _screen_blade_destroy(tool_args: Any, messages: list) -> tuple[EffectiveTarget, GuardDecision]:
    """Allow cleanup only for an experiment created by this graph task."""
    args = tool_args if isinstance(tool_args, dict) else {}
    uid = str(args.get("uid") or "").strip()
    effective = EffectiveTarget(
        scope="__blade_cleanup__",
        namespace="",
        confidence=ConfidenceLevel.HIGH,
        raw_command=f"blade_destroy uid={uid}",
    )
    if uid and uid in _blade_uids_created_by_current_task(messages):
        return effective, GuardDecision(
            verdict=GuardVerdict.ALLOW,
            reason="experiment UID was created by this task",
            effective=effective,
        )
    return effective, GuardDecision(
        verdict=GuardVerdict.REJECT_UNKNOWN,
        reason="blade_destroy UID was not produced by this task's blade_create",
        effective=effective,
        suggestion="Only clean the UID reported by the current failed blade_create call.",
    )


# Sentinel used by ``route_after_screener`` to dispatch to the right
# successor node. Cleared each time the screener runs so a stale
# value can't leak into a later iteration.
SCREENER_ROUTE_PASS = "pass"
SCREENER_ROUTE_REPLAN = "replan"
SCREENER_ROUTE_RETRY = "retry"


def _carrier_within_liveness_window(
    artifact: Any, from_registered: bool, current_task_id: str,
) -> bool:
    """Whether a registered carrier is fresh enough to skip the live re-probe.

    ``registered_carrier_is_current`` re-reads the pod via ``kubectl get pod``
    to reject stale/recreated carriers. Under an in-band network fault that
    probe rides the very API path the fault is severing, so it times out and
    turns "injection actually cut the link" into a false "carrier unavailable"
    rejection. When this task itself created the debug pod and confirmed it
    active within ``carrier_liveness_ttl_seconds``, we trust the in-memory
    registration and skip the probe.

    Only the *registered* path qualifies: live-discovered carriers carry no
    ``confirmed_live_epoch`` / ``task_id`` and must still be probed. ``ttl<=0``
    disables the window entirely (always probe = pre-optimization behaviour),
    and the explicit ``ttl > 0`` guard avoids a degenerate float-equality skip.
    """
    if not from_registered or not isinstance(artifact, dict):
        return False
    if artifact.get("status") != "active":
        return False
    ttl = int(getattr(settings, "carrier_liveness_ttl_seconds", 0) or 0)
    if ttl <= 0:
        return False
    if not current_task_id or artifact.get("task_id") != current_task_id:
        return False
    epoch = artifact.get("confirmed_live_epoch")
    if not isinstance(epoch, (int, float)):
        return False
    return (time.time() - float(epoch)) <= ttl


_HARD_BLOCK_MARK = "refused as stagnant"


def _blocked_on_previous_attempt(state: AgentState, tool_name: str) -> bool:
    """Did the immediately preceding attempt at this tool get hard-blocked?

    Used to make the block ALTERNATE rather than latch. A permanent block is the
    wrong shape: the subcommand is often legitimately needed again once the model
    changes angle, and refusing forever converts "stop repeating" into "you may
    never look at this again" — which the model cannot satisfy and which would
    make an otherwise recoverable drill unfinishable.

    Alternating breaks the streak (every other attempt fails) while keeping the
    call reachable. Derived from the rejection message the previous block already
    left in history, so no extra state field is needed and the two cannot drift
    apart: the evidence IS the previous decision.

    Scans backwards to the most recent result for this tool and asks only about
    THAT one — an older block separated by successful calls is not "the previous
    attempt" and must not grant a free pass now.
    """
    for msg in reversed(state.get("messages", []) or []):
        if getattr(msg, "type", "") != "tool":
            continue
        if (getattr(msg, "name", "") or "") != tool_name:
            continue
        content = getattr(msg, "content", "")
        return _HARD_BLOCK_MARK in (content if isinstance(content, str) else "")
    return False


def _hard_stagnation_block(
    state: AgentState, tool_name: str, tool_args,
) -> tuple[str, str]:
    """Refuse a call whose ``tool:subcommand`` has exhausted the soft warnings.

    Returns ``(reason, suggestion)``, both empty when the call may proceed.

    Keyed through ``_stagnation_key`` — the SAME function the detector uses — so
    the block covers exactly what was warned about. Deriving the key here
    independently is how a block ends up refusing a call nobody warned about, or
    letting through the one that was.

    The block ALTERNATES: an attempt right after a blocked one is let through.
    See :func:`_blocked_on_previous_attempt` for why latching is the wrong shape.

    Read-only on state: the count is written by the loop nodes when they issue
    the hint. This function only decides whether the count has passed the point
    where notices were shown not to work.
    """
    if not settings.target_guard_enforcing:
        # The guard's global switch governs refusals; in log-only mode a stuck
        # model is a diagnosis, not something to block.
        return "", ""
    counts = state.get("hint_repeat_counts") or {}
    if not counts:
        return "", ""
    key = _stagnation_key(tool_name, tool_args if isinstance(tool_args, dict) else {})
    issued = 0
    try:
        issued = int(counts.get(hint_count_key("stagnation", key), 0) or 0)
    except (TypeError, ValueError):
        return "", ""
    threshold = int(settings.hint_escalate_after or 0)
    if threshold <= 0 or issued <= threshold:
        return "", ""
    if _blocked_on_previous_attempt(state, tool_name):
        # Just blocked — let this one through so the model can act on a changed
        # angle. If it repeats again, the next attempt is blocked again.
        return "", ""

    _, _, sub = key.partition(":")
    what = f"'{tool_name}' with subcommand '{sub}'" if sub else f"'{tool_name}'"
    reason = (
        f"{what} was {_HARD_BLOCK_MARK}: the stagnation notice was issued "
        f"{issued} times for this exact call shape and the call kept repeating, "
        f"so this attempt is refused instead of warned about again"
    )
    suggestion = (
        "This is a refusal, not advice — but it alternates: the NEXT attempt at "
        "this call will be allowed, and blocked again after that if nothing "
        "changes. Use that opening deliberately. Two moves remain: use a "
        "DIFFERENT "
        + ("subcommand or tool" if sub else "tool")
        + " to obtain the information, or stop gathering and state your "
        "conclusion from the evidence already collected. Being unable to observe "
        "further is itself a reportable conclusion."
    )
    return reason, suggestion


async def tool_screener(state: AgentState) -> dict:
    """Inspect pending tool_calls and decide whether to forward them.

    Returns a state delta. The delta always sets ``screener_route`` so
    the conditional edge can dispatch deterministically; it may also
    append synthetic ``ToolMessage`` responses (for REJECT/BANNED cases)
    or interrupt for human confirmation (for DRIFT cases).

    Fail-open policy: if the screener itself throws (classifier crash
    on malformed args, unexpected tool_call shape, etc.) the whole
    in-flight turn would die. We catch at the per-tool_call boundary,
    log the exception, and treat the offending call as ALLOW. The
    alternative — fail-closed — would let a classifier bug take
    production down. Operator sees ERROR-level logs and can intervene.
    """
    messages = state.get("messages", [])
    last_msg = messages[-1] if messages else None

    # Truncated response: execute_loop already neutralised the batch — parseable
    # calls were answered with a synthetic error, unparseable ones were stripped.
    # Route straight back to the loop so the ToolNode never sees it.
    #
    # Gated on there being no UNANSWERED batch pending: that is the flag's
    # precondition. A stale flag (from a turn that exited via replan/end without
    # passing a screener) would otherwise divert a FRESH batch, and the loop would
    # spin without those calls ever running.
    if state.get("truncated_tool_calls"):
        pending = isinstance(last_msg, AIMessage) and getattr(last_msg, "tool_calls", None)
        if not pending:
            return {
                "screener_route": SCREENER_ROUTE_RETRY,
                "truncated_tool_calls": False,
            }
        logger.warning(
            "tool_screener: stale truncated_tool_calls flag with an unanswered "
            "batch pending — clearing and screening normally",
        )

    # Defensive: no tool_calls to screen → pass through. This shouldn't
    # happen in practice because ``should_continue_execute_loop`` only
    # routes to "continue" when the last AIMessage has tool_calls, but
    # belt-and-braces.
    if not isinstance(last_msg, AIMessage) or not getattr(last_msg, "tool_calls", None):
        return {"screener_route": SCREENER_ROUTE_PASS, "truncated_tool_calls": False}

    approved = approved_from_dict(state.get("approved_target"))
    enforcing = bool(settings.target_guard_enforcing)
    skill_script_allowed = bool(settings.skill_script_default_allow)

    decisions: list[dict[str, Any]] = []
    has_drift = False
    has_other_reject = False
    has_provenance_reject = False
    has_context_reject = False
    # Vehicle live-discovery cache for this screening round + the state
    # delta persisting its outcome (positive and negative alike).
    cluster_vehicles: frozenset[str] = frozenset(
        state.get("known_vehicle_pods") or (),
    )
    probe_misses: frozenset[str] = frozenset(
        state.get("vehicle_probe_misses") or (),
    )
    probed_this_round = False
    vehicle_cache: dict[str, Any] = {}
    for tc in last_msg.tool_calls:
        tool_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        tool_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
        tool_call_id = (
            tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
        ) or ""
        # Which carrier gate refused, when one did. Carried to the single
        # rejection-logging outlet below (same pattern as ``constraint``) so a
        # stuck drill can be grouped by GATE in logs instead of by prose that
        # may be reworded.
        carrier_gate = ""

        # Shared capability verdict (fail-CLOSED), see capabilities.context.
        if not tool_call_allowed(tool_name, state, "execute"):
            # The verdict alone ("unavailable for the current environment
            # capability profile") is the SAME sentence for every tool in every
            # profile — it names neither the profile in force, nor the one the
            # tool belongs to, nor what to use instead. Ask the layer that made
            # the judgement for the actual cause.
            _cap_reason, _cap_fix = explain_tool_refusal(tool_name, state, "execute")
            decisions.append({
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "verdict": GuardVerdict.REJECT_UNKNOWN.value,
                "reason": _cap_reason,
                "suggestion": _cap_fix,
                "effective": None,
            })
            has_other_reject = True
            has_context_reject = True
            continue

        # Hard stagnation block. The soft path (a hint appended to the turn) is
        # the only lever ``filter_stagnant_tool`` leaves for SUBCOMMAND-level
        # stagnation, because removing the whole tool would blind the phase. That
        # lever assumes the model reads the hint and reconsiders — and
        # task-ff057e7f showed what happens when it does not: 100 iterations,
        # ~20 consecutive notices, and reasoning_content present on 2 of 100
        # turns. A model that is not reasoning cannot be reached by text, so no
        # number of reminders was ever going to work; only refusing the call can.
        #
        # The threshold is the escalation point itself, not a further grace
        # period: escalation already means "overwriting the notice has been shown
        # not to change behaviour", and there is no evidence that more notices
        # after that help. This runs in the screener rather than at bind time
        # because a subcommand is an ARGUMENT — there is nothing to unbind.
        _blocked_reason, _blocked_fix = _hard_stagnation_block(
            state, tool_name, tool_args,
        )
        if _blocked_reason:
            decisions.append({
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                # Its OWN verdict, not REJECT_UNKNOWN: the call is admissible
                # and this refusal alternates, so labelling it "unknown to the
                # classifier" would teach the model the tool is unavailable.
                "verdict": GuardVerdict.REJECT_STAGNANT.value,
                "reason": _blocked_reason,
                "suggestion": _blocked_fix,
                "effective": None,
            })
            has_other_reject = True
            carrier_gate = "stagnation_hard_block"
            continue

        try:
            if tool_name == "blade_destroy":
                effective, decision = _screen_blade_destroy(tool_args, messages)
                if decision.verdict != GuardVerdict.ALLOW:
                    has_provenance_reject = True
                feedback = decision_to_feedback(decision)
            else:
                effective = infer_effective_target(
                    tool_name, tool_args,
                    skill_script_allowed=skill_script_allowed,
                )
            if tool_name != "blade_destroy" and (
                effective.scope in (SCOPE_UNKNOWN, SCOPE_ESCAPE)
                or (
                    is_host_carrier_call(tool_name, tool_args)
                    # A host-entry wrapper (chroot/nsenter) around a READ-ONLY
                    # inner command is a diagnostic probe, not an injection. The
                    # classifier already resolved it to __readonly__ (its inner
                    # argv is read-only AND carries no shell metacharacter — the
                    # same test host_inject uses for its skip-guard fast path).
                    # Routing it into carrier resolution anyway made
                    # ``classify_host_operation`` return an empty family and the
                    # call was rejected as an "uncleared host-escape primitive"
                    # — wrongly, since it mutates nothing. A mutating inner
                    # command is classified __escape__ (not __readonly__) and
                    # still enters carrier resolution here.
                    and effective.scope != SCOPE_READONLY
                )
            ):
                carrier_resolved = False
                try:
                    carrier_resolution = effective_target_from_registered_carrier(
                        tool_name,
                        tool_args,
                        state.get("execution_artifacts"),
                        approved,
                    )
                except Exception as exc:
                    logger.exception(
                        "target_guard: carrier resolution failed; keeping call UNKNOWN"
                    )
                    carrier_resolution = CarrierResolution.errored(exc)
                # A carrier resolved here comes from this task's in-memory
                # registration; the live-discovery fallback below does not
                # qualify for the freshness window.
                carrier_from_registered = carrier_resolution.resolved
                # Fallback: live discovery for unregistered debug pods
                # (e.g. kubectl debug timed out before emitting metadata).
                # By exec time the pod is guaranteed to exist — the LLM
                # saw it in kubectl get pods before attempting exec.
                #
                # Only gates that a live read could actually overturn are
                # retried (see ``LIVE_DISCOVERY_RETRYABLE_REASONS``). A
                # FAMILY_MISMATCH / NO_BOUNDED_RECOVERY verdict is about the
                # COMMAND, so re-reading the cluster cannot change it — and it
                # would additionally let a synthetic (family-less) artifact
                # bypass the registered carrier's ``operation_family`` check.
                if (
                    carrier_resolution.reason in LIVE_DISCOVERY_RETRYABLE_REASONS
                    and is_host_carrier_call(tool_name, tool_args)
                ):
                    try:
                        carrier_resolution = await discover_unregistered_carrier(
                            tool_name,
                            tool_args,
                            state,
                            approved,
                        )
                    except Exception as exc:
                        logger.debug(
                            "target_guard: live carrier discovery failed",
                            exc_info=True,
                        )
                        carrier_resolution = CarrierResolution.errored(exc)
                if carrier_resolution.resolved:
                    carrier_effective = carrier_resolution.effective
                    artifact = carrier_resolution.artifact or {}
                    # A carrier that came from the live-discovery fallback (i.e.
                    # NOT from in-memory registration) was JUST confirmed by a
                    # fresh in-band ``kubectl get pod`` inside
                    # ``discover_unregistered_carrier`` (privileged + approved
                    # node + uid). Re-probing it via
                    # ``registered_carrier_is_current`` would be a redundant
                    # second in-band read on the very API path a network fault is
                    # severing — self-poisoning. Trust the discovery probe.
                    carrier_from_live_discovery = not carrier_from_registered
                    if carrier_from_live_discovery or _carrier_within_liveness_window(
                        artifact,
                        carrier_from_registered,
                        state.get("task_id", ""),
                    ):
                        # Either a freshly live-discovered carrier (already
                        # probed once) or a fresh, this-task, active registered
                        # carrier within the liveness window: trust it and skip
                        # the live re-probe, which under an in-band network fault
                        # would time out on the same API path the fault is
                        # severing (self-poisoning).
                        effective = carrier_effective
                        carrier_resolved = True
                    else:
                        try:
                            carrier_is_current = await registered_carrier_is_current(
                                artifact, state,
                            )
                        except Exception as exc:
                            logger.exception(
                                "target_guard: registered carrier verification failed; "
                                "keeping call UNKNOWN"
                            )
                            # "The re-read raised" and "the re-read disagreed"
                            # are different facts, and only one was observed.
                            # Reporting a mismatch we never saw would repeat, at
                            # a smaller scale, the misattribution this whole
                            # change removes.
                            carrier_resolution = CarrierResolution.verification_failed(
                                str(artifact.get("name") or "<unknown>"), exc,
                            )
                            carrier_is_current = False
                        if carrier_is_current:
                            effective = carrier_effective
                            carrier_resolved = True
                        elif carrier_resolution.resolved:
                            # Probe completed and disagreed (it did not raise, so
                            # ``carrier_resolution`` is still the resolved one).
                            carrier_resolution = CarrierResolution.stale(
                                str(artifact.get("name") or "<unknown>"),
                            )
                if is_host_carrier_call(tool_name, tool_args) and not carrier_resolved:
                    # Forward the gate's OWN account of the refusal. The
                    # screener does NOT infer it: resolution used to answer
                    # ``tuple | None``, and this branch guessed "pod not
                    # registered" for all ~12 gates — which is how
                    # task-866648cc was told its (correctly registered) debug
                    # pod was unapproved while the real gate was a missing
                    # self-reversal on an otherwise valid ``tc netem`` command.
                    carrier_gate = (
                        carrier_resolution.reason.value
                        if carrier_resolution.reason else "unknown"
                    )
                    effective = EffectiveTarget(
                        scope=SCOPE_ESCAPE,
                        namespace="",
                        confidence=ConfidenceLevel.UNKNOWN,
                        raw_command=effective.raw_command,
                        reject_detail=carrier_resolution.detail,
                        reject_suggestion=carrier_resolution.suggestion,
                    )
            if (
                tool_name != "blade_destroy"
                and effective.scope == "pod"
                and not effective.is_vehicle_exec
                and effective.names
                and not _identity_matches_approved(effective, approved)
            ):
                # An exec into an injection VEHICLE is machinery access, not
                # an operation on the fault target — exempt it from identity
                # drift. Vehicle identity is DATA-driven, never name-based:
                #   1. task-registered vehicles (``is_vehicle_name``: debug
                #      pod artifacts, kubectl_exec_pod_name, meta tags);
                #   2. LIVE cluster discovery for pods this task never
                #      registered (e.g. the ChaosBlade tool DaemonSet), one
                #      bounded probe per screening round, outcome persisted.
                # The probe runs on the fault-binary branch too: a drift
                # verdict that SURVIVES there can still be human-approved,
                # and ``_apply_drift_correction`` needs the discovered
                # identity to refuse rewriting the contract toward
                # machinery. Only the exemption FLAG is withheld from that
                # branch — a fault binary inside a privileged / hostNetwork
                # tool pod shapes the HOST and keeps identity review.
                unregistered = [
                    n for n in effective.names
                    if not is_vehicle_name(n, state)
                    and n not in cluster_vehicles
                    and n not in probe_misses
                ]
                if unregistered and not probed_this_round:
                    probed_this_round = True
                    positives, misses = await _discover_vehicle_pods(
                        state, unregistered,
                    )
                    cluster_vehicles = cluster_vehicles | positives
                    probe_misses = probe_misses | misses
                    new_known = positives - frozenset(
                        state.get("known_vehicle_pods") or (),
                    )
                    if new_known:
                        vehicle_cache["known_vehicle_pods"] = tuple(
                            (state.get("known_vehicle_pods") or ())
                            + tuple(sorted(new_known)),
                        )
                    if misses:
                        vehicle_cache["vehicle_probe_misses"] = tuple(
                            sorted(
                                frozenset(
                                    state.get("vehicle_probe_misses") or (),
                                ) | misses,
                            ),
                        )
                if not effective.fault_binary_mutation and all(
                    is_vehicle_name(n, state) or n in cluster_vehicles
                    for n in effective.names
                ):
                    # EffectiveTarget is frozen, so rebuild instead of mutating.
                    effective = replace(effective, is_vehicle_exec=True)
            if tool_name != "blade_destroy":
                # Single funnel: identity / recoverability verdict via the
                # gateway. ``decision`` drives routing (drift interrupt /
                # retry); ``feedback`` is the uniform shape rendered + audited
                # — reused below, never recomputed.
                decision, feedback = get_guard_gateway().check_target(
                    effective, approved,
                )
        except Exception as exc:
            if is_host_carrier_call(tool_name, tool_args):
                logger.exception(
                    "target_guard: host carrier screening crashed; failing closed"
                )
                decisions.append({
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "verdict": GuardVerdict.REJECT_UNKNOWN.value,
                    "reason": (
                        "host carrier safety classification failed: "
                        f"{exc.__class__.__name__}"
                    ),
                    "suggestion": "Use a registered, current execution carrier.",
                    "effective": None,
                })
                has_other_reject = True
                continue
            # Fail-open: classifier or guard crashed. Log loudly so
            # the bug surfaces, but don't kill the turn — produce an
            # ALLOW decision for this tool_call. The pre-existing
            # safety layers (safety_check, confirmation_gate) still
            # gate the broader plan.
            logger.exception(
                "target_guard: screener crashed on tool=%s args=%r; "
                "failing open (allowing the call)",
                tool_name, tool_args,
            )
            decisions.append({
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "verdict": "allow",  # treated as ALLOW for routing
                "reason": f"screener exception: {exc.__class__.__name__}: {exc}",
                "suggestion": "",
                "effective": None,
            })
            continue

        decisions.append({
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "verdict": decision.verdict.value,
            "reason": decision.reason,
            "suggestion": decision.suggestion,
            "is_hard_floor": feedback.is_hard_floor,
            "constraint": feedback.constraint.value,
            "carrier_gate": carrier_gate,
            "effective": effective,
        })

        if decision.verdict == GuardVerdict.REJECT_DRIFT:
            has_drift = True
        elif decision.verdict in (
            GuardVerdict.REJECT_BANNED, GuardVerdict.REJECT_UNKNOWN,
        ):
            has_other_reject = True

    any_reject = has_drift or has_other_reject

    # Log every non-ALLOW outcome so operators can audit false-positives
    # before flipping enforcement on. Logging happens regardless of mode.
    for d in decisions:
        if d["verdict"] in ("allow", "readonly"):
            continue
        _gate = d.get("carrier_gate") or ""
        logger.warning(
            "target_guard: %s [%s] tool=%s%s reason=%s%s",
            d["verdict"], d.get("constraint", "-"), d["tool_name"],
            f" gate={_gate}" if _gate else "",
            d["reason"],
            "" if enforcing else " (log-only, enforcement disabled)",
        )

    # Log-only mode: pass through regardless of verdicts.
    if (
        not enforcing
        and not has_provenance_reject
        and not has_context_reject
    ) or not any_reject:
        return {"screener_route": SCREENER_ROUTE_PASS, **vehicle_cache}

    # Enforcing mode + at least one reject — fabricate ToolMessages so
    # the LangChain conversation stays well-formed (every tool_call
    # needs a matching response) and the LLM sees the rejection text.
    rejection_msgs = [
        ToolMessage(
            content=_format_rejection_for_llm(d, approved is None, approved),
            name=d["tool_name"],
            tool_call_id=d["tool_call_id"],
            status="error",
        )
        for d in decisions
    ]

    # --- Drift path: interrupt for human confirmation ---
    if has_drift:
        drifted = [d for d in decisions if d["verdict"] == GuardVerdict.REJECT_DRIFT.value]
        first_eff = drifted[0].get("effective") if drifted else None
        drift_reject_count = int(state.get("drift_reject_count") or 0)

        if drift_reject_count >= 1:
            # Already rejected once — hard terminate.
            return {
                "messages": rejection_msgs,
                "screener_route": SCREENER_ROUTE_RETRY,
                **fail_state(
                    FailureCategory.USER_REJECTED,
                    "Target drift persists after user rejection; terminating.",
                ),
                **vehicle_cache,
            }

        # CLI mode: no interactive human to confirm drift — reject and
        # let LLM self-correct. Second drift hits drift_reject_count>=1
        # hard-terminate above.
        if state.get("interaction_mode") == "cli":
            logger.warning(
                "target_guard: drift in CLI mode (count=%d), rejecting tool_calls",
                drift_reject_count,
            )
            return {
                "messages": rejection_msgs,
                "screener_route": SCREENER_ROUTE_RETRY,
                "drift_reject_count": drift_reject_count + 1,
                **vehicle_cache,
            }

        _reason = drifted[0]["reason"] if drifted else ""
        agent_reason = _extract_agent_reason(last_msg)
        drift_info = {
            "type": "target_change",
            "summary": f"Target change detected: {_reason}",
            "reason": _reason,
            "agent_reason": agent_reason,
            "original": _format_approved_for_card(approved),
            "proposed": _format_effective_for_card(first_eff) if first_eff else {},
            "tool_calls": [
                {"name": d["tool_name"], "reason": d["reason"]}
                for d in drifted
            ],
        }

        user_decision = interrupt(drift_info)

        if user_decision == "approved":
            spec_delta = _apply_drift_correction(
                state, first_eff, cluster_vehicles,
            )
            return {
                "screener_route": SCREENER_ROUTE_PASS,
                "drift_reject_count": 0,
                **spec_delta,
                **vehicle_cache,
            }
        else:
            return {
                "messages": rejection_msgs,
                "screener_route": SCREENER_ROUTE_RETRY,
                "drift_reject_count": drift_reject_count + 1,
                **vehicle_cache,
            }

    # --- Non-drift reject (BANNED / UNKNOWN): retry in place ---
    return {
        "messages": rejection_msgs,
        "screener_route": SCREENER_ROUTE_RETRY,
        **vehicle_cache,
    }


def route_after_screener(state: AgentState) -> str:
    """Map the screener's ``screener_route`` field to a graph edge.

    Mirrors the SCREENER_ROUTE_* sentinels. Defaults to "pass" so a
    missing/unknown value never strands the graph.
    """
    route = state.get("screener_route") or SCREENER_ROUTE_PASS
    if route == SCREENER_ROUTE_REPLAN:
        return "replan"
    if route == SCREENER_ROUTE_RETRY:
        return "retry"
    return "pass"


def _format_rejection_for_llm(
    decision: dict[str, Any],
    approved_missing: bool,
    approved: ApprovedTarget | None = None,
) -> str:
    """Render a ToolMessage body explaining why the call was blocked.

    Three goals:
      - Tell the LLM WHAT went wrong (reason) so it can rethink.
      - Tell the LLM what WOULD have been allowed (suggestion).
      - Tell the LLM whether the path is a hard floor (stop) or a reshapeable
        form issue (fix and retry), so it keeps its exploration space instead
        of concluding a viable path is a dead-end.
      - Be short — long rejections waste context tokens.
    """
    verdict = decision["verdict"]
    reason = decision["reason"]
    suggestion = decision["suggestion"]
    is_hard_floor = decision.get("is_hard_floor", False)
    parts = [
        f"[target_guard] {verdict.upper()} — {reason}",
    ]
    if suggestion:
        parts.append(suggestion)
    if approved_missing and verdict == GuardVerdict.REJECT_UNKNOWN.value:
        parts.append(
            "no approved target on record; the screener default-denies "
            "destructive calls until confirmation_gate has been passed."
        )
    # Node-scope drift on a node-scope task is almost always "host-escape in the
    # right shape, wrong node" (e.g. `kubectl debug node/<unapproved>`). The
    # audit reason already embeds approved.names, but buried in dense text the
    # LLM tends to read it as a dead-end and bail to verify. Surface an
    # imperative, copy-pasteable hint so it re-targets an approved node instead.
    eff = decision.get("effective")
    if (
        verdict == GuardVerdict.REJECT_DRIFT.value
        and approved is not None
        and approved.scope == "node"
        and approved.names
        and eff is not None
        and getattr(eff, "scope", "") == "node"
    ):
        parts.append(
            "Approved nodes: [" + ", ".join(approved.names) + "]. For a "
            "host-level change, target ONE of these approved nodes "
            "(e.g. `kubectl debug node/<approved-node> --profile=sysadmin`) "
            "— not any other node."
        )
    if is_hard_floor:
        parts.append(
            "This is a boundary the guard will not relax; operate within the "
            "approved target or abort if the task cannot proceed."
        )
    else:
        parts.append(
            "This is not a dead-end: adjust the tool_call as above and retry."
        )
    return " ".join(parts)


def _format_approved_for_card(approved: ApprovedTarget | None) -> dict:
    if approved is None:
        return {}
    return {
        "scope": approved.scope,
        "namespace": approved.namespace,
        "names": list(approved.names),
        "labels": dict(approved.labels),
        "blade_target": approved.blade_target,
    }


def _format_effective_for_card(eff: EffectiveTarget) -> dict:
    return {
        "scope": eff.scope,
        "namespace": eff.namespace,
        "names": list(eff.names),
        "labels": dict(eff.labels),
        "blade_target": eff.blade_target,
    }


_AGENT_REASON_MAX_LEN = 200


def _extract_agent_reason(msg: AIMessage) -> str:
    """Extract a short explanation from the AIMessage that triggered drift.

    Prefers ``content`` (the LLM's visible text); falls back to a
    truncated ``reasoning_content`` (thinking trace).

    ``content`` may be a str or a list of content blocks (multimodal /
    thinking models). We normalise to str before truncating.
    """
    raw = getattr(msg, "content", "") or ""
    if isinstance(raw, list):
        raw = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in raw
        ).strip()
    text = raw.strip() if isinstance(raw, str) else ""
    if text:
        return text[:_AGENT_REASON_MAX_LEN]
    additional = getattr(msg, "additional_kwargs", None) or {}
    reasoning = (additional.get("reasoning_content", "") or "").strip()
    if reasoning:
        return reasoning[:_AGENT_REASON_MAX_LEN]
    return ""


def _apply_drift_correction(
    state: AgentState,
    eff: EffectiveTarget | None,
    discovered_vehicles: frozenset[str] = frozenset(),
) -> dict:
    """Correct fault_spec + refreeze approved_target after user approves drift."""
    from chaos_agent.config.settings import settings as _settings

    spec = read_fault_spec(state)
    if not spec or not eff:
        return {}

    # Never rewrite fault_spec toward an injection vehicle. Even if a
    # vehicle exec still produced a drift verdict (a residual path that
    # keeps identity review, e.g. the fault-binary branch) and a human
    # approved it, the correction must not point the spec at injection
    # machinery — that is how a drift loop ends its run "targeting" the
    # tool pod instead of the workload. Same DATA-driven oracle as the
    # screener: task-registered vehicles, previously persisted discoveries,
    # and vehicles discovered in THIS screening round (an interrupt resumes
    # before the round's cache reaches state, so the caller passes it in).
    if eff.names and all(
        is_vehicle_name(n, state)
        or n in frozenset(state.get("known_vehicle_pods") or ())
        or n in discovered_vehicles
        for n in eff.names
    ):
        logger.warning(
            "target_guard: drift correction toward vehicle pod(s) %s skipped "
            "(fault_spec must never point at injection machinery)",
            list(eff.names),
        )
        return {}

    corrections: dict = {}
    if eff.namespace and eff.namespace != spec.namespace:
        if eff.namespace not in (_settings.blacklist_namespaces or []):
            corrections["namespace"] = eff.namespace
    if eff.names and tuple(eff.names) != spec.names:
        corrections["names"] = tuple(eff.names)
    if eff.labels and eff.labels != spec.labels:
        corrections["labels"] = eff.labels

    if corrections:
        new_spec = spec.replace(**corrections)
        if "names" in corrections:
            logger.debug(
                "spec-write: writer=tool_screener._apply_drift_correction "
                "names %s -> %s basis=user-approved drift EffectiveTarget",
                list(spec.names), list(corrections["names"]),
            )
    else:
        new_spec = spec

    # Preserve the label-derived frozen sets (owner_names / resolved_names)
    # that safety_check discovered, UNLESS the correction changed the label
    # selector — in which case they are stale and must be dropped (the guard
    # then falls back to its stricter labels/name comparison). Both are derived
    # from the approved labels, so "labels changed" invalidates both.
    existing = state.get("approved_target") or {}
    if "labels" in corrections:
        owner_names: tuple[str, ...] = ()
        resolved_names: tuple[str, ...] = ()
    else:
        owner_names = tuple(existing.get("owner_names") or ())
        resolved_names = tuple(existing.get("resolved_names") or ())

    result: dict = {"fault_spec": new_spec.to_dict()}
    result["approved_target"] = freeze_approved_target_from_spec(
        new_spec, owner_names=owner_names, resolved_names=resolved_names,
    )
    return result


__all__ = [
    "SCREENER_ROUTE_PASS",
    "SCREENER_ROUTE_REPLAN",
    "SCREENER_ROUTE_RETRY",
    "route_after_screener",
    "tool_screener",
]
