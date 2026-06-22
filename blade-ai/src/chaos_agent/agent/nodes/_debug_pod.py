"""Shared debug pod lifecycle management.

Provides public functions for creating, waiting, and deleting debug pods
on Kubernetes nodes. These are used by both baseline_capture and verifier
modules to avoid duplication (DRY principle).

Debug pods are created via `kubectl debug node/<node>` and provide host-level
filesystem access for verification commands. The host filesystem is typically
mounted at `/host/` inside the debug pod.

Debug pods are NOT tied to any specific namespace (e.g. ChaosBlade).
They are created in the ``default`` namespace (which always exists in any
K8s cluster) unless an explicit namespace is provided. The namespace is
recorded at creation time and used for subsequent wait/delete operations.
"""

import asyncio
import logging
import re

from chaos_agent.config.settings import settings
from chaos_agent.errors import ToolGuardError, ToolTimeoutError
from chaos_agent.tools.kubectl import _adapt_kubewiz_result, build_kubectl_cmd
from chaos_agent.tools.shell import run_command

logger = logging.getLogger(__name__)

# Default container name used by `kubectl debug node/<node>`
DEBUG_CONTAINER_NAME = "debugger"

# Default namespace for debug pods — always exists in any K8s cluster.
_DEFAULT_DEBUG_NS = "default"


def parse_debug_pod_name(output: str) -> str:
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


def parse_debug_pod_info(tool_message_content: str) -> tuple[str, str]:
    """Extract debug pod name AND namespace from a ToolMessage content block.

    The ToolMessage typically contains the full kubectl command invocation
    (with ``-n <namespace>``) followed by the output (containing the pod name).
    The kubectl tool also appends a structured ``[debug-pod-ns: <ns>]`` tag
    for reliable namespace extraction.

    Returns:
        (pod_name, namespace) tuple. namespace defaults to "default"
        if not found in the message text.
    """
    pod_name = parse_debug_pod_name(tool_message_content)
    if not pod_name:
        return ("", "")
    # Priority 1: structured tag appended by kubectl tool
    ns_tag = re.search(r'\[debug-pod-ns:\s*(\S+)\]', tool_message_content)
    if ns_tag:
        return (pod_name, ns_tag.group(1))
    # Priority 2: -n / --namespace flag in the message text
    ns_match = re.search(r'(?:-n\s+|--namespace[=\s])(\S+)', tool_message_content)
    if ns_match:
        return (pod_name, ns_match.group(1))
    # Fallback: kubectl default namespace
    return (pod_name, "default")


async def wait_for_debug_pod_ready(
    pod_name: str, kubeconfig: str, task_id: str,
    timeout: int = 60, namespace: str = "",
) -> bool:
    """Wait for debug pod container to be ready before exec.

    kubectl debug returns after creating the Pod object in etcd, NOT after
    the container is running.  This wait bridges the gap.
    Best-effort: returns False on timeout, caller still tries exec.
    """
    ns = namespace or _DEFAULT_DEBUG_NS
    # Preferred: kubectl wait --for=condition=Ready
    wait_cmd = build_kubectl_cmd("wait", [
        "--for=condition=Ready", f"pod/{pod_name}",
        "-n", ns, f"--timeout={timeout}s",
    ], kubeconfig=kubeconfig)
    try:
        result = await run_command(
            wait_cmd, timeout=timeout + 10, task_id=task_id,
        )
        result = _adapt_kubewiz_result(result)
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
        check_cmd = build_kubectl_cmd("get", [
            pod_name, "-n", ns,
            "-o", "jsonpath={.status.containerStatuses[0].ready}",
        ], kubeconfig=kubeconfig)
        try:
            check_result = await run_command(
                check_cmd, timeout=settings.timeout_kubectl, task_id=task_id,
            )
            check_result = _adapt_kubewiz_result(check_result)
            if check_result.stdout.strip() == "true":
                return True
        except (ToolGuardError, ToolTimeoutError):
            continue

    logger.warning(
        "Debug pod %s not ready after wait, will try exec anyway", pod_name,
    )
    return False


