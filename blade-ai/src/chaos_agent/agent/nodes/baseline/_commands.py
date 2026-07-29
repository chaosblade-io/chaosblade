"""Baseline command data layer: the ``BaselineCommand`` type, the static
registry tables, and the pure lookup / normalization helpers.

Split out of ``baseline_capture.py`` (Phase 2 module split) so the large data
tables and the node/execution logic live apart. This module is a leaf: it
imports only the extractor contract, the tool-pod namespace constant, and the
channel profile constant — never ``baseline_capture`` — so there is no import
cycle. ``baseline_capture`` re-exports these names to preserve existing import
paths (tests / ``plan_generator`` import several of them by name).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from chaos_agent.agent.baseline_extractors import (
    Extractor,
    extract_pod_top_metrics,
)
from chaos_agent.agent.nodes.execute._injection_detection import (
    _TOOL_POD_NAMESPACE,
)
from chaos_agent.transports import PROFILE_HOST

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BaselineCommand data structure
# ---------------------------------------------------------------------------

@dataclass
class BaselineCommand:
    """A single baseline collection command specification.

    ``extractors`` lets a command opt into structured-field extraction:
    after the command's stdout is captured, each extractor parses it
    and the resulting dict is merged into ``state["target_metadata"]``.
    Downstream nodes (FCAT P0 size ceiling, OOMKill risk check, etc.)
    then read those structured fields by name instead of re-issuing
    their own kubectl call against the same data. See
    ``chaos_agent.agent.baseline_extractors`` for the extractor contract
    and ``extract_pod_top_metrics`` as the first concrete example.

    Extractor failure is non-fatal: an extractor that raises is logged
    at debug level and the consumer falls back to its own fresh fetch.
    """

    description: str       # "Node disk usage"
    command: str           # full command string:
                           #   k8s : "kubectl describe node {node_name}"
                           #   host: "top -bn1"
                           # registry entries may carry template variables
                           # ({node_name}/{pod_name}/{namespace}/
                           # {label_selector}/{debug_pod}); LLM-derived
                           # commands are already concrete.
    mode: str = "simple"   # "simple" | "debug_two_step"
    # Optional list of structured-field extractors. Empty for free-form
    # commands (LLM-derived baseline commands at runtime). See the
    # ``BaselineCommand`` docstring above for the contract.
    extractors: list[Extractor] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Python Registry: three-level lookup
# ---------------------------------------------------------------------------

BASELINE_COMMANDS: dict[tuple[str, ...], list[BaselineCommand]] = {
    # ── Exact match: (scope, target, action) ──
    ("node", "disk", "fill"): [
        BaselineCommand("Node DiskPressure", "kubectl describe node {node_name}"),
        BaselineCommand("Node disk usage",
                        f"kubectl exec {{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- df -h",
                        mode="debug_two_step"),
    ],
    ("node", "disk", "burn"): [
        BaselineCommand("Node conditions", "kubectl describe node {node_name}"),
        BaselineCommand("Node disk IO",
                        f"kubectl exec {{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- iostat -xd 1 3",
                        mode="debug_two_step"),
    ],
    ("pod", "process", "kill"): [
        BaselineCommand("Service endpoints", "kubectl get endpoints -n {namespace} {label_selector}"),
        BaselineCommand("Pod status/restarts", "kubectl get pod {pod_name} -n {namespace} -o wide"),
        BaselineCommand("Pod events", "kubectl describe pod {pod_name} -n {namespace}"),
    ],
    ("pod", "network", "drop"): [
        BaselineCommand("Service endpoints", "kubectl get endpoints -n {namespace} {label_selector}"),
        BaselineCommand("Pod conditions", "kubectl describe pod {pod_name} -n {namespace}"),
    ],
    # ── Target-level fallback: (scope, target) ──
    # NOTE: pod-scope injection always carries a precise ``names[0]`` (the
    # ChaosBlade target pod). Use {pod_name} as the primary locator so a
    # caller that only supplied ``names`` (labels={}) still gets a viable
    # ``kubectl top``. Aligns with extract_pod_top_metrics, which itself
    # filters output by ``names[0]`` regardless of how the row was fetched.
    ("pod", "cpu"): [
        BaselineCommand(
            "Pod CPU/Memory", "kubectl top pod {pod_name} -n {namespace}",
            extractors=[extract_pod_top_metrics],
        ),
        BaselineCommand("Pod conditions/restarts", "kubectl describe pod {pod_name} -n {namespace}"),
    ],
    ("pod", "mem"): [
        BaselineCommand(
            "Pod CPU/Memory", "kubectl top pod {pod_name} -n {namespace}",
            extractors=[extract_pod_top_metrics],
        ),
        BaselineCommand("Pod OOM events", "kubectl describe pod {pod_name} -n {namespace}"),
    ],
    ("pod", "disk"): [
        BaselineCommand("Container disk usage", "kubectl exec {pod_name} -n {namespace} -- df -h"),
    ],
    ("pod", "network"): [
        BaselineCommand("Service endpoints", "kubectl get endpoints -n {namespace} {label_selector}"),
        BaselineCommand("Pod conditions", "kubectl describe pod {pod_name} -n {namespace}"),
    ],
    ("pod", "process"): [
        BaselineCommand("Pod status", "kubectl get pod {pod_name} -n {namespace}"),
        BaselineCommand("Pod events", "kubectl describe pod {pod_name} -n {namespace}"),
    ],
    ("node", "cpu"): [
        BaselineCommand("Node resource usage", "kubectl top node {node_name}"),
        BaselineCommand("Node conditions", "kubectl describe node {node_name}"),
    ],
    ("node", "mem"): [
        BaselineCommand("Node resource usage", "kubectl top node {node_name}"),
        BaselineCommand("Node MemoryPressure", "kubectl describe node {node_name}"),
    ],
    ("node", "disk"): [
        BaselineCommand("Node DiskPressure", "kubectl describe node {node_name}"),
        BaselineCommand("Node disk usage",
                        f"kubectl exec {{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- df -h",
                        mode="debug_two_step"),
    ],
    ("node", "network"): [
        BaselineCommand("Node conditions", "kubectl describe node {node_name}"),
        BaselineCommand("Pods on node",
                        "kubectl get pods -o wide -A --field-selector spec.nodeName={node_name}"),
    ],
    ("node", "process"): [
        BaselineCommand("Node conditions", "kubectl describe node {node_name}"),
        BaselineCommand("Pods on node",
                        "kubectl get pods -o wide -A --field-selector spec.nodeName={node_name}"),
    ],
}

# Scope-only fallback (used when no match in BASELINE_COMMANDS at any level)
_SCOPE_FALLBACK: dict[str, list[BaselineCommand]] = {
    "node": [BaselineCommand("Node resource usage", "kubectl top node {node_name}")],
    "pod": [BaselineCommand("Pod resource usage",
                            "kubectl top pod {pod_name} -n {namespace}")],
    "deployment": [
        BaselineCommand("Deployment status",
                        "kubectl get deployment -n {namespace} -o wide"),
        BaselineCommand("Pod status",
                        "kubectl get pods -n {namespace} {label_selector} -o wide"),
    ],
}

# ---------------------------------------------------------------------------
# Host Registry (profile == "host"): blade_target -> host shell diagnostics
#
# Host baseline runs the SAME strategy chain as k8s (LLM -> registry ->
# fallback). These are the registry entries: plain read-only diagnostics
# with NO template variables (the connected host *is* the target, so
# resolution is the identity and every command is trivially viable).
# On failure they degrade to /proc reads via ``_HOST_FALLBACK_CHAIN``.
# ---------------------------------------------------------------------------
_HOST_BASELINE_COMMANDS: dict[str, list[BaselineCommand]] = {
    "cpu": [BaselineCommand("Host CPU/load", "top -bn1")],
    "mem": [BaselineCommand("Host memory", "free -m")],
    "disk": [
        BaselineCommand("Host disk usage", "df -h"),
        BaselineCommand("Host disk IO", "iostat -xd 1 2"),
    ],
    "network": [
        BaselineCommand("Host network stats", "ss -s"),
        BaselineCommand("Host link stats", "ip -s link"),
    ],
    "process": [
        BaselineCommand("Host processes", "ps aux"),
        BaselineCommand("Host uptime/load", "uptime"),
    ],
}

# Host scope-only fallback (no (target) match in _HOST_BASELINE_COMMANDS).
_HOST_FALLBACK: list[BaselineCommand] = [
    BaselineCommand("Host uptime/load", "uptime"),
    BaselineCommand("Host CPU/load", "top -bn1"),
]

# Host command -> ordered /proc degrade chain. Best-effort when the primary
# diagnostic binary is missing on a minimal host (mirrors the k8s iostat
# fallback idea, extended to cpu/mem/net).
_HOST_FALLBACK_CHAIN: dict[str, list[str]] = {
    "top -bn1": ["cat /proc/stat"],
    "free -m": ["cat /proc/meminfo"],
    "iostat -xd 1 2": ["cat /proc/diskstats"],
    "ss -s": ["cat /proc/net/dev"],
    "ip -s link": ["cat /proc/net/dev"],
}

# iostat two-level fallback chain for containers without sysstat installed.
# BusyBox iostat does not support -x (extended) or -c (CPU) flags, but does
# support basic -d (device) and bare iostat.  Level 1 tries the BusyBox-
# compatible form; Level 2 falls back to /proc raw data.
# Verified: /proc/diskstats in debug pods already shows host data (shared kernel).
_IOSTAT_FALLBACK_CHAIN: dict[str, list[str]] = {
    "iostat -xd 1 3": ["iostat -d 1 1", "cat /proc/diskstats"],
    "iostat -c 1 3": ["iostat 1 1", "cat /proc/stat"],
}


def _get_iostat_fallback_chain(
    v_args: str, stderr: str = "",
) -> list[str] | None:
    """Get the ordered list of fallback commands for a failed iostat exec.

    Matches the command after '--' in v_args against known iostat patterns.
    Returns a list of fallback v_args strings (prefix preserved), or None.

    When *stderr* indicates the binary was not found ("not found in" or
    "No such file"), intermediate fallbacks that use the same binary are
    skipped — they would fail identically, wasting a network round-trip.
    """
    if "--" not in v_args:
        return None
    after_dash = v_args.split("--", 1)[1].strip()
    prefix = v_args.split("--", 1)[0]
    for iostat_cmd, fallbacks in _IOSTAT_FALLBACK_CHAIN.items():
        if after_dash == iostat_cmd:
            result = [f"{prefix}-- {fb}" for fb in fallbacks]
            stderr_lower = (stderr or "").lower()
            if "not found in" in stderr_lower or "no such file" in stderr_lower:
                original_binary = iostat_cmd.split()[0]
                result = [
                    fb for fb in result
                    if fb.split("-- ", 1)[1].split()[0] != original_binary
                ]
            return result or None
    return None


def _normalize_debug_namespace(v_args: str) -> str:
    """Normalize namespace in v_args for debug_two_step commands.

    Ensures the namespace in v_args matches _TOOL_POD_NAMESPACE (chaosblade),
    which is where _exec_debug_two_step creates and deletes debug pods.

    - If -n/--namespace exists before --, replace with -n {namespace}
    - If no namespace before --, insert -n {namespace} after {debug_pod}
    """
    ns = _TOOL_POD_NAMESPACE
    # Split on -- to only modify the kubectl-side arguments
    parts = v_args.split("--", 1)
    before_dash = parts[0]
    after_dash = f"-- {parts[1]}" if len(parts) > 1 else ""

    if re.search(r'(-n\s+|--namespace\s+)\S+', before_dash):
        # Replace existing namespace
        new_before = re.sub(
            r'(-n\s+|--namespace\s+)\S+',
            f'-n {ns}',
            before_dash,
            count=1,
        )
    else:
        # Insert -n {ns} after {debug_pod}
        if "{debug_pod}" in before_dash:
            new_before = before_dash.replace(
                "{debug_pod}", f"{{debug_pod}} -n {ns}", 1,
            )
        else:
            # No {debug_pod} placeholder and no namespace — append before --
            new_before = f"{before_dash.rstrip()} -n {ns}"

    if after_dash:
        return f"{new_before}{after_dash}"
    return new_before


# ---------------------------------------------------------------------------
# LLM command whitelists and System Prompt now live in ``_baseline_profiles``
# (``validate_command`` / ``build_baseline_system_prompt``) so the prompt and
# safety layer are decoupled from any specific execution mechanism / fault
# type / connection channel. See that module for the per-profile fragments.
# ---------------------------------------------------------------------------

# FCAT P3: dimension → {scope → BaselineCommand} mapping
# Maps dimension names (declared in FCAT rules) to scope-aware concrete baseline
# commands. New dimensions or scopes can be added here without changing FCAT
# rules or knowledge docs.
# Note: iostat commands have automatic /proc fallback when iostat is unavailable
# in the container (handled in _exec_simple).
_FCAT_DIMENSION_COMMANDS: dict[str, dict[str, BaselineCommand]] = {
    "io_utilization": {
        "pod": BaselineCommand(
            "Container disk IO utilization",
            "kubectl exec {pod_name} -n {namespace} -- iostat -xd 1 3",
        ),
        "node": BaselineCommand(
            "Node disk IO utilization",
            f"kubectl exec {{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- iostat -xd 1 3",
            mode="debug_two_step",
        ),
    },
    "io_iowait": {
        "pod": BaselineCommand(
            "Container CPU iowait",
            "kubectl exec {pod_name} -n {namespace} -- iostat -c 1 3",
        ),
        "node": BaselineCommand(
            "Node CPU iowait",
            f"kubectl exec {{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- iostat -c 1 3",
            mode="debug_two_step",
        ),
    },
}


def _lookup_baseline_commands(
    profile: str, scope: str, target: str, action: str,
) -> list[BaselineCommand]:
    """Registry lookup, profile-aware.

    - ``host``: single-level lookup by blade ``target`` in
      ``_HOST_BASELINE_COMMANDS`` (the connected host *is* the target, so
      scope/action do not further narrow the command set).
    - ``k8s`` : three-level lookup exact -> (scope, target) -> (scope,)
      in ``BASELINE_COMMANDS``.
    """
    if profile == PROFILE_HOST:
        return _HOST_BASELINE_COMMANDS.get(target, [])
    for key in [(scope, target, action), (scope, target), (scope,)]:
        if key in BASELINE_COMMANDS:
            return BASELINE_COMMANDS[key]
    return []


# ---------------------------------------------------------------------------
# Observation success judgement
# ---------------------------------------------------------------------------
#
# kubectl can return exit_code=0 even on partial failures. For example, when a
# jsonpath template containing a space is split by the remote shell into two
# arguments, kubectl will resolve the first as a valid path while treating the
# second as a (non-existent) pod name — producing
# ``Error from server (NotFound)`` on stdout/stderr while still exiting 0.
# Counting that observation as a success masks real failures from the verifier
# and downstream consumers, so every success check must inspect the captured
# output as well as exit_code.
#
# Note: the kubewiz channel sometimes merges stderr into stdout, so we always
# scan stdout for these markers regardless of where the error was reported.
_KUBECTL_ERROR_MARKERS = (
    "Error from server",
    "error: ",
)


def _is_observation_success(obs: dict) -> bool:
    """Return True iff a baseline observation truly succeeded.

    Rules:
      * exit_code != 0  → False
      * exit_code == 0 but stdout contains a kubectl error marker → False
      * otherwise → True
    """
    if obs.get("exit_code") != 0:
        return False
    stdout = obs.get("stdout") or ""
    for marker in _KUBECTL_ERROR_MARKERS:
        if marker in stdout:
            return False
    return True


__all__ = [
    "_KUBECTL_ERROR_MARKERS",
    "_is_observation_success",
    "BaselineCommand",
    "BASELINE_COMMANDS",
    "_SCOPE_FALLBACK",
    "_HOST_BASELINE_COMMANDS",
    "_HOST_FALLBACK",
    "_HOST_FALLBACK_CHAIN",
    "_IOSTAT_FALLBACK_CHAIN",
    "_get_iostat_fallback_chain",
    "_normalize_debug_namespace",
    "_FCAT_DIMENSION_COMMANDS",
    "_lookup_baseline_commands",
]
