"""Hints domain code for the verifier node.

Extracted from verifier.py — contains observability hints, parameter-dependent
hint generators, baseline metric extraction, fault verification hint assembly,
and tool pod discovery for Layer 2 verification.

Symbols moved to _verifier_shared.py (imported here):
- _IMAGEFS_PATHS, _NODEFS_PATHS (used by _derive_disk_fill_partition)
- _get_node_disk_topology_hints (used by _get_fault_verification_hints)
"""

import logging
import typing

from chaos_agent.agent.nodes._injection_detection import (
    discover_tool_pods,
    discover_tool_pods_with_nodes,
    _TOOL_POD_NAMESPACE,
    _TOOL_POD_LABEL_SELECTOR,
)
from chaos_agent.agent.nodes._verifier_shared import (
    _IMAGEFS_PATHS,
    _NODEFS_PATHS,
    _get_node_disk_topology_hints,
)
from chaos_agent.agent.state import AgentState

logger = logging.getLogger(__name__)


# Parameter observability hints: warn LLM when parameters may be too small
# to produce observable effects.  Keyed by (blade_target, blade_action).
_PARAM_OBSERVABILITY_HINTS: dict[tuple[str, str], str] = {
    ("disk", "fill"): (
        "Disk fill verification: the 'size' parameter must be large enough to produce "
        "observable effects. A small fill (e.g., 100MB on a 100GB disk, ~0.1%) will NOT "
        "trigger DiskPressure (>85%) or show visible df -h percentage change. "
        "For observable verification, prefer using 'percent' parameter (e.g., percent=85) "
        "or a size large enough to push usage past 85%."
    ),
    ("disk", "burn"): (
        "Disk burn verification: pod-disk-burn creates TEMPORARY files that are "
        "automatically deleted when the experiment completes. The fault effect is "
        "TRANSIENT by design. Verification strategy:\n"
        "1. Compare df -h output against baseline — a usage increase (1-2GB+) is "
        "indirect evidence the burn occurred, even if files are now cleaned up.\n"
        "2. If experiment state is 'Success' (experiment completed, files cleaned up) "
        "but df -h shows significant increase from baseline → use "
        "'recovered_before_observation', NOT 'failed'.\n"
        "3. Check for burn file remnants: `ls -lah <path>/` in target pod.\n"
        "4. For direct mode: the agent auto-boosts burn --size to widen "
        "the observable effect window — even if files are cleaned up, "
        "the larger I/O volume leaves stronger residual evidence in df -h.\n"
        "5. ⚠ OOM KILL IS A SIDE EFFECT: burn I/O may trigger OOMKill via page cache "
        "exhaustion on memory-constrained pods. If the pod restarted during or after "
        "injection, the restart will wipe all burn evidence. Classify as "
        "'recovered_before_observation'. The OOMKill is CONSISTENT WITH the fault "
        "but does NOT confirm it — pre-existing OOMKill history (check baseline) "
        "is a confounding factor. Set PrimaryEvidenceObserved: false."
    ),
    ("network", "dns"): (
        "DNS fault mechanism: ChaosBlade pod-network dns modifies /etc/hosts (adds "
        "'<forged-ip> <domain> #chaosblade' entry), NOT the DNS server. "
        "Verification MUST use tools that respect /etc/hosts:\n"
        "1. `cat /etc/hosts` — direct evidence of injection (look for #chaosblade entry)\n"
        "2. `ping -c 1 <domain>` — shows resolved IP from /etc/hosts\n"
        "3. `wget/curl <domain>` — application-level DNS resolution\n"
        "DO NOT use `nslookup` or `dig` — they bypass /etc/hosts and query DNS directly, "
        "so they CANNOT detect this fault type. If the target application does NOT use "
        "the hijacked domain, mark application impact verification as 'skipped' with a note "
        "recommending the user choose a domain the app actually depends on."
    ),
}


# ---------------------------------------------------------------------------
# Parameter-dependent hint generators: produce verification guidance based
# on the actual values of blade command flags (not just the fault type).
# Each generator receives parsed_flags dict and returns a hint string or None.
# ---------------------------------------------------------------------------


