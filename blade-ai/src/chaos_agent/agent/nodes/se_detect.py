"""se_detect node — post-verification side-effect detection via snapshot diff."""

import asyncio
import logging

from chaos_agent.agent.fault_spec import read_fault_spec
from chaos_agent.agent.nodes._side_effect_detectors import (
    DetectionContext,
    SideEffectSnapshot,
    fetch_post_inject_state,
    run_all_detectors,
)
from chaos_agent.agent.nodes._store_sync import sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory

logger = logging.getLogger(__name__)

_DETECT_TIMEOUT = 15.0


async def se_detect_node(state: AgentState) -> dict:
    """Query current namespace state, diff against pre-injection snapshot.

    Writes detected incremental side-effects into
    verification["side_effects"]. Runs after verifier_loop completes.
    """
    spec = read_fault_spec(state)
    namespace = spec.namespace if spec else ""
    kubeconfig = state.get("kubeconfig", "")
    injection_start = state.get("injection_start_time", "")
    task_id = state.get("task_id", "")

    if not namespace:
        logger.debug("se_detect: no namespace, skipping")
        return {}
    if not injection_start:
        logger.debug("se_detect: no injection_start_time, skipping")
        return {}

    tracker = get_tracker(task_id)
    tracker.start(StatusCategory.NODE, "se_detect", "Detecting post-injection side effects")

    snapshot_dict = state.get("se_snapshot")
    snapshot = SideEffectSnapshot.from_dict(snapshot_dict) if snapshot_dict else None

    target_names = list(spec.names) if spec else []

    try:
        after = await asyncio.wait_for(
            fetch_post_inject_state(namespace, kubeconfig, injection_start, target_names),
            timeout=_DETECT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("se_detect: fetch_post_inject_state timed out after %.0fs", _DETECT_TIMEOUT)
        tracker.complete("Side-effect detection timed out")
        return {}
    except Exception as e:
        logger.warning("se_detect: fetch failed: %s", e)
        tracker.complete(f"Side-effect detection fetch failed: {e}")
        return {}

    ctx = DetectionContext(
        namespace=namespace,
        target_names=target_names,
        scope=spec.scope if spec else "",
        kubeconfig=kubeconfig,
        injection_start_time=injection_start,
        task_id=task_id,
    )

    try:
        detected = run_all_detectors(snapshot, after, ctx)
    except Exception as e:
        logger.warning("se_detect: run_all_detectors failed: %s", e)
        tracker.complete(f"Side-effect detectors failed: {e}")
        return {}

    if not detected:
        logger.info("se_detect: no incremental side-effects detected")
        tracker.complete("No incremental side-effects detected")
        sync_node_status_to_session(
            state, "se_detect", "No side-effects detected",
        )
        return {}

    total_items = sum(len(v) for v in detected.values())
    categories = list(detected.keys())
    logger.info(
        "se_detect: detected %d side-effect(s) across %d categories: %s",
        total_items,
        len(categories),
        categories,
    )
    tracker.complete(
        f"Detected {total_items} side-effect(s): {', '.join(categories)}",
        {"total": total_items, "categories": categories, "details": detected},
    )
    sync_node_status_to_session(
        state, "se_detect",
        f"Detected {total_items} side-effect(s) in {len(categories)} categories: {categories}",
        {"side_effects": detected},
    )

    verification = dict(state.get("verification") or {})
    existing_se = dict(verification.get("side_effects") or {})
    existing_se.update(detected)
    verification["side_effects"] = existing_se

    return {"verification": verification}
