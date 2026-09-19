"""finalize_verification node (Scheme B).

The verifier ReAct loop (``verifier_loop``) is now a pure LLM step: it
gathers evidence and, when done, calls ``submit_verification``. That call
runs through the ToolNode, then ``route_after_verifier_tools`` sends control
here. This node:

  1. Reads the verdict — from ``submit_verification`` args (preferred) or,
     as a fallback, by parsing the last AIMessage's free text.
  2. Runs ALL post-processing that used to live in verifier_loop's
     no-tool_calls branch: evidence cross-check, programmatic enforcement
     (disk-burn), step coverage, P2 verification-integrity gaps
     (re-verification), and baseline enforcement.
  3. On gaps with remaining budget → re-prompts and routes back to
     verifier_loop (``route_after_finalize`` keys on ``verification`` being
     unset). Otherwise sets ``verification`` → ``se_detect``.
  4. Cleans up debug pods (moved here from verifier_loop; dedup preserved).

Why a separate node (vs finishing inside verifier_loop): the verdict comes
from a tool call that must pass through the ToolNode for a well-formed
ToolMessage, and post-processing must run AFTER that — mirroring how
``extract_planning_metadata`` finalizes Phase 1 after ``finish_planning``.
"""

import logging
import re

from langchain_core.messages import HumanMessage, ToolMessage

from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.evidence import EvidenceProfile, host_evidence_supplements
from chaos_agent.agent.replan import ReplanRequest
from chaos_agent.agent.providers.base import DestroyOutcome
from chaos_agent.transports import PROFILE_HOST, profile_of, resolve_channel_name
from chaos_agent.agent.node_names import FINALIZE_VERIFICATION
from chaos_agent.agent.prompts.reminder import wrap_system_reminder
from chaos_agent.agent.result.operation_outcome import write_inject_verification
from chaos_agent.agent.result.verdict import (
    CHECKLIST_BENIGN_STATUSES,
    CHECKLIST_NON_PASSED_STATUSES,
    CHECKLIST_STATUS_VALUES,
    INJECT_VERDICT_VALUES,
    LAYER2_DEGRADED_STATUSES,
    LAYER2_STATUS_VALUES,
    layer1_to_dict,
    Layer1Result,
)
from chaos_agent.agent.nodes.execute._debug_pod import parse_debug_pod_info, delete_debug_pod
from chaos_agent.agent.nodes.execute._kubeconfig_inject import _resolve_kubeconfig, sync_kubewiz_runtime
from chaos_agent.agent.nodes.store._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.nodes.verify._deterministic_rules import (
    DeterministicVerdict,
    RuleContext,
)
from chaos_agent.agent.nodes.verify._verification_profiles import (
    resolve_deterministic_rules,
)
from chaos_agent.agent.nodes.verify._verifier_layer1 import _restore_layer1_from_state
from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
    _collect_evidence_text,
    _count_verification_steps_in_skill_case,
    _detect_checklist_conclusion_inconsistency,
    _extract_verification_step_descriptions,
    _parse_verification_result,
    _split_candidates,
    _try_parse_json,
    _validate_step_number_coverage,
    cross_check_evidence,
)
from chaos_agent.agent.nodes.verify._verifier_shared import (
    _compute_baseline_confidence,
    extract_submit_args,
    last_ai_text,
)
from chaos_agent.agent.nodes.verify._verifier_submit import SUBMIT_VERIFICATION_TOOL_NAME
from chaos_agent.agent.execution_artifacts import cleanup_debug_pod_artifacts
from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.config.settings import settings
from chaos_agent.memory.session_store import get_global_session_store
from chaos_agent.agent.state import AgentState, materialize_fault_handle
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory

# Backward-compat aliases
_parse_debug_pod_info = parse_debug_pod_info
_delete_debug_pod = delete_debug_pod

logger = logging.getLogger(__name__)


async def _cleanup_debug_pods(
    state: AgentState,
    kubeconfig: str,
    task_id: str,
    result_update: dict,
) -> None:
    """Programmatic debug-pod cleanup with cross-reentry dedup.

    Scans the message history for ``kubectl debug node/...`` pods created
    by the LLM, extracts both pod name and namespace from the ToolMessage
    content. Diffs against ``state.cleaned_debug_pods`` (pods we've already
    attempted to delete in earlier verifier re-entries), deletes only the
    new ones, and writes the merged set back into ``result_update`` so the
    next re-entry sees them as already-handled.
    """
    tracked_artifacts, artifact_cleaned = await cleanup_debug_pod_artifacts(
        state.get("execution_artifacts"),
        kubeconfig=kubeconfig,
        task_id=task_id,
    )
    if tracked_artifacts != (state.get("execution_artifacts") or []):
        result_update["execution_artifacts"] = tracked_artifacts

    # Legacy discovery keeps old tasks (without execution_artifacts) cleanable.
    # discovered: pod_name -> namespace
    discovered_pods: dict[str, str] = {}
    for msg in state.get("messages", []):
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") in ("kubectl", "kubectl_read"):
            msg_content = msg.content if isinstance(msg.content, str) else str(msg.content)
            pod_name, ns, tool_cleaned = _parse_debug_pod_info(msg_content)
            # Skip pods the kubectl tool already auto-removed: a one-shot
            # ``kubectl_read debug`` pod is deleted by the tool itself (meta
            # ``cleaned: true``) and never enters the artifact registry
            # (collect only indexes the full ``kubectl`` tool, and only
            # during execute) — deleting it again is a guaranteed NotFound
            # (#31: 8 redundant deletes here + 10 more on recover re-entry).
            if pod_name and not tool_cleaned:
                discovered_pods[pod_name] = ns
    already_cleaned: set[str] = set(state.get("cleaned_debug_pods") or [])
    already_cleaned.update(artifact_cleaned)
    # Artifacts are authoritative for new tasks. In particular, a
    # ``recovery_armed`` carrier must stay alive until its node-local rollback
    # timer expires. The legacy message scan is only for old, untracked pods.
    # The exclusion set covers EVERY vehicle artifact type (not just
    # debug_pod): ``parse_debug_pod_name``'s generic ``pod/<name> created``
    # pattern also matches a recovery carrier's creation banner, so a
    # carrier would otherwise be force-deleted here with NO armed gate —
    # killing its in-flight recovery timer (run4 live fire: carrier
    # force-deleted 3 minutes into a 600s window, fault left unrecovered).
    from chaos_agent.agent.execution_artifacts import VEHICLE_ARTIFACT_TYPES
    tracked_names = {
        str(artifact.get("name") or "")
        for artifact in tracked_artifacts
        if isinstance(artifact, dict)
        and artifact.get("type") in VEHICLE_ARTIFACT_TYPES
    }
    pods_to_delete = (
        set(discovered_pods.keys()) - already_cleaned - tracked_names
    )
    for pod_name in pods_to_delete:
        ns = discovered_pods[pod_name]
        logger.info(f"Programmatic cleanup: deleting debug pod {pod_name} in namespace {ns}")
        await _delete_debug_pod(pod_name, kubeconfig, task_id, namespace=ns)
    if pods_to_delete or artifact_cleaned:
        result_update["cleaned_debug_pods"] = sorted(already_cleaned | pods_to_delete)


def _overall_to_level(overall: str) -> str:
    """Map submit_verification's ``overall`` to the internal ``level``.

    Accepts exactly InjectVerdict's members (derived, B76 round-14 —
    no hand-copied word list); anything else falls back fail-closed.
    """
    return overall if overall in INJECT_VERDICT_VALUES else "unverified"


