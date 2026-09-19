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
from typing import Mapping, Optional

from chaos_agent.agent.spec.fault_registry import is_host_scope, is_workload_scope
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.tools.kubectl import query_kubectl
from .classifier import canonicalise_kind
from .guard import CLUSTER_SCOPED_KINDS, OWNER_SCOPES
from .mechanism_writes import MechanismWriteEntry, entries_from_list, entries_to_list
from .types import ApprovedTarget

logger = logging.getLogger(__name__)

def freeze_approved_target_from_spec(
    spec: FaultSpec | dict | None,
    *,
    lock_fault_type: bool = True,
    owner_names: tuple[str, ...] = (),
    resolved_names: tuple[str, ...] = (),
    pvc_claims: tuple[str, ...] = (),
    mechanism_entries: tuple[MechanismWriteEntry, ...] = (),
    widening_pending_approval: bool = False,
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
        fault_scope=spec_obj.scope,
        fault_target=spec_obj.fault_target,
        fault_action=spec_obj.fault_action,
        lock_fault_type=lock_fault_type,
        owner_names=owner_names,
        resolved_names=resolved_names,
        pvc_claims=pvc_claims,
        mechanism_entries=mechanism_entries,
        widening_pending_approval=widening_pending_approval,
        duration_seconds=int(getattr(spec_obj, "duration_seconds", 0) or 0),
    )


