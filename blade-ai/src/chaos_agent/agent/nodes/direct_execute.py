"""Direct execute node: deterministic blade_create invocation (no LLM)."""

import logging
import re
import shlex

from langchain_core.messages import HumanMessage, ToolMessage

from chaos_agent.agent.node_names import DIRECT_EXECUTE
from chaos_agent.agent.nodes._injection_detection import discover_tool_pods
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.agent.state_helpers import fail_state
from chaos_agent.agent.verdict import FailureCategory
from chaos_agent.memory.session_store import get_global_session_store
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.tools.blade import blade_create
from chaos_agent.tools.kubectl import _build_kubectl_global_args, _split_args
from chaos_agent.tools.shell import run_command
from chaos_agent.utils.blade_uid import extract_blade_uid
from chaos_agent.utils.fault_type import build_blade_create_args
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

# Parameter observability warnings: warn when parameters may be too small
# to produce observable effects.  Keyed by (blade_target, blade_action).
# Each entry is a function that returns a warning string or None.
def _disk_fill_warning(params: dict) -> str | None:
    """Generate a warning when disk fill size may be too small to observe.

    Considers the absolute size and estimates impact on a typical 100GB node disk.
    """
    size_str = params.get("size", "0")
    try:
        size_mb = int(str(size_str).strip())
    except (ValueError, TypeError):
        return None
    if size_mb <= 0:
        return None
    if size_mb < 5120:
        return (
            f"Disk fill size={size_mb}MB (~{size_mb/1024:.1f}GB) is likely too small to observe. "
            f"On a typical 100GB node disk, this adds ~{size_mb/1024:.1f}% usage — "
            f"NOT enough to trigger DiskPressure (>85%) or show visible df -h change. "
            f"Consider using percent=85 instead, or increase size to at least 5120MB (5GB)."
        )
    return None


_PARAM_OBSERVABILITY_WARNINGS: dict[tuple[str, str], callable] = {
    ("disk", "fill"): _disk_fill_warning,
}

# ---------------------------------------------------------------------------
# Required-flag auto-completion: ensure critical flags are present for
# each scope+target+action combination. Without these, blade may report
# "Success" but produce no observable effect (e.g., mem load without
# --mode ram, or node-mem without --include-buffer-cache).
#
# This is the DETERMINISTIC safety net — it does NOT depend on LLM or
# prompt quality. If a flag is missing, it's added with a known-good
# default. If already present, it's left unchanged.
# ---------------------------------------------------------------------------

# (scope, target, action) → {param: default, ...} + [bare_flags]
_REQUIRED_PARAMS: dict[tuple[str, str, str], tuple[dict[str, str], list[str]]] = {
    # mem load: --mode ram is essential (without it, mem-burn may not allocate
    # physical RAM, showing no effect in kubectl top)
    ("pod", "mem", "load"):       ({"mode": "ram"}, []),
    ("node", "mem", "load"):      ({"mode": "ram"}, ["include-buffer-cache", "avoid-being-killed"]),
    ("container", "mem", "load"): ({"mode": "ram"}, []),
    # disk burn: --read and/or --write must be set (without them, no IO is generated)
    ("pod", "disk", "burn"):      ({}, ["read", "write"]),
    ("node", "disk", "burn"):     ({}, ["read", "write"]),
}


def _auto_complete_params(
    scope: str, target: str, action: str,
    params: dict, params_flags: list,
) -> list[str]:
    """Auto-add known-required flags if missing. Mutates params/params_flags in place.

    Returns list of what was added (for logging). Empty if nothing needed.
    """
    key = (scope, target, action)
    if key not in _REQUIRED_PARAMS:
        return []

    required_kv, required_bare = _REQUIRED_PARAMS[key]
    added = []

    for k, default in required_kv.items():
        if k not in params:
            params[k] = default
            added.append(f"{k}={default}")

    for flag in required_bare:
        if flag not in params_flags:
            params_flags.append(flag)
            added.append(f"--{flag}")

    return added


# ---------------------------------------------------------------------------
# Burn parameter auto-boost: widens the effect window for transient disk I/O
# faults so that the verification pipeline (L1 + LLM reasoning + L2 checks,
# ~10-15s latency) can reliably observe effects before they dissipate.
#
# ChaosBlade pod-disk-burn supports ONLY --size (block size, MB) and
# --read/--write boolean flags. The iteration count is hardcoded at 100.
# Default ChaosBlade: --size 10 * 100 iterations = ~1GB write, completing
# in 5-10 seconds — too narrow for our verification window.
#
# Auto-boosting --size to 100MB gives 100MB * 100 = 10GB total write,
# producing a 30-60+ second effect window.
# ---------------------------------------------------------------------------

_BURN_DEFAULT_SIZE = "100"   # 100MB blocks (100 * 100 iterations hardcoded = 10GB total write)


def _auto_boost_burn_params(params: dict, size_ceiling: int | None = None) -> dict:
    """Inject reasonable burn defaults to widen the observable effect window.

    Only applies to pod-disk-burn in direct mode. Does NOT override
    user-specified values — only fills in missing parameters.

    Args:
        params: Current parameter dict.
        size_ceiling: FCAT-computed maximum safe size (MB). If provided and
            the user did not explicitly specify --size, clamp the auto-boosted
            value to this ceiling to prevent OOMKill (P0 param safety guard).

    Note: ChaosBlade pod-disk-burn does NOT support --count. Only --size
    is tuneable (iteration count is hardcoded at 100).
    """
    boosted = dict(params)
    if "size" not in boosted:
        size = _BURN_DEFAULT_SIZE
        if size_ceiling is not None:
            size = str(min(int(size), size_ceiling))
        boosted["size"] = size
    return boosted


# OOMKill risk threshold (MB): pods below this limit are at high risk
# of OOMKill when burn uses the default --size=100 (10GB total I/O).
_OOMKILL_RISK_THRESHOLD_MB = 512


