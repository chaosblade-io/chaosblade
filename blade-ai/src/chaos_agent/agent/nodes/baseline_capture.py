"""Baseline capture node: pre-injection metric collection for direct mode.

Collects baseline metrics before fault injection so the verifier can perform
before/after comparison instead of relying solely on absolute thresholds.

Shared across ALL execution modes (direct and NL) — baseline_capture runs
after safety_check/confirmation_gate for every fault injection flow, then
route_after_baseline dispatches to direct_execute or execute_loop.

Strategy priority:
  1. LLM-driven (parse full skill_case_content to derive commands)
  2. Python Registry three-level lookup (scope,target,action) -> (scope,target) -> (scope,)
  3. Scope fallback

Design principle: best-effort — any failure does NOT block injection.
"""

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage

from chaos_agent.agent.baseline_extractors import (
    Extractor,
    extract_pod_top_metrics,
)
from chaos_agent.agent.nodes._injection_detection import _TOOL_POD_NAMESPACE, discover_tool_pods_with_nodes
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.errors import ToolGuardError, ToolTimeoutError
from chaos_agent.memory.session_store import get_global_session_store
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.tools.kubectl import _build_kubectl_global_args, _split_args
from chaos_agent.tools.shell import run_command
from chaos_agent.utils.time import now_iso

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
    subcommand: str        # kubectl subcommand: "top", "describe", "get", "exec", "debug"
    v_args_template: str   # "node/{node_name}" or "{pod_name} -n {namespace} -- df -h"
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
        BaselineCommand("Node DiskPressure", "describe", "node {node_name}"),
        BaselineCommand("Node disk usage", "exec",
                        f"{{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- df -h",
                        mode="debug_two_step"),
    ],
    ("node", "disk", "burn"): [
        BaselineCommand("Node conditions", "describe", "node {node_name}"),
        BaselineCommand("Node disk IO", "exec",
                        f"{{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- iostat -xd 1 3",
                        mode="debug_two_step"),
    ],
    ("pod", "network", "loss"): [
        BaselineCommand("Service endpoints", "get", "endpoints -n {namespace}"),
        BaselineCommand("Pod conditions", "describe", "pod {pod_name} -n {namespace}"),
    ],
    ("pod", "network", "delay"): [
        BaselineCommand("Service endpoints", "get", "endpoints -n {namespace}"),
        BaselineCommand("Pod conditions", "describe", "pod {pod_name} -n {namespace}"),
    ],
    # ── Target-level fallback: (scope, target) ──
    ("pod", "cpu"): [
        BaselineCommand(
            "Pod CPU/Memory", "top", "pod -n {namespace} {label_selector}",
            extractors=[extract_pod_top_metrics],
        ),
        BaselineCommand("Pod conditions/restarts", "describe", "pod {pod_name} -n {namespace}"),
    ],
    ("pod", "mem"): [
        BaselineCommand(
            "Pod CPU/Memory", "top", "pod -n {namespace} {label_selector}",
            extractors=[extract_pod_top_metrics],
        ),
        BaselineCommand("Pod OOM events", "describe", "pod {pod_name} -n {namespace}"),
    ],
    ("pod", "disk"): [
        BaselineCommand("Container disk usage", "exec", "{pod_name} -n {namespace} -- df -h"),
    ],
    ("pod", "network"): [
        BaselineCommand("Service endpoints", "get", "endpoints -n {namespace}"),
        BaselineCommand("Pod conditions", "describe", "pod {pod_name} -n {namespace}"),
    ],
    ("pod", "process"): [
        BaselineCommand("Pod status", "get", "pods -n {namespace} {label_selector}"),
        BaselineCommand("Pod events", "describe", "pod {pod_name} -n {namespace}"),
    ],
    ("node", "cpu"): [
        BaselineCommand("Node resource usage", "top", "node {node_name}"),
        BaselineCommand("Node conditions", "describe", "node {node_name}"),
    ],
    ("node", "mem"): [
        BaselineCommand("Node resource usage", "top", "node {node_name}"),
        BaselineCommand("Node MemoryPressure", "describe", "node {node_name}"),
    ],
    ("node", "disk"): [
        BaselineCommand("Node DiskPressure", "describe", "node {node_name}"),
        BaselineCommand("Node disk usage", "exec",
                        f"{{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- df -h",
                        mode="debug_two_step"),
    ],
    ("node", "network"): [
        BaselineCommand("Node conditions", "describe", "node {node_name}"),
        BaselineCommand("Pods on node", "get",
                        "pods -o wide -A --field-selector spec.nodeName={node_name}"),
    ],
}