def freeze_approved_target(
    target: Optional[dict],
    params: Optional[dict],
    fault_scope: Optional[str],
    fault_target: Optional[str],
    fault_action: Optional[str],
    *,
    lock_fault_type: bool = True,
    owner_names: tuple[str, ...] = (),
    resolved_names: tuple[str, ...] = (),
    pvc_claims: tuple[str, ...] = (),
    mechanism_entries: tuple[MechanismWriteEntry, ...] = (),
    widening_pending_approval: bool = False,
    duration_seconds: int = 0,
) -> Optional[dict]:
    """Build the ``approved_target`` dict to store in AgentState.

    Args:
        target: ``state.target`` — typically ``{namespace, names,
            labels, resource_type}``. May be None / empty for
            old-style state.
        params: ``state.params`` — fallback source of ``scope``,
            ``target`` (blade target), ``action``.
        fault_scope: ``state.fault_scope`` — explicit scope hint.
        fault_target: ``state.fault_target`` — preferred over
            ``params['target']``.
        fault_action: ``state.fault_action`` — preferred over
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
        or fault_scope
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
    if not namespace and scope not in CLUSTER_SCOPED_KINDS and scope != "host":
        namespace = "default"
    # Cross-scope: secondary scopes for operations that need resources
    # beyond the primary scope (e.g. node faults needing pod delete,
    # pod faults needing quota creation).
    secondary_namespace = ""
    secondary_scopes: tuple[str, ...] = ()
    if scope in CLUSTER_SCOPED_KINDS:
        secondary_namespace = namespace  # preserve before clearing
        if scope == "node":
            secondary_scopes = ("pod", "deployment", "daemonset", "statefulset")
        # Cluster-scoped resources never carry a namespace; null it
        # to keep the snapshot tidy.
        namespace = ""
    elif is_workload_scope(scope):
        # Workload faults may need to:
        # - Create dependency resources (ConfigMap, Secret) — PVC writes
        #   are NOT covered here: they go through the case manifest
        #   (mechanism_writes frontmatter), the write-set legislation
        #   channel with name-level precision
        # - Delete/patch pods belonging to the workload
        # - Taint/cordon nodes to affect pod scheduling (e.g. Taint→Pending)
        # - Create/delete a ResourceQuota to drive admission failures
        #   (quota-exceeded → new replicas stay Pending)
        # - Build a temporary drill carrier (SA + least-privilege Role/
        #   RoleBinding + carrier pod) when the cluster has no resident
        #   kubectl vehicle, so an in-cluster token can arm the bounded
        #   self-recovery timer. Namespace anchoring still binds every
        #   RBAC object to the approved namespace.
        # - Extend that carrier stack to the five-object variant
        #   (ClusterRole + ClusterRoleBinding) when the recovery writes
        #   touch cluster-scoped resources (e.g. restoring a node's
        #   taints/labels after a Taint→Pending drill): namespaced Roles
        #   cannot grant verbs on cluster-scoped kinds, so the carrier's
        #   timer needs cluster-scoped RBAC (B28 — the guard previously
        #   rejected these creates as scope drift, killing the run after
        #   the CLI drift budget). Cluster-scoped RBAC objects carry no
        #   namespace to anchor; the blast radius is bounded by the
        #   carrier naming convention + task-side family registration
        #   (cleanup deletes them) + the transport identity's own RBAC.
        secondary_scopes = ("pv", "persistentvolume", "configmap", "secret", "pod", "node", "resourcequota", "serviceaccount", "role", "rolebinding", "clusterrole", "clusterrolebinding")
        secondary_namespace = namespace

    # W-55-1 (方案 B, 用户钦定通用化): the recovery-carrier companion surface
    # (SA + Role/RoleBinding + carrier pod) is granted to EVERY namespaced
    # fault scope, not just workloads — the bounded self-recovery timer is
    # shared infrastructure for all in-band recovery cases and must not
    # re-break whenever a new namespaced scope type appears (B28 family:
    # #15 five-object variant, #55 service first-run — three strikes of the
    # same "new scope = companion-surface gap" pattern). Triple control is
    # unchanged: namespace anchoring + carrier naming convention + task-side
    # family registration cleanup. Workload branch already carries the union;
    # cluster-scoped / host scopes are excluded (they anchor differently and
    # historically use non-k8s carriers).
    if (scope not in CLUSTER_SCOPED_KINDS and scope != "host"
            and "serviceaccount" not in secondary_scopes):
        secondary_scopes = secondary_scopes + ("pod", "serviceaccount", "role", "rolebinding")
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
    bt = str(fault_target or params.get("target") or "").strip().lower()
    ba = str(fault_action or params.get("action") or "").strip().lower()

    # ---- Host identity (bare-metal / VM faults) --------------------------
    # Host scope is anchored by the host name (first name), not by a k8s
    # namespace/selector. The guard's host branch compares this directly.
    host_name = names[0] if is_host_scope(scope) and names else ""

    return {
        "scope": scope,
        "namespace": namespace,
        "names": names,
        "labels": labels,
        "is_namespace_wide": is_namespace_wide,
        "fault_target": bt,
        "fault_action": ba,
        "lock_fault_type": bool(lock_fault_type),
        "owner_names": list(owner_names),
        "resolved_names": list(resolved_names),
        "pvc_claims": list(pvc_claims),
        "secondary_scopes": list(secondary_scopes),
        "secondary_namespace": secondary_namespace,
        "host_name": host_name,
        # Key present ONLY when the case carries a manifest, so the
        # no-manifest snapshot stays byte-identical to today's output
        # (golden-locked). Serialized via ``entries_to_list`` for the
        # checkpointer round-trip.
        **({"mechanism_entries": entries_to_list(mechanism_entries)}
           if mechanism_entries else {}),
        # Pending-approval marker — present ONLY while the widened
        # contract awaits its knowing human, so both the no-manifest and
        # the post-approval snapshots stay byte-identical. Cleared by
        # the gate's approved re-freeze; enforced by execute_loop's
        # entry sentinel.
        **({"widening_pending_approval": True}
           if widening_pending_approval else {}),
        # Contract duration anchor (twelfth-round E4): frozen from
        # ``FaultSpec.duration_seconds`` at this single freeze point so
        # the guard can compare the execution-side ``--timeout`` against
        # the user-approved bound. Key present ONLY when the spec carried
        # a duration, keeping the no-duration snapshot byte-identical.
        **({"duration_seconds": int(duration_seconds)}
           if duration_seconds else {}),
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
        fault_target=str(d.get("fault_target") or ""),
        fault_action=str(d.get("fault_action") or ""),
        lock_fault_type=bool(d.get("lock_fault_type", True)),
        owner_names=tuple(str(n) for n in (d.get("owner_names") or [])),
        resolved_names=tuple(str(n) for n in (d.get("resolved_names") or [])),
        pvc_claims=tuple(str(n) for n in (d.get("pvc_claims") or [])),
        secondary_scopes=tuple(str(s) for s in (d.get("secondary_scopes") or [])),
        secondary_namespace=str(d.get("secondary_namespace") or ""),
        host_name=str(d.get("host_name") or ""),
        mechanism_entries=entries_from_list(d.get("mechanism_entries")),
        widening_pending_approval=bool(d.get("widening_pending_approval") or False),
        duration_seconds=int(d.get("duration_seconds") or 0),
    )


# Kinds one level BELOW the top-level workload in the ownership
# chain: a pod's direct controller of these kinds points further up
# (replicaset → deployment, job → cronjob), so the chain walk takes one
# more hop. Freezing EVERY hop's name matters for the
# generation-successor prefix check: a controller-owned pod is named
# after its DIRECT controller — a CronJob-owned pod is ``<job>-<hash>``,
# prefixed by the JOB name, not the cronjob's.
_OWNER_CHAIN_INTERMEDIATE: frozenset[str] = frozenset({"replicaset", "job"})


def _parse_controller_references(stdout: str) -> list[tuple[str, str]]:
    """Parse ``{kind}|{name}|{controller};`` jsonpath-range records.

    Only the controller entry (``controller == true`` — the API server
    guarantees at most one per object) survives; malformed records are
    skipped rather than raising (best-effort discovery).
    """
    pairs: list[tuple[str, str]] = []
    for record in (stdout or "").split(";"):
        parts = [p.strip() for p in record.split("|")]
        if len(parts) != 3:
            continue
        kind_raw, name, controller = parts
        if controller.lower() != "true" or not name:
            continue
        kind = canonicalise_kind(kind_raw)
        if kind and name:
            pairs.append((kind, name))
    return pairs


async def _controller_reference_of(
    kind: str,
    name: str,
    namespace: str,
    kubeconfig: str,
) -> Optional[tuple[str, str]]:
    """One ownerReferences query → the object's controller (kind, name).

    Returns ``None`` on any failure or when the object has no
    controller reference (bare pod, top-level workload) — the caller
    treats both as "chain ends here".
    """
    out = await query_kubectl(
        _jsonpath_get_args(
            kind,
            "{range .metadata.ownerReferences[*]}"
            "{.kind}|{.name}|{.controller};{end}",
            namespace=namespace, name=name,
        ),
        kubeconfig,
        log_name=f"controller-ref {kind}/{name}",
    )
    pairs = _parse_controller_references(out.text)
    return pairs[0] if pairs else None


async def _discover_owners_by_names(
    namespace: str,
    pod_names: tuple[str, ...],
    kubeconfig: str,
) -> list[str]:
    """Resolve each named pod's controller owner chain (≤2 hops).

    Hop 1: the pod's own controller reference (usually a ReplicaSet;
    DaemonSet/StatefulSet/Job own their pods directly). Hop 2, only
    when hop 1 landed on a chain-intermediate kind: that controller's
    own controller reference (Deployment / CronJob). Names collected at
    every completed hop count — a failed hop just ends that chain
    (best-effort, fail-closed downstream when nothing was found).
    """
    owners: list[str] = []
    seen: set[str] = set()
    for pod_name in pod_names:
        pod_name = str(pod_name).strip()
        if not pod_name or pod_name in seen:
            continue
        seen.add(pod_name)
        direct = await _controller_reference_of(
            "pod", pod_name, namespace, kubeconfig,
        )
        if direct is None:
            continue
        direct_kind, direct_name = direct
        owners.append(direct_name)
        if direct_kind in _OWNER_CHAIN_INTERMEDIATE:
            upper = await _controller_reference_of(
                direct_kind, direct_name, namespace, kubeconfig,
            )
            if upper is not None:
                owners.append(upper[1])
    return owners


async def discover_owner_names(
    scope: str,
    namespace: str,
    labels: dict[str, str],
    kubeconfig: str = "",
    names: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Query the cluster for owner resources of the approved target.

    Two discovery channels, unioned:

    - **labels channel**: ``scope=pod`` with labels finds
      Deployments/DaemonSets/StatefulSets (etc.) in the same namespace
      whose ``spec.selector.matchLabels`` are a subset of the given
      labels, so the guard can validate owner-scope operations at the
      instance level.
    - **names channel** (the generation anchor, case #39): ``scope=pod``
      (or ``container``) with explicit pod names resolves each pod's
      ``ownerReferences`` chain into the frozen owner set. A pod whose
      mechanism deletes it and lets the controller recreate it ALWAYS
      changes name (``<deployment>-<rs-hash>-<pod-hash>``) — the
      persistent identity is the owner chain, not the name, and the
      approval must freeze the chain so the guard can recognise the
      recreated successor instead of rejecting it as drift.

    Best-effort: returns empty tuple on any failure (guard falls back
    to namespace-only anchoring — fail closed).
    """
    scope_l = (scope or "").strip().lower()

    # ---- labels channel (unchanged behaviour) --------------------------
    found: list[str] = []
    if scope in OWNER_SCOPES and labels and namespace:
        owner_kinds = OWNER_SCOPES[scope]
        for kind in sorted(owner_kinds):
            out = await query_kubectl(
                _jsonpath_get_args(
                    kind, "{.items[*].metadata.name}",
                    namespace=namespace, labels=labels,
                ),
                kubeconfig,
                log_name=f"owner-names {kind}",
            )
            found.extend(out.words)

    # ---- names channel (generation anchor) -----------------------------
    if scope_l in ("pod", "container") and namespace and names:
        found.extend(await _discover_owners_by_names(
            namespace, tuple(names), kubeconfig,
        ))

    if found:
        unique = tuple(sorted(set(found)))
        logger.info(
            "discover_owner_names: found owners %s (scope=%s labels=%s "
            "names=%s in ns=%s)",
            list(unique), scope, labels, list(names), namespace,
        )
        return unique
    return tuple(found)