async def _fetch_pod_memory_limit_mb(
    namespace: str,
    names: list[str],
    labels: dict,
    kubeconfig: str,
    task_id: str,
) -> int | None:
    """Fetch the memory limit (in MB) of the target pod.

    Uses kubectl get pod -o jsonpath to read spec.containers[0].resources.limits.memory.
    Returns None if the limit cannot be determined (no limit set, kubectl error, etc.).
    Best-effort: never blocks injection on failure.
    """
    if not namespace:
        return None

    tracker = get_tracker(task_id) if task_id and task_id != "unknown" else None

    try:
        from chaos_agent.tools.shell import run_command
        from chaos_agent.tools.kubectl import _build_kubectl_global_args
        from chaos_agent.config.settings import settings as _settings
        from chaos_agent.utils.fault_type import parse_k8s_memory_to_mb

        kubectl_path = _settings.kubectl_path
        global_args = _build_kubectl_global_args(kubeconfig)

        # Build command as list (run_command requires list[str], not string)
        if names:
            pod_name = names[0].split(",")[0] if isinstance(names[0], str) else names[0]
            cmd = [kubectl_path, *global_args, "get", "pod", pod_name,
                   "-n", namespace,
                   "-o", "jsonpath={.spec.containers[0].resources.limits.memory}"]
        elif labels:
            label_selector = ",".join(f"{k}={v}" for k, v in labels.items())
            cmd = [kubectl_path, *global_args, "get", "pods",
                   "-n", namespace, "-l", label_selector,
                   "-o", "jsonpath={.items[0].spec.containers[0].resources.limits.memory}"]
        else:
            return None

        result = await run_command(
            cmd, timeout=_settings.timeout_kubectl, task_id=task_id,
            source="direct_execute-memory-limit",
        )
        if result.exit_code != 0 or not result.stdout:
            if tracker:
                tracker.update(
                    f"Pod memory limit query failed (rc={result.exit_code}): {result.stderr[:200]}",
                    {"step": "memory_limit", "rc": result.exit_code, "stderr": result.stderr[:500]},
                )
            return None

        # Strip surrounding quotes from jsonpath output
        stdout = result.stdout.strip().strip("'\"")
        if not stdout:
            if tracker:
                tracker.update(
                    "Pod memory limit: no limit set (empty jsonpath output)",
                    {"step": "memory_limit", "result": "no_limit"},
                )
            return None

        mem_mb = parse_k8s_memory_to_mb(stdout)
        if tracker:
            tracker.update(
                f"Pod memory limit: {stdout} = {mem_mb}MB",
                {"step": "memory_limit", "raw_value": stdout, "mb": mem_mb},
            )
        return mem_mb

    except Exception as e:
        logger.warning("Failed to fetch pod memory limit, skipping OOMKill risk check")
        if tracker:
            tracker.update(
                f"Pod memory limit fetch error: {str(e)[:200]}",
                {"step": "memory_limit", "error": str(e)[:500]},
            )
        return None


async def _fetch_pod_memory_usage_mb(
    namespace: str,
    names: list[str],
    kubeconfig: str,
    task_id: str,
) -> int | None:
    """Fetch current memory usage (in MB) of the target pod via kubectl top.

    Best-effort: returns None on any error (metrics-server unavailable, etc.).
    """
    if not namespace or not names:
        return None
    try:
        from chaos_agent.tools.shell import run_command
        from chaos_agent.tools.kubectl import _build_kubectl_global_args
        from chaos_agent.config.settings import settings as _settings

        kubectl_path = _settings.kubectl_path
        global_args = _build_kubectl_global_args(kubeconfig)
        pod_name = names[0].split(",")[0] if isinstance(names[0], str) else names[0]
        cmd = [kubectl_path, *global_args, "top", "pod", pod_name,
               "-n", namespace, "--no-headers"]
        result = await run_command(
            cmd, timeout=_settings.timeout_kubectl, task_id=task_id,
            source="direct_execute-memory-usage",
        )
        if result.exit_code != 0 or not result.stdout:
            return None
        # Parse: "pod-name  123m  208Mi" → extract Mi value
        parts = result.stdout.strip().split()
        for i, p in enumerate(parts):
            p_upper = p.upper()
            if p_upper.endswith("MI") or p_upper.endswith("MIB"):
                try:
                    return int(p_upper.rstrip("MIB").rstrip("MI"))
                except ValueError:
                    pass
            elif p_upper.endswith("GI") or p_upper.endswith("GIB"):
                try:
                    return int(float(p_upper.rstrip("GIB").rstrip("GI")) * 1024)
                except ValueError:
                    pass
        return None
    except Exception:
        return None


def _parse_blade_uid_from_content(content: str) -> str:
    """Extract blade_uid from a ChaosBlade tool response.

    Thin wrapper around `chaos_agent.utils.blade_uid.extract_blade_uid` —
    preserves this module's empty-string-on-failure contract (the shared
    util uses Optional[str]). All multi-strategy parsing logic, including
    the 54000+success=false safeguard, lives in the shared util.
    """
    uid = extract_blade_uid(content)
    return uid or ""


def _build_blade_command_for_exec(
    scope: str,
    target: str,
    action: str,
    namespace: str = "",
    names: str = "",
    labels: str = "",
    flags: str = "",
) -> str:
    """Build blade command string for kubectl exec (no --kubeconfig).

    Mirrors blade.py blade_create command construction, but:
    - Omits --kubeconfig (pod uses ServiceAccount internally)
    - Auto-appends --timeout if flags don't specify one
    """
    parts = ["blade", "create", "k8s", f"{scope}-{target}", action]

    if namespace and scope != "node":
        parts.extend(["--namespace", namespace])
    if names:
        parts.extend(["--names", names])
    if labels and scope != "node":
        parts.extend(["--labels", labels])

    if flags:
        try:
            parts.extend(shlex.split(flags))
        except ValueError:
            parts.extend(flags.split())

    # Auto-inject or boost --timeout: mirrors blade_create tool logic
    # Ensures all paths (including LLM-generated short timeouts) are boosted
    from chaos_agent.utils.fault_type import ensure_min_duration

    if "--timeout" not in parts:
        # No timeout specified: auto-inject recommended minimum
        effective_timeout = ensure_min_duration(None, scope, target, action)
        parts.extend(["--timeout", str(effective_timeout)])
    else:
        # Timeout specified (by LLM or flags): check if it meets the minimum
        timeout_idx = parts.index("--timeout")
        if timeout_idx + 1 < len(parts):
            current_val = parts[timeout_idx + 1]
            effective_timeout = ensure_min_duration(current_val, scope, target, action)
            if effective_timeout != int(current_val):
                parts[timeout_idx + 1] = str(effective_timeout)
                logger.info(
                    f"Auto-boosted --timeout from {current_val}s to {effective_timeout}s "
                    f"for {scope}-{target}-{action} (recommended minimum)"
                )

    return " ".join(parts)


