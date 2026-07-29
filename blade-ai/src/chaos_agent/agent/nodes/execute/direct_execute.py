"""Direct execute node: deterministic blade_create invocation (no LLM)."""

import logging
import re
import shlex

from langchain_core.messages import HumanMessage, ToolMessage

from chaos_agent.agent.node_names import DIRECT_EXECUTE
from chaos_agent.agent.nodes.store._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.capabilities import resolve_profile_for_state
from chaos_agent.transports import PROFILE_K8S
from chaos_agent.agent.environment_profiles import get_environment_profile
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.agent.spec.fault_registry import is_memory_burn_scope, is_python_scope
from chaos_agent.config.settings import settings
from chaos_agent.memory.session_store import get_global_session_store
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.tools.blade import blade_create
from chaos_agent.tools.kubectl import _split_args, build_kubectl_cmd
from chaos_agent.transports import TransportTarget, execute_via_transport
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

    tracker = get_tracker(task_id)

    try:
        from chaos_agent.tools.kubectl import build_kubectl_cmd
        from chaos_agent.config.settings import settings as _settings
        from chaos_agent.utils.fault_type import parse_k8s_memory_to_mb

        # Build command as list (run_command requires list[str], not string)
        if names:
            pod_name = names[0].split(",")[0] if isinstance(names[0], str) else names[0]
            cmd = build_kubectl_cmd("get", ["pod", pod_name,
                   "-n", namespace,
                   "-o", "jsonpath={.spec.containers[0].resources.limits.memory}"], kubeconfig=kubeconfig)
        elif labels:
            label_selector = ",".join(f"{k}={v}" for k, v in labels.items())
            cmd = build_kubectl_cmd("get", ["pods",
                   "-n", namespace, "-l", label_selector,
                   "-o", "jsonpath={.items[0].spec.containers[0].resources.limits.memory}"], kubeconfig=kubeconfig)
        else:
            return None

        _target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, _target, timeout=_settings.timeout_kubectl, task_id=task_id,
            source="direct_execute-memory-limit", expect_profile=PROFILE_K8S,
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
        from chaos_agent.tools.kubectl import build_kubectl_cmd
        from chaos_agent.config.settings import settings as _settings

        pod_name = names[0].split(",")[0] if isinstance(names[0], str) else names[0]
        cmd = build_kubectl_cmd("top", ["pod", pod_name,
               "-n", namespace, "--no-headers"], kubeconfig=kubeconfig)
        _target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, _target, timeout=_settings.timeout_kubectl, task_id=task_id,
            source="direct_execute-memory-usage", expect_profile=PROFILE_K8S,
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
    from chaos_agent.utils.fault_type import ensure_min_duration, normalize_timeout_flag

    timeout_value = normalize_timeout_flag(parts)
    if timeout_value is None:
        # No timeout specified: auto-inject recommended minimum
        effective_timeout = ensure_min_duration(None, scope, target, action)
        parts.extend(["--timeout", str(effective_timeout)])
    else:
        # Timeout specified (by LLM or flags): check if it meets the minimum.
        # ``normalize_timeout_flag`` also canonicalizes ``--timeout=<value>``.
        timeout_idx = parts.index("--timeout")
        try:
            current_int = int(timeout_value)
        except (ValueError, TypeError):
            current_int = 0
        effective_timeout = ensure_min_duration(timeout_value, scope, target, action)
        if effective_timeout != current_int:
            parts[timeout_idx + 1] = str(effective_timeout)
            logger.info(
                f"Auto-boosted --timeout from {timeout_value}s to {effective_timeout}s "
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
    # Step 1: Discover a running tool pod via cluster-wide search
    # (pass task_id so tracker events are emitted for CLI visibility)
    from chaos_agent.agent.nodes.execute._injection_detection import (
        discover_tool_pods_cluster_wide,
        discover_tool_pod_on_node,
    )

    pod_name = None
    pod_ns = None

    # For node-scope, prefer a tool pod on the target node for observability.
    # CRD-based injection works from any pod, but selecting the target node's
    # pod keeps logs and diagnostics co-located with the fault target.
    if scope == "node" and names:
        _target_nodes = [n.strip() for n in names.split(",") if n.strip()]
        for _tnode in _target_nodes:
            result = await discover_tool_pod_on_node(_tnode, kubeconfig, task_id)
            if result:
                pod_name, pod_ns = result
                logger.info(
                    "Fallback: node-scope selected pod %s (ns=%s) on node %s",
                    pod_name, pod_ns, _tnode,
                )
                break

    # Fall back to any available pod cluster-wide (CRD still works from any pod)
    if not pod_name:
        try:
            all_pods = await discover_tool_pods_cluster_wide(kubeconfig, task_id)
        except Exception as e:
            logger.warning("Fallback: failed to discover tool pods: %s", e)
            return None
        if not all_pods:
            logger.warning("Fallback: no running tool pods found cluster-wide")
            return None
        pod_name, pod_ns = all_pods[0]

    logger.info(f"Fallback: using tool pod {pod_name} (ns={pod_ns})")

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
    v_args = f"{pod_name} -n {pod_ns} -- {blade_cmd}"

    # Step 3: Execute via kubectl exec (direct run_command for tracker visibility)
    # Defense-in-depth: _build_blade_command_for_exec already handles --timeout
    # inject/boost, but in case it didn't (shouldn't happen), catch it here.
    exec_args = _split_args(v_args)
    if re.search(r"\bblade\s+create\b", v_args) and "--timeout" not in v_args:
        from chaos_agent.utils.fault_type import ensure_min_duration
        effective_timeout = ensure_min_duration(None, scope, target, action)
        exec_args.extend(["--timeout", str(effective_timeout)])
        logger.info(
            "Fallback: auto-injected --timeout %ss into kubectl exec blade create",
            effective_timeout,
        )
    cmd = build_kubectl_cmd("exec", exec_args, kubeconfig=kubeconfig)
    try:
        _target = TransportTarget.from_state({})
        exec_result = await execute_via_transport(
            cmd, _target, timeout=settings.timeout_kubectl_exec, task_id=task_id,
            expect_profile=PROFILE_K8S,
        )
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


async def _run_profile_post_checks(
    result: dict,
    *,
    scope: str,
    target: str,
    action: str,
    names: str,
    kubeconfig: str,
    params: dict,
    blade_uid: str,
    task_id: str,
    namespace: str,
    state: dict | None,
) -> None:
    """Run the resolved fault profile's declared post-injection effect checks.

    Replaces the previously hardcoded pair of ``_verify_disk_*_effect`` calls at
    each injection path with a declarative loop over
    ``VerificationProfile.post_injection_checks``. Each check's non-empty return
    is stored under its declared ``result_key``. Effect-check function bodies
    (and their defensive self-guards) are unchanged; only the dispatch moved
    from hardcoded calls to descriptor iteration. Kwargs are filtered to each
    function's accepted parameters so their differing signatures (burn takes
    ``namespace``) stay untouched.
    """
    import inspect

    from chaos_agent.agent.nodes.verify._verification_profiles import (
        VerificationContext,
        resolve_verification_profile,
    )

    _available = {
        "scope": scope,
        "target": target,
        "action": action,
        "names": names,
        "kubeconfig": kubeconfig,
        "params": params,
        "blade_uid": blade_uid,
        "task_id": task_id,
        "namespace": namespace,
        "state": state,
    }
    _ctx = VerificationContext(scope=scope, target=target, action=action)
    for _spec in resolve_verification_profile(target).post_injection_checks(_ctx):
        _accepted = inspect.signature(_spec.fn).parameters
        _kwargs = {k: v for k, v in _available.items() if k in _accepted}
        _check = await _spec.fn(**_kwargs)
        if _check:
            result[_spec.result_key] = _check


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
            _snap_target = TransportTarget.from_state({})
            for cmd in commands:
                exec_cmd = build_kubectl_cmd("exec", [
                    names.split(',')[0], "-n", namespace, "--",
                ] + _split_args(cmd), kubeconfig=kubeconfig)
                result = await execute_via_transport(
                    exec_cmd, _snap_target, timeout=10, task_id=task_id,
                    source="direct_execute-evidence-snapshot", expect_profile=PROFILE_K8S,
                )
                snapshot_data[cmd] = {
                    "rc": result.exit_code,
                    "stdout": result.stdout[:2000],
                    "stderr": result.stderr[:500],
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



async def _apply_burn_adjustments(
    params: dict,
    scope: str, target: str, action: str,
    target_metadata: dict,
    namespace: str, spec_names: list[str],
    kubeconfig: str, task_id: str,
    tracker, state: AgentState,
) -> tuple[dict, int | None]:
    """Apply disk-burn parameter auto-boost (size, count, nohup).

    Returns (modified_params, fcat_size_ceiling).
    """
    if target != "disk" or action != "burn":
        return params, None

    original_params = params.copy()

    _session_store = get_global_session_store()
    _task_id_local = state.get("task_id", "")

    # FCAT P0: compute safe burn size from target_metadata
    fcat_size_ceiling = None
    from chaos_agent.utils.fault_context import lookup_adaptations, compute_safe_burn_size
    adaptations = lookup_adaptations(
        scope, target, action, target_metadata, rule_type="param_override",
    )
    for adj in adaptations:
        if "param_overrides" in adj.action and adj.action["param_overrides"].get("size") == "auto":
            _usage_mb = (target_metadata or {}).get("pod_memory_usage_mb")
            if _usage_mb is None:
                _usage_mb = await _fetch_pod_memory_usage_mb(
                    namespace, list(spec_names), kubeconfig, task_id,
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

    # P0 ceiling computed → always write session message
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

    return params, fcat_size_ceiling


async def _run_blade_create_with_fallback(
    state: AgentState,
    args: dict,
    result_params: dict,
    blade_parsed_flags: dict | None,
    scope: str, target: str, action: str,
    namespace: str, names: str,
    kubeconfig: str, params: dict,
    target_metadata: dict,
    task_id: str, tracker,
) -> dict:
    """Execute blade_create, handle retries and kubectl exec fallback.

    Returns the final result dict for direct_execute.
    """
    # Call blade_create
    flags_str = args.get("flags", "")
    result_params["flags"] = flags_str
    logger.info(
        f"Direct execute: blade create k8s {scope}-{target} {action}"
        + (f" {flags_str}" if flags_str else "")
    )
    blade_result = await blade_create.ainvoke({**args, "task_id": task_id})

    # Extract blade_uid from result JSON
    blade_uid = _parse_blade_uid_from_content(blade_result)

    # Parse key parameters from flags string for verifier consumption
    if not blade_parsed_flags and flags_str:
        from chaos_agent.utils.fault_type import parse_blade_flags
        parsed = parse_blade_flags(flags_str)
        if parsed:
            blade_parsed_flags = parsed

    # Error handling with kubectl exec fallback
    if not blade_uid:
        logger.warning(
            "Host blade_create returned no uid. raw_output(%d)=%r",
            len(str(blade_result)) if blade_result else 0,
            str(blade_result)[:500] if blade_result else "(empty)",
        )
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

        # Namespace compatibility retry
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

    # Fall back to kubectl exec if still no uid
    if not blade_uid:
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
            _session_store = get_global_session_store()
            _task_id_local = state.get("task_id", "")
            if _session_store and _task_id_local:
                _session_store.append_messages(_task_id_local, result["messages"], node_name=DIRECT_EXECUTE)
            await _run_profile_post_checks(
                result,
                scope=scope, target=target, action=action, names=names,
                kubeconfig=kubeconfig, params=params, blade_uid=blade_uid,
                task_id=task_id, namespace=namespace, state=None,
            )
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
        _session_store = get_global_session_store()
        _task_id_local = state.get("task_id", "")
        if _session_store and _task_id_local:
            _session_store.append_messages(_task_id_local, result["messages"], node_name=DIRECT_EXECUTE)
        return result

    # Success path: blade_create returned a uid
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
    _session_store = get_global_session_store()
    _task_id_local = state.get("task_id", "")
    if _session_store and _task_id_local:
        _session_store.append_messages(_task_id_local, result["messages"], node_name=DIRECT_EXECUTE)
    await _run_profile_post_checks(
        result,
        scope=scope, target=target, action=action, names=names,
        kubeconfig=kubeconfig, params=params, blade_uid=blade_uid,
        task_id=task_id, namespace=namespace, state=state,
    )
    return result


async def _run_python_agent_create(
    state: AgentState,
    *,
    target: str, action: str,
    params: dict, params_flags: list, duration: int,
    result_params: dict,
    task_id: str, tracker,
) -> dict:
    """Execute a Python-application (in-process agent) injection.

    Separate from :func:`_run_blade_create_with_fallback` because this fault
    domain has its own command shape and no fallback path: the fault lives
    inside the target application process, reached through the host-local agent,
    so there is no cluster tool pod to fall back to. A failure is terminal here
    instead of triggering the kubectl-exec fallback (which would be meaningless).

    ``params`` are split by the tool itself: matcher keys go to matcher flags,
    everything else stays in the action ``flags`` string. Flag VALUES are
    shell-quoted: the tool re-splits the string with ``shlex``, so an unquoted
    value containing spaces (``--exception-message "chaos drill: db down"`` — the
    normal case for this fault domain) would otherwise be torn into several argv
    items, truncating the message and leaving stray positional arguments.
    """
    import shlex

    from chaos_agent.tools.blade_python import _TARGET_MATCHERS, blade_python_create

    _matcher_names = _TARGET_MATCHERS.get(target, ())
    _matchers = {k: str(v) for k, v in params.items() if k in _matcher_names}
    _flag_parts: list[str] = []
    for k, v in params.items():
        if k in _matcher_names:
            continue
        _flag_parts.extend([f"--{k}", shlex.quote(str(v))])
    for flag in params_flags or []:
        _flag_parts.append(f"--{flag}")
    if duration > 0 and "timeout" not in params:
        _flag_parts.extend(["--timeout", str(duration)])
    _flags = " ".join(_flag_parts)
    result_params["flags"] = _flags

    logger.info(
        "Direct execute: blade create python %s %s%s",
        target, action, f" {_flags}" if _flags else "",
    )
    tool_output = await blade_python_create.ainvoke({
        "target": target, "action": action, "flags": _flags,
        "task_id": task_id, **_matchers,
    })
    blade_uid = _parse_blade_uid_from_content(tool_output)

    if not blade_uid:
        error_msg = str(tool_output)[:500] if tool_output else "no UID returned"
        result = {
            **fail_state(
                FailureCategory.EXECUTION_FAILED,
                "blade create python failed (no experiment UID)",
                state.get("messages", []),
            ),
            "injection_method": "python_agent",
            "params": result_params,
            "execute_loop_count": 1,
            "messages": [
                HumanMessage(content=(
                    f"[Injection Phase] blade create python failed: {error_msg[:200]}"
                )),
                ToolMessage(
                    content=tool_output,
                    name="blade_python_create",
                    tool_call_id="direct",
                ),
            ],
        }
        tracker.fail(f"blade create python failed: {error_msg[:200]}")
        sync_node_status_to_session(
            state, DIRECT_EXECUTE, f"Injection failed: {error_msg[:200]}",
            detail={"injection_method": "python_agent",
                    "safety_status": "rejected",
                    "reason": "blade_python_create_failed"},
        )
        await sync_to_store(state, result)
        _store = get_global_session_store()
        if _store and task_id:
            _store.append_messages(task_id, result["messages"], node_name=DIRECT_EXECUTE)
        return result

    result = {
        "blade_uid": blade_uid,
        "injection_method": "python_agent",
        "injection_start_time": now_iso(),
        "params": result_params,
        "execute_loop_count": 1,
        "messages": [
            HumanMessage(content=(
                f"[Injection Phase] blade create python succeeded: "
                f"blade_uid={blade_uid} (injection_method=python_agent)"
            )),
            ToolMessage(
                content=tool_output,
                name="blade_python_create",
                tool_call_id="direct",
            ),
        ],
    }
    tracker.complete(f"Direct execute done: blade_uid={blade_uid}")
    sync_node_status_to_session(
        state, DIRECT_EXECUTE, f"Injection completed, blade_uid={blade_uid}",
        detail={"blade_uid": blade_uid, "injection_method": "python_agent",
                "fallback_used": False},
    )
    await sync_to_store(state, result)
    _store = get_global_session_store()
    if _store and task_id:
        _store.append_messages(task_id, result["messages"], node_name=DIRECT_EXECUTE)
    return result


async def direct_execute(state: AgentState) -> dict:
    """Directly invoke blade_create without LLM, replacing execute_loop.

    Constructs blade_create arguments from AgentState's structured
    parameters, calls blade_create.ainvoke(), and extracts the blade_uid.
    """
    task_id = state.get("task_id", "") or ""

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "direct_execute",
        "Direct execute: calling blade_create",
        {},
    )

    # 1. Build blade_create arguments from FaultSpec
    from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec
    spec = read_fault_spec(state) or FaultSpec()
    scope = spec.scope

    # 1a. Capability gate — direct mode used to bypass it entirely (zero calls
    # into ``capabilities``), so a fault domain incompatible with the configured
    # transport reached execution with no check at all. It now applies the same
    # rule as the LLM path: the fault scope's profile must agree with the
    # resolved transport's profile.
    #
    # This deliberately removes the previously-documented direct-only escape
    # (co-located ``kubeconfig`` + a host-profile fault). Consistency was chosen
    # over that convenience — see the module docstring in tools/blade_python.py.
    _profile = resolve_profile_for_state(state)
    if get_environment_profile(_profile) is None:
        message = (
            "The requested fault domain cannot run through the configured "
            "execution transport. Direct mode enforces the same capability "
            f"profile rule as the LLM path. Selected domain: {scope or 'unknown'}; "
            f"resolved capability profile: {_profile}. Configure a transport "
            "whose profile matches the fault domain, or revise the request."
        )
        logger.warning("direct_execute refused: %s", message)
        tracker.fail(message)
        return {
            "safety_status": "rejected",
            **fail_state(
                FailureCategory.PREREQUISITE_FAILED,
                message,
                state.get("messages", []),
                llm_analysis=message,
            ),
        }

    target = spec.blade_target
    action = spec.blade_action
    namespace = spec.namespace
    names = ",".join(spec.names)
    labels_str = ",".join(f"{k}={v}" for k, v in spec.labels.items())
    kubeconfig = state.get("kubeconfig") or ""
    params = dict(spec.params)
    params_flags = list(spec.params_flags)
    target_metadata = state.get("target_metadata") or {}

    # Duration auto-boost
    from chaos_agent.utils.fault_type import ensure_min_duration
    _duration = ensure_min_duration(
        spec.duration_seconds, scope, target, action,
    )
    if _duration != spec.duration_seconds:
        logger.info(
            f"Duration auto-adjusted from {spec.duration_seconds}s to {_duration}s "
            f"for {scope}-{target}-{action}"
        )

    # Required-flag auto-completion
    _completions = _auto_complete_params(scope, target, action, params, params_flags)
    if _completions:
        logger.info(
            "Auto-completed params for %s-%s %s: %s",
            scope, target, action, _completions,
        )

    # Python-application faults branch off BEFORE the k8s/host-specific
    # preparation below: they have no namespace/labels selector, no burn/OOM
    # tuning, no DaemonSet pre-flight and no kubectl-exec fallback. Routing here
    # keeps that cluster-only logic from running against an in-process fault.
    if is_python_scope(scope):
        return await _run_python_agent_create(
            state,
            target=target, action=action,
            params=params, params_flags=params_flags, duration=_duration,
            result_params={
                "scope": scope, "target": target, "action": action,
                "namespace": "", "names": names, "labels": labels_str,
            },
            task_id=task_id, tracker=tracker,
        )

    # Burn parameter adjustments (disk-burn only)
    params, _fcat_ceiling = await _apply_burn_adjustments(
        params, scope, target, action,
        target_metadata, namespace, list(spec.names),
        kubeconfig, task_id, tracker, state,
    )

    # OOMKill risk warning for memory-burn faults: compare burn size
    # against pod memory limit. Only meaningful for target=="mem" where
    # the size param represents memory allocation.
    if is_memory_burn_scope(scope, target):
        memory_limit_mb = (target_metadata or {}).get("pod_memory_limit_mb")
        if memory_limit_mb is None:
            memory_limit_mb = await _fetch_pod_memory_limit_mb(
                namespace=namespace,
                names=list(spec.names),
                labels=dict(spec.labels),
                kubeconfig=kubeconfig,
                task_id=task_id,
            )
        if memory_limit_mb is not None and memory_limit_mb < _OOMKILL_RISK_THRESHOLD_MB:
            burn_size = params.get("size", _BURN_DEFAULT_SIZE)
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
        _session_store = get_global_session_store()
        if _session_store and task_id:
            _mem_msg = (
                f"[OOMKill Risk] Pod memory limit: {memory_limit_mb}MB"
                if memory_limit_mb is not None
                else "[OOMKill Risk] Pod memory limit: not available"
            )
            _session_store.append_messages(
                task_id,
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

    # Parameter observability warning
    warning_fn = _PARAM_OBSERVABILITY_WARNINGS.get((target, action))
    if warning_fn:
        try:
            warning_msg = warning_fn(params)
            if warning_msg:
                tracker.update(f"WARNING: {warning_msg}", {"warning": True})
        except Exception:
            logger.debug("Parameter observability warning check failed", exc_info=True)

    result_params = {
        "scope": scope,
        "target": target,
        "action": action,
        "namespace": namespace,
        "names": names,
        "labels": labels_str,
    }

    # Pre-flight check: for node-scope, verify DaemonSet pod on target node(s)
    if scope == "node" and names:
        from chaos_agent.agent.nodes.execute._injection_detection import (
            discover_tool_pods_cluster_wide_with_nodes,
        )
        _preflight_blocked = False
        try:
            _pods_with_nodes = await discover_tool_pods_cluster_wide_with_nodes(
                kubeconfig, task_id,
            )

            _target_nodes = [n.strip() for n in names.split(",") if n.strip()]
            _available_nodes = {pnode for _, _, pnode in _pods_with_nodes}
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

    # Parse blade_parsed_flags from spec
    blade_parsed_flags = None
    flags_str = args.get("flags", "")
    if flags_str:
        from chaos_agent.utils.fault_type import parse_blade_flags
        parsed = parse_blade_flags(flags_str)
        if parsed:
            blade_parsed_flags = parsed

    return await _run_blade_create_with_fallback(
        state, args, result_params, blade_parsed_flags,
        scope, target, action, namespace, names,
        kubeconfig, params, target_metadata, task_id, tracker,
    )
