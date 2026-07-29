"""Cleanup for ephemeral capability-probe debug pods created during Phase 1.

Planning nodes (agent_loop / intent_clarification / plan_builder) may create a
short-lived ``kubectl debug`` probe pod via ``kubectl_read`` to verify an
image's / host's capability (e.g. does the candidate image carry ``sh`` /
``chroot``) before committing to a plan. Those pods are read-only-use only
(``kubectl_read`` gates every exec) but they ARE a transient cluster mutation,
so they must be removed when planning exits — before the confirmation gate and
before execution — so a reject leaves nothing behind.

This mirrors ``verify/_verifier_finalize._cleanup_debug_pods`` but is a neutral,
state-in / update-dict-out helper shared by the planning exits (extract_planning
_metadata's proceed/terminal-reject paths and the plan_builder terminal path).
"""

from __future__ import annotations

import logging

from langchain_core.messages import ToolMessage

from chaos_agent.agent.state import AgentState

logger = logging.getLogger(__name__)


async def cleanup_planning_debug_pods(state: AgentState) -> dict:
    """Delete probe debug pods created during Phase 1 planning.

    Idempotent across re-entries: diffs discovered pods against
    ``state.cleaned_debug_pods`` and only deletes the new ones, writing the
    merged set back so a later call (or the verify backstop) sees them handled.

    Returns a state-update dict (possibly empty) to merge into the node result.
    """
    from chaos_agent.agent.execution_artifacts import cleanup_debug_pod_artifacts
    from chaos_agent.agent.nodes.execute._debug_pod import (
        delete_debug_pod,
        parse_debug_pod_info,
    )
    from chaos_agent.agent.nodes.execute._kubeconfig_inject import _resolve_kubeconfig

    result_update: dict = {}
    kubeconfig = _resolve_kubeconfig(state) or ""
    task_id = state.get("task_id", "") or ""

    tracked_artifacts, artifact_cleaned = await cleanup_debug_pod_artifacts(
        state.get("execution_artifacts"),
        kubeconfig=kubeconfig,
        task_id=task_id,
    )
    if tracked_artifacts != (state.get("execution_artifacts") or []):
        result_update["execution_artifacts"] = tracked_artifacts

    # Probe pods created via ``kubectl_read`` are NOT tracked as execution
    # artifacts (collect_execution_artifacts only indexes the full ``kubectl``
    # tool), so scan the message history for both tool names.
    discovered_pods: dict[str, str] = {}
    for msg in state.get("messages", []):
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") in (
            "kubectl", "kubectl_read",
        ):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            pod_name, ns = parse_debug_pod_info(content)
            if pod_name:
                discovered_pods[pod_name] = ns

    already_cleaned: set[str] = set(state.get("cleaned_debug_pods") or [])
    already_cleaned.update(artifact_cleaned)
    tracked_names = {
        str(artifact.get("name") or "")
        for artifact in tracked_artifacts
        if isinstance(artifact, dict) and artifact.get("type") == "debug_pod"
    }
    pods_to_delete = set(discovered_pods.keys()) - already_cleaned - tracked_names
    for pod_name in pods_to_delete:
        ns = discovered_pods[pod_name]
        logger.info(
            "planning cleanup: deleting probe debug pod %s in namespace %s",
            pod_name, ns,
        )
        await delete_debug_pod(pod_name, kubeconfig, task_id, namespace=ns)
    if pods_to_delete or artifact_cleaned:
        result_update["cleaned_debug_pods"] = sorted(already_cleaned | pods_to_delete)

    return result_update


__all__ = ["cleanup_planning_debug_pods"]