async def _try_kubectl_exec_fallback(
    scope: str,
    target: str,
    action: str,
    namespace: str,
    names: str,
    labels: str,
    kubeconfig: str,
    flags: str,
    task_id: str,
) -> dict | None:
    """Attempt fault injection via kubectl exec into a cluster tool pod.

    Used as a fallback when host blade_create fails (e.g. K8s API server
    unreachable from the host). Discovers a running otel-c-tool pod,
    executes blade create inside it, and extracts the blade_uid.

    Returns:
        {"blade_uid": str, "pod_name": str, "output": str} on success,
        None if fallback is impossible or fails.
    """
    # Step 1: Discover a running otel-c-tool pod via direct run_command
    # (pass task_id so tracker events are emitted for CLI visibility)
    cmd = [settings.kubectl_path]
    cmd.extend(_build_kubectl_global_args(kubeconfig))
    cmd.append("get")
    cmd.extend(_split_args("pods -n chaosblade -l app=otel-c-tool -o wide"))
    try:
        get_result = await run_command(cmd, timeout=settings.timeout_kubectl, task_id=task_id)
    except Exception as e:
        logger.warning("Fallback: failed to discover tool pods: %s", e)
        return None
    if get_result.exit_code != 0:
        logger.warning(
            "Fallback: failed to discover tool pods (exit=%d): %s",
            get_result.exit_code, get_result.stderr[:200],
        )
        return None
    pods = discover_tool_pods(get_result.stdout)
    if not pods:
        logger.warning("Fallback: no running otel-c-tool pods found")
        return None

    # For node-scope, prefer a tool pod on the target node for observability.
    # CRD-based injection works from any pod, but selecting the target node's
    # pod keeps logs and diagnostics co-located with the fault target.
    pod_name = pods[0]
    if scope == "node" and names:
        from chaos_agent.agent.nodes._injection_detection import discover_tool_pods_with_nodes
        pods_with_nodes = discover_tool_pods_with_nodes(get_result.stdout)
        _target_nodes = {n.strip() for n in names.split(",") if n.strip()}
        for pname, pnode in pods_with_nodes:
            if pnode in _target_nodes:
                pod_name = pname
                break
        # If no pod on target node, fall back to any available pod (CRD still works)
        logger.info(
            "Fallback: node-scope selected pod %s (target nodes: %s)",
            pod_name, ', '.join(_target_nodes),
        )
    logger.info(f"Fallback: using tool pod {pod_name}")

    # Step 2: Build blade command (without --kubeconfig)
    blade_cmd = _build_blade_command_for_exec(
        scope=scope,
        target=target,
        action=action,
        namespace=namespace,
        names=names,
        labels=labels,
        flags=flags,
    )
    v_args = f"{pod_name} -n chaosblade -- {blade_cmd}"

    # Step 3: Execute via kubectl exec (direct run_command for tracker visibility)
    cmd = [settings.kubectl_path]
    cmd.extend(_build_kubectl_global_args(kubeconfig))
    cmd.append("exec")
    cmd.extend(_split_args(v_args))
    # Defense-in-depth: _build_blade_command_for_exec already handles --timeout
    # inject/boost, but in case it didn't (shouldn't happen), catch it here.
    if re.search(r"\bblade\s+create\b", v_args) and "--timeout" not in v_args:
        from chaos_agent.utils.fault_type import ensure_min_duration
        effective_timeout = ensure_min_duration(None, scope, target, action)
        cmd.extend(["--timeout", str(effective_timeout)])
        logger.info(
            "Fallback: auto-injected --timeout %ss into kubectl exec blade create",
            effective_timeout,
        )
    try:
        exec_result = await run_command(cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id)
    except Exception as e:
        logger.warning("Fallback: kubectl exec failed: %s", e)
        return None
    # Diagnostic: log actual stdout/stderr lengths and previews
    logger.warning(
        "Fallback: kubectl exec exit=%d stdout(%d)=%r stderr(%d)=%r",
        exec_result.exit_code,
        len(exec_result.stdout) if exec_result.stdout else 0,
        (exec_result.stdout or "")[:300],
        len(exec_result.stderr) if exec_result.stderr else 0,
        (exec_result.stderr or "")[:300],
    )

    # Step 4: Extract blade_uid from kubectl exec output.
    # Blade writes JSON to stdout on success, but on error (e.g. 54000)
    # the JSON may land on either stdout or stderr.  Try both streams.
    blade_uid = (
        _parse_blade_uid_from_content(exec_result.stdout) or
        _parse_blade_uid_from_content(exec_result.stderr)
    )
    if blade_uid:
        logger.info(
            "Fallback: kubectl exec succeeded via pod %s, blade_uid=%s",
            pod_name, blade_uid,
        )
        # Use whichever stream had the JSON for recording
        output = exec_result.stdout if _parse_blade_uid_from_content(exec_result.stdout) else exec_result.stderr
        return {"blade_uid": blade_uid, "pod_name": pod_name, "output": output}

    # Failed to extract uid — error details already logged in diagnostic above
    logger.warning(
        "Fallback: kubectl exec completed but no blade_uid extracted. "
        "pod=%s v_args=%s",
        pod_name, v_args,
    )
    return None


async def _verify_disk_fill_effect(
    scope: str,
    target: str,
    action: str,
    names: str,
    kubeconfig: str,
    params: dict,
    blade_uid: str,
    task_id: str,
) -> dict | None:
    """Programmatic post-injection effect check for disk-fill faults.

    Bridges the trust gap between "blade query k8s says Success" and
    "the filesystem was actually filled". For node-disk-fill, discovers
    the tool pod on the TARGET node and checks for the fill file in its
    container overlay (imagefs — where CRD-mode fills actually write).

    Returns a dict with check results, or None if not applicable.
    """
    if not (scope == "node" and target == "disk" and action == "fill"):
        return None

    fill_path = params.get("path", "/tmp")
    size = params.get("size", "?")
    node_name = names  # --names maps to node name for node-level faults

    if not node_name:
        return None

    # Discover tool pod on the TARGET node (not injection pod)
    from chaos_agent.tools.shell import run_command
    from chaos_agent.tools.kubectl import _build_kubectl_global_args, _split_args
    from chaos_agent.agent.nodes._injection_detection import discover_tool_pods_with_nodes, _TOOL_POD_NAMESPACE, _TOOL_POD_LABEL_SELECTOR

    # Step 1: Discover tool pod on target node
    discover_cmd = [settings.kubectl_path]
    discover_cmd.extend(_build_kubectl_global_args(kubeconfig))
    discover_cmd.extend(["get", "pods", "-n", _TOOL_POD_NAMESPACE,
                         "-l", _TOOL_POD_LABEL_SELECTOR, "-o", "wide"])
    try:
        discover_result = await run_command(discover_cmd, timeout=settings.timeout_kubectl,
                                            task_id=task_id)
        pods_with_nodes = discover_tool_pods_with_nodes(discover_result.stdout)
    except Exception as e:
        logger.warning(f"disk-fill post-check: failed to discover tool pods: {e}")
        return None

    target_pod = None
    for pod_name, pod_node in pods_with_nodes:
        if pod_node == node_name:
            target_pod = pod_name
            break

    if not target_pod:
        logger.warning(
            f"disk-fill post-check: no tool pod found on target node {node_name}"
        )
        return None

    logger.info(f"disk-fill post-check: using tool pod {target_pod} on {node_name}")

    # Step 2: Check for fill file in the tool pod's overlay filesystem
    check_cmd = [settings.kubectl_path]
    check_cmd.extend(_build_kubectl_global_args(kubeconfig))
    check_cmd.append("exec")
    check_cmd.extend(_split_args(
        f"{target_pod} -n {_TOOL_POD_NAMESPACE} -- ls -lh {fill_path}/"
    ))
    try:
        check_result = await run_command(check_cmd, timeout=settings.timeout_kubectl,
                                         task_id=task_id)
    except Exception as e:
        logger.warning(f"disk-fill post-check: kubectl exec failed: {e}")
        return None

    stdout = check_result.stdout or ""
    # ChaosBlade fill files typically named chaos_filldisk.log.dat or chaos_fill*
    has_fill_file = any(pat in stdout for pat in ("chaos_fill", "chaosblade"))

    # Step 3: Check overlay df for usage increase
    df_cmd = [settings.kubectl_path]
    df_cmd.extend(_build_kubectl_global_args(kubeconfig))
    df_cmd.append("exec")
    df_cmd.extend(_split_args(
        f"{target_pod} -n {_TOOL_POD_NAMESPACE} -- df -h {fill_path}"
    ))
    try:
        df_result = await run_command(df_cmd, timeout=settings.timeout_kubectl,
                                      task_id=task_id)
    except Exception as e:
        logger.warning(f"disk-fill post-check: df -h failed: {e}")
        df_result = None

    result = {
        "fill_file_found": has_fill_file,
        "target_pod": target_pod,
        "node": node_name,
        "ls_output": stdout[:500],
        "df_output": (df_result.stdout or "")[:500] if df_result else "",
        "blade_uid": blade_uid,
    }

    if has_fill_file:
        logger.info(
            f"disk-fill post-check PASSED: fill file found in "
            f"{target_pod}:{fill_path}/"
        )
    else:
        logger.warning(
            f"disk-fill post-check WARNING: no fill file found in "
            f"{target_pod}:{fill_path}/ — blade_uid={blade_uid} "
            f"reports Success but filesystem may not have been modified"
        )

    return result


