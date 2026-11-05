"""Single source of truth for resolving a fault target's identity into the
concrete cluster object a pod-level probe must address.

Why this module exists (W-83-5, #83 audit root-fix)
---------------------------------------------------
The "workload target → observable Pod" resolution used to be a PRIVATE helper
of the baseline node (``baseline_capture._resolve_target_pod``, built for Case
#61 / W-61-1). Every OTHER node that needed pod-level addressing re-derived it
from ``target_names[0]`` — and repeated #61. The side-effect logs channel
issued ``kubectl logs <deployment-name>`` (a bare workload name), which kubectl
resolves as a **Pod** → ``NotFound`` → the ``target_logs`` capture channel went
Unreadable → ``DependencyErrorDetector`` was silently skipped for the ENTIRE
workload class (deployment/statefulset/daemonset). Across ≥6 cases this read as
a benign ``NotFound`` and was mislabeled "cosmetic"; the summary even printed
``DependencyErrors: 0`` (a skipped detector rendered as a clean zero).

The invariant this module enforces (the #61 / W-61-1 invariant, generalized):

    A pod-level probe (``logs`` / ``exec`` / ``top`` / ``describe pod``) must
    address a literal POD name. A workload name is NEVER a valid substitute —
    kubectl resolves a bare name as a Pod, so the substitution fails NotFound
    and the failure launders into a silent skip / green receipt. An
    unresolvable target yields a VISIBLE ABSENCE (``None``), never a guess.

Consumers (baseline ``_resolve_target_pod`` / ``_discover_pod_selector`` and
the side-effect logs channel) call into here, so no node can hand-roll pod
addressing again. The transport executor is injectable (``executor=``) so a
caller keeps its own module-level binding — the resolution LOGIC is single-
sourced without forcing every consumer to share one patch target.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from chaos_agent.config.settings import settings
from chaos_agent.tools.kubectl_cli import build_kubectl_cmd
from chaos_agent.transports import (
    PROFILE_K8S,
    TransportTarget,
    resolve_transport_target,
)

logger = logging.getLogger(__name__)

# jsonpath for the pod-owning selector of each workload-kind scope. Service
# selectors live at ``.spec.selector`` directly (no matchLabels wrapper);
# workload selectors at ``.spec.selector.matchLabels``. A selectorless service
# returns an empty/None jsonpath and discovery fail-opens to None.
#
# Single source: baseline_capture re-imports this under its historical private
# name so the two never drift.
POD_OWNER_SELECTOR_JSONPATH: dict[str, str] = {
    "deployment": "{.spec.selector.matchLabels}",
    "statefulset": "{.spec.selector.matchLabels}",
    "daemonset": "{.spec.selector.matchLabels}",
    "service": "{.spec.selector}",
}

# An injectable transport executor: same signature as
# ``chaos_agent.transports.execute_via_transport``. Callers that keep their own
# module-level binding (baseline_capture, so its existing test patch target is
# preserved) pass it explicitly; the default resolves lazily to the shared one.
_Executor = Callable[..., Awaitable[Any]]


def _resolve_executor(executor: _Executor | None) -> _Executor:
    """Default executor resolved LAZILY from ``chaos_agent.transports`` so the
    seam is interceptable by the same ``monkeypatch`` on
    ``chaos_agent.transports.execute_via_transport`` that the rest of the
    side-effect IO uses (a top-level binding would freeze the original and
    defeat the patch). An explicit ``executor`` always wins."""
    if executor is not None:
        return executor
    from chaos_agent.transports import execute_via_transport
    return execute_via_transport


async def discover_pod_selector(
    *,
    scope: str,
    name: str,
    namespace: str,
    kubeconfig: str,
    task_id: str = "",
    profile: str = PROFILE_K8S,
    executor: _Executor | None = None,
    target: TransportTarget | None = None,
) -> dict[str, str] | None:
    """Authoritative pod-selector discovery: ask the API server for the
    workload's OWN selector (``kubectl get <kind> <name> -o jsonpath=<sel>``).

    Needed because ``spec.labels`` is structurally EMPTY under a workload scope
    (the B31 scope/name guards reject a pod probe's labels), so a consumer that
    only has the workload name cannot build a ``-l`` selector without asking
    the workload itself.

    Pure query + parse. Derive-specific gates and tracker recording stay in the
    caller (baseline's ``_discover_pod_selector`` wrapper). Fail-open: any
    error / non-zero exit / unparseable or empty output → ``None``.
    """
    if scope not in POD_OWNER_SELECTOR_JSONPATH or not name:
        return None
    _exec = _resolve_executor(executor)
    v_args = [scope, name]
    if namespace:
        v_args += ["-n", namespace]
    v_args += ["-o", f"jsonpath={POD_OWNER_SELECTOR_JSONPATH[scope]}"]
    _target = target if target is not None else resolve_transport_target(None)
    try:
        cmd = build_kubectl_cmd("get", v_args, kubeconfig=kubeconfig, target=_target)
        result = await _exec(
            cmd, _target,
            timeout=settings.timeout_kubectl, task_id=task_id,
            expect_profile=profile,
        )
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        logger.info("Pod selector discovery for %s %s failed: %s", scope, name, e)
        return None
    raw = (result.stdout or "").strip()
    if result.exit_code != 0 or not raw:
        return None
    try:
        selector = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(selector, dict):
        return None
    selector = {
        str(k): str(v)
        for k, v in selector.items()
        if v is not None and str(v) != ""
    }
    return selector or None


async def resolve_pod_by_selector(
    *,
    selector: dict[str, str] | None,
    namespace: str,
    kubeconfig: str,
    field_selector: str | None = None,
    task_id: str = "",
    profile: str = PROFILE_K8S,
    executor: _Executor | None = None,
    target: TransportTarget | None = None,
) -> str | None:
    """The single ``selector → literal pod name`` query:
    ``kubectl get pods -l <sel> -o jsonpath={.items[0].metadata.name}``.

    This is the primitive both the baseline ``_resolve_target_pod`` and the
    side-effect logs channel consume — the exact query whose absence caused
    W-83-5. Single-target by design (``items[0]``), mirroring baseline's
    deliberate one-pod-per-run resolution.

    ``field_selector`` is an OPTIONAL ``--field-selector`` filter (e.g.
    ``status.phase=Running``). The feasibility checkers need a Running pod to
    probe; baseline / side-effect address the frozen target regardless of phase
    and leave it ``None`` so their query is byte-identical to before.

    Fail-open: missing selector / any error / non-zero exit / empty output (no
    pod matched) → ``None``. NEVER returns a workload name.
    """
    if not selector:
        return None
    _exec = _resolve_executor(executor)
    selector_str = ",".join(f"{k}={v}" for k, v in selector.items())
    v_args = ["pods", "-l", selector_str]
    if namespace:
        v_args += ["-n", namespace]
    if field_selector:
        v_args += ["--field-selector", field_selector]
    v_args += ["-o", "jsonpath={.items[0].metadata.name}"]
    _target = target if target is not None else resolve_transport_target(None)
    try:
        cmd = build_kubectl_cmd("get", v_args, kubeconfig=kubeconfig, target=_target)
        result = await _exec(
            cmd, _target,
            timeout=settings.timeout_kubectl, task_id=task_id,
            expect_profile=profile,
        )
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        logger.info("Pod resolution by selector %s failed: %s", selector_str, e)
        return None
    pod = (result.stdout or "").strip()
    if result.exit_code != 0 or not pod:
        return None
    return pod


async def resolve_pods_by_selector(
    *,
    selector: dict[str, str] | None,
    namespace: str,
    kubeconfig: str,
    field_selector: str | None = None,
    task_id: str = "",
    profile: str = PROFILE_K8S,
    executor: _Executor | None = None,
    target: TransportTarget | None = None,
    source: str = "",
) -> tuple[str, ...]:
    """The SET twin of :func:`resolve_pod_by_selector`: ``selector → EVERY
    matching pod name``, ``kubectl get pods -l <sel> -o
    jsonpath={.items[*].metadata.name}``.

    Why the seam grew a SET form (the W-83-5 duplication shape, one axis up):
    the single-value resolvers above answer "which ONE pod do I address", but
    several consumers need the WHOLE victim set — the planning
    ``tool_screener`` selector cross-shape probe (does the executed label
    selector pick exactly the approved names?) and ``target_health``'s
    ``_resolve_pod_names`` (every Running pod behind a label approval). Each
    hand-rolled its own ``{.items[*].metadata.name}`` query — the latter the
    R69 "``Error from server …`` split into pod names" site. That is the same
    private-reimplementation drift the #83 audit closed on the single-value
    axis, so the SET form is single-sourced here too; consumers keep only
    their wrapper concerns (caching, fail-closed-vs-fail-open policy, the
    labels-empty fallback).

    DELIBERATELY still NOT subsuming the target-guard ``freeze`` enumerations
    (``discover_owner_names`` / ``discover_names_by_labels`` /
    ``discover_statefulset_pvc_claims`` / ``discover_victim_nodes``): those
    resolve ARBITRARY kinds' name / node / PVC SETs for the approval snapshot
    through the guard's own tri-state ``query_kubectl`` + ``_jsonpath_get_args``
    single-render-point (B81) plumbing — a different concern (approval
    completeness, fail-CLOSED) on different infrastructure, allowlisted by the
    completeness sentinel. This resolver owns the pod-probe addressing SET only.

    ``field_selector`` mirrors :func:`resolve_pod_by_selector` (e.g.
    ``status.phase=Running`` for the health face); ``source`` is forwarded to
    the transport for attribution (the screener's ``selector-name-probe``).

    Fail-open: missing selector / any error / non-zero exit / empty output →
    ``()``. NEVER a workload name, NEVER an error-string token — the query
    gates on ``exit_code`` and splits only the undecorated stdout, so a failed
    query yields ``()`` rather than laundering ``Error from server …`` into
    pod names (the R69 invariant, isomorphic to the single-value axis).
    """
    if not selector:
        return ()
    _exec = _resolve_executor(executor)
    selector_str = ",".join(f"{k}={v}" for k, v in selector.items())
    v_args = ["pods", "-l", selector_str]
    if namespace:
        v_args += ["-n", namespace]
    if field_selector:
        v_args += ["--field-selector", field_selector]
    v_args += ["-o", "jsonpath={.items[*].metadata.name}"]
    _target = target if target is not None else resolve_transport_target(None)
    try:
        cmd = build_kubectl_cmd("get", v_args, kubeconfig=kubeconfig, target=_target)
        result = await _exec(
            cmd, _target,
            timeout=settings.timeout_kubectl, task_id=task_id,
            source=source, expect_profile=profile,
        )
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        logger.info("Pods resolution by selector %s failed: %s", selector_str, e)
        return ()
    if result.exit_code != 0:
        return ()
    return tuple((result.stdout or "").split())


async def resolve_target_pod_name(
    *,
    scope: str,
    names: list[str] | tuple[str, ...],
    namespace: str,
    kubeconfig: str,
    labels: dict[str, str] | None = None,
    selector: dict[str, str] | None = None,
    discover_selector: bool = False,
    field_selector: str | None = None,
    task_id: str = "",
    profile: str = PROFILE_K8S,
    executor: _Executor | None = None,
    target: TransportTarget | None = None,
) -> str | None:
    """Resolve a fault target into the literal POD name a pod-level probe must
    address — the single authority every pod-addressing observer consumes.

    Resolution rules by scope:

    * ``pod``: ``names[0]`` IS the literal pod name (frozen by confirmation_gate
      as part of the approved_target snapshot) — zero extra calls.
    * workload (``deployment`` / ``statefulset`` / ``daemonset`` / ``service``):
      ``selector`` (a pre-discovered one, e.g. baseline's Phase 1.5
      ``pod_selector``) → else ``labels`` → else, when ``discover_selector`` is
      set, :func:`discover_pod_selector` asks the workload for its own
      selector; then :func:`resolve_pod_by_selector` maps selector → pod.
    * ``node`` / ``host`` / unknown: not applicable — ``None``.

    ``discover_selector`` is opt-in: baseline pre-discovers the selector
    upstream (and passes it via ``selector=``), so it keeps its zero-redundant-
    call behavior; the side-effect node has no pre-discovery and sets
    ``discover_selector=True`` to resolve self-containedly.

    Fail-open (any error / non-zero exit / empty output / missing selector):
    return ``None``. NEVER a guess, NEVER ``names[0]`` as a workload-name
    substitute — the #61 / W-61-1 invariant. A ``None`` result must surface as
    a VISIBLE ABSENCE at the consumer (an Unreadable channel / a skipped
    observation), never as a silent wrong-value.
    """
    name_list = list(names or ())
    if profile != PROFILE_K8S:
        return None
    if scope == "pod":
        return name_list[0] if name_list else None
    if scope not in POD_OWNER_SELECTOR_JSONPATH:
        return None  # node / host / unknown — no pod to address

    sel = selector or (dict(labels) if labels else None)
    if not sel and discover_selector and name_list:
        sel = await discover_pod_selector(
            scope=scope, name=name_list[0], namespace=namespace,
            kubeconfig=kubeconfig, task_id=task_id, profile=profile,
            executor=executor, target=target,
        )
    if not sel:
        return None
    return await resolve_pod_by_selector(
        selector=sel, namespace=namespace, kubeconfig=kubeconfig,
        field_selector=field_selector,
        task_id=task_id, profile=profile, executor=executor, target=target,
    )


# ---------------------------------------------------------------------------
# Node axis — the ``… → hosting NODE name`` primitives
# ---------------------------------------------------------------------------
#
# The isomorphic twin of the pod axis above. Before the #83 audit the
# ``.spec.nodeName`` read was hand-rolled in FOUR places (baseline's
# ``_resolve_target_node`` pod + workload branches, the feasibility network
# checker's ``_fetch_pod_node``, the planning ``tool_screener`` node-binding
# probe, and the execute ``_effect_channels`` tool-pod-on-node lookup, since
# retired with the programmatic post-check seam) — the exact duplication
# shape that caused W-83-5 on the pod axis. Two primitives
# now single-source it; every consumer delegates and keeps only its own
# wrapper concerns (tracker recording, caching, fail-closed vs fail-open).
#
# Deliberately NOT provided: a ``resolve_target_node_name(scope, …)``
# orchestrator. Baseline keeps its own per-scope branch dispatch because each
# branch writes a distinct tracker message (Identity-axiom transparency), and
# no other consumer needs the whole orchestration — an unused orchestrator
# would be dead code the completeness sentinel has to reason about.


async def resolve_node_of_pod(
    *,
    pod_name: str,
    namespace: str,
    kubeconfig: str,
    task_id: str = "",
    profile: str = PROFILE_K8S,
    executor: _Executor | None = None,
    target: TransportTarget | None = None,
) -> str | None:
    """The single ``pod → hosting node`` query:
    ``kubectl get pod <name> -o jsonpath={.spec.nodeName}``.

    The node-axis twin of :func:`resolve_pod_by_selector`. Consumed by
    baseline's pod-scope node resolution, the feasibility network checker
    (``_fetch_pod_node``), and the planning/execute node-binding probes — so
    no node can hand-roll ``.spec.nodeName`` addressing again.

    Fail-open: empty pod_name / any error / non-zero exit / empty output →
    ``None``. NEVER a guess, NEVER the pod name as a node substitute.
    """
    if not pod_name:
        return None
    _exec = _resolve_executor(executor)
    v_args = ["pod", pod_name]
    if namespace:
        v_args += ["-n", namespace]
    v_args += ["-o", "jsonpath={.spec.nodeName}"]
    _target = target if target is not None else resolve_transport_target(None)
    try:
        cmd = build_kubectl_cmd("get", v_args, kubeconfig=kubeconfig, target=_target)
        result = await _exec(
            cmd, _target,
            timeout=settings.timeout_kubectl, task_id=task_id,
            expect_profile=profile,
        )
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        logger.info("Node-of-pod resolution for %s failed: %s", pod_name, e)
        return None
    node = (result.stdout or "").strip()
    if result.exit_code != 0 or not node:
        return None
    return node


async def resolve_node_by_selector(
    *,
    selector: dict[str, str] | None,
    namespace: str,
    kubeconfig: str,
    field_selector: str | None = None,
    task_id: str = "",
    profile: str = PROFILE_K8S,
    executor: _Executor | None = None,
    target: TransportTarget | None = None,
) -> str | None:
    """The single ``selector → first matching pod's hosting node`` query:
    ``kubectl get pods -l <sel> -o jsonpath={.items[0].spec.nodeName}``.

    Workload-scope twin of :func:`resolve_node_of_pod`: a workload target has
    no single frozen pod name, so its node is the node of ``items[0]`` —
    baseline's deliberate one-pod-per-run resolution, mirrored. Single-target
    by design; the multi-node SET form (``items[*]``) the target-guard freeze
    needs is a DIFFERENT semantic and stays there (see freeze's exclusion).

    Fail-open: missing selector / any error / non-zero exit / empty output →
    ``None``. NEVER a workload name, NEVER a guess.
    """
    if not selector:
        return None
    _exec = _resolve_executor(executor)
    selector_str = ",".join(f"{k}={v}" for k, v in selector.items())
    v_args = ["pods", "-l", selector_str]
    if namespace:
        v_args += ["-n", namespace]
    if field_selector:
        v_args += ["--field-selector", field_selector]
    v_args += ["-o", "jsonpath={.items[0].spec.nodeName}"]
    _target = target if target is not None else resolve_transport_target(None)
    try:
        cmd = build_kubectl_cmd("get", v_args, kubeconfig=kubeconfig, target=_target)
        result = await _exec(
            cmd, _target,
            timeout=settings.timeout_kubectl, task_id=task_id,
            expect_profile=profile,
        )
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        logger.info("Node-by-selector resolution for %s failed: %s", selector_str, e)
        return None
    node = (result.stdout or "").strip()
    if result.exit_code != 0 or not node:
        return None
    return node
