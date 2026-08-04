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

from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.evidence import EvidenceProfile, host_evidence_supplements
from chaos_agent.agent.replan import ReplanRequest
from chaos_agent.transports import PROFILE_HOST, profile_of, resolve_channel_name
from chaos_agent.agent.node_names import FINALIZE_VERIFICATION
from chaos_agent.agent.result.operation_outcome import write_inject_verification
from chaos_agent.agent.nodes.execute._debug_pod import parse_debug_pod_info, delete_debug_pod
from chaos_agent.agent.nodes.execute._kubeconfig_inject import _resolve_kubeconfig, sync_kubewiz_runtime
from chaos_agent.agent.nodes.store._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.nodes.verify._verifier_layer1 import _layer1_to_dict, _restore_layer1_from_state
from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
    _count_verification_steps_in_skill_case,
    _detect_checklist_conclusion_inconsistency,
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

# Backward-compat aliases
_parse_debug_pod_info = parse_debug_pod_info
_delete_debug_pod = delete_debug_pod
from chaos_agent.agent.state import AgentState
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
            pod_name, ns = _parse_debug_pod_info(msg_content)
            if pod_name:
                discovered_pods[pod_name] = ns
    already_cleaned: set[str] = set(state.get("cleaned_debug_pods") or [])
    already_cleaned.update(artifact_cleaned)
    # Artifacts are authoritative for new tasks. In particular, a
    # ``recovery_armed`` carrier must stay alive until its node-local rollback
    # timer expires. The legacy message scan is only for old, untracked pods.
    tracked_names = {
        str(artifact.get("name") or "")
        for artifact in tracked_artifacts
        if isinstance(artifact, dict) and artifact.get("type") == "debug_pod"
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
                "skipped": "○", "recovered_before_observation": "◇"}
    level_icon = {"verified": "✓", "partial": "◐", "unverified": "✗"}.get(level, "·")

    lines = [f"{level_icon} Verification: {level} (Layer1: {layer1.status}, Layer2: {l2_status})"]

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

    Tool coupling: ``blade_uid`` cleanup is ChaosBlade-specific. For
    ``kubectl_native`` injections, the revert command is injection-specific
    (e.g. ``kubectl scale`` back, ``kubectl untaint``) and not tracked in
    state, so no deterministic cleanup is performed — the replan is expected
    to produce a different injection method that overwrites the residual.
    Users can manually recover via ``blade-ai recover`` if needed.
    """
    cleaned = []

    blade_uid = state.get("blade_uid", "")
    if blade_uid:
        try:
            from chaos_agent.tools.blade import blade_destroy as _blade_destroy
            _destroy_out = await _blade_destroy.ainvoke(
                {"uid": blade_uid, "kubeconfig": kubeconfig}
            )
            cleaned.append({
                "type": "running_experiment",
                "id": blade_uid,
                "cleanup_result": str(_destroy_out)[:200],
            })
            logger.info(
                "Verify-replan cleanup: destroyed experiment %s", blade_uid,
            )
        except Exception as e:
            cleaned.append({
                "type": "running_experiment",
                "id": blade_uid,
                "cleanup_result": f"failed: {e}",
            })
            logger.warning(
                "Verify-replan cleanup: failed for %s: %s", blade_uid, e,
            )

    return cleaned


def _retired_uids_from_residuals(residuals_cleaned: list[dict]) -> list[str]:
    """UIDs that verify-replan cleanup actually destroyed.

    Only successfully-destroyed UIDs are retired: a failed destroy (exception
    -> ``failed: ...``, or a soft tool failure -> ``Error: ...`` — the tool
    returns the error string instead of raising) may leave a live experiment
    that we must keep tracking, not hide behind retirement.
    """
    return [
        r["id"] for r in residuals_cleaned
        if r.get("type") == "running_experiment"
        and r.get("id")
        and not str(r.get("cleanup_result", "")).startswith(
            ("failed", "Error:")
        )
    ]


def _build_verify_replan_context(
    verification: dict,
    residuals_cleaned: list[dict],
    verify_replan_count: int,
    skill_name: str,
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

    return {
        "error_summary": invalidated_assumption,
        **request.as_context(),
        "iteration_at_failure": verify_replan_count + 1,
        "failed_tool_calls": [],  # No tool failure — tool succeeded but effect absent
        "rejected_params": [],
        "failed_tool_names": [],
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
        spec.blade_target if spec else "", missing, existing_text,
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


def _enforce_disk_burn_facts(verification: dict, state: AgentState) -> bool:
    """Programmatic Fact Enforcement: override the LLM verdict when the
    ``disk_burn_post_check`` measured I/O still ACTIVE.

    Mutates ``verification`` in place (checklist items / layer2 / level /
    warnings) and returns whether an override was applied. Pure extraction from
    ``finalize_verification`` — behaviour unchanged.
    """
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
    return _enforcement_applied


def _apply_step_coverage(
    verification: dict, state: AgentState, submit_args: dict | None,
    enforcement_applied: bool,
) -> tuple[list | None, int, int]:
    """Validate checklist step coverage against the skill case.

    Returns ``(missing_step_nums, expected_steps, executed_steps)`` and mutates
    ``verification`` in place (warnings / layer2 downgrade to partial). Pure
    extraction from ``finalize_verification`` — behaviour unchanged.
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


