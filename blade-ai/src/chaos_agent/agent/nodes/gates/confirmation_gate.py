"""Confirmation gate node: interrupt() for human-in-the-loop approval."""

import logging

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec
from chaos_agent.agent.nodes.store._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.target_guard import freeze_approved_target_from_spec
from chaos_agent.agent.target_guard.freeze import approved_from_dict
from chaos_agent.agent.target_guard.mechanism_writes import (
    entries_beyond_victim,
    entries_from_list,
    format_entries_for_payload,
)
from chaos_agent.agent.nodes.gates._write_set_boundary import (
    write_set_boundary_result,
)
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)


def _generate_dry_run_plan(state: AgentState) -> str:
    """Generate a complete injection plan for dry_run (/plan) output."""
    from chaos_agent.agent.spec.plan_generator import generate_injection_plan
    return generate_injection_plan(state)


def _build_plan_preview_markdown(state: dict) -> str:
    """Build a Markdown preview string for the injection plan.

    Uses only the restricted Markdown subset (## / ### / - / **bold** / `code`).
    Returns an empty string when there is nothing to preview.

    Only the review-relevant sections are surfaced: ``## Task Summary``
    (what/why in a few lines) + ``## Execution Steps`` (what the approver
    actually sanctions). The complex-track plan now lives FULL in
    ``state["plan"]`` (50-70 lines in practice); rendering it inline
    recreates the Ink cursor desync that killed the original inline
    plan_summary body. Verification Methods / Expected Impact travel to
    the verifier instead of the card; the full markdown stays one
    ``cat <plan_path>`` away. Plans without the headers (simple track,
    legacy) fall back to the full text so nothing is lost.
    """
    from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
        _plan_section,
    )

    parts: list[str] = []

    plan = state.get("plan", "")
    if plan:
        sections = [
            _plan_section(plan, header)
            for header in ("task summary", "execution steps")
        ]
        preview = "\n\n".join(p for p in sections if p) or plan
        parts.append(f"## Plan Overview\n\n{preview}")

    scope = state.get("blast_radius_scope", "")
    detail = state.get("blast_radius_detail", "")
    if scope:
        blast = f"**{scope}**"
        if detail:
            blast += f" — {detail}"
        parts.append(f"## Blast Radius\n\n{blast}")

    return "\n\n".join(parts)


def _write_set_widening(state: dict) -> list[dict]:
    """Manifest entries beyond the victim target's coverage, or ``[]``.

    Reads the snapshot safety_check froze (victim ∪ manifest entries) and
    applies the coverage predicate: an entry the victim approval — plus its
    same-namespace secondary net — already governs is NOT widening. The
    returned payloads render the entries verbatim for the confirmation
    card and the unattended boundary exit; empty for every case without
    a manifest, so both surfaces stay unchanged for them.
    """
    approved = approved_from_dict(state.get("approved_target") or {})
    if approved is None:
        return []
    beyond = entries_beyond_victim(approved)
    if not beyond:
        return []
    return format_entries_for_payload(beyond)