def _derive_disk_fill_partition(parsed_flags: dict) -> str | None:
    """Derive LIKELY target partition type from --path value.

    Returns 'imagefs', 'nodefs', or None. This is a HEURISTIC based on
    common node configurations — the actual partition depends on the node's
    mount layout and can only be confirmed by running `df -h` on the node.
    """
    path_val = parsed_flags.get("path", "").rstrip("/")
    if not path_val:
        return None
    if path_val in _IMAGEFS_PATHS or any(
        path_val.startswith(p.rstrip("/") + "/") for p in _IMAGEFS_PATHS
    ):
        return "imagefs"
    if path_val in _NODEFS_PATHS or any(
        path_val.startswith(p.rstrip("/") + "/") for p in _NODEFS_PATHS
    ):
        return "nodefs"
    return None


def _extract_baseline_key_metrics(
    baseline: dict,
    blade_target: str,
    blade_action: str,
) -> dict[str, str]:
    """Extract structured key metrics from baseline observations.

    Thin wrapper around ``_metric_extractor.extract_baseline_metrics``
    (E2). The actual parsing lives in the shared extractor so Layer 2
    verification can reuse the same per-format parsers on
    post-injection kubectl output, not just baseline. Returns the
    fault-filtered dict the existing Layer 2 prompt builder expects.
    """
    from chaos_agent.agent.nodes._metric_extractor import extract_baseline_metrics
    return extract_baseline_metrics(baseline, blade_target, blade_action)


_COMMAND_PRIORITY_HINT = (
    "- COMMAND PRIORITY: Your FIRST disk check MUST be `df -h` (bare, no path argument) to "
    "identify ALL partitions and their usage. Do NOT run `df -h /host` or `df -h /host/<path>` "
    "as your first command — these may show only one partition and give you an incomplete "
    "baseline. After `df -h` (bare) reveals all partitions, you know which one to monitor "
    "for changes.\n"
)


def _disk_fill_param_hints(parsed_flags: dict) -> str | None:
    """Generate partition-aware verification hints for node-disk fill based on --path value."""
    partition_type = _derive_disk_fill_partition(parsed_flags)
    if partition_type is None:
        path_val = parsed_flags.get("path", "").rstrip("/")
        if not path_val:
            return None
        # Unknown path: provide generic guidance to check all partitions
        return (
            f"⚠ PARTITION DERIVATION: --path {parsed_flags.get('path', '')} — unable to determine "
            f"target partition automatically. This path may be on imagefs (container overlay) or "
            f"nodefs (root filesystem) depending on the mount configuration.\n"
            f"{_COMMAND_PRIORITY_HINT}"
            f"- YOU MUST use `df -h` (bare, no path) to list ALL mounted filesystems and identify "
            f"which partition shows increased usage.\n"
            f"- Do NOT assume the fill target without checking — verify which partition changed.\n"
            f"- BASELINE INTEGRITY: Record which partition shows increased usage and use THAT SAME partition "
            f"for all before/after comparisons. Do not compare different partitions' percentages.\n"
        )

    if partition_type == "imagefs":
        partition_desc = "container overlay filesystem (typically backed by a separate disk like /dev/vdb)"
        verify_cmd = "df -h (bare, no path argument) → find the overlay/imagefs partition with increased usage"
        false_negative = "df -h /host or df -h /host/<path> → shows nodefs ONLY, will NOT show imagefs change"
        baseline_hint = (
            f"{_COMMAND_PRIORITY_HINT}"
            "- BASELINE INTEGRITY: You must compare disk usage on the SAME partition before/after injection. "
            "If your only baseline is from a different partition (e.g., nodefs /dev/vda3 at 16% but the fill "
            "targets imagefs), do NOT use it as the comparison baseline — state "
            "\"No pre-injection baseline available for imagefs\" instead.\n"
            "- FIRST-CHECK-AS-BASELINE: If no pre-injection baseline exists for the target partition, "
            "your first `df -h` check IS the baseline. Record the target partition's usage % and partition "
            "identity, then wait 5-10 seconds and re-check. If the value is stable near the expected "
            "fill percentage (e.g., --percent 85 → observed 84%), the fill has completed — this IS "
            "evidence the fault is in effect. If the value is increasing, the fill is still active.\n"
        )
    else:  # nodefs
        partition_desc = "root filesystem (typically /dev/vda3 mounted at /host)"
        verify_cmd = "df -h /host → shows nodefs usage. You can also use df -h (bare) to confirm"
        false_negative = "df -h (bare, looking at overlay) → shows imagefs, which is NOT the target partition"
        baseline_hint = (
            f"{_COMMAND_PRIORITY_HINT}"
            "- BASELINE INTEGRITY: You must compare disk usage on the SAME partition before/after injection. "
            "If your only baseline is from imagefs/overlay but the fill targets nodefs, do NOT use it as "
            "the comparison baseline — state "
            "\"No pre-injection baseline available for nodefs\" instead.\n"
            "- FIRST-CHECK-AS-BASELINE: If no pre-injection baseline exists for the target partition, "
            "your first `df -h /host` check IS the baseline. Record the usage % and re-check after 5-10 seconds.\n"
        )

    return (
        f"⚠ PROGRAMMATIC PARTITION DERIVATION (override generic hints for this specific injection):\n"
        f"- Fill path: {parsed_flags.get('path', '')}\n"
        f"- Likely target partition: {partition_type} ({partition_desc}) — verify with `df -h`\n"
        f"- CORRECT verification: {verify_cmd}\n"
        f"- FALSE NEGATIVE: {false_negative}\n"
        f"- If df shows NO partition with increased usage, check kubectl describe node for "
        f"DiskPressure condition as alternative evidence, but DiskPressure alone is NOT sufficient "
        f"to conclude the primary metric (disk usage >85%) is met.\n"
        f"{baseline_hint}"
    )


