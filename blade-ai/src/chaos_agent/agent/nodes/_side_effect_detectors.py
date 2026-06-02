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
class SideEffectSnapshot:
    captured_at: str
    namespace: str
    pods: dict[str, PodSnapshot] = field(default_factory=dict)
    endpoints: dict[str, EndpointSnapshot] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "captured_at": self.captured_at,
            "namespace": self.namespace,
            "pods": {k: v.to_dict() for k, v in self.pods.items()},
            "endpoints": {k: v.to_dict() for k, v in self.endpoints.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> SideEffectSnapshot:
        pods = {k: PodSnapshot.from_dict(v) for k, v in d.get("pods", {}).items()}
        endpoints = {k: EndpointSnapshot.from_dict(v) for k, v in d.get("endpoints", {}).items()}
        return cls(
            captured_at=d.get("captured_at", ""),
            namespace=d.get("namespace", ""),
            pods=pods,
            endpoints=endpoints,
        )


@dataclass
class PostInjectState:
    """Current namespace state queried after verification."""
    pods_json: dict = field(default_factory=dict)
    events_json: dict = field(default_factory=dict)
    endpoints_json: dict = field(default_factory=dict)
    target_logs: str = ""
    captured_at: str = ""


@dataclass
class DetectionContext:
    namespace: str
    target_names: list[str]
    scope: str
    kubeconfig: str
    injection_start_time: str
    task_id: str


# ---------------------------------------------------------------------------
# Detector Protocol & Registry
# ---------------------------------------------------------------------------


class SideEffectDetector(Protocol):
    key: str

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        ...


_DETECTORS: list[SideEffectDetector] = []


def register(d: SideEffectDetector) -> None:
    _DETECTORS.append(d)


def run_all_detectors(
    before: SideEffectSnapshot | None,
    after: PostInjectState,
    ctx: DetectionContext,
) -> dict[str, list[dict]]:
    """Run all registered detectors. Pure synchronous — no IO here."""
    results: dict[str, list[dict]] = {}
    for d in _DETECTORS:
        try:
            items = d.detect(before, after, ctx)
            if items:
                results[d.key] = items
        except Exception as e:
            logger.warning("side-effect detector %s failed: %s", d.key, e)
    return results


# ---------------------------------------------------------------------------
# Snapshot Capture (async — called by se_snapshot node)
# ---------------------------------------------------------------------------


async def capture_snapshot(namespace: str, kubeconfig: str) -> SideEffectSnapshot | None:
    """Capture pre-injection namespace state. Returns None on failure."""
    from chaos_agent.tools.shell import run_command
    from chaos_agent.tools.kubectl import _build_kubectl_global_args
    from chaos_agent.config.settings import settings

    kubectl_path = settings.kubectl_path
    global_args = " ".join(_build_kubectl_global_args(kubeconfig))

    pods_cmd = [kubectl_path, *global_args.split(), "get", "pods", "-n", namespace, "-o", "json"]
    ep_cmd = [kubectl_path, *global_args.split(), "get", "endpoints", "-n", namespace, "-o", "json"]

    try:
        result_p, result_e = await asyncio.gather(
            run_command(pods_cmd, timeout=settings.timeout_kubectl, source="se-snapshot-pods"),
            run_command(ep_cmd, timeout=settings.timeout_kubectl, source="se-snapshot-endpoints"),
        )
    except Exception as e:
        logger.warning("se_snapshot capture failed: %s", e)
        return None

    rc_p, stdout_p = result_p.exit_code, result_p.stdout
    rc_e, stdout_e = result_e.exit_code, result_e.stdout

    pods: dict[str, PodSnapshot] = {}
    if rc_p == 0 and stdout_p:
        try:
            pods_data = json.loads(stdout_p)
            for item in pods_data.get("items", []):
                ps = _parse_pod_snapshot(item, namespace)
                if ps:
                    pods[ps.name] = ps
        except (json.JSONDecodeError, KeyError):
            pass

    endpoints: dict[str, EndpointSnapshot] = {}
    if rc_e == 0 and stdout_e:
        try:
            ep_data = json.loads(stdout_e)
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
) -> PostInjectState:
    """Query current namespace state for side-effect diffing."""
    from chaos_agent.tools.shell import run_command
    from chaos_agent.tools.kubectl import _build_kubectl_global_args
    from chaos_agent.config.settings import settings

    kubectl_path = settings.kubectl_path
    global_args = " ".join(_build_kubectl_global_args(kubeconfig))

    _ga = global_args.split()
    pods_cmd = [kubectl_path, *_ga, "get", "pods", "-n", namespace, "-o", "json"]
    events_cmd = [kubectl_path, *_ga, "get", "events", "-n", namespace, "-o", "json"]
    ep_cmd = [kubectl_path, *_ga, "get", "endpoints", "-n", namespace, "-o", "json"]

    logs_cmd: list[str] = []
    if target_names:
        logs_cmd = [
            kubectl_path, *_ga, "logs", target_names[0],
            "-n", namespace, f"--since-time={injection_start_time}", "--tail=200",
        ]

    tasks = [
        run_command(pods_cmd, timeout=settings.timeout_kubectl, source="se-detect-pods"),
        run_command(events_cmd, timeout=settings.timeout_kubectl, source="se-detect-events"),
        run_command(ep_cmd, timeout=settings.timeout_kubectl, source="se-detect-endpoints"),
    ]
    if logs_cmd:
        tasks.append(run_command(logs_cmd, timeout=settings.timeout_kubectl, source="se-detect-logs"))

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
    endpoints_json = _safe_json(results[2])
    target_logs = _safe_text(results[3]) if len(results) > 3 else ""

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

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results = []
        for item in after.pods_json.get("items", []):
            pod_name = item.get("metadata", {}).get("name", "")
            if not pod_name:
                continue
            for cs in item.get("status", {}).get("containerStatuses", []):
                cname = cs.get("name", "")
                current_rc = cs.get("restartCount", 0)

                baseline_rc = 0
                if before and pod_name in before.pods:
                    baseline_rc = before.pods[pod_name].restart_counts.get(cname, 0)

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
                    "container": cname,
                    "restart_delta": delta,
                    "restart_count": current_rc,
                    "reason": reason,
                    "finished_at": finished_at,
                })
        return results