async def discover_names_by_labels(
    scope: str,
    namespace: str,
    labels: dict[str, str],
    kubeconfig: str = "",
) -> tuple[str, ...]:
    """Resolve a LABEL selector to the concrete resource names it matches.

    For a label-approved fault (e.g. an availability-zone node partition
    approved by ``labels={topology.kubernetes.io/zone: ...}``, or a pod fault
    approved by ``labels={app: ...}``), execution legitimately fans out per
    resource name (kubectl-native needs one debug Pod per node; batched name
    targeting for pods). Freezing the resolved name set lets the drift guard
    validate ``effective.names ⊆ resolved_names`` instead of rejecting the
    labels↔names cross as spurious drift — while still catching a genuinely
    out-of-selector name.

    Supported scopes: ``node`` (cluster-scoped) and ``pod`` (``container``
    normalises to ``pod``; requires a namespace). Other scopes return an empty
    tuple (owner-scope workloads are already anchored by ``owner_names``).
    Best-effort: returns an empty tuple on any failure (guard falls back to the
    previous labels-only behaviour).
    """
    scope_l = (scope or "").strip().lower()
    if scope_l == "container":
        scope_l = "pod"
    kind = {"node": "nodes", "pod": "pods"}.get(scope_l)
    if kind is None or not labels:
        return ()
    # Namespaced kinds need a namespace to scope the query; cluster-scoped
    # (nodes) must NOT carry one.
    if kind == "pods" and not namespace:
        return ()

    out = await query_kubectl(
        _jsonpath_get_args(
            kind, "{.items[*].metadata.name}",
            namespace=namespace if kind == "pods" else "",
            labels=labels,
        ),
        kubeconfig,
        log_name=f"names-by-labels {kind}",
    )
    found = out.words
    if found:
        logger.info("discover_names_by_labels: %s labels=%s resolved to %d name(s)",
                     kind, labels, len(found))
    return found