def _disk_burn_param_hints(parsed_flags: dict, scope: str | None = None) -> str:
    """Generate transient-fault-aware verification hints for disk-burn.

    pod-disk-burn creates TEMPORARY files that are auto-deleted when the
    experiment completes. The fault effect window may be narrower than the
    verification pipeline latency. These hints guide the LLM to:
    - For pod-scope: Use df -h baseline comparison as indirect evidence
    - For node-scope: Use /proc/diskstats delta sampling to detect I/O pressure
    - Use 'recovered_before_observation' when files are cleaned up but
      disk usage shows significant increase from baseline

    Note: ChaosBlade disk-burn hardcodes iterations at 100. Only
    --size is tuneable. Total write = size_mb * 100.
    """
    path_val = parsed_flags.get("path", "/")
    size_val = parsed_flags.get("size", "10")
    # ChaosBlade burn iterations are hardcoded at 100
    _BURN_ITERATIONS = 100
    try:
        size_mb = int(size_val)
        total_write_mb = size_mb * _BURN_ITERATIONS
    except (ValueError, TypeError):
        total_write_mb = 1000  # fallback estimate (10MB * 100)

    if scope == "node":
        return (
            f"⚠ node-disk-burn TRANSIENT FAULT GUIDANCE:\n"
            f"- Burn path: {path_val}\n"
            f"- Block size: {size_val}MB (estimated {total_write_mb}MB total write, "
            f"{_BURN_ITERATIONS} iterations hardcoded by ChaosBlade)\n"
            f"- CRITICAL: node-disk-burn in CRD mode creates I/O pressure via dd "
            f"processes running inside the container overlay. The I/O appears on the "
            f"overlay's backing partition (typically imagefs/vdb), NOT on the nodefs "
            f"partition (vda3) where /host{path_val} resides.\n"
            f"- Verification strategy:\n"
            f"  1. **PRIMARY**: Check the Injection Engine Post-Check above — if burn "
            f"I/O was already detected programmatically, that is AUTHORITATIVE evidence.\n"
            f"  2. Compare two /proc/diskstats samples (3-5s apart) — calculate write "
            f"throughput delta on ALL partitions. If any partition shows >10MB/s "
            f"sustained write → burn IS in effect.\n"
            f"  3. DO NOT check /host{path_val}/ for burn files — they exist in the "
            f"container overlay, not on the host filesystem.\n"
            f"  4. df -h is USELESS for burn — burn creates I/O pressure, not data "
            f"accumulation. Only use df -h as supplementary evidence if burn is "
            f"still active (temporary files may increase overlay usage).\n"
            f"  5. If burn I/O was detected on any partition → 'passed'.\n"
            f"  6. If experiment completed and I/O has stopped but was previously "
            f"detected → 'recovered_before_observation', NOT 'failed'.\n"
            f"  7. If NO I/O detected on ANY partition AND no burn evidence → 'failed'.\n"
            f"- Note: The agent auto-boosts burn --size for direct mode to ensure "
            f"a wider observable effect window."
        )
    else:
        return (
            f"⚠ pod-disk-burn TRANSIENT FAULT GUIDANCE:\n"
            f"- Burn path: {path_val}\n"
            f"- Block size: {size_val}MB (estimated {total_write_mb}MB total write, "
            f"{_BURN_ITERATIONS} iterations hardcoded by ChaosBlade)\n"
            f"- CRITICAL: pod-disk-burn creates TEMPORARY files that are automatically "
            f"deleted when the experiment completes. If experiment state is 'Success', "
            f"the burn files have probably been cleaned up.\n"
            f"- Verification strategy:\n"
            f"  1. Compare df -h {path_val} against baseline — a usage increase "
            f"({total_write_mb}MB+) is INDIRECT evidence the burn occurred, even if "
            f"files are gone\n"
            f"  2. Check for burn file remnants: `ls -lah {path_val}/`\n"
            f"  3. If files gone BUT df -h shows significant increase from baseline "
            f"→ 'recovered_before_observation', NOT 'failed'\n"
            f"  4. If NO df change AND no files → 'failed' (burn likely never executed)\n"
            f"- Note: The agent auto-boosts burn --size for direct mode to ensure "
            f"a wider observable effect window."
        )


