"""Runtime environment information collection (迁移点 7).

Collects runtime environment info for injection into the system prompt,
following the Claude Code pattern of queryContext.ts + envDynamic.ts.
"""

import asyncio
import logging

from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)

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

    # Static info from settings
    env["kube_connection_mode"] = settings.kube_connection_mode
    if settings.kube_connection_mode == "kubewiz":
        env["kubeconfig_path"] = "(kubewiz)"
        env["kube_context"] = "(kubewiz)"
    else:
        env["kubeconfig_path"] = settings.kubeconfig_path or "(default)"
        env["kube_context"] = settings.kube_context or "(current)"
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


async def _check_k8s_available() -> bool:
    """Check if kubectl can reach a cluster."""
    try:
        from chaos_agent.tools.kubectl import exec_kubectl_raw

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