async def discover_pod_pvc_claims(
    namespace: str,
    pod_names: tuple[str, ...],
    kubeconfig: str = "",
) -> tuple[str, ...]:
    """Resolve the PVC claim names referenced by concrete pods.

    Frozen into ``approved_target.pvc_claims`` so the drill-occupancy-vehicle
    exception can anchor on them: an occupant pod may only claim a PVC the
    approved target actually uses. Best-effort: returns an empty tuple on any
    failure (the occupant exception then stays refused — fail closed).
    """
    if not namespace or not pod_names:
        return ()

    claims: set[str] = set()
    for pod_name in pod_names:
        out = await query_kubectl(
            _jsonpath_get_args(
                "pod",
                "{.spec.volumes[*].persistentVolumeClaim.claimName}",
                namespace=namespace, name=pod_name,
            ),
            kubeconfig,
            log_name=f"pod-claims {pod_name}",
        )
        claims.update(out.words)

    if claims:
        logger.info(
            "discover_pod_pvc_claims: pods %s in ns=%s reference claims %s",
            list(pod_names), namespace, sorted(claims),
        )
    return tuple(sorted(claims))


# B79 (case #39-R): scopes whose PVC claims are authored in the pod
# TEMPLATE. The claimName a workload's pods mount is the same string the
# pod channel reads off a live pod, so a workload-scope approval freezes
# the same anchor a pod-scope approval of its pods would — the LLM's
# scope-extraction variance (same intent, "pod" one run, "deployment"
# the next) stops changing which guard paths a drill can take.
WORKLOAD_TEMPLATE_SCOPES = frozenset({
    "deployment", "daemonset", "replicaset",
    "job", "cronjob", "replicationcontroller",
})

