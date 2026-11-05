"""Runtime environment information collection (迁移点 7).

Collects runtime environment info for injection into the system prompt,
following the Claude Code pattern of queryContext.ts + envDynamic.ts.
"""

import asyncio

from chaos_agent.config.settings import settings

# Cache: per task_id, only collect once (avoid repeated blade_version calls)
_env_cache: dict[str, dict] = {}


async def compute_env_info(task_id: str = "") -> dict:
    """Collect runtime environment information for system prompt injection.

    Results are cached per task_id to avoid redundant calls.

    Args:
        task_id: Optional task identifier for caching.

    Returns:
        Dict of environment key-value pairs.
    """
    if task_id and task_id in _env_cache:
        return _env_cache[task_id]

    env: dict = {}

    # ChaosBlade version
    env["blade_version"] = await _get_blade_version()

    # K8s availability
    env["k8s_available"] = await _check_k8s_available()

    # Static info from settings — channel name replaces kube_connection_mode.
    # resolve_channel_name returns an explicit "unknown" channel for an invalid
    # override or under-specified target. This keeps environment information
    # available without representing a misconfiguration as the default cluster.
    #
    # NOTE: this reflects the *default* channel from settings — not a per-task
    # override carried in the graph state. It is an informational field for the
    # System Prompt; the authoritative channel is resolved per-task at execution.
    #
    # Local import: avoid a module-level circular import
    # (transports -> config.settings -> ... -> agent.env_info).
    from chaos_agent.transports import (
        KUBEWIZ_CHANNELS,
        PROFILE_HOST,
        profile_of,
        resolve_channel_name,
        resolve_transport_target,
    )
    _channel_name = resolve_channel_name()
    env["kube_connection_mode"] = _channel_name
    if _channel_name in KUBEWIZ_CHANNELS:
        env["kubeconfig_path"] = "(kubewiz)"
        env["kube_context"] = "(kubewiz)"
    else:
        env["kubeconfig_path"] = settings.kubeconfig_path or "(default)"
        env["kube_context"] = settings.kube_context or "(current)"

    # D1: ``_get_blade_version`` probes the agent-LOCAL vendor blade, but a
    # host-scoped drill executes injections with the REMOTE host's blade, whose
    # version can differ (observed: local vendor 1.9.0-alpha vs target host
    # 1.8.2). Presenting the local version as ``blade_version`` misleads the
    # planner into a spurious "agent-side vs host-side blade mismatch" story —
    # the confusion is manufactured by this field, not by any routing error
    # (blade_bin_for already sends host-scope commands to the remote blade).
    # Make the field scope-truthful: for a host channel, probe the blade that
    # actually runs and label the local one as local so the model can never
    # conflate them.
    if profile_of(_channel_name) == PROFILE_HOST:
        host_ver = await _get_host_blade_version(resolve_transport_target(None))
        local_ver = env["blade_version"]
        env["blade_version"] = (
            f"{host_ver} (target host blade; agent-local blade {local_ver})"
            if host_ver
            else f"(target host blade unprobed; agent-local blade {local_ver})"
        )

    env["model_name"] = settings.model_name

    if task_id:
        _env_cache[task_id] = env

    return env


async def _get_blade_version() -> str:
    """Get ChaosBlade version string.

    Resolves via ``settings._resolve_blade_path()`` so the bundled blade
    binary is picked up when ``blade_path`` is empty — otherwise this
    function would always report "not installed" on default configs,
    feeding wrong env info to the system prompt and surfacing a false
    negative in the TUI boot panel.
    """
    blade = settings._resolve_blade_path()
    if not blade:
        return "not installed"
    from chaos_agent.utils.exec_path import resolve_exec_path
    blade = resolve_exec_path(blade)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            blade, "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0 and stdout:
            return stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        # Don't leak the subprocess past the timeout — kill it so the
        # parent isn't holding fds for a stuck blade invocation.
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
    except (FileNotFoundError, OSError):
        pass
    return "not installed"


async def _get_host_blade_version(target) -> str:
    """Probe the blade version ON the target host (host-scoped drills only).

    ``_get_blade_version`` reports the agent-local vendor blade; in a host
    drill the injection actually runs the remote host's blade, whose version
    can differ. Probe it through the transport so the Environment section
    carries the version that will execute. Best-effort by contract: any
    transport/probe failure yields ``""`` and the caller labels the field, so
    prompt assembly never breaks on an unreachable host. ``skip_guard`` mirrors
    the local probe's status as an internal, high-frequency check.
    """
    from chaos_agent.transports.executor import execute_via_transport
    from chaos_agent.transports.registry import PROFILE_HOST

    host_blade = settings.host_blade_path or "blade"
    try:
        result = await execute_via_transport(
            [host_blade, "version"],
            target,
            timeout=10,
            skip_guard=True,
            source="env-host-blade-version",
            # Fail closed: the probe MUST reach the target host. If the channel
            # cannot serve a host profile the command is refused (exit != 0),
            # the helper returns "", and the caller labels the field "unprobed"
            # — never silently reporting the agent-local version as the host's.
            expect_profile=PROFILE_HOST,
        )
        if result.exit_code == 0 and result.stdout:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001 - best-effort probe, never fail prompt assembly
        pass
    return ""


async def _check_k8s_available() -> bool:
    """Check if kubectl can reach a cluster."""
    try:
        from chaos_agent.tools.kubectl_cli import exec_kubectl_raw

        result = await exec_kubectl_raw("cluster-info", [], timeout=10.0)
        return result.exit_code == 0
    except Exception:
        return False


def clear_env_cache(task_id: str = "") -> None:
    """Clear the environment info cache.

    Args:
        task_id: If provided, clear only for this task.
                 If empty, clear the entire cache.
    """
    if task_id:
        _env_cache.pop(task_id, None)
    else:
        _env_cache.clear()
