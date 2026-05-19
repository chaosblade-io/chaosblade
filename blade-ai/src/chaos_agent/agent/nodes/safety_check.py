"""Safety check node: rule-based + LLM-assisted safety assessment."""

import logging

from chaos_agent.agent.nodes._conflict_check import check_blade_conflicts
from chaos_agent.agent.nodes._kubeconfig_inject import _resolve_kubeconfig
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.errors import FailureReason
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)


async def safety_check(state: AgentState) -> dict:
    """Perform safety checks before fault injection.

    Rule-based checks (deterministic, no LLM):
    1. Namespace blacklist
    2. Target existence (must be verified by agent_loop already)
    3. Conflict detection (active experiments on cluster)
    4. Skill existence

    Returns updated safety_status and safety_reason.
    """
    task_id = state.get("task_id", "unknown")
    target = state.get("target") or {}
    namespace = target.get("namespace", "")
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
            "failure_reason": f"{FailureReason.SAFETY_REJECTED.value}: Namespace '{namespace}' is in the safety blacklist",
        }
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
            "failure_reason": f"{FailureReason.PREREQUISITE_FAILED.value}: No skill activated before safety check",
        }
        await sync_to_store(state, result)
        return result

    # 3. Basic target validation
    if not target:
        tracker.fail("No target specified")
        sync_node_status_to_session(state, "safety_check",
            "Safety check rejected: no target specified",
            detail={"safety_status": "rejected", "reason": "no_target"})
        result = {
            "safety_status": "rejected",
            "safety_reason": "No target specified",
            "failure_reason": f"{FailureReason.PREREQUISITE_FAILED.value}: No target specified",
        }
        await sync_to_store(state, result)
        return result

    # 4. Blade conflict detection (enhanced with target overlap)
    kubeconfig = _resolve_kubeconfig(state)
    if kubeconfig:
        namespace = target.get("namespace", "")
        raw_labels = state.get("params", {}).get("labels", "") or target.get("labels", "")
        # Normalize labels to comma-separated "k=v" string.
        # target["labels"] may be a dict {"app": "accounting"} from CLI runner,
        # while params["labels"] is already a string like "app=accounting".
        if isinstance(raw_labels, dict):
            labels = ",".join(f"{k}={v}" for k, v in raw_labels.items())
        else:
            labels = str(raw_labels) if raw_labels else ""
        target_names = ",".join(target.get("names", []))

        # Build scope-target-action for action compatibility check (P1)
        scope = state.get("blade_scope", "")
        blade_target = state.get("blade_target", "")
        action = state.get("blade_action", "")
        request_sta = f"{scope}-{blade_target}-{action}" if scope and blade_target and action else ""

        uids, conflict_details = await check_blade_conflicts(
            kubeconfig, task_id,
            namespace=namespace, labels=labels,
            target_names=target_names,
            request_scope_target_action=request_sta,
        )
        if uids:
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
                    # Escalate to confirm_required (stronger than warning)
                    active_same_action_uids = [c.uid for c in same_action]
                    tracker.complete(
                        f"Safety check: confirm_required — {len(same_action)} same-action experiment(s) "
                        f"on target (FCAT P1 escalation)"
                    )
                    sync_node_status_to_session(state, "safety_check",
                        "Same-target same-action overlay detected (confirm_required)",
                        detail={"safety_status": "confirm_required",
                                "reason": "same_target_same_action",
                                "same_action_uids": active_same_action_uids,
                                "conflict_count": len(uids)})
                    # Populate target_metadata.active_same_action_experiments for downstream
                    if target_metadata is None:
                        target_metadata = {}
                    target_metadata["active_same_action_experiments"] = active_same_action_uids
                    result = {
                        "safety_status": "confirm_required",
                        "safety_reason": (
                            f"{len(same_action)} active experiment(s) with the SAME action "
                            f"({scope}-{blade_target}-{action}) already target this resource. "
                            f"Compound effects make individual verification impossible. "
                            f"Use --force-override to proceed anyway."
                        ),
                        "conflict_uids": uids,
                        "target_metadata": target_metadata,
                    }
                    await sync_to_store(state, result)
                    return result

            # Active experiments detected → warning (including target overlap)
            if overlapping:
                tracker.complete(
                    f"Safety checks passed with warning: {len(uids)} active experiment(s), "
                    f"{len(overlapping)} target the same resource(s)"
                )
                sync_node_status_to_session(state, "safety_check",
                    f"Safety checks passed with warning: {len(overlapping)} experiment(s) "
                    f"target overlap",
                    detail={"safety_status": "warning", "reason": "target_overlap",
                            "overlap_count": len(overlapping),
                            "overlap_uids": [c.uid for c in overlapping],
                            "conflict_count": len(uids),
                            "conflict_uids": uids[:5]})
                result = {
                    "safety_status": "warning",
                    "safety_reason": (
                        f"{len(uids)} active ChaosBlade experiment(s) already exist on this cluster. "
                        f"WARNING: {len(overlapping)} of them target the SAME resource(s): "
                        f"{overlap_desc}. "
                        f"Overlapping injections on the same target produce unpredictable "
                        f"compound effects and cannot be individually verified. "
                        f"Consider destroying the conflicting experiment(s) first: "
                        f"{', '.join(c.uid for c in overlapping)}"
                    ),
                    "conflict_uids": uids,
                }
            else:
                # Namespace-level overlap only → warning (existing behavior)
                tracker.complete(
                    f"Safety checks passed with warning: {len(uids)} active experiment(s) detected"
                )
                sync_node_status_to_session(state, "safety_check",
                    f"Safety checks passed with warning: {len(uids)} active experiment(s)",
                    detail={"safety_status": "warning", "conflict_count": len(uids),
                            "conflict_uids": uids[:5]})
                result = {
                    "safety_status": "warning",
                    "safety_reason": (
                        f"{len(uids)} active ChaosBlade experiment(s) already exist on this cluster: "
                        f"{', '.join(uids[:5])}. "
                        f"No direct target overlap detected, but compound effects are possible. "
                        f"Consider destroying existing experiments first before proceeding."
                    ),
                    "conflict_uids": uids,
                }
            await sync_to_store(state, result)
            return result

    tracker.complete("Safety checks passed")
    sync_node_status_to_session(state, "safety_check", "Safety checks passed",
        detail={"safety_status": "safe"})
    result = {
        "safety_status": "safe",
        "safety_reason": None,
        "conflict_uids": [],
    }
    await sync_to_store(state, result)
    return result