async def confirmation_gate(state: AgentState) -> dict:
    """Pause execution and wait for human confirmation.

    Uses LangGraph's interrupt() mechanism to pause the graph.
    The caller (Server route) will resume with Command(resume="approved"|"rejected").

    For confirm_required status (P1: same-target same-action overlay),
    CLI mode checks --force-override flag to skip interrupt().

    Dry-Run mode (TUI `/plan`): when ``state.dry_run`` is True, the gate emits
    a preview AIMessage describing what would happen and returns immediately
    (no interrupt). The post-gate router will then send the graph to END.
    """
    task_id = state.get("task_id", "") or ""
    plan = state.get("plan", "")
    skill_name = read_active_skill_name(state)
    # Read FaultSpec once and project to legacy target dict for the
    # confirm_info payload (TUI confirm card still consumes the
    # 4-key target dict shape — TUI rendering layer change is out
    # of scope for the state refactor).
    spec = read_fault_spec(state) or FaultSpec()
    target = {
        "namespace": spec.namespace,
        "names": list(spec.names),
        "labels": dict(spec.labels),
        "resource_type": spec.scope,
    }
    safety_status = state.get("safety_status", "safe")

    # Emit status: waiting for confirmation
    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "confirmation_gate",
        f"Waiting for human confirmation for skill '{skill_name}'",
        {"skill_name": skill_name, "target": target},
    )

    # Dry-Run: generate a complete injection plan and emit as AIMessage.
    if state.get("dry_run"):
        plan_text = _generate_dry_run_plan(state)
        # Widened-contract entries ride the /plan preview verbatim so the
        # operator sees the case legislation BEFORE issuing /run — the
        # lift seam's knowledge card then confirms what was previewed.
        widening = _write_set_widening(state)
        if widening:
            from chaos_agent.agent.target_guard.mechanism_writes import (
                format_mechanism_writes_for_display,
            )
            entries_block = format_mechanism_writes_for_display(widening)
            if entries_block:
                plan_text = f"{plan_text}\n\n{entries_block}"
        logger.info("dry_run plan generated for task %s", task_id)
        tracker.complete("Dry-Run plan generated")
        sync_node_status_to_session(
            state,
            "confirmation_gate",
            "Dry-Run plan generated",
            detail={"dry_run": True},
        )
        result = {
            "messages": [AIMessage(content=plan_text)],
            "needs_confirmation": False,
            "plan_summary": plan_text,
        }
        await sync_to_store(state, result)
        return result

    # Widened-contract entries beyond the victim coverage — computed
    # EARLY because two branches below consult it: the force_override
    # bypass must not swallow a widened contract (its charter is the
    # same-action overlay exemption, not write-set authorization), and
    # the card renders the entries verbatim. Empty for every case
    # without a manifest.
    widening = _write_set_widening(state)

    # P1: confirm_required with --force-override → skip interrupt
    # A widened write-set contract is EXEMPT from this bypass: the flag's
    # charter is the same-action overlay exemption only. Widened payloads
    # keep one unified path — the interrupt below, where interactive
    # channels render the card and unattended channels take the audited
    # AUTO delegation (the manifest is the authority either way).
    if (
        safety_status == "confirm_required"
        and state.get("force_override")
        and not widening
    ):
        logger.info("confirm_required bypassed via --force-override")
        tracker.complete("Execution auto-approved via --force-override")
        sync_node_status_to_session(state, "confirmation_gate",
            "Auto-approved via --force-override",
            detail={"approved": True, "bypass": "force_override"})
        result = {
            "needs_confirmation": False,
            "approved_target": _freeze_from_state(state),
        }
        await sync_to_store(state, result)
        return result

    # Build the confirmation request.
    #
    # Field rationale (added beyond the original 5-key payload so the
    # TUI confirm card can surface what's already in state instead of
    # collapsing everything into safety_reason prose):
    #   · ``params``               — structured fault params (cpu %,
    #                                timeout, …); the plan_summary
    #                                markdown otherwise hides them.
    #   · ``target_health_report`` — DiskPressure / Evicted / Pending
    #                                pre-check; ``state.py`` comment
    #                                explicitly named confirm card as
    #                                the consumer but the surface was
    #                                missing.
    #   · ``conflict_uids``        — structured list (was already
    #                                embedded in safety_reason as
    #                                free text; structured form lets
    #                                the UI render a list + offer
    #                                /show experiments).
    #   · ``pipeline_attempt``     — N>1 means this is a re-attempt
    #                                after a previous failure; the UI
    #                                can surface "attempt N" so the
    #                                user knows.
    #   · ``is_complex``           — formal plan track flag.
    #   · ``plan_path``            — saved plan file path; UI can
    #                                show "Plan saved to xxx.md".
    #   · ``fault_intent``         — semantic classification from L1
    #                                (fault_type / scope / target /
    #                                action). The L2 confirm card
    #                                previously only had ``target``
    #                                (namespace + names) — operators
    #                                had to reverse-engineer "is this
    #                                a mem-load?" from ``params`` keys.
    #                                Surfacing the L1 classification
    #                                makes the fault category visible
    #                                at a glance without changing
    #                                anything else.
    fault_intent_brief = {
        "fault_type": spec.fault_type,    # derived: "{scope}-{target}-{action}"
        "scope":      spec.scope,
        "target":     spec.fault_target,  # blade "target" axis: cpu / mem / network / ...
        "action":     spec.fault_action,  # blade "action" axis: fullload / load / loss / ...
    } if spec and spec.fault_type else None

    confirmation_info = {
        "skill_name": skill_name,
        "fault_intent": fault_intent_brief,
        "target": target,
        # Human-facing summary first (finish_planning's summary, stored by
        # extract_planning_metadata); the head-of-plan slice is only a
        # fallback for paths that never produced one.
        "plan_summary": state.get("plan_summary") or (plan[:500] if plan else ""),
        "safety_status": safety_status,
        "safety_reason": state.get("safety_reason"),
        "safety_checked_detail": state.get("safety_checked_detail"),
        "params": dict(spec.params),
        # Duration contract: params no longer carry ``timeout``, so the
        # effective bound must be surfaced explicitly — this is the last
        # gate before execution and the operator must see how long the
        # fault will live.
        "duration_seconds": spec.duration_seconds,
        "target_health_report": state.get("target_health_report"),
        "conflict_uids": list(state.get("conflict_uids") or []),
        "pipeline_attempt": int(state.get("pipeline_attempt") or 0),
        "is_complex": bool(state.get("is_complex")),
        "plan_path": state.get("plan_path") or "",
        # E10 — multi-dimensional numeric safety score for confirm card
        # display. None when safety_check hasn't run (e.g. dry_run path).
        "safety_score": state.get("safety_score"),
        # E18 — injection feasibility report (headroom assessment).
        "feasibility_report": state.get("feasibility_report"),
        "plan_preview_markdown": _build_plan_preview_markdown(state),
    }

    # Case-level write-set contract, rendered VERBATIM on the card (the
    # case legislated these entries — not run-time plan prose). Absent
    # for cases without a manifest, so their card is unchanged.
    # ``widening`` itself was computed before the force_override branch.
    if widening:
        confirmation_info["mechanism_writes"] = widening
        # Marker for the unattended channels: an auto-approve that covers
        # a widened payload is an AUDITABLE delegation (the shared helper
        # routes the ``auto_approved`` event with these entries verbatim —
        # the manifest is the authority, the guard is the enforcement).
        # Interactive channels (TUI card, CLI confirm callback) ignore it
        # and decide through their own human-facing semantics.
        confirmation_info["write_set_widened"] = {
            "mechanism_writes": widening,
        }

    # P1: confirm_required without --force-override in CLI mode → reject
    # with guidance. A WIDENED contract is exempt from this short-circuit:
    # its rejection message ("Add --force-override") would be misleading —
    # under AUTO delegation the widened contract proceeds WITHOUT any
    # flag (the manifest is the authority; the runner emits the audited
    # auto-approve), so the short-circuit would add friction that solves
    # nothing. Widened payloads fall through to the interrupt, where the
    # unattended runner takes the audited delegation path and interactive
    # channels render the card.
    if (
        safety_status == "confirm_required"
        and state.get("interaction_mode") == "cli"
        and not widening
    ):
        safety_reason = state.get("safety_reason", "")
        logger.info("confirm_required rejected: no --force-override in CLI mode")
        tracker.fail("Execution rejected: --force-override required")
        sync_node_status_to_session(state, "confirmation_gate",
            "Rejected: --force-override required for same-action overlay",
            detail={"approved": False, "reason": "force_override_required"})
        result = {
            "safety_status": "rejected",
            "safety_reason": f"{safety_reason} Add --force-override to proceed.",
            "needs_confirmation": False,
            **fail_state(FailureCategory.SAFETY_REJECTED, f"confirm_required without --force-override; {safety_reason}"),
        }
        await sync_to_store(state, result)
        return result

    # Interrupt and wait for resume
    decision = interrupt(confirmation_info)

    if decision == "write_set_boundary":
        # Defence-in-depth, not a live path: since the AUTO-delegation
        # flip (2026-09-01) no shipped channel resumes with this signal
        # — ``unattended_resume_value`` always returns "approved". The
        # branch stays so a FUTURE channel that (mistakenly or by new
        # design) sends the boundary signal still terminates cleanly
        # with the dedicated category and machine-readable payload
        # instead of falling into an unmatched-decision limbo.
        logger.info(
            "write_set_boundary: run declined widened contract "
            "(%d entries beyond victim coverage)", len(widening),
        )
        tracker.fail("Unattended run: widened write-set contract declined")
        sync_node_status_to_session(state, "confirmation_gate",
            "Declined: mechanism writes beyond victim target (unattended)",
            detail={"approved": False, "write_set_boundary": True,
                    "entries": len(widening)},
        )
        result = write_set_boundary_result(state, widening)
        await sync_to_store(state, result)
        return result

    if decision == "approved":
        tracker.complete("Execution approved by user")
        sync_node_status_to_session(state, "confirmation_gate", "Execution approved",
            detail={"approved": True})
        # Freeze the approved target so execute_loop's screener can
        # compare every subsequent tool_call against this snapshot.
        # See chaos_agent.agent.target_guard for the policy.
        result = {
            "needs_confirmation": False,
            "approved_target": _freeze_from_state(state),
        }
        await sync_to_store(state, result)
        return result
    else:
        tracker.fail("Execution rejected by user")
        sync_node_status_to_session(state, "confirmation_gate", "Execution rejected",
            detail={"approved": False})
        planning_alternatives = state.get("_planning_alternatives", "")
        result = {
            "safety_status": "rejected",
            "safety_reason": "User rejected the execution",
            "needs_confirmation": False,
            **fail_state(FailureCategory.USER_REJECTED, "User rejected the execution at confirmation gate", alternatives=planning_alternatives),
            # Clear any stale approval — the next attempt will refreeze.
            "approved_target": None,
        }
        await sync_to_store(state, result)
        return result