def make_finalize_verification(registry=None):
    """Build the finalize_verification node."""

    async def finalize_verification(state: AgentState) -> dict:
        task_id = state.get("task_id", "")
        skill_name = read_active_skill_name(state)
        blade_uid = state.get("blade_uid", "")
        kubeconfig = _resolve_kubeconfig(state)
        sync_kubewiz_runtime(state)
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
        # (extracted to _enforce_disk_burn_facts; mutates verification in place)
        _enforcement_applied = _enforce_disk_burn_facts(verification, state)

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

        # Only a LIVE ChaosBlade experiment can meaningfully contradict itself
        # here. If Layer 2 has already independently confirmed the fault is in
        # effect, a "passed but 0 affected" Layer 1 count is noise (e.g. a
        # residual / kubectl-native case) — re-verifying on it just spins
        # without terminating. Gate the gap on Layer 2 NOT having passed.
        _l1c_l2_status = verification.get("layer2", {}).get("status", "unknown")
        if (
            layer1.status == "passed"
            and layer1.affected_count == 0
            and _l1c_l2_status != "passed"
        ):
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

        # ---- Verify-Replan: unverified + L2 failed → replan to Phase 1 ----
        _level = verification.get("level", "")
        _l2_status = verification.get("layer2", {}).get("status", "unknown")

        if _level == "unverified" and _l2_status == "failed":
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
                # message scan would resurrect the stale UID into blade_uid and
                # misroute the next verification's Layer-1 (task-29848471).
                _retired_new = _retired_uids_from_residuals(residuals_cleaned)
                if _retired_new:
                    result_update["retired_blade_uids"] = list(
                        state.get("retired_blade_uids") or []
                    ) + _retired_new

                # 2. Build replan context with verifier findings
                _replan_ctx = _build_verify_replan_context(
                    verification, residuals_cleaned, verify_replan_count, skill_name,
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
                # Shared attribution reset (blade_uid included — the residue
                # was just destroyed and retired above): re-arms injection
                # method re-detection so the registry can re-attribute by
                # RECENCY if the replanned attempt switches carriers. The
                # message_count records the attribution epoch boundary so the
                # re-detection scan cannot resurrect pre-seam attempts
                # (task-5193538b).
                from chaos_agent.agent.nodes.execute.execute_loop import (
                    reset_attribution_state,
                )
                reset_attribution_state(
                    result_update,
                    message_count=len(state.get("messages") or [])
                    + len(result_update.get("messages") or []),
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
                    f"Verify-replan triggered: level=unverified, L2=failed"
                )
                # Clean up debug pods created by the verifier (same as
                # the normal finalize path — early return would skip it).
                await _cleanup_debug_pods(state, kubeconfig, task_id, result_update)
                await sync_to_store(state, result_update)
                return result_update

        result = {
            "task_id": task_id,
            "skill": skill_name,
            "blade_uid": blade_uid,
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
        l1_status = layer1.status
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
