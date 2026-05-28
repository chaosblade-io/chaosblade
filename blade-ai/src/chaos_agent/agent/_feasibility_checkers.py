"""Built-in feasibility checkers: Memory + CPU.

Each checker probes current resource usage via kubectl and compares
against the injection target parameters to determine headroom.

I/O helpers are module-private; they mirror the kubectl patterns in
direct_execute.py but are decoupled from task_id/tracker dependencies
so they can be called from the safety_check context.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from chaos_agent.agent.feasibility import (
    FeasibilityReport,
    FeasibilitySeverity,
    register_feasibility_checker,
)

if TYPE_CHECKING:
    from chaos_agent.agent.fault_spec import FaultSpec

logger = logging.getLogger(__name__)

_HEADROOM_IMPOSSIBLE = 0.05
_HEADROOM_TIGHT = 0.20


# ---------------------------------------------------------------------------
# Metrics-server availability probe (TTL-cached)
# ---------------------------------------------------------------------------

import time as _time

_metrics_probe_cache: tuple[bool, float] | None = None
_METRICS_PROBE_TTL = 300  # 5 minutes


async def is_metrics_server_available(kubeconfig: str) -> bool:
    """Check if metrics-server is reachable via kubectl top node.

    Result is cached with a 5-minute TTL to avoid repeated probes
    within a single session while still detecting recovery.
    """
    global _metrics_probe_cache
    now = _time.monotonic()
    if _metrics_probe_cache and (now - _metrics_probe_cache[1]) < _METRICS_PROBE_TTL:
        return _metrics_probe_cache[0]
    result = await _run_kubectl(["top", "node", "--no-headers"], kubeconfig, timeout=5)
    available = result is not None and len(result.strip()) > 0
    _metrics_probe_cache = (available, now)
    return available


# ---------------------------------------------------------------------------
# Shared kubectl I/O helpers
# ---------------------------------------------------------------------------


async def _run_kubectl(args: list[str], kubeconfig: str, timeout: int = 8) -> str | None:
    """Run a kubectl command. Returns stdout on success, None on any error."""
    from chaos_agent.config.settings import settings
    from chaos_agent.tools.kubectl import _build_kubectl_global_args
    from chaos_agent.tools.shell import run_command

    kubectl_path = settings.kubectl_path
    global_args = _build_kubectl_global_args(kubeconfig)
    cmd = [kubectl_path, *global_args, *args]

    try:
        result = await run_command(
            cmd,
            timeout=timeout,
            task_id="",
            skip_guard=True,
            source="feasibility-check",
        )
        if result.exit_code != 0 or not result.stdout:
            return None
        return result.stdout.strip()
    except Exception:
        return None


async def _resolve_first_pod(
    spec: "FaultSpec", kubeconfig: str
) -> str | None:
    """Resolve a real pod name from FaultSpec for feasibility checks.

    When labels are set, queries kubectl to find a Running pod matching
    the selector.  Falls back to spec.names[0] (assumed exact pod name).
    """
    if spec.labels:
        label_selector = ",".join(f"{k}={v}" for k, v in spec.labels.items())
        args = [
            "get", "pod",
            "-l", label_selector,
            "-n", spec.namespace or "default",
            "--field-selector=status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
        ]
        stdout = await _run_kubectl(args, kubeconfig, timeout=5)
        pod_name = (stdout or "").strip().strip("'\"")
        if pod_name:
            return pod_name
    if spec.names:
        return spec.names[0]
    return None


async def _fetch_memory_usage_mb(
    pod_name: str, namespace: str, kubeconfig: str
) -> int | None:
    """kubectl top pod → memory usage in MB."""
    stdout = await _run_kubectl(
        ["top", "pod", pod_name, "-n", namespace, "--no-headers"],
        kubeconfig,
    )
    if not stdout:
        return None
    parts = stdout.split()
    for p in parts[1:]:  # skip column 0 (name)
        upper = p.upper()
        if upper.endswith("MI") or upper.endswith("MIB"):
            try:
                return int(upper.rstrip("MIB").rstrip("MI"))
            except ValueError:
                pass
        elif upper.endswith("GI") or upper.endswith("GIB"):
            try:
                return int(float(upper.rstrip("GIB").rstrip("GI")) * 1024)
            except ValueError:
                pass
    return None


async def _fetch_memory_limit_mb(
    pod_name: str, namespace: str, kubeconfig: str
) -> int | None:
    """kubectl get pod → resources.limits.memory in MB."""
    from chaos_agent.utils.fault_type import parse_k8s_memory_to_mb

    stdout = await _run_kubectl(
        [
            "get", "pod", pod_name, "-n", namespace,
            "-o", "jsonpath={.spec.containers[0].resources.limits.memory}",
        ],
        kubeconfig,
    )
    if not stdout:
        return None
    raw = stdout.strip().strip("'\"")
    if not raw:
        return None
    return parse_k8s_memory_to_mb(raw)


async def _fetch_cpu_usage_millicores(
    name: str, namespace: str, kubeconfig: str, *, is_node: bool = False
) -> int | None:
    """kubectl top pod/node → CPU usage in millicores."""
    if is_node:
        args = ["top", "node", name, "--no-headers"]
    else:
        args = ["top", "pod", name, "-n", namespace, "--no-headers"]
    stdout = await _run_kubectl(args, kubeconfig)
    if not stdout:
        return None
    parts = stdout.split()
    for p in parts[1:]:  # skip column 0 (name)
        if p.endswith("m"):
            try:
                return int(p[:-1])
            except ValueError:
                pass
        # Whole-core values like "2" (= 2000m)
        try:
            val = float(p)
            if 0 < val < 200:
                return int(val * 1000)
        except ValueError:
            continue
    return None


async def _fetch_cpu_limit_millicores(
    pod_name: str, namespace: str, kubeconfig: str
) -> int | None:
    """kubectl get pod → resources.limits.cpu in millicores."""
    stdout = await _run_kubectl(
        [
            "get", "pod", pod_name, "-n", namespace,
            "-o", "jsonpath={.spec.containers[0].resources.limits.cpu}",
        ],
        kubeconfig,
    )
    if not stdout:
        return None
    raw = stdout.strip().strip("'\"")
    if not raw:
        return None
    if raw.endswith("m"):
        try:
            return int(raw[:-1])
        except ValueError:
            return None
    try:
        return int(float(raw) * 1000)
    except ValueError:
        return None


async def _fetch_node_cpu_capacity_millicores(
    node_name: str, kubeconfig: str
) -> int | None:
    """kubectl get node → status.capacity.cpu in millicores."""
    stdout = await _run_kubectl(
        [
            "get", "node", node_name,
            "-o", "jsonpath={.status.capacity.cpu}",
        ],
        kubeconfig,
    )
    if not stdout:
        return None
    raw = stdout.strip().strip("'\"")
    if not raw:
        return None
    if raw.endswith("m"):
        try:
            return int(raw[:-1])
        except ValueError:
            return None
    try:
        return int(float(raw) * 1000)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Memory checker
# ---------------------------------------------------------------------------


class MemoryFeasibilityChecker:
    blade_target = "mem"

    async def assess(
        self, spec: "FaultSpec", kubeconfig: str
    ) -> FeasibilityReport | None:
        if not spec.namespace:
            return None
        target_percent = _parse_int_param(spec.params.get("mem-percent"))
        if target_percent is None or target_percent <= 0:
            return None

        pod_name = await _resolve_first_pod(spec, kubeconfig)
        if not pod_name:
            return None
        usage_mb = await _fetch_memory_usage_mb(pod_name, spec.namespace, kubeconfig)
        limit_mb = await _fetch_memory_limit_mb(pod_name, spec.namespace, kubeconfig)
        if usage_mb is None or limit_mb is None or limit_mb == 0:
            return None

        target_mb = limit_mb * target_percent / 100
        headroom = (target_mb - usage_mb) / limit_mb
        current_percent = round(usage_mb / limit_mb * 100, 1)

        current_str = f"{usage_mb}Mi ({current_percent}%)"
        limit_str = f"{limit_mb}Mi"
        target_str = f"{int(target_mb)}Mi ({target_percent}%)"
        delta_mb = max(0, int(target_mb - usage_mb))

        if headroom <= _HEADROOM_IMPOSSIBLE:
            return FeasibilityReport(
                severity=FeasibilitySeverity.IMPOSSIBLE,
                headroom=max(0.0, headroom),
                current_value=current_str,
                limit_value=limit_str,
                target_value=target_str,
                message=(
                    f"Memory at {current_percent}% ({usage_mb}Mi/{limit_mb}Mi), "
                    f"target {target_percent}% — only {delta_mb}Mi headroom"
                ),
                recommendation="Pick a Pod with lower memory usage",
            )
        elif headroom <= _HEADROOM_TIGHT:
            return FeasibilityReport(
                severity=FeasibilitySeverity.TIGHT,
                headroom=headroom,
                current_value=current_str,
                limit_value=limit_str,
                target_value=target_str,
                message=(
                    f"Memory at {current_percent}% ({usage_mb}Mi/{limit_mb}Mi), "
                    f"target {target_percent}% — {delta_mb}Mi headroom (tight)"
                ),
                recommendation="Injection may succeed but effect could be marginal",
            )
        else:
            return FeasibilityReport(
                severity=FeasibilitySeverity.OK,
                headroom=headroom,
                current_value=current_str,
                limit_value=limit_str,
                target_value=target_str,
                message=f"Sufficient headroom ({headroom:.0%})",
                recommendation="",
            )


# ---------------------------------------------------------------------------
# CPU checker
# ---------------------------------------------------------------------------


class CpuFeasibilityChecker:
    blade_target = "cpu"

    async def assess(
        self, spec: "FaultSpec", kubeconfig: str
    ) -> FeasibilityReport | None:
        target_percent = _parse_int_param(spec.params.get("cpu-percent"))
        if target_percent is None or target_percent <= 0:
            if spec.blade_action in ("fullload", "burn"):
                target_percent = 100
            else:
                return None

        is_node = spec.scope == "node"
        if is_node:
            if not spec.names:
                return None
            name = spec.names[0]
        else:
            name = await _resolve_first_pod(spec, kubeconfig)
            if not name:
                return None

        usage_mc = await _fetch_cpu_usage_millicores(
            name, spec.namespace, kubeconfig, is_node=is_node
        )
        if usage_mc is None:
            return None

        if is_node:
            capacity_mc = await _fetch_node_cpu_capacity_millicores(name, kubeconfig)
        else:
            capacity_mc = await _fetch_cpu_limit_millicores(name, spec.namespace, kubeconfig)

        if capacity_mc is None or capacity_mc == 0:
            return None

        target_mc = capacity_mc * target_percent / 100
        headroom = (target_mc - usage_mc) / capacity_mc
        current_percent = round(usage_mc / capacity_mc * 100, 1)

        current_str = f"{usage_mc}m ({current_percent}%)"
        limit_str = f"{capacity_mc}m"
        target_str = f"{int(target_mc)}m ({target_percent}%)"
        delta_mc = max(0, int(target_mc - usage_mc))

        if headroom <= _HEADROOM_IMPOSSIBLE:
            return FeasibilityReport(
                severity=FeasibilitySeverity.IMPOSSIBLE,
                headroom=max(0.0, headroom),
                current_value=current_str,
                limit_value=limit_str,
                target_value=target_str,
                message=(
                    f"CPU at {current_percent}% ({usage_mc}m/{capacity_mc}m), "
                    f"target {target_percent}% — only {delta_mc}m headroom"
                ),
                recommendation="Pick a target with lower CPU usage",
            )
        elif headroom <= _HEADROOM_TIGHT:
            return FeasibilityReport(
                severity=FeasibilitySeverity.TIGHT,
                headroom=headroom,
                current_value=current_str,
                limit_value=limit_str,
                target_value=target_str,
                message=(
                    f"CPU at {current_percent}% ({usage_mc}m/{capacity_mc}m), "
                    f"target {target_percent}% — {delta_mc}m headroom (tight)"
                ),
                recommendation="Injection may succeed but effect could be marginal",
            )
        else:
            return FeasibilityReport(
                severity=FeasibilitySeverity.OK,
                headroom=headroom,
                current_value=current_str,
                limit_value=limit_str,
                target_value=target_str,
                message=f"Sufficient headroom ({headroom:.0%})",
                recommendation="",
            )


# ---------------------------------------------------------------------------
# Network checker
# ---------------------------------------------------------------------------


class NetworkFeasibilityChecker:
    blade_target = "network"

    async def assess(
        self, spec: "FaultSpec", kubeconfig: str
    ) -> FeasibilityReport | None:
        if not spec.namespace:
            return None
        if not spec.names and not spec.labels:
            return None

        # Node scope: fail-open for now (pod/container covers 90%+ of network faults)
        if spec.scope == "node":
            return None

        pod_name = await _resolve_first_pod(spec, kubeconfig)
        if not pod_name:
            return None
        namespace = spec.namespace

        phase = await _fetch_pod_phase(pod_name, namespace, kubeconfig)
        if phase is None:
            return None
        if phase != "Running":
            return FeasibilityReport(
                severity=FeasibilitySeverity.IMPOSSIBLE,
                headroom=0.0,
                current_value=f"phase={phase}",
                limit_value="Running",
                target_value="",
                message=(
                    f"Pod {pod_name} is {phase}, not Running "
                    f"— network injection ineffective"
                ),
                recommendation="Wait for Pod to be Running before injecting network faults",
            )

        interface = spec.params.get("interface", "eth0")
        iface_exists = await _check_interface_exists(
            pod_name, namespace, interface, kubeconfig
        )
        if iface_exists is False:
            return FeasibilityReport(
                severity=FeasibilitySeverity.IMPOSSIBLE,
                headroom=0.0,
                current_value=f"interface={interface} not found",
                limit_value="",
                target_value=f"--interface {interface}",
                message=f"Interface '{interface}' not found in Pod {pod_name}",
                recommendation=(
                    f"Check available interfaces: kubectl exec {pod_name} "
                    f"-n {namespace} -- ip link show"
                ),
            )

        has_conflict = await _check_active_network_experiment(
            pod_name, namespace, kubeconfig
        )
        if has_conflict:
            return FeasibilityReport(
                severity=FeasibilitySeverity.TIGHT,
                headroom=0.2,
                current_value="active network experiment exists",
                limit_value="",
                target_value="",
                message=(
                    f"Pod {pod_name} already has active network fault injection "
                    f"— stacking may cause unpredictable behavior"
                ),
                recommendation="Destroy existing network experiment before injecting a new one",
            )

        return FeasibilityReport(
            severity=FeasibilitySeverity.OK,
            headroom=1.0,
            current_value=f"phase=Running, interface={interface} present",
            limit_value="",
            target_value="",
            message="Network injection feasible",
            recommendation="",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_int_param(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


async def _fetch_pod_phase(
    pod_name: str, namespace: str, kubeconfig: str
) -> str | None:
    """kubectl get pod → .status.phase"""
    stdout = await _run_kubectl(
        ["get", "pod", pod_name, "-n", namespace,
         "-o", "jsonpath={.status.phase}"],
        kubeconfig,
    )
    return stdout if stdout else None


async def _check_interface_exists(
    pod_name: str, namespace: str, interface: str, kubeconfig: str
) -> bool | None:
    """Check if network interface exists in pod via /sys/class/net/.

    Returns True if exists, False if confirmed missing, None if cannot determine.
    """
    from chaos_agent.config.settings import settings
    from chaos_agent.tools.kubectl import _build_kubectl_global_args
    from chaos_agent.tools.shell import run_command

    kubectl_path = settings.kubectl_path
    global_args = _build_kubectl_global_args(kubeconfig)
    cmd = [kubectl_path, *global_args, "exec", pod_name, "-n", namespace,
           "--", "cat", f"/sys/class/net/{interface}/operstate"]

    try:
        result = await run_command(
            cmd, timeout=5, task_id="", skip_guard=True, source="feasibility-check",
        )
        if result.exit_code == 0:
            return True
        stderr = (result.stderr or "").lower()
        if "no such file or directory" in stderr:
            return False
        return None
    except Exception:
        return None


async def _check_active_network_experiment(
    pod_name: str, namespace: str, kubeconfig: str
) -> bool:
    """Check if there's already an active chaosblade network experiment on this pod.

    Parses ChaosBlade CR JSON to find Running experiments with target=network
    whose resourceStatuses identifier matches the pod.
    """
    import json as _json

    stdout = await _run_kubectl(
        ["get", "chaosblade", "-o", "json"],
        kubeconfig,
        timeout=8,
    )
    if not stdout:
        return False
    try:
        data = _json.loads(stdout)
    except (ValueError, TypeError):
        return False

    for item in data.get("items", []):
        if item.get("status", {}).get("phase") != "Running":
            continue
        for exp_status in item.get("status", {}).get("expStatuses", []):
            if exp_status.get("target") != "network":
                continue
            for rs in exp_status.get("resourceStatuses", []):
                # identifier format: "namespace/node/pod/container/runtime"
                identifier = rs.get("identifier", "")
                parts = identifier.split("/")
                if len(parts) >= 3 and parts[0] == namespace and parts[2] == pod_name:
                    return True
    return False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_all() -> None:
    register_feasibility_checker(MemoryFeasibilityChecker())
    register_feasibility_checker(CpuFeasibilityChecker())
    register_feasibility_checker(NetworkFeasibilityChecker())
