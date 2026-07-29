"""Baseline command execution layer: run resolved baseline commands and return
observation dicts.

Split out of ``baseline_capture.py`` (Phase 2 module split). Owns the actual
execution mechanics — simple kubectl, host-shell diagnostics, debug-pod
two-step (create → exec → delete) with pod reuse, tool-pod fallback, and the
BusyBox/proc iostat degrade chain. Depends only on the transport / kubectl /
debug-pod primitives and the command data layer (``_commands``) — never on
``baseline_capture`` — so there is no import cycle.
"""

from __future__ import annotations

import logging
import re
import shlex

from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.nodes.baseline._commands import (
    _HOST_FALLBACK_CHAIN,
    _get_iostat_fallback_chain,
    _is_observation_success,
)
from chaos_agent.agent.nodes.execute._debug_pod import (
    DEBUG_CONTAINER_NAME,
    create_and_wait_debug_pod,
    delete_debug_pod,
    parse_debug_pod_name,
    wait_for_debug_pod_ready,
)
from chaos_agent.agent.nodes.execute._injection_detection import (
    discover_tool_pod_on_node,
)
from chaos_agent.config.settings import settings
from chaos_agent.observability.status_tracker import get_tracker
from chaos_agent.tools.kubectl import _split_args, build_kubectl_cmd, display_cmd
from chaos_agent.transports import (
    PROFILE_HOST,
    PROFILE_K8S,
    TransportTarget,
    execute_via_transport,
)