def _verification_from_submit_args(args: dict) -> dict:
    """Build a verification dict from submit_verification tool-call args.

    Produces the SAME shape ``_parse_verification_result`` / ``_try_parse_json``
    yield, so all downstream post-processing is source-agnostic. Also runs the
    checklist↔conclusion inconsistency check (mirrors the JSON-mode path).
    """
    checklist = args.get("checklist") or []
    if not isinstance(checklist, list):
        checklist = []
    raw_l2_status = args.get("layer2_status", "unknown")
    overall = args.get("overall", "unverified")
    warnings = list(args.get("warnings") or [])
    # Closed sets derive from the legislation enums (B76 round-14
    # root-cause fix): out-of-set layer2 claims clamp to 'unknown' and
    # stay visible instead of flowing verbatim into the task JSON.
    if raw_l2_status in LAYER2_STATUS_VALUES:
        l2_status = raw_l2_status
    else:
        l2_status = "unknown"
        warnings.append(
            f"Layer2 status '{raw_l2_status}' is outside the closed vocabulary; "
            "recorded as 'unknown'."
        )

    result = {
        "level": _overall_to_level(overall),
        "layer1": {"status": "unknown", "details": ""},  # overwritten by code later
        "layer2": {"status": l2_status, "details": args.get("layer2_details", "")},
        "warnings": warnings,
        "overall": overall,
        "primary_evidence_observed": bool(args.get("primary_evidence_observed", False)),
        "baseline_used": bool(args.get("baseline_used", False)),
    }
    if checklist:
        # Guard: LLM may pass non-dict items (e.g. plain strings); filter to
        # dicts only to prevent AttributeError in downstream .get() calls.
        checklist = [c for c in checklist if isinstance(c, dict)]
        result["checklist"] = {
            "items": checklist,
            "skipped_count": sum(1 for c in checklist if c.get("status") == "skipped"),
            # Fail-closed counting (B76 round-14 F3): an item counts as
            # non-passed unless its status is in the benign set — a
            # closed-set-outside word is not a pass claim.
            "non_passed_count": sum(
                1 for c in checklist
                if c.get("status") not in CHECKLIST_BENIGN_STATUSES
            ),
            "total_count": len(checklist),
            "total_executed": len(checklist),
        }
        _outside = sorted({
            c.get("status") for c in checklist
            if c.get("status") not in CHECKLIST_STATUS_VALUES
        })
        if _outside:
            result["warnings"].append(
                f"Checklist item statuses outside the closed vocabulary: {_outside}."
            )
        if l2_status == "passed":
            _non_passed_ev = " ".join(
                c.get("evidence", "") for c in checklist
                if isinstance(c, dict) and c.get("status") in CHECKLIST_NON_PASSED_STATUSES
            )
            inc_warning, should_downgrade = _detect_checklist_conclusion_inconsistency(
                checklist, l2_status, _non_passed_ev,
            )
            if inc_warning:
                result["warnings"].append(inc_warning)
                if should_downgrade:
                    result["layer2"]["status"] = "partial"

    # PrimaryEvidenceObserved hard constraint: verified requires it.
    if result["level"] == "verified" and not result["primary_evidence_observed"]:
        result["level"] = "partial"
        result["warnings"].append(
            "Verdict 'verified' is incompatible with PrimaryEvidenceObserved=false. "
            "Downgraded to 'partial'."
        )
    # Level sync: layer2 status must be consistent with overall level.
    # 'failed' layer2 is incompatible with 'verified' level (fault effect absent).
    if result["layer2"]["status"] == "failed" and result["level"] == "verified":
        result["level"] = "unverified"
        result["warnings"].append(
            "Verdict 'verified' is incompatible with Layer2='failed' (fault effect not observed). "
            "Downgraded to 'unverified'."
        )
    if result["layer2"]["status"] == "partial" and result["level"] in ("verified", "unverified"):
        result["level"] = "partial"
    return result


def _extract_submit_args(messages: list) -> dict | None:
    """Return the args of the most recent submit_verification tool_call, or None."""
    return extract_submit_args(
        messages,
        tool_name=SUBMIT_VERIFICATION_TOOL_NAME,
        guard_markers=("Verification gaps", "re-verification"),
    )


_last_ai_text = last_ai_text


def _format_verification_detail(verification: dict, layer1) -> str:
    """Format verification verdict as readable text for TUI display."""
    level = verification.get("level", "unknown")
    l2 = verification.get("layer2", {})
    l2_status = l2.get("status", "unknown") if isinstance(l2, dict) else "unknown"
    l2_details = l2.get("details", "") if isinstance(l2, dict) else ""
    checklist = verification.get("checklist", {})
    items = checklist.get("items", []) if isinstance(checklist, dict) else []
    warnings = verification.get("warnings", [])

    icon_map = {"passed": "✓", "failed": "✗", "partial": "◐",
                "skipped": "○", "recovered_before_observation": "◇",
                "expected": "◌", "not_applicable": "–"}
    # Three-way glyph, matching the batch summary (✓ / ? / ✗): "unverified"
    # is honest ignorance — the observation channel was unavailable — so it
    # keeps "?"; ✗ would translate "cannot tell" back into "failed", the
    # very conflation this vocabulary exists to prevent.
    level_icon = {"verified": "✓", "partial": "◐", "unverified": "?"}.get(level, "·")

    lines = [f"{level_icon} Verification: {level} (Layer1: {layer1.status.value}, Layer2: {l2_status})"]

    if l2_details:
        lines.append(f"  {l2_details}")

    if items:
        lines.append("")
        for item in items:
            if not isinstance(item, dict):
                continue
            step = item.get("step", "?")
            st = item.get("status", "?")
            ev = item.get("evidence", "")
            icon = icon_map.get(st, "·")
            lines.append(f"  {icon} Step {step}: {st} — {ev}")

    if warnings:
        lines.append("")
        for w in warnings:
            lines.append(f"  ⚠ {w}")

    return "\n".join(lines)


async def _cleanup_residuals(state: AgentState, kubeconfig: str) -> list[dict]:
    """Clean up residual side effects from the previous injection attempt.

    Checks state for known residual types and cleans them up deterministically.
    Returns a list of cleaned-up artifacts for replan context.

    Dispatch (round-31): the experiment-residual cleanup rides the
    registry's carrier-neutral liability SWEEP — the same plural
    settlement primitive the plan-change seam and the recover finale
    legislated — because this seam's own rationale (a live residual
    experiment pollutes the replan's fresh verification) is the
    plan-change seam's rationale verbatim, and the singular claim
    destroy it replaced proved domain-misaligned (round-31 R6''): the
    claim layer is COMMITTED (no death filter — the r25 finding), so
    with the slot's first birth dead and a sibling live, the cleanup
    dispatched at the corpse while the live sibling survived into the
    replan — the fresh injection then verified against TWO stacked
    faults. The sweep judges the live liability SET and destroys every
    member: a corpse never dispatches (NOT_FOUND waste gone), and a
    failure renders an honest FAILED artifact for the replan context
    instead of vanishing. For ``kubectl_native`` injections the owned
    set is empty and the sweep is a no-op — the replan is expected to
    produce a different injection method that overwrites the residual.
    Users can manually recover via ``blade-ai recover`` if needed.
    """
    cleaned: list[dict] = []

    from chaos_agent.agent.providers import FaultProviderRegistry

    # Round-31 merged the caller's resolved kubeconfig in because the
    # sweep then read the state key bare; round-32 moved the three-level
    # fallback (state > spec > settings) INSIDE the sweep, so this merge
    # is now idempotent belt-and-suspenders — kept so the seam's own
    # resolution stays visible at the call site (a non-empty state value
    # short-circuits the resolver unchanged).
    values = {**dict(state or {}), "kubeconfig": kubeconfig}
    retired_new, failures = (
        await FaultProviderRegistry.sweep_live_liabilities(values)
    )
    for uid in retired_new:
        cleaned.append({
            "type": "running_experiment",
            "id": uid,
            "cleanup_result": "destroyed (liability sweep)",
            "cleanup_outcome": DestroyOutcome.SUCCESS.value,
        })
        logger.info(
            "Verify-replan cleanup: destroyed experiment %s", uid,
        )
    for line in failures:
        # Sweep failures are "uid: reason" lines — split once so the
        # artifact keeps a uid-shaped id (the replan context renders it)
        # while the reason rides the result field verbatim.
        uid, _, reason = line.partition(": ")
        cleaned.append({
            "type": "running_experiment",
            "id": uid,
            "cleanup_result": f"failed: {reason or line}",
            "cleanup_outcome": DestroyOutcome.FAILED.value,
        })
        logger.warning(
            "Verify-replan cleanup: failed for %s: %s", uid, reason or line,
        )

    return cleaned