async def _find_available_namespace(kubeconfig: str, task_id: str) -> str:
    """Find an accessible namespace in the cluster for debug pod creation.

    Tries ``default`` first (always exists in standard K8s clusters).
    If not accessible, lists all namespaces and picks the first Active one.
    Returns the namespace name, or empty string if none found.
    """
    # Try default first
    cmd = build_kubectl_cmd("get", ["namespace", "default", "--no-headers"],
                            kubeconfig=kubeconfig)
    try:
        result = await run_command(cmd, timeout=settings.timeout_kubectl, task_id=task_id)
        result = _adapt_kubewiz_result(result)
        if result.exit_code == 0:
            return "default"
    except Exception:
        pass

    # Fallback: list all namespaces, pick first Active one
    list_cmd = build_kubectl_cmd("get", [
        "namespaces", "--no-headers",
        "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase",
    ], kubeconfig=kubeconfig)
    try:
        result = await run_command(list_cmd, timeout=settings.timeout_kubectl, task_id=task_id)
        result = _adapt_kubewiz_result(result)
        if result.exit_code == 0:
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "Active":
                    return parts[0]
                elif len(parts) == 1:
                    return parts[0]
    except Exception:
        pass

    return ""


async def create_and_wait_debug_pod(
    node_name: str, kubeconfig: str, task_id: str,
    namespace: str = "",
) -> tuple[str, str] | None:
    """Create a debug pod on the specified node and wait for it to be ready.

    If the specified namespace doesn't exist, automatically discovers an
    available namespace in the cluster. Records and returns (pod_name,
    namespace) so callers can delete it from the correct namespace later.

    Returns (pod_name, namespace) tuple or None if creation failed.
    Host filesystem is mounted at /host/ inside the pod.
    """
    ns = namespace or _DEFAULT_DEBUG_NS

    # Find a namespace that actually exists
    ns = await _find_available_namespace(kubeconfig, task_id)
    if not ns:
        logger.warning("No accessible namespace found for debug pod creation")
        return None

    debug_cmd = build_kubectl_cmd("debug", [
        f"node/{node_name}", "-n", ns,
        "--image=busybox", "--", "sleep", "3600",
    ], kubeconfig=kubeconfig)
    try:
        debug_result = await run_command(
            debug_cmd, timeout=settings.timeout_kubectl_exec, task_id=task_id,
        )
        debug_result = _adapt_kubewiz_result(debug_result)
    except (ToolGuardError, ToolTimeoutError) as e:
        logger.warning(
            "Failed to create debug pod for node %s: %s", node_name, e,
        )
        return None
    except Exception as e:
        logger.warning(
            "Failed to create debug pod for node %s: %s", node_name, e,
        )
        return None

    if debug_result.exit_code != 0:
        logger.warning(
            "Failed to create debug pod for node %s: %s",
            node_name, debug_result.stderr[:200],
        )
        return None

    pod_name = parse_debug_pod_name(debug_result.stdout)
    if not pod_name:
        logger.warning(
            "Failed to parse debug pod name from: %s",
            debug_result.stdout[:200],
        )
        return None

    # Critical fix: wait for container readiness (prevents race condition)
    await wait_for_debug_pod_ready(pod_name, kubeconfig, task_id, namespace=ns)
    return (pod_name, ns)


async def delete_debug_pod(
    pod_name: str, kubeconfig: str, task_id: str,
    namespace: str = "",
) -> None:
    """Force-delete a debug pod. Best-effort, logs warning on failure.

    Args:
        namespace: Target namespace. Defaults to ``default`` if empty.
    """
    ns = namespace or _DEFAULT_DEBUG_NS
    del_cmd = build_kubectl_cmd("delete", [
        "pod", pod_name, "-n", ns,
        "--force", "--grace-period=0",
    ], kubeconfig=kubeconfig)
    try:
        await run_command(del_cmd, timeout=30, task_id=task_id)
    except Exception:
        logger.warning("Failed to delete debug pod %s in namespace %s", pod_name, ns)
