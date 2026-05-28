"""se_snapshot node — capture pre-injection namespace state for side-effect diffing."""

import logging

from chaos_agent.agent.fault_spec import read_fault_spec
from chaos_agent.agent.nodes._side_effect_detectors import capture_snapshot
from chaos_agent.agent.nodes._store_sync import sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory

logger = logging.getLogger(__name__)


async def se_snapshot_node(state: AgentState) -> dict:
    """Capture pre-injection namespace state (pods + endpoints).

    Runs after baseline_capture, before injection. Writes the snapshot
    to state["se_snapshot"] for later comparison by se_detect.
    """
    spec = read_fault_spec(state)
    namespace = spec.namespace if spec else ""
    kubeconfig = state.get("kubeconfig", "")
    task_id = state.get("task_id", "")

    if not namespace:
        logger.debug("se_snapshot: no namespace, skipping")
        return {}

    tracker = get_tracker(task_id)
    tracker.start(StatusCategory.NODE, "se_snapshot", "Capturing pre-injection side-effect snapshot")

    try:
        snapshot = await capture_snapshot(namespace, kubeconfig)
    except Exception as e:
        logger.warning("se_snapshot: capture failed: %s", e)
        tracker.complete(f"Side-effect snapshot failed: {e}")
        return {}

    if not snapshot:
        tracker.complete("Side-effect snapshot: no data captured")
        return {}

    pod_count = len(snapshot.pods)
    ep_count = len(snapshot.endpoints)
    logger.info(
        "se_snapshot: captured %d pods, %d endpoints in ns=%s",
        pod_count, ep_count, namespace,
    )
    tracker.complete(
        f"Side-effect snapshot: {pod_count} pods, {ep_count} endpoints",
        {"pods": pod_count, "endpoints": ep_count, "namespace": namespace},
    )
    sync_node_status_to_session(
        state, "se_snapshot",
        f"Captured {pod_count} pods, {ep_count} endpoints in {namespace}",
        {"pods": pod_count, "endpoints": ep_count},
    )
    return {"se_snapshot": snapshot.to_dict()}