# Registry of parameter-dependent hint generators.
# Key: (blade_target, blade_action). Value: callable(parsed_flags, scope=None) -> str | None.
# Extend this dict for new fault types — no verifier main-logic changes needed.
_PARAM_HINT_GENERATORS: dict[tuple[str, str], typing.Callable[..., str | None]] = {
    ("disk", "fill"): _disk_fill_param_hints,
    ("disk", "burn"): _disk_burn_param_hints,
    # Future: ("network", "drop"): _network_drop_param_hints,
    # Future: ("cpu", "fullload"): _cpu_fullload_param_hints,
}


_BASELINE_INTEGRITY_PROMPT: str = (
    "**BASELINE INTEGRITY** (applies to ALL quantitative metric verification — "
    "disk %, CPU %, memory %, latency ms, etc. Does NOT apply to qualitative status "
    "checks like 'Pod is Running' or 'Service is reachable'):\n"
    "1. IDENTIFY the exact resource you are measuring — be specific:\n"
    '   "imagefs /dev/vdb", "node cn-hongkong.10.0.2.69 CPU", "pod accounting memory", '
    '"endpoint /api/health latency"\n'
    '   "disk" or "CPU" alone is ambiguous — always include the resource identity.\n'
    "2. Your FIRST measurement is your BASELINE. Record the resource identity AND value together.\n"
    "3. ALL comparisons MUST be against the SAME resource. NEVER compare metrics from different resources:\n"
    '   ✅ "imagefs /dev/vdb: first-check 42% → re-check 84%" (same partition, valid delta)\n'
    '   ✅ "node X CPU: 12% → 89%" (same node, valid delta)\n'
    '   ❌ "first-check 16% → re-check 84%" (different partitions: 16% was nodefs /dev/vda3, '
    "84% was imagefs /dev/vdb — INVALID comparison)\n"
    "4. If you lack a pre-injection baseline for the target resource, say so explicitly:\n"
    '   "No pre-injection baseline available for imagefs /dev/vdb. Current value: 84%."\n'
    "5. HIGH post-injection values WITHOUT baseline context are ambiguous — the value may be "
    "pre-existing, not fault-caused. Look for corroborating evidence (e.g., DiskPressure condition, "
    "recent events, timestamp correlation with injection time).\n"
    "6. If your first-check value already matches the expected injection parameter "
    "(e.g., --percent 85 → first-check shows 84%), this IS evidence the fault is in effect — "
    "do NOT conclude 'no change' just because re-check shows the same value.\n"
    "7. EXPECTED NEGATIVE RESULTS: If the PRIMARY metric confirms the fault is in effect "
    "(e.g., disk usage matches --percent), but a THRESHOLD-DEPENDENT condition is not met "
    "(e.g., DiskPressure=False because usage is 84% vs 85% threshold), mark that step as "
    "'expected' — the negative result is anticipated and does not indicate injection failure. "
    "Do NOT use 'expected' as a synonym for 'failed'."
)


def _resolve_target_node(state: AgentState) -> str | None:
    """Extract the target node name from state for node-level faults."""
    from chaos_agent.agent.fault_spec import read_fault_spec
    spec = read_fault_spec(state)
    if spec and spec.names:
        return spec.names[0]
    return None