async def _verify_disk_burn_effect(
    scope: str,
    target: str,
    action: str,
    names: str,
    kubeconfig: str,
    params: dict,
    blade_uid: str,
    task_id: str,
    namespace: str = "",
) -> dict | None:
    """Programmatic post-injection effect check for disk-burn faults.

    Bridges the trust gap between "blade query k8s says Success" and
    "the disk I/O pressure is actually present".

    For node-disk-burn: discovers the tool pod on the TARGET node and
    samples /proc/diskstats twice with a short interval to detect
    sustained write throughput.

    For pod-disk-burn: execs into the target pod to sample /proc/diskstats;
    falls back to tool pod on the target pod's node if the target pod
    lacks the ``cat`` command (minimal container images).

    Returns a dict with check results, or None if not applicable.
    """
    if not (target == "disk" and action == "burn"):
        return None

    if not names:
        return None

    from chaos_agent.tools.shell import run_command
    from chaos_agent.tools.kubectl import _build_kubectl_global_args, _split_args
    from chaos_agent.agent.nodes._injection_detection import (
        discover_tool_pods_with_nodes, _TOOL_POD_NAMESPACE, _TOOL_POD_LABEL_SELECTOR,
    )

    # Resolve the exec target: where to sample /proc/diskstats from.
    # For node-scope: tool pod on the target node.
    # For pod-scope: target pod directly, with tool-pod fallback.
    exec_pod_name = ""
    exec_pod_namespace = ""
    node_name = ""

    if scope == "node":
        node_name = names  # --names maps to node name for node-level faults
        # Discover tool pod on the TARGET node
        discover_cmd = [settings.kubectl_path]
        discover_cmd.extend(_build_kubectl_global_args(kubeconfig))
        discover_cmd.extend(["get", "pods", "-n", _TOOL_POD_NAMESPACE,
                             "-l", _TOOL_POD_LABEL_SELECTOR, "-o", "wide"])
        try:
            discover_result = await run_command(
                discover_cmd, timeout=settings.timeout_kubectl, task_id=task_id,
            )
            pods_with_nodes = discover_tool_pods_with_nodes(discover_result.stdout)
        except Exception as e:
            logger.warning(f"disk-burn post-check: failed to discover tool pods: {e}")
            return None

        for pod_name, pod_node in pods_with_nodes:
            if pod_node == node_name:
                exec_pod_name = pod_name
                break

        if not exec_pod_name:
            logger.warning(
                f"disk-burn post-check: no tool pod found on target node {node_name}"
            )
            return None

        exec_pod_namespace = _TOOL_POD_NAMESPACE
        logger.info(f"disk-burn post-check: using tool pod {exec_pod_name} on {node_name}")

    elif scope == "pod":
        # Primary: exec into the target pod directly
        pod_name = names.split(",")[0].strip()  # Use first pod name
        node_name = ""  # Will resolve below if needed
        exec_pod_namespace = namespace or "default"

        # Try sampling /proc/diskstats from the target pod
        try_sample_cmd = [settings.kubectl_path]
        try_sample_cmd.extend(_build_kubectl_global_args(kubeconfig))
        try_sample_cmd.append("exec")
        try_sample_cmd.extend(_split_args(
            f"{pod_name} -n {exec_pod_namespace} -- cat /proc/diskstats"
        ))

        try:
            probe_result = await run_command(
                try_sample_cmd, timeout=settings.timeout_kubectl, task_id=task_id,
            )
            if probe_result.exit_code == 0 and probe_result.stdout:
                exec_pod_name = pod_name
                logger.info(
                    f"disk-burn post-check: target pod {pod_name} supports "
                    f"cat /proc/diskstats, sampling directly"
                )
        except Exception:
            pass  # Fall through to tool-pod fallback

        # Fallback: discover tool pod on the target pod's node
        if not exec_pod_name:
            logger.info(
                f"disk-burn post-check: target pod {pod_name} does not support "
                f"cat /proc/diskstats, falling back to tool pod"
            )
            # Discover which node the target pod is on
            node_cmd = [settings.kubectl_path]
            node_cmd.extend(_build_kubectl_global_args(kubeconfig))
            node_cmd.extend([
                "get", "pod", pod_name, "-n", exec_pod_namespace,
                "-o", "jsonpath={.spec.nodeName}",
            ])
            try:
                node_result = await run_command(
                    node_cmd, timeout=settings.timeout_kubectl, task_id=task_id,
                )
                if node_result.exit_code == 0 and node_result.stdout:
                    node_name = node_result.stdout.strip()
            except Exception:
                pass

            if not node_name:
                logger.warning(
                    f"disk-burn post-check: cannot determine node for "
                    f"pod {pod_name}, skipping"
                )
                return None

            # Find tool pod on that node
            discover_cmd = [settings.kubectl_path]
            discover_cmd.extend(_build_kubectl_global_args(kubeconfig))
            discover_cmd.extend(["get", "pods", "-n", _TOOL_POD_NAMESPACE,
                                 "-l", _TOOL_POD_LABEL_SELECTOR, "-o", "wide"])
            try:
                discover_result = await run_command(
                    discover_cmd, timeout=settings.timeout_kubectl, task_id=task_id,
                )
                pods_with_nodes = discover_tool_pods_with_nodes(discover_result.stdout)
            except Exception as e:
                logger.warning(f"disk-burn post-check: failed to discover tool pods: {e}")
                return None

            for tp_name, tp_node in pods_with_nodes:
                if tp_node == node_name:
                    exec_pod_name = tp_name
                    break

            if not exec_pod_name:
                logger.warning(
                    f"disk-burn post-check: no tool pod found on node "
                    f"{node_name} for pod {pod_name}"
                )
                return None

            exec_pod_namespace = _TOOL_POD_NAMESPACE
            logger.info(
                f"disk-burn post-check: using tool pod {exec_pod_name} "
                f"on {node_name} (fallback for pod {pod_name})"
            )
    else:
        return None

    # Step 2: Sample /proc/diskstats twice with 5-second interval
    sample_cmd_prefix = [settings.kubectl_path]
    sample_cmd_prefix.extend(_build_kubectl_global_args(kubeconfig))
    sample_cmd_prefix.append("exec")
    sample_cmd_prefix.extend(_split_args(
        f"{exec_pod_name} -n {exec_pod_namespace} -- cat /proc/diskstats"
    ))

    try:
        sample1_result = await run_command(
            sample_cmd_prefix, timeout=settings.timeout_kubectl, task_id=task_id,
        )
        sample1_text = sample1_result.stdout or ""
    except Exception as e:
        logger.warning(f"disk-burn post-check: first diskstats sample failed: {e}")
        return None

    import asyncio
    await asyncio.sleep(5)

    try:
        sample2_result = await run_command(
            sample_cmd_prefix, timeout=settings.timeout_kubectl, task_id=task_id,
        )
        sample2_text = sample2_result.stdout or ""
    except Exception as e:
        logger.warning(f"disk-burn post-check: second diskstats sample failed: {e}")
        return None

    # Step 3: Parse diskstats and compute write throughput per partition
    _SECTOR_SIZE = 512
    _SAMPLE_INTERVAL = 5
    _BURN_DETECTION_THRESHOLD_MB_S = 10  # 10 MB/s sustained write = burn detected

    def _parse_diskstats(text: str) -> dict[str, int]:
        """Parse /proc/diskstats into {partition_name: sectors_written}.

        /proc/diskstats format (fields 0-based):
            0: major  1: minor  2: name  3: reads_completed  4: reads_merged
            5: sectors_read  6: ms_reading  7: writes_completed  8: writes_merged
            9: sectors_written  10: ms_writing  ...
        Field 9 = sectors_written (cumulative).
        """
        result = {}
        for line in text.strip().splitlines():
            fields = line.split()
            if len(fields) < 10:
                continue
            name = fields[2]
            # Skip partitions (e.g., vda1, vda3) — only track whole devices.
            # nvme0n1 is a whole device; nvme0n1p1 is a partition.
            # vdX/sdX without trailing digit = whole device.
            # vdX1/vdX3/sdX1 = partition.
            if re.match(r"^(vd|sd|xvd)\D+\d+$", name):
                # Partition: vda1, vda3, sda1 — skip
                continue
            if re.match(r"^nvme\d+n\d+p\d+$", name):
                # NVMe partition: nvme0n1p1 — skip
                continue
            try:
                sectors_written = int(fields[9])
                result[name] = sectors_written
            except (ValueError, IndexError):
                continue
        return result

    stats1 = _parse_diskstats(sample1_text)
    stats2 = _parse_diskstats(sample2_text)

    active_partitions = []
    burn_io_detected = False
    for name in stats2:
        if name not in stats1:
            continue
        delta_sectors = stats2[name] - stats1[name]
        if delta_sectors < 0:
            continue  # counter wraparound or parse error
        throughput_mb_s = delta_sectors * _SECTOR_SIZE / (1024 * 1024) / _SAMPLE_INTERVAL
        if throughput_mb_s > 0.1:  # Only report partitions with measurable I/O
            active_partitions.append({
                "name": name,
                "write_throughput_mb_s": round(throughput_mb_s, 1),
            })
        if throughput_mb_s > _BURN_DETECTION_THRESHOLD_MB_S:
            burn_io_detected = True

    # Sort by throughput descending
    active_partitions.sort(key=lambda p: p["write_throughput_mb_s"], reverse=True)

    result = {
        "burn_io_detected": burn_io_detected,
        "active_partitions": active_partitions,
        "target_pod": exec_pod_name,
        "node": node_name,
        "scope": scope,
        "blade_uid": blade_uid,
        "sample_interval_seconds": _SAMPLE_INTERVAL,
    }

    if burn_io_detected:
        top_partition = active_partitions[0] if active_partitions else {}
        logger.info(
            f"disk-burn post-check PASSED: burn I/O detected on "
            f"{top_partition.get('name', '?')}: "
            f"~{top_partition.get('write_throughput_mb_s', 0)} MB/s write throughput"
        )
    else:
        logger.warning(
            f"disk-burn post-check WARNING: no significant I/O detected on any partition "
            f"(top: {active_partitions[0] if active_partitions else 'none'}) — "
            f"blade_uid={blade_uid} reports Success but no burn I/O observed"
        )

    return result