def _retired_uids_from_residuals(residuals_cleaned: list[dict]) -> list[str]:
    """UIDs that verify-replan cleanup actually destroyed.

    Retire gate composes the single destroy-decision source: only a
    cleanup whose recorded outcome is SUCCESS retires (the pre-merge
    prefix table retired ANY output not starting ``failed``/``Error:`` —
    an empty or garbage output silently retired too). A failed destroy
    (exception -> ``failed: ...``, or a soft tool failure -> ``Error: ...``
    — the tool returns the error string instead of raising) may leave a
    live experiment that we must keep tracking, not hide behind
    retirement.
    """
    return [
        r["id"] for r in residuals_cleaned
        if r.get("type") == "running_experiment"
        and r.get("id")
        and str(r.get("cleanup_outcome", "")) == DestroyOutcome.SUCCESS.value
    ]


def _verify_replan_eligible(verification: dict) -> bool:
    """Whether an unverified verdict may re-enter Phase 1 for re-planning.

    Gate on the verdict alone: unverified + Layer 2 failed. Budget gating
    stays at the call site.
    """
    return (
        verification.get("level", "") == "unverified"
        and verification.get("layer2", {}).get("status", "unknown") == "failed"
    )


def _build_verify_replan_context(
    verification: dict,
    residuals_cleaned: list[dict],
    verify_replan_count: int,
    skill_name: str,
    messages: list | None = None,
) -> dict:
    """Build replan context for verifier-triggered replan."""
    l1 = verification.get("layer1", {})
    l2 = verification.get("layer2", {})
    checklist = verification.get("checklist", {})
    items = checklist.get("items", []) if isinstance(checklist, dict) else []

    # Collect evidence from failed checklist items
    failed_evidence = []
    for item in items:
        if isinstance(item, dict) and item.get("status") == "failed":
            failed_evidence.append(
                f"Step {item.get('step', '?')}: {item.get('evidence', '')}"
            )

    # Build residuals description for Phase 1
    residuals_desc = []
    for r in residuals_cleaned:
        residuals_desc.append(
            f"- {r['type']} (id={r['id']}): {r['cleanup_result']}"
        )

    invalidated_assumption = (
            f"Injection executed successfully (L1={l1.get('status', 'unknown')}) "
            f"but verification found the fault effect was NOT observed "
            f"(L2={l2.get('status', 'unknown')}). "
            f"The injection method did not produce the expected fault effect."
    )
    request = ReplanRequest(
        kind="verification",
        decision="plan_invalid",
        invalidated_assumption=invalidated_assumption,
        observed_evidence=failed_evidence or [
            f"Layer1={l1.get('status', 'unknown')}",
            f"Layer2={l2.get('status', 'unknown')}",
        ],
        evidence_refs=[],
        affected_step="post-injection verification",
        unresolved_questions=[
            "Which available method can produce the approved effect on this target?"
        ],
        changes_target_or_risk=False,
    )

    # Guard rejections are NOT verify-specific knowledge: the verifier's own
    # probes can be rejected by target_guard (r4 task inject-5552c6e4
    # msg[231] — a verifier probe hit REJECT_DRIFT and this replan branch
    # fired), and a form-level rejection is the same never-relaxing boundary
    # here as on the execute path. Same collector, same contract-relative
    # boundary, same form-level filter — one seam, both replan branches
    # (B76 review C1: this branch was previously blind, so the optimistic
    # re-planning pathway the hard-constraint section exists to close stayed
    # open exactly where the original deadlock actually happened).
    # Lazy import: execute_loop already imports this package's
    # _verifier_messages, so a module-level import would be circular.
    from chaos_agent.agent.nodes.execute.execute_loop import (
        _collect_guard_rejections,
    )

    return {
        "error_summary": invalidated_assumption,
        **request.as_context(),
        "iteration_at_failure": verify_replan_count + 1,
        "failed_tool_calls": [],  # No tool failure — tool succeeded but effect absent
        "rejected_params": [],
        "failed_tool_names": [],
        "guard_rejections": _collect_guard_rejections(messages or []),
        "trigger": "verify_replan",
        "verifier_findings": {
            "level": verification.get("level", ""),
            "layer1_status": l1.get("status", ""),
            "layer1_details": l1.get("details", ""),
            "layer2_status": l2.get("status", ""),
            "layer2_details": l2.get("details", ""),
            "failed_evidence": failed_evidence,
            "warnings": verification.get("warnings", []),
        },
        "residuals_cleaned": residuals_cleaned,
        "residuals_description": "\n".join(residuals_desc) if residuals_desc else "None",
        "skill_name": skill_name,
        "suggestion": (
            "The previous injection method executed successfully but the fault "
            "effect was not observed. Try an alternative injection method from "
            "the skill case. Residual side effects from the previous attempt "
            "have been cleaned up."
        ),
    }


def _record_evidence_text(record: object) -> str:
    """Flatten a verification record's command/description/stdout for matching."""
    if isinstance(record, dict):
        parts = [
            str(record.get(k, ""))
            for k in ("description", "command", "stdout", "evidence")
        ]
        return " ".join(p for p in parts if p)
    return str(record or "")


async def _supplement_host_verification_evidence(
    spec,
    missing: set[str],
    existing_records: list,
    state: dict,
) -> list[dict]:
    """Run cheap read-only host probes to close evidence-coverage gaps.

    A fast, strong fault (e.g. CPU fullload) legitimately concludes on the
    first observation, but the LLM's metric probes (``vmstat`` / ``top``)
    rarely include host identity — leaving ``target_identity`` uncovered even
    though the verdict is sound. This mirrors the baseline-side supplement:
    deterministically anchor identity + an independent cross-metric so
    post-injection coverage is complete WITHOUT forcing the verifier to loop.

    Best-effort: any probe failure is swallowed (the advisory coverage warning
    still fires) and never changes the verdict.
    """
    from chaos_agent.transports import (
        PROFILE_HOST,
        TransportTarget,
        execute_via_transport,
    )

    existing_text = " ".join(
        _record_evidence_text(r) for r in existing_records
    ).lower()
    probes = host_evidence_supplements(
        spec.fault_target if spec else "", missing, existing_text,
    )
    if not probes:
        return []

    target = TransportTarget.from_state(dict(state))
    _task_id = dict(state).get("task_id", "")
    records: list[dict] = []
    for description, argv in probes:
        try:
            res = await execute_via_transport(
                list(argv), target, timeout=10, task_id=_task_id,
                source="verify-evidence-supplement", skip_guard=True,
                # These probes exist to ANCHOR HOST IDENTITY in the verdict's
                # evidence. On a cluster-addressing channel they would anchor
                # the platform executor's identity instead — the wrong machine
                # recorded as proof (task-46317228).
                expect_profile=PROFILE_HOST,
            )
        except Exception as exc:  # best-effort; never fail the verdict
            logger.debug("verify evidence supplement failed for %s: %s", argv, exc)
            continue
        if res.exit_code == 0 and res.stdout:
            records.append({
                "description": description,
                "command": " ".join(argv),
                "stdout": res.stdout.strip(),
            })
    return records


