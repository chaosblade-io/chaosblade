"""Lifecycle helpers for AgentState reset/build policies.

The graph still exposes flat top-level fields for compatibility with
TaskStore, CLI/TUI renderers, and existing checkpoints.  This module is the
single place that decides which of those fields are per-fault runtime state
and must be reset when a batch moves to the next fault.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from chaos_agent.agent.fault_spec import FaultSpec


_NO_RESET = object()


@dataclass(frozen=True)
class StateFieldPolicy:
    """Lifecycle metadata for one flat AgentState field."""

    name: str
    group: str
    durable: bool = False
    batch_fault_default: Any = _NO_RESET
    recover_default: Any = _NO_RESET

    @property
    def reset_on_batch_fault(self) -> bool:
        return self.batch_fault_default is not _NO_RESET

    @property
    def reset_on_recover(self) -> bool:
        return self.recover_default is not _NO_RESET


def _p(
    name: str,
    group: str,
    *,
    durable: bool = False,
    batch: Any = _NO_RESET,
    recover: Any = _NO_RESET,
) -> StateFieldPolicy:
    return StateFieldPolicy(
        name=name,
        group=group,
        durable=durable,
        batch_fault_default=batch,
        recover_default=recover,
    )


_STATE_FIELD_POLICY_LIST: tuple[StateFieldPolicy, ...] = (
    # ── Core Identity ──────────────────────────────────────────────
    _p("messages", "core_identity", durable=True, recover=[]),
    _p("task_id", "core_identity", durable=True),
    _p("tui_session_id", "core_identity", durable=True),
    _p("parent_task_id", "core_identity", durable=True, batch=""),
    _p("operation", "core_identity", durable=True, batch="inject", recover="recover"),

    # ── Intent & Input ─────────────────────────────────────────────
    _p("input", "intent_input", durable=True, batch=None),
    _p("confirmed_intent", "intent_input", durable=True, batch="inject", recover=None),
    _p("interaction_mode", "intent_input", durable=True),
    _p("intent_context", "intent_input", durable=True, batch=None),
    _p("intent_confidence", "intent_input", batch=0.0),
    _p("clarification_round", "intent_input", batch=0),
    _p("dialogue_round", "intent_input", batch=0),
    _p("intent_reasoning", "intent_input", batch=None),
    _p("needs_task_selection", "intent_input", batch=False),
    _p("recover_task_id", "intent_input", batch=None),
    _p("dry_run", "intent_input", batch=False, recover=False),

    # ── Planning ───────────────────────────────────────────────────
    _p("skill_name", "planning", durable=True, batch=None),
    _p("fault_spec", "planning", durable=True),
    _p("skill_case_content", "planning", durable=True, batch=None),
    _p("matched_use_case_path", "planning", durable=True, batch=None),
    _p("plan", "planning", batch=None),
    _p("plan_summary", "planning", batch=""),
    _p("plan_path", "planning", batch=None),
    _p("is_complex", "planning", batch=None),
    _p("planning_rejected", "planning", batch=False),
    _p("_planning_rejection_reason", "planning", batch=None),
    _p("_planning_alternatives", "planning", batch=""),
    _p("_catalogue_rejection_nudged", "planning", batch=False),
    _p("plan_builder_round", "planning", batch=0),
    _p("plan_confirmed", "planning", batch=False),

    # ── Safety ─────────────────────────────────────────────────────
    _p("safety_status", "safety", batch="pending"),
    _p("safety_reason", "safety", batch=None),
    _p("safety_checked_detail", "safety", batch=None),
    _p("conflict_uids", "safety", batch=None),
    _p("safety_score", "safety", batch=None),
    _p("blast_radius_scope", "safety", batch=None),
    _p("blast_radius_detail", "safety", batch=None),
    _p("target_health_report", "safety", batch=None),
    _p("feasibility_report", "safety", batch=None),

    # ── Confirmation ───────────────────────────────────────────────
    _p("needs_confirmation", "confirmation", batch=False, recover=False),
    _p("approved_target", "confirmation", batch=None),
    _p("drift_reject_count", "confirmation", batch=0),
    _p("plan_change_reject_count", "confirmation", batch=0),
    _p("screener_route", "confirmation", batch=None),

    # ── Execution ──────────────────────────────────────────────────
    _p("blade_uid", "execution", durable=True, batch=None),
    _p("injection_method", "execution", durable=True, batch=None),
    _p("kubectl_exec_pod_name", "execution", durable=True, batch=None),
    _p("blade_parsed_flags", "execution", durable=True, batch=None),
    _p("direct", "execution", batch=False, recover=False),
    _p("original_replicas", "execution", durable=True, batch=None),
    _p("kubeconfig", "execution", durable=True),
    _p("kube_context", "execution", durable=True),
    _p("kubewiz_cluster_uuid", "execution", durable=True),
    _p("kubewiz_profile", "execution", durable=True),
    _p("inject_context", "execution", durable=True, batch=None),
    _p("baseline_data", "execution", durable=True, batch=None),
    _p("target_metadata", "execution", durable=True, batch=None),
    _p("evidence_snapshot", "execution", batch=None),
    _p("disk_burn_post_check", "execution", batch=None),
    _p("disk_fill_post_check", "execution", batch=None),
    _p("se_snapshot", "execution", batch=None),
    _p("force_override", "execution", batch=False),
    _p("_execute_text_nudged", "execution", batch=False),
    _p("_kubectl_step_nudged", "execution", batch=False),
    _p("batch_submit_args", "execution", durable=True),
    _p("current_fault_index", "execution", durable=True),
    _p("batch_results", "execution", durable=True),

    # ── Verification ───────────────────────────────────────────────
    _p("verification", "verification", batch=None, recover=None),
    _p("recover_verification", "verification", batch=None, recover=None),
    _p("inject_layer1_cache", "verification", batch=None, recover=None),
    _p("recover_layer1_cache", "verification", batch=None, recover=None),
    _p("metric_observations", "verification", batch=None, recover=None),
    _p("inject_verification_summary", "verification", durable=True, batch=None),
    _p("reverify_count", "verification", batch=0, recover=0),
    _p("reverify_gaps", "verification", batch=None, recover=None),
    _p("cleaned_debug_pods", "verification", batch=None, recover=None),

    # ── Recovery ───────────────────────────────────────────────────
    _p("recover_phase", "recovery", batch="layer1_recovery", recover="layer1_recovery"),
    _p("recover_layer1_type", "recovery", batch=None, recover=None),
    _p("layer1_iteration_count", "recovery", batch=0, recover=0),
    _p("layer2_context_added", "recovery", batch=False, recover=False),
    _p("recover_layer2_first", "recovery", batch=False, recover=False),

    # ── Loop Control ───────────────────────────────────────────────
    _p("agent_loop_count", "loop_control", batch=0, recover=0),
    _p("execute_loop_count", "loop_control", batch=0, recover=0),
    _p("verifier_loop_count", "loop_control", batch=0, recover=0),
    _p("pipeline_started_at", "loop_control", batch=0.0, recover=0.0),
    _p("transient_retry_count", "loop_control", batch=0, recover=0),
    _p("pipeline_attempt", "loop_control", batch=0, recover=0),
    _p("pipeline_attempts_history", "loop_control", batch=None, recover=None),
    _p("replan_requested", "loop_control", batch=False, recover=False),
    _p("replan_count", "loop_control", batch=0, recover=0),
    _p("replan_context", "loop_control", batch=None, recover=None),
    _p("replan_history", "loop_control", batch=None, recover=None),
    _p("_replan_loop_reset", "loop_control", batch=None, recover=None),

    # ── Results ────────────────────────────────────────────────────
    _p("result", "outcome", batch=None, recover=None),
    _p("error", "outcome", batch=None, recover=None),
    _p("failure_reason", "outcome", batch=None, recover=None),
    _p("failure_detail", "outcome", batch=None, recover=None),
    _p("postmortem", "outcome", batch=None, recover=None),
    _p("created_at", "outcome", durable=True),
    _p("finished_at", "outcome", durable=True, batch=None, recover=None),
    _p("injection_start_time", "outcome", durable=True, batch=None),

    # ── Memory ─────────────────────────────────────────────────────
    _p("compressed_summary", "memory", durable=True, batch=None),
    _p("experiment_history", "memory", durable=True),
    _p("operational_notes", "memory", durable=True),
)


def _build_state_field_policies() -> dict[str, StateFieldPolicy]:
    policies: dict[str, StateFieldPolicy] = {}
    for policy in _STATE_FIELD_POLICY_LIST:
        if policy.name in policies:
            raise ValueError(f"Duplicate AgentState lifecycle policy: {policy.name}")
        policies[policy.name] = policy
    return policies


STATE_FIELD_POLICIES: dict[str, StateFieldPolicy] = _build_state_field_policies()


def _build_state_field_groups() -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = {}
    for policy in _STATE_FIELD_POLICY_LIST:
        groups.setdefault(policy.group, []).append(policy.name)
    return {group: tuple(fields) for group, fields in groups.items()}


STATE_FIELD_GROUPS: dict[str, tuple[str, ...]] = _build_state_field_groups()


STATE_DURABLE_FACT_FIELDS: tuple[str, ...] = tuple(
    policy.name for policy in _STATE_FIELD_POLICY_LIST if policy.durable
)


def iter_state_fields() -> tuple[str, ...]:
    """Return every AgentState field declared in the lifecycle registry."""
    return tuple(field for fields in STATE_FIELD_GROUPS.values() for field in fields)


def state_field_group(field: str) -> str | None:
    """Return the lifecycle group name for ``field`` if it is registered."""
    for group, fields in STATE_FIELD_GROUPS.items():
        if field in fields:
            return group
    return None


def _build_reset_defaults(kind: str) -> dict[str, Any]:
    if kind not in {"batch", "recover"}:
        raise ValueError(f"Unsupported reset kind: {kind}")
    attr = "batch_fault_default" if kind == "batch" else "recover_default"
    return {
        policy.name: deepcopy(getattr(policy, attr))
        for policy in _STATE_FIELD_POLICY_LIST
        if getattr(policy, attr) is not _NO_RESET
    }


_PER_FAULT_RESET_DEFAULTS: dict[str, Any] = _build_reset_defaults("batch")
_RECOVER_RESET_DEFAULTS: dict[str, Any] = _build_reset_defaults("recover")


def per_fault_reset_state() -> dict[str, Any]:
    """Return a fresh reset delta for one batch fault iteration."""
    return deepcopy(_PER_FAULT_RESET_DEFAULTS)


def recover_reset_state() -> dict[str, Any]:
    """Return a fresh reset delta for a recover attempt."""
    return deepcopy(_RECOVER_RESET_DEFAULTS)


def ensure_recover_runtime_defaults(initial: dict) -> dict:
    """Set missing recover runtime defaults without overwriting durable facts."""
    result = dict(initial)
    for key, value in _RECOVER_RESET_DEFAULTS.items():
        result.setdefault(key, deepcopy(value))
    return result


def state_field_policy(field: str) -> StateFieldPolicy | None:
    """Return lifecycle policy metadata for ``field`` if registered."""
    return STATE_FIELD_POLICIES.get(field)


def build_batch_iteration_state(
    *,
    task_id: str,
    spec: FaultSpec,
    batch_args: dict,
    created_at: str,
    messages: list,
) -> dict[str, Any]:
    """Build the full state delta for a new batch fault iteration."""
    result = per_fault_reset_state()
    result.update({
        "task_id": task_id,
        "fault_spec": spec.to_dict(),
        "created_at": created_at,
        "needs_confirmation": True,
        "batch_submit_args": batch_args,
        "messages": messages,
    })
    return result


__all__ = [
    "STATE_DURABLE_FACT_FIELDS",
    "STATE_FIELD_GROUPS",
    "STATE_FIELD_POLICIES",
    "StateFieldPolicy",
    "build_batch_iteration_state",
    "ensure_recover_runtime_defaults",
    "iter_state_fields",
    "per_fault_reset_state",
    "recover_reset_state",
    "state_field_policy",
    "state_field_group",
]