async def _capture_evidence_snapshot(
    scope: str,
    target: str,
    action: str,
    target_metadata: dict,
    namespace: str,
    names: str,
    kubeconfig: str,
    task_id: str,
) -> dict | None:
    """P0-evidence-snapshot: capture quick evidence after blade_create.

    For low-memory pods that may OOMKill before verifier can observe.
    Returns snapshot_data dict or None if not applicable / capture failed.
    """
    if target_metadata is None:
        return None

    from chaos_agent.utils.fault_context import lookup_adaptations

    snapshot_adaptations = [
        a for a in lookup_adaptations(
            scope, target, action, target_metadata, rule_type="param_override",
        )
        if a.action.get("evidence_capture")
    ]
    for snap_adj in snapshot_adaptations:
        try:
            import asyncio
            delay = snap_adj.action.get("snapshot_delay_seconds", 3)
            commands = snap_adj.action.get("snapshot_commands", [])
            logger.info(
                "FCAT: %s — capturing evidence snapshot in %ds", snap_adj.id, delay,
            )
            await asyncio.sleep(delay)
            snapshot_data = {}
            for cmd in commands:
                kubectl_args_str = " ".join(_build_kubectl_global_args(kubeconfig))
                exec_cmd = (
                    f"{settings.kubectl_path} {kubectl_args_str} "
                    f"exec {names.split(',')[0]} -n {namespace} -- {cmd}"
                )
                rc, stdout, stderr = await run_command(
                    exec_cmd, timeout=10, task_id=task_id,
                    source="direct_execute-evidence-snapshot",
                )
                snapshot_data[cmd] = {
                    "rc": rc, "stdout": stdout[:2000], "stderr": stderr[:500],
                }
            logger.info(
                "FCAT: %s — evidence snapshot captured (%d commands)",
                snap_adj.id, len(commands),
            )
            return snapshot_data
        except Exception:
            logger.debug(
                "FCAT: evidence snapshot failed (non-critical)", exc_info=True,
            )
    return None