# Where the pod template (and thus the PVC claims) sits per kind.
# CronJob nests one level deeper (jobTemplate → template).
_TEMPLATE_CLAIM_JSONPATH = {
    "deployment": "{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}",
    "daemonset": "{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}",
    "replicaset": "{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}",
    "job": "{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}",
    "cronjob": "{.spec.jobTemplate.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}",
    "replicationcontroller": "{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}",
}


async def _run_kubectl_query(args: list, kubeconfig: str):
    """Deprecated alias kept only for in-tree tests; see :func:`query_kubectl`.

    B81/B82 rework: the guard's reads now go through the tri-state
    :func:`chaos_agent.tools.kubectl.query_kubectl` (error ≠ empty ≠
    value) and are rendered by :func:`_jsonpath_get_args`. This stub
    exists so any straggler caller keeps working during the migration;
    new code MUST NOT use it.
    """
    from chaos_agent.tools.kubectl import query_kubectl
    return await query_kubectl(args, kubeconfig)


def _jsonpath_get_args(
    kind: str,
    path: str,
    *,
    namespace: str = "",
    name: str = "",
    labels: Mapping[str, str] | None = None,
    label_selector: str = "",
    items: bool = False,
) -> list[str]:
    """Single render point for every jsonpath read the guard issues (B81).

    The ``jsonpath=`` prefix used to live at six separate assembly
    sites until one of them dropped it and kubectl treated the bare
    expression as an unknown output format (exit 1 → silent empty
    set → the guard honestly-but-wrongly banned the occupant channel).
    Rendering here makes the prefix a construction-level invariant:
    every ``-o`` token this module emits is built from THIS f-string,
    and a hand-built output flag cannot reappear. The path sanity
    check is fail-loud on purpose — a malformed path is a PROGRAM
    error (typo in a code constant), not a cluster state, and hiding
    it behind an empty query would repeat B81 with a different body.
    """
    if not path.startswith("{"):
        raise ValueError(f"jsonpath must start with '{{': {path!r}")
    if items:
        path = path.replace("{.", "{.items[*].", 1)
    args: list[str] = [kind]
    if name:
        args.append(str(name))
    if namespace:
        args += ["-n", namespace]
    if labels:
        selector = ",".join(f"{k}={v}" for k, v in labels.items())
        args += ["-l", selector]
    elif label_selector:
        args += ["-l", label_selector]
    args += ["-o", f"jsonpath={path}"]
    return args


def _parse_match_labels_map(raw: str) -> str:
    """``map[app:web tier:backend]`` → ``app=web,tier=backend``.

    kubectl jsonpath renders a string-map field as ``map[k:v ...]``. K8s
    label keys/values never contain spaces or colons (label grammar), so
    whitespace-then-colon tokenisation is exact — no quoting edge cases.
    """
    raw = (raw or "").strip()
    if not raw.startswith("map[") or not raw.endswith("]"):
        return ""
    inner = raw[4:-1].strip()
    if not inner:
        return ""
    parts = []
    for token in inner.split(" "):
        k, sep, v = token.partition(":")
        if sep and k and v:
            parts.append(f"{k}={v}")
    return ",".join(parts)


