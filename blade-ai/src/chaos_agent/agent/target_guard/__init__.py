"""Target-drift guard subsystem.

Prevents ``execute_loop``'s LLM from acting on a different k8s
resource than ``confirmation_gate`` approved. The package splits into
three concerns:

  - ``types`` — frozen record types (``ApprovedTarget``,
    ``EffectiveTarget``) plus the mutable ``GuardDecision`` and the
    ``GuardVerdict`` / ``ConfidenceLevel`` enums.
  - ``classifier`` — the GENERIC classification layer: the top-level
    ``infer_effective_target`` entry point plus cross-carrier shared
    helpers. Carrier-specific vocabulary (blade / kubectl command-line
    families) lives in the provider domains since phase-7 T5 and is
    enacted through ``FaultProviderRegistry.classify_tool_target``.
  - ``guard`` — the policy. Compares an ``EffectiveTarget`` against
    the ``ApprovedTarget`` and returns a ``GuardDecision``.

Wiring (added in later steps): a screener node sits between
``execute_loop``'s LLM and the ToolNode. For each ``tool_call`` it
runs ``infer_effective_target`` → ``target_drift_guard``. On REJECT
verdicts it stops the call from reaching tools and either (a) emits a
``ToolMessage`` back to the LLM so it can retry, or (b) triggers
replan + re-confirm. On READONLY / ALLOW it passes through.
"""

from .classifier import (
    SCOPE_BANNED,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    canonicalise_kind,
    infer_effective_target,
    parse_labels,
    parse_namespace,
)
from .freeze import (
    WORKLOAD_TEMPLATE_SCOPES,
    approved_from_dict,
    discover_names_by_labels,
    discover_owner_names,
    discover_pod_pvc_claims,
    discover_statefulset_pvc_claims,
    discover_workload_pvc_claims,
    freeze_approved_target,
    freeze_approved_target_from_spec,
)
from .guard import CLUSTER_SCOPED_KINDS, target_drift_guard
from .types import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardDecision,
    GuardVerdict,
)

__all__ = [
    "ApprovedTarget",
    "CLUSTER_SCOPED_KINDS",
    "ConfidenceLevel",
    "EffectiveTarget",
    "GuardDecision",
    "GuardVerdict",
    "SCOPE_BANNED",
    "SCOPE_READONLY",
    "SCOPE_UNKNOWN",
    "WORKLOAD_TEMPLATE_SCOPES",
    "approved_from_dict",
    "canonicalise_kind",
    "discover_names_by_labels",
    "discover_owner_names",
    "discover_pod_pvc_claims",
    "discover_statefulset_pvc_claims",
    "discover_workload_pvc_claims",
    "freeze_approved_target",
    "freeze_approved_target_from_spec",
    "infer_effective_target",
    "parse_labels",
    "parse_namespace",
    "target_drift_guard",
]
