"""se_detect node — post-verification side-effect detection via snapshot diff."""

import asyncio
import logging

from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.spec.feasibility import profile_for_spec
from chaos_agent.agent.result.operation_outcome import (
    read_inject_verification,
    write_inject_verification,
)
from chaos_agent.agent.nodes.side_effect._side_effect_detectors import (
    DetectionContext,
    SideEffectSnapshot,
    resolve_observer,
    run_all_detectors,
)
from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.nodes.store._store_sync import sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.transports import PROFILE_K8S

logger = logging.getLogger(__name__)

_DETECT_TIMEOUT = 15.0


async def se_detect_node(state: AgentState) -> dict:
    """Query current state, diff against pre-injection snapshot.

    Writes detected incremental side-effects into
    verification["side_effects"]. Runs after verifier_loop completes.
    Dispatches on the fault profile (k8s/host) via the registered observer.
    """
    spec = read_fault_spec(state)
    namespace = spec.namespace if spec else ""
    kubeconfig = state.get("kubeconfig", "")
    injection_start = state.get("injection_start_time", "")
    task_id = state.get("task_id", "")

    profile = profile_for_spec(spec) if spec else PROFILE_K8S
    observer = resolve_observer(profile)
    if observer is None:
        logger.debug("se_detect: no observer for profile=%s, skipping", profile)
        return {}
    if not observer.can_capture(spec):
        logger.debug("se_detect: profile=%s cannot capture for this spec, skipping", profile)
        return {}
    if not injection_start:
        logger.debug("se_detect: no injection_start_time, skipping")
        return {}

    tracker = get_tracker(task_id)
    tracker.start(StatusCategory.NODE, "se_detect", "Detecting post-injection side effects")
    await dispatch_node_message("se_detect", "正在检测注入副作用...\n\n")

    snapshot_dict = state.get("se_snapshot")
    snapshot = SideEffectSnapshot.from_dict(snapshot_dict) if snapshot_dict else None

    target_names = list(spec.names) if spec else []

    try:
        after = await asyncio.wait_for(
            observer.fetch_post_inject_state(spec, kubeconfig, injection_start, task_id=task_id),
            timeout=_DETECT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("se_detect: fetch_post_inject_state timed out after %.0fs", _DETECT_TIMEOUT)
        tracker.complete("Side-effect detection timed out")
        await dispatch_node_message("se_detect", "副作用检测超时\n\n")
        return {}
    except Exception as e:
        logger.warning("se_detect: fetch failed: %s", e)
        tracker.complete(f"Side-effect detection fetch failed: {e}")
        await dispatch_node_message("se_detect", f"副作用检测失败: {e}\n\n")
        return {}

    ctx = DetectionContext(
        namespace=namespace,
        target_names=target_names,
        scope=spec.scope if spec else "",
        kubeconfig=kubeconfig,
        injection_start_time=injection_start,
        task_id=task_id,
        target=spec.blade_target if spec else "",
        profile=profile,
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
        await dispatch_node_message("se_detect", "未检测到增量副作用\n\n")
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
    await dispatch_node_message("se_detect", f"检测到 {total_items} 个副作用: {', '.join(categories)}\n\n")
    sync_node_status_to_session(
        state, "se_detect",
        f"Detected {total_items} side-effect(s) in {len(categories)} categories: {categories}",
        {"side_effects": detected},
    )

    verification = read_inject_verification(state) or {}
    existing_se = dict(verification.get("side_effects") or {})
    existing_se.update(detected)
    verification["side_effects"] = existing_se

    return write_inject_verification(verification=verification)
