"""Built-in feasibility checkers: Memory + CPU.

Each checker probes current resource usage via kubectl and compares
against the injection target parameters to determine headroom.

I/O helpers are module-private; they mirror the kubectl patterns in
direct_execute.py but are decoupled from task_id/tracker dependencies
so they can be called from the safety_check context.
"""

from __future__ import annotations

import logging
import time as _time
from typing import TYPE_CHECKING

from chaos_agent.agent.spec.feasibility import (
    FeasibilityReport,
    FeasibilitySeverity,
    register_feasibility_checker,
)
from chaos_agent.transports import PROFILE_K8S

if TYPE_CHECKING:
    from chaos_agent.agent.spec.fault_spec import FaultSpec

logger = logging.getLogger(__name__)

_HEADROOM_IMPOSSIBLE = 0.05
_HEADROOM_TIGHT = 0.20


# ---------------------------------------------------------------------------
# Metrics-server availability probe (TTL-cached)
# ---------------------------------------------------------------------------

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
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.transports import TransportTarget, execute_via_transport

    if not args:
        return None
    subcommand = args[0]
    sub_args = args[1:]
    cmd = build_kubectl_cmd(subcommand, sub_args, kubeconfig=kubeconfig)

    try:
        _target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, _target, timeout=timeout, source="feasibility-check",
            skip_guard=True, expect_profile=PROFILE_K8S)
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


async def _resolve_scope_target(
    spec: "FaultSpec", kubeconfig: str, *, require_namespace: bool
) -> str | None:
    """Resolve the probe target name by scope (single dispatch point).

    Centralises the ``node`` vs pod data-source selection shared by the
    metrics-server checkers so the branch lives in one place:
      - ``node`` scope → the node object name (``spec.names[0]``).
      - otherwise      → a Running pod resolved from labels/names.

    ``require_namespace`` gates the pod branch on ``spec.namespace`` being
    set: the memory checker requires it (it probes per-namespace pod metrics),
    while the cpu checker tolerates an empty namespace. Kept as a parameter so
    this convergence preserves each checker's exact pre-existing behaviour.
    """
    if spec.scope == "node":
        if not spec.names:
            return None
        return spec.names[0]
    if require_namespace and not spec.namespace:
        return None
    return await _resolve_first_pod(spec, kubeconfig)


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


async def _fetch_node_memory_usage_mb(
    node_name: str, kubeconfig: str
) -> int | None:
    """kubectl top node → memory usage in MB."""
    stdout = await _run_kubectl(
        ["top", "node", node_name, "--no-headers"],
        kubeconfig,
    )
    if not stdout:
        return None
    parts = stdout.split()
    for p in parts[1:]:
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