def _lift_verdict_to_passed(
    verification: dict, verdict: DeterministicVerdict,
) -> bool:
    """Quadrant-2 body: lift every degraded verdict the LLM emitted.

    Lift semantics (inherited from the disk_burn precedent — design
    decision 3, quadrant "program passed × LLM degraded"):
      * checklist items in a degraded status (failed /
        recovered_before_observation / partial) flip to ``passed`` with
        ``[OVERRIDE]`` evidence;
      * a degraded Layer 2 lifts to ``passed`` with an override note —
        INDEPENDENT of the checklist (an L2 downgrade with an all-green
        checklist is still a degraded verdict the program evidence
        overrides);
      * warnings record what was overridden and why; the level
        re-derives from the lifted state;
      * an already-green verdict (nothing to lift) returns False — the
        caller then applies the quadrant-1 dual-source annotation.

    Mutates ``verification`` in place; returns whether a lift happened.
    """
    evidence_main = (
        verdict.checklist_subject
        or " ".join(verdict.evidence_lines)
        or f"Deterministic rule '{verdict.rule_name}' passed."
    )
    l2_subject = (
        verdict.layer2_subject
        or f"Deterministic rule '{verdict.rule_name}' confirmed the fault effect."
    )
    warn_subject = (
        verdict.warning_subject
        or f"deterministic rule '{verdict.rule_name}' confirmed the fault effect"
    )

    _flipped = 0
    for _ci in verification.get("checklist", {}).get("items", []):
        if _ci.get("status") in CHECKLIST_NON_PASSED_STATUSES:
            _ci["status"] = "passed"
            _ci["evidence"] = (
                f"[OVERRIDE] {evidence_main} "
                f"Fault is still in effect — LLM observation was insufficient, "
                f"not evidence of recovery."
            )
            _flipped += 1

    _l2_val = verification.get("layer2", {}).get("status", "unknown")
    _l2_degraded = _l2_val in LAYER2_DEGRADED_STATUSES
    if not _flipped and not _l2_degraded:
        return False  # all green — quadrant 1 (dual-source annotation)

    if _flipped:
        logger.info(
            "Programmatic enforcement: deterministic rule '%s' passed — "
            "overriding degraded LLM checklist verdicts.",
            verdict.rule_name,
        )

    if _l2_degraded:
        verification["layer2"]["status"] = "passed"
        verification["layer2"]["details"] = f"{l2_subject} LLM conclusion overridden."
        _l2_desc = (
            "the fault was absent" if _l2_val == "failed"
            else "the fault effect had already dissipated before observation"
            if _l2_val == "recovered_before_observation"
            else "the fault effect was only partially confirmed"
        )
        verification.setdefault("warnings", []).append(
            f"Programmatic override: {warn_subject}, but LLM concluded "
            f"{_l2_desc} (original status: '{_l2_val}')."
        )
    else:
        verification.setdefault("warnings", []).append(
            f"Programmatic override: {warn_subject} "
            f"(LLM Layer2 concluded '{_l2_val}'; override applied to checklist steps only)."
        )

    _all_items = verification.get("checklist", {}).get("items", [])
    _cl_meta = verification.get("checklist") or {}
    if isinstance(_cl_meta, dict) and "non_passed_count" in _cl_meta:
        # Keep the derived count honest: the flip above turned degraded
        # items green, and a stale non_passed_count would contradict the
        # items at the structured-result boundary (dict_to_verification_result
        # carries the field verbatim).
        _cl_meta["non_passed_count"] = sum(
            1 for _ci in _all_items
            if _ci.get("status") not in CHECKLIST_BENIGN_STATUSES
        )
    if _all_items:
        _remaining_bad = sum(
            1 for _ci in _all_items
            if _ci.get("status") not in CHECKLIST_BENIGN_STATUSES
        )
        if _remaining_bad == 0 and verification.get("layer2", {}).get("status") == "passed":
            verification["level"] = "verified"
        elif verification.get("layer2", {}).get("status") == "passed" and _remaining_bad > 0:
            verification["level"] = "partial"
    elif verification.get("layer2", {}).get("status") == "passed":
        # No checklist items were ever emitted — the lifted L2 is the
        # whole verdict, and leaving a stale 'unverified'/'partial'
        # level next to a passed L2 is an inconsistent state (spec:
        # a counter-proof-free downgrade is lifted to passed; re-audit
        # finding 3).
        verification["level"] = "verified"
    return True


# A replacement signal in the LLM's evidence — the fault object the rule
# measured was itself replaced (new container/pod ID), so the rule's
# numbers may describe a stale object. Wording variants seen in live runs
# ("container ID ... changed", "new container id", "pod was re-created").
# Structured as noun→short-window→core-verb co-occurrence, NOT an
# enumerated auxiliary-verb list: the list form missed "have been" on its
# own re-audit (fix-of-fix on finding 8) and would keep missing "got /
# might be" — ANY auxiliary must work. Negation handling is two-layered:
# a "no" directly before the adjective-first arm ("no new container id"
# is a CONTINUITY statement — the green case) via lookbehind, and
# not/never/no inside the matched window via code check. Miss direction
# is the dangerous one (a real counter-proof slipping past the gate
# flips an honest degradation), so coverage errs wide; a hit is only
# discarded when an explicit negation sits inside the match itself.
_COUNTER_EVIDENCE_REPLACEMENT_RE = re.compile(
    r"(?<!no\s)(?:new|replaced|re-?created|different|stale)\s+"
    r"(?:container|pod)\s*ids?"
    r"|container\s*ids?[^.\n]{0,20}?(?:changed|replaced|differs?)"
    r"|(?:container|pod)s?[^.\n]{0,40}?(?:re-?created|replaced)",
    re.IGNORECASE,
)
_NEGATION_IN_MATCH_RE = re.compile(r"\b(?:not|never|no)\b", re.IGNORECASE)


def _counter_evidence(
    verification: dict, *, scan_replacement: bool = True,
) -> str | None:
    """Detect a SPECIFIC counter-proof in the LLM's evidence that the
    deterministic rule does not cover (design decision 3, quadrant-2 gate).

    Two programmatic forms, per the Open-Question adjudication:
      1. a cross_check numeric contradiction already landed in warnings —
         the LLM cited numbers the observation timeline disproves
         (hallucinated deltas ARE counter-proof, not mere absence).
         ALWAYS scanned: a numeric contradiction refutes any rule;
      2. a replacement signal in the evidence text — the measured object
         was replaced, so the rule's numbers may describe a stale object.
         SKIPPED for rules whose own criteria include the replacement
         (``treats_replacement_as_effect`` — process kill: a changed
         container ID is the effect, and an honest green LLM cites it;
         the gate would otherwise misfire on nearly every kill run;
         re-audit finding 1).

    Deliberately NOT counter-proof: mere restatement of numbers, hedging
    phrases ("might have recovered"), or silence — those are absence,
    which is the LLM's territory, not refutation.
    """
    for _w in verification.get("warnings") or []:
        if "but observation timeline shows no change" in _w:
            return f"numeric contradiction (cross-check): {_w}"
    if scan_replacement:
        # Programmatic rows ([OVERRIDE] / [DETERMINISTIC] prefixes) are
        # the pipeline's own words, not the LLM's — e.g. the kill rule's
        # override text names "container ID replaced" (its own criteria).
        # Scanning them would let the program refute itself: via the
        # multi-rule loop order (a later rule sees an earlier rule's
        # applied text) or via an LLM echoing a prior turn's override
        # wording into its new checklist. The gate scans the LLM's words.
        _llm_text = "\n".join(
            _line for _line in _collect_evidence_text(verification).splitlines()
            if not _line.lstrip().startswith(("[OVERRIDE]", "[DETERMINISTIC]"))
        )
        _m = _COUNTER_EVIDENCE_REPLACEMENT_RE.search(_llm_text)
        if _m and not _NEGATION_IN_MATCH_RE.search(_m.group(0)):
            return f"replacement signal in evidence: {_m.group(0)!r}"
    return None