# Scope-only fallback (used when no match in BASELINE_COMMANDS at any level)
_SCOPE_FALLBACK: dict[str, list[BaselineCommand]] = {
    "node": [BaselineCommand("Node resource usage", "top", "node {node_name}")],
    "pod": [BaselineCommand("Pod resource usage", "top",
                            "pod -n {namespace} {label_selector}")],
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


# Allowed kubectl subcommands (whitelist for LLM-generated commands)
_ALLOWED_SUBCOMMANDS = frozenset({"get", "top", "describe", "exec", "debug"})

# Allowed commands after `--` in kubectl exec (read-only diagnostics)
_ALLOWED_EXEC_COMMANDS = frozenset({
    "df", "ps", "ls", "cat", "top", "iostat", "free",
    "uptime", "hostname", "mount", "grep", "wc", "du",
    "head", "tail", "find", "stat", "ip", "ss", "netstat",
})

# ---------------------------------------------------------------------------
# LLM System Prompt for baseline derivation (U-shaped architecture)
#
# U-shaped attention principle (Liu et al., 2023): LLMs attend most to
# the BEGINNING (primacy effect) and END (recency effect) of prompts,
# with lowest compliance in the MIDDLE. This prompt places critical rules
# at both extremes and supporting details in the middle zone.
#
# Note: subcommand='debug' prohibition rule is NOT included here — it is
# fully covered by _validate_and_filter_commands() Phase 2 (programmatic
# defense is the responsibility layer, prompt rules are the knowledge layer).
# ---------------------------------------------------------------------------
_BASELINE_SYSTEM_PROMPT = (
    # ── Primacy zone (highest LLM attention): role + critical rules ──
    "You are a chaos engineering baseline collection strategist. "
    "Derive the pre-injection baseline metrics for before/after comparison.\n\n"

    "### CRITICAL RULES (mandatory — violations produce unexecutable commands)\n\n"

    "1. **Scope→Variables mapping** — node-scope uses {node_name}, {debug_pod}; "
    "pod/container-scope uses {pod_name}, {namespace}, {label_selector}. "
    "Using wrong scope variables (e.g., {pod_name} in node-scope) makes "
    "commands unexecutable after template resolution.\n\n"

    "2. **{debug_pod} → debug_two_step** — If v_args_template contains "
    "{debug_pod}, mode MUST be 'debug_two_step'. This variable is only "
    "resolved by the debug_two_step execution path.\n\n"

    "3. **exec command whitelist** — For kubectl exec, the command after -- "
    "MUST be a read-only diagnostic command. "
    f"Allowed: {', '.join(sorted(_ALLOWED_EXEC_COMMANDS))}.\n\n"

    "4. **Output format** — Output ONLY a JSON list, no other text. "
    "Each element:\n"
    '  {"description": "...", "subcommand": "get|top|describe|exec|debug", '
    '"v_args_template": "...", "mode": "simple|debug_two_step"}\n\n'

    "5. **Conciseness** — 2-4 commands maximum. Focus on metrics most "
    "relevant to the fault type.\n\n"

    # ── Middle zone (lowest attention): schema details + template variables ──
    "### Output Schema Details\n"
    "- description: short metric label (e.g., 'Node disk usage', "
    "'Pod CPU/Memory')\n"
    "- subcommand: kubectl subcommand (allowed: get, top, describe, exec, debug)\n"
    "- v_args_template: kubectl arguments with template variables\n"
    "- mode: 'simple' or 'debug_two_step'\n\n"

    "### Template Variables\n"
    "Available: {namespace}, {node_name}, {pod_name}, "
    "{label_selector}, {debug_pod}\n\n"

    # ── Recency zone (highest LLM attention): critical rules reminder ──
    "### REMINDER (critical rules recap)\n"
    "1. Scope→variables: node={node_name},{debug_pod}; "
    "pod={pod_name},{namespace},{label_selector}\n"
    "2. {debug_pod} → mode MUST be debug_two_step\n"
    "3. Only output the JSON list, no other text\n"
)

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
            "exec",
            "{pod_name} -n {namespace} -- iostat -xd 1 3",
        ),
        "node": BaselineCommand(
            "Node disk IO utilization",
            "exec",
            f"{{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- iostat -xd 1 3",
            mode="debug_two_step",
        ),
    },
    "io_iowait": {
        "pod": BaselineCommand(
            "Container CPU iowait",
            "exec",
            "{pod_name} -n {namespace} -- iostat -c 1 3",
        ),
        "node": BaselineCommand(
            "Node CPU iowait",
            "exec",
            f"{{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- iostat -c 1 3",
            mode="debug_two_step",
        ),
    },
}


def _lookup_baseline_commands(scope: str, target: str, action: str) -> list[BaselineCommand]:
    """Three-level lookup: exact -> target -> scope."""
    for key in [(scope, target, action), (scope, target), (scope,)]:
        if key in BASELINE_COMMANDS:
            return BASELINE_COMMANDS[key]
    return []


# ---------------------------------------------------------------------------
# Template variable resolution
# ---------------------------------------------------------------------------

def _resolve_templates(
    commands: list[BaselineCommand],
    state: AgentState,
) -> list[dict]:
    """Resolve template variables in BaselineCommand list.

    Returns list of dicts with resolved values. Unresolvable commands
    are marked with _unresolved=True so _execute_observations can skip them.
    """
    from chaos_agent.agent.fault_spec import read_fault_spec

    # Single source of truth — FaultSpec contracts names as
    # ``tuple[str, ...]`` and labels as ``dict[str, str]`` so we
    # don't need the old coerce-on-read defensive layer. The previous
    # design read state.target/state.blade_scope directly with
    # coerce_to_dict / coerce_to_list to absorb LLM-side shape drift;
    # all that shape normalisation now lives in FaultSpec's
    # constructors so consumers get clean types.
    spec = read_fault_spec(state)
    if spec is None:
        # No spec → no useful target info → produce one unresolved
        # entry per command so the caller logs + skips uniformly.
        return [
            {
                "description": cmd.description,
                "subcommand": cmd.subcommand,
                "v_args": cmd.v_args_template,
                "mode": cmd.mode,
                "_unresolved": True,
                "_node_name": "",
                "_extractors": cmd.extractors,
            }
            for cmd in commands
        ]

    namespace = spec.namespace
    names = list(spec.names)
    node_name = names[0] if names else ""
    # For node-scope, names contains node names — not pod names.
    # Setting pod_name to a node name causes incorrect baseline commands
    # (e.g., kubectl exec into a "pod" that is actually a node name).
    pod_name = "" if spec.scope == "node" else (names[0] if names else "")
    labels_dict = spec.labels
    label_selector = (
        "-l " + ",".join(f"{k}={v}" for k, v in labels_dict.items())
        if labels_dict
        else ""
    )

    resolved = []
    for cmd in commands:
        v_args = cmd.v_args_template
        unresolved = False

        # Replace known variables
        if "{namespace}" in v_args:
            if namespace:
                v_args = v_args.replace("{namespace}", namespace)
            else:
                unresolved = True
        if "{node_name}" in v_args:
            if node_name:
                v_args = v_args.replace("{node_name}", node_name)
            else:
                unresolved = True
        if "{pod_name}" in v_args:
            if pod_name:
                v_args = v_args.replace("{pod_name}", pod_name)
            else:
                unresolved = True
        if "{label_selector}" in v_args:
            if label_selector:
                v_args = v_args.replace("{label_selector}", label_selector)
            else:
                unresolved = True
        # {debug_pod} is resolved later in _exec_debug_two_step

        # Deep defense: auto-correct mode if {debug_pod} present but mode is wrong
        mode = cmd.mode
        if "{debug_pod}" in v_args and mode != "debug_two_step":
            logger.warning(
                "Deep defense: auto-correcting mode from '%s' to "
                "'debug_two_step' in _resolve_templates for: %s",
                mode, cmd.description,
            )
            mode = "debug_two_step"

        # Normalize namespace for debug_two_step commands
        if mode == "debug_two_step":
            v_args = _normalize_debug_namespace(v_args)

        # Detect unknown template variables left after known-variable replacement.
        # Any remaining {xxx} (except {debug_pod}) indicates the LLM invented a
        # variable we don't support, making the command non-executable.
        if not unresolved:
            remaining_vars = re.findall(r'\{([a-z_]+)\}', v_args)
            unknown_vars = [v for v in remaining_vars if v != "debug_pod"]
            if unknown_vars:
                logger.warning(
                    "Unknown template variable(s) in baseline command '%s': %s",
                    cmd.description, unknown_vars,
                )
                unresolved = True

        entry = {
            "description": cmd.description,
            "subcommand": cmd.subcommand,
            "v_args": v_args,
            "mode": mode,
            "_unresolved": unresolved,
            "_node_name": node_name,
            # Pass extractor callables through resolution so the
            # post-execute extraction loop can reach them. Underscore
            # prefix marks this as an internal runtime field — the
            # observation dicts written to ``baseline_data`` strip
            # underscored keys, so extractors never leak onto the
            # wire / into persisted state.
            "_extractors": cmd.extractors,
        }
        resolved.append(entry)

    return resolved


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

