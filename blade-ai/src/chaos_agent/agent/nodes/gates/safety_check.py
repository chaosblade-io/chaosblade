"""Safety check node: rule-based + LLM-assisted safety assessment."""

import logging

from langchain_core.messages import HumanMessage

from chaos_agent.agent.nodes.side_effect._conflict_check import (
    check_blade_conflicts,
)
from chaos_agent.tools.request_identity import build_request_fingerprint
from chaos_agent.agent.nodes.execute._kubeconfig_inject import _resolve_kubeconfig, sync_kubewiz_runtime
from chaos_agent.agent.nodes.store._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.prompts.reminder import wrap_system_reminder
from chaos_agent.agent.spec.intent_anchor import extract_explicit_node_anchor
from chaos_agent.agent.spec.safety_score import (
    compute_safety_score,
    maybe_escalate_status,
)
from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.target_guard import (
    canonicalise_kind,
    discover_names_by_labels,
    discover_owner_names,
    discover_pod_pvc_claims,
    discover_statefulset_pvc_claims,
    discover_workload_pvc_claims,
    freeze_approved_target_from_spec,
    WORKLOAD_TEMPLATE_SCOPES,
)
from chaos_agent.agent.target_guard.mechanism_writes import (
    derive_pvc_claims_from_writes,
    entries_beyond_victim,
    load_case_mechanism_writes,
    load_case_recovery_channel,
)
from chaos_agent.agent.target_guard.freeze import approved_from_dict
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.config.settings import settings
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
    if spec.scope != "deployment" or not spec.names:
        return (0, "")
    try:
        from chaos_agent.tools.kubectl_cli import exec_kubectl_raw

        result = await exec_kubectl_raw(
            "get",
            ["deployment", spec.names[0], "-n", spec.namespace, "-o", "jsonpath={.spec.replicas}"],
            kubeconfig=kubeconfig,
            timeout=5.0,
        )
        if result.exit_code != 0:
            return (0, "")
        replicas = int(result.stdout.strip() or 0)
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
    br_scope = state.get("blast_radius_scope") or ""
    if br_scope:
        context["blast_radius_scope"] = br_scope
        context["blast_radius_detail"] = state.get("blast_radius_detail") or ""

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

    # Cluster-wide blast radius: always escalate to at least "warning",
    # independent of safety_score_routing_enabled. A self-declared
    # cluster-wide execution scope is a fundamental safety signal that
    # should never be silently ignored.
    if br_scope == "cluster-wide":
        current = result.get("safety_status") or state.get("safety_status") or "safe"
        if current == "safe":
            br_detail = state.get("blast_radius_detail") or ""
            result["safety_status"] = "warning"
            existing_reason = result.get("safety_reason") or ""
            br_warning = (
                "Cluster-wide blast radius: execution will mutate resources "
                "beyond the target scope."
            )
            if br_detail:
                br_warning += f" {br_detail}"
            result["safety_reason"] = (
                f"{existing_reason}; {br_warning}" if existing_reason
                else br_warning
            )
            existing_detail = result.get("safety_checked_detail") or ""
            result["safety_checked_detail"] = (
                f"{existing_detail}, blast radius: cluster-wide"
            )
            logger.info(
                "blast_radius escalation: safe → warning (scope=%s)", br_scope,
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
    task_id = state.get("task_id", "") or ""
    # Single source of truth: read the FaultSpec written by entry-point
    # constructors or intent_clarification. Falls back to an empty spec
    # when missing so the no-target check below still fires uniformly.
    from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec
    spec = read_fault_spec(state) or FaultSpec()
    namespace = spec.namespace
    skill_name = read_active_skill_name(state)

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

    # 2. Skill existence — recoverable: feed back to agent_loop
    if not skill_name:
        tracker.start(
            StatusCategory.NODE,
            "safety_check",
            "No skill activated — routing back to agent_loop for activation",
            {"reason": "no_skill", "action": "retry"},
        )
        sync_node_status_to_session(state, "safety_check",
            "Safety check: no skill activated — returning to planner",
            detail={"safety_status": "retry", "reason": "no_skill"})
        retry_msg = HumanMessage(content=wrap_system_reminder(
            "No skill activated. Select the most appropriate skill from "
            "the Skill Index in your system prompt and call `activate_skill` now."
        ))
        messages = list(state.get("messages", []))
        messages.append(retry_msg)
        result = {
            "safety_status": "retry",
            "safety_reason": "No skill activated — returned to planner for activation",
            "messages": messages,
        }
        result = _attach_safety_score(result, spec, state)
        await sync_to_store(state, result)
        tracker.complete("Routed back to agent_loop for skill activation")
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
    sync_kubewiz_runtime(state)

    # E10 — optional deep K8s topology signal (replica count). Fetched
    # once here so all downstream return paths use the same signal.
    # No-op + (0, "") when the flag is off, kubeconfig missing, or the
    # query fails — never blocks safety_check.
    deep_signal: tuple[int, str] | None = None
    if settings.safety_score_topology_deep:
        await dispatch_node_message("safety_check", "Collecting topology signals (replica count)...\n\n")
        deep_signal = await _get_topology_deep_signal(spec, kubeconfig or "")

    # 4. Blade conflict detection — record result, do NOT early-return.
    # Health/feasibility checks below always run regardless of conflicts
    # so the user sees the full picture in the confirm card.
    conflict_status: str | None = None  # "confirm_required" | "warning" | None
    conflict_reason: str = ""
    conflict_uids: list = []
    conflict_extra: dict = {}
    # Weak note for UNDETERMINABLE experiments (cri/node scope, no
    # --namespace in Flag): they never enter conflict_uids and never
    # trigger a warning — surfaced here as a non-blocking note only.
    undet_note: str = ""
    from chaos_agent.transports import (
        PROFILE_K8S,
        is_kubewiz_channel,
        profile_of,
        resolve_channel_name,
    )
    # Conflict detection queries the cluster-side ChaosBlade CRDs, so it only
    # applies to k8s-side injection (kubeconfig / kubewiz_k8s → PROFILE_K8S).
    # Positive whitelist, NOT an exclusion of host scope: any future non-k8s
    # injection type is naturally out without touching this condition.
    # PROFILE_HOST and PROFILE_UNKNOWN are both intentionally excluded here:
    # host has no cluster CRD, and an unresolvable channel means we can't
    # reliably target any cluster — running `blade query k8s` against it would
    # just fail. Such an unresolvable channel only arises from a misconfig that
    # preflight (check_transport_config) blocks upstream, so skipping the
    # (advisory-only) conflict check here is safe rather than a silent gap.
    _is_k8s_injection = profile_of(resolve_channel_name(state)) == PROFILE_K8S
    # Whether we can actually reach the cluster to run the query (original
    # semantics): explicit kubeconfig or a kubewiz gateway channel.
    _cluster_reachable = bool(kubeconfig) or is_kubewiz_channel()
    if _is_k8s_injection and _cluster_reachable:
        await dispatch_node_message("safety_check", "Checking for conflicting experiments on the cluster...\n\n")
        scope = spec.scope
        fault_target = spec.fault_target
        action = spec.fault_action
        # Shared four-dimension request fingerprint (RequestFingerprint):
        # the same construction serves this pre-injection conflict query
        # and the create-reconcile gate's registration/probe paths.
        fingerprint = build_request_fingerprint(
            namespace=spec.namespace,
            labels=",".join(f"{k}={v}" for k, v in spec.labels.items()),
            names=",".join(spec.names),
            scope=scope,
            target=fault_target,
            action=action,
        )
        uids, conflict_details = await check_blade_conflicts(
            kubeconfig, task_id,
            **fingerprint.as_query_kwargs(),
        )
        if uids:
            conflict_uids = uids
            overlapping = [c for c in conflict_details if c.overlaps_target]
            same_action_same_target = [
                c for c in conflict_details
                if c.same_action_as_request and c.overlaps_target
            ]
            overlap_desc = "; ".join(c.overlap_reason for c in overlapping) if overlapping else ""

            # P1: FCAT conflict_escalation check for same-target same-action
            target_metadata = state.get("target_metadata") or {}
            if same_action_same_target:
                from chaos_agent.utils.fault_context import lookup_adaptations
                adaptations = lookup_adaptations(
                    scope, fault_target, action, target_metadata,
                    rule_type="conflict_escalation",
                )
                if adaptations:
                    active_same_action_uids = [c.uid for c in same_action_same_target]
                    conflict_status = "confirm_required"
                    conflict_reason = (
                        f"{len(same_action_same_target)} active experiment(s) with the SAME action "
                        f"({scope}-{fault_target}-{action}) already target this resource. "
                        f"Compound effects make individual verification impossible. "
                        f"Use --force-override to proceed anyway."
                    )
                    target_metadata["active_same_action_experiments"] = active_same_action_uids
                    conflict_extra = {"target_metadata": target_metadata}

            if conflict_status is None:
                conflict_status = "warning"
                if overlapping:
                    conflict_reason = (
                        f"{len(uids)} active ChaosBlade experiment(s) already exist in your namespace. "
                        f"WARNING: {len(overlapping)} of them target the SAME resource(s): "
                        f"{overlap_desc}. "
                        f"Overlapping injections on the same target produce unpredictable "
                        f"compound effects and cannot be individually verified. "
                        f"Consider destroying the conflicting experiment(s) first: "
                        f"{', '.join(c.uid for c in overlapping)}"
                    )
                else:
                    conflict_reason = (
                        f"{len(uids)} active ChaosBlade experiment(s) already exist in your namespace: "
                        f"{', '.join(uids[:5])}. "
                        f"No direct target overlap detected, but compound effects are possible. "
                        f"Consider destroying existing experiments first before proceeding."
                    )
        else:
            # No conflict candidates in the target namespace. Experiments
            # whose overlap CANNOT be determined (cri/node scope, no
            # --namespace in Flag — e.g. a stale cri mem-load CR) get a
            # non-blocking note instead of silence: isolation must not
            # degrade back into the inject-17617837 blind spot where
            # "no active experiments" was reported on a cluster that
            # actually had a live experiment.
            undet = [c for c in conflict_details if c.undeterminable]
            if undet:
                undet_note = (
                    f"{len(undet)} active experiment(s) carry no namespace info "
                    f"(cri/node scope) — overlap with your target cannot be "
                    f"determined: {', '.join(c.uid[:16] for c in undet[:5])}"
                )
                await dispatch_node_message("safety_check", f"Note: {undet_note}.\n\n")

    # 5. Target health pre-check — always runs regardless of conflicts.
    target_health_report: dict | None = None
    health_rejected = False
    if settings.target_health_check_enabled:
        try:
            await dispatch_node_message("safety_check", "Checking target health...\n\n")
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
            await dispatch_node_message("safety_check", "Assessing injection feasibility...\n\n")
            from chaos_agent.agent.spec.feasibility import assess_feasibility, FeasibilitySeverity

            feas = await assess_feasibility(spec, kubeconfig or "")
            if feas is not None:
                feasibility_report = feas.to_dict()
                logger.info(
                    "feasibility assessment: fault_target=%s severity=%s headroom=%.2f",
                    spec.fault_target, feas.severity.value, feas.headroom,
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
            "Safety check: confirm_required — conflicts on target"
        )
        sync_node_status_to_session(state, "safety_check",
            "Same-target same-action overlay detected (confirm_required)",
            detail={"safety_status": "confirm_required",
                    "conflict_count": len(conflict_uids)})
        result = {
            "safety_status": "confirm_required",
            "safety_reason": conflict_reason,
            "safety_checked_detail": f"namespace={namespace} compliant, {len(conflict_uids)} conflicting experiment(s) (same target, same action)",
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
            "safety_checked_detail": f"namespace={namespace} compliant, {len(conflict_uids)} active experiment(s) (different action)",
            "conflict_uids": conflict_uids,
        }
    elif _is_k8s_injection and not _cluster_reachable:
        tracker.complete("Safety checks passed (conflict check skipped: no cluster access)")
        sync_node_status_to_session(state, "safety_check",
            "Conflict check skipped (no cluster access)",
            detail={"safety_status": "warning"})
        result = {
            "safety_status": "warning",
            "safety_reason": "Conflict check skipped (no cluster access); cannot confirm whether active experiments exist on the cluster",
            "safety_checked_detail": f"namespace={namespace} compliant, conflict check skipped (no cluster access)",
            "conflict_uids": [],
        }
    else:
        tracker.complete("Safety checks passed")
        sync_node_status_to_session(state, "safety_check", "Safety checks passed",
            detail={"safety_status": "safe"})
        result = {
            "safety_status": "safe",
            "safety_reason": None,
            "safety_checked_detail": f"namespace={namespace} compliant, no conflicting experiments"
                + (f"; {undet_note}" if undet_note else ""),
            "conflict_uids": [],
        }

    # Always attach reports so TUI confirm card shows full picture.
    if target_health_report is not None:
        result["target_health_report"] = target_health_report
    if feasibility_report is not None:
        result["feasibility_report"] = feasibility_report

    # Freeze approved_target for screener comparison (mirrors confirmation_gate).
    kubeconfig = state.get("kubeconfig", "")
    await dispatch_node_message("safety_check", "Discovering the target Pod's owner...\n\n")
    owner_names = await discover_owner_names(
        spec.scope, spec.namespace, dict(spec.labels), kubeconfig,
        # names channel (generation anchor, case #39): a name-based pod
        # approval must also freeze the ownerReferences chain so the
        # drift guard recognises deleted-and-recreated successors.
        names=tuple(spec.names or ()),
    )
    # Resolve a LABEL selector (node AZ zone, or pod app labels) to its
    # concrete resource names so the drift guard can validate per-name batches
    # as an in-selector subset instead of rejecting the labels↔names cross as
    # spurious drift.
    resolved_names = await discover_names_by_labels(
        spec.scope, spec.namespace, dict(spec.labels), kubeconfig,
    )
    # Freeze the PVC claim names the approved target references — the anchor
    # for the drill-occupancy-vehicle exception (a resource-occupancy drill
    # creates a behaviourless pod claiming the SAME PVC; the screener
    # validates the occupant's claims against this frozen set).
    # B79 (case #39-R): claim discovery dispatches by scope. The LLM's
    # scope extraction is a free variable (the same intent read as "pod"
    # one run and "deployment" the next); the frozen anchor must not be.
    pvc_claims: tuple[str, ...] = ()
    _scope_l = (spec.scope or "").strip().lower()
    if _scope_l in ("pod", "container"):
        pod_identities = tuple(spec.names) if spec.names else resolved_names
        pvc_claims = await discover_pod_pvc_claims(
            spec.namespace, pod_identities, kubeconfig,
        )
    elif _scope_l in WORKLOAD_TEMPLATE_SCOPES:
        # Template channel: the claimName authored in the pod template is
        # the same string every replica mounts, so a workload approval
        # freezes the same whitelist a pod approval of its pods would.
        pvc_claims = await discover_workload_pvc_claims(
            _scope_l, spec.namespace, tuple(spec.names or ()),
            dict(spec.labels or {}), kubeconfig,
        )
    elif _scope_l == "statefulset":
        # STS channel: claims are per-replica instances of
        # volumeClaimTemplates, never the template names (a template name
        # is not a PVC — whitelisting it would admit ghost entries). Ground
        # truth is what the live pods mount; scaled-to-zero freezes empty
        # and the occupant exception stays refused (fail closed).
        pvc_claims = await discover_statefulset_pvc_claims(
            spec.namespace, tuple(spec.names or ()),
            dict(spec.labels or {}), kubeconfig,
        )
    # Case-manifest mechanism writes: deterministic load at case
    # settlement. Code re-reads the SAME case file the Agent showed the
    # user (skill_name + spec.case_resource_path), so the Agent's reading
    # of the case prose — a ToolMessage the LLM produced — is never an
    # authorization input. Empty for every case without a manifest
    # (behaviour unchanged); parse failures also yield empty (fail closed
    # at the guard, where the rejection carries manifest attribution).
    mechanism_entries = load_case_mechanism_writes(
        skill_name, getattr(spec, "case_resource_path", "") or "",
    )
    # Case-file recovery-route legislation (D3 source 1, openspec
    # faultdrill-cr-channel): the SAME deterministic re-read discipline as
    # the manifest — code reads the settled case file, so neither the
    # Agent's prose reading nor any LLM planning declaration is a routing
    # input. Frozen into the snapshot so the CR-channel route gate consults
    # the declaration BEFORE its blade verb-vocabulary proxy (the proxy is
    # a temporary M2 stand-in; run8 inject-2a8cd99a proved it misroutes a
    # k8s-native mechanism whose taxonomy verbs land in the blade
    # vocabulary — NXDOMAIN target=network action=dns). Empty for every
    # case without the declaration (gate behaviour unchanged).
    recovery_channel = load_case_recovery_channel(
        skill_name, getattr(spec, "case_resource_path", "") or "",
    )
    # #39 time-dimension gap: live claim discovery only finds PVCs that
    # ALREADY exist; a #38-shaped case applies its PVC during execution.
    # The in-band PVC is already legislated in mechanism_writes (an
    # unlegislated PVC write is rejected by the guard itself), so the
    # WRITE-set is the claim anchor's time-proof half — DERIVED, never
    # re-declared: a hand-copied pvc_claims list could drift from the
    # write it shadows and rode no confirmation card the human saw.
    manifest_pvc_claims = derive_pvc_claims_from_writes(mechanism_entries)
    if manifest_pvc_claims:
        # Union, not replace: for a target that EXISTS the live channels
        # are ground truth and the manifest adds nothing; for a target the
        # drill creates, the manifest is the only source. Unioning keeps
        # both worlds correct and costs nothing when the manifest is empty.
        pvc_claims = tuple(sorted(set(pvc_claims) | set(manifest_pvc_claims)))
        logger.info(
            "derived in-band pvc_claims %s from mechanism writes → frozen anchor now %s",
            list(manifest_pvc_claims), list(pvc_claims),
        )
    # D4 invariant, graph-side half: a manifest carrying entries beyond
    # the victim coverage is a WIDENED contract. Freezing alone must NOT
    # complete its authorization — stamp the snapshot pending until a
    # knowing human clears it at the gate, AND force the confirm route so
    # every channel (interactive and unattended alike) actually reaches
    # the card/boundary decision instead of silently sliding past the
    # gate via ``needs_confirmation=False`` auto-execute routing.
    # Probe-freeze evaluates the coverage predicate on the exact shape
    # the guard will later enforce (freeze is pure — no cluster I/O; the
    # discovery queries above already ran).
    _widening: tuple = ()
    if mechanism_entries:
        _probe = approved_from_dict(freeze_approved_target_from_spec(
            spec,
            owner_names=owner_names,
            resolved_names=resolved_names,
            pvc_claims=pvc_claims,
            mechanism_entries=mechanism_entries,
            recovery_channel=recovery_channel,
        ) or {})
        if _probe is not None:
            _widening = entries_beyond_victim(_probe)
    result["approved_target"] = freeze_approved_target_from_spec(
        spec,
        owner_names=owner_names,
        resolved_names=resolved_names,
        pvc_claims=pvc_claims,
        mechanism_entries=mechanism_entries,
        recovery_channel=recovery_channel,
        widening_pending_approval=bool(_widening),
    )
    if _widening:
        # Layer-2 pre-positioning: interactive channels were already
        # True (no behaviour change); unattended channels now pause at
        # the gate where the boundary decision runs. Without this the
        # ``safe + needs_confirmation=False`` route skips the gate
        # entirely and the execute_loop sentinel becomes the only guard.
        result["needs_confirmation"] = True
        logger.info(
            "safety_check: write-set contract widened beyond victim coverage "
            "(%d manifest entries) — forcing confirmation gate", len(_widening),
        )

    # B76 review F (probe_b76_round6.py) — anchor-kind integrity: the
    # frozen identity must still name the resource KIND the user
    # explicitly anchored in the entry-point text. The pre-filled anchor
    # (intent_anchor → from_cli_nl / from_http_request NL) can be silently
    # replaced downstream: extract_planning_metadata's scope override —
    # anti-monster legislation that correctly clears names+labels when the
    # blade skill declares a different scope — treats a pre-filled anchor
    # as "old scope residue", after which agent_loop's lazy derivation
    # rebuilds a consistent-but-DIFFERENT identity (user asked for node X
    # down; the plan delivers pod-cpu-fullload on an unrelated pod, no
    # user touchpoint in CLI mode — probe F1-F3). Guarding here, at the
    # last identity consumer before the freeze, covers EVERY upstream
    # writer channel (scope override, lazy derivation, future ones) —
    # defense-in-depth with ④'s entry-point check.
    #
    # Routing: confirm_required (not rejected) — TUI renders the card
    # where the human sees anchor vs plan; CLI without --force-override
    # gets the gate's explicit rejection ("Add --force-override to
    # proceed" = the user knowingly accepting the retarget). A status
    # that is already rejected/confirm_required keeps its own reason
    # untouched: those paths already reach the gate/terminal state and
    # their reason is the more specific diagnosis.
    if result.get("safety_status") not in ("rejected", "confirm_required"):
        _anchors = extract_explicit_node_anchor(spec.user_description or "")
        # node and host are the SAME machine seen from two angles — a
        # node-level drill routinely lands on host scope (Node_CPU cases
        # run systemd-run payloads on the host); an anchored-node intent
        # under host scope is a correct domain mapping, not a retarget.
        # Everything else (pod / deployment / service / ...) is a different
        # KIND of resource — that is the F shape.
        if _anchors and canonicalise_kind(spec.scope or "") not in ("node", "host"):
            _anchor_reason = (
                "Target does not match the user's request: the request explicitly "
                f"names node(s) {list(_anchors)}, but the planned fault targets "
                f"{spec.scope} {list(spec.names) or dict(spec.labels) or '<unresolved>'}. "
                "Re-plan against the named node(s), or let the user decide "
                "explicitly."
            )
            result["safety_status"] = "confirm_required"
            result["needs_confirmation"] = True
            if result.get("safety_reason"):
                result["safety_reason"] = (
                    f"{_anchor_reason} Also: {result['safety_reason']}"
                )
            else:
                result["safety_reason"] = _anchor_reason
            logger.info(
                "safety_check: anchor-kind mismatch — user anchored node(s) %s "
                "but plan targets %s %s; forcing confirmation gate",
                list(_anchors), spec.scope, list(spec.names),
            )

    result = _attach_safety_score(result, spec, state, deep_signal)
    await sync_to_store(state, result)
    return result
