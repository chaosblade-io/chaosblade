"""Serialisation between ``AgentState`` and ``ApprovedTarget``.

``AgentState`` stores ``approved_target`` as a plain dict (LangGraph
serialises state via Pydantic + JSON, and frozen dataclasses don't
round-trip cleanly through the checkpointer). The helpers here
are the only place this dict shape is constructed or consumed:

  - ``freeze_approved_target_from_spec`` — graph nodes call this when
    the user accepts a plan. It projects ``FaultSpec`` into the
    canonical approved-target snapshot.
  - ``freeze_approved_target`` — legacy-compatible lower-level
    constructor that accepts the historical target/params/blade_* pieces.
  - ``approved_from_dict`` — the screener node calls this to hydrate
    a dict back into an ``ApprovedTarget`` for the guard.

Centralising the conversion keeps the policy in ``guard.py`` free of
state-shape coupling and ensures both writer + reader agree on
field names + defaults.
"""

from __future__ import annotations

import logging
from typing import Optional

from chaos_agent.agent.fault_spec import FaultSpec
from .guard import CLUSTER_SCOPED_KINDS, OWNER_SCOPES
from .types import ApprovedTarget

logger = logging.getLogger(__name__)

def freeze_approved_target_from_spec(
    spec: FaultSpec | dict | None,
    *,
    lock_fault_type: bool = True,
    owner_names: tuple[str, ...] = (),
) -> Optional[dict]:
    """Build the ``approved_target`` snapshot from a FaultSpec.

    ``FaultSpec`` is the source of truth for the operator-approved intent.
    This helper is the graph-facing constructor; it keeps nodes from
    hand-assembling the old target/params/blade_* shape and accidentally
    reviving scattered state fields as facts.
    """
    if isinstance(spec, dict):
        spec_obj = FaultSpec.from_dict(spec)
    elif isinstance(spec, FaultSpec):
        spec_obj = spec
    else:
        spec_obj = None
    if spec_obj is None:
        return None

    return freeze_approved_target(
        target={
            "namespace": spec_obj.namespace,
            "names": list(spec_obj.names),
            "labels": dict(spec_obj.labels),
            "resource_type": spec_obj.scope,
        },
        params=dict(spec_obj.params),
        blade_scope=spec_obj.scope,
        blade_target=spec_obj.blade_target,
        blade_action=spec_obj.blade_action,
        lock_fault_type=lock_fault_type,
        owner_names=owner_names,
    )


def freeze_approved_target(
    target: Optional[dict],
    params: Optional[dict],
    blade_scope: Optional[str],
    blade_target: Optional[str],
    blade_action: Optional[str],
    *,
    lock_fault_type: bool = True,
    owner_names: tuple[str, ...] = (),
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
    # Cross-scope: secondary scopes for operations that need resources
    # beyond the primary scope (e.g. node faults needing pod delete,
    # pod faults needing PVC creation).
    secondary_namespace = ""
    secondary_scopes: tuple[str, ...] = ()
    if scope in CLUSTER_SCOPED_KINDS:
        secondary_namespace = namespace  # preserve before clearing
        if scope == "node":
            secondary_scopes = ("pod", "deployment", "daemonset", "statefulset")
        # Cluster-scoped resources never carry a namespace; null it
        # to keep the snapshot tidy.
        namespace = ""
    elif scope in ("pod", "deployment", "statefulset", "daemonset"):
        # Workload faults may need to:
        # - Create dependency resources (PVC, ConfigMap, Secret)
        # - Delete/patch pods belonging to the workload
        # - Taint/cordon nodes to affect pod scheduling (e.g. Taint→Pending)
        secondary_scopes = ("pvc", "persistentvolumeclaim", "pv", "persistentvolume", "configmap", "secret", "pod", "node")
        secondary_namespace = namespace

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
        "owner_names": list(owner_names),
        "secondary_scopes": list(secondary_scopes),
        "secondary_namespace": secondary_namespace,
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
        owner_names=tuple(str(n) for n in (d.get("owner_names") or [])),
        secondary_scopes=tuple(str(s) for s in (d.get("secondary_scopes") or [])),
        secondary_namespace=str(d.get("secondary_namespace") or ""),
    )


async def discover_owner_names(
    scope: str,
    namespace: str,
    labels: dict[str, str],
    kubeconfig: str = "",
) -> tuple[str, ...]:
    """Query the cluster for owner resources whose selector matches ``labels``.

    When scope=pod, finds Deployments/DaemonSets/StatefulSets in the
    same namespace whose ``spec.selector.matchLabels`` are a subset of
    the given labels. Returns their names so the guard can validate
    owner-scope operations at the instance level.

    Best-effort: returns empty tuple on any failure (guard falls back
    to namespace-only anchoring).
    """
    if scope not in OWNER_SCOPES or not labels or not namespace:
        return ()

    from chaos_agent.config.settings import settings
    from chaos_agent.tools.kubectl import build_kubectl_cmd, _adapt_kubewiz_result
    from chaos_agent.tools.shell import run_command

    label_selector = ",".join(f"{k}={v}" for k, v in labels.items())
    owner_kinds = OWNER_SCOPES[scope]
    found: list[str] = []

    for kind in sorted(owner_kinds):
        cmd = build_kubectl_cmd("get", [
            kind, "-n", namespace,
            "-l", label_selector,
            "-o", "jsonpath={.items[*].metadata.name}",
        ], kubeconfig=kubeconfig)
        try:
            result = await run_command(cmd, timeout=settings.timeout_kubectl)
            result = _adapt_kubewiz_result(result)
            if result.exit_code == 0 and result.stdout.strip():
                found.extend(result.stdout.strip().split())
        except Exception as e:
            logger.debug("discover_owner_names: %s query failed: %s", kind, e)

    if found:
        logger.info("discover_owner_names: found owners %s for labels=%s in ns=%s",
                     found, labels, namespace)
    return tuple(found)


__all__ = [
    "approved_from_dict",
    "discover_owner_names",
    "freeze_approved_target",
    "freeze_approved_target_from_spec",
]
