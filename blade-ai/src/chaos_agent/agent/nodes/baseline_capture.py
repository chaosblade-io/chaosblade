"""Baseline capture node: pre-injection metric collection for direct mode.

Collects baseline metrics before fault injection so the verifier can perform
before/after comparison instead of relying solely on absolute thresholds.

Shared across ALL execution modes (direct and NL) — baseline_capture runs
after safety_check/confirmation_gate for every fault injection flow, then
route_after_baseline dispatches to direct_execute or execute_loop.

Strategy priority (matches the actual chain in ``make_baseline_capture``):
  1. LLM-driven (parse full skill_case_content to derive commands)
  2. Python Registry three-level lookup (scope,target,action) -> (scope,target) -> (scope,)
  3. Scope fallback

Each strategy is gated by a *full-viability* check: only a strategy whose
commands are all executable after template resolution short-circuits the
chain. Partially-viable strategies are remembered as a best-effort
fallback that is used if no later strategy produces a complete set.

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
from chaos_agent.agent.node_names import BASELINE_CAPTURE
from chaos_agent.agent.nodes._debug_pod import (
    create_and_wait_debug_pod,
    delete_debug_pod,
    parse_debug_pod_name,
    wait_for_debug_pod_ready,
    DEBUG_CONTAINER_NAME,
)
from chaos_agent.agent.nodes._injection_detection import (
    _TOOL_POD_NAMESPACE,
    discover_tool_pod_on_node,
)
from chaos_agent.agent.nodes._kubeconfig_inject import sync_kubewiz_runtime
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.memory.session_store import get_global_session_store
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.tools.kubectl import _adapt_kubewiz_result, _split_args, build_kubectl_cmd, display_cmd
from chaos_agent.tools.shell import run_command
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


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
    ("pod", "process", "kill"): [
        BaselineCommand("Service endpoints", "get", "endpoints -n {namespace} {label_selector}"),
        BaselineCommand("Pod status/restarts", "get", "pod {pod_name} -n {namespace} -o wide"),
        BaselineCommand("Pod events", "describe", "pod {pod_name} -n {namespace}"),
    ],
    ("pod", "network", "drop"): [
        BaselineCommand("Service endpoints", "get", "endpoints -n {namespace} {label_selector}"),
        BaselineCommand("Pod conditions", "describe", "pod {pod_name} -n {namespace}"),
    ],
    # ── Target-level fallback: (scope, target) ──
    # NOTE: pod-scope injection always carries a precise ``names[0]`` (the
    # ChaosBlade target pod). Use {pod_name} as the primary locator so a
    # caller that only supplied ``names`` (labels={}) still gets a viable
    # ``kubectl top``. Aligns with extract_pod_top_metrics, which itself
    # filters output by ``names[0]`` regardless of how the row was fetched.
    ("pod", "cpu"): [
        BaselineCommand(
            "Pod CPU/Memory", "top", "pod {pod_name} -n {namespace}",
            extractors=[extract_pod_top_metrics],
        ),
        BaselineCommand("Pod conditions/restarts", "describe", "pod {pod_name} -n {namespace}"),
    ],
    ("pod", "mem"): [
        BaselineCommand(
            "Pod CPU/Memory", "top", "pod {pod_name} -n {namespace}",
            extractors=[extract_pod_top_metrics],
        ),
        BaselineCommand("Pod OOM events", "describe", "pod {pod_name} -n {namespace}"),
    ],
    ("pod", "disk"): [
        BaselineCommand("Container disk usage", "exec", "{pod_name} -n {namespace} -- df -h"),
    ],
    ("pod", "network"): [
        BaselineCommand("Service endpoints", "get", "endpoints -n {namespace} {label_selector}"),
        BaselineCommand("Pod conditions", "describe", "pod {pod_name} -n {namespace}"),
    ],
    ("pod", "process"): [
        BaselineCommand("Pod status", "get", "pod {pod_name} -n {namespace}"),
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
    ("node", "process"): [
        BaselineCommand("Node conditions", "describe", "node {node_name}"),
        BaselineCommand("Pods on node", "get",
                        "pods -o wide -A --field-selector spec.nodeName={node_name}"),
    ],
}

# Scope-only fallback (used when no match in BASELINE_COMMANDS at any level)
_SCOPE_FALLBACK: dict[str, list[BaselineCommand]] = {
    "node": [BaselineCommand("Node resource usage", "top", "node {node_name}")],
    "pod": [BaselineCommand("Pod resource usage", "top",
                            "pod {pod_name} -n {namespace}")],
    "deployment": [
        BaselineCommand("Deployment status", "get",
                        "deployment -n {namespace} -o wide"),
        BaselineCommand("Pod status", "get",
                        "pods -n {namespace} {label_selector} -o wide"),
    ],
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
    # ── Primacy zone: role + core principle (WHY + WHAT) ──
    "You are a chaos engineering baseline collection strategist. "
    "Derive the pre-injection baseline for causation attribution.\n\n"

    "# Core Principle\n"
    "Your baseline is the control for causation attribution. "
    "The verifier will compare post-injection state against YOUR baseline "
    "to determine if changes are fault-caused or pre-existing. "
    "If you miss a metric, the verifier CANNOT prove causation for it.\n\n"

    "Reason about what states the fault WILL modify — both quantitative "
    "metrics (CPU, memory, disk, network) and qualitative state "
    "(replica count, pod phase, endpoint list, node condition). "
    "Collect baseline for each affected state, on the EXACT resource "
    "the fault targets. The verifier can only compare the SAME metric "
    "on the SAME resource.\n\n"

    # ── Middle zone: syntax rules (HOW) ──
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

    "5. **Conciseness** — 2-4 commands maximum, covering all states the "
    "fault will modify.\n\n"

    "### Output Schema Details\n"
    "- description: short metric label (e.g., 'Node disk usage', "
    "'Pod CPU/Memory')\n"
    "- subcommand: kubectl subcommand (allowed: get, top, describe, exec, debug)\n"
    "- v_args_template: kubectl arguments with template variables\n"
    "- mode: 'simple' or 'debug_two_step'\n\n"

    "### Template Variables\n"
    "Available: {namespace}, {node_name}, {pod_name}, "
    "{label_selector}, {debug_pod}\n\n"

    # ── Recency zone: WHY + WHAT recap + critical syntax ──
    "### REMINDER\n"
    "1. Reason first: what will the fault modify? → collect baseline for that\n"
    "2. SAME metric on SAME resource — the verifier compares these\n"
    "3. Scope→variables: node={node_name},{debug_pod}; "
    "pod={pod_name},{namespace},{label_selector}\n"
    "4. {debug_pod} → mode MUST be debug_two_step\n"
    "5. Only output the JSON list, no other text\n"
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
    # kubectl 必须用 ``-l key=value`` 形式才能把字符串识别为 label selector；
    # 仅给 ``key=value`` 会被当成裸 pod name 触发 NotFound（参见
    # 历史 task acc1015c 的 ``kubectl top pod -n arms-prom app=node-exporter``
    # 报 ``pod "app=node-exporter" not found``）。
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
    debug_pods: dict[str, tuple[str, str]] = {}  # node_name -> (pod_name, namespace)
    # Tool pod fallback: when debug pod creation fails, discover a tool pod
    tool_pod_fallbacks: dict[str, tuple[str, str]] = {}  # node_name -> (pod_name, namespace)
    debug_two_step_cmds = [c for c in commands if c.get("mode") == "debug_two_step"]

    if debug_two_step_cmds:
        node_names = set(c.get("_node_name", "") for c in debug_two_step_cmds)
        node_names.discard("")
        for node_name in node_names:
            result = await _create_and_wait_debug_pod(
                node_name, kubeconfig, task_id,
            )
            if result:
                debug_pods[node_name] = result
            else:
                # Fallback: try to find a tool pod on this node
                tool_pod_info = await discover_tool_pod_on_node(
                    node_name, kubeconfig, task_id,
                )
                if tool_pod_info:
                    tool_pod_fallbacks[node_name] = tool_pod_info
                    logger.info(
                        "Debug pod unavailable for node %s, using tool pod %s "
                        "in namespace %s as fallback for baseline commands",
                        node_name, tool_pod_info[0], tool_pod_info[1],
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
                        pod_name, pod_ns = tool_pod_fallbacks[node_name]
                        obs = await _exec_in_tool_pod(
                            cmd_info, kubeconfig, task_id,
                            pod_name, pod_ns,
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
                    if _is_observation_success(obs):
                        _status = "ok"
                    elif obs.get("exit_code") == 0:
                        _status = "exit=0(stderr_error)"
                    else:
                        _status = f"exit={obs.get('exit_code')}"
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
        for pod_name, ns in debug_pods.values():
            await _delete_debug_pod(pod_name, kubeconfig, task_id, namespace=ns)

    return observations


async def _exec_simple(cmd_info: dict, kubeconfig: str, task_id: str) -> dict:
    """Execute a simple kubectl command.

    When the command is a kubectl exec containing iostat and it fails
    (because sysstat is not installed in the container), automatically
    retries with BusyBox-compatible iostat first, then /proc fallback.
    """
    cmd = build_kubectl_cmd(cmd_info["subcommand"], cmd_info["v_args"], kubeconfig=kubeconfig)
    timeout = (
        settings.timeout_kubectl_exec
        if cmd_info["subcommand"] == "exec"
        else settings.timeout_kubectl
    )
    result = await run_command(cmd, timeout=timeout, task_id=task_id)
    result = _adapt_kubewiz_result(result)

    # Two-level iostat fallback
    if result.exit_code != 0 and cmd_info["subcommand"] == "exec":
        fallback_list = _get_iostat_fallback_chain(cmd_info["v_args"], result.stderr)
        if fallback_list:
            for fb_v_args in fallback_list:
                logger.info(
                    "iostat unavailable in container, retrying with "
                    "fallback: %s", fb_v_args,
                )
                fb_cmd = build_kubectl_cmd("exec", fb_v_args, kubeconfig=kubeconfig)
                fb_result = await run_command(
                    fb_cmd, timeout=settings.timeout_kubectl_exec,
                    task_id=task_id,
                )
                fb_result = _adapt_kubewiz_result(fb_result)
                if fb_result.exit_code == 0:
                    return {
                        "description": cmd_info["description"],
                        "command": display_cmd(fb_cmd),
                        "exit_code": 0,
                        "stdout": fb_result.stdout,
                        "stderr": fb_result.stderr,
                    }

    return {
        "description": cmd_info["description"],
        "command": display_cmd(cmd),
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
    # Auto-discover namespace via create_and_wait_debug_pod
    create_result = await _create_and_wait_debug_pod(node_name, kubeconfig, task_id)
    if not create_result:
        return {
            "description": cmd_info["description"],
            "command": "",
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Failed to create debug pod for node {node_name}",
        }

    debug_pod, pod_ns = create_result

    try:
        # Step 2: kubectl exec {debug_pod} -n {namespace} -c debugger -- {cmd}
        v_args = cmd_info["v_args"].replace("{debug_pod}", debug_pod)

        # Namespace defense: ensure exec targets the same namespace as the debug pod
        _has_ns = "-n " in v_args or "--namespace " in v_args
        _has_correct_ns = (
            f"-n {pod_ns}" in v_args
            or f"--namespace {pod_ns}" in v_args
        )
        if _has_ns and not _has_correct_ns:
            logger.warning(
                "Overriding namespace in debug_two_step exec to '%s'",
                pod_ns,
            )
            v_args = re.sub(
                r'(-n\s+|--namespace\s+)\S+',
                f'-n {pod_ns}', v_args, count=1,
            )
        elif not _has_ns:
            # No namespace at all — insert after debug pod name
            v_args = v_args.replace(debug_pod, f"{debug_pod} -n {pod_ns}", 1)

        exec_cmd = build_kubectl_cmd("exec", ["-c", _DEBUG_CONTAINER_NAME] + _split_args(v_args), kubeconfig=kubeconfig)
        exec_result = await run_command(
            exec_cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id,
        )
        exec_result = _adapt_kubewiz_result(exec_result)

        # Two-level iostat fallback
        if exec_result.exit_code != 0:
            fallback_list = _get_iostat_fallback_chain(v_args, exec_result.stderr)
            if fallback_list:
                for fb_v_args in fallback_list:
                    logger.info(
                        "iostat unavailable in debug pod, retrying with "
                        "fallback: %s", fb_v_args,
                    )
                    fb_cmd = build_kubectl_cmd("exec", ["-c", _DEBUG_CONTAINER_NAME] + _split_args(fb_v_args), kubeconfig=kubeconfig)
                    fb_result = await run_command(
                        fb_cmd, timeout=settings.timeout_kubectl_exec,
                        task_id=task_id,
                    )
                    fb_result = _adapt_kubewiz_result(fb_result)
                    if fb_result.exit_code == 0:
                        return {
                            "description": cmd_info["description"],
                            "command": display_cmd(fb_cmd),
                            "exit_code": 0,
                            "stdout": fb_result.stdout,
                            "stderr": fb_result.stderr,
                        }

        return {
            "description": cmd_info["description"],
            "command": display_cmd(exec_cmd),
            "exit_code": exec_result.exit_code,
            "stdout": exec_result.stdout,
            "stderr": exec_result.stderr,
        }
    finally:
        # Step 3: cleanup debug pod
        await _delete_debug_pod(debug_pod, kubeconfig, task_id, namespace=pod_ns)


def _parse_debug_pod_name(output: str) -> str:
    """Backward-compat wrapper — delegates to shared _debug_pod module."""
    return parse_debug_pod_name(output)


# ---------------------------------------------------------------------------
# Debug pod lifecycle helpers (create + wait + exec + delete)
# ---------------------------------------------------------------------------

# Default container name — backward-compat alias for shared module constant
_DEBUG_CONTAINER_NAME = DEBUG_CONTAINER_NAME


async def _wait_for_debug_pod_ready(
    pod_name: str, kubeconfig: str, task_id: str, timeout: int = 60,
) -> bool:
    """Backward-compat wrapper — delegates to shared _debug_pod module."""
    return await wait_for_debug_pod_ready(pod_name, kubeconfig, task_id, timeout)


async def _create_and_wait_debug_pod(
    node_name: str, kubeconfig: str, task_id: str,
) -> tuple[str, str] | None:
    """Backward-compat wrapper — delegates to shared _debug_pod module."""
    return await create_and_wait_debug_pod(node_name, kubeconfig, task_id)


async def _delete_debug_pod(
    pod_name: str, kubeconfig: str, task_id: str,
    namespace: str = "",
) -> None:
    """Backward-compat wrapper — delegates to shared _debug_pod module."""
    await delete_debug_pod(pod_name, kubeconfig, task_id, namespace=namespace)


async def _exec_in_tool_pod(
    cmd_info: dict, kubeconfig: str, task_id: str,
    tool_pod_name: str, tool_pod_namespace: str,
) -> dict:
    """Execute a baseline command in a tool pod (fallback when debug pod unavailable).

    Unlike debug pod exec, this does NOT add '-c debugger' because tool pods
    have a single container (not an ephemeral debugger container).
    The v_args template is adapted: {debug_pod} is replaced with the tool pod name.
    The namespace is taken from the discovered tool pod, not hardcoded —
    real clusters often deploy chaosblade-tool in 'default' rather than 'chaosblade'.
    """
    v_args = cmd_info["v_args"].replace("{debug_pod}", tool_pod_name)

    # Ensure correct namespace (use the actual namespace where tool pod lives)
    _has_ns = "-n " in v_args or "--namespace " in v_args
    _has_correct_ns = (
        f"-n {tool_pod_namespace}" in v_args
        or f"--namespace {tool_pod_namespace}" in v_args
    )
    if _has_ns and not _has_correct_ns:
        v_args = re.sub(
            r'(-n\s+|--namespace\s+)\S+',
            f'-n {tool_pod_namespace}', v_args, count=1,
        )
    elif not _has_ns:
        v_args = v_args.replace(
            tool_pod_name, f"{tool_pod_name} -n {tool_pod_namespace}", 1,
        )

    # NOTE: No '-c debugger' — tool pods have a single main container
    exec_cmd = build_kubectl_cmd("exec", v_args, kubeconfig=kubeconfig)
    exec_result = await run_command(
        exec_cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id,
    )
    exec_result = _adapt_kubewiz_result(exec_result)

    # Two-level iostat fallback (same as debug pod path)
    if exec_result.exit_code != 0:
        fallback_list = _get_iostat_fallback_chain(v_args, exec_result.stderr)
        if fallback_list:
            for fb_v_args in fallback_list:
                logger.info(
                    "iostat unavailable in tool pod, retrying with "
                    "fallback: %s", fb_v_args,
                )
                fb_cmd = build_kubectl_cmd("exec", fb_v_args, kubeconfig=kubeconfig)
                fb_result = await run_command(
                    fb_cmd, timeout=settings.timeout_kubectl_exec,
                    task_id=task_id,
                )
                fb_result = _adapt_kubewiz_result(fb_result)
                if fb_result.exit_code == 0:
                    return {
                        "description": cmd_info["description"],
                        "command": display_cmd(fb_cmd),
                        "exit_code": 0,
                        "stdout": fb_result.stdout,
                        "stderr": fb_result.stderr,
                    }

    return {
        "description": cmd_info["description"],
        "command": display_cmd(exec_cmd),
        "exit_code": exec_result.exit_code,
        "stdout": exec_result.stdout,
        "stderr": exec_result.stderr,
    }


async def _exec_in_debug_pod(
    cmd_info: dict, kubeconfig: str, task_id: str,
    debug_pods: dict[str, tuple[str, str]],
) -> dict:
    """Execute a command in an existing debug pod (no create/destroy).

    Used by _execute_observations when reusing a shared debug pod.
    """
    node_name = cmd_info.get("_node_name", "")
    pod_info = debug_pods.get(node_name)
    if not pod_info:
        return {
            "description": cmd_info["description"],
            "command": "",
            "exit_code": -1,
            "stdout": "",
            "stderr": f"No debug pod for node {node_name}",
        }

    pod_name, pod_ns = pod_info
    v_args = cmd_info["v_args"].replace("{debug_pod}", pod_name)

    # Namespace defense: ensure exec targets the same namespace as the debug pod
    _has_ns = "-n " in v_args or "--namespace " in v_args
    _has_correct_ns = (
        f"-n {pod_ns}" in v_args
        or f"--namespace {pod_ns}" in v_args
    )
    if _has_ns and not _has_correct_ns:
        v_args = re.sub(
            r'(-n\s+|--namespace\s+)\S+',
            f'-n {pod_ns}', v_args, count=1,
        )
    elif not _has_ns:
        # No namespace at all — insert after pod name
        v_args = v_args.replace(pod_name, f"{pod_name} -n {pod_ns}", 1)

    exec_cmd = build_kubectl_cmd("exec", ["-c", _DEBUG_CONTAINER_NAME] + _split_args(v_args), kubeconfig=kubeconfig)
    exec_result = await run_command(
        exec_cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id,
    )
    exec_result = _adapt_kubewiz_result(exec_result)

    # Two-level iostat fallback
    if exec_result.exit_code != 0:
        fallback_list = _get_iostat_fallback_chain(v_args, exec_result.stderr)
        if fallback_list:
            for fb_v_args in fallback_list:
                logger.info(
                    "iostat unavailable in debug pod, retrying with "
                    "fallback: %s", fb_v_args,
                )
                fb_cmd = build_kubectl_cmd("exec", ["-c", _DEBUG_CONTAINER_NAME] + _split_args(fb_v_args), kubeconfig=kubeconfig)
                fb_result = await run_command(
                    fb_cmd, timeout=settings.timeout_kubectl_exec,
                    task_id=task_id,
                )
                fb_result = _adapt_kubewiz_result(fb_result)
                if fb_result.exit_code == 0:
                    return {
                        "description": cmd_info["description"],
                        "command": display_cmd(fb_cmd),
                        "exit_code": 0,
                        "stdout": fb_result.stdout,
                        "stderr": fb_result.stderr,
                    }

    return {
        "description": cmd_info["description"],
        "command": display_cmd(exec_cmd),
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
    *,
    namespace: str = "",
    names: tuple[str, ...] = (),
    labels: dict[str, str] | None = None,
) -> list[BaselineCommand]:
    """Let LLM derive baseline collection commands from full skill content.

    Uses SystemMessage (U-shaped _BASELINE_SYSTEM_PROMPT) for role/rules
    and HumanMessage for task-specific content, aligning with the project's
    [SystemMessage] + messages convention used by all other LLM nodes.

    Falls back to empty list on any failure (triggers Registry fallback).
    """
    if not llm or not skill_case_content:
        return []

    # Build target context so the LLM uses correct resource names/labels
    target_lines = [f"Fault type: {scope}-{target}-{action}", f"Fault scope: {scope}"]
    if namespace:
        target_lines.append(f"Namespace: {namespace}")
    if names:
        target_lines.append(f"Resource names: {', '.join(names[:5])}")
    if labels:
        label_str = ", ".join(f"{k}={v}" for k, v in labels.items())
        target_lines.append(f"Label selector: {label_str}")

    # Show template variable → resolved value mapping so the LLM
    # uses template variables instead of hardcoding values.
    # 注意：``{label_selector}`` 经 ``_resolve_templates`` 渲染后**已含
    # ``-l `` 前缀**（例如 ``-l app=foo``），LLM 生成的 v_args_template
    # 必须直接使用 ``{label_selector}``，禁止再叠 ``-l``，否则会拼出
    # ``-l -l app=foo`` 让 kubectl 报错。
    pod_name = names[0] if names else ""
    label_selector_val = (
        "-l " + ",".join(f"{k}={v}" for k, v in labels.items())
        if labels else ""
    )
    var_lines = ["\nTemplate variable values (use these in v_args_template):"]
    if namespace:
        var_lines.append(f"  {{namespace}} → {namespace}")
    if pod_name and scope != "node":
        var_lines.append(f"  {{pod_name}} → {pod_name}")
    if pod_name and scope == "node":
        var_lines.append(f"  {{node_name}} → {pod_name}")
    if label_selector_val:
        var_lines.append(
            f"  {{label_selector}} → {label_selector_val} "
            f"(already includes -l prefix — use as-is, "
            f"do NOT prepend another -l)"
        )
    target_lines.extend(var_lines)

    target_context = "\n".join(target_lines)

    human_prompt = (
        f"{target_context}\n\n"
        f"<skill-case>\n{skill_case_content}\n</skill-case>\n\n"
        "Based on the skill-case content, reason about what states this fault "
        "will modify. The baseline_facts and symptoms sections describe expected "
        "changes; injection verification provides additional hints. "
        "Generate commands to capture pre-injection baseline for each affected "
        "state.\n\n"
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


_LLM_BASELINE_MAX_RETRIES = 2


async def _llm_retry_failed_commands(
    llm,
    skill_case_content: str,
    scope: str,
    target: str,
    action: str,
    failed_observations: list[dict],
    *,
    namespace: str = "",
    names: tuple[str, ...] = (),
    labels: dict[str, str] | None = None,
) -> list[BaselineCommand]:
    """Re-derive baseline commands with execution error feedback.

    Called when LLM-generated commands fail at runtime (e.g. bad flags,
    wrong resource type). Feeds the error output back to the LLM so it
    can self-correct.
    """
    if not llm or not failed_observations:
        return []

    error_lines = []
    for obs in failed_observations:
        stderr_preview = (obs.get("stderr") or "")[:300]
        error_lines.append(
            f"- Command: `{obs.get('command', '')}`\n"
            f"  exit_code={obs.get('exit_code')}\n"
            f"  stderr: {stderr_preview}"
        )
    error_feedback = "\n".join(error_lines)

    target_lines = [f"Fault type: {scope}-{target}-{action}", f"Fault scope: {scope}"]
    if namespace:
        target_lines.append(f"Namespace: {namespace}")
    if names:
        target_lines.append(f"Resource names: {', '.join(names[:5])}")
    if labels:
        label_str = ", ".join(f"{k}={v}" for k, v in labels.items())
        target_lines.append(f"Label selector: {label_str}")

    pod_name = names[0] if names else ""
    label_selector_val = (
        ",".join(f"{k}={v}" for k, v in labels.items()) if labels else ""
    )
    var_lines = ["\nTemplate variable values (use these in v_args_template):"]
    if namespace:
        var_lines.append(f"  {{namespace}} → {namespace}")
    if pod_name and scope != "node":
        var_lines.append(f"  {{pod_name}} → {pod_name}")
    if pod_name and scope == "node":
        var_lines.append(f"  {{node_name}} → {pod_name}")
    if label_selector_val:
        var_lines.append(
            f"  {{label_selector}} → {label_selector_val} "
            f"(raw value — always use with -l flag, e.g. -l {{label_selector}})"
        )
    target_lines.extend(var_lines)

    target_context = "\n".join(target_lines)

    human_prompt = (
        f"{target_context}\n\n"
        f"<skill-case>\n{skill_case_content}\n</skill-case>\n\n"
        "The following baseline commands FAILED during execution:\n\n"
        f"{error_feedback}\n\n"
        "Analyze the errors and generate CORRECTED replacement commands. "
        "Output ONLY the corrected commands as a JSON list.\n\n"
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
        logger.warning("LLM baseline retry failed: %s", e)
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
            sync_kubewiz_runtime(state)

            # 2. Strategy selection with Viability Gate
            # Each strategy is tried in priority order (llm → registry →
            # scope_fallback). A strategy's output is accepted only if it
            # is *fully viable* (every command is executable after template
            # resolution). A *partially viable* strategy (some commands
            # unresolved) is remembered as a fallback but does NOT
            # short-circuit the chain — we keep trying later strategies
            # in case one of them produces a complete set. If no strategy
            # is fully viable, the first partial result we saw is used as
            # a best-effort fallback.
            #
            # Rationale (task 23ee60d retro): the previous "any-viable wins"
            # rule let a half-broken strategy (e.g. label_selector unresolved
            # because labels={}, pod_name fine) lock in as the source, which
            # silently dropped the ``kubectl top`` baseline and left the
            # verifier without an authoritative pre-injection memory value.
            # The full-viability gate ensures fallback strategies still get
            # a chance when the first hit is incomplete.
            commands = []
            source = "none"
            partial_commands: list = []
            partial_source = ""
            partial_viable = 0
            partial_total = 0

            # Strategy chain: (name, factory). Evaluated lazily.
            async def _llm_strategy():
                if not llm or not skill_case:
                    return []
                tracker.update(
                    "Strategy: LLM-driven baseline derivation...",
                    {"step": "strategy", "strategy": "llm"},
                )
                try:
                    return await asyncio.wait_for(
                        _llm_derive_baseline_commands(
                            llm, skill_case, scope, target, action,
                            namespace=spec.namespace if spec else "",
                            names=spec.names if spec else (),
                            labels=dict(spec.labels) if spec and spec.labels else None,
                        ),
                        timeout=settings.timeout_baseline_llm,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "LLM baseline derivation timed out after %ds, "
                        "falling back to registry",
                        settings.timeout_baseline_llm,
                    )
                    return []

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

                # Viability Gate: how many commands survive template resolution
                resolved_preview = _resolve_templates(strategy_commands, state)
                viable_count = sum(1 for c in resolved_preview if not c.get("_unresolved"))
                total_count = len(strategy_commands)

                if viable_count == 0:
                    logger.warning(
                        "Strategy '%s' produced %d command(s) but 0 viable "
                        "(all unresolved after template resolution), trying next",
                        strategy_name, total_count,
                    )
                    tracker.update(
                        f"Strategy {strategy_name}: 0 viable, falling back",
                        {"step": "strategy", "source": strategy_name,
                         "viable": 0, "total": total_count},
                    )
                    continue

                if viable_count == total_count:
                    # Fully viable — lock in this strategy.
                    commands = strategy_commands
                    source = strategy_name
                    tracker.update(
                        f"Strategy selected: {strategy_name} "
                        f"({viable_count}/{total_count} viable, complete)",
                        {"step": "strategy", "source": strategy_name,
                         "viable": viable_count, "total": total_count},
                    )
                    break

                # Partial: keep first partial as fallback, but continue trying
                # later strategies (e.g. LLM) for a complete set.
                if not partial_commands:
                    partial_commands = strategy_commands
                    partial_source = strategy_name
                    partial_viable = viable_count
                    partial_total = total_count
                logger.info(
                    "Strategy '%s' is partial (%d/%d viable), retained as "
                    "fallback; continuing strategy chain",
                    strategy_name, viable_count, total_count,
                )
                tracker.update(
                    f"Strategy {strategy_name}: partial "
                    f"({viable_count}/{total_count}), keep trying",
                    {"step": "strategy", "source": strategy_name,
                     "viable": viable_count, "total": total_count,
                     "partial": True},
                )

            # No fully-viable strategy — fall back to the first partial we saw.
            if not commands and partial_commands:
                commands = partial_commands
                source = partial_source
                logger.warning(
                    "No fully-viable baseline strategy; using partial '%s' "
                    "(%d/%d viable) as best-effort fallback",
                    partial_source, partial_viable, partial_total,
                )
                tracker.update(
                    f"Strategy selected: {partial_source} (partial fallback, "
                    f"{partial_viable}/{partial_total} viable)",
                    {"step": "strategy", "source": partial_source,
                     "viable": partial_viable, "total": partial_total,
                     "partial_fallback": True},
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
                        sync_node_status_to_session(state, BASELINE_CAPTURE,
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

            # 4.0.5 LLM self-correcting retry: when LLM-generated commands
            # fail execution, feed errors back to the LLM and let it
            # self-correct (up to _LLM_BASELINE_MAX_RETRIES attempts).
            # Runs BEFORE the strategy-level fallback (4.0.7) so that the
            # LLM is given a chance to fix itself before we abandon the
            # primary strategy and reach for registry / scope_fallback.
            if source == "llm" and llm:
                all_pairs = list(zip(resolved, observations))

                for retry_num in range(1, _LLM_BASELINE_MAX_RETRIES + 1):
                    failed_obs = [o for _, o in all_pairs
                                  if o.get("exit_code") != 0]
                    if not failed_obs:
                        break

                    logger.info(
                        "LLM baseline retry %d/%d: %d command(s) failed",
                        retry_num, _LLM_BASELINE_MAX_RETRIES, len(failed_obs),
                    )
                    tracker.update(
                        f"LLM retry {retry_num}/{_LLM_BASELINE_MAX_RETRIES}: "
                        f"{len(failed_obs)} command(s) failed, "
                        f"regenerating with error feedback...",
                        {"step": "llm_retry", "attempt": retry_num,
                         "failed_count": len(failed_obs)},
                    )

                    try:
                        retry_commands = await asyncio.wait_for(
                            _llm_retry_failed_commands(
                                llm, skill_case, scope, target, action,
                                failed_obs,
                                namespace=spec.namespace if spec else "",
                                names=spec.names if spec else (),
                                labels=dict(spec.labels) if spec and spec.labels else None,
                            ),
                            timeout=settings.timeout_baseline_llm,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "LLM baseline retry %d timed out after %ds",
                            retry_num, settings.timeout_baseline_llm,
                        )
                        break
                    if not retry_commands:
                        logger.info(
                            "LLM retry %d: no corrected commands returned",
                            retry_num,
                        )
                        break

                    retry_resolved = _resolve_templates(retry_commands, state)
                    retry_viable = [
                        c for c in retry_resolved if not c.get("_unresolved")
                    ]
                    if not retry_viable:
                        break

                    retry_obs = await _execute_observations(
                        retry_resolved, kubeconfig, task_id,
                    )

                    # Keep original successes, replace failures with retry.
                    # Use _is_observation_success so kubectl partial failures
                    # (exit_code=0 + 'Error from server' in stdout) are treated
                    # as failures and properly retried.
                    success_pairs = [
                        (r, o) for r, o in all_pairs
                        if _is_observation_success(o)
                    ]
                    all_pairs = success_pairs + list(
                        zip(retry_resolved, retry_obs)
                    )

                if all_pairs:
                    resolved, observations = (
                        [r for r, _ in all_pairs],
                        [o for _, o in all_pairs],
                    )
                else:
                    resolved, observations = [], []

            # 4.0.7 Execution-level strategy fallback：
            # 当前 strategy 的 Viability Gate 仅校验 "模板 placeholder 是否填得上"，
            # 不保证命令真的能跑通（典型反例：模板拼接缺 ``-l`` 前缀时
            # ``label_selector`` 字符串非空 → viable_count > 0 → 锁定该策略 →
            # 执行全部失败 → 没机会回落到下一级策略）。
            #
            # 设计意图（LLM 优先链：llm → registry → scope_fallback）：
            #   首选策略命中且至少 1 条跑通 → 沿用首选
            #   首选策略命中但全部跑挂      → 自动回落到链中其他未尝试的策略
            #   首选策略完全没给           → 直接走下一级（已由 viable gate 处理）
            #
            # 注意：source == "llm" 时同样会进入此段，因为 4.0.5 已经给过
            # LLM 最多 3 次 self-correcting retry，retry 仍救不回来才会走
            # 到这里。``_attempted = {source}`` 保证 LLM 不会被再调一次，
            # 也就杜绝了"LLM 已经退化失败 → 再调 LLM"的死循环风险。
            if (
                observations
                and not any(_is_observation_success(o) for o in observations)
            ):
                _attempted = {source}
                for _fb_name, _fb_fn in strategy_chain:
                    if _fb_name in _attempted:
                        continue
                    try:
                        _fb_commands = await _fb_fn() \
                            if inspect.iscoroutinefunction(_fb_fn) \
                            else _fb_fn()
                    except Exception as e:
                        logger.warning(
                            "Fallback strategy '%s' raised exception: %s",
                            _fb_name, e,
                        )
                        _attempted.add(_fb_name)
                        continue
                    if not _fb_commands:
                        _attempted.add(_fb_name)
                        continue
                    _fb_resolved_preview = _resolve_templates(_fb_commands, state)
                    _fb_viable = sum(
                        1 for c in _fb_resolved_preview
                        if not c.get("_unresolved")
                    )
                    if _fb_viable == 0:
                        _attempted.add(_fb_name)
                        continue

                    logger.warning(
                        "Strategy '%s' executed 0/%d succeeded, "
                        "falling through to '%s' (%d/%d viable)",
                        source, len(observations),
                        _fb_name, _fb_viable, len(_fb_commands),
                    )
                    tracker.update(
                        f"Strategy {source}: 0/{len(observations)} succeeded, "
                        f"falling through to {_fb_name}",
                        {"step": "strategy_fallback",
                         "from": source, "to": _fb_name,
                         "from_total": len(observations)},
                    )

                    commands = list(_fb_commands)
                    source = _fb_name
                    resolved = _resolve_templates(commands, state)
                    observations = await _execute_observations(
                        resolved, kubeconfig, task_id,
                    )
                    _attempted.add(_fb_name)

                    if any(_is_observation_success(o) for o in observations):
                        break
                    # 否则继续遍历下一个 strategy

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
                        1 for o in observations if _is_observation_success(o)
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
                state, BASELINE_CAPTURE,
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
                _store.append_messages(_tid, _session_msgs, node_name=BASELINE_CAPTURE)

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
                state, BASELINE_CAPTURE,
                f"Baseline capture failed: {e}",
                detail={"source": "error", "error": str(e)},
            )
            await sync_to_store(state, result)
            return result

    return baseline_capture
