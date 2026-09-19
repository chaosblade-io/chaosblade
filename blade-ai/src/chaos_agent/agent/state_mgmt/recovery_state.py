"""Recover graph initial-state builders.

Recover can be launched from CLI, TS TUI, HTTP streaming, and L4.  All of
those entry points need the same rule: copy only durable inject facts, then
reset recover/runtime fields so stale verification, messages, and loop state
cannot leak into the new recover graph.

Connection credentials are the deliberate exception to "copy durable
facts": they are runtime context, not facts.  See
``build_recover_initial_from_checkpoint`` for the precedence chain.
"""

from __future__ import annotations

import logging

from chaos_agent.agent.spec.fault_spec import fault_spec_from_legacy_state
from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.agent.state import materialize_fault_handle
from chaos_agent.agent.state_mgmt.state_lifecycle import (
    ensure_recover_runtime_defaults,
    recover_reset_state,
)

logger = logging.getLogger(__name__)


def build_recover_initial_from_checkpoint(
    inject_values: dict,
    inject_task_id: str,
    *,
    record_task_id: str | None = None,
    inject_context: str | None = None,
    kubeconfig_override: str | None = None,
    tui_session_id_override: str | None = None,
    connection_override: dict | None = None,
) -> dict:
    """Build recover initial state from an inject graph checkpoint/state dict."""
    record_task_id = record_task_id or f"recover-{inject_task_id}"
    if inject_context is None:
        from chaos_agent.utils.inject_context import build_inject_context

        inject_context = build_inject_context(inject_values.get("messages", []))

    # Connection credentials are RUNTIME CONTEXT, not durable inject facts.
    # The recovering caller's explicitly carried connection
    # (``connection_override`` — the L4 entry assembles it from task.payload,
    # where the platform resolves the session-bound environment) outranks
    # the checkpoint-frozen ones: a cross-time / cross-user recover must not
    # inherit the injector's identity (incident 2026-09-15: recover kept
    # running as the injector's wiz profile '526255' and hit the auth wall
    # seven times while the recovering user's own login sat unused).  The
    # checkpoint values remain the fallback so bare CLI / HTTP / TUI /
    # auto-recover entries (which carry no connection) behave unchanged.
    conn = connection_override or {}
    kubeconfig = (
        conn.get("kubeconfig")
        if conn.get("kubeconfig")
        else (
            kubeconfig_override
            if kubeconfig_override is not None
            else inject_values.get("kubeconfig", "")
        )
    ) or ""
    kube_context = (
        conn.get("kube_context") or inject_values.get("kube_context", "")
    ) or ""
    kubewiz_cluster_uuid = (
        conn.get("kubewiz_cluster_uuid")
        or inject_values.get("kubewiz_cluster_uuid", "")
    ) or ""
    kubewiz_profile = (
        conn.get("kubewiz_profile")
        or inject_values.get("kubewiz_profile", "")
    ) or ""
    # Cross-cluster guard: recovery is cluster-affine — blade destroy UIDs
    # and kubectl patches must land on the ORIGINAL cluster.  A carried
    # connection pointing at a different cluster is almost certainly a
    # mis-bound environment; warn loudly instead of failing quietly.
    # Non-blocking by design, mirroring the kubeconfig-mode precedent
    # (kubeconfig_override has always switched clusters silently because the
    # inject-time temp file is long gone — this guard only restores
    # visibility for the kubewiz axis).
    _conn_cluster = conn.get("kubewiz_cluster_uuid")
    _inject_cluster = inject_values.get("kubewiz_cluster_uuid")
    if _conn_cluster and _inject_cluster and _conn_cluster != _inject_cluster:
        logger.warning(
            "recover connection override targets cluster %s but the fault "
            "was injected on cluster %s (task=%s): recovery commands will "
            "run against the OVERRIDE cluster — verify the session-bound "
            "environment points at the right cluster",
            _conn_cluster, _inject_cluster, inject_task_id,
        )
    # Cross-channel guard: the same visibility rule on the channel axis.  A
    # carried channel different from the injection channel either targets
    # the wrong kind of resource outright (e.g. fault injected via
    # kubewiz_k8s but the session-bound environment speaks kubewiz_host) or
    # leaves cluster affinity unverifiable on the uuid axis (kubeconfig ↔
    # kubewiz pairs carry no comparable uuid) — warn in both cases, still
    # non-blocking.
    _conn_mode = conn.get("kube_connection_mode")
    _inject_mode = inject_values.get("kube_connection_mode")
    if _conn_mode and _inject_mode and _conn_mode != _inject_mode:
        logger.warning(
            "recover connection override switches the channel from %s "
            "(injection) to %s (task=%s): recovery is cluster-affine but "
            "cross-channel recovery cannot be validated against the "
            "injection cluster — verify the session-bound environment "
            "points at the right target",
            _inject_mode, _conn_mode, inject_task_id,
        )

    initial = recover_reset_state()
    initial.update({
        "task_id": record_task_id,
        "tui_session_id": (
            tui_session_id_override
            if tui_session_id_override is not None
            else inject_values.get("tui_session_id", "")
        ) or "",
        "parent_task_id": inject_task_id,
        "recover_task_id": inject_task_id,
        "operation": "recover",
        "experiment_uid": inject_values.get("experiment_uid", ""),
        # Liability axis (B76 review G): the birth registry and the death
        # registry must BOTH cross the graph boundary for the recover
        # finale's residual sweep (``sweep_live_liabilities``) — the birth
        # registry without the death registry would re-destroy already-
        # retired experiments, the death registry without the birth registry
        # filters nothing. Legacy inject states predate both keys; the empty
        # defaults keep the sweep a no-op there (hydration lives in
        # ``live_liability_uids``).
        "owned_experiment_uids": list(
            inject_values.get("owned_experiment_uids") or []
        ),
        "retired_experiment_uids": list(
            inject_values.get("retired_experiment_uids") or []
        ),
        # Round-32b — combo discriminator: routes the recover Layer-1 flow
        # (combo → deterministic experiment destroy FIRST, then the LLM
        # native undo). Seeded typed by task_snapshot's marker hydration
        # (record or checkpoint leg); None (unknown) leaves the live
        # criterion-2 fallback in charge.
        "combo_native_issued": inject_values.get("combo_native_issued"),
        # Carrier-agnostic fault handle for the recover graph. Inject graphs
        # written before the handle existed (legacy checkpoints, persisted
        # task snapshots) lack the field, so materialize it from whatever
        # attribution facts survived — the registry hydration seam keeps the
        # legacy knowledge provider-side instead of in this builder.
        "fault_handle": materialize_fault_handle(inject_values),
        "skill_name": read_active_skill_name(inject_values),
        "fault_type": inject_values.get("fault_type", "") or "",
        "skill_case_content": inject_values.get("skill_case_content", "") or "",
        "blast_radius_detail": inject_values.get("blast_radius_detail", "") or "",
        # Side effects recorded at injection time (collateral impact beyond
        # the primary target). Carried into recover so Layer 1 can undo /
        # reconcile them and Layer 2 must verify each one.
        "side_effects": dict(inject_values.get("side_effects") or {}),
        "injection_parsed_params": inject_values.get("injection_parsed_params") or {},
        "inject_verification_summary": (
            inject_values.get("inject_verification_summary", "") or ""
        ),
        "inject_context": inject_context or "",
        "baseline_data": inject_values.get("baseline_data"),
        "fault_spec": _recover_fault_spec(inject_values),
        "kubeconfig": kubeconfig,
        "kube_context": kube_context,
        "kubewiz_cluster_uuid": kubewiz_cluster_uuid,
        "kubewiz_profile": kubewiz_profile,
        "injection_method": inject_values.get("injection_method"),
        "execution_artifacts": list(inject_values.get("execution_artifacts") or []),
        "kubectl_exec_pod_name": inject_values.get("kubectl_exec_pod_name"),
        "created_at": str(inject_values.get("created_at") or inject_values.get("gmt_create") or ""),
        "tenant_id": inject_values.get("tenant_id", "") or "",
        # Workspace is a durable OWNERSHIP fact (unlike connection credentials,
        # which are runtime context): the task belongs to the injector's
        # workspace forever, regardless of who recovers it — so recover-side
        # queries stay scoped to the task's home workspace.
        "workspace_id": inject_values.get("workspace_id", "") or "",
    })
    # Channel must travel with the credentials snapshot: a carried kubewiz
    # profile under a leftover kubeconfig-mode channel (or vice versa) would
    # resolve the wrong transport in ``TransportTarget.from_state``.  Only a
    # non-empty carried mode writes the key — entries that carry no mode
    # keep channel resolution on the caller's settings (current behavior).
    if conn.get("kube_connection_mode"):
        initial["kube_connection_mode"] = conn["kube_connection_mode"]
    return initial


def _recover_fault_spec(values: dict) -> dict | None:
    raw = values.get("fault_spec")
    if isinstance(raw, dict) and raw:
        return dict(raw)
    spec = fault_spec_from_legacy_state(values, source="recover_checkpoint")
    return spec.to_dict() if spec else None


__all__ = [
    "build_recover_initial_from_checkpoint",
    "ensure_recover_runtime_defaults",
    "recover_reset_state",
]
