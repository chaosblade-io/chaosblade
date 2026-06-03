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

from langchain_core.messages import HumanMessage, ToolMessage

from chaos_agent.agent.fault_spec import read_fault_spec
from chaos_agent.agent.node_names import FINALIZE_VERIFICATION, VERIFIER
from chaos_agent.agent.nodes._kubeconfig_inject import _resolve_kubeconfig
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.nodes._verifier_layer1 import _layer1_to_dict, _restore_layer1_from_state
from chaos_agent.agent.nodes._verifier_layer2_parse import (
    _count_verification_steps_in_skill_case,
    _detect_checklist_conclusion_inconsistency,
    _parse_verification_result,
    _split_candidates,
    _try_parse_json,
    _validate_step_number_coverage,
    cross_check_evidence,
)
from chaos_agent.agent.nodes._verifier_shared import _compute_baseline_confidence
from chaos_agent.agent.nodes._verifier_submit import SUBMIT_VERIFICATION_TOOL_NAME
from chaos_agent.agent.nodes.baseline_capture import _parse_debug_pod_name, _delete_debug_pod
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory

logger = logging.getLogger(__name__)


async def _cleanup_debug_pods(
    state: AgentState,
    kubeconfig: str,
    task_id: str,
    result_update: dict,
) -> None:
    """Programmatic debug-pod cleanup with cross-reentry dedup.

    Scans the message history for ``kubectl debug node/...`` pods created
    by the LLM, diffs against ``state.cleaned_debug_pods`` (pods we've
    already attempted to delete in earlier verifier re-entries), deletes
    only the new ones, and writes the merged set back into
    ``result_update`` so the next re-entry sees them as already-handled.

    Moved here from verifier_loop (Scheme B): cleanup now runs once, at
    finalize, rather than every ReAct iteration. The dedup is still needed
    because finalize can be reached more than once (re-verification rounds).
    """
    discovered_pods: set[str] = set()
    for msg in state.get("messages", []):
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "kubectl":
            msg_content = msg.content if isinstance(msg.content, str) else str(msg.content)
            pod_name = _parse_debug_pod_name(msg_content)
            if pod_name:
                discovered_pods.add(pod_name)
    already_cleaned: set[str] = set(state.get("cleaned_debug_pods") or [])
    pods_to_delete = discovered_pods - already_cleaned
    for pod_name in pods_to_delete:
        logger.info(f"Programmatic cleanup: deleting debug pod {pod_name}")
        await _delete_debug_pod(pod_name, kubeconfig, task_id)
    if pods_to_delete:
        result_update["cleaned_debug_pods"] = sorted(already_cleaned | pods_to_delete)


def _overall_to_level(overall: str) -> str:
    """Map submit_verification's ``overall`` to the internal ``level``."""
    return overall if overall in ("verified", "partial", "unverified") else "unverified"