class EvictedPodDetector:
    key = "evicted_pods"

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results = []
        for item in after.pods_json.get("items", []):
            pod_name = item.get("metadata", {}).get("name", "")
            status = item.get("status", {})
            phase = status.get("phase", "")
            reason = status.get("reason", "")

            if phase != "Failed" or reason != "Evicted":
                continue

            if before and pod_name in before.pods and before.pods[pod_name].evicted:
                continue

            message = status.get("message", "")
            results.append({
                "pod": pod_name,
                "reason": "Evicted",
                "message": message,
            })
        return results


class OOMKilledSiblingDetector:
    key = "oom_killed_pods"

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results = []
        for item in after.pods_json.get("items", []):
            pod_name = item.get("metadata", {}).get("name", "")
            if not pod_name:
                continue
            if pod_name in ctx.target_names:
                continue

            for cs in item.get("status", {}).get("containerStatuses", []):
                cname = cs.get("name", "")
                last_terminated = (cs.get("lastState") or {}).get("terminated") or {}
                if last_terminated.get("reason") != "OOMKilled":
                    continue

                if before and pod_name in before.pods:
                    if cname in before.pods[pod_name].oom_killed_containers:
                        finished_at = last_terminated.get("finishedAt", "")
                        if not _is_after_injection(finished_at, ctx.injection_start_time):
                            continue

                finished_at = last_terminated.get("finishedAt", "")
                if finished_at and not _is_after_injection(finished_at, ctx.injection_start_time):
                    continue

                results.append({
                    "pod": pod_name,
                    "container": cname,
                    "finished_at": finished_at,
                })
        return results


class CrashLoopDetector:
    key = "crash_loop_pods"

    def detect(
        self,
        before: SideEffectSnapshot | None,
        after: PostInjectState,
        ctx: DetectionContext,
    ) -> list[dict]:
        results = []
        for item in after.pods_json.get("items", []):
            pod_name = item.get("metadata", {}).get("name", "")
            if not pod_name:
                continue

            for cs in item.get("status", {}).get("containerStatuses", []):
                cname = cs.get("name", "")
                waiting = (cs.get("state") or {}).get("waiting") or {}
                if waiting.get("reason") != "CrashLoopBackOff":
                    continue

                if before and pod_name in before.pods:
                    if cname in before.pods[pod_name].crash_loop_containers:
                        continue

                current_rc = cs.get("restartCount", 0)
                baseline_rc = 0
                if before and pod_name in before.pods:
                    baseline_rc = before.pods[pod_name].restart_counts.get(cname, 0)
                delta = current_rc - baseline_rc

                results.append({
                    "pod": pod_name,
                    "container": cname,
                    "restart_delta": delta,
                })
        return results


class EndpointRemovalDetector:
    key = "endpoint_removals"

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
            matching = [l for l in lines if _match_dependency_pattern(l, pattern)]
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
