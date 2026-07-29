"""Host-profile feasibility checks.

Carrier-agnostic counterpart to the kubectl-based checks in
``_feasibility_checkers``. When a fault targets a bare host
(``spec.scope == "host"``) there is no cluster tool pod / metrics-server to
consult — headroom must be probed directly on the host. These helpers mirror
the ``baseline_capture._exec_host_simple`` pattern: dispatch a read-only shell
diagnostic through the transport layer with ``skip_guard=True``.

Kept in a dedicated module so ``_feasibility_checkers`` (k8s-flavoured) does not
grow a parallel host copy of each probe.
"""

from __future__ import annotations

import logging

from chaos_agent.transports import PROFILE_HOST

logger = logging.getLogger(__name__)


async def _run_host(command: list[str], timeout: int = 8) -> str | None:
    """Run one read-only diagnostic on the target host. stdout or None."""
    from chaos_agent.transports import TransportTarget, execute_via_transport

    if not command:
        return None
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            command, target, timeout=timeout,
            source="feasibility-check", skip_guard=True,
            # This builds a bare shell diagnostic for ONE machine. Without the
            # gate it would run on whatever the configured channel addresses —
            # on a cluster channel that is the platform executor, producing
            # feasibility evidence about the wrong machine (task-46317228).
            expect_profile=PROFILE_HOST,
        )
        if result.exit_code != 0 or not result.stdout:
            return None
        return result.stdout.strip()
    except Exception as exc:
        logger.debug("host feasibility probe failed for %s: %s", command, exc)
        return None


async def fetch_host_disk_usage(path: str) -> tuple[int | None, int | None]:
    """Return ``(usage_percent, total_gb)`` for ``path`` on the target host.

    Runs ``df -P <path>`` directly on the host; parsing is shared with the
    kubectl path via ``_parse_df_output``.
    """
    from chaos_agent.agent.spec._feasibility_checkers import _parse_df_output

    df_out = await _run_host(["df", "-P", path], timeout=10)
    return _parse_df_output(df_out)


# ---------------------------------------------------------------------------
# Host (profile) probes — the "read via host_read" data source, mirroring the
# k8s probes in ``_feasibility_checkers``. Each carries a ``/proc`` degrade
# chain (baseline ``_HOST_FALLBACK_CHAIN`` pattern) for minimal hosts missing
# the primary diagnostic binary.
# ---------------------------------------------------------------------------


def _parse_free_used_total_mb(out: str | None) -> tuple[int | None, int | None]:
    """Parse ``free -m`` → (used MiB, total MiB) from the ``Mem:`` row."""
    if not out:
        return None, None
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].rstrip(":").lower() == "mem" and len(parts) >= 3:
            try:
                return int(parts[2]), int(parts[1])
            except ValueError:
                return None, None
    return None, None


def _parse_meminfo_used_total_mb(out: str | None) -> tuple[int | None, int | None]:
    """Parse ``/proc/meminfo`` → (used MiB, total MiB).

    used = MemTotal - MemAvailable; values are in kB → converted to MiB.
    """
    if not out:
        return None, None
    total_kb: int | None = None
    avail_kb: int | None = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            if key == "MemTotal":
                total_kb = _safe_int(parts[1])
            elif key == "MemAvailable":
                avail_kb = _safe_int(parts[1])
    if total_kb is None or avail_kb is None:
        return None, None
    return (total_kb - avail_kb) // 1024, total_kb // 1024


def _parse_loadavg_1m(out: str | None) -> float | None:
    """Parse the 1-minute load average from ``uptime`` or ``/proc/loadavg``."""
    if not out:
        return None
    text = out.strip()
    if "load average" in text:
        tail = text.split("load average:", 1)[1].strip()
        first = tail.replace(",", " ").split()
        return _safe_float(first[0]) if first else None
    # /proc/loadavg: "0.52 0.58 0.59 1/234 5678"
    first = text.split()
    return _safe_float(first[0]) if first else None


def _parse_nproc(out: str | None) -> int | None:
    """Parse the online CPU count from ``nproc`` or ``/proc/stat``."""
    if not out:
        return None
    text = out.strip()
    val = _safe_int(text)
    if val is not None and val > 0:
        return val
    # /proc/stat: count "cpuN" rows (exclude the aggregate "cpu" row).
    count = 0
    for line in text.splitlines():
        tok = line.split(" ", 1)[0]
        if tok.startswith("cpu") and tok != "cpu" and tok[3:].isdigit():
            count += 1
    return count or None


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _safe_float(value: str) -> float | None:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


class HostMemProbe:
    profile = PROFILE_HOST
    target = "mem"

    async def measure(self, spec, kubeconfig):
        from chaos_agent.agent.spec.feasibility import Measurement

        used, total = _parse_free_used_total_mb(await _run_host(["free", "-m"]))
        if used is None or total is None:
            used, total = _parse_meminfo_used_total_mb(
                await _run_host(["cat", "/proc/meminfo"])
            )
        if used is None or total is None or total == 0:
            return None
        return Measurement(current=used, limit=total)


class HostCpuProbe:
    profile = PROFILE_HOST
    target = "cpu"

    async def measure(self, spec, kubeconfig):
        from chaos_agent.agent.spec.feasibility import Measurement

        load1 = _parse_loadavg_1m(await _run_host(["uptime"]))
        if load1 is None:
            load1 = _parse_loadavg_1m(await _run_host(["cat", "/proc/loadavg"]))
        cores = _parse_nproc(await _run_host(["nproc"]))
        if cores is None:
            cores = _parse_nproc(await _run_host(["cat", "/proc/stat"]))
        if load1 is None or not cores:
            return None
        # Millicores, mirroring the k8s cpu probe: capacity = cores * 1000,
        # usage derived from the 1-minute load (load "cores busy" → millicores).
        return Measurement(current=int(load1 * 1000), limit=cores * 1000)


class HostDiskProbe:
    profile = PROFILE_HOST
    target = "disk"

    async def measure(self, spec, kubeconfig):
        from chaos_agent.agent.spec.feasibility import Measurement

        path = spec.params.get("path", "/")
        usage_pct, total_gb = await fetch_host_disk_usage(path)
        if usage_pct is None:
            return None
        return Measurement(current=usage_pct, limit=total_gb)


def register_host_probes() -> None:
    """Register the host-profile feasibility probes (mem/cpu/disk)."""
    from chaos_agent.agent.spec.feasibility import register_feasibility_probe

    register_feasibility_probe(HostMemProbe())
    register_feasibility_probe(HostCpuProbe())
    register_feasibility_probe(HostDiskProbe())