def _verification_from_submit_args(args: dict) -> dict:
    """Build a verification dict from submit_verification tool-call args.

    Produces the SAME shape ``_parse_verification_result`` / ``_try_parse_json``
    yield, so all downstream post-processing is source-agnostic. Also runs the
    checklist↔conclusion inconsistency check (mirrors the JSON-mode path).
    """
    checklist = args.get("checklist") or []
    if not isinstance(checklist, list):
        checklist = []
    l2_status = args.get("layer2_status", "unknown")
    overall = args.get("overall", "unverified")
    warnings = list(args.get("warnings") or [])

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
            "non_passed_count": sum(
                1 for c in checklist
                if c.get("status") in ("failed", "partial", "recovered_before_observation")
            ),
            "total_count": len(checklist),
            "total_executed": len(checklist),
        }
        if l2_status == "passed":
            _non_passed_ev = " ".join(
                c.get("evidence", "") for c in checklist
                if isinstance(c, dict) and c.get("status") in ("failed", "partial", "recovered_before_observation")
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
    """Return the args of the most recent submit_verification tool_call, or None.

    Only scans messages AFTER the last reverify/guard HumanMessage that finalize
    itself injected (identified by the "Verification gaps" / "re-verification"
    / "GUARD" marker). This prevents stale submit args from a prior round being
    re-used after a reverify loop-back (where the LLM may answer with text
    instead of a second submit call).
    """
    # Find the boundary: last HumanMessage that finalize injected for reverify/guard.
    boundary_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            content = getattr(msg, "content", "") or ""
            if "Verification gaps" in content or "re-verification" in content:
                boundary_idx = i

    # Only scan after the boundary (or from start if no boundary).
    search_slice = messages[boundary_idx + 1:] if boundary_idx >= 0 else messages

    for msg in reversed(search_slice):
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name == SUBMIT_VERIFICATION_TOOL_NAME:
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                return args or {}
    return None


def _last_ai_text(messages: list) -> str:
    """Return the content of the last assistant message (text fallback source).

    The assistant message is the last entry that is neither a HumanMessage,
    ToolMessage, nor SystemMessage — robust to both real AIMessage (type="ai")
    and test doubles (MagicMock without .type).
    """
    from langchain_core.messages import SystemMessage
    for msg in reversed(messages):
        if isinstance(msg, (HumanMessage, ToolMessage, SystemMessage)):
            continue
        content = getattr(msg, "content", "") or ""
        if content:
            return content if isinstance(content, str) else str(content)
    return ""


def make_finalize_verification(registry=None):
    """Build the finalize_verification node."""

    async def finalize_verification(state: AgentState) -> dict:
        task_id = state.get("task_id", "")
        skill_name = state.get("skill_name", "")
        blade_uid = state.get("blade_uid", "")
        kubeconfig = _resolve_kubeconfig(state)
        count = state.get("verifier_loop_count", 0)
        messages = state.get("messages", [])

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "finalize_verification",
            "Finalizing verification verdict",
            {"blade_uid": blade_uid},
        )

        layer1 = _restore_layer1_from_state(state)

        # ---- Source the verdict: submit_verification args > text fallback ----
        submit_args = _extract_submit_args(messages)
        is_text_source = submit_args is None
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
        verification["layer1"] = _layer1_to_dict(layer1)

        # ---- Programmatic Fact Enforcement: disk_burn I/O active ----
        _burn_enforce = state.get("disk_burn_post_check")
        _enforcement_applied = False
        if _burn_enforce and _burn_enforce.get("burn_io_detected"):
            _active_parts = _burn_enforce.get("active_partitions", [])
            _parts_str = ", ".join(
                f"{p['name']}: ~{p['write_throughput_mb_s']} MB/s"
                for p in _active_parts[:3]
            ) or "measured"
            _io_overridden = False
            for _ci in verification.get("checklist", {}).get("items", []):
                if _ci.get("status") in ("failed", "recovered_before_observation", "partial"):
                    _ci["status"] = "passed"
                    _ci["evidence"] = (
                        f"[OVERRIDE] Programmatic I/O check confirmed ACTIVE "
                        f"(write throughput: {_parts_str}). "
                        f"Fault is still in effect — LLM observation was insufficient, "
                        f"not evidence of recovery."
                    )
                    _io_overridden = True
            if _io_overridden:
                logger.info(
                    "Programmatic enforcement: disk_burn_post_check confirmed I/O ACTIVE, "
                    "overriding LLM checklist."
                )
                _l2_val = verification.get("layer2", {}).get("status", "unknown")
                if _l2_val in ("failed", "recovered_before_observation", "partial"):
                    verification["layer2"]["status"] = "passed"
                    verification["layer2"]["details"] = (
                        f"Programmatic I/O check: disk burn ACTIVE "
                        f"(write throughput: {_parts_str}). LLM conclusion overridden."
                    )
                    _l2_desc = (
                        "the fault was absent" if _l2_val == "failed"
                        else "the fault effect had already dissipated before observation"
                        if _l2_val == "recovered_before_observation"
                        else "the fault effect was only partially confirmed"
                    )
                    verification.setdefault("warnings", []).append(
                        f"Programmatic override: disk_burn_post_check confirmed I/O ACTIVE "
                        f"(write throughput: {_parts_str}), but LLM concluded "
                        f"{_l2_desc} (original status: '{_l2_val}')."
                    )
                else:
                    verification.setdefault("warnings", []).append(
                        f"Programmatic override: disk_burn_post_check confirmed I/O ACTIVE "
                        f"(write throughput: {_parts_str}) "
                        f"(LLM Layer2 concluded '{_l2_val}'; override applied to checklist steps only)."
                    )
                _enforcement_applied = True

        if _enforcement_applied:
            _all_items = verification.get("checklist", {}).get("items", [])
            if _all_items:
                _remaining_bad = sum(
                    1 for _ci in _all_items
                    if _ci.get("status") in ("failed", "recovered_before_observation", "partial")
                )
                if _remaining_bad == 0 and verification.get("layer2", {}).get("status") == "passed":
                    verification["level"] = "verified"
                elif verification.get("layer2", {}).get("status") == "passed" and _remaining_bad > 0:
                    verification["level"] = "partial"

        # ---- Step coverage vs skill case ----
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
            executed_steps = verification["checklist"].get("total_executed", 0)
            checklist_items = verification["checklist"].get("items", [])
            missing_step_nums, _deviated = _validate_step_number_coverage(
                _skill_for_validation, checklist_items,
            )
            if missing_step_nums:
                step_list = ", ".join(str(s) for s in missing_step_nums)
                verification.setdefault("warnings", []).append(
                    f"Step coverage: steps {step_list} from skill case "
                    f"are missing from the verification checklist. "
                    f"Verification may be incomplete."
                )
                if not _enforcement_applied and verification["layer2"]["status"] == "passed":
                    verification["layer2"]["status"] = "partial"
                    if verification.get("level") == "verified":
                        verification["level"] = "partial"
            elif expected_steps > 0 and executed_steps < expected_steps:
                missing = expected_steps - executed_steps
                verification.setdefault("warnings", []).append(
                    f"Step coverage: {executed_steps}/{expected_steps} steps executed. "
                    f"{missing} step(s) never attempted. Verification may be incomplete."
                )
                if not _enforcement_applied and verification["layer2"]["status"] == "passed":
                    verification["layer2"]["status"] = "partial"
                    if verification.get("level") == "verified":
                        verification["level"] = "partial"

        # ---- Programmatic coverage warning ----
        layer1_affected = layer1.affected_count
        _spec3 = read_fault_spec(state)
        target_names = list(_spec3.names) if _spec3 else []
        if layer1_affected > 0 and len(target_names) > layer1_affected:
            coverage_warning = (
                f"Coverage: {layer1_affected}/{len(target_names)} target resources "
                f"affected by ChaosBlade experiment."
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

        if layer1.status == "passed" and layer1.affected_count == 0:
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

        if gaps:
            reverify_count = state.get("reverify_count", 0)
            target_metadata = state.get("target_metadata") or {}
            _spec4 = read_fault_spec(state)
            adaptations = lookup_adaptations(
                _spec4.scope if _spec4 else "",
                _spec4.blade_target if _spec4 else "",
                _spec4.blade_action if _spec4 else "",
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
                result_update["messages"] = [HumanMessage(content=reverify_msg)]
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

        result = {
            "task_id": task_id,
            "skill": skill_name,
            "blade_uid": blade_uid,
            "verified": verification["level"] == "verified",
        }
        result_update["result"] = result
        result_update["verification"] = verification

        l2_details = verification.get("layer2", {}).get("details", "")
        if l2_details:
            result_update["inject_verification_summary"] = (
                f"Layer2={verification.get('layer2', {}).get('status', 'unknown')}, "
                f"Details={l2_details}"
            )

        level = verification["level"]
        l1_status = layer1.status
        l2_status = verification.get("layer2", {}).get("status", "unknown")
        warnings = verification.get("warnings", [])
        status_msg = f"Verification: {level} (Layer1: {l1_status}, Layer2: {l2_status})"
        if warnings:
            status_msg += f" | warnings: {'; '.join(warnings)}"
        tracker.complete(status_msg)

        # Programmatic debug-pod cleanup (moved here; dedup preserved).
        await _cleanup_debug_pods(state, kubeconfig, task_id, result_update)

        await sync_to_store(state, result_update)
        from chaos_agent.agent.router import mark_wall_clock_timeout
        return mark_wall_clock_timeout(state, result_update)

    return finalize_verification