async def _execute_observations(
    commands: list[dict],
    kubeconfig: str,
    task_id: str,
) -> list[dict]:
    """Execute baseline collection commands, return observation results.

    Each result: {"description": str, "command": str, "exit_code": int,
                  "stdout": str, "stderr": str}

    Debug pods are created once per node and reused across all
    debug_two_step commands, then cleaned up in a single finally block.
    This avoids the race condition where each command creates/destroys
    its own pod and exec fails because the container isn't ready yet.

    Emits per-command tracker.update() so the CLI shows progress with
    output previews (truncated for CLI readability).
    """
    tracker = get_tracker(task_id) if task_id and task_id != "unknown" else None
    observations = []

    # ── Pre-create debug pods for all debug_two_step commands ──
    debug_pods: dict[str, str] = {}  # node_name -> pod_name
    # Tool pod fallback: when debug pod creation fails, discover a tool pod
    tool_pod_fallbacks: dict[str, str] = {}  # node_name -> tool_pod_name
    debug_two_step_cmds = [c for c in commands if c.get("mode") == "debug_two_step"]

    if debug_two_step_cmds:
        node_names = set(c.get("_node_name", "") for c in debug_two_step_cmds)
        node_names.discard("")
        for node_name in node_names:
            pod_name = await _create_and_wait_debug_pod(
                node_name, kubeconfig, task_id,
            )
            if pod_name:
                debug_pods[node_name] = pod_name
            else:
                # Fallback: try to find a tool pod on this node
                tool_pod = await _discover_tool_pod_on_node(
                    node_name, kubeconfig, task_id,
                )
                if tool_pod:
                    tool_pod_fallbacks[node_name] = tool_pod
                    logger.info(
                        "Debug pod unavailable for node %s, using tool pod %s "
                        "as fallback for baseline commands",
                        node_name, tool_pod,
                    )

    try:
        for idx, cmd_info in enumerate(commands, 1):
            if cmd_info.get("_unresolved"):
                logger.warning(
                    "Skipping unresolved command: %s", cmd_info['description'],
                )
                if tracker:
                    tracker.update(
                        f"[{idx}/{len(commands)}] Skipped (unresolved): "
                        f"{cmd_info['description']}",
                        {"step": idx, "description": cmd_info["description"],
                         "status": "skipped"},
                    )
                continue

            try:
                if cmd_info["mode"] == "debug_two_step":
                    node_name = cmd_info.get("_node_name", "")
                    if node_name in debug_pods:
                        obs = await _exec_in_debug_pod(
                            cmd_info, kubeconfig, task_id, debug_pods,
                        )
                    elif node_name in tool_pod_fallbacks:
                        obs = await _exec_in_tool_pod(
                            cmd_info, kubeconfig, task_id,
                            tool_pod_fallbacks[node_name],
                        )
                    else:
                        obs = {
                            "description": cmd_info["description"],
                            "command": "",
                            "exit_code": -1,
                            "stdout": "",
                            "stderr": (
                                f"No debug pod or tool pod available for "
                                f"node {node_name}"
                            ),
                        }
                else:
                    obs = await _exec_simple(cmd_info, kubeconfig, task_id)
                observations.append(obs)

                # Emit per-command tracker update with output preview
                if tracker:
                    _preview = (
                        obs.get("stdout", "")[:200]
                        or obs.get("stderr", "")[:200]
                        or "(empty)"
                    )
                    _status = (
                        "ok" if obs.get("exit_code") == 0
                        else f"exit={obs.get('exit_code')}"
                    )
                    tracker.update(
                        f"[{idx}/{len(commands)}] {obs['description']}: "
                        f"{_status} — {_preview}",
                        {"step": idx, "description": obs["description"],
                         "exit_code": obs.get("exit_code"), "status": _status},
                    )
            except Exception as e:
                obs = {
                    "description": cmd_info["description"],
                    "command": cmd_info.get("_full_command", ""),
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": str(e),
                }
                observations.append(obs)
                if tracker:
                    tracker.update(
                        f"[{idx}/{len(commands)}] {obs['description']}: "
                        f"error — {str(e)[:200]}",
                        {"step": idx, "description": obs["description"],
                         "exit_code": -1, "status": "error"},
                    )
    finally:
        # ── Cleanup all debug pods ──
        for pod_name in debug_pods.values():
            await _delete_debug_pod(pod_name, kubeconfig, task_id)

    return observations