async def _discover_tool_pod_for_verification(
    kubeconfig: str, task_id: str = "", target_node: str | None = None
) -> str | None:
    """Discover a running tool pod for Layer 2 verification.

    When target_node is provided, only returns a pod running on that node.
    When target_node is None, returns the first available running pod.
    """
    from chaos_agent.tools.shell import run_command
    kubeconfig_args = ["--kubeconfig", kubeconfig] if kubeconfig else []

    if target_node:
        # Use -o wide to get node info for filtering
        discover_cmd = ["kubectl", "get", "pods", "-n", _TOOL_POD_NAMESPACE,
                        "-l", _TOOL_POD_LABEL_SELECTOR, "-o", "wide"] + kubeconfig_args
        try:
            result = await run_command(discover_cmd, task_id=task_id, source="verifier-L2-pod-discovery")
            pods_with_nodes = discover_tool_pods_with_nodes(result.stdout)
            for pod_name, node_name in pods_with_nodes:
                if node_name == target_node:
                    logger.info(f"Discovered tool pod on target node {target_node}: {pod_name}")
                    return pod_name
            logger.info(f"No tool pod found on target node {target_node}")
            return None
        except Exception as e:
            logger.warning(f"Failed to discover tool pods for verification: {e}")
            return None
    else:
        # Original behavior: first available pod
        discover_cmd = ["kubectl", "get", "pods", "-n", _TOOL_POD_NAMESPACE,
                        "-l", _TOOL_POD_LABEL_SELECTOR] + kubeconfig_args
        try:
            result = await run_command(discover_cmd, task_id=task_id, source="verifier-L2-pod-discovery")
            pods = discover_tool_pods(result.stdout)
            if pods:
                logger.info(f"Discovered tool pod for Layer 2 verification: {pods[0]}")
                return pods[0]
            logger.info("No running tool pods found for Layer 2 verification")
            return None
        except Exception as e:
            logger.warning(f"Failed to discover tool pods for verification: {e}")
            return None


