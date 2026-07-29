"""Structured metric extraction from kubectl / shell tool outputs.

Pure functions: ``extract_metrics(tool_name, command, stdout) -> dict``.
Used by three call sites:

  1. Baseline collection (pre-injection snapshot) — historical caller,
     was inlined in ``_verifier_hints._extract_baseline_key_metrics``.
  2. Layer 2 verification — PreReasoningHook prepends a short
     ``[Auto-extracted]`` summary to each new ToolMessage's content
     BEFORE ``tool_compactor`` truncates it to 1KB. The summary lives
     in the HEAD so it survives ``truncate_text``'s head-only retention.
  3. Cross-check in ``_parse_verification_result`` — compares
     extractor's ground truth against the numbers the LLM cites in
     evidence, to catch hallucinated baseline→post deltas.

No state, no side effects, never raises. ``{}`` on any failure — the
caller decides what to do with the absence of data.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_metrics(
    tool_name: str,
    command: str,
    stdout: str,
) -> dict[str, str]:
    """Extract structured metrics from a tool result.

    Dispatch is COMMAND-based (not tool-name-based): ``kubectl exec``
    can run df, top, describe, or cat /proc/... and each needs a
    different parser. The tool_name is reserved for future use (e.g.
    routing vendor-specific outputs) but currently only used as a
    diagnostic tag in logs.

    Args:
        tool_name: Tool identifier (e.g. ``"kubectl"``, ``"shell"``).
        command: The actual command/subcommand text (e.g. ``"describe
            pod xyz"``, ``"df -h"``, ``"exec ... cat /proc/diskstats"``).
        stdout: Raw command output. Empty input returns ``{}``.

    Returns:
        Dict of ``metric_name -> human_value`` (e.g.
        ``{"RestartCount": "8"}``). Empty when nothing matches.
    """
    if not stdout or not stdout.strip():
        return {}

    cmd_lower = (command or "").lower()
    metrics: dict[str, str] = {}

    # ── df -h (host or container fs via kubectl exec)
    if "df -h" in cmd_lower or "df --human" in cmd_lower:
        metrics.update(_parse_df_h(stdout))

    # ── kubectl describe pod / po
    if "describe pod" in cmd_lower or "describe po " in cmd_lower:
        metrics.update(_parse_describe_pod(stdout))

    # ── kubectl get pod -o json (API server's authoritative shape)
    if ("get pod" in cmd_lower or "get po " in cmd_lower) and (
        "-o json" in cmd_lower or "-ojson" in cmd_lower or "--output=json" in cmd_lower
    ):
        metrics.update(_parse_get_pod_json(stdout))

    # ── kubectl top pod/node
    if "top pod" in cmd_lower or "top node" in cmd_lower:
        metrics.update(_parse_kubectl_top(stdout))

    # ── /proc/diskstats (write throughput)
    if "diskstats" in cmd_lower:
        metrics.update(_parse_diskstats(stdout))

    # ── /proc/stat (CPU iowait)
    if "/proc/stat" in cmd_lower:
        metrics.update(_parse_proc_stat(stdout))

    # ── du -s (target path size)
    if re.search(r"\bdu\b", cmd_lower) and ("-s" in cmd_lower or "-sh" in cmd_lower):
        metrics.update(_parse_du_sh(stdout))

    # ── kubectl logs (error pattern counts — feeds E4 side-effect detection)
    if " logs " in f" {cmd_lower} " or cmd_lower.startswith("logs "):
        metrics.update(_parse_kubectl_logs(stdout))

    if metrics:
        logger.debug(
            "extract_metrics(%s, %r) → %s",
            tool_name, command[:60] if command else "", metrics,
        )
    return metrics


# ---------------------------------------------------------------------------
# Per-format parsers — small, focused, return {} when nothing matches
# ---------------------------------------------------------------------------


def _parse_df_h(stdout: str) -> dict[str, str]:
    """``df -h``: overlay/root partition usage.

    Recognises:
      - ``/``           → overlay/container fs ("Disk usage (overlay)")
      - ``/host``       → host node fs ("Disk usage (nodefs)")
      - filesystems starting with ``overlay`` → also overlay
    """
    metrics: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        parts = line.strip().split()
        if len(parts) < 6:
            continue
        if parts[0] == "Filesystem":
            continue
        use_pct, mount = parts[4], parts[5]
        if mount == "/" or parts[0].startswith("overlay"):
            metrics["Disk usage (overlay)"] = f"{use_pct} ({parts[2]}/{parts[1]})"
        elif mount == "/host":
            metrics["Disk usage (nodefs)"] = f"{use_pct} ({parts[2]}/{parts[1]})"
    return metrics


def _parse_describe_pod(stdout: str) -> dict[str, str]:
    """``kubectl describe pod``: restart count, ready state, termination reason."""
    metrics: dict[str, str] = {}

    for line in stdout.splitlines():
        s = line.strip()
        # ``Restart Count:  8`` (the variable-whitespace form 'describe' uses)
        if "Restart Count" in s:
            try:
                metrics["RestartCount"] = str(int(s.split()[-1]))
            except (ValueError, IndexError):
                pass
        # Ready conditions appear in multiple forms:
        #   ``Ready             True``
        #   ``Ready   True``
        # Use regex to tolerate variable spacing without false matches.
        m = re.search(r"^\s*Ready\s+(True|False)\b", s)
        if m:
            metrics["Pod Ready"] = m.group(1)
        # Last termination reason
        if "Reason:" in s:
            for kw in ("OOMKilled", "Error", "Completed", "Evicted"):
                if kw in s:
                    metrics["Last termination reason"] = kw
                    break
    return metrics


def _parse_get_pod_json(stdout: str) -> dict[str, str]:
    """``kubectl get pod -o json``: canonical structured pod state.

    More reliable than ``describe pod`` parsing — same fields but from
    the API server's authoritative JSON instead of human-formatted
    text. When both run, the JSON path wins on metric collisions
    because ``extract_metrics`` runs them in dict-update order with
    describe earlier.
    """
    metrics: dict[str, str] = {}
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return metrics

    items = (
        data.get("items", []) if isinstance(data, dict) and data.get("kind") == "PodList"
        else [data]
    )

    for pod in items:
        if not isinstance(pod, dict):
            continue
        status = pod.get("status") or {}
        cs_list = status.get("containerStatuses") or []
        if cs_list:
            cs = cs_list[0]  # first container — caller can re-call per-container
            if "restartCount" in cs:
                metrics["RestartCount"] = str(cs.get("restartCount", 0))
            if "ready" in cs:
                metrics["Pod Ready"] = "True" if cs["ready"] else "False"
            terminated = (cs.get("lastState") or {}).get("terminated") or {}
            if terminated.get("reason"):
                metrics["Last termination reason"] = terminated["reason"]
        # Pod-level phase (Running / Pending / Failed / Succeeded / Unknown)
        if status.get("phase"):
            metrics["Pod phase"] = status["phase"]
        # Top-level Ready condition (overrides cs.ready when set; more
        # authoritative for "pod overall ready" semantics)
        for cond in status.get("conditions") or []:
            if isinstance(cond, dict) and cond.get("type") == "Ready":
                metrics["Pod Ready"] = cond.get("status", "Unknown")
    return metrics


def _parse_kubectl_top(stdout: str) -> dict[str, str]:
    """``kubectl top pod/node``: current CPU/Memory usage.

    Returns only the FIRST data row's metrics. For multi-pod tables the
    caller is responsible for splitting by pod name before calling.
    """
    metrics: dict[str, str] = {}
    lines = stdout.strip().splitlines()
    if len(lines) < 2:
        return metrics

    # Header row(s) get skipped — kubectl top can echo header multiple
    # times if the output spans namespaces.
    for line in lines:
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        if parts[0].upper() in ("NAME", "POD", "NODE"):
            continue
        cpu_field = parts[1]
        mem_field = parts[2]
        # CPU in millicores ("250m") or cores ("0.5"); memory in Mi/Gi.
        if cpu_field and (cpu_field[-1] in "mn" or cpu_field.replace(".", "").isdigit()):
            metrics["CPU usage"] = cpu_field
        if mem_field.lower().endswith(("mi", "gi", "ki")):
            metrics["Memory usage"] = mem_field
        break  # first data row only
    return metrics


def _parse_diskstats(stdout: str) -> dict[str, str]:
    """``/proc/diskstats``: write throughput hint for vdb/sdb device.

    Field positions per ``Documentation/iostats.txt``:
      [0] major  [1] minor  [2] device-name  …  [9] sectors-written
    """
    for line in stdout.strip().splitlines():
        parts = line.strip().split()
        if len(parts) < 11:
            continue
        name = parts[2]
        if name.startswith("vdb") or name.startswith("sdb"):
            return {f"Disk writes ({name})": f"{parts[9]} sectors"}
    return {}


def _parse_proc_stat(stdout: str) -> dict[str, str]:
    """``/proc/stat`` first ``cpu`` line: aggregate iowait percentage.

    Skips per-CPU rows (``cpu0`` etc.) — the first row is the
    cluster-wide aggregate, which is what we want for "is the node
    waiting on IO?"
    """
    for line in stdout.strip().splitlines():
        if line.startswith("cpu ") and not line.startswith("cpu0"):
            parts = line.strip().split()
            if len(parts) >= 6:
                try:
                    idle = int(parts[4])
                    iowait = int(parts[5])
                    pct = round(iowait / max(idle + iowait, 1) * 100, 1)
                    return {"CPU iowait %": f"{pct}%"}
                except (ValueError, ZeroDivisionError):
                    pass
    return {}


def _parse_du_sh(stdout: str) -> dict[str, str]:
    """``du -s[h]``: first column of first row is target path size."""
    for line in stdout.strip().splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0]:
            return {"Target path size": parts[0]}
    return {}


# Patterns whose mere presence (count > 0) in logs is a side-effect
# signal. The verifier verdict cross-check uses these to flag the
# "no errors observed" hallucination — if extractor saw 12 OOMKilled
# log lines but LLM claims "no errors observed", that's contradiction.
_ERR_PATTERNS = (
    "OOMKilled", "CrashLoopBackOff", "panic:", "connection refused",
    "no space left", "DiskPressure", "MemoryPressure", "Evicted",
)


def _parse_kubectl_logs(stdout: str) -> dict[str, str]:
    """``kubectl logs``: count occurrences of known error patterns.

    Returns one metric per pattern that appeared at least once. Empty
    when logs are clean — explicit "0 errors" would mislead the verdict
    cross-check into asserting a positive ground truth where the
    extractor really has no signal.
    """
    counts: dict[str, int] = {}
    for pattern in _ERR_PATTERNS:
        n = stdout.count(pattern)
        if n > 0:
            counts[pattern] = n
    return {
        f"Log: {pattern} count": str(n)
        for pattern, n in counts.items()
    }


# ---------------------------------------------------------------------------
# Baseline integration — backward-compat wrapper used by
# ``_verifier_hints._extract_baseline_key_metrics``.
# ---------------------------------------------------------------------------


def extract_baseline_metrics(
    baseline: dict[str, Any] | None,
    blade_target: str,
    blade_action: str,
) -> dict[str, str]:
    """Walk a baseline ``observations`` list and aggregate metrics.

    Each observation dict carries ``{"command": str, "stdout": str,
    "description": str, "exit_code": int}``. We use ``command`` as the
    primary dispatch key (matches what the LLM actually ran) and fall
    back to ``description`` for fixtures / older traces that didn't
    record the raw command.

    The returned dict is filtered to fault-relevant metrics so the
    Layer 2 prompt only sees what the LLM should compare against (e.g.
    a disk-fill fault doesn't need iowait noise from the pod's CPU
    counter).
    """
    metrics: dict[str, str] = {}
    if not baseline:
        return metrics

    for obs in baseline.get("observations", []) or []:
        if obs.get("exit_code") != 0:
            continue
        stdout = obs.get("stdout", "") or ""
        if not stdout:
            continue
        # Dispatch key: prefer recorded command, fall back to description.
        cmd = obs.get("command") or obs.get("description", "") or ""
        metrics.update(extract_metrics("baseline", cmd, stdout))

    return _filter_metrics_by_fault(metrics, blade_target, blade_action)


# ---------------------------------------------------------------------------
# Fault-type filter — keeps Layer 2 prompt focused on what matters
# (preserves the legacy behaviour from _verifier_hints._filter_metrics_by_fault)
# ---------------------------------------------------------------------------

_ALWAYS_KEEP = frozenset({
    "RestartCount", "Pod Ready", "Pod phase",
    "Last termination reason", "Target path size",
})

_FAULT_METRICS: dict[str, frozenset[str]] = {
    "disk": frozenset({"Disk usage", "Disk writes", "CPU iowait", "Target path size"}),
    "cpu": frozenset({"CPU usage", "Memory usage", "CPU iowait"}),
    "mem": frozenset({"Memory usage", "CPU usage"}),
    "network": frozenset({"Memory usage", "CPU usage"}),
    "process": frozenset({"RestartCount", "Pod Ready"}),
}


def _filter_metrics_by_fault(
    metrics: dict[str, str],
    blade_target: str,
    blade_action: str,
) -> dict[str, str]:
    """Keep only fault-relevant metrics (prefix-match on metric name)."""
    target_key = (blade_target or "").lower()
    if target_key not in _FAULT_METRICS:
        # Unknown fault: pass-through with always-keep set merged in.
        return dict(metrics)

    relevant: set[str] = set(_ALWAYS_KEEP)
    for prefix in _FAULT_METRICS[target_key]:
        for k in metrics:
            if k.lower().startswith(prefix.lower()):
                relevant.add(k)
    return {k: v for k, v in metrics.items() if k in relevant}


__all__ = [
    "extract_metrics",
    "extract_baseline_metrics",
]
