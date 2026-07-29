"""se_snapshot node — capture pre-injection namespace state for side-effect diffing."""

import logging

from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.spec.feasibility import profile_for_spec
from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.nodes.side_effect._side_effect_detectors import resolve_observer
from chaos_agent.agent.nodes.store._store_sync import sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.transports import PROFILE_K8S

logger = logging.getLogger(__name__)


async def se_snapshot_node(state: AgentState) -> dict:
    """Capture pre-injection state (pods+endpoints on k8s, host picture on host).

    Runs after baseline_capture, before injection. Writes the snapshot
    to state["se_snapshot"] for later comparison by se_detect. The resolved
    observer owns whether a namespace is required and how the snapshot reads,
    so this node carries no ``if profile == ...`` branch.
    """
    spec = read_fault_spec(state)
    namespace = spec.namespace if spec else ""
    kubeconfig = state.get("kubeconfig", "")
    task_id = state.get("task_id", "")

    profile = profile_for_spec(spec) if spec else PROFILE_K8S
    observer = resolve_observer(profile)
    if observer is None:
        logger.debug("se_snapshot: no observer for profile=%s, skipping", profile)
        return {}
    if not observer.can_capture(spec):
        logger.debug("se_snapshot: profile=%s cannot capture for this spec, skipping", profile)
        return {}

    tracker = get_tracker(task_id)
    tracker.start(StatusCategory.NODE, "se_snapshot", "Capturing pre-injection side-effect snapshot")
    await dispatch_node_message("se_snapshot", "正在采集注入前副作用快照...\n\n")

    try:
        snapshot = await observer.capture_base_snapshot(spec, kubeconfig, task_id=task_id)
    except Exception as e:
        logger.warning("se_snapshot: capture failed: %s", e)
        tracker.complete(f"Side-effect snapshot failed: {e}")
        return {}

    if not snapshot:
        tracker.complete("Side-effect snapshot: no data captured")
        return {}

    phrase, metrics = observer.summarize(snapshot)
    logger.info("se_snapshot: captured %s (profile=%s)", phrase, profile)
    await dispatch_node_message("se_snapshot", f"副作用快照: {phrase}\n\n")
    # ``namespace`` is spec-level context (not profile semantics), so it is
    # surfaced alongside the profile-specific metrics whenever present — this
    # keeps the k8s tracker/session detail on par with the pre-seam behaviour
    # (which located the snapshot by namespace) while host (no namespace)
    # naturally omits it.
    detail = {**metrics, "profile": profile}
    where = ""
    if namespace:
        detail["namespace"] = namespace
        where = f" in {namespace}"
    tracker.complete(
        f"Side-effect snapshot: {phrase}",
        detail,
    )
    sync_node_status_to_session(
        state, "se_snapshot",
        f"Captured {phrase}{where}",
        metrics,
    )
    return {"se_snapshot": snapshot.to_dict()}