async def _exec_simple(cmd_info: dict, kubeconfig: str, task_id: str) -> dict:
    """Execute a simple kubectl command.

    When the command is a kubectl exec containing iostat and it fails
    (because sysstat is not installed in the container), automatically
    retries with BusyBox-compatible iostat first, then /proc fallback.
    """
    cmd = [settings.kubectl_path]
    cmd.extend(_build_kubectl_global_args(kubeconfig))
    cmd.append(cmd_info["subcommand"])
    cmd.extend(_split_args(cmd_info["v_args"]))
    timeout = (
        settings.timeout_kubectl_exec
        if cmd_info["subcommand"] == "exec"
        else settings.timeout_kubectl
    )
    result = await run_command(cmd, timeout=timeout, task_id=task_id)

    # Two-level iostat fallback
    if result.exit_code != 0 and cmd_info["subcommand"] == "exec":
        fallback_list = _get_iostat_fallback_chain(cmd_info["v_args"], result.stderr)
        if fallback_list:
            for fb_v_args in fallback_list:
                logger.info(
                    "iostat unavailable in container, retrying with "
                    "fallback: %s", fb_v_args,
                )
                fb_cmd = [settings.kubectl_path]
                fb_cmd.extend(_build_kubectl_global_args(kubeconfig))
                fb_cmd.append("exec")
                fb_cmd.extend(_split_args(fb_v_args))
                fb_result = await run_command(
                    fb_cmd, timeout=settings.timeout_kubectl_exec,
                    task_id=task_id,
                )
                if fb_result.exit_code == 0:
                    return {
                        "description": cmd_info["description"],
                        "command": " ".join(fb_cmd),
                        "exit_code": 0,
                        "stdout": fb_result.stdout,
                        "stderr": fb_result.stderr,
                    }

    return {
        "description": cmd_info["description"],
        "command": " ".join(cmd),
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


async def _exec_debug_two_step(
    cmd_info: dict, kubeconfig: str, task_id: str,
) -> dict:
    """Execute debug_two_step: kubectl debug -> wait -> kubectl exec -> kubectl delete.

    Kept for backward compatibility and standalone use.  The main path
    (_execute_observations) now uses _exec_in_debug_pod with pod reuse.
    """
    node_name = cmd_info.get("_node_name", "")
    if not node_name:
        return {
            "description": cmd_info["description"],
            "command": "",
            "exit_code": -1,
            "stdout": "",
            "stderr": "No node_name for debug_two_step",
        }

    # Step 1: kubectl debug node/{node} -n {namespace} --image=busybox -- sleep 3600
    debug_cmd = [settings.kubectl_path]
    debug_cmd.extend(_build_kubectl_global_args(kubeconfig))
    debug_cmd.extend([
        "debug", f"node/{node_name}", "-n", _TOOL_POD_NAMESPACE,
        "--image=busybox", "--", "sleep", "3600",
    ])
    debug_result = await run_command(
        debug_cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id,
    )

    if debug_result.exit_code != 0:
        return {
            "description": cmd_info["description"],
            "command": " ".join(debug_cmd),
            "exit_code": debug_result.exit_code,
            "stdout": debug_result.stdout,
            "stderr": debug_result.stderr,
        }

    # Parse debug pod name from output like "pod/<name> created"
    debug_pod = _parse_debug_pod_name(debug_result.stdout)
    if not debug_pod:
        return {
            "description": cmd_info["description"],
            "command": " ".join(debug_cmd),
            "exit_code": -1,
            "stdout": debug_result.stdout,
            "stderr": "Failed to parse debug pod name from output",
        }

    # Step 1.5: wait for container readiness (prevents race condition)
    await _wait_for_debug_pod_ready(debug_pod, kubeconfig, task_id)

    try:
        # Step 2: kubectl exec {debug_pod} -n {namespace} -c debugger -- {cmd}
        v_args = cmd_info["v_args"].replace("{debug_pod}", debug_pod)

        # Namespace defense: ensure exec targets the same namespace as the debug pod
        _has_ns = "-n " in v_args or "--namespace " in v_args
        _has_correct_ns = (
            f"-n {_TOOL_POD_NAMESPACE}" in v_args
            or f"--namespace {_TOOL_POD_NAMESPACE}" in v_args
        )
        if _has_ns and not _has_correct_ns:
            logger.warning(
                "Overriding namespace in debug_two_step exec to '%s'",
                _TOOL_POD_NAMESPACE,
            )
            v_args = re.sub(
                r'(-n\s+|--namespace\s+)\S+',
                f'-n {_TOOL_POD_NAMESPACE}', v_args, count=1,
            )
        elif not _has_ns:
            # No namespace at all — insert after debug pod name
            v_args = v_args.replace(debug_pod, f"{debug_pod} -n {_TOOL_POD_NAMESPACE}", 1)

        exec_cmd = [settings.kubectl_path]
        exec_cmd.extend(_build_kubectl_global_args(kubeconfig))
        exec_cmd.append("exec")
        exec_cmd.extend(["-c", _DEBUG_CONTAINER_NAME])
        exec_cmd.extend(_split_args(v_args))
        exec_result = await run_command(
            exec_cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id,
        )

        # Two-level iostat fallback
        if exec_result.exit_code != 0:
            fallback_list = _get_iostat_fallback_chain(v_args, exec_result.stderr)
            if fallback_list:
                for fb_v_args in fallback_list:
                    logger.info(
                        "iostat unavailable in debug pod, retrying with "
                        "fallback: %s", fb_v_args,
                    )
                    fb_cmd = [settings.kubectl_path]
                    fb_cmd.extend(_build_kubectl_global_args(kubeconfig))
                    fb_cmd.append("exec")
                    fb_cmd.extend(["-c", _DEBUG_CONTAINER_NAME])
                    fb_cmd.extend(_split_args(fb_v_args))
                    fb_result = await run_command(
                        fb_cmd, timeout=settings.timeout_kubectl_exec,
                        task_id=task_id,
                    )
                    if fb_result.exit_code == 0:
                        return {
                            "description": cmd_info["description"],
                            "command": " ".join(fb_cmd),
                            "exit_code": 0,
                            "stdout": fb_result.stdout,
                            "stderr": fb_result.stderr,
                        }

        return {
            "description": cmd_info["description"],
            "command": " ".join(exec_cmd),
            "exit_code": exec_result.exit_code,
            "stdout": exec_result.stdout,
            "stderr": exec_result.stderr,
        }
    finally:
        # Step 3: cleanup debug pod
        await _delete_debug_pod(debug_pod, kubeconfig, task_id)


def _parse_debug_pod_name(output: str) -> str:
    """Extract debug pod name from kubectl debug output.

    Handles formats like:
      - "Creating debugging pod node-debugger-xxx with container debugger on node yyy."
      - "pod/node-name-debug-xxxxx created"
      - "Starting debugging pod node-name-debug-xxxxx..."
    """
    if not output:
        return ""
    # K8s 1.25+ format: "Creating debugging pod node-debugger-xxx ..."
    m = re.search(r'pod\s+(node-debugger-\S+)', output)
    if m:
        return m.group(1).rstrip(".,;:")
    # Match pod name after "pod/" or "pod " in the output
    m = re.search(r'pod[/\s]+(\S+?-debug-\S+)', output)
    if m:
        return m.group(1)
    # Alternative: match any valid pod name followed by "created"
    m = re.search(r'(\S+-debug-\S+)\s+created', output)
    if m:
        return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Debug pod lifecycle helpers (create + wait + exec + delete)
# ---------------------------------------------------------------------------

# Default container name used by `kubectl debug node/<node>`
_DEBUG_CONTAINER_NAME = "debugger"


async def _wait_for_debug_pod_ready(
    pod_name: str, kubeconfig: str, task_id: str, timeout: int = 60,
) -> bool:
    """Wait for debug pod container to be ready before exec.

    kubectl debug returns after creating the Pod object in etcd, NOT after
    the container is running.  This wait bridges the gap.
    Best-effort: returns False on timeout, caller still tries exec.
    """
    # Preferred: kubectl wait --for=condition=Ready
    wait_cmd = [settings.kubectl_path]
    wait_cmd.extend(_build_kubectl_global_args(kubeconfig))
    wait_cmd.extend([
        "wait", "--for=condition=Ready", f"pod/{pod_name}",
        "-n", _TOOL_POD_NAMESPACE, f"--timeout={timeout}s",
    ])
    try:
        result = await run_command(
            wait_cmd, timeout=timeout + 10, task_id=task_id,
        )
        if result.exit_code == 0:
            return True
    except (ToolGuardError, ToolTimeoutError):
        logger.info(
            "kubectl wait blocked/timed out, falling back to polling for %s",
            pod_name,
        )

    # Fallback: poll container ready status
    for _ in range(6):
        await asyncio.sleep(2)
        check_cmd = [settings.kubectl_path]
        check_cmd.extend(_build_kubectl_global_args(kubeconfig))
        check_cmd.extend([
            "get", pod_name, "-n", _TOOL_POD_NAMESPACE,
            "-o", "jsonpath={.status.containerStatuses[0].ready}",
        ])
        try:
            check_result = await run_command(
                check_cmd, timeout=settings.timeout_kubectl, task_id=task_id,
            )
            if check_result.stdout.strip() == "true":
                return True
        except (ToolGuardError, ToolTimeoutError):
            continue

    logger.warning(
        "Debug pod %s not ready after wait, will try exec anyway", pod_name,
    )
    return False


async def _create_and_wait_debug_pod(
    node_name: str, kubeconfig: str, task_id: str,
) -> str | None:
    """Create a debug pod and wait for container readiness.

    Returns pod name or None on failure.
    """
    debug_cmd = [settings.kubectl_path]
    debug_cmd.extend(_build_kubectl_global_args(kubeconfig))
    debug_cmd.extend([
        "debug", f"node/{node_name}", "-n", _TOOL_POD_NAMESPACE,
        "--image=busybox", "--", "sleep", "3600",
    ])
    debug_result = await run_command(
        debug_cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id,
    )
    if debug_result.exit_code != 0:
        logger.warning(
            "Failed to create debug pod for node %s: %s",
            node_name, debug_result.stderr[:200],
        )
        return None

    pod_name = _parse_debug_pod_name(debug_result.stdout)
    if not pod_name:
        logger.warning(
            "Failed to parse debug pod name from: %s",
            debug_result.stdout[:200],
        )
        return None

    # Critical fix: wait for container readiness (prevents race condition)
    await _wait_for_debug_pod_ready(pod_name, kubeconfig, task_id)
    return pod_name


async def _delete_debug_pod(
    pod_name: str, kubeconfig: str, task_id: str,
) -> None:
    """Delete a debug pod. Best-effort, logs warning on failure."""
    del_cmd = [settings.kubectl_path]
    del_cmd.extend(_build_kubectl_global_args(kubeconfig))
    del_cmd.extend([
        "delete", "pod", pod_name, "-n", _TOOL_POD_NAMESPACE,
        "--force", "--grace-period=0",
    ])
    try:
        await run_command(del_cmd, timeout=30, task_id=task_id)
    except Exception:
        logger.warning("Failed to delete debug pod %s", pod_name)


async def _discover_tool_pod_on_node(
    node_name: str, kubeconfig: str, task_id: str,
) -> str | None:
    """Discover a running tool pod on the specified node.

    Returns pod name or None if no running pod found on that node.
    """
    list_cmd = [settings.kubectl_path]
    list_cmd.extend(_build_kubectl_global_args(kubeconfig))
    list_cmd.extend([
        "get", "pods", "-n", _TOOL_POD_NAMESPACE,
        "-l", "app=otel-c-tool", "-o", "wide",
    ])
    try:
        result = await run_command(list_cmd, timeout=settings.timeout_kubectl, task_id=task_id)
        if result.exit_code == 0:
            pods_with_nodes = discover_tool_pods_with_nodes(result.stdout)
            for pod, node in pods_with_nodes:
                if node == node_name:
                    return pod
    except Exception as e:
        logger.warning("Failed to discover tool pods: %s", e)
    return None


async def _exec_in_tool_pod(
    cmd_info: dict, kubeconfig: str, task_id: str,
    tool_pod_name: str,
) -> dict:
    """Execute a baseline command in a tool pod (fallback when debug pod unavailable).

    Unlike debug pod exec, this does NOT add '-c debugger' because tool pods
    have a single container (not an ephemeral debugger container).
    The v_args template is adapted: {debug_pod} is replaced with the tool pod name.
    """
    v_args = cmd_info["v_args"].replace("{debug_pod}", tool_pod_name)

    # Ensure correct namespace
    _has_ns = "-n " in v_args or "--namespace " in v_args
    _has_correct_ns = (
        f"-n {_TOOL_POD_NAMESPACE}" in v_args
        or f"--namespace {_TOOL_POD_NAMESPACE}" in v_args
    )
    if _has_ns and not _has_correct_ns:
        v_args = re.sub(
            r'(-n\s+|--namespace\s+)\S+',
            f'-n {_TOOL_POD_NAMESPACE}', v_args, count=1,
        )
    elif not _has_ns:
        v_args = v_args.replace(tool_pod_name, f"{tool_pod_name} -n {_TOOL_POD_NAMESPACE}", 1)

    exec_cmd = [settings.kubectl_path]
    exec_cmd.extend(_build_kubectl_global_args(kubeconfig))
    exec_cmd.append("exec")
    # NOTE: No '-c debugger' — tool pods have a single main container
    exec_cmd.extend(_split_args(v_args))
    exec_result = await run_command(
        exec_cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id,
    )

    # Two-level iostat fallback (same as debug pod path)
    if exec_result.exit_code != 0:
        fallback_list = _get_iostat_fallback_chain(v_args, exec_result.stderr)
        if fallback_list:
            for fb_v_args in fallback_list:
                logger.info(
                    "iostat unavailable in tool pod, retrying with "
                    "fallback: %s", fb_v_args,
                )
                fb_cmd = [settings.kubectl_path]
                fb_cmd.extend(_build_kubectl_global_args(kubeconfig))
                fb_cmd.append("exec")
                fb_cmd.extend(_split_args(fb_v_args))
                fb_result = await run_command(
                    fb_cmd, timeout=settings.timeout_kubectl_exec,
                    task_id=task_id,
                )
                if fb_result.exit_code == 0:
                    return {
                        "description": cmd_info["description"],
                        "command": " ".join(fb_cmd),
                        "exit_code": 0,
                        "stdout": fb_result.stdout,
                        "stderr": fb_result.stderr,
                    }

    return {
        "description": cmd_info["description"],
        "command": " ".join(exec_cmd),
        "exit_code": exec_result.exit_code,
        "stdout": exec_result.stdout,
        "stderr": exec_result.stderr,
    }


async def _exec_in_debug_pod(
    cmd_info: dict, kubeconfig: str, task_id: str,
    debug_pods: dict[str, str],
) -> dict:
    """Execute a command in an existing debug pod (no create/destroy).

    Used by _execute_observations when reusing a shared debug pod.
    """
    node_name = cmd_info.get("_node_name", "")
    pod_name = debug_pods.get(node_name, "")
    if not pod_name:
        return {
            "description": cmd_info["description"],
            "command": "",
            "exit_code": -1,
            "stdout": "",
            "stderr": f"No debug pod for node {node_name}",
        }

    v_args = cmd_info["v_args"].replace("{debug_pod}", pod_name)

    # Namespace defense: ensure exec targets the same namespace as the debug pod
    _has_ns = "-n " in v_args or "--namespace " in v_args
    _has_correct_ns = (
        f"-n {_TOOL_POD_NAMESPACE}" in v_args
        or f"--namespace {_TOOL_POD_NAMESPACE}" in v_args
    )
    if _has_ns and not _has_correct_ns:
        v_args = re.sub(
            r'(-n\s+|--namespace\s+)\S+',
            f'-n {_TOOL_POD_NAMESPACE}', v_args, count=1,
        )
    elif not _has_ns:
        # No namespace at all — insert after pod name
        v_args = v_args.replace(pod_name, f"{pod_name} -n {_TOOL_POD_NAMESPACE}", 1)

    exec_cmd = [settings.kubectl_path]
    exec_cmd.extend(_build_kubectl_global_args(kubeconfig))
    exec_cmd.append("exec")
    exec_cmd.extend(["-c", _DEBUG_CONTAINER_NAME])
    exec_cmd.extend(_split_args(v_args))
    exec_result = await run_command(
        exec_cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id,
    )

    # Two-level iostat fallback
    if exec_result.exit_code != 0:
        fallback_list = _get_iostat_fallback_chain(v_args, exec_result.stderr)
        if fallback_list:
            for fb_v_args in fallback_list:
                logger.info(
                    "iostat unavailable in debug pod, retrying with "
                    "fallback: %s", fb_v_args,
                )
                fb_cmd = [settings.kubectl_path]
                fb_cmd.extend(_build_kubectl_global_args(kubeconfig))
                fb_cmd.append("exec")
                fb_cmd.extend(["-c", _DEBUG_CONTAINER_NAME])
                fb_cmd.extend(_split_args(fb_v_args))
                fb_result = await run_command(
                    fb_cmd, timeout=settings.timeout_kubectl_exec,
                    task_id=task_id,
                )
                if fb_result.exit_code == 0:
                    return {
                        "description": cmd_info["description"],
                        "command": " ".join(fb_cmd),
                        "exit_code": 0,
                        "stdout": fb_result.stdout,
                        "stderr": fb_result.stderr,
                    }

    return {
        "description": cmd_info["description"],
        "command": " ".join(exec_cmd),
        "exit_code": exec_result.exit_code,
        "stdout": exec_result.stdout,
        "stderr": exec_result.stderr,
    }


# ---------------------------------------------------------------------------
# LLM-driven baseline derivation (primary strategy)
# ---------------------------------------------------------------------------

def _build_scope_specific_examples(scope: str) -> str:
    """Build scope-specific examples for the HumanMessage.

    Node-scope examples show debug_two_step + simple modes.
    Pod/container-scope examples show top + describe patterns.
    """
    if scope == "node":
        return (
            "Examples:\n"
            f'[{{"description": "Node disk IO", "subcommand": "exec", '
            f'"v_args_template": "{{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- iostat -xd 1 3", '
            f'"mode": "debug_two_step"}},\n'
            f'{{"description": "Node conditions", "subcommand": "describe", '
            f'"v_args_template": "node {{node_name}}", '
            f'"mode": "simple"}}]'
        )
    else:  # pod / container scope
        return (
            "Examples:\n"
            '[{"description": "Pod CPU/Memory", "subcommand": "top", '
            '"v_args_template": "pod -n {namespace} {label_selector}", '
            '"mode": "simple"},\n'
            '{"description": "Pod conditions", "subcommand": "describe", '
            '"v_args_template": "pod {pod_name} -n {namespace}", '
            '"mode": "simple"},\n'
            '{"description": "Container disk usage", "subcommand": "exec", '
            '"v_args_template": "{pod_name} -n {namespace} -- df -h", '
            '"mode": "simple"},\n'
            '{"description": "Container disk IO", "subcommand": "exec", '
            '"v_args_template": "{pod_name} -n {namespace} -- iostat -xd 1 3", '
            '"mode": "simple"}]'
        )


async def _llm_derive_baseline_commands(
    llm,
    skill_case_content: str,
    scope: str,
    target: str,
    action: str,
) -> list[BaselineCommand]:
    """Let LLM derive baseline collection commands from full skill content.

    Uses SystemMessage (U-shaped _BASELINE_SYSTEM_PROMPT) for role/rules
    and HumanMessage for task-specific content, aligning with the project's
    [SystemMessage] + messages convention used by all other LLM nodes.

    Falls back to empty list on any failure (triggers Registry fallback).
    """
    if not llm or not skill_case_content:
        return []

    human_prompt = (
        f"Fault type: {scope}-{target}-{action}\n"
        f"Fault scope: {scope}\n\n"
        f"<skill-case>\n{skill_case_content}\n</skill-case>\n\n"
        "Based on the skill-case content above, derive pre-injection baseline "
        "metrics. Focus primarily on baseline_facts and symptoms sections; "
        "injection verification also provides useful hints about expected "
        "changes.\n\n"
        + _build_scope_specific_examples(scope)
    )

    try:
        from langchain_core.messages import SystemMessage as SM, HumanMessage as HM
        response = await llm.ainvoke([
            SM(content=_BASELINE_SYSTEM_PROMPT),
            HM(content=human_prompt),
        ])
        raw = response.content if hasattr(response, "content") else str(response)
        commands = _parse_llm_json_output(raw)
        return _validate_and_filter_commands(commands)
    except Exception as e:
        logger.warning(f"LLM baseline derivation failed: {e}")
        return []


def _parse_llm_json_output(raw: str) -> list[dict]:
    """Robustly parse JSON from LLM output.

    Handles: pure JSON, JSON in markdown code blocks, trailing text.
    """
    if not raw:
        return []

    # Try direct parse
    text = raw.strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(1).strip())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Try finding first [ ... ] block
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return []