async def _fetch_node_memory_capacity_mb(
    node_name: str, kubeconfig: str
) -> int | None:
    """kubectl get node → status.allocatable.memory in MB."""
    from chaos_agent.utils.fault_type import parse_k8s_memory_to_mb

    stdout = await _run_kubectl(
        [
            "get", "node", node_name,
            "-o", "jsonpath={.status.allocatable.memory}",
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
    requires_metrics_server = True

    async def assess(
        self, spec: "FaultSpec", kubeconfig: str
    ) -> FeasibilityReport | None:
        target_percent = _parse_int_param(spec.params.get("mem-percent"))
        if target_percent is None or target_percent <= 0:
            return None

        # Data source is the only environment-dependent step: delegate to the
        # (profile, target) probe (k8s: node capacity/usage vs pod limit/usage;
        # host: free -m / /proc/meminfo). The headroom physics below is
        # environment-independent.
        from chaos_agent.agent.spec.feasibility import (
            profile_for_spec,
            resolve_feasibility_probe,
        )

        probe = resolve_feasibility_probe(profile_for_spec(spec), "mem")
        if probe is None:
            return None
        m = await probe.measure(spec, kubeconfig)
        if m is None or m.limit is None or m.limit == 0:
            return None
        usage_mb = int(m.current)
        limit_mb = int(m.limit)

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
    requires_metrics_server = True

    async def assess(
        self, spec: "FaultSpec", kubeconfig: str
    ) -> FeasibilityReport | None:
        target_percent = _parse_int_param(spec.params.get("cpu-percent"))
        if target_percent is None or target_percent <= 0:
            if spec.blade_action in ("fullload", "burn"):
                target_percent = 100
            else:
                return None

        # Data source is the only environment-dependent step: delegate to the
        # (profile, target) probe (k8s: node capacity vs pod limit; host: load
        # / nproc via top or /proc/stat). Physics below is environment-free.
        from chaos_agent.agent.spec.feasibility import (
            profile_for_spec,
            resolve_feasibility_probe,
        )

        probe = resolve_feasibility_probe(profile_for_spec(spec), "cpu")
        if probe is None:
            return None
        m = await probe.measure(spec, kubeconfig)
        if m is None or m.limit is None or m.limit == 0:
            return None
        usage_mc = int(m.current)
        capacity_mc = int(m.limit)

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
    requires_metrics_server = False

    async def assess(
        self, spec: "FaultSpec", kubeconfig: str
    ) -> FeasibilityReport | None:
        # Network is an availability check (not a headroom dimension), so it
        # has no (profile, target) probe. It is only assessed on the k8s
        # profile (node iptables via a tool pod); every other profile
        # (host today, any third profile tomorrow) is fail-open rather than
        # being treated as "host" by a literal string check.
        from chaos_agent.agent.spec.feasibility import profile_for_spec
        from chaos_agent.transports import PROFILE_K8S

        if profile_for_spec(spec) != PROFILE_K8S:
            return None
        # Node scope: check host iptables via chaosblade-tool pod
        if spec.scope == "node":
            if not spec.names:
                return None
            node_name = spec.names[0]
            has_iptables, iptables_detail = await _check_node_iptables_available(
                node_name, kubeconfig
            )
            if has_iptables is False:
                return FeasibilityReport(
                    severity=FeasibilitySeverity.IMPOSSIBLE,
                    headroom=0.0,
                    current_value="host iptables not available",
                    limit_value="",
                    target_value="",
                    message=(
                        f"iptables not available on node {node_name} host "
                        f"— node network faults require host iptables"
                    ),
                    recommendation=(
                        "Verify: kubectl exec <tool-pod> -n chaosblade "
                        "-- chroot /host iptables -L -n. "
                        "If chroot is missing, use a debug pod image with "
                        "iptables (e.g. nicolaka/netshoot)"
                    ),
                )
            if has_iptables is None:
                return FeasibilityReport(
                    severity=FeasibilitySeverity.TIGHT,
                    headroom=0.5,
                    current_value=f"indeterminate: {iptables_detail}",
                    limit_value="",
                    target_value="",
                    message=(
                        f"Cannot verify host iptables on node {node_name}: "
                        f"{iptables_detail}"
                    ),
                    recommendation=(
                        "Verify manually: kubectl exec <tool-pod> -n chaosblade "
                        "-- chroot /host iptables -L -n"
                    ),
                )
            return None  # feasible, proceed

        if not spec.namespace:
            return None
        if not spec.names and not spec.labels:
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
        iface_exists, iface_detail = await _check_interface_exists(
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
        if iface_exists is None:
            return FeasibilityReport(
                severity=FeasibilitySeverity.TIGHT,
                headroom=0.5,
                current_value=f"interface check failed: {iface_detail}",
                limit_value="",
                target_value=f"--interface {interface}",
                message=(
                    f"Cannot verify interface '{interface}' in Pod {pod_name}: {iface_detail}"
                ),
                recommendation=(
                    f"Verify: kubectl exec {pod_name} -n {namespace} "
                    f"-- cat /sys/class/net/{interface}/operstate"
                ),
            )

        has_iptables, iptables_detail = await _check_iptables_available(
            pod_name, namespace, kubeconfig
        )
        if has_iptables is False:
            return FeasibilityReport(
                severity=FeasibilitySeverity.IMPOSSIBLE,
                headroom=0.0,
                current_value="iptables not found",
                limit_value="",
                target_value="",
                message=(
                    f"iptables not available in Pod {pod_name} "
                    f"— ChaosBlade network faults require iptables"
                ),
                recommendation=(
                    "Use a container image that includes iptables, "
                    "or consider CNI-level network policy injection"
                ),
            )
        if has_iptables is None:
            return FeasibilityReport(
                severity=FeasibilitySeverity.TIGHT,
                headroom=0.5,
                current_value=f"iptables check failed: {iptables_detail}",
                limit_value="",
                target_value="",
                message=(
                    f"Cannot verify iptables in Pod {pod_name}: {iptables_detail}"
                ),
                recommendation=(
                    "Network injection may fail if iptables is missing. "
                    f"Verify: kubectl exec {pod_name} -n {namespace} -- iptables --version"
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
            current_value=f"phase=Running, interface={interface} present, iptables available",
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
) -> tuple[bool | None, str]:
    """Check if network interface exists in pod via /sys/class/net/.

    Returns:
        (True, "") — interface confirmed present
        (False, reason) — interface confirmed missing
        (None, reason) — indeterminate (timeout/unexpected error)
    """
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.transports import TransportTarget, execute_via_transport

    cmd = build_kubectl_cmd("exec", [pod_name, "-n", namespace,
           "--", "cat", f"/sys/class/net/{interface}/operstate"], kubeconfig=kubeconfig)

    try:
        _target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, _target, timeout=10, source="feasibility-check",
            skip_guard=True, expect_profile=PROFILE_K8S)
        if result.exit_code == 0:
            return True, ""
        stderr = (result.stderr or "").strip()
        if "no such file or directory" in stderr.lower():
            return False, stderr
        return None, stderr or f"exit code {result.exit_code}"
    except Exception as exc:
        return None, str(exc)


async def _check_iptables_available(
    pod_name: str, namespace: str, kubeconfig: str
) -> tuple[bool | None, str]:
    """Check if iptables is *functionally* available in the pod container.

    ChaosBlade network faults (drop/delay/loss/corrupt) work by injecting
    iptables rules inside the target container's network namespace.
    Simply checking `iptables --version` only verifies the binary exists but
    does NOT confirm the container has CAP_NET_ADMIN.  We use `iptables -L -n`
    which actually requires the capability to list rules.

    Returns:
        (True, "") — confirmed available (binary exists AND has permissions)
        (False, reason) — confirmed unavailable (missing binary or no permission)
        (None, reason) — indeterminate (timeout/unexpected error)
    """
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.transports import TransportTarget, execute_via_transport

    # Use `iptables -L -n` to verify actual functionality.
    # This requires CAP_NET_ADMIN; without it, exit_code != 0.
    cmd = build_kubectl_cmd("exec", [pod_name, "-n", namespace,
           "--", "iptables", "-L", "-n"], kubeconfig=kubeconfig)

    try:
        _target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, _target, timeout=10, source="feasibility-check",
            skip_guard=True, expect_profile=PROFILE_K8S)
        if result.exit_code == 0:
            return True, ""
        stderr = (result.stderr or "").strip()
        if "not found" in stderr.lower():
            return False, f"iptables binary not found: {stderr}"
        # Permission denied or capability missing — treat as unavailable
        if any(kw in stderr.lower() for kw in (
            "permission denied", "operation not permitted",
            "getsockopt", "nf_tables",
        )):
            return False, f"iptables not functional (likely missing CAP_NET_ADMIN): {stderr}"
        return None, stderr or f"exit code {result.exit_code}"
    except Exception as exc:
        return None, str(exc)


async def _find_tool_pod_on_node(
    node_name: str, kubeconfig: str
) -> tuple[str, str] | None:
    """Find a running pod on *node_name* in the chaosblade namespace.

    Used to locate a chaosblade-tool DaemonSet pod that can serve as a
    probe carrier for host-level iptables checks.
    """
    stdout = await _run_kubectl(
        ["get", "pods", "-n", "chaosblade",
         "--field-selector", f"spec.nodeName={node_name},status.phase=Running",
         "-o", "jsonpath={.items[0].metadata.name}"],
        kubeconfig, timeout=5,
    )
    if stdout:
        return stdout, "chaosblade"
    return None


async def _check_node_iptables_available(
    node_name: str, kubeconfig: str
) -> tuple[bool | None, str]:
    """Check if iptables is functionally available on a node's host.

    Node-level network faults require iptables on the host's network
    namespace.  We probe via a chaosblade-tool DaemonSet pod using
    ``chroot /host`` to access the host's iptables — this succeeds where
    a direct ``/host/sbin/iptables`` call fails, because ``chroot``
    switches to the host root filesystem where dynamic libraries are
    complete.

    Returns:
        (True, "") — host iptables confirmed available
        (False, reason) — confirmed unavailable
        (None, reason) — indeterminate (no tool pod, timeout, etc.)
    """
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.transports import TransportTarget, execute_via_transport

    tool_pod = await _find_tool_pod_on_node(node_name, kubeconfig)
    if not tool_pod:
        return None, "no chaosblade-tool pod found on target node"
    pod_name, namespace = tool_pod

    cmd = build_kubectl_cmd(
        "exec", [pod_name, "-n", namespace,
                 "--", "chroot", "/host", "iptables", "-L", "-n"],
        kubeconfig=kubeconfig,
    )

    try:
        _target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, _target, timeout=10, source="feasibility-check",
            skip_guard=True, expect_profile=PROFILE_K8S)
        if result.exit_code == 0:
            return True, ""
        stderr = (result.stderr or "").strip()
        lowered = stderr.lower()
        if "not found" in lowered or "no such file" in lowered:
            return False, f"host iptables not available: {stderr}"
        if "permission denied" in lowered or "operation not permitted" in lowered:
            return False, f"host iptables not functional (permission): {stderr}"
        return None, stderr or f"exit code {result.exit_code}"
    except Exception as exc:
        return None, str(exc)


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
# Disk checker
# ---------------------------------------------------------------------------


class DiskFeasibilityChecker:
    blade_target = "disk"
    requires_metrics_server = False

    async def assess(
        self, spec: "FaultSpec", kubeconfig: str
    ) -> FeasibilityReport | None:
        path = spec.params.get("path", "/")

        # Data source is the only environment-dependent step: delegate to the
        # (profile, target) probe (k8s: node tool-pod df vs pod df; host: df /
        # /proc). ``fill`` vs ``burn`` physics below is environment-free.
        from chaos_agent.agent.spec.feasibility import (
            profile_for_spec,
            resolve_feasibility_probe,
        )

        probe = resolve_feasibility_probe(profile_for_spec(spec), "disk")
        if probe is None:
            return None
        m = await probe.measure(spec, kubeconfig)
        if m is None:
            return None
        usage_pct = int(m.current)
        total_gb = m.limit

        total_str = f"{total_gb}G" if total_gb else "?"

        # ``fill`` vs ``burn`` is a legitimate per-action difference: only the
        # fill action has a target-percent headroom to assess; burn just needs
        # an accessible path. This action branch is intentionally kept (not a
        # hard-coded scope/target check).
        if spec.blade_action == "fill":
            target_pct = _parse_int_param(spec.params.get("percent"))
            if target_pct is None:
                target_pct = 90
            headroom = (target_pct - usage_pct) / 100
            if headroom <= _HEADROOM_IMPOSSIBLE:
                return FeasibilityReport(
                    severity=FeasibilitySeverity.IMPOSSIBLE,
                    headroom=max(0.0, headroom),
                    current_value=f"{usage_pct}% (path={path})",
                    limit_value=total_str,
                    target_value=f"{target_pct}%",
                    message=(
                        f"Disk at {usage_pct}% on {path}, target {target_pct}% "
                        f"— already near/above target"
                    ),
                    recommendation="Choose a path with more free space",
                )
            elif headroom <= _HEADROOM_TIGHT:
                return FeasibilityReport(
                    severity=FeasibilitySeverity.TIGHT,
                    headroom=headroom,
                    current_value=f"{usage_pct}% (path={path})",
                    limit_value=total_str,
                    target_value=f"{target_pct}%",
                    message=(
                        f"Disk at {usage_pct}% on {path}, target {target_pct}% "
                        f"— tight headroom"
                    ),
                    recommendation="Injection may succeed but fill amount is small",
                )
            return FeasibilityReport(
                severity=FeasibilitySeverity.OK,
                headroom=headroom,
                current_value=f"{usage_pct}% (path={path})",
                limit_value=total_str,
                target_value=f"{target_pct}%",
                message=f"Sufficient disk headroom ({headroom:.0%})",
                recommendation="",
            )

        # burn: just verify path is accessible and report current usage
        return FeasibilityReport(
            severity=FeasibilitySeverity.OK,
            headroom=1.0 - usage_pct / 100,
            current_value=f"{usage_pct}% (path={path})",
            limit_value=total_str,
            target_value=f"IO burn on {path}",
            message=f"Disk burn feasible, current usage {usage_pct}%",
            recommendation="",
        )


async def _fetch_node_disk_usage(
    node_name: str, path: str, kubeconfig: str
) -> tuple[int | None, int | None]:
    """Get disk usage % and total GB for a path on a node via tool pod.

    Uses `kubectl exec <tool-pod-on-node> -- df <path>` to check.
    Falls back to `kubectl get node` ephemeral-storage if tool pod unavailable.
    """
    import re as _re

    # Find tool pod on target node
    stdout = await _run_kubectl(
        ["get", "pods", "-n", "chaosblade", "-l", "app=otel-c-tool",
         "-o", "wide", "--no-headers"],
        kubeconfig, timeout=8,
    )
    if not stdout:
        return None, None

    tool_pod = None
    for line in stdout.strip().splitlines():
        cols = _re.split(r"\s{2,}", line.strip())
        if len(cols) >= 7 and cols[2] == "Running" and cols[6] == node_name:
            tool_pod = cols[0]
            break

    if not tool_pod:
        return None, None

    # df on the path inside the tool pod (host filesystem is mounted)
    df_out = await _run_kubectl(
        ["exec", tool_pod, "-n", "chaosblade", "--",
         "df", "-P", path],
        kubeconfig, timeout=10,
    )
    return _parse_df_output(df_out)


async def _fetch_pod_disk_usage(
    pod_name: str, namespace: str, path: str, kubeconfig: str
) -> tuple[int | None, int | None]:
    """Get disk usage % and total GB for a path inside a pod."""
    df_out = await _run_kubectl(
        ["exec", pod_name, "-n", namespace, "--",
         "df", "-P", path],
        kubeconfig, timeout=10,
    )
    return _parse_df_output(df_out)


def _parse_df_output(df_out: str | None) -> tuple[int | None, int | None]:
    """Parse `df -P` output → (usage_percent, total_gb)."""
    if not df_out:
        return None, None
    lines = df_out.strip().splitlines()
    if len(lines) < 2:
        return None, None
    # df -P format: Filesystem  1024-blocks  Used  Available  Capacity  Mounted
    parts = lines[-1].split()
    if len(parts) < 5:
        return None, None
    try:
        total_kb = int(parts[1])
        total_gb = round(total_kb / 1024 / 1024, 1)
        pct_str = parts[4].replace("%", "")
        usage_pct = int(pct_str)
        return usage_pct, total_gb
    except (ValueError, IndexError):
        return None, None


# ---------------------------------------------------------------------------
# k8s (profile) probes — the "read via kubectl" data source for each target
# dimension. They wrap the module's existing ``_fetch_*`` helpers so the k8s
# numbers are byte-identical to the pre-refactor inline fetches; the node-vs-pod
# data-source branch is a legitimate per-scope difference kept inside the probe.
# ---------------------------------------------------------------------------


class K8sMemProbe:
    profile = PROFILE_K8S
    target = "mem"

    async def measure(self, spec: "FaultSpec", kubeconfig: str):
        from chaos_agent.agent.spec.feasibility import Measurement

        is_node = spec.scope == "node"
        name = await _resolve_scope_target(spec, kubeconfig, require_namespace=True)
        if not name:
            return None
        if is_node:
            usage = await _fetch_node_memory_usage_mb(name, kubeconfig)
            limit = await _fetch_node_memory_capacity_mb(name, kubeconfig)
        else:
            usage = await _fetch_memory_usage_mb(name, spec.namespace, kubeconfig)
            limit = await _fetch_memory_limit_mb(name, spec.namespace, kubeconfig)
        if usage is None or limit is None:
            return None
        return Measurement(current=usage, limit=limit)


class K8sCpuProbe:
    profile = PROFILE_K8S
    target = "cpu"

    async def measure(self, spec: "FaultSpec", kubeconfig: str):
        from chaos_agent.agent.spec.feasibility import Measurement

        is_node = spec.scope == "node"
        name = await _resolve_scope_target(spec, kubeconfig, require_namespace=False)
        if not name:
            return None
        usage = await _fetch_cpu_usage_millicores(
            name, spec.namespace, kubeconfig, is_node=is_node
        )
        if usage is None:
            return None
        if is_node:
            capacity = await _fetch_node_cpu_capacity_millicores(name, kubeconfig)
        else:
            capacity = await _fetch_cpu_limit_millicores(name, spec.namespace, kubeconfig)
        if capacity is None:
            return None
        return Measurement(current=usage, limit=capacity)


class K8sDiskProbe:
    profile = PROFILE_K8S
    target = "disk"

    async def measure(self, spec: "FaultSpec", kubeconfig: str):
        from chaos_agent.agent.spec.feasibility import Measurement

        path = spec.params.get("path", "/")
        if spec.scope == "node":
            if not spec.names:
                return None
            usage_pct, total_gb = await _fetch_node_disk_usage(
                spec.names[0], path, kubeconfig
            )
        else:
            pod_name = await _resolve_first_pod(spec, kubeconfig)
            if not pod_name:
                return None
            usage_pct, total_gb = await _fetch_pod_disk_usage(
                pod_name, spec.namespace or "default", path, kubeconfig
            )
        if usage_pct is None:
            return None
        return Measurement(current=usage_pct, limit=total_gb)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_all() -> None:
    register_feasibility_checker(MemoryFeasibilityChecker())
    register_feasibility_checker(CpuFeasibilityChecker())
    register_feasibility_checker(NetworkFeasibilityChecker())
    register_feasibility_checker(DiskFeasibilityChecker())

    # (profile, target) probes: k8s here + host in the host module. The
    # checkers above resolve these by (profile_for_spec(spec), target).
    from chaos_agent.agent.spec.feasibility import register_feasibility_probe

    register_feasibility_probe(K8sMemProbe())
    register_feasibility_probe(K8sCpuProbe())
    register_feasibility_probe(K8sDiskProbe())

    from chaos_agent.agent.spec._host_feasibility_checkers import register_host_probes

    register_host_probes()