async def direct_execute(state: AgentState) -> dict:
    """Directly invoke blade_create without LLM, replacing execute_loop.

    Constructs blade_create arguments from AgentState's structured
    parameters (blade_scope/blade_target/blade_action/params/params_flags),
    calls blade_create.ainvoke(), and extracts the blade_uid.

    The params dict is set in the same format that execute_loop would
    produce from blade_create tool_call args, ensuring verifier and
    build_status_data work correctly.
    """
    task_id = state.get("task_id", "unknown")

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "direct_execute",
        "Direct execute: calling blade_create",
        {},
    )

    # 1. Build blade_create arguments from the FaultSpec — single
    # source of truth. The 8-field flat layout means we no longer have
    # to read state.target / state.blade_* / state.params separately
    # and worry about cross-field consistency.
    from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec
    spec = read_fault_spec(state) or FaultSpec()
    scope = spec.scope
    target = spec.blade_target
    action = spec.blade_action
    namespace = spec.namespace
    names = ",".join(spec.names)
    labels_str = ",".join(f"{k}={v}" for k, v in spec.labels.items())
    kubeconfig = state.get("kubeconfig") or ""
    params = dict(spec.params)
    params_flags = list(spec.params_flags)
    target_metadata = state.get("target_metadata") or {}

    # Duration auto-boost: MIDDLE layer of three-layer guarantee
    from chaos_agent.utils.fault_type import ensure_min_duration
    _duration = ensure_min_duration(
        spec.duration_seconds, scope, target, action,
    )
    if _duration != spec.duration_seconds:
        logger.info(
            f"Duration auto-adjusted from {spec.duration_seconds}s to {_duration}s "
            f"for {scope}-{target}-{action}"
        )

    # ── Required-flag auto-completion ──────────────────────────────────
    # Deterministic safety net: auto-add flags that are known-required
    # for certain scope+target+action combinations to produce the
    # intended effect. Same pattern as ensure_min_duration for --timeout.
    _completions = _auto_complete_params(scope, target, action, params, params_flags)
    if _completions:
        logger.info(
            "Auto-completed params for %s-%s %s: %s",
            scope, target, action, _completions,
        )

    # Burn parameter auto-boost: widen the effect window for transient disk I/O
    if target == "disk" and action == "burn":
        original_params = params.copy()

        # Session store (used by P0 and OOMKill risk messages below)
        _session_store = get_global_session_store()
        _task_id_local = state.get("task_id", "")

        # FCAT P0: compute safe burn size from target_metadata
        # Also fetch current memory usage for usage-aware size calculation.
        fcat_size_ceiling = None
        from chaos_agent.utils.fault_context import lookup_adaptations, compute_safe_burn_size
        adaptations = lookup_adaptations(
            scope, target, action, target_metadata, rule_type="param_override",
        )
        for adj in adaptations:
            if "param_overrides" in adj.action and adj.action["param_overrides"].get("size") == "auto":
                # Prefer the usage value baseline_capture already parsed
                # out of its own ``kubectl top pod`` call (via
                # ``baseline_extractors.extract_pod_top_metrics``). When
                # present this avoids issuing the same ``kubectl top
                # pod`` a second time. Fallback to a fresh fetch covers:
                # baseline failed / extractor couldn't match the pod /
                # the (pod,target) entry doesn't carry the extractor.
                _usage_mb = (target_metadata or {}).get("pod_memory_usage_mb")
                if _usage_mb is None:
                    _usage_mb = await _fetch_pod_memory_usage_mb(
                        namespace, target_info.get("names", []), kubeconfig, task_id,
                    )
                fcat_size_ceiling = compute_safe_burn_size(
                    target_metadata.get("pod_memory_limit_mb"),
                    pod_memory_usage_mb=_usage_mb,
                )
                logger.info(
                    "FCAT: %s matched, size_ceiling=%d (limit=%sMB, usage=%sMB)",
                    adj.id, fcat_size_ceiling,
                    target_metadata.get("pod_memory_limit_mb"),
                    _usage_mb,
                )
                break

        params = _auto_boost_burn_params(params, size_ceiling=fcat_size_ceiling)

        # P0 ceiling computed → always write session message (regardless of param change)
        if fcat_size_ceiling is not None:
            if _session_store and _task_id_local:
                _p0_msg = (
                    f"[FCAT P0] P0-param-safety-burn-lowmem: ceiling={fcat_size_ceiling}MB, "
                    f"pod_memory_limit={target_metadata.get('pod_memory_limit_mb', 'unknown')}MB, "
                    f"pod_memory_usage={_usage_mb or 'unknown'}MB, "
                    f"size={params.get('size', 'auto')}MB"
                )
                _session_store.append_messages(_task_id_local, [HumanMessage(content=_p0_msg)], node_name=DIRECT_EXECUTE)
            if settings.is_debug and tracker:
                tracker.update(
                    (f"[FCAT P0] ceiling={fcat_size_ceiling}MB, "
                     f"limit={target_metadata.get('pod_memory_limit_mb', 'unknown')}MB, "
                     f"usage={_usage_mb or 'unknown'}MB, "
                     f"size={params.get('size', 'auto')}MB")[:200],
                    {"debug": True, "fcat": True},
                )
            if params != original_params:
                logger.info(
                    "Burn params auto-boosted (FCAT P0): size=%s (ceiling=%d)",
                    params.get("size"), fcat_size_ceiling,
                )
        elif params != original_params:
            # No P0 ceiling → auto-boost without FCAT intervention
            if _session_store and _task_id_local:
                _boost_msg = (
                    f"[FCAT P0] Burn params auto-boosted (no FCAT ceiling): "
                    f"size={params.get('size')}MB"
                )
                _session_store.append_messages(_task_id_local, [HumanMessage(content=_boost_msg)], node_name=DIRECT_EXECUTE)
            if settings.is_debug and tracker:
                tracker.update(
                    f"[FCAT P0] Burn params auto-boosted (no FCAT ceiling): size={params.get('size')}MB"[:200],
                    {"debug": True, "fcat": True},
                )
            logger.info(
                "Burn params auto-boosted for %s-%s-%s: %s injected",
                scope, target, action,
                set(params.keys()) - set(original_params.keys()),
            )

        # OOMKill risk warning: check pod memory limit before injection.
        # Prefer target_metadata (already collected by direct_setup) over re-fetching.
        # Gate by ``target == "mem"`` — the whole block compares
        # ``params.get("size", _BURN_DEFAULT_SIZE)`` against the pod's
        # memory limit, which only carries meaning for memory-burn
        # faults. For cpu / network / io etc. ``size`` isn't a real
        # param and the default-vs-limit math produces a misleading
        # warning ("Pod memory limit too low for burn --size=..." on
        # a CPU drill). Skipping also avoids the fallback ``kubectl
        # ... resources.limits.memory`` call when direct_setup also
        # skipped its prefetch for the same reason.
        if scope == "pod" and target == "mem":
            memory_limit_mb = (target_metadata or {}).get("pod_memory_limit_mb")
            if memory_limit_mb is None:
                # Fallback: fetch if not in target_metadata (e.g., old code path)
                memory_limit_mb = await _fetch_pod_memory_limit_mb(
                    namespace=namespace,
                    names=target_info.get("names", []),
                    labels=labels_dict,
                    kubeconfig=kubeconfig,
                    task_id=task_id,
                )
            if memory_limit_mb is not None and memory_limit_mb < _OOMKILL_RISK_THRESHOLD_MB:
                burn_size = params.get("size", _BURN_DEFAULT_SIZE)
                if fcat_size_ceiling is not None and int(burn_size) <= fcat_size_ceiling:
                    # FCAT already reduced the size — just log it
                    logger.info(
                        "FCAT P0: burn size already reduced to %s (OOMKill risk mitigated)",
                        burn_size,
                    )
                else:
                    burn_warning = (
                        f"Pod memory limit ({memory_limit_mb}MB) may be too low for "
                        f"burn --size={burn_size} (~{burn_size}MB*100=10GB total I/O). "
                        f"OOMKill is likely. If OOMKill occurs, the verifier will "
                        f"detect it as a side-effect-confirmed result. "
                        f"To reduce OOMKill risk, specify --params size=20 explicitly."
                    )
                    logger.warning(burn_warning)
                    tracker.update(
                        f"WARNING: {burn_warning}",
                        {"warning": True, "memory_limit_mb": memory_limit_mb},
                    )
            # Persist memory limit result to session store
            if _session_store and _task_id_local:
                _mem_msg = (
                    f"[OOMKill Risk] Pod memory limit: {memory_limit_mb}MB"
                    if memory_limit_mb is not None
                    else "[OOMKill Risk] Pod memory limit: not available"
                )
                _session_store.append_messages(
                    _task_id_local,
                    [HumanMessage(content=_mem_msg)],
                    node_name=DIRECT_EXECUTE,
                )

    args = build_blade_create_args(
        scope=scope,
        target=target,
        action=action,
        namespace=namespace,
        names=names,
        labels=labels_str,
        kubeconfig=kubeconfig,
        params=params,
        params_flags=params_flags,
        duration=_duration,
    )

    # 2. Parameter observability warning (before blade_create)
    warning_fn = _PARAM_OBSERVABILITY_WARNINGS.get((target, action))
    if warning_fn:
        try:
            warning_msg = warning_fn(params)
            if warning_msg:
                tracker.update(f"WARNING: {warning_msg}", {"warning": True})
        except Exception:
            logger.debug("Parameter observability warning check failed", exc_info=True)

    # 2.5 Pre-flight check: for node-scope, verify DaemonSet pod on target node(s)
    # This prevents wasting time on injections that are guaranteed to fail
    # (e.g., target node has DiskPressure and DaemonSet pod is Evicted).
    result_params = {
        "scope": scope,
        "target": target,
        "action": action,
        "namespace": namespace,
        "names": names,
        "labels": labels_str,
    }

    if scope == "node" and names:
        from chaos_agent.agent.nodes._injection_detection import (
            discover_tool_pods_with_nodes, _TOOL_POD_NAMESPACE, _TOOL_POD_LABEL_SELECTOR,
        )
        _preflight_cmd = [settings.kubectl_path]
        _preflight_cmd.extend(_build_kubectl_global_args(kubeconfig))
        _preflight_cmd.extend([
            "get", "pods", "-n", _TOOL_POD_NAMESPACE,
            "-l", _TOOL_POD_LABEL_SELECTOR, "-o", "wide",
        ])
        _preflight_blocked = False
        try:
            _preflight_result = await run_command(
                _preflight_cmd, timeout=settings.timeout_kubectl, task_id=task_id,
            )
            _pods_with_nodes = discover_tool_pods_with_nodes(_preflight_result.stdout)

            _target_nodes = [n.strip() for n in names.split(",") if n.strip()]
            _available_nodes = {pnode for _, pnode in _pods_with_nodes}
            _missing_nodes = [n for n in _target_nodes if n not in _available_nodes]

            if _missing_nodes:
                _preflight_blocked = True
                _preflight_msg = (
                    f"目标节点 {', '.join(_missing_nodes)} 上无 Running 的 ChaosBlade "
                    f"DaemonSet Pod，节点级故障注入不可行。"
                    f"请检查节点 DiskPressure/MemoryPressure 状态及 DaemonSet 运行情况。"
                )
                logger.error("Pre-flight check FAILED: %s", _preflight_msg)
                tracker.complete(
                    f"Pre-flight check failed: no running DaemonSet pod on "
                    f"{', '.join(_missing_nodes)}",
                    detail={"prerequisite": "daemonset_pod_on_target_node",
                            "missing_nodes": _missing_nodes},
                )
                sync_node_status_to_session(
                    state, "direct_execute", _preflight_msg,
                    detail={"failure_category": "prerequisite_failed"},
                )
            else:
                logger.info(
                    "Pre-flight check passed: DaemonSet pod available on %s",
                    ', '.join(_target_nodes),
                )
        except Exception:
            # 检查失败时不阻塞，避免网络抖动误杀正常注入 (fail-open)
            logger.warning(
                "Pre-flight check raised exception, skipping (fail-open): ",
                exc_info=True,
            )

        if _preflight_blocked:
            return {
                **fail_state(
                    FailureCategory.PREREQUISITE_FAILED,
                    f"no running ChaosBlade DaemonSet pod on target node(s) {', '.join(_missing_nodes)}",
                    state.get("messages", []),
                ),
                "params": result_params,
                "execute_loop_count": 1,
                "messages": [],
            }

    # 3. Call blade_create
    flags_str = args.get("flags", "")
    result_params["flags"] = flags_str
    logger.info(
        f"Direct execute: blade create k8s {scope}-{target} {action}"
        + (f" {flags_str}" if flags_str else "")
    )
    blade_result = await blade_create.ainvoke({**args, "task_id": task_id})

    # 4. Extract blade_uid from result JSON
    blade_uid = _parse_blade_uid_from_content(blade_result)

    # 5. Parse key parameters from flags string for verifier consumption
    blade_parsed_flags = None
    if flags_str:
        from chaos_agent.utils.fault_type import parse_blade_flags
        parsed = parse_blade_flags(flags_str)
        if parsed:
            blade_parsed_flags = parsed

    # 6. Error handling with kubectl exec fallback
    if not blade_uid:
        # Diagnostic: log host blade output so we can see why it failed
        logger.warning(
            "Host blade_create returned no uid. raw_output(%d)=%r",
            len(str(blade_result)) if blade_result else 0,
            str(blade_result)[:500] if blade_result else "(empty)",
        )
        # New: pattern-match known environmental errors for better diagnostics
        raw_output = str(blade_result) if blade_result else ""
        diag_hint = ""
        if "bad file descriptor" in raw_output:
            diag_hint = (
                "Host blade CLI cannot connect to K8s API — possible causes: "
                "ulimit too low, file descriptor leak, network stack issue. "
                "Falling back to kubectl exec."
            )
        elif "connection refused" in raw_output.lower():
            diag_hint = (
                "K8s API server unreachable from host. "
                "Check kubeconfig server address and network. "
                "Falling back to kubectl exec."
            )
        if diag_hint:
            logger.warning("Host blade_create diagnostic: %s", diag_hint)

        # --- Namespace compatibility retry ---
        # Some ChaosBlade versions (particularly host-installed binaries) do not
        # support --namespace for k8s sub-commands.  The blade_create tool
        # docstring documents this known issue.  Retry without --namespace before
        # falling back to kubectl exec.
        if "unknown flag" in raw_output and "--namespace" in raw_output:
            logger.info(
                "Host blade_create failed with 'unknown flag: --namespace'. "
                "Retrying without --namespace (blade version compatibility)."
            )
            tracker.update("Retrying host blade without --namespace (version incompatibility)", {})
            retry_args = {**args, "namespace": ""}
            blade_result = await blade_create.ainvoke({**retry_args, "task_id": task_id})
            blade_uid = _parse_blade_uid_from_content(blade_result)

            if blade_uid:
                logger.info(
                    "Host blade_create succeeded on retry (without --namespace): "
                    "blade_uid=%s", blade_uid,
                )
            else:
                logger.warning(
                    "Host blade_create retry (without --namespace) also failed. "
                    "Falling back to kubectl exec."
                )

    # 7. Second check after namespace retry — fall back to kubectl exec if still no uid
    if not blade_uid:
        # --- Fallback: try kubectl exec into cluster tool pod ---
        fallback_result = await _try_kubectl_exec_fallback(
            scope=scope,
            target=target,
            action=action,
            namespace=namespace,
            names=names,
            labels=args.get("labels", ""),
            kubeconfig=kubeconfig,
            flags=args.get("flags", ""),
            task_id=task_id,
        )

        if fallback_result:
            blade_uid = fallback_result["blade_uid"]
            pod_name = fallback_result["pod_name"]
            logger.info(
                f"Direct execute: kubectl exec fallback succeeded via pod {pod_name}, "
                f"blade_uid={blade_uid}"
            )
            tracker.complete(
                f"Direct execute done via kubectl exec fallback: blade_uid={blade_uid}"
            )
            sync_node_status_to_session(state, DIRECT_EXECUTE,
                f"Injection completed via kubectl exec fallback, blade_uid={blade_uid}",
                detail={"blade_uid": blade_uid, "injection_method": "kubectl_exec",
                        "fallback_used": True})
            result = {
                "blade_uid": blade_uid,
                "injection_method": "kubectl_exec",
                "injection_start_time": now_iso(),
                "kubectl_exec_pod_name": pod_name,
                "params": result_params,
                "blade_parsed_flags": blade_parsed_flags,
                "execute_loop_count": 1,
                "messages": [
                    HumanMessage(content=(
                        f"[Injection Phase] kubectl exec fallback succeeded via pod {pod_name}: "
                        f"blade_uid={blade_uid} (injection_method=kubectl_exec)"
                    )),
                    ToolMessage(
                        content=blade_result,
                        name="blade_create",
                        tool_call_id="direct",
                    ),
                    ToolMessage(
                        content=fallback_result["output"],
                        name="kubectl",
                        tool_call_id="direct_fallback",
                    ),
                ],
            }
            # P0-evidence-snapshot: also capture for kubectl_exec fallback path
            snapshot_data = await _capture_evidence_snapshot(
                scope, target, action, target_metadata or {},
                namespace, names, kubeconfig, task_id,
            )
            if snapshot_data:
                result["evidence_snapshot"] = snapshot_data
                _snap_summary = "; ".join(
                    f"{cmd} → rc={data.get('rc', '?')}"
                    for cmd, data in snapshot_data.items()
                )
                _snap_store = get_global_session_store()
                _snap_tid = state.get("task_id", "")
                if _snap_store and _snap_tid:
                    _snap_store.append_messages(_snap_tid, [HumanMessage(
                        content=f"[FCAT P0] Evidence snapshot captured ({len(snapshot_data)} commands): {_snap_summary}"
                    )], node_name=DIRECT_EXECUTE)
                if settings.is_debug and tracker:
                    tracker.update(
                        f"[FCAT P0] Evidence snapshot captured ({len(snapshot_data)} cmds): {_snap_summary}"[:200],
                        {"debug": True, "fcat": True},
                    )
            await sync_to_store(state, result)
            # Record messages to session store (direct_execute bypasses PreReasoningHook)
            _session_store = get_global_session_store()
            _task_id_local = state.get("task_id", "")
            if _session_store and _task_id_local:
                _session_store.append_messages(_task_id_local, result["messages"], node_name=DIRECT_EXECUTE)
            # Post-injection effect check: programmatic fill file verification
            # for disk-fill faults (bridges CRD Success ↔ filesystem reality gap)
            disk_fill_check = await _verify_disk_fill_effect(
                scope, target, action, names, kubeconfig, params, blade_uid, task_id,
            )
            if disk_fill_check:
                result["disk_fill_post_check"] = disk_fill_check
            # Post-injection effect check: programmatic I/O throughput verification
            # for disk-burn faults (bridges CRD Success ↔ I/O pressure reality gap)
            disk_burn_check = await _verify_disk_burn_effect(
                scope, target, action, names, kubeconfig, params, blade_uid, task_id,
                namespace=namespace,
            )
            if disk_burn_check:
                result["disk_burn_post_check"] = disk_burn_check
            return result
        logger.warning("Direct execute: kubectl exec fallback also failed")
        error_msg = (
            blade_result[:500]
            if isinstance(blade_result, str)
            else "blade_create returned no UID"
        )
        result = {
            **fail_state(
                FailureCategory.EXECUTION_FAILED,
                "blade_create failed (and kubectl exec fallback also failed)",
                state.get("messages", []),
            ),
            "injection_method": "host_blade",
            "params": result_params,
            "blade_parsed_flags": blade_parsed_flags,
            "execute_loop_count": 1,
            "messages": [
                HumanMessage(content=(
                    f"[Injection Phase] blade_create failed and kubectl exec fallback also failed: "
                    f"{error_msg[:200]}"
                )),
                ToolMessage(
                    content=blade_result,
                    name="blade_create",
                    tool_call_id="direct",
                )
            ],
        }
        tracker.fail(f"blade_create failed: {error_msg[:200]}")
        sync_node_status_to_session(state, DIRECT_EXECUTE,
            f"Injection failed: {error_msg[:200]}",
            detail={"injection_method": "host_blade", "fallback_used": False,
                    "safety_status": "rejected", "reason": "blade_create_failed"})
        await sync_to_store(state, result)
        # Record messages to session store (direct_execute bypasses PreReasoningHook)
        _session_store = get_global_session_store()
        _task_id_local = state.get("task_id", "")
        if _session_store and _task_id_local:
            _session_store.append_messages(_task_id_local, result["messages"], node_name=DIRECT_EXECUTE)
        return result

    result = {
        "blade_uid": blade_uid,
        "injection_method": "host_blade",
        "injection_start_time": now_iso(),
        "params": result_params,
        "blade_parsed_flags": blade_parsed_flags,
        "execute_loop_count": 1,
        "messages": [
            HumanMessage(content=(
                f"[Injection Phase] blade_create succeeded: blade_uid={blade_uid} "
                f"(injection_method=host_blade)"
            )),
            ToolMessage(
                content=blade_result,
                name="blade_create",
                tool_call_id="direct",
            )
        ],
    }

    # P0-evidence-snapshot: capture quick evidence after blade_create
    # for low-memory pods that may OOMKill before verifier can observe.
    snapshot_data = await _capture_evidence_snapshot(
        scope, target, action, target_metadata or {},
        namespace, names, kubeconfig, task_id,
    )
    if snapshot_data:
        result["evidence_snapshot"] = snapshot_data
        _snap_summary = "; ".join(
            f"{cmd} → rc={data.get('rc', '?')}"
            for cmd, data in snapshot_data.items()
        )
        _snap_store = get_global_session_store()
        _snap_tid = state.get("task_id", "")
        if _snap_store and _snap_tid:
            _snap_store.append_messages(_snap_tid, [HumanMessage(
                content=f"[FCAT P0] Evidence snapshot captured ({len(snapshot_data)} commands): {_snap_summary}"
            )])
        if settings.is_debug and tracker:
            tracker.update(
                f"[FCAT P0] Evidence snapshot captured ({len(snapshot_data)} cmds): {_snap_summary}"[:200],
                {"debug": True, "fcat": True},
            )

    tracker.complete(f"Direct execute done: blade_uid={blade_uid}")
    sync_node_status_to_session(state, DIRECT_EXECUTE,
        f"Injection completed, blade_uid={blade_uid}",
        detail={"blade_uid": blade_uid, "injection_method": "host_blade",
                "fallback_used": False})
    await sync_to_store(state, result)
    # Record messages to session store (direct_execute bypasses PreReasoningHook)
    _session_store = get_global_session_store()
    _task_id_local = state.get("task_id", "")
    if _session_store and _task_id_local:
        _session_store.append_messages(_task_id_local, result["messages"], node_name=DIRECT_EXECUTE)
    # Post-injection effect check: programmatic fill file verification
    disk_fill_check = await _verify_disk_fill_effect(
        scope, target, action, names, kubeconfig, params, blade_uid, task_id,
    )
    if disk_fill_check:
        result["disk_fill_post_check"] = disk_fill_check
    # Post-injection effect check: programmatic I/O throughput verification
    disk_burn_check = await _verify_disk_burn_effect(
        scope, target, action, names, kubeconfig, params, blade_uid, task_id,
        namespace=namespace,
    )
    if disk_burn_check:
        result["disk_burn_post_check"] = disk_burn_check
    return result