async def discover_workload_pvc_claims(
    scope: str,
    namespace: str,
    names: tuple[str, ...],
    labels: dict[str, str],
    kubeconfig: str = "",
) -> tuple[str, ...]:
    """Resolve PVC claim names from a workload's pod template (B79).

    For deployment/daemonset/replicaset/job/cronjob/rc scopes the claim
    a replica mounts is authored in the pod template, so the frozen set
    is exactly the PVCs every replica of the approved target uses — no
    ghost entries (a template claimName IS a real PVC reference). The
    label form resolves via ``-l`` on the same kind. Best-effort: any
    failure or empty output yields an empty tuple (the occupant
    exception stays refused — fail closed, same as the pod channel).
    """
    scope_l = (scope or "").strip().lower()
    path = _TEMPLATE_CLAIM_JSONPATH.get(scope_l)
    if not path or not namespace or (not names and not labels):
        return ()
    kind = scope_l  # canonical singular — kubectl accepts it directly
    claims: set[str] = set()
    if names:
        for name in names:
            out = await query_kubectl(
                _jsonpath_get_args(
                    kind, path, namespace=namespace, name=name,
                ),
                kubeconfig,
                log_name=f"workload-claims {kind}/{name}",
            )
            claims.update(out.words)
    else:
        out = await query_kubectl(
            _jsonpath_get_args(
                kind, path, namespace=namespace, labels=labels, items=True,
            ),
            kubeconfig,
            log_name=f"workload-claims {kind} by labels",
        )
        claims.update(out.words)
    if claims:
        logger.info(
            "discover_workload_pvc_claims: %s %s in ns=%s reference "
            "claims %s",
            kind, list(names) or dict(labels), namespace, sorted(claims),
        )
    return tuple(sorted(claims))


async def discover_statefulset_pvc_claims(
    namespace: str,
    names: tuple[str, ...],
    labels: dict[str, str],
    kubeconfig: str = "",
) -> tuple[str, ...]:
    """Resolve PVC claims for a StatefulSet approval via its live pods (B79).

    STS claims are per-replica INSTANCES of ``volumeClaimTemplates``
    (``data-web-0``), not the template names — whitelisting a template
    name would admit a ghost entry (a template ``data`` is not the PVC
    any replica mounts, yet an unrelated PVC literally named ``data``
    would pass the subset check). Ground truth is what the live pods
    mount, so the selector is resolved to pods and the existing pod
    channel is reused verbatim. Scaled-to-zero (or any query failure)
    yields an empty tuple — fail closed at the guard, honest when the
    target genuinely has no mounted storage.
    """
    if not namespace or (not names and not labels):
        return ()
    sts_names = list(names)
    if not sts_names and labels:
        out = await query_kubectl(
            _jsonpath_get_args(
                "statefulset", "{.items[*].metadata.name}",
                namespace=namespace, labels=labels,
            ),
            kubeconfig,
            log_name="sts-claims sts by labels",
        )
        sts_names = list(out.words)
    claims: set[str] = set()
    for sts_name in sts_names:
        raw = await query_kubectl(
            _jsonpath_get_args(
                "statefulset", "{.spec.selector.matchLabels}",
                namespace=namespace, name=sts_name,
            ),
            kubeconfig,
            log_name=f"sts-claims {sts_name} selector",
        )
        selector = _parse_match_labels_map(raw.text)
        if not selector:
            continue
        pods_out = await query_kubectl(
            _jsonpath_get_args(
                "pods", "{.items[*].metadata.name}",
                namespace=namespace, label_selector=selector,
            ),
            kubeconfig,
            log_name=f"sts-claims pods of {sts_name}",
        )
        pod_names = pods_out.words
        if pod_names:
            claims.update(await discover_pod_pvc_claims(
                namespace, pod_names, kubeconfig,
            ))
    if claims:
        logger.info(
            "discover_statefulset_pvc_claims: sts %s in ns=%s resolved "
            "to claims %s",
            list(sts_names), namespace, sorted(claims),
        )
    return tuple(sorted(claims))


__all__ = [
    "approved_from_dict",
    "discover_names_by_labels",
    "discover_owner_names",
    "discover_pod_pvc_claims",
    "discover_statefulset_pvc_claims",
    "discover_workload_pvc_claims",
    "WORKLOAD_TEMPLATE_SCOPES",
    "freeze_approved_target",
    "freeze_approved_target_from_spec",
]