def _annotate_dual_source(verification: dict, verdict: DeterministicVerdict) -> None:
    """Quadrant-1 annotation: program passed AND the LLM was already green.

    Appends a checklist item marked ``[DETERMINISTIC]`` carrying the rule
    name, the numeric evidence lines and the anchor signal — the audit
    contract (same evidence, two independent sources, one conclusion).
    The verdict fields themselves stay untouched (they were already
    correct; this records WHY they are trustworthy).

    When the LLM emitted NO checklist at all, the annotation degrades to
    a warnings entry instead of fabricating a checklist skeleton: a
    synthetic ``checklist`` key would flip the step-coverage guard from
    "checklist absent → skip" to "checklist present → validate",
    manufacturing a phantom step gap that downgrades an otherwise-green
    verdict and spins a re-verify loop (re-audit finding 2). Same
    attributability, zero structural side effects.
    """
    _detail = "; ".join(
        line.rstrip(".") for line in verdict.evidence_lines
    )
    items = (verification.get("checklist") or {}).get("items")
    if not items:
        verification.setdefault("warnings", []).append(
            f"[DETERMINISTIC] rule '{verdict.rule_name}' passed — "
            f"{_detail}. (dual-source annotation; LLM emitted no checklist)"
        )
        return
    # Idempotence guard: never stack the same rule's annotation twice.
    if any(
        isinstance(_ci, dict)
        and f"[DETERMINISTIC] rule '{verdict.rule_name}' passed" in (_ci.get("evidence") or "")
        for _ci in items
    ):
        return
    items.append({
        # Non-int step marker: downstream step-coverage validators skip
        # non-int steps, and the TUI renders it as a rule annotation row.
        "step": "rule",
        "status": "passed",
        "evidence": (
            f"[DETERMINISTIC] rule '{verdict.rule_name}' passed — "
            f"{_detail}."
        ),
    })
    # Keep the checklist's derived counts consistent with its items
    # (dict_to_verification_result carries them verbatim):
    #   total_count        +1 — the annotation row IS a checklist row;
    #   total_executed     UNTOUCHED — it counts LLM-EXECUTED skill steps
    #                      (the step-coverage gap reads it), and a
    #                      programmatic annotation is not an executed step.
    if isinstance(verification.get("checklist"), dict):
        _cl = verification["checklist"]
        if "total_count" in _cl:
            _cl["total_count"] = int(_cl.get("total_count", 0)) + 1


def _synthesize_passed(
    verification: dict, verdict: DeterministicVerdict,
    *, replacement_as_effect: bool = False,
) -> bool:
    """Five-quadrant synthesis entry for a programmatic ``passed`` verdict.

      program passed × LLM degraded  → lift, UNLESS the LLM cites a
                                      specific counter-proof the rule
                                      does not cover (then respect the
                                      LLM, warn, and stay out);
      program passed × LLM green      → dual-source [DETERMINISTIC]
                                      annotation (verdict untouched).

    ``replacement_as_effect``: the caller rule's declaration that object
    replacement is part of its OWN effect criteria — the replacement
    arm of the counter-evidence gate is then skipped for it (see
    ``_counter_evidence``).

    Returns whether a LIFT was applied (the legacy ``enforcement``
    contract — True suppresses downstream gap-triggered downgrades).
    """
    _counter = _counter_evidence(
        verification, scan_replacement=not replacement_as_effect,
    )
    if _counter is not None:
        verification.setdefault("warnings", []).append(
            f"Programmatic override withheld: deterministic rule "
            f"'{verdict.rule_name}' passed, but LLM evidence cites a "
            f"counter-proof ({_counter}). LLM verdict respected."
        )
        logger.info(
            "Deterministic rule '%s' passed but counter-proof cited — "
            "LLM verdict respected (%s).",
            verdict.rule_name, _counter,
        )
        return False

    _lifted = _lift_verdict_to_passed(verification, verdict)
    if not _lifted:
        _annotate_dual_source(verification, verdict)
    return _lifted


def _apply_deterministic_verdicts(
    verification: dict,
    state: AgentState,
    *,
    layer1: Layer1Result | None = None,
    fault_handle: dict | None = None,
) -> bool:
    """Run the deterministic verdict rules (Layer 1.5) and apply their
    adjudication to ``verification``.

    Resolution: the fault identity ``(fault_target, fault_action)`` selects
    the declared rules from the VerificationProfile registry. Families
    without a declared rule return immediately — zero interference, the
    LLM verdict stands exactly as before this layer existed.

    Returns whether a programmatic lift was applied (the same contract
    ``_enforce_disk_burn_facts`` had: ``True`` suppresses downstream
    gap-triggered downgrades, e.g. step-coverage partials).
    """
    spec = read_fault_spec(state)
    target = spec.fault_target if spec else ""
    action = spec.fault_action if spec else ""
    rules = resolve_deterministic_rules(target, action)
    if not rules:
        return False

    handle = (
        fault_handle if fault_handle is not None
        else materialize_fault_handle(state)
    )
    l1 = layer1 if layer1 is not None else _restore_layer1_from_state(state)
    if l1.expired:
        # The fault window has closed (timeout expiry or early destroy) and
        # Layer 1 recorded it as a known cause — the rules measure effects-
        # in-presence, but their evidence outlives the window: timeline peaks
        # (fill), cumulative counters (kill RestartCount) and the injection-
        # time post-check snapshot (burn) are historical traces. Lifting an
        # honest recovered_before_observation here would assert "Fault is
        # still in effect" over a window that verifiably closed — the LLM
        # keeps the call (re-audit finding 12; same stance as the L1-only
        # entry, which records expiry instead of adjudicating it).
        return False
    applied = False
    for rule in rules:
        post_check = state.get(rule.post_check_key) if rule.post_check_key else None
        ctx = RuleContext(
            metric_observations=list(state.get("metric_observations") or []),
            fault_handle=handle,
            layer1_passed=bool(l1.is_passed()),
            post_check=post_check,
            spec_names=tuple(spec.names) if spec else (),
            spec_params=dict(spec.params) if spec else {},
        )
        verdict = rule.evaluate(ctx)
        if verdict.is_passed:
            applied = _synthesize_passed(
                verification, verdict,
                replacement_as_effect=rule.treats_replacement_as_effect,
            ) or applied
    return applied