def _get_fault_verification_hints(
    blade_scope: str | None,
    blade_target: str | None,
    blade_action: str | None,
    injection_method: str | None,
    injection_pod_name: str | None = None,
    parsed_flags: dict | None = None,
) -> str:
    """Generate verification hints based on fault metadata.

    Provides FACTUAL context (injection method, fault metadata, tool pod) to help the LLM
    design verification. Does NOT provide operational advice or domain pitfalls —
    those come from knowledge files (read_knowledge_resource) and skill_case_content.
    """
    hints = []

    # Injection method hints (factual context)
    if injection_method == "kubectl_exec":
        hints.append(
            "Injection was performed via kubectl exec (host blade was unavailable). "
            "The blade experiment is running inside a cluster tool pod."
        )
        # BusyBox Quick Reference for kubectl_exec verification
        hints.append(
            "BusyBox Quick Reference (commands via kubectl exec run in a BusyBox container):\n"
            "- iostat: NO -x flag. Use `iostat -d -k 1 3` (device stats) + `iostat -c 1 3` (CPU/iowait)\n"
            "  NOTE: cumulative counters may overflow (values near 9e18); use interval deltas, NOT absolute values\n"
            "- ps: NO -w flag. Use `ps` (bare) or `ps -o pid,args`, NOT `ps -w` or `ps -o PID,USER,TIME,COMMAND`\n"
            "- grep: NO -E flag (no extended regex). Use `grep -e pattern1 -e pattern2` or basic regex\n"
            "- mount: output differs; use `cat /proc/mounts` as alternative\n"
            "- top: may not exist. Use `top -bn1` (batch mode) or `cat /proc/stat`\n"
            "- df: `df -h` works normally on BusyBox\n"
            "- find: limited but functional. Avoid complex predicates\n"
            "- awk/sed: BusyBox versions have fewer features; prefer simple grep/cut"
        )
        # When injection used kubectl_exec and we know the pod name, that pod can
        # be used for ChaosBlade commands and kubectl API checks, but NOT for
        # host filesystem access (it does not mount /host).
        if injection_pod_name:
            hints.append(
                f"Tool pod `{injection_pod_name}` in `chaosblade` is available for:\n"
                f"  - ChaosBlade commands (blade status, blade destroy)\n"
                f"  - kubectl API checks (describe node, top node, get events)\n"
                f"LIMITATION: This pod does NOT mount /host. `df -h` inside it shows "
                f"the overlay filesystem, not the host disk."
            )
    elif injection_method == "kubectl_native":
        hints.append(
            "Injection was performed via kubectl-native operations (no ChaosBlade). "
            "Verify the configuration change directly via kubectl."
        )

    # Node-level overlay filesystem hint
    if blade_scope == "node":
        _version_skew_note = (
            "\nkubectl debug version note: kubectl debug node/ uses the EphemeralContainers API, "
            "which has breaking changes between K8s versions. When kubectl client and server "
            "versions differ by more than ±1 minor version, the command may return \"NotFound\". "
            "If kubectl debug fails: fall back to API-level checks only (kubectl describe node, "
            "kubectl get events) which are always available. You CANNOT use kubectl run as an "
            "alternative — it is not in the allowed subcommand list. Do not attempt kubectl run."
        )
        if injection_pod_name:
            hints.append(
                f"Node-level fault: Regular pods see OVERLAY filesystem, NOT the host.\n"
                f"For host filesystem checks (df -h on host, iostat, du on host paths), "
                f"use kubectl debug with the TWO-STEP approach:\n"
                f"  Step 1: kubectl(subcommand='debug', v_args='node/<node_name> --image=busybox -- sleep 3600')\n"
                f"  Step 2: From output, find debug pod name, then:\n"
                f"    kubectl(subcommand='exec', v_args='<debug-pod> -n default -- <command>')\n"
                f"CRITICAL: Must include '-- sleep 3600' in Step 1 — bare busybox exits immediately.\n"
                f"Host paths use /host/ prefix (e.g., /host/tmp, /host/var/log).\n"
                f"For API-level checks (describe node, top node), use kubectl directly or "
                f"the tool pod `{injection_pod_name}`."
                f"{_version_skew_note}"
            )
        else:
            hints.append(
                "Node-level fault: Regular pods see OVERLAY filesystem, NOT the host.\n"
                "For API-level checks (describe node, top node), use kubectl directly.\n"
                "For host filesystem checks (df -h, iostat, du on host paths), use kubectl debug:\n"
                "  Step 1: kubectl(subcommand='debug', v_args='node/<node_name> --image=busybox -- sleep 3600')\n"
                "  Step 2: From output, find debug pod name, then:\n"
                "    kubectl(subcommand='exec', v_args='<debug-pod> -n default -- <command>')\n"
                "CRITICAL: Must include '-- sleep 3600' in Step 1 — bare busybox exits immediately.\n"
                "Host paths use /host/ prefix (e.g., /host/tmp, /host/var/log)."
                f"{_version_skew_note}"
            )

    # Fault metadata (factual context) — OR so partial metadata is still useful
    if blade_scope or blade_target or blade_action:
        known = []
        if blade_scope:
            known.append(f"Scope: {blade_scope}")
        if blade_target:
            known.append(f"Target: {blade_target}")
        if blade_action:
            known.append(f"Action: {blade_action}")
        hints.append(f"Fault metadata: {' | '.join(known)}")

        if blade_scope and blade_target and blade_action:
            scope_target_action = f"{blade_scope}-{blade_target} {blade_action}"
            hints.append(f"ChaosBlade scenario: {scope_target_action}")

    # Parameter observability hints (e.g., disk fill size too small to observe)
    if blade_target and blade_action:
        compound_key = (blade_target, blade_action)
        # Static hints (independent of parameter values)
        param_hint = _PARAM_OBSERVABILITY_HINTS.get(compound_key)
        if param_hint:
            hints.append(param_hint)
        # Dynamic hints (dependent on parameter values)
        generator = _PARAM_HINT_GENERATORS.get(compound_key)
        if generator and parsed_flags:
            # Pass scope for burn hints (node vs pod verification strategy differs)
            if compound_key == ("disk", "burn"):
                dynamic_hint = generator(parsed_flags, scope=blade_scope)
            else:
                dynamic_hint = generator(parsed_flags)
            if dynamic_hint:
                hints.append(dynamic_hint)

    # Multi-disk topology hints for node-disk scenarios
    if blade_target == "disk" and blade_scope == "node":
        hints.append(_get_node_disk_topology_hints(blade_action))
        # Event filtering guidance: avoid false positives from other nodes
        hints.append(
            "Event filtering: When checking kubectl events for DiskPressure, "
            "you MUST filter by the target node. Events from OTHER nodes are from "
            "OTHER experiments and are NOT evidence for THIS injection. "
            "Use: kubectl(subcommand='get', v_args='events -A --field-selector "
            "involvedObject.name=<TARGET_NODE> --kubeconfig=...') "
            "or check `kubectl describe node <TARGET_NODE>` Conditions section. "
            "Do NOT use `kubectl get events -A | grep DiskPressure` — this returns "
            "events from ALL nodes and will include false positives."
        )

    # General guidance: domain knowledge available via knowledge docs
    hints.append(
        "For domain-specific verification patterns, data interpretation pitfalls, "
        "or kubectl field reference, check the Domain Knowledge Index and use "
        "`read_knowledge_resource` to load relevant documents."
    )

    return "\n".join(hints)