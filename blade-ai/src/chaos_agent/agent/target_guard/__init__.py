"""Target-drift guard subsystem.

Prevents ``execute_loop``'s LLM from acting on a different k8s
resource than ``confirmation_gate`` approved. The package splits into
three concerns:

  - ``types`` — frozen record types (``ApprovedTarget``,
    ``EffectiveTarget``) plus the mutable ``GuardDecision`` and the
    ``GuardVerdict`` / ``ConfidenceLevel`` enums.
  - ``classifier`` — turns a raw tool_call into an ``EffectiveTarget``.
    Knows kubectl subcommands, recursive ``kubectl exec`` payloads,
    and ChaosBlade ``--target`` → k8s-scope mapping.
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
    BLADE_TARGET_TO_SCOPE,
    SCOPE_BANNED,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    canonicalise_kind,
    infer_effective_target,
    parse_labels,
    parse_namespace,
)
from .freeze import approved_from_dict, freeze_approved_target
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
    "BLADE_TARGET_TO_SCOPE",
    "CLUSTER_SCOPED_KINDS",
    "ConfidenceLevel",
    "EffectiveTarget",
    "GuardDecision",
    "GuardVerdict",
    "SCOPE_BANNED",
    "SCOPE_READONLY",
    "SCOPE_UNKNOWN",
    "approved_from_dict",
    "canonicalise_kind",
    "freeze_approved_target",
    "infer_effective_target",
    "parse_labels",
    "parse_namespace",
    "target_drift_guard",
]