def _apply_step_coverage(
    verification: dict, state: AgentState, submit_args: dict | None,
    enforcement_applied: bool,
) -> tuple[list | None, int, int]:
    """Validate checklist step coverage against the skill case.

    Answer-based coverage: every skill-case step must be ANSWERED in the
    checklist — any status with evidence counts (passed/failed as well as
    the discretionary expected/not_applicable/skipped). Only SILENT omission
    of a step is a coverage gap. Discretionary statuses without evidence
    are not valid answers: they downgrade 'passed' to 'partial' but do NOT
    trigger a re-verification loop.

    Returns ``(missing_step_nums, expected_steps, executed_steps)`` and mutates
    ``verification`` in place (warnings / layer2 downgrade to partial).
    """
    skill_case = state.get("skill_case_content", "")
    missing_step_nums = None
    expected_steps = 0
    executed_steps = 0
    if skill_case and verification.get("checklist"):
        # Multi-candidate: validate against the candidate the LLM chose
        _chosen = (submit_args or {}).get("chosen_candidate", 0)
        _skill_for_validation = skill_case
        if _chosen and isinstance(_chosen, int) and _chosen > 0:
            _candidates = _split_candidates(skill_case)
            if 0 < _chosen <= len(_candidates):
                _skill_for_validation = _candidates[_chosen - 1]

        expected_steps = _count_verification_steps_in_skill_case(_skill_for_validation)
        # Mode 2/3 contract: coverage validation is DISABLED when the case
        # has no parseable steps — the count fallback must not fire on
        # prose/bullet content the extractor could not enumerate.
        if not _extract_verification_step_descriptions(_skill_for_validation):
            expected_steps = 0
        executed_steps = verification["checklist"].get("total_executed", 0)
        checklist_items = verification["checklist"].get("items", [])
        missing_step_nums, _deviated = _validate_step_number_coverage(
            _skill_for_validation, checklist_items,
        )
        # Discretionary answers must carry a reason: 'expected' and
        # 'not_applicable' without evidence are judgments without
        # observation — downgrade, but do not re-verify on this alone.
        _unjustified = sorted(
            it.get("step") for it in checklist_items
            if it.get("status") in ("expected", "not_applicable")
            and not (it.get("evidence") or "").strip()
            and isinstance(it.get("step"), int)
        )
        if _unjustified:
            _ulist = ", ".join(str(s) for s in _unjustified)
            verification.setdefault("warnings", []).append(
                f"Step coverage: step(s) {_ulist} marked expected/not_applicable "
                f"without evidence. Discretionary statuses require an "
                f"observation or a reason."
            )
            if not enforcement_applied and verification["layer2"]["status"] == "passed":
                verification["layer2"]["status"] = "partial"
                if verification.get("level") == "verified":
                    verification["level"] = "partial"
        if missing_step_nums:
            step_list = ", ".join(str(s) for s in missing_step_nums)
            verification.setdefault("warnings", []).append(
                f"Step coverage: steps {step_list} from skill case "
                f"are missing from the verification checklist. "
                f"Verification may be incomplete."
            )
            if not enforcement_applied and verification["layer2"]["status"] == "passed":
                verification["layer2"]["status"] = "partial"
                if verification.get("level") == "verified":
                    verification["level"] = "partial"
        elif expected_steps > 0 and executed_steps < expected_steps:
            missing = expected_steps - executed_steps
            verification.setdefault("warnings", []).append(
                f"Step coverage: {executed_steps}/{expected_steps} steps executed. "
                f"{missing} step(s) never attempted. Verification may be incomplete."
            )
            if not enforcement_applied and verification["layer2"]["status"] == "passed":
                verification["layer2"]["status"] = "partial"
                if verification.get("level") == "verified":
                    verification["level"] = "partial"
    return missing_step_nums, expected_steps, executed_steps


def _layer1_contradiction_gap_fires(
    layer1: Layer1Result, verification: dict,
) -> bool:
    """Whether the layer1-contradiction gap ("blade Success but 0
    affected") should fire — extracted for test anchoring.

    Tasks 6.1: the gate reads the POST-synthesis layer2 status. The
    deterministic rules run EARLIER in the finalize pipeline (707 vs
    this read), so a programmatic lift has already upgraded a degraded
    L2 verdict to passed by the time we look — the gap then stays
    silent instead of spinning a pointless re-check loop. A withheld
    lift (specific counter-proof) leaves the LLM downgrade in place,
    and the gap correctly fires: the contradiction deserves a re-look.
    """
    _l2 = verification.get("layer2", {}).get("status", "unknown")
    return (
        layer1.status == "passed"
        and layer1.affected_count == 0
        and _l2 != "passed"
    )


