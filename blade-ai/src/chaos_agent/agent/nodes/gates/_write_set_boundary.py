"""Write-set boundary: the shared D4 surfaces.

D4 (openspec: write-set-approval-contract): a case whose manifest
carries entries beyond the victim target's coverage is a WIDENED
contract. Its AUTHORITY is the manifest itself — legislated before
the run, deterministically loaded, invisible to the LLM — and its
ENFORCEMENT is the target_guard's per-name union (fail-closed beyond
the manifest). What this module adds on top is VISIBILITY and
CONSISTENCY, not gatekeeping:

  - ``confirmation_gate`` — renders the manifest entries on the
    interactive card (TUI / ``--confirm``: a human is present, the
    entries are worth one glance) and clears the pending marker on
    approval, so the re-frozen snapshot is executable.
  - unattended channels (CLI streaming / non-streaming, HTTP SSE, L4
    pre_approved, and POST /api/v1/inject) — resume through
    :func:`unattended_resume_value` (AUTO delegation:
    always ``"approved"``) and emit an auditable ``auto_approved``
    event when the delegation covers a widened contract
    (:func:`widened_auto_approval_payload`; event on channels that have
    a stream, audit-log line on those that do not).
  - ``execute_loop`` — the entry sentinel audits a still-pending
    snapshot (no approval path ran — a channel bug or an unwired
    seam) with an ERROR log, then lets execution proceed: the guard
    is the enforcement boundary, and fail-closing here would trade
    real automation for no real safety.
"""

from __future__ import annotations

import logging

from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.target_guard.freeze import approved_from_dict
from chaos_agent.agent.target_guard.mechanism_writes import (
    entries_beyond_victim,
)
from chaos_agent.agent.result.verdict import FailureCategory

logger = logging.getLogger(__name__)


def widened_auto_approval_payload(interrupt_info) -> dict | None:
    """The widened-contract payload when an unattended auto-approve covers it.

    Channels consult this before resuming so the delegation is AUDITABLE:
    a widened auto-approval emits an ``auto_approved`` event carrying the
    full interrupt payload (the manifest entries ride it verbatim), the
    same read-only rendering every interactive card uses. Returns ``None``
    for ordinary payloads — no event, no noise.
    """
    if isinstance(interrupt_info, dict) and interrupt_info.get("write_set_widened"):
        return interrupt_info
    return None


def unattended_resume_value(interrupt_info) -> str:
    """Resume signal for an unattended confirmation_gate auto-approve.

    AUTO delegation semantics (established product design; the interim
    fail-closed "write_set_boundary" resume was an over-reach reverted
    2026-09-01): the write-set contract's AUTHORITY is the case manifest
    — legislated before the run, loaded deterministically by code the
    LLM cannot influence — and its ENFORCEMENT is the guard's per-name
    union (fail-closed beyond the manifest). Per-run human approval
    adds no authority an operator can meaningfully re-adjudicate on the
    Nth run of an unchanged manifest; approval fatigue degrades it to a
    rubber-stamp. Unattended channels therefore proceed: resume
    ``"approved"`` for every payload, widened or not. Interactive
    channels (TUI / ``--confirm``) still render the card because a
    human is present there and the entries are worth one glance.

    Auto-approving a WIDENED payload is an auditable event: callers
    emit ``auto_approved`` with the verbatim payload (see
    :func:`widened_auto_approval_payload`) so the delegation stays on
    the record.
    """
    return "approved"


def snapshot_widening_pending(state: dict) -> bool:
    """Does the frozen snapshot still await its knowing human?

    Reads ``approved_target.widening_pending_approval`` — set by
    safety_check at freeze time when the manifest widens the contract,
    cleared by the gate's approved re-freeze (and the human-approved
    drift-correction rebuild). Snapshots from before this marker
    existed (legacy checkpoints) hydrate as False and keep their
    recorded semantics.
    """
    approved = approved_from_dict((state or {}).get("approved_target") or {})
    return bool(approved is not None and approved.widening_pending_approval)


def write_set_boundary_result(state: dict, widening: list[dict]) -> dict:
    """Terminal state for the widened-contract boundary exit.

    Shared by the gate's ``write_set_boundary`` resume branch and the
    execute_loop entry sentinel so both exits produce byte-identical
    state: rejected safety status, the machine-readable payload, the
    dedicated failure category, and NO approved target (the next
    attempt re-freezes from scratch).

    Machine-readable: the manifest entries verbatim, a summary of the
    approved (victim) target, and guidance naming the interactive
    channel. No cluster mutation happened before this point.
    """
    spec = read_fault_spec(state) or FaultSpec()
    payload = {
        "type": "write_set_boundary",
        "mechanism_writes": widening,
        "approved_target": {
            "scope": spec.scope,
            "namespace": spec.namespace,
            "names": list(spec.names),
            "labels": dict(spec.labels),
        },
        "guidance": (
            "This case's write-set contract is wider than its victim target "
            "(case manifest mechanism_writes). Re-run the same intent in an "
            "interactive session — TUI, or a CLI session with --confirm — "
            "where the confirmation card renders these entries verbatim and "
            "human approval freezes the extended contract. There is no "
            "unattended re-submission channel."
        ),
    }
    entries_desc = "; ".join(
        f"{e.get('scope')}/{e.get('namespace') or '<cluster>'}: "
        f"{e.get('names') or [chr(39) + e.get('name_prefix', '') + chr(39) + '*']}"
        for e in widening
    )
    return {
        "safety_status": "rejected",
        "safety_reason": (
            "Unattended run declined to auto-approve a widened write-set "
            f"contract: {entries_desc}. {payload['guidance']}"
        ),
        "needs_confirmation": False,
        "write_set_boundary": payload,
        **fail_state(
            FailureCategory.WRITE_SET_BOUNDARY,
            f"unattended CLI cannot auto-approve mechanism writes beyond the "
            f"victim target ({len(widening)} manifest entr"
            f"{'y' if len(widening) == 1 else 'ies'} beyond coverage); "
            f"re-run interactively to approve the case contract",
        ),
        "approved_target": None,
    }


def execute_loop_entry_sentinel(state: dict) -> None:
    """Execution-boundary consistency assertion (audit, not a gate).

    A snapshot that still carries ``widening_pending_approval`` at
    execution time means NO approval path ran for it — a channel bug,
    a raw state write, or a replan seam not yet wired to the gate.
    Under AUTO delegation semantics this is not a safety violation:
    the manifest is the authority and the guard enforces the boundary
    per-name (fail-closed beyond the manifest), so the pending flag
    only records that the interactive card never fired. The sentinel
    therefore AUDITS loudly (ERROR log — someone should look at the
    path) and lets execution proceed: fail-closing here would kill
    legitimate unattended runs through any seam we failed to
    enumerate, trading real automation for no real safety (the guard
    is the boundary). Callers wire this at the very top of the
    execute_loop node body, before any LLM call or tool dispatch.
    """
    approved = approved_from_dict((state or {}).get("approved_target") or {})
    if approved is None or not approved.widening_pending_approval:
        return
    beyond = entries_beyond_victim(approved)
    logger.error(
        "write_set_boundary sentinel (audit): execute_loop reached with a "
        "widened contract still pending approval (%d entries beyond victim "
        "coverage) — no approval path ran for this snapshot. Proceeding "
        "(the guard enforces the manifest boundary); investigate the path "
        "that reached execution without a gate decision",
        len(beyond),
    )


__all__ = [
    "execute_loop_entry_sentinel",
    "snapshot_widening_pending",
    "unattended_resume_value",
    "widened_auto_approval_payload",
    "write_set_boundary_result",
]