def _freeze_from_state(state: AgentState) -> dict | None:
    """Convenience wrapper around ``freeze_approved_target`` that
    reads from the FaultSpec — the single source of truth. Returns
    None when no spec is on state (the caller should not be reaching
    this function in that case, but we default-deny to make the bug
    visible in the screener's WARNING log rather than silently
    constructing an empty approval).

    Reuses ``owner_names``, ``resolved_names``, ``pvc_claims``, the
    case-manifest ``mechanism_entries`` AND the case-file
    ``recovery_channel`` from the ``approved_target`` that safety_check
    already froze (avoiding a redundant cluster query and re-read of
    the case file — both were legislated at settlement and must
    survive re-freeze unchanged).

    This re-freeze is the SINGLE approval point for the widened
    contract: ``widening_pending_approval`` deliberately defaults to
    False here, so a knowing human's "approved" clears the pending
    marker safety_check stamped — the execute_loop sentinel then lets
    the run proceed.
    """
    spec = read_fault_spec(state)
    if spec is None:
        return None
    existing = state.get("approved_target") or {}
    owner_names = tuple(existing.get("owner_names") or ())
    resolved_names = tuple(existing.get("resolved_names") or ())
    pvc_claims = tuple(existing.get("pvc_claims") or ())
    mechanism_entries = entries_from_list(existing.get("mechanism_entries"))
    # The case-file recovery-route legislation survives re-freeze verbatim
    # (same rationale as the manifest entries: legislated at settlement,
    # orthogonal to whatever the human just approved).
    recovery_channel = str(existing.get("recovery_channel") or "")
    return freeze_approved_target_from_spec(
        spec, owner_names=owner_names, resolved_names=resolved_names,
        pvc_claims=pvc_claims, mechanism_entries=mechanism_entries,
        recovery_channel=recovery_channel,
        widening_pending_approval=False,
    )