def make_finalize_verification(registry=None):
    """Build the finalize_verification node."""

    async def finalize_verification(state: AgentState) -> dict:
        task_id = state.get("task_id", "")
        skill_name = read_active_skill_name(state)
        # Phase-4 T5: same identity resolution as both verifier entries —
        # the finalize node renders the SAME experiment UID the entry's
        # dispatch produced (a bare state read renders "" on
        # message-history-claim states, disagreeing with the entry).
        from chaos_agent.agent.nodes.verify.verifier import (
            _experiment_uid_of,
            _resolve_fault_dispatch,
        )
        from chaos_agent.agent.state import materialize_fault_handle

        _handle = materialize_fault_handle(state)
        _, _identity = _resolve_fault_dispatch(state)
        experiment_uid = _experiment_uid_of(_identity) or _experiment_uid_of(_handle)
        kubeconfig = _resolve_kubeconfig(state)
        sync_kubewiz_runtime(state)
        messages = state.get("messages", [])

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "finalize_verification",
            "Finalizing verification verdict",
            {"experiment_uid": experiment_uid},
        )

        layer1 = _restore_layer1_from_state(state)

        # ---- Source the verdict: submit_verification args > text fallback ----
        submit_args = _extract_submit_args(messages)
        if submit_args is not None:
            verification = _verification_from_submit_args(submit_args)
            content = ""
        else:
            content = _last_ai_text(messages)
            verification = _try_parse_json(content)
            if verification is None:
                verification = _parse_verification_result(content)

        result_update: dict = {}

        # E2 Phase 3 — cross-check LLM evidence numbers vs observation timeline.
        verification = cross_check_evidence(
            verification, state.get("metric_observations"),
        )
        verification["layer1"] = layer1_to_dict(layer1)

        # ---- Programmatic Fact Enforcement: deterministic rules (Layer 1.5) ----
        # disk_burn's legacy direct call migrated onto the rule registry
        # pipeline; behaviour is byte-identical for the burn family and a
        # no-op for every family without a declared rule.
        _enforcement_applied = _apply_deterministic_verdicts(
            verification, state, layer1=layer1, fault_handle=_handle,
        )

        # ---- Step coverage vs skill case ---- (extracted to _apply_step_coverage)
        missing_step_nums, expected_steps, executed_steps = _apply_step_coverage(
            verification, state, submit_args, _enforcement_applied,
        )

        # ---- Programmatic coverage warning ----
        layer1_affected = layer1.affected_count
        _spec3 = read_fault_spec(state)
        target_names = list(_spec3.names) if _spec3 else []
        if layer1_affected > 0 and len(target_names) > layer1_affected:
            coverage_warning = (
                f"Coverage: {layer1_affected}/{len(target_names)} target resources "
                f"affected by the fault experiment."
            )
            warnings = verification.get("warnings", [])
            if coverage_warning not in warnings:
                warnings.append(coverage_warning)
                verification["warnings"] = warnings

        # ---- P2 verification-integrity gaps → re-verification ----
        from chaos_agent.utils.fault_context import VerificationGap, lookup_adaptations
        gaps: list[VerificationGap] = []
        # Clear any previous reverify_gaps; re-set below if still gapped.
        if state.get("reverify_gaps"):
            result_update["reverify_gaps"] = None

        if not _enforcement_applied:
            if missing_step_nums:
                gaps.append(VerificationGap(
                    gap_type="step_gap",
                    description=f"Steps {missing_step_nums} from skill case missing from checklist",
                    missing_steps=missing_step_nums,
                ))
            elif expected_steps > 0 and executed_steps < expected_steps:
                missing_count = expected_steps - executed_steps
                gaps.append(VerificationGap(
                    gap_type="step_gap",
                    description=f"{executed_steps}/{expected_steps} steps executed, {missing_count} missing",
                ))

        # Only a LIVE ChaosBlade experiment can meaningfully contradict itself
        # here. If Layer 2 has already independently confirmed the fault is in
        # effect — or the deterministic-rule synthesis lifted it to passed
        # (the gate reads the POST-synthesis status; see
        # _layer1_contradiction_gap_fires) — a "passed but 0 affected"
        # Layer 1 count is noise (e.g. a residual / kubectl-native case) —
        # re-verifying on it just spins without terminating.
        if _layer1_contradiction_gap_fires(layer1, verification):
            gaps.append(VerificationGap(
                gap_type="layer1_contradiction",
                description="blade reports Success but 0 resources affected",
            ))

        l2_status_val = verification.get("layer2", {}).get("status", "unknown")
        side_effects = verification.get("side_effects") or {}
        container_restarts = side_effects.get("container_restarts", False)
        if l2_status_val == "passed" and container_restarts:
            gaps.append(VerificationGap(
                gap_type="layer2_layer1_conflict",
                description="Layer2 says verified but container restarts (OOMKill) detected in Layer1",
            ))

        _baseline = state.get("baseline_data")
        _baseline_available = _baseline and _baseline.get("success_count", 0) > 0
        if _baseline_available and not verification.get("baseline_used", False):
            gaps.append(VerificationGap(
                gap_type="baseline_used_check",
                description=(
                    "Pre-injection baseline data was available but BaselineUsed=false. "
                    "Compare observations against the baseline and set BaselineUsed: true."
                ),
            ))

        _peo = verification.get("primary_evidence_observed", False)
        _overall = verification.get("overall", "")
        if not _peo and _overall == "verified":
            gaps.append(VerificationGap(
                gap_type="primary_evidence_consistency",
                description=(
                    "PrimaryEvidenceObserved=false but Overall=verified. "
                    "Overall MUST be 'partial' or 'unverified'."
                ),
            ))

        # EvidenceProfile is the shared baseline/verification contract.  A
        # verdict may only be fully verified when the post-injection evidence
        # independently covers target identity, its main metric, and a cross
        # metric.  Unknown profiles are deliberately not inferred by the LLM.
        _verify_profile = profile_of(resolve_channel_name(state))
        _evidence_profile = EvidenceProfile.for_fault(
            read_fault_spec(state), _verify_profile,
        )
        _verification_records = list(state.get("metric_observations") or [])
        _verification_records.extend(
            item for item in verification.get("checklist", {}).get("items", [])
            if isinstance(item, dict)
        )
        # Q2 optimization: deterministically anchor host verification evidence.
        # When the verdict concluded fast on a strong fault, the LLM's probes may
        # not cover target_identity / an independent cross-metric. Run cheap
        # read-only host probes to close those gaps without forcing a re-verify
        # loop; failures are best-effort and never change the verdict.
        if _verify_profile == PROFILE_HOST and _evidence_profile.enabled:
            _pre_cov = _evidence_profile.coverage(_verification_records)
            if _pre_cov.missing:
                _supp_records = await _supplement_host_verification_evidence(
                    read_fault_spec(state), set(_pre_cov.missing),
                    _verification_records, state,
                )
                if _supp_records:
                    _verification_records.extend(_supp_records)
                    result_update["metric_observations"] = (
                        list(state.get("metric_observations") or []) + _supp_records
                    )
        _evidence_coverage = _evidence_profile.coverage(_verification_records)
        verification["evidence_coverage"] = _evidence_coverage.as_dict()
        # Evidence sufficiency is the LLM's holistic judgment, NOT a framework
        # keyword-match gate. A keyword-coverage miss (e.g. a node-terminal fault
        # whose decisive evidence is "NotReady + kubelet lost" rather than the
        # network vocabulary) must NOT force re-verify — that pathologically loops
        # the verifier probing an unreachable target to manufacture a cross-metric.
        # Keep coverage as audit metadata + a non-blocking warning; the real
        # anti-cheat floor is primary_evidence_observed (hard downgrade, below) +
        # the skill-case step-coverage gap.
        if _evidence_coverage.missing:
            _cov_note = (
                f"Evidence profile {_evidence_coverage.profile_id} did not keyword-match: "
                f"{', '.join(_evidence_coverage.missing)} (advisory only, not a verdict gate)."
            )
            _cov_warnings = verification.get("warnings", [])
            if _cov_note not in _cov_warnings:
                _cov_warnings.append(_cov_note)
                verification["warnings"] = _cov_warnings

        if gaps:
            reverify_count = state.get("reverify_count", 0)
            target_metadata = state.get("target_metadata") or {}
            _spec4 = read_fault_spec(state)
            adaptations = lookup_adaptations(
                _spec4.scope if _spec4 else "",
                _spec4.fault_target if _spec4 else "",
                _spec4.fault_action if _spec4 else "",
                target_metadata,
                rule_type="verification_integrity_guard",
            )
            max_attempts = adaptations[0].action.get("max_reverify_attempts", 1) if adaptations else 1

            if reverify_count < max_attempts:
                gap_descriptions = "; ".join(g.description for g in gaps)
                logger.info(
                    "P2 verification gaps detected: %s — re-verification (attempt %d/%d)",
                    gap_descriptions, reverify_count + 1, max_attempts,
                )
                _gap_instructions = []
                for _g in gaps:
                    if _g.gap_type == "step_gap":
                        _missing = _g.missing_steps or []
                        _step_str = ", ".join(str(s) for s in _missing) if _missing else "unknown"
                        _gap_instructions.append(
                            f"- STEP GAP: Skill case steps [{_step_str}] are missing from your "
                            f"checklist. Add each missing step with status and evidence."
                        )
                    elif _g.gap_type == "layer1_contradiction":
                        _gap_instructions.append(
                            "- LAYER1 CONTRADICTION: blade reports Success but 0 resources "
                            "affected. Explain consistency with your Layer2 conclusion."
                        )
                    elif _g.gap_type == "layer2_layer1_conflict":
                        _gap_instructions.append(
                            "- LAYER2/LAYER1 CONFLICT: Layer2=passed but container restarts "
                            "detected. Reconcile: fault evidence, or destroyed primary evidence?"
                        )
                    elif _g.gap_type == "baseline_used_check":
                        _gap_instructions.append(
                            "- BASELINE NOT USED: Include \"baseline: X → current: Y (ΔZ)\" "
                            "comparisons and set BaselineUsed: true."
                        )
                    elif _g.gap_type == "primary_evidence_consistency":
                        _gap_instructions.append(
                            "- EVIDENCE/CONCLUSION CONFLICT: PrimaryEvidenceObserved=false but "
                            "Overall=verified. Use 'partial' or 'unverified'."
                        )
                    else:
                        _gap_instructions.append(f"- {_g.description}")
                _instructions_str = "\n".join(_gap_instructions)
                reverify_msg = (
                    f"Verification gaps detected:\n{_instructions_str}\n\n"
                    f"Re-attempt verification and call submit_verification again with ALL "
                    f"gaps addressed."
                )
                # Clean message handling: append only the reverify prompt; the
                # prior response + ToolMessages are already in state. Do NOT set
                # verification → route_after_finalize sends us back to verifier_loop.
                result_update["messages"] = [HumanMessage(content=wrap_system_reminder(reverify_msg))]
                result_update["reverify_count"] = reverify_count + 1
                result_update["reverify_gaps"] = [g.gap_type for g in gaps]
                sync_node_status_to_session(
                    state, FINALIZE_VERIFICATION,
                    f"P2 re-verification triggered: {gap_descriptions} "
                    f"(attempt {reverify_count + 1}/{max_attempts})",
                    detail={"gap_types": [g.gap_type for g in gaps],
                            "attempt": reverify_count + 1, "max_attempts": max_attempts},
                )
                tracker.complete(f"Re-verification triggered: {gap_descriptions}")
                await sync_to_store(state, result_update)
                return result_update
            else:
                logger.info(
                    "P2 gaps detected but max reverify attempts (%d) reached — degrade to partial",
                    max_attempts,
                )
                sync_node_status_to_session(
                    state, FINALIZE_VERIFICATION,
                    f"P2 re-verification max attempts reached, degrading to partial ({max_attempts})",
                    detail={"gap_types": [g.gap_type for g in gaps], "max_attempts": max_attempts},
                )

        # ---- Finalize (no gaps, or budget exhausted) ----
        # baseline_confidence + enforcement
        if "baseline_confidence" not in verification:
            verification["baseline_confidence"] = _compute_baseline_confidence(state)
        _bl_conf = verification.get("baseline_confidence", "none")
        if _bl_conf in ("high", "partial") and not verification.get("baseline_used"):
            _bl_used_orig = verification.get("baseline_used")
            verification["baseline_used"] = True
            verification.setdefault("warnings", []).append(
                f"Programmatic override: BaselineUsed forced to true — pre-injection "
                f"baseline was available (confidence={_bl_conf}) but LLM declared "
                f"BaselineUsed={_bl_used_orig}."
            )

        # ---- Verify-Replan: unverified + L2 failed → replan to Phase 1 ----
        _level = verification.get("level", "")
        _l2_status = verification.get("layer2", {}).get("status", "unknown")
        if _verify_replan_eligible(verification):
            verify_replan_count = state.get("verify_replan_count", 0)
            try:
                _max_verify_replan = int(settings.max_verify_replan_count)
            except (TypeError, ValueError):
                _max_verify_replan = 3

            if verify_replan_count < _max_verify_replan:
                # 1. Deterministic residual cleanup — based on what's actually in state
                residuals_cleaned = await _cleanup_residuals(state, kubeconfig)

                # 1b. Retire the UIDs the framework just destroyed. The destroy
                # ran in CODE (no blade_destroy ToolMessage in history), so the
                # message scan would resurrect the stale UID into experiment_uid and
                # misroute the next verification's Layer-1 (task-29848471).
                _retired_new = _retired_uids_from_residuals(residuals_cleaned)
                if _retired_new:
                    result_update["retired_experiment_uids"] = list(
                        state.get("retired_experiment_uids") or []
                    ) + _retired_new

                # 2. Build replan context with verifier findings
                _replan_ctx = _build_verify_replan_context(
                    verification, residuals_cleaned, verify_replan_count, skill_name,
                    messages=state.get("messages") or [],
                )
                _replan_request = ReplanRequest.model_validate({
                    key: _replan_ctx[key]
                    for key in (
                        "kind", "decision", "invalidated_assumption",
                        "observed_evidence", "evidence_refs", "affected_step",
                        "unresolved_questions", "changes_target_or_risk",
                    )
                })

                # 3. Set state for replan
                result_update["replan_requested"] = True
                result_update["replan_context"] = _replan_ctx
                result_update["replan_request"] = _replan_request.model_dump()
                result_update["verify_replan_count"] = verify_replan_count + 1
                result_update["execute_loop_count"] = 0
                result_update["verifier_loop_count"] = 0
                result_update["reverify_count"] = 0
                result_update["verification"] = None
                result_update["approved_target"] = None
                result_update["reverify_gaps"] = None
                result_update["error"] = None
                # Shared attribution reset (experiment_uid included — the residue
                # was just destroyed and retired above): re-arms injection
                # method re-detection so the registry can re-attribute by
                # RECENCY if the replanned attempt switches carriers. The
                # message_count records the attribution epoch boundary so the
                # re-detection scan cannot resurrect pre-seam attempts
                # (task-5193538b).
                from chaos_agent.agent.nodes.execute.execute_loop import (
                    reset_attribution_state,
                )
                # state_messages (not a tail count): the seam also removes the
                # stale synthetic baseline pair and re-bases the epoch on the
                # POST-MERGE length — see reset_attribution_state docstring.
                reset_attribution_state(
                    result_update,
                    state_messages=state.get("messages") or [],
                    state=state,
                )

                # 4. Append replan history (with compact verification snapshot for auditing)
                _vf = _replan_ctx.get("verifier_findings", {})
                _history = list(state.get("replan_history") or [])
                _history.append({
                    "attempt": verify_replan_count + 1,
                    "original_error": f"Verification unverified: L2={_l2_status}",
                    "action_taken": "(pending Phase 1 analysis)",
                    "trigger": "verify_replan",
                    "verification_snapshot": {
                        "level": _level,
                        "layer1_status": _vf.get("layer1_status", ""),
                        "layer2_status": _l2_status,
                        "layer2_details": (_vf.get("layer2_details", "") or "")[:500],
                        "failed_evidence": _vf.get("failed_evidence", [])[:5],
                    },
                })
                result_update["replan_history"] = _history

                # 5. Record attempt for tracking/auditing
                from chaos_agent.agent.attempt_tracker import (
                    REASON_GRAPH_REPLAN,
                    begin_attempt,
                )
                _attempt_delta = begin_attempt(
                    {**state, **result_update},
                    target=state.get("fault_spec"),
                    reason=REASON_GRAPH_REPLAN,
                    notes=_replan_ctx.get("error_summary", "")[:200],
                )
                result_update.update(_attempt_delta)

                # 6. Log + status
                logger.info(
                    "Verify-replan triggered: level=unverified, L2=failed, "
                    "attempt %d/%d, residuals cleaned: %s",
                    verify_replan_count + 1, _max_verify_replan, residuals_cleaned,
                )
                sync_node_status_to_session(
                    state, FINALIZE_VERIFICATION,
                    f"Verify-replan triggered (attempt {verify_replan_count + 1}/{_max_verify_replan}): "
                    f"verification unverified, L2 failed",
                    detail={"residuals_cleaned": residuals_cleaned,
                            "verify_replan_count": verify_replan_count + 1},
                )
                tracker.complete(
                    "Verify-replan triggered: level=unverified, L2=failed"
                )
                # Clean up debug pods created by the verifier (same as
                # the normal finalize path — early return would skip it).
                await _cleanup_debug_pods(state, kubeconfig, task_id, result_update)
                await sync_to_store(state, result_update)
                return result_update

        # Round-29 K2 (option A): ``experiment_uid`` keeps its identity-
        # first attribution semantics — the L4/Web/DB traceability axis
        # ("what did this task inject"), which legitimately survives the
        # experiment's death. The LIVE axis gets its own contract field
        # rendered from the liability oracle ("what is still running at
        # verdict time") — the same set the sweep and the destroy
        # whitelist consume, never the never-cleared slot: external
        # consumers stopped seeing the corpse as the task's only
        # experiment.
        try:
            from chaos_agent.agent.state import live_liability_uids
            _live_uids = live_liability_uids(state)
        except Exception:
            _live_uids = []

        result = {
            "task_id": task_id,
            "skill": skill_name,
            "experiment_uid": experiment_uid,
            "live_experiment_uids": _live_uids,
            "verified": verification["level"] == "verified",
        }

        l2_details = verification.get("layer2", {}).get("details", "")
        summary_kwargs = {}
        if l2_details:
            summary_kwargs["inject_verification_summary"] = (
                f"Layer2={verification.get('layer2', {}).get('status', 'unknown')}, "
                f"Details={l2_details}"
            )
        result_update = write_inject_verification(
            result_update,
            result=result,
            verification=verification,
            **summary_kwargs,
        )

        level = verification["level"]
        l1_status = layer1.status.value
        l2_status = verification.get("layer2", {}).get("status", "unknown")
        warnings = verification.get("warnings", [])
        status_msg = f"Verification: {level} (Layer1: {l1_status}, Layer2: {l2_status})"
        if warnings:
            status_msg += f" | warnings: {'; '.join(warnings)}"
        tracker.complete(status_msg)

        # Write verification detail to session store as plain text.
        # Renders in the TUI conversation stream between the tool card
        # and ResultCard — not inside any card or tool box, no line limit.
        _store = get_global_session_store()
        if _store and task_id:
            detail_text = _format_verification_detail(verification, layer1)
            if detail_text:
                _store.append_messages(
                    task_id,
                    [HumanMessage(content=f"[Verification Result]\n{detail_text}")],
                    node_name="finalize_verification",
                )

        # Programmatic debug-pod cleanup (moved here; dedup preserved).
        await _cleanup_debug_pods(state, kubeconfig, task_id, result_update)

        await sync_to_store(state, result_update)
        from chaos_agent.agent.router import mark_wall_clock_timeout
        return mark_wall_clock_timeout(state, result_update)

    return finalize_verification
