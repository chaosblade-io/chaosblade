"""Confirmation gate for a material FaultSpec change during planning.

``FaultSpec`` is the only durable fault contract.  A plan-change tool call is
an ephemeral proposal: this node validates it against the reviewed revision,
asks the user, and atomically replaces the contract only after approval.
"""

from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import interrupt

from chaos_agent.agent.nodes.execute.execute_loop import reset_attribution_state
from chaos_agent.agent.nodes.store._store_sync import sync_node_status_to_session, sync_to_store
from chaos_agent.agent.prompts.reminder import wrap_system_reminder
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.config.settings import settings
from chaos_agent.agent.spec.fault_spec import (
    FaultSpec,
    is_full_fault_spec_proposal,
    read_fault_spec,
    strip_timeout_alias,
)
from chaos_agent.agent.spec.intent_anchor import extract_explicit_node_anchor
from chaos_agent.agent.state import AgentState, has_live_fault
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.target_guard import canonicalise_kind

logger = logging.getLogger(__name__)


def _terminal_rejection_failure(state: AgentState, detail: str) -> dict:
    context = state.get("replan_context")
    original_error = (
        str(context.get("error_summary") or "").strip()
        if isinstance(context, dict)
        else ""
    )
    if original_error:
        return fail_state(FailureCategory.EXECUTION_FAILED, f"{original_error} | {detail}")
    return fail_state(FailureCategory.USER_REJECTED, detail)