def _validate_and_filter_commands(commands: list[dict]) -> list[BaselineCommand]:
    """Validate and filter LLM-generated commands.

    - Only allows whitelisted kubectl subcommands
    - For exec, validates the command after -- is in _ALLOWED_EXEC_COMMANDS
    - Auto-corrects mode to debug_two_step when {debug_pod} is present
    - Smart-converts subcommand "debug" to exec + {debug_pod} + debug_two_step
    - Returns filtered BaselineCommand list
    """
    # Phase 1: basic validation + mode auto-correction
    validated: list[dict] = []
    has_debug_pod_exec = False  # track if any exec {debug_pod} command exists

    for cmd in commands:
        if not isinstance(cmd, dict):
            continue
        subcommand = cmd.get("subcommand", "")
        if subcommand not in _ALLOWED_SUBCOMMANDS:
            logger.warning(f"LLM baseline: rejected subcommand '{subcommand}'")
            continue

        # Extra validation for exec: check command after -- is read-only
        if subcommand == "exec":
            v_args = cmd.get("v_args_template", "")
            if "--" in v_args:
                after_dash = v_args.split("--", 1)[1].strip()
                first_word = after_dash.split()[0] if after_dash else ""
                if first_word not in _ALLOWED_EXEC_COMMANDS:
                    logger.warning(
                        f"LLM baseline: rejected exec command '{first_word}' "
                        f"(not in allowed list)"
                    )
                    continue

        v_args_template = cmd.get("v_args_template", "")
        mode = cmd.get("mode", "simple")

        # Auto-correct mode when {debug_pod} is present
        if "{debug_pod}" in v_args_template and mode != "debug_two_step":
            logger.warning(
                "Auto-correcting mode from '%s' to 'debug_two_step' "
                "for command with {debug_pod}: %s",
                mode, cmd.get("description", ""),
            )
            mode = "debug_two_step"

        if "{debug_pod}" in v_args_template and subcommand == "exec":
            has_debug_pod_exec = True

        validated.append({
            "description": cmd.get("description", ""),
            "subcommand": subcommand,
            "v_args_template": v_args_template,
            "mode": mode,
        })

    # Phase 2: smart-convert subcommand "debug" commands
    result = []
    for cmd in validated:
        subcommand = cmd["subcommand"]

        if subcommand == "debug":
            # Scene A: redundant if exec {debug_pod} already exists
            if has_debug_pod_exec:
                logger.warning(
                    "Dropping redundant 'debug' command (exec {debug_pod} "
                    "already present): %s", cmd["description"],
                )
                continue

            # Scene B: try to extract diagnostic command from v_args
            # Use rsplit to find the LAST -- (the separator between kubectl
            # args and container command), e.g., "node/x --image=busybox -- df -h"
            v_args = cmd["v_args_template"]
            extracted_cmd = ""
            if "--" in v_args:
                after_dash = v_args.rsplit("--", 1)[1].strip()
                first_word = after_dash.split()[0] if after_dash else ""
                if first_word in _ALLOWED_EXEC_COMMANDS:
                    extracted_cmd = after_dash

            if extracted_cmd:
                logger.warning(
                    "Converting 'debug' command to exec + {debug_pod} + "
                    "debug_two_step: %s", cmd["description"],
                )
                result.append(BaselineCommand(
                    description=cmd["description"],
                    subcommand="exec",
                    v_args_template=f"{{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- {extracted_cmd}",
                    mode="debug_two_step",
                ))
            else:
                logger.warning(
                    "Dropping 'debug' command with no extractable diagnostic "
                    "command (Registry/FCAT fallback will provide baseline): %s",
                    cmd["description"],
                )
            continue

        try:
            result.append(BaselineCommand(
                description=cmd["description"],
                subcommand=subcommand,
                v_args_template=cmd["v_args_template"],
                mode=cmd["mode"],
            ))
        except Exception:
            continue

    return result


