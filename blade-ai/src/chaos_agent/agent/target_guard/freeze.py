"""Serialisation between ``AgentState`` and ``ApprovedTarget``.

``AgentState`` stores ``approved_target`` as a plain dict (LangGraph
serialises state via Pydantic + JSON, and frozen dataclasses don't
round-trip cleanly through the checkpointer). The two helpers here
are the only place this dict shape is constructed or consumed:

  - ``freeze_approved_target`` — confirmation_gate calls this when
    the user accepts a plan. Pulls the relevant fields out of
    ``state.target`` / ``state.params`` / ``state.blade_*`` and
    produces the canonical dict.
  - ``approved_from_dict`` — the screener node calls this to hydrate
    a dict back into an ``ApprovedTarget`` for the guard.

Centralising the conversion keeps the policy in ``guard.py`` free of
state-shape coupling and ensures both writer + reader agree on
field names + defaults.
"""

from __future__ import annotations

from typing import Optional

from .guard import CLUSTER_SCOPED_KINDS
from .types import ApprovedTarget

# Re-export for callers that want to skip ``freeze_approved_target``
# and build directly from a FaultSpec instance.  We keep the legacy
# ``freeze_approved_target(target, params, blade_*)`` signature too
# because confirmation_gate / safety_check assemble the same dict
# from their local FaultSpec read and the contract is cleaner than
# threading a FaultSpec object through this single-purpose module.


def freeze_approved_target(
    target: Optional[dict],
    params: Optional[dict],
    blade_scope: Optional[str],
    blade_target: Optional[str],
    blade_action: Optional[str],
    *,
    lock_fault_type: bool = True,
) -> Optional[dict]:
    """Build the ``approved_target`` dict to store in AgentState.

    Args:
        target: ``state.target`` — typically ``{namespace, names,
            labels, resource_type}``. May be None / empty for
            old-style state.
        params: ``state.params`` — fallback source of ``scope``,
            ``target`` (blade target), ``action``.
        blade_scope: ``state.blade_scope`` — explicit scope hint.
        blade_target: ``state.blade_target`` — preferred over
            ``params['target']``.
        blade_action: ``state.blade_action`` — preferred over
            ``params['action']``.
        lock_fault_type: Whether to lock the blade target type so
            ``cpu`` → ``mem`` would trigger drift. Defaults True per
            the spec (user can later relax via per-call override or
            future settings flag).

    Returns:
        The frozen dict, or ``None`` if not enough info to construct
        a sensible approval (no resolvable scope). Callers should
        treat ``None`` as "do not enable target-drift guarding for
        this turn" — typically chat-only turns or sessions where the
        target hasn't been pinned yet.
    """
    target = target or {}
    params = params or {}

    # ---- Resolve k8s scope -------------------------------------------------
    scope_raw = (
        target.get("resource_type")
        or params.get("scope")
        or blade_scope
        or ""
    )
    scope = str(scope_raw).strip().lower()
    if scope == "container":
        # container chaos is pod-scoped — the container lives inside
        # a pod and the guard tracks the pod identity.
        scope = "pod"
    if not scope:
        return None

    # ---- Namespace (default-normalise for namespace-scoped scopes) --------
    namespace = str(target.get("namespace") or "").strip()
    if not namespace and scope not in CLUSTER_SCOPED_KINDS:
        namespace = "default"
    if scope in CLUSTER_SCOPED_KINDS:
        # Cluster-scoped resources never carry a namespace; null it
        # to keep the snapshot tidy.
        namespace = ""

    # ---- Names (accept CSV string for back-compat) ------------------------
    raw_names = target.get("names") or []
    if isinstance(raw_names, str):
        raw_names = [n.strip() for n in raw_names.split(",") if n.strip()]
    names = [str(n) for n in raw_names if n]

    # ---- Labels -----------------------------------------------------------
    raw_labels = target.get("labels") or {}
    if isinstance(raw_labels, dict):
        labels = {str(k): str(v) for k, v in raw_labels.items()}
    else:
        labels = {}

    # ---- Namespace-wide opt-in -------------------------------------------
    # If neither names nor labels were given, the user effectively
    # approved "any resource of this scope in this namespace". The
    # guard then allows specific names without further checking.
    is_namespace_wide = not names and not labels

    # ---- Blade fault type / action ---------------------------------------
    bt = str(blade_target or params.get("target") or "").strip().lower()
    ba = str(blade_action or params.get("action") or "").strip().lower()

    return {
        "scope": scope,
        "namespace": namespace,
        "names": names,
        "labels": labels,
        "is_namespace_wide": is_namespace_wide,
        "blade_target": bt,
        "blade_action": ba,
        "lock_fault_type": bool(lock_fault_type),
    }


def approved_from_dict(d: Optional[dict]) -> Optional[ApprovedTarget]:
    """Hydrate an ``ApprovedTarget`` from the dict in ``state.approved_target``.

    Returns ``None`` for missing/empty/malformed dicts so the screener
    can short-circuit to its "no approval on record" branch instead of
    constructing a defaulted ApprovedTarget that would silently
    compare against zero-valued fields.
    """
    if not d or not isinstance(d, dict):
        return None
    scope = str(d.get("scope") or "").strip()
    if not scope:
        return None
    return ApprovedTarget(
        scope=scope,
        namespace=str(d.get("namespace") or ""),
        names=tuple(str(n) for n in (d.get("names") or [])),
        labels={str(k): str(v) for k, v in (d.get("labels") or {}).items()},
        is_namespace_wide=bool(d.get("is_namespace_wide") or False),
        blade_target=str(d.get("blade_target") or ""),
        blade_action=str(d.get("blade_action") or ""),
        lock_fault_type=bool(d.get("lock_fault_type", True)),
    )


__all__ = ["approved_from_dict", "freeze_approved_target"]
