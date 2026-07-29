"""Side-effect detection framework: Snapshot-Diff architecture.

Two graph nodes use this module:
  - se_snapshot (pre-injection): calls capture_snapshot()
  - se_detect (post-verification): calls fetch_post_inject_state() + run_all_detectors()

Each detector is a pure synchronous function that receives the before/after
state and returns incremental side-effects. All IO is done upfront by the
runner, keeping detectors trivially testable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class PodSnapshot:
    name: str
    namespace: str
    phase: str
    restart_counts: dict[str, int] = field(default_factory=dict)
    oom_killed_containers: set[str] = field(default_factory=set)
    crash_loop_containers: set[str] = field(default_factory=set)
    evicted: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "phase": self.phase,
            "restart_counts": self.restart_counts,
            "oom_killed_containers": list(self.oom_killed_containers),
            "crash_loop_containers": list(self.crash_loop_containers),
            "evicted": self.evicted,
        }

    @classmethod
    def from_dict(cls, d: dict) -> PodSnapshot:
        return cls(
            name=d["name"],
            namespace=d.get("namespace", ""),
            phase=d.get("phase", ""),
            restart_counts=d.get("restart_counts", {}),
            oom_killed_containers=set(d.get("oom_killed_containers", [])),
            crash_loop_containers=set(d.get("crash_loop_containers", [])),
            evicted=d.get("evicted", False),
        )


@dataclass
class EndpointSnapshot:
    service: str
    ready_count: int

    def to_dict(self) -> dict:
        return {"service": self.service, "ready_count": self.ready_count}

    @classmethod
    def from_dict(cls, d: dict) -> EndpointSnapshot:
        return cls(service=d["service"], ready_count=d.get("ready_count", 0))


@dataclass
class HostSnapshot:
    """Pre-injection host-wide state for the ``host`` profile observer.

    Broad-spectrum environment picture used to surface unintended collateral
    on a bare host: which processes/services were alive, per-mount usage, and
    a cursor into the kernel ring buffer so post-inject diffing only considers
    *new* dmesg lines.
    """
    processes: set[str] = field(default_factory=set)
    mounts: dict[str, int] = field(default_factory=dict)
    services: dict[str, str] = field(default_factory=dict)
    dmesg_line_count: int = 0

    def to_dict(self) -> dict:
        return {
            "processes": sorted(self.processes),
            "mounts": self.mounts,
            "services": self.services,
            "dmesg_line_count": self.dmesg_line_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> HostSnapshot:
        return cls(
            processes=set(d.get("processes", [])),
            mounts=d.get("mounts", {}),
            services=d.get("services", {}),
            dmesg_line_count=d.get("dmesg_line_count", 0),
        )


@dataclass
class SideEffectSnapshot:
    captured_at: str
    namespace: str
    pods: dict[str, PodSnapshot] = field(default_factory=dict)
    endpoints: dict[str, EndpointSnapshot] = field(default_factory=dict)
    # host profile payload (None on the k8s path — k8s fields untouched).
    host: HostSnapshot | None = None
    # primary-effect metrics for the fault dimension, captured by reusing the
    # feasibility ``(profile, target)`` probe. Empty when no probe applies.
    primary_metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "captured_at": self.captured_at,
            "namespace": self.namespace,
            "pods": {k: v.to_dict() for k, v in self.pods.items()},
            "endpoints": {k: v.to_dict() for k, v in self.endpoints.items()},
            "host": self.host.to_dict() if self.host else None,
            "primary_metrics": self.primary_metrics,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SideEffectSnapshot:
        pods = {k: PodSnapshot.from_dict(v) for k, v in d.get("pods", {}).items()}
        endpoints = {k: EndpointSnapshot.from_dict(v) for k, v in d.get("endpoints", {}).items()}
        host_d = d.get("host")
        return cls(
            captured_at=d.get("captured_at", ""),
            namespace=d.get("namespace", ""),
            pods=pods,
            endpoints=endpoints,
            host=HostSnapshot.from_dict(host_d) if host_d else None,
            primary_metrics=d.get("primary_metrics", {}),
        )


@dataclass
class HostPostInjectState:
    """Current host state queried after verification, diffed against a
    :class:`HostSnapshot`. ``dmesg_lines`` holds only lines *after* the
    baseline cursor so kernel-log detectors see just what the fault produced.
    """
    processes: set[str] = field(default_factory=set)
    mounts: dict[str, int] = field(default_factory=dict)
    services: dict[str, str] = field(default_factory=dict)
    dmesg_lines: list[str] = field(default_factory=list)


@dataclass
class PostInjectState:
    """Current namespace state queried after verification."""
    pods_json: dict = field(default_factory=dict)
    events_json: dict = field(default_factory=dict)
    endpoints_json: dict = field(default_factory=dict)
    target_logs: str = ""
    captured_at: str = ""
    # host profile payload (None on the k8s path).
    host: HostPostInjectState | None = None
    # primary-effect metrics for the fault dimension (see SideEffectSnapshot).
    primary_metrics: dict = field(default_factory=dict)


@dataclass
class DetectionContext:
    namespace: str
    target_names: list[str]
    scope: str
    kubeconfig: str
    injection_start_time: str
    task_id: str
    # target axis: the fault dimension (mem/cpu/disk/network/process). Used to
    # filter dimension-relevant detectors. Empty = unknown → no target filter
    # (all profile detectors run, preserving broad side-effect discovery).
    target: str = ""
    # profile axis: "k8s" | "host". Selects the detector group + observer.
    profile: str = PROFILE_K8S


# ---------------------------------------------------------------------------
# Detector Protocol & Registry
# ---------------------------------------------------------------------------


class SideEffectDetector(Protocol):
    key: str
    # target axis: fault dimensions this detector is relevant to. Empty tuple =
    # dimension-agnostic (always runs, e.g. CrashLoop/ProbeFailure — a broad
    # anomaly worth flagging regardless of fault). Non-empty = only runs when
    # ctx.target matches (e.g. OOMKilledSibling → ("mem",)).
    applies_to_targets: tuple[str, ...]

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        ...


# profile → ordered detectors. New backends/profiles append their own group
# without touching k8s; the built-in k8s detectors register into "k8s".
_DETECTORS: dict[str, list[SideEffectDetector]] = {}


def register(d: SideEffectDetector, profile: str = PROFILE_K8S) -> None:
    _DETECTORS.setdefault(profile, []).append(d)


def _detector_applies(d: SideEffectDetector, target: str) -> bool:
    """True if detector ``d`` should run for fault dimension ``target``.

    Dimension-agnostic detectors (empty ``applies_to_targets``) always run.
    Dimension-relevant detectors run only when ``target`` matches. An empty
    ``target`` (unknown) disables filtering so all detectors run — this keeps
    the pre-two-axis behaviour for callers that do not supply a target.
    """
    applies = getattr(d, "applies_to_targets", ())
    if not applies or not target:
        return True
    return target in applies


def run_all_detectors(
    before: SideEffectSnapshot | None,
    after: PostInjectState,
    ctx: DetectionContext,
    profile: str | None = None,
) -> dict[str, list[dict]]:
    """Run the detectors for ``profile`` (default ``ctx.profile``).

    Pure synchronous — no IO here. Within the profile group, dimension-relevant
    detectors are filtered by ``ctx.target`` (see :func:`_detector_applies`).
    """
    prof = profile or ctx.profile or PROFILE_K8S
    results: dict[str, list[dict]] = {}
    for d in _DETECTORS.get(prof, []):
        if not _detector_applies(d, ctx.target):
            continue
        try:
            items = d.detect(before, after, ctx)
            if items:
                results[d.key] = items
        except Exception as e:
            logger.warning("side-effect detector %s failed: %s", d.key, e)
    return results


def iter_all_detectors() -> list[SideEffectDetector]:
    """All registered detectors across every profile (stable order).

    Used by summary builders that enumerate "what was checked" regardless of
    profile. k8s detectors come first (registered first), then host.
    """
    out: list[SideEffectDetector] = []
    for group in _DETECTORS.values():
        out.extend(group)
    return out


def detectors_for(profile: str | None) -> list[SideEffectDetector]:
    """Detectors registered for ONE ``profile`` (default k8s), in registration
    order.

    The profile-scoped counterpart to :func:`iter_all_detectors`: a summary
    builder that wants only the run's profile (so a k8s run's breakdown lists
    only k8s categories and a host run's only host categories) uses this.
    Mirrors :func:`run_all_detectors`'s ``profile or PROFILE_K8S`` fallback.
    """
    return list(_DETECTORS.get(profile or PROFILE_K8S, []))


# ---------------------------------------------------------------------------
# Observer Protocol & Registry (profile axis — the "where/how to observe")
# ---------------------------------------------------------------------------
#
# The observer owns environment-dependent IO: capturing the pre-injection
# baseline and fetching post-inject state. k8s reads via kubectl; host reads
# via read-only shell diagnostics. Both additionally stash the fault
# dimension's primary metric (reusing the feasibility (profile, target) probe)
# so cpu vs mem faults record their own signal.


class SideEffectObserver(Protocol):
    profile: str  # "k8s" | "host" | any third profile

    def can_capture(self, spec) -> bool:
        """Whether this profile can capture a snapshot for ``spec``.

        Replaces the old static ``requires_namespace`` gate: the decision is
        now spec-aware, so one profile can serve several scopes with different
        locators. k8s, for instance, snapshots namespace-scoped faults by
        namespace but node-scoped faults by node name — both are capturable
        even though the latter carries no namespace. The se_snapshot /
        se_detect nodes consult this instead of hardcoding
        ``if profile == PROFILE_K8S`` or ``and not namespace``, so a
        namespace-less environment (host, or a future third profile) is never
        skipped merely for lacking a namespace.
        """
        ...

    async def capture_base_snapshot(
        self, spec, kubeconfig: str, task_id: str = ""
    ) -> SideEffectSnapshot | None: ...

    async def fetch_post_inject_state(
        self, spec, kubeconfig: str, injection_start_time: str, task_id: str = ""
    ) -> PostInjectState: ...

    def summarize(self, snapshot: SideEffectSnapshot) -> tuple[str, dict]:
        """Return ``(phrase, metrics)`` describing a captured snapshot.

        ``phrase`` is a short human string (e.g. ``"5 pods, 4 endpoints"`` /
        ``"3 processes, 2 mounts"``); ``metrics`` is the structured counterpart.
        Lets the node report progress without a ``if profile == ...`` display
        branch — each profile owns how its snapshot reads.
        """
        ...


_OBSERVERS: dict[str, SideEffectObserver] = {}


def register_observer(observer: SideEffectObserver) -> None:
    _OBSERVERS[observer.profile] = observer


def resolve_observer(profile: str) -> SideEffectObserver | None:
    """Return the observer for ``profile`` or ``None`` (fail-open)."""
    return _OBSERVERS.get(profile)


async def capture_primary_metrics(spec, kubeconfig: str) -> dict:
    """Capture the fault dimension's primary metric by reusing the feasibility
    ``(profile, target)`` probe. Returns ``{}`` when no probe applies or the
    read fails (fail-open — primary metrics are advisory context).
    """
    from chaos_agent.agent.spec.feasibility import (
        profile_for_spec,
        resolve_feasibility_probe,
    )

    target = getattr(spec, "blade_target", "")
    if not target:
        return {}
    probe = resolve_feasibility_probe(profile_for_spec(spec), target)
    if probe is None:
        return {}
    try:
        m = await probe.measure(spec, kubeconfig)
    except Exception as exc:
        logger.debug("primary-metric probe failed for target=%s: %s", target, exc)
        return {}
    if m is None:
        return {}
    return {"target": target, "current": m.current, "limit": m.limit}


# ---------------------------------------------------------------------------
# Snapshot Capture (async — called by se_snapshot node)
# ---------------------------------------------------------------------------


def _pods_get_args(namespace: str, node_name: str) -> list[str]:
    """``kubectl get`` args for the pods relevant to a fault's scope.

    Node scope (``node_name`` set): the pods running on that node across all
    namespaces — the eviction/reschedule surface of a node fault. Namespace
    scope: the pods in ``namespace``.
    """
    if node_name:
        return ["pods", "-A", "--field-selector", f"spec.nodeName={node_name}", "-o", "json"]
    return ["pods", "-n", namespace, "-o", "json"]


def _pod_key(namespace: str, name: str) -> str:
    """Globally-unique snapshot key for a pod.

    Node-scoped snapshots span namespaces (``kubectl get pods -A``), where a
    bare pod name is NOT unique — two namespaces can each hold a ``web-0`` on
    the same node. Keying by ``namespace/name`` stops one from overwriting the
    other (and the detectors from diffing against the wrong baseline). For
    namespace-scoped snapshots the namespace is constant, so this still yields
    a stable, back-compatible key.
    """
    return f"{namespace}/{name}"


async def capture_snapshot(
    namespace: str, kubeconfig: str, *, node_name: str = "", task_id: str = ""
) -> SideEffectSnapshot | None:
    """Capture pre-injection state. Returns None on failure.

    Namespace-scoped faults snapshot the pods/endpoints of ``namespace``.
    Node-scoped faults (``node_name`` set, no namespace) instead snapshot the
    pods *running on that node* across all namespaces — the blast radius of a
    node fault is "which pods on this node get evicted/rescheduled", not a
    namespace. Endpoints are namespace/service concepts and are left empty for
    node scope.
    """
    from chaos_agent.transports import TransportTarget, execute_via_transport
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.config.settings import settings

    _target = TransportTarget.from_state({})
    pods_cmd = build_kubectl_cmd("get", _pods_get_args(namespace, node_name), kubeconfig=kubeconfig)
    tasks = [execute_via_transport(pods_cmd, _target, timeout=settings.timeout_kubectl, task_id=task_id, source="se-snapshot-pods", expect_profile=PROFILE_K8S)]
    if not node_name:
        ep_cmd = build_kubectl_cmd("get", ["endpoints", "-n", namespace, "-o", "json"], kubeconfig=kubeconfig)
        tasks.append(execute_via_transport(ep_cmd, _target, timeout=settings.timeout_kubectl, task_id=task_id, source="se-snapshot-endpoints", expect_profile=PROFILE_K8S))

    try:
        results = await asyncio.gather(*tasks)
    except Exception as e:
        logger.warning("se_snapshot capture failed: %s", e)
        return None

    result_p = results[0]
    rc_p, stdout_p = result_p.exit_code, result_p.stdout

    pods: dict[str, PodSnapshot] = {}
    if rc_p == 0 and stdout_p:
        try:
            pods_data = json.loads(stdout_p)
            for item in pods_data.get("items", []):
                # Node scope spans namespaces, so key each pod off its own
                # metadata namespace rather than the (empty) spec namespace.
                ns = item.get("metadata", {}).get("namespace", namespace)
                ps = _parse_pod_snapshot(item, ns)
                if ps:
                    pods[_pod_key(ns, ps.name)] = ps
        except (json.JSONDecodeError, KeyError):
            pass

    endpoints: dict[str, EndpointSnapshot] = {}
    if not node_name and len(results) > 1:
        result_e = results[1]
        if result_e.exit_code == 0 and result_e.stdout:
            try:
                ep_data = json.loads(result_e.stdout)
                for item in ep_data.get("items", []):
                    es = _parse_endpoint_snapshot(item)
                    if es:
                        endpoints[es.service] = es
            except (json.JSONDecodeError, KeyError):
                pass

    return SideEffectSnapshot(
        captured_at=now_iso(),
        namespace=namespace,
        pods=pods,
        endpoints=endpoints,
    )


# ---------------------------------------------------------------------------
# Post-Inject State Fetch (async — called by se_detect node)
# ---------------------------------------------------------------------------


async def fetch_post_inject_state(
    namespace: str,
    kubeconfig: str,
    injection_start_time: str,
    target_names: list[str],
    *,
    node_name: str = "",
    task_id: str = "",
) -> PostInjectState:
    """Query current state for side-effect diffing.

    Symmetric with ``capture_snapshot``: node-scoped faults fetch the pods on
    the node (all namespaces) and that node's events; namespace-scoped faults
    fetch the namespace's pods / events / endpoints plus the target pod's logs.
    """
    from chaos_agent.transports import TransportTarget, execute_via_transport
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.config.settings import settings

    _target = TransportTarget.from_state({})
    pods_cmd = build_kubectl_cmd("get", _pods_get_args(namespace, node_name), kubeconfig=kubeconfig)
    if node_name:
        events_cmd = build_kubectl_cmd("get", [
            "events", "-A", "--field-selector", f"involvedObject.name={node_name}", "-o", "json",
        ], kubeconfig=kubeconfig)
    else:
        events_cmd = build_kubectl_cmd("get", ["events", "-n", namespace, "-o", "json"], kubeconfig=kubeconfig)

    tasks = [
        execute_via_transport(pods_cmd, _target, timeout=settings.timeout_kubectl, task_id=task_id, source="se-detect-pods", expect_profile=PROFILE_K8S),
        execute_via_transport(events_cmd, _target, timeout=settings.timeout_kubectl, task_id=task_id, source="se-detect-events", expect_profile=PROFILE_K8S),
    ]
    # Endpoints and target-pod logs are namespace/service concepts — only
    # meaningful for a namespace-scoped fault, skipped for node scope.
    ep_index = -1
    logs_index = -1
    if not node_name:
        ep_cmd = build_kubectl_cmd("get", ["endpoints", "-n", namespace, "-o", "json"], kubeconfig=kubeconfig)
        tasks.append(execute_via_transport(ep_cmd, _target, timeout=settings.timeout_kubectl, task_id=task_id, source="se-detect-endpoints", expect_profile=PROFILE_K8S))
        ep_index = len(tasks) - 1
        if target_names:
            logs_cmd = build_kubectl_cmd("logs", [
                target_names[0], "-n", namespace,
                f"--since-time={injection_start_time}", "--tail=200",
            ], kubeconfig=kubeconfig)
            tasks.append(execute_via_transport(logs_cmd, _target, timeout=settings.timeout_kubectl, task_id=task_id, source="se-detect-logs", expect_profile=PROFILE_K8S))
            logs_index = len(tasks) - 1

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        return PostInjectState(captured_at=now_iso())

    def _safe_json(result) -> dict:
        if isinstance(result, Exception):
            return {}
        if result.exit_code != 0 or not result.stdout:
            return {}
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return {}

    def _safe_text(result) -> str:
        if isinstance(result, Exception):
            return ""
        return result.stdout if result.exit_code == 0 else ""

    pods_json = _safe_json(results[0])
    events_json = _safe_json(results[1])
    endpoints_json = _safe_json(results[ep_index]) if ep_index >= 0 else {}
    target_logs = _safe_text(results[logs_index]) if logs_index >= 0 else ""

    return PostInjectState(
        pods_json=pods_json,
        events_json=events_json,
        endpoints_json=endpoints_json,
        target_logs=target_logs,
        captured_at=now_iso(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_pod_snapshot(pod_item: dict, namespace: str) -> PodSnapshot | None:
    metadata = pod_item.get("metadata", {})
    status = pod_item.get("status", {})
    name = metadata.get("name", "")
    if not name:
        return None

    phase = status.get("phase", "")
    reason = status.get("reason", "")
    evicted = phase == "Failed" and reason == "Evicted"

    restart_counts: dict[str, int] = {}
    oom_killed: set[str] = set()
    crash_loop: set[str] = set()

    for cs in status.get("containerStatuses", []):
        cname = cs.get("name", "")
        restart_counts[cname] = cs.get("restartCount", 0)

        last_terminated = (cs.get("lastState") or {}).get("terminated") or {}
        if last_terminated.get("reason") == "OOMKilled":
            oom_killed.add(cname)

        waiting = (cs.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") == "CrashLoopBackOff":
            crash_loop.add(cname)

    return PodSnapshot(
        name=name,
        namespace=namespace,
        phase=phase,
        restart_counts=restart_counts,
        oom_killed_containers=oom_killed,
        crash_loop_containers=crash_loop,
        evicted=evicted,
    )


def _parse_endpoint_snapshot(ep_item: dict) -> EndpointSnapshot | None:
    metadata = ep_item.get("metadata", {})
    name = metadata.get("name", "")
    if not name:
        return None

    ready_count = 0
    for subset in ep_item.get("subsets", []):
        addresses = subset.get("addresses") or []
        ready_count += len(addresses)

    return EndpointSnapshot(service=name, ready_count=ready_count)


def _parse_iso_timestamp(ts: str) -> str | None:
    """Normalize ISO timestamp for comparison. Returns None on failure."""
    if not ts:
        return None
    return ts.replace("Z", "+00:00") if "Z" in ts else ts


def _is_after_injection(timestamp: str, injection_start: str) -> bool:
    """Check if a timestamp is after injection start time."""
    if not timestamp or not injection_start:
        return False
    try:
        t_norm = _parse_iso_timestamp(timestamp)
        i_norm = _parse_iso_timestamp(injection_start)
        if not t_norm or not i_norm:
            return False
        t_dt = datetime.fromisoformat(t_norm)
        i_dt = datetime.fromisoformat(i_norm)
        return t_dt >= i_dt
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


class ContainerRestartDetector:
    key = "container_restarts"
    applies_to_targets: tuple[str, ...] = ()

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results = []
        for item in after.pods_json.get("items", []):
            meta = item.get("metadata", {})
            pod_name = meta.get("name", "")
            if not pod_name:
                continue
            pod_ns = meta.get("namespace", "") or ctx.namespace
            base = before.pods.get(_pod_key(pod_ns, pod_name)) if before else None
            for cs in item.get("status", {}).get("containerStatuses", []):
                cname = cs.get("name", "")
                current_rc = cs.get("restartCount", 0)

                baseline_rc = base.restart_counts.get(cname, 0) if base else 0

                delta = current_rc - baseline_rc
                if delta <= 0:
                    continue

                last_terminated = (cs.get("lastState") or {}).get("terminated") or {}
                reason = last_terminated.get("reason", "")
                finished_at = last_terminated.get("finishedAt", "")

                if finished_at and not _is_after_injection(finished_at, ctx.injection_start_time):
                    continue

                results.append({
                    "pod": pod_name,
                    "namespace": pod_ns,
                    "container": cname,
                    "restart_delta": delta,
                    "restart_count": current_rc,
                    "reason": reason,
                    "finished_at": finished_at,
                })
        return results


class EvictedPodDetector:
    key = "evicted_pods"
    applies_to_targets: tuple[str, ...] = ()

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results = []
        for item in after.pods_json.get("items", []):
            meta = item.get("metadata", {})
            pod_name = meta.get("name", "")
            status = item.get("status", {})
            phase = status.get("phase", "")
            reason = status.get("reason", "")

            if phase != "Failed" or reason != "Evicted":
                continue

            pod_ns = meta.get("namespace", "") or ctx.namespace
            base = before.pods.get(_pod_key(pod_ns, pod_name)) if before else None
            if base and base.evicted:
                continue

            message = status.get("message", "")
            results.append({
                "pod": pod_name,
                "namespace": pod_ns,
                "reason": "Evicted",
                "message": message,
            })
        return results


class OOMKilledSiblingDetector:
    key = "oom_killed_pods"
    applies_to_targets: tuple[str, ...] = ("mem",)

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results = []
        for item in after.pods_json.get("items", []):
            meta = item.get("metadata", {})
            pod_name = meta.get("name", "")
            if not pod_name:
                continue
            if pod_name in ctx.target_names:
                continue
            pod_ns = meta.get("namespace", "") or ctx.namespace
            base = before.pods.get(_pod_key(pod_ns, pod_name)) if before else None

            for cs in item.get("status", {}).get("containerStatuses", []):
                cname = cs.get("name", "")
                last_terminated = (cs.get("lastState") or {}).get("terminated") or {}
                if last_terminated.get("reason") != "OOMKilled":
                    continue

                if base and cname in base.oom_killed_containers:
                    finished_at = last_terminated.get("finishedAt", "")
                    if not _is_after_injection(finished_at, ctx.injection_start_time):
                        continue

                finished_at = last_terminated.get("finishedAt", "")
                if finished_at and not _is_after_injection(finished_at, ctx.injection_start_time):
                    continue

                results.append({
                    "pod": pod_name,
                    "namespace": pod_ns,
                    "container": cname,
                    "finished_at": finished_at,
                })
        return results


class CrashLoopDetector:
    key = "crash_loop_pods"
    applies_to_targets: tuple[str, ...] = ()

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results = []
        for item in after.pods_json.get("items", []):
            meta = item.get("metadata", {})
            pod_name = meta.get("name", "")
            if not pod_name:
                continue
            pod_ns = meta.get("namespace", "") or ctx.namespace
            base = before.pods.get(_pod_key(pod_ns, pod_name)) if before else None

            for cs in item.get("status", {}).get("containerStatuses", []):
                cname = cs.get("name", "")
                waiting = (cs.get("state") or {}).get("waiting") or {}
                if waiting.get("reason") != "CrashLoopBackOff":
                    continue

                if base and cname in base.crash_loop_containers:
                    continue

                current_rc = cs.get("restartCount", 0)
                baseline_rc = base.restart_counts.get(cname, 0) if base else 0
                delta = current_rc - baseline_rc

                results.append({
                    "pod": pod_name,
                    "namespace": pod_ns,
                    "container": cname,
                    "restart_delta": delta,
                })
        return results


class EndpointRemovalDetector:
    key = "endpoint_removals"
    applies_to_targets: tuple[str, ...] = ()

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        if not before:
            return []

        results = []
        current_endpoints: dict[str, int] = {}
        for item in after.endpoints_json.get("items", []):
            svc_name = item.get("metadata", {}).get("name", "")
            if not svc_name:
                continue
            ready = 0
            for subset in item.get("subsets", []):
                ready += len(subset.get("addresses") or [])
            current_endpoints[svc_name] = ready

        for svc_name, snap in before.endpoints.items():
            if snap.ready_count == 0:
                continue
            current = current_endpoints.get(svc_name, 0)
            if current < snap.ready_count:
                results.append({
                    "service": svc_name,
                    "before": snap.ready_count,
                    "after": current,
                })
        return results


class HPAScaleDetector:
    key = "hpa_scaling"
    applies_to_targets: tuple[str, ...] = ("cpu", "mem")

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results = []
        for item in after.events_json.get("items", []):
            if item.get("reason") != "SuccessfulRescale":
                continue
            last_ts = item.get("lastTimestamp") or item.get("eventTime") or ""
            if not _is_after_injection(last_ts, ctx.injection_start_time):
                continue

            involved = item.get("involvedObject", {})
            name = involved.get("name", "")
            message = item.get("message", "")

            old_replicas, new_replicas = _parse_rescale_message(message)
            results.append({
                "hpa": name,
                "old_replicas": old_replicas,
                "new_replicas": new_replicas,
                "message": message,
            })
        return results


class ProbeFailureDetector:
    key = "probe_failures"
    applies_to_targets: tuple[str, ...] = ()

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results: dict[str, dict] = {}
        for item in after.events_json.get("items", []):
            if item.get("reason") != "Unhealthy":
                continue
            last_ts = item.get("lastTimestamp") or item.get("eventTime") or ""
            if not _is_after_injection(last_ts, ctx.injection_start_time):
                continue

            involved = item.get("involvedObject", {})
            pod_name = involved.get("name", "")
            if pod_name in ctx.target_names:
                continue

            message = item.get("message", "")
            msg_lower = message.lower()
            if "liveness" in msg_lower:
                probe_type = "Liveness"
            elif "startup" in msg_lower:
                probe_type = "Startup"
            else:
                probe_type = "Readiness"
            key = f"{pod_name}:{probe_type}"
            if key in results:
                results[key]["count"] += 1
            else:
                results[key] = {
                    "pod": pod_name,
                    "probe_type": probe_type,
                    "count": 1,
                }
        return list(results.values())


_DEPENDENCY_PATTERNS = (
    "connection refused", "connection timed out",
    "upstream connect error", "no healthy upstream",
    "ETIMEDOUT", "ECONNREFUSED", "ECONNRESET",
    "502", "503", "504",
)


def _match_dependency_pattern(line: str, pattern: str) -> bool:
    """Match pattern with word-boundary awareness for short numeric patterns."""
    if pattern.isdigit() and len(pattern) <= 3:
        return bool(re.search(rf"\b{pattern}\b", line))
    return pattern in line


class DependencyErrorDetector:
    key = "dependency_errors"
    applies_to_targets: tuple[str, ...] = ()

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        if not after.target_logs:
            return []

        results = []
        lines = after.target_logs.splitlines()
        for pattern in _DEPENDENCY_PATTERNS:
            matching = [ln for ln in lines if _match_dependency_pattern(ln, pattern)]
            if matching:
                results.append({
                    "pattern": pattern,
                    "count": len(matching),
                    "sample_line": matching[0][:200],
                })
        return results


_RESCALE_RE = re.compile(r"from (\d+) to (\d+)")


def _parse_rescale_message(message: str) -> tuple[int, int]:
    """Extract old/new replica counts from HPA rescale event message."""
    m = _RESCALE_RE.search(message)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


# ---------------------------------------------------------------------------
# Observers (profile axis)
# ---------------------------------------------------------------------------


class K8sObserver:
    """k8s profile observer — wraps the existing kubectl snapshot/fetch and
    stashes the fault dimension's primary metric. Behaviour-preserving: the
    pods/events/endpoints payloads are byte-identical to the pre-seam path.
    """
    profile = PROFILE_K8S

    @staticmethod
    def _node_name(spec) -> str:
        """Node name for a node-scoped fault (its first target), else ""."""
        if getattr(spec, "scope", "") == "node":
            names = getattr(spec, "names", ()) or ()
            return names[0] if names else ""
        return ""

    def can_capture(self, spec) -> bool:
        # k8s can snapshot whenever it has a locator: a namespace
        # (namespace-scoped faults) OR a node name (node-scoped faults observe
        # the pods on the node). Only a spec with neither is un-snapshottable.
        if getattr(spec, "namespace", ""):
            return True
        return bool(self._node_name(spec))

    async def capture_base_snapshot(self, spec, kubeconfig: str, task_id: str = "") -> SideEffectSnapshot | None:
        namespace = getattr(spec, "namespace", "") or ""
        snap = await capture_snapshot(namespace, kubeconfig, node_name=self._node_name(spec), task_id=task_id)
        if snap is not None:
            snap.primary_metrics = await capture_primary_metrics(spec, kubeconfig)
        return snap

    async def fetch_post_inject_state(
        self, spec, kubeconfig: str, injection_start_time: str, task_id: str = ""
    ) -> PostInjectState:
        namespace = getattr(spec, "namespace", "") or ""
        target_names = list(getattr(spec, "names", []) or [])
        state = await fetch_post_inject_state(
            namespace, kubeconfig, injection_start_time, target_names,
            node_name=self._node_name(spec), task_id=task_id,
        )
        state.primary_metrics = await capture_primary_metrics(spec, kubeconfig)
        return state

    def summarize(self, snapshot: SideEffectSnapshot) -> tuple[str, dict]:
        pod_count = len(snapshot.pods)
        ep_count = len(snapshot.endpoints)
        return (
            f"{pod_count} pods, {ep_count} endpoints",
            {"pods": pod_count, "endpoints": ep_count},
        )


# --- host diagnostic IO (read-only shell via transport, skip_guard) ---------


async def _run_host_diag(command: list[str], timeout: int = 10) -> str | None:
    """Run one read-only host diagnostic. Returns stdout or None on failure."""
    from chaos_agent.transports import TransportTarget, execute_via_transport

    if not command:
        return None
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            command, target, timeout=timeout,
            source="se-host-observe", skip_guard=True,
            # A bare host diagnostic: on a cluster-addressing channel the
            # platform executor answers, so the "side effect" observed would
            # belong to another machine (task-46317228).
            expect_profile=PROFILE_HOST,
        )
        if result.exit_code != 0 or not result.stdout:
            return None
        return result.stdout
    except Exception as exc:
        logger.debug("se host diag failed for %s: %s", command, exc)
        return None


# Kernel threads (children of kthreadd, scheduled by the kernel) churn
# constantly and independently of any injected fault. Their ``comm`` names are
# volatile (a worker pool renames ``kworker/u16:3`` → ``kworker/u16:5`` between
# two ``ps`` snapshots), so a plain before/after set diff reports them as dozens
# of "process deaths" — pure noise that also poisons the postmortem with invented
# causal stories (e.g. "CPU starvation reaped kworker threads"). They are never a
# meaningful process-death side effect, so we drop them before the snapshot is
# ever captured. We match a curated set of well-known names rather than a ps flag
# (``-o flags`` / ``--ppid 2``), which is not portable across the minimal images /
# BusyBox environments host injection may target.
#
# Caveat: we deliberately match the kernel softlockup watchdog via the
# ``watchdog/`` PREFIX (``watchdog/0``, ``watchdog/1``) and do NOT list the bare
# ``watchdogd`` name — that is a real USER-SPACE daemon whose death is a genuine
# side effect we must still surface.
_KERNEL_THREAD_PREFIXES: tuple[str, ...] = (
    "kworker/", "ksoftirqd/", "migration/", "rcu_", "watchdog/",
    "cpuhp/", "irq/", "idle_inject/", "kswapd", "kcompactd",
)
_KERNEL_THREAD_EXACT: frozenset[str] = frozenset({
    "kthreadd", "khugepaged", "kauditd", "kblockd", "kintegrityd",
    "khungtaskd", "oom_reaper", "writeback", "kthrotld", "kpsmoused",
    "netns", "ksmd", "kdevtmpfs", "kstrp", "scsi_eh",
    "acpi_thermal_pm", "ksoftirqd", "migration",
})


def _is_kernel_thread(comm: str) -> bool:
    """Heuristic: is ``comm`` a kernel thread name?

    Kernel threads have kernel-managed, volatile names and are never a
    meaningful process-death side effect. Matched by a curated prefix/exact
    set for portability (no dependency on ps flags).
    """
    if comm in _KERNEL_THREAD_EXACT:
        return True
    return any(comm.startswith(p) for p in _KERNEL_THREAD_PREFIXES)


def _parse_process_comms(out: str | None) -> set[str]:
    """Parse ``ps -e -o comm=`` into a set of USER-SPACE process names.

    Kernel threads are filtered out (see :func:`_is_kernel_thread`): they churn
    on their own and would otherwise flood ProcessDeathDetector with false
    positives.
    """
    if not out:
        return set()
    return {
        comm
        for ln in out.splitlines()
        if (comm := ln.strip()) and not _is_kernel_thread(comm)
    }


def _parse_df_mounts(out: str | None) -> dict[str, int]:
    """Parse ``df -P`` into ``{mount_point: use_percent}``."""
    mounts: dict[str, int] = {}
    if not out:
        return mounts
    lines = out.splitlines()
    for line in lines[1:]:  # skip header
        cols = line.split()
        if len(cols) < 6:
            continue
        use_str = cols[4].rstrip("%")
        mount = cols[5]
        try:
            mounts[mount] = int(use_str)
        except ValueError:
            continue
    return mounts


def _parse_systemctl_services(out: str | None) -> dict[str, str]:
    """Parse ``systemctl list-units --type=service`` into ``{unit: active_state}``."""
    services: dict[str, str] = {}
    if not out:
        return services
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 3:
            continue
        unit = cols[0]
        if not unit.endswith(".service"):
            continue
        services[unit] = cols[2]  # ACTIVE column (active/inactive/failed)
    return services


class HostObserver:
    """host profile observer — broad-spectrum host state via read-only shell
    diagnostics (ps / df / systemctl / dmesg). No namespace required.
    """
    profile = PROFILE_HOST

    def can_capture(self, spec) -> bool:
        # Host diagnostics need no namespace and no node locator — the target
        # is always "this host", so capture is always possible.
        return True

    async def _capture_host(self) -> HostSnapshot:
        ps_out, df_out, svc_out, dmesg_out = await asyncio.gather(
            _run_host_diag(["ps", "-e", "-o", "comm="]),
            _run_host_diag(["df", "-P"]),
            _run_host_diag(["systemctl", "list-units", "--type=service", "--no-legend", "--plain"]),
            _run_host_diag(["dmesg"], timeout=8),
        )
        dmesg_count = len(dmesg_out.splitlines()) if dmesg_out else 0
        return HostSnapshot(
            processes=_parse_process_comms(ps_out),
            mounts=_parse_df_mounts(df_out),
            services=_parse_systemctl_services(svc_out),
            dmesg_line_count=dmesg_count,
        )

    async def capture_base_snapshot(self, spec, kubeconfig: str, task_id: str = "") -> SideEffectSnapshot | None:
        host = await self._capture_host()
        snap = SideEffectSnapshot(captured_at=now_iso(), namespace="", host=host)
        snap.primary_metrics = await capture_primary_metrics(spec, kubeconfig)
        return snap

    async def fetch_post_inject_state(
        self, spec, kubeconfig: str, injection_start_time: str, task_id: str = ""
    ) -> PostInjectState:
        # Return the full current host picture; DmesgOOMDetector slices the
        # dmesg tail against the baseline cursor (before.host.dmesg_line_count)
        # so only kernel lines produced after injection are considered.
        ps_out, df_out, svc_out, dmesg_out = await asyncio.gather(
            _run_host_diag(["ps", "-e", "-o", "comm="]),
            _run_host_diag(["df", "-P"]),
            _run_host_diag(["systemctl", "list-units", "--type=service", "--no-legend", "--plain"]),
            _run_host_diag(["dmesg"], timeout=8),
        )
        dmesg_lines = dmesg_out.splitlines() if dmesg_out else []
        host = HostPostInjectState(
            processes=_parse_process_comms(ps_out),
            mounts=_parse_df_mounts(df_out),
            services=_parse_systemctl_services(svc_out),
            dmesg_lines=dmesg_lines,
        )
        state = PostInjectState(captured_at=now_iso(), host=host)
        state.primary_metrics = await capture_primary_metrics(spec, kubeconfig)
        return state

    def summarize(self, snapshot: SideEffectSnapshot) -> tuple[str, dict]:
        host = snapshot.host
        proc_count = len(host.processes) if host else 0
        mount_count = len(host.mounts) if host else 0
        return (
            f"{proc_count} processes, {mount_count} mounts",
            {"processes": proc_count, "mounts": mount_count},
        )


# ---------------------------------------------------------------------------
# Host Detectors
# ---------------------------------------------------------------------------


_FS_FULL_THRESHOLD = 95  # use% at/above which a mount is considered full


class ProcessDeathDetector:
    """A non-target process that was alive at baseline is gone post-inject.

    Excludes ``ctx.target_names`` (the target dying is the intended effect, not
    collateral). Dimension-agnostic within host — an unexpectedly dead sibling
    process is worth surfacing regardless of the injected fault.
    """
    key = "process_deaths"
    applies_to_targets: tuple[str, ...] = ()

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        if not before or not before.host or not after.host:
            return []
        targets = set(ctx.target_names or [])
        gone = before.host.processes - after.host.processes - targets
        return [{"process": name} for name in sorted(gone)]


class FilesystemFullDetector:
    """A mount crossed the full threshold after injection (disk faults)."""
    key = "filesystem_full"
    applies_to_targets: tuple[str, ...] = ("disk",)

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        if not after.host:
            return []
        base_mounts = before.host.mounts if (before and before.host) else {}
        results = []
        for mount, use_pct in after.host.mounts.items():
            if use_pct < _FS_FULL_THRESHOLD:
                continue
            if base_mounts.get(mount, 0) >= _FS_FULL_THRESHOLD:
                continue  # already full at baseline — not incremental
            results.append({
                "mount": mount,
                "use_percent": use_pct,
                "baseline_percent": base_mounts.get(mount, 0),
            })
        return results


_OOM_PATTERNS = ("out of memory", "oom-killer", "killed process")


class DmesgOOMDetector:
    """Kernel OOM-killer activity in post-inject dmesg lines (mem faults)."""
    key = "dmesg_oom"
    applies_to_targets: tuple[str, ...] = ("mem",)

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        if not after.host:
            return []
        base_count = before.host.dmesg_line_count if (before and before.host) else 0
        new_lines = after.host.dmesg_lines[base_count:] if base_count else after.host.dmesg_lines
        results = []
        for line in new_lines:
            low = line.lower()
            if any(pat in low for pat in _OOM_PATTERNS):
                results.append({"line": line.strip()[:200]})
        return results


class ServiceDownDetector:
    """A systemd service that was active at baseline is no longer active.

    Tier-2 read-only (systemctl list-units). Dimension-agnostic within host.
    """
    key = "service_down"
    applies_to_targets: tuple[str, ...] = ()

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        if not before or not before.host or not after.host:
            return []
        results = []
        for unit, state in before.host.services.items():
            if state != "active":
                continue
            after_state = after.host.services.get(unit, "gone")
            if after_state != "active":
                results.append({
                    "service": unit,
                    "before": state,
                    "after": after_state,
                })
        return results


# ---------------------------------------------------------------------------
# Register all built-in detectors
# ---------------------------------------------------------------------------

register(ContainerRestartDetector())
register(EvictedPodDetector())
register(OOMKilledSiblingDetector())
register(CrashLoopDetector())
register(EndpointRemovalDetector())
register(HPAScaleDetector())
register(ProbeFailureDetector())
register(DependencyErrorDetector())

register(ProcessDeathDetector(), profile=PROFILE_HOST)
register(FilesystemFullDetector(), profile=PROFILE_HOST)
register(DmesgOOMDetector(), profile=PROFILE_HOST)
register(ServiceDownDetector(), profile=PROFILE_HOST)

register_observer(K8sObserver())
register_observer(HostObserver())