logger = logging.getLogger(__name__)


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
    tracker = get_tracker(task_id)
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
                # Append a skipped-marker observation so ``observations`` stays
                # index-aligned with ``commands`` — downstream callers pair the
                # two by ``zip`` (extractor merge, LLM retry), and dropping the
                # entry here would silently misalign resolved[i] with obs[i].
                observations.append({
                    "description": cmd_info["description"],
                    "command": cmd_info.get("command", ""),
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": "skipped: unresolved template variable(s)",
                })
                if tracker:
                    tracker.update(
                        f"[{idx}/{len(commands)}] Skipped (unresolved): "
                        f"{cmd_info['description']}",
                        {"step": idx, "description": cmd_info["description"],
                         "status": "skipped"},
                    )
                continue

            await dispatch_node_message(
                "baseline_capture",
                f"[{idx}/{len(commands)}] 正在采集: {cmd_info['description']}\n\n",
            )
            try:
                if cmd_info.get("profile") == "host":
                    # Host profile: run the diagnostic verbatim on the target
                    # host via the transport layer. No debug pod, no tool pod
                    # discovery — the connected host IS the target.
                    obs = await _exec_host_simple(cmd_info, task_id)
                elif cmd_info["mode"] == "debug_two_step":
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
                _cmd_display = obs.get('command', '') or ''
                _msg_parts = [f"[{idx}/{len(commands)}] {obs['description']}: {_status}"]
                if _cmd_display:
                    _msg_parts.append(f"  命令: `{_cmd_display}`")
                if _status != "ok" and obs.get('stderr'):
                    _stderr_short = (obs['stderr'] or '')[:200]
                    _msg_parts.append(f"  stderr: {_stderr_short}")
                await dispatch_node_message(
                    "baseline_capture",
                    "\n".join(_msg_parts) + "\n\n",
                )
            except Exception as e:
                obs = {
                    "description": cmd_info["description"],
                    # ``command`` is the resolved-dict key (there is no
                    # ``_full_command``); the stale name silently blanked the
                    # command shown on unexpected-exception observations.
                    "command": cmd_info.get("command", ""),
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
                await dispatch_node_message(
                    "baseline_capture",
                    f"[{idx}/{len(commands)}] {obs['description']}: error\n\n",
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
    _target = TransportTarget.from_state({})
    result = await execute_via_transport(
        cmd, _target, timeout=timeout, task_id=task_id,
        # kubectl-shaped: a host channel cannot serve cluster reads.
        expect_profile=PROFILE_K8S,
    )

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
                fb_result = await execute_via_transport(
                    fb_cmd, _target, timeout=settings.timeout_kubectl_exec,
                    task_id=task_id, expect_profile=PROFILE_K8S,
                )
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


async def _exec_host_simple(cmd_info: dict, task_id: str) -> dict:
    """Execute a host-profile diagnostic verbatim on the target host.

    The command is a plain read-only shell diagnostic (validated by
    ``validate_command`` for the host profile, or a registry entry) and is
    dispatched through the transport layer with ``skip_guard=True`` — the
    host diagnostic binaries (top/free/iostat/ss/ip/ps/uptime/cat) are not
    on the ToolGuard top-level whitelist, mirroring preflight's read-only
    host connectivity probe. Safety is enforced upstream by the host
    whitelist + shell-metachar rejection in ``validate_command``.

    On non-zero exit, best-effort degrades to a ``/proc`` read via
    ``_HOST_FALLBACK_CHAIN`` (e.g. ``iostat -xd 1 2`` → ``cat /proc/diskstats``)
    for minimal hosts that lack the primary binary.
    """
    command = cmd_info["command"]
    _target = TransportTarget.from_state({})

    async def _run(cmd_str: str):
        try:
            argv = shlex.split(cmd_str)
        except ValueError:
            argv = cmd_str.split()
        return await execute_via_transport(
            argv, _target, timeout=settings.command_timeout,
            task_id=task_id, skip_guard=True, source="baseline-host",
            # A bare host diagnostic. On a cluster-addressing channel it
            # would be answered by the platform executor, i.e. the BASELINE
            # would describe the wrong machine (task-46317228).
            expect_profile=PROFILE_HOST,
        )

    result = await _run(command)

    if result.exit_code != 0:
        for fb in _HOST_FALLBACK_CHAIN.get(command, []):
            logger.info(
                "host diagnostic '%s' failed, retrying with fallback: %s",
                command, fb,
            )
            fb_result = await _run(fb)
            if fb_result.exit_code == 0:
                return {
                    "description": cmd_info["description"],
                    "command": fb,
                    "exit_code": 0,
                    "stdout": fb_result.stdout,
                    "stderr": fb_result.stderr,
                }

    return {
        "description": cmd_info["description"],
        "command": command,
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
        _target = TransportTarget.from_state({})
        exec_result = await execute_via_transport(
            exec_cmd, _target, timeout=settings.timeout_kubectl_exec, task_id=task_id,
            expect_profile=PROFILE_K8S,
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
                    fb_cmd = build_kubectl_cmd("exec", ["-c", _DEBUG_CONTAINER_NAME] + _split_args(fb_v_args), kubeconfig=kubeconfig)
                    fb_result = await execute_via_transport(
                        fb_cmd, _target, timeout=settings.timeout_kubectl_exec,
                        task_id=task_id, expect_profile=PROFILE_K8S,
                    )
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
    _target = TransportTarget.from_state({})
    exec_result = await execute_via_transport(
        exec_cmd, _target, timeout=settings.timeout_kubectl_exec, task_id=task_id,
        expect_profile=PROFILE_K8S,
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
                fb_cmd = build_kubectl_cmd("exec", fb_v_args, kubeconfig=kubeconfig)
                fb_result = await execute_via_transport(
                    fb_cmd, _target, timeout=settings.timeout_kubectl_exec,
                    task_id=task_id, expect_profile=PROFILE_K8S,
                )
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
    _target = TransportTarget.from_state({})
    exec_result = await execute_via_transport(
        exec_cmd, _target, timeout=settings.timeout_kubectl_exec, task_id=task_id,
        expect_profile=PROFILE_K8S,
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
                fb_cmd = build_kubectl_cmd("exec", ["-c", _DEBUG_CONTAINER_NAME] + _split_args(fb_v_args), kubeconfig=kubeconfig)
                fb_result = await execute_via_transport(
                    fb_cmd, _target, timeout=settings.timeout_kubectl_exec,
                    task_id=task_id, expect_profile=PROFILE_K8S,
                )
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


__all__ = [
    "_execute_observations",
    "_exec_simple",
    "_exec_host_simple",
    "_exec_debug_two_step",
    "_exec_in_tool_pod",
    "_exec_in_debug_pod",
    "_parse_debug_pod_name",
    "_wait_for_debug_pod_ready",
    "_create_and_wait_debug_pod",
    "_delete_debug_pod",
    "_DEBUG_CONTAINER_NAME",
]
