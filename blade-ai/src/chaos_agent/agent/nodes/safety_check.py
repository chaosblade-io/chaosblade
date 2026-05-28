"""Safety check node: rule-based + LLM-assisted safety assessment."""

import logging

from chaos_agent.agent.nodes._conflict_check import check_blade_conflicts
from chaos_agent.agent.nodes._kubeconfig_inject import _resolve_kubeconfig
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.safety_score import (
    compute_safety_score,
    maybe_escalate_status,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.target_guard import freeze_approved_target
from chaos_agent.config.settings import settings
from chaos_agent.agent.state_helpers import fail_state
from chaos_agent.agent.verdict import FailureCategory
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)


async def _get_topology_deep_signal(spec, kubeconfig: str) -> tuple[int, str]:
    """Optional async kubectl query for deployment replica count.

    Only applies to deployment scope with a named target. Returns
    (0, "") on any error so safety_check never fails because of it.
    """
    if not kubeconfig or spec.scope != "deployment" or not spec.names:
        return (0, "")
    try:
        import asyncio as _asyncio
        cmd = [
            "kubectl", "--kubeconfig", kubeconfig,
            "get", "deployment", spec.names[0],
            "-n", spec.namespace,
            "-o", "jsonpath={.spec.replicas}",
        ]
        proc = await _asyncio.create_subprocess_exec(
            *cmd,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=5.0)
        except _asyncio.TimeoutError:
            proc.kill()
            return (0, "")
        if proc.returncode != 0:
            return (0, "")
        replicas = int(stdout.decode().strip() or 0)
        if replicas == 1:
            return (20, "deployment has 1 replica (SPOF)")
        if replicas == 2:
            return (10, "deployment has 2 replicas (limited redundancy)")
        return (0, "")
    except Exception as e:
        logger.debug("topology deep signal failed: %s", e)
        return (0, "")


def _attach_safety_score(
    result: dict,
    spec,
    state: AgentState,
    deep_signal: tuple[int, str] | None = None,
) -> dict:
    """Compute safety_score and merge into result; escalate status if enabled.

    Always advisory: never downgrades. Escalation is gated by
    ``settings.safety_score_routing_enabled``.
    """
    context = {
        "conflict_uids": result.get("conflict_uids") or state.get("conflict_uids") or [],
        "pipeline_attempt": state.get("pipeline_attempt") or 0,
    }
    if deep_signal is not None:
        context["topology_deep_signal"] = deep_signal

    score = compute_safety_score(spec, context)
    result["safety_score"] = score.to_dict()

    if settings.safety_score_routing_enabled:
        current = result.get("safety_status") or state.get("safety_status") or "safe"
        new = maybe_escalate_status(
            current,
            score.overall,
            warning_thresh=settings.safety_score_warning_threshold,
            confirm_thresh=settings.safety_score_confirm_threshold,
        )
        if new != current:
            result["safety_status"] = new
            logger.info(
                "safety_score escalation: %s → %s (overall=%d)",
                current, new, score.overall,
            )
    return result