async def _destroy_superseded_experiments(
    state: AgentState,
) -> tuple[list[str], list[str]]:
    """Deterministically destroy every live experiment the old contract left
    running (B76 review G — contract-boundary serialization).

    An approval replaces the INTENT, not the cluster state: whatever the old
    contract injected keeps running until proven destroyed, and a live
    superseded experiment both pollutes the new contract's verification (two
    faults stacked on the same target) and falls out of every single-value
    recovery channel the moment the new create overwrites the attribution
    slot. Mirrors verify-replan's destroy-first seam. Thin delegation to the
    registry's carrier-neutral sweep (``sweep_live_liabilities``) so the
    approval seam and the recover finale share ONE destroy/death-filter
    contract.

    Returns ``(retired_new, failures)``; failures are human-readable
    ``uid: reason`` lines.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    return await FaultProviderRegistry.sweep_live_liabilities(state)


def _extract_proposal(state: AgentState, current: FaultSpec) -> tuple[str, FaultSpec, int] | None:
    """Read one transient proposal from the most recent Phase 1 tool call."""
    for message in reversed(state.get("messages", [])):
        if not isinstance(message, AIMessage):
            continue
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
            if name != "propose_plan_change":
                continue
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
            if not isinstance(args, dict):
                return None
            raw = args.get("proposed_fault")
            if not is_full_fault_spec_proposal(raw):
                return None
            try:
                revision = int(args.get("fault_revision"))
            except (TypeError, ValueError):
                return None
            candidate = FaultSpec.from_intent_args(
                strip_timeout_alias(raw), existing=current,
            )
            if not candidate.is_complete:
                return None
            reason = str(args.get("reason") or "").strip()
            return reason, candidate, revision
        break
    return None


def _public_fault(spec: FaultSpec) -> dict[str, Any]:
    """Use a stable, renderer-friendly projection without a duplicate model."""
    return {
        "scope": spec.scope,
        "fault_target": spec.fault_target,
        "fault_action": spec.fault_action,
        "fault_type": spec.fault_type,
        "fault_spec": spec.to_intent_dict(),
        "boundaries": list(spec.boundaries),
        "constraints": list(spec.constraints),
        "assumptions": list(spec.assumptions),
        "revision": spec.revision,
    }


def _replace_batch_item(state: AgentState, spec: FaultSpec) -> dict | None:
    """Keep the serial batch list equal to the approved canonical contract."""
    batch = state.get("batch_submit_args")
    if not isinstance(batch, dict) or not isinstance(batch.get("faults"), list):
        return None
    try:
        index = int(state.get("current_fault_index") or 0)
    except (TypeError, ValueError):
        return None
    faults = list(batch["faults"])
    if index < 0 or index >= len(faults):
        return None
    faults[index] = spec.to_dict()
    result = deepcopy(batch)
    result["faults"] = faults
    first = FaultSpec.from_dict(faults[0]) if faults else None
    result["fault_revision"] = first.revision if first is not None else 0
    return result


def _alignment_violation(candidate: FaultSpec, current: FaultSpec) -> str | None:
    """Return WHY this proposal cannot be auto-applied in CLI mode (None = aligned).

    The user's request text anchors TWO dimensions, and the non-interactive
    auto-approval may only fire when BOTH survive the replacement:

    - identity — the resource the user named themselves. Recovered ONLY from
      the current contract's verbatim entry-point text — never from the
      candidate's: the proposal dict is LLM-authored end to end, so trusting
      its echoed description would let a fabricated anchor unlock the
      auto-approval. Kind must match and proposed names must be a subset of
      the anchor set.
    - mechanism — the fault domain the user described, as frozen into the
      current contract's ``fault_target``. B76 review D1 (probe
      ``probe_b76_round4.py``): a disk/fill proposal riding aligned names
      past the auto-approval silently swapped a network-loss drill for a 95%
      disk fill — different fault nature, risk tier, and recovery shape. A
      different domain is NEVER aligned. ``fault_action`` and ``params``
      stay free: in-domain adjustment (loss→delay, percent tuning, name
      narrowing) is the routine replan path.

    A plan change replaces WHAT is attacked, which normally demands human
    approval. CLI mode has no human at the interrupt — until B76 that made
    every proposal a dead end: the executor could prove the reviewed contract
    unexecutable, correctly call ``propose_plan_change``, and be sent back to
    re-plan under the very contract it had just disproven (task
    inject-5552c6e4: rejected twice, third planning round re-derived the same
    dead plan, terminal rejection 44 minutes later).

    The one auto-approvable class: the proposal's target IS the resource the
    user themselves named — same KIND (node: the anchor vocabulary is
    node-only, so a pod/deployment namesake is a different resource, not the
    named one) and names INSIDE the anchor set (a proposal adding targets the
    user never named is an expansion, not an alignment). The anchor is read
    from the CURRENT contract's ``user_description`` — the entry point's
    verbatim intent text — NEVER from the candidate's (see identity above).
    Anything else (different node, different kind, extra targets, anchorless
    text, different domain) keeps the existing rejection — an unaligned
    change without the user in the loop stays a dead end by design.

    The returned token names the violated dimension so the CLI rejection
    receipt can point the LLM at the right correction (identity vs mechanism
    need different fixes).
    """
    anchors = extract_explicit_node_anchor(current.user_description)
    if not anchors:
        return "anchorless"
    if canonicalise_kind(candidate.scope) != "node":
        return "kind"
    proposed_names = set(candidate.names or ())
    if not proposed_names or not proposed_names <= set(anchors):
        return "identity"
    if candidate.fault_target != current.fault_target:
        return "mechanism"
    return None


async def plan_change_confirm(state: AgentState) -> dict:
    """Confirm an explicit plan-time replacement of the reviewed FaultSpec."""
    current = read_fault_spec(state)
    if current is None:
        return {}
    proposal = _extract_proposal(state, current)
    if proposal is None:
        return {}
    reason, candidate, submitted_revision = proposal
    if submitted_revision != current.revision:
        return {
            "messages": [HumanMessage(content=wrap_system_reminder(
                "[PLAN CHANGE RETRY] The proposal referenced a stale FaultSpec revision. "
                "Read the current reviewed contract and propose a complete replacement."
            ))],
        }
    if candidate.contract_dict() == current.contract_dict():
        return {
            "messages": [HumanMessage(content=wrap_system_reminder(
                "[PLAN CHANGE RETRY] The proposal does not change the reviewed FaultSpec. "
                "Continue planning or finish with the current contract."
            ))],
        }

    rejected_count = int(state.get("plan_change_reject_count") or 0)
    auto_approved_cli = False
    if state.get("interaction_mode") == "cli":
        violation = _alignment_violation(candidate, current)
        if violation is None:
            auto_budget = int(state.get("plan_change_auto_approve_count") or 0)
            if auto_budget >= settings.max_plan_change_auto_approvals:
                # Budget seam (B76 review E1, probe_b76_round5.py): every
                # auto-approval also resets the replan/execute budgets and —
                # via agent_loop's replan-entry reset — agent_loop_count
                # itself, so an unbounded aligned-approval loop can never
                # trip MAX_AGENT_LOOP (its unbounded-loop defence at
                # agent_loop L376 assumes monotonic replan counters). The
                # counter is task-lifetime and NEVER reset by an approval,
                # mirroring how max_replan_count caps per-contract replans.
                # Terminate through the same channel as rejected-twice: the
                # loop demonstrably cannot converge.
                result = {
                    **_terminal_rejection_failure(
                        state,
                        "Plan change auto-approval budget exhausted in CLI "
                        f"mode; terminating after {auto_budget} aligned "
                        "replacements without convergence.",
                    ),
                }
                await sync_to_store(state, result)
                return result
            # Non-interactive auto-approval (B76): the proposal keeps every
            # dimension the user anchored (identity AND mechanism), so no
            # human decision is being substituted — the seam below stamps the
            # audit trail.
            auto_approved_cli = True
        elif rejected_count >= 1:
            result = {
                "plan_change_reject_count": rejected_count + 1,
                **_terminal_rejection_failure(state, "Plan change rejected twice in CLI mode; terminating."),
            }
            await sync_to_store(state, result)
            return result
        elif violation == "mechanism":
            result = {
                "plan_change_reject_count": rejected_count + 1,
                "messages": [HumanMessage(content=wrap_system_reminder(
                    "[PLAN CHANGE REJECTED] CLI mode cannot confirm a plan change "
                    f"that swaps the fault domain ({current.fault_target} -> "
                    f"{candidate.fault_target}): the user asked for a "
                    f"{current.fault_target} fault. Adjust fault_action or "
                    "params within the same domain, or finish_planning(rejected=True)."
                ))],
            }
            await sync_to_store(state, result)
            return result
        else:
            result = {
                "plan_change_reject_count": rejected_count + 1,
                "messages": [HumanMessage(content=wrap_system_reminder(
                    "[PLAN CHANGE REJECTED] CLI mode cannot confirm a plan change "
                    "that does not target the resource the user explicitly named "
                    "in the request. Continue with the reviewed FaultSpec or "
                    "finish_planning(rejected=True)."
                ))],
            }
            await sync_to_store(state, result)
            return result

    if not auto_approved_cli:
        decision = interrupt({
            "type": "plan_change",
            "reason": reason or "Planning requires a materially different fault contract.",
            "original": _public_fault(current),
            "proposed": _public_fault(candidate),
        })
        if decision != "approved":
            rejected_count += 1
            if rejected_count >= 2:
                result = {
                    "plan_change_reject_count": rejected_count,
                    **_terminal_rejection_failure(state, "Plan change rejected twice; terminating planning."),
                }
            else:
                result = {
                    "plan_change_reject_count": rejected_count,
                    "messages": [HumanMessage(content=wrap_system_reminder(
                        "[PLAN CHANGE REJECTED] The user declined the replacement. Continue with the "
                        "reviewed FaultSpec, try a different proposal, or finish_planning(rejected=True)."
                    ))],
                }
            await sync_to_store(state, result)
            return result

    approved = candidate.replace(
        revision=current.revision + 1,
        source=current.source,
        user_description=candidate.user_description or current.user_description,
    )
    batch = _replace_batch_item(state, approved)
    # Contract-boundary serialization (B76 review G): the old contract's live
    # experiments are destroyed HERE, before the replacement can inject on
    # top of them — keep_experiment_uid below only preserves the attribution
    # pointer, which the next create would overwrite anyway (the orphan
    # chain, probe_b76_round7.py). Success retires; failure degrades to a
    # warning and the liability set keeps the UID for the recover sweep.
    retired_new, destroy_failures = await _destroy_superseded_experiments(state)
    _supersede_note = ""
    if retired_new:
        _supersede_note += (
            "The framework destroyed the superseded experiment(s) before this "
            f"contract takes effect: {', '.join(retired_new)}. "
        )
    if destroy_failures:
        _supersede_note += (
            "WARNING — superseded experiment(s) could not be destroyed "
            f"({' | '.join(destroy_failures)}). They remain tracked: do NOT "
            "build on top of them; the recovery sweep will retry their destroy. "
        )
    result: dict[str, Any] = {
        "fault_spec": approved.to_dict(),
        "skill_name": None,
        "plan": None,
        "plan_path": None,
        "is_complex": False,
        "skill_case_content": None,
        "plan_change_reject_count": 0,
        "safety_status": "pending",
        "safety_reason": None,
        "safety_checked_detail": None,
        "feasibility_report": None,
        "conflict_uids": None,
        "needs_confirmation": False,
        "approved_target": None,
        "baseline_data": None,
        "inject_layer1_cache": None,
        "verification": None,
        # Contract seam: a new contract starts with fresh loop budgets.
        # Inheriting the OLD contract's replan debt lets one unlucky round
        # exhaust the new contract's budget before it has executed anything
        # (task-71fa78b6: rev3 entered with 2/3 spent and died on its first
        # replan). New contract == new budget, same as the message promises.
        "replan_count": 0,
        "verify_replan_count": 0,
        # Same fresh-budget seam for the CLI drift tally (code review
        # 2026-09-11, cascade-verified): drift_reject_count is measured
        # against the contract's approved identity — the FIRST silent
        # drift is counted so the LLM can self-correct, a SECOND hits
        # DRIFT_TERMINATED. The old contract's tally must not pre-spend
        # the new contract's only silent-drift allowance: without this
        # reset, one drift under the old contract + one under the new
        # one terminates a task whose current contract never drifted.
        # The TUI drift-approved branch resets the same counter for the
        # same reason (tool_screener): every new authority starts at zero.
        "drift_reject_count": 0,
        # Explicit contract boundary (B76 review C3): the OLD contract's
        # replan_context — error summaries AND its guard_rejections — is
        # stale the moment the approval lands; guard receipts are
        # contract-relative. agent_loop's is_replan gating previously kept
        # this dormant only as a side effect of the counter resets above;
        # clearing it here makes the seam explicit and ordering-independent
        # (no future consumer needs to re-derive "counters imply staleness").
        "replan_context": None,
        "messages": [HumanMessage(content=wrap_system_reminder(
            ("[PLAN CHANGE APPROVED — CLI non-interactive] The proposal re-targets the "
             "node the user explicitly named in the request, so it was applied "
             "without an interactive confirmation. "
             if auto_approved_cli else "")
            + f"[PLAN CHANGE APPROVED] FaultSpec revision {approved.revision} is now authoritative: "
            f"{approved.fault_type}. Re-evaluate feasibility, choose a matching skill, and build "
            "a fresh plan. Do not reuse evidence or execution assumptions from the old contract. "
            "Execution budgets and injection attribution have been reset for this new contract. "
            + _supersede_note
        ))],
    }
    if retired_new:
        result["retired_experiment_uids"] = (
            list(state.get("retired_experiment_uids") or []) + retired_new
        )
    if auto_approved_cli:
        # Budget spend (B76 review E1): monotonic task-lifetime tally — the
        # ONE counter this seam deliberately does NOT reset. Resetting it
        # here would hand the aligned-approval loop an infinite budget and
        # re-arm the unbounded loop (probe_b76_round5.py). Interactive
        # approvals never touch it: the human IS the budget.
        result["plan_change_auto_approve_count"] = (
            int(state.get("plan_change_auto_approve_count") or 0) + 1
        )
    if settings.replan_reset_execute_count:
        result["execute_loop_count"] = 0
    # Same canonical seam reset the execute-time replan applies: attribution
    # (method / carrier pod / caches / epoch boundary) must not leak across
    # contracts. The keep decision keys on the LIVE fault predicate
    # (round-27 R1): a fault the environment still carries (any carrier:
    # live blade experiment OR attributed native mutation) keeps its
    # recovery handle; wiping it here would orphan a live fault the
    # recover graph can no longer identify. The committed twin answered
    # "ever-committed" instead — a destroyed experiment's UID survived
    # the seam and rode into the next intent's epoch as a corpse (with
    # the method cleared, the round-27 R2 combo check then licensed a
    # combo on it). Destroyed experiments release the slot here; the
    # retired ledger and message history keep their identity for
    # recovery targeting regardless.
    # state_messages (not a tail count): the seam also removes the stale
    # synthetic baseline pair — this path already nulls baseline_data, so a
    # surviving old pair would contradict the "no baseline yet" state the
    # next cycle starts from — and re-bases the epoch on the POST-MERGE
    # length. See reset_attribution_state docstring.
    reset_attribution_state(
        result,
        keep_experiment_uid=has_live_fault(state),
        state_messages=state.get("messages") or [],
        state=state,
    )
    # Anchor spec re-freeze (cascade review O1): the anchor's
    # ``fault_spec`` snapshot froze the APPROVED-at-the-time contract and
    # never refreshes (``_ledger_seeded`` re-freezes only when the anchor
    # is absent), so a user-approved plan change would leave the ledger
    # rendering the RETIRED spec as "Goal (ANCHOR, immutable)" into the
    # execute/verify/recover prompts while state.fault_spec carries the
    # approved one — two competing truth sources, undefined which the
    # model obeys. The anchor's immutability contract (merge rejects
    # anchor deltas) exists to stop the MODEL rewriting its own goal;
    # a user-approved change on this confirmation seam is the legitimate
    # writer the contract never provisioned. Split the semantics by
    # field: goal stays verbatim (user intent never changes here),
    # fault_spec is re-frozen to the approved contract. Built by the
    # harness node directly, NOT via merge — the merge channel is exactly
    # what must stay closed to tools. Read order: result first (the
    # reset above may already have written a plan-scoped-field patch —
    # compose, don't clobber), else the pre-seam state ledger.
    from chaos_agent.agent.progress_ledger import ANCHOR
    _ledger_now = result.get("progress_ledger")
    if not isinstance(_ledger_now, dict):
        _ledger_now = state.get("progress_ledger")
    if (
        isinstance(_ledger_now, dict)
        # Empty anchor == absent (execute_loop's seeding reads
        # ``not (ledger.get(ANCHOR) or {})``): an intent-stage ledger
        # carries an empty anchor by design, and the lazy seeding —
        # not this seam — owns freezing it.
        and _ledger_now.get(ANCHOR)
    ):
        result["progress_ledger"] = {
            **_ledger_now,
            ANCHOR: {
                **_ledger_now[ANCHOR],
                "fault_spec": approved.to_dict(),
            },
        }
    # Anchor absent (first attempt shape, or cross-graph intent ledger
    # without an anchor): execute_loop's lazy seeding freezes the
    # APPROVED spec on the next entry — nothing to re-freeze here.
    if batch is not None:
        result["batch_submit_args"] = batch
    sync_node_status_to_session(
        state,
        "plan_change_confirm",
        f"Plan change approved: {current.fault_type} -> {approved.fault_type}",
        detail={
            "approved": True,
            "auto": auto_approved_cli,
            "old_revision": current.revision,
            "new_revision": approved.revision,
        },
    )
    await sync_to_store(state, result)
    return result