# ---------------------------------------------------------------------------
# baseline_capture node function
# ---------------------------------------------------------------------------

def make_baseline_capture(llm=None, registry=None):
    """Factory: create baseline_capture node with LLM and SkillRegistry injection."""

    async def baseline_capture(state: AgentState) -> dict:
        task_id = state.get("task_id", "unknown")

        # ── Observability: tracker event ──
        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "baseline_capture",
            "Baseline capture: collecting pre-injection metrics",
            {},
        )

        try:
            # 1. Extract parameters from state.fault_spec — single
            # source of truth. ``read_fault_spec`` returns a typed
            # FaultSpec so we read scope/blade_target/blade_action
            # directly instead of from 3 separate state fields.
            from chaos_agent.agent.fault_spec import read_fault_spec
            spec = read_fault_spec(state)
            scope = spec.scope if spec else ""
            target = spec.blade_target if spec else ""
            action = spec.blade_action if spec else ""
            skill_case = state.get("skill_case_content", "")
            kubeconfig = state.get("kubeconfig", "")

            # 2. Strategy selection with Viability Gate
            # Each strategy is tried in priority order. A strategy's output is
            # only accepted if at least one command is *viable* (executable after
            # template resolution). If 0 viable, we fall through to the next
            # strategy — preventing "has output but can't execute" from blocking
            # the fallback chain.
            commands = []
            source = "none"

            # Strategy chain: (name, factory). Evaluated lazily.
            async def _llm_strategy():
                if not llm or not skill_case:
                    return []
                tracker.update(
                    "Strategy: LLM-driven baseline derivation...",
                    {"step": "strategy", "strategy": "llm"},
                )
                return await _llm_derive_baseline_commands(
                    llm, skill_case, scope, target, action,
                )

            def _registry_strategy():
                return _lookup_baseline_commands(scope, target, action)

            def _scope_fallback_strategy():
                return _SCOPE_FALLBACK.get(scope, [])

            strategy_chain = [
                ("llm", _llm_strategy),
                ("registry", _registry_strategy),
                ("scope_fallback", _scope_fallback_strategy),
            ]

            for strategy_name, strategy_fn in strategy_chain:
                try:
                    strategy_commands = await strategy_fn() \
                        if inspect.iscoroutinefunction(strategy_fn) \
                        else strategy_fn()
                except Exception as e:
                    logger.warning("Strategy '%s' raised exception: %s", strategy_name, e)
                    continue

                if not strategy_commands:
                    continue

                # Viability Gate: check how many commands survive template resolution
                resolved_preview = _resolve_templates(strategy_commands, state)
                viable_count = sum(1 for c in resolved_preview if not c.get("_unresolved"))

                if viable_count > 0:
                    commands = strategy_commands
                    source = strategy_name
                    tracker.update(
                        f"Strategy selected: {strategy_name} "
                        f"({viable_count}/{len(strategy_commands)} viable)",
                        {"step": "strategy", "source": strategy_name,
                         "viable": viable_count, "total": len(strategy_commands)},
                    )
                    break  # first viable strategy wins
                else:
                    logger.warning(
                        "Strategy '%s' produced %d command(s) but 0 viable "
                        "(all unresolved after template resolution), trying next",
                        strategy_name, len(strategy_commands),
                    )
                    tracker.update(
                        f"Strategy {strategy_name}: 0 viable, falling back",
                        {"step": "strategy", "source": strategy_name,
                         "viable": 0, "total": len(strategy_commands)},
                    )

            # P3: FCAT baseline_supplement — enrich with dimensions from knowledge docs
            target_metadata = state.get("target_metadata") or {}
            _p3_added_dims = []
            if target_metadata or (scope and target and action):
                from chaos_agent.utils.fault_context import lookup_adaptations
                supplements = lookup_adaptations(
                    scope, target, action, target_metadata or {},
                    rule_type="baseline_supplement",
                )
                for supp in supplements:
                    dimensions = supp.action.get("dimensions", [])
                    if not dimensions:
                        continue
                    for dim in dimensions:
                        # Map dimension names to scope-aware BaselineCommand entries
                        # (dimension → scope → command — P3 knowledge-driven enrichment)
                        dim_cmds = _FCAT_DIMENSION_COMMANDS.get(dim, {})
                        dim_cmd = dim_cmds.get(scope) or dim_cmds.get("pod")
                        if dim_cmd:
                            # Deduplicate by description
                            if not any(c.description == dim_cmd.description for c in commands):
                                commands.append(dim_cmd)
                                _p3_added_dims.append(dim)
                                logger.info(
                                    "FCAT P3: added baseline command for dimension '%s': %s",
                                    dim, dim_cmd.description,
                                )
                        else:
                            logger.warning(
                                "FCAT P3: no command mapping for dimension '%s', skipping", dim,
                            )
                    # Write P3 session event after processing each supplement
                    if _p3_added_dims:
                        sync_node_status_to_session(state, "baseline_capture",
                            f"P3 baseline supplement: added {', '.join(_p3_added_dims)} dimensions",
                            detail={"dimensions": _p3_added_dims, "rule_id": supp.id})
                        if settings.is_debug and tracker:
                            tracker.update(
                                f"[P3] baseline supplement: added {', '.join(_p3_added_dims)} dimensions"[:200],
                                {"debug": True, "fcat": True},
                            )

            tracker.update(
                f"Strategy selected: {source} ({len(commands)} command(s))",
                {"step": "strategy", "source": source, "command_count": len(commands)},
            )

            # 3. Resolve template variables
            resolved = _resolve_templates(commands, state)

            # 4. Execute collection (best-effort)
            tracker.update(
                f"Executing {len(resolved)} baseline command(s)...",
                {"step": "execute", "command_count": len(resolved)},
            )
            observations = await _execute_observations(resolved, kubeconfig, task_id)

            # 4.5 Run per-command extractors → merge structured fields
            # into target_metadata. This is the "free side benefit" of
            # running baseline anyway — we parse the stdout we already
            # captured into integers downstream nodes can consume,
            # instead of letting them re-issue the same kubectl call.
            # Failure of any extractor is non-fatal: log debug, skip
            # that field, the consumer falls back to its own fetch.
            extracted_metadata: dict = {}
            for cmd_info, obs in zip(resolved, observations):
                if obs.get("exit_code") != 0:
                    continue  # don't try to parse error output
                for extractor in cmd_info.get("_extractors") or []:
                    try:
                        fields = extractor(obs.get("stdout", "") or "", state)
                    except Exception:
                        logger.debug(
                            "baseline extractor %s raised on %s (non-fatal)",
                            getattr(extractor, "__name__", repr(extractor)),
                            cmd_info.get("description", "?"),
                            exc_info=True,
                        )
                        continue
                    # Defensive: contract says extractors return a dict
                    # (possibly empty). A buggy extractor returning the
                    # wrong type (None / list / int) would crash the
                    # .update() below and take baseline_capture down
                    # with it. ``isinstance`` keeps the runner robust
                    # against future extractor authors who break the
                    # contract — logged debug, skipped, downstream
                    # consumer falls back to its own fetch.
                    if not isinstance(fields, dict):
                        logger.debug(
                            "baseline extractor %s returned non-dict %r "
                            "(contract violation, ignored)",
                            getattr(extractor, "__name__", repr(extractor)),
                            type(fields).__name__,
                        )
                        continue
                    if fields:
                        extracted_metadata.update(fields)

            # 5. Assemble baseline_data
            result = {
                "baseline_data": {
                    "captured_at": now_iso(),
                    "source": source,
                    "observations": observations,
                    "success_count": sum(
                        1 for o in observations if o.get("exit_code") == 0
                    ),
                }
            }

            # Merge extracted fields into target_metadata. ``AgentState``
            # has no reducer for this field, so we MUST do the merge
            # here — returning just ``extracted_metadata`` would clobber
            # whatever direct_setup wrote earlier (e.g.
            # ``pod_memory_limit_mb``). Empty-dict short-circuit avoids
            # writing back an unchanged value for the common case.
            if extracted_metadata:
                existing_metadata = state.get("target_metadata") or {}
                merged = {**existing_metadata, **extracted_metadata}
                result["target_metadata"] = merged
                logger.info(
                    "baseline extractors produced: %s",
                    sorted(extracted_metadata.keys()),
                )

            # ── Observability: tracker complete ──
            _success = result["baseline_data"]["success_count"]
            _total = len(observations)
            # Build output previews for detail dict (standard [:200] truncation)
            _obs_previews = []
            for obs in observations:
                _preview = ""
                if obs.get("exit_code") == 0 and obs.get("stdout"):
                    _preview = obs["stdout"][:200]
                elif obs.get("stderr"):
                    _preview = obs["stderr"][:200]
                _obs_previews.append({
                    "description": obs["description"],
                    "exit_code": obs.get("exit_code", -1),
                    "stdout_preview": _preview,
                })
            tracker.complete(
                f"Baseline capture done: {source} strategy, "
                f"{_success}/{_total} commands succeeded",
                detail={
                    "source": source,
                    "success_count": _success,
                    "total_count": _total,
                    "observations": _obs_previews,
                },
            )

            # ── Observability: session status ──
            sync_node_status_to_session(
                state, "baseline_capture",
                f"Baseline collected ({source}): {_success}/{_total} succeeded",
                detail={
                    "source": source,
                    "success_count": _success,
                    "total_count": _total,
                },
            )

            # ── Observability: TaskStore persistence ──
            await sync_to_store(state, result)

            # ── Observability: message history (full content, no truncation) ──
            _store = get_global_session_store()
            _tid = state.get("task_id", "")
            if _store and _tid:
                _session_msgs = [
                    HumanMessage(content=(
                        f"[Baseline Capture] Collected pre-injection metrics "
                        f"({source} strategy, {_success}/{_total} succeeded)"
                    )),
                ]
                for obs in observations:
                    _obs_parts = [
                        f"### {obs['description']}",
                        f"Command: `{obs.get('command', '')}`",
                    ]
                    if obs.get("exit_code") is not None:
                        _obs_parts.append(f"Exit code: {obs['exit_code']}")
                    if obs.get("stdout"):
                        _obs_parts.append(f"```\n{obs['stdout']}\n```")
                    if obs.get("stderr"):
                        _obs_parts.append(f"stderr:\n```\n{obs['stderr']}\n```")
                    _session_msgs.append(HumanMessage(content="\n".join(_obs_parts)))
                _store.append_messages(_tid, _session_msgs)

            return result

        except Exception as e:
            logger.error(f"baseline_capture unexpected error: {e}", exc_info=True)
            # Exception safety: never block injection
            result = {
                "baseline_data": {
                    "captured_at": now_iso(),
                    "source": "error",
                    "observations": [],
                    "success_count": 0,
                }
            }
            tracker.fail(f"Baseline capture failed: {e}")
            sync_node_status_to_session(
                state, "baseline_capture",
                f"Baseline capture failed: {e}",
                detail={"source": "error", "error": str(e)},
            )
            await sync_to_store(state, result)
            return result

    return baseline_capture