async def safety_check(state: AgentState) -> dict:
    """Perform safety checks before fault injection.

    Rule-based checks (deterministic, no LLM):
    1. Namespace blacklist
    2. Target existence (must be verified by agent_loop already)
    3. Conflict detection (active experiments on cluster)
    4. Skill existence
    5. Target health pre-check (optional, gated by settings)

    E10 — also attaches a multi-dimensional ``safety_score`` dict to
    every return path (blast_radius / frequency / time / topology +
    weighted overall + level). When
    ``settings.safety_score_routing_enabled`` is on, a high overall
    can upgrade ``safety_status`` from safe → warning → confirm_required.

    Returns updated safety_status, safety_reason, and safety_score.
    """
    task_id = state.get("task_id", "unknown")
    # Single source of truth: read the FaultSpec written by entry-point
    # constructors or intent_clarification. Falls back to an empty spec
    # when missing so the no-target check below still fires uniformly.
    from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec
    spec = read_fault_spec(state) or FaultSpec()
    namespace = spec.namespace
    skill_name = state.get("skill_name", "")

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "safety_check",
        f"Running safety checks for skill '{skill_name}' in namespace '{namespace}'",
        {"skill_name": skill_name, "namespace": namespace},
    )

    # 1. Namespace blacklist
    blacklist = settings.blacklist_namespaces
    if namespace in blacklist:
        tracker.fail(f"Namespace '{namespace}' is in the safety blacklist")
        sync_node_status_to_session(state, "safety_check",
            f"Safety check rejected: namespace '{namespace}' is blacklisted",
            detail={"safety_status": "rejected", "reason": "namespace_blacklisted"})
        result = {
            "safety_status": "rejected",
            "safety_reason": f"Namespace '{namespace}' is in the safety blacklist",
            **fail_state(FailureCategory.SAFETY_REJECTED, f"namespace={namespace}"),
        }
        result = _attach_safety_score(result, spec, state)
        await sync_to_store(state, result)
        return result

    # 2. Skill existence
    if not skill_name:
        tracker.fail("No skill activated")
        sync_node_status_to_session(state, "safety_check",
            "Safety check rejected: no skill activated",
            detail={"safety_status": "rejected", "reason": "no_skill"})
        result = {
            "safety_status": "rejected",
            "safety_reason": "No skill activated",
            **fail_state(FailureCategory.PREREQUISITE_FAILED, "no skill activated"),
        }
        result = _attach_safety_score(result, spec, state)
        await sync_to_store(state, result)
        return result

    # 3. Basic target validation — spec must at least carry a scope
    # (cluster-scoped resources can have empty namespace; pod/container
    # need namespace via the FaultSpec.is_complete contract).
    if not spec.scope:
        tracker.fail("No target specified")
        sync_node_status_to_session(state, "safety_check",
            "Safety check rejected: no target specified",
            detail={"safety_status": "rejected", "reason": "no_target"})
        result = {
            "safety_status": "rejected",
            "safety_reason": "No target specified",
            **fail_state(FailureCategory.PREREQUISITE_FAILED, "no target specified"),
        }
        result = _attach_safety_score(result, spec, state)
        await sync_to_store(state, result)
        return result

    # Resolve kubeconfig once — used by both the E10 deep topology
    # signal (optional) and the blade conflict detection below.
    kubeconfig = _resolve_kubeconfig(state)

    # E10 — optional deep K8s topology signal (replica count). Fetched
    # once here so all downstream return paths use the same signal.
    # No-op + (0, "") when the flag is off, kubeconfig missing, or the
    # query fails — never blocks safety_check.
    deep_signal: tuple[int, str] | None = None
    if settings.safety_score_topology_deep:
        deep_signal = await _get_topology_deep_signal(spec, kubeconfig or "")

    # 4. Blade conflict detection — record result, do NOT early-return.
    # Health/feasibility checks below always run regardless of conflicts
    # so the user sees the full picture in the confirm card.
    conflict_status: str | None = None  # "confirm_required" | "warning" | None
    conflict_reason: str = ""
    conflict_uids: list = []
    conflict_extra: dict = {}
    if kubeconfig:
        namespace = spec.namespace
        labels = ",".join(f"{k}={v}" for k, v in spec.labels.items())
        target_names = ",".join(spec.names)

        scope = spec.scope
        blade_target = spec.blade_target
        action = spec.blade_action
        request_sta = f"{scope}-{blade_target}-{action}" if scope and blade_target and action else ""

        uids, conflict_details = await check_blade_conflicts(
            kubeconfig, task_id,
            namespace=namespace, labels=labels,
            target_names=target_names,
            request_scope_target_action=request_sta,
        )
        if uids:
            conflict_uids = uids
            overlapping = [c for c in conflict_details if c.overlaps_target]
            same_action = [c for c in conflict_details if c.same_action_as_request]
            overlap_desc = "; ".join(c.overlap_reason for c in overlapping) if overlapping else ""

            # P1: FCAT conflict_escalation check for same-target same-action
            target_metadata = state.get("target_metadata") or {}
            if same_action and target_metadata is not None:
                from chaos_agent.utils.fault_context import lookup_adaptations
                adaptations = lookup_adaptations(
                    scope, blade_target, action, target_metadata,
                    rule_type="conflict_escalation",
                )
                if adaptations:
                    active_same_action_uids = [c.uid for c in same_action]
                    conflict_status = "confirm_required"
                    conflict_reason = (
                        f"{len(same_action)} active experiment(s) with the SAME action "
                        f"({scope}-{blade_target}-{action}) already target this resource. "
                        f"Compound effects make individual verification impossible. "
                        f"Use --force-override to proceed anyway."
                    )
                    if target_metadata is None:
                        target_metadata = {}
                    target_metadata["active_same_action_experiments"] = active_same_action_uids
                    conflict_extra = {"target_metadata": target_metadata}

            if conflict_status is None:
                conflict_status = "warning"
                if overlapping:
                    conflict_reason = (
                        f"{len(uids)} active ChaosBlade experiment(s) already exist on this cluster. "
                        f"WARNING: {len(overlapping)} of them target the SAME resource(s): "
                        f"{overlap_desc}. "
                        f"Overlapping injections on the same target produce unpredictable "
                        f"compound effects and cannot be individually verified. "
                        f"Consider destroying the conflicting experiment(s) first: "
                        f"{', '.join(c.uid for c in overlapping)}"
                    )
                else:
                    conflict_reason = (
                        f"{len(uids)} active ChaosBlade experiment(s) already exist on this cluster: "
                        f"{', '.join(uids[:5])}. "
                        f"No direct target overlap detected, but compound effects are possible. "
                        f"Consider destroying existing experiments first before proceeding."
                    )

    # 5. Target health pre-check — always runs regardless of conflicts.
    target_health_report: dict | None = None
    health_rejected = False
    if settings.target_health_check_enabled:
        try:
            from chaos_agent.agent.target_health import assess_target_health

            target_payload = {
                "namespace": spec.namespace,
                "names": list(spec.names),
                "labels": dict(spec.labels),
                "resource_type": spec.scope,
            }
            health = await assess_target_health(spec.scope, target_payload, kubeconfig or "")
            target_health_report = health.to_dict()
            logger.info(
                "target health pre-check: scope=%s overall=%s issues=%d",
                spec.scope, health.overall.value, len(health.issues),
            )
            tracker.update(
                f"Target health: {health.overall.value} ({len(health.issues)} issue(s))",
                {"debug": True, "target_health_report": target_health_report},
            )
            sync_node_status_to_session(state, "safety_check",
                f"Target health pre-check: {health.overall.value}, "
                f"{len(health.issues)} issue(s)",
                detail={"target_health_report": target_health_report})
            if health.is_blocking() and settings.target_health_check_block_on_blocker:
                health_rejected = True
        except Exception as exc:  # noqa: BLE001 — never fatal
            logger.warning("target health pre-check failed (non-fatal): %s", exc)

    # 6. Injection feasibility assessment — always runs regardless of conflicts.
    feasibility_report: dict | None = None
    feas_rejected = False
    if settings.feasibility_check_enabled:
        try:
            from chaos_agent.agent.feasibility import assess_feasibility, FeasibilitySeverity

            feas = await assess_feasibility(spec, kubeconfig or "")
            if feas is not None:
                feasibility_report = feas.to_dict()
                logger.info(
                    "feasibility assessment: blade_target=%s severity=%s headroom=%.2f",
                    spec.blade_target, feas.severity.value, feas.headroom,
                )
                tracker.update(
                    f"Feasibility: {feas.severity.value} (headroom={feas.headroom:.2f})",
                    {"debug": True, "feasibility_report": feasibility_report},
                )
                sync_node_status_to_session(state, "safety_check",
                    f"Feasibility assessment: {feas.severity.value}, "
                    f"headroom={feas.headroom:.2f}, {feas.message}",
                    detail={"feasibility_report": feasibility_report})
                if (
                    feas.severity == FeasibilitySeverity.IMPOSSIBLE
                    and settings.feasibility_check_block_on_impossible
                ):
                    feas_rejected = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("feasibility check failed (non-fatal): %s", exc)

    # 7. Determine final status: rejected > confirm_required > warning > safe
    if health_rejected or feas_rejected:
        reject_reasons = []
        if health_rejected:
            reject_reasons.append(
                f"Target health blocker: {health.summary()}. "
                f"Set BLADE_AI_TARGET_HEALTH_CHECK_BLOCK=0 to override."
            )
        if feas_rejected:
            reject_reasons.append(
                f"Injection not feasible: {feas.message}. {feas.recommendation}"
            )
        if conflict_reason:
            reject_reasons.append(f"Also: {conflict_reason}")
        tracker.fail("; ".join(reject_reasons[:1]))
        sync_node_status_to_session(state, "safety_check",
            f"Safety check rejected: {reject_reasons[0][:80]}",
            detail={"safety_status": "rejected"})
        result = {
            "safety_status": "rejected",
            "safety_reason": " ".join(reject_reasons),
            "conflict_uids": conflict_uids,
            **conflict_extra,
        }
    elif conflict_status == "confirm_required":
        tracker.complete(
            f"Safety check: confirm_required — conflicts on target"
        )
        sync_node_status_to_session(state, "safety_check",
            "Same-target same-action overlay detected (confirm_required)",
            detail={"safety_status": "confirm_required",
                    "conflict_count": len(conflict_uids)})
        result = {
            "safety_status": "confirm_required",
            "safety_reason": conflict_reason,
            "conflict_uids": conflict_uids,
            **conflict_extra,
        }
    elif conflict_status == "warning":
        tracker.complete(
            f"Safety checks passed with warning: {len(conflict_uids)} active experiment(s)"
        )
        sync_node_status_to_session(state, "safety_check",
            f"Safety checks passed with warning: {len(conflict_uids)} active experiment(s)",
            detail={"safety_status": "warning",
                    "conflict_count": len(conflict_uids),
                    "conflict_uids": conflict_uids[:5]})
        result = {
            "safety_status": "warning",
            "safety_reason": conflict_reason,
            "conflict_uids": conflict_uids,
        }
    else:
        tracker.complete("Safety checks passed")
        sync_node_status_to_session(state, "safety_check", "Safety checks passed",
            detail={"safety_status": "safe"})
        result = {
            "safety_status": "safe",
            "safety_reason": None,
            "conflict_uids": [],
        }

    # Always attach reports so TUI confirm card shows full picture.
    if target_health_report is not None:
        result["target_health_report"] = target_health_report
    if feasibility_report is not None:
        result["feasibility_report"] = feasibility_report

    # Freeze approved_target for screener comparison (mirrors confirmation_gate).
    result["approved_target"] = freeze_approved_target(
        target={"namespace": spec.namespace, "names": list(spec.names),
                "labels": dict(spec.labels), "resource_type": spec.scope},
        params=dict(spec.params),
        blade_scope=spec.scope,
        blade_target=spec.blade_target,
        blade_action=spec.blade_action,
    )

    result = _attach_safety_score(result, spec, state, deep_signal)
    await sync_to_store(state, result)
    return result
