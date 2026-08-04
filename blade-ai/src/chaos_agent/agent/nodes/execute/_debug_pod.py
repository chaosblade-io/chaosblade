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
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from chaos_agent.config.settings import settings
from chaos_agent.errors import ToolGuardError, ToolTimeoutError
from chaos_agent.tools.kubectl import build_kubectl_cmd
from chaos_agent.transports import (
    PROFILE_K8S,
    TransportTarget,
    execute_via_transport,
)

logger = logging.getLogger(__name__)

# Default container name used by `kubectl debug node/<node>`
DEBUG_CONTAINER_NAME = "debugger"

# Default namespace for debug pods — always exists in any K8s cluster.
_DEFAULT_DEBUG_NS = "default"

# Project convention: `kubectl debug node/<node>` names the pod
# ``node-debugger-<node>-<suffix>``. Used ONLY as a discovery filter (data
# sources take priority elsewhere); not a creation rule.
DEBUG_POD_NAME_PREFIX = "node-debugger-"


def parse_debug_pod_name(output: str) -> str:
    """Extract debug pod name from kubectl debug output.

    THE single parsing source for every consumer (baseline, verifier, recover,
    and the kubectl tool wrapper itself — task-29848471: the wrapper's old
    private copy had weaker patterns and produced false "no pod created"
    reports). Handles formats like:
      - "Creating debugging pod node-debugger-xxx with container debugger on node yyy."
      - "Starting debugging pod node-name-debug-xxxxx..."
      - "pod/node-name-debug-xxxxx created"
    """
    if not output:
        return ""
    # Most specific first: kubectl's own creation banner (K8s 1.25+), then the
    # generic ``pod/<name> created`` form, then convention/pattern fallbacks.
    for pattern in (
        r"Creating debugging pod\s+(\S+)",
        r"Starting debugging pod\s+(\S+)",
        r"pod/(\S+)\s+created",
        r"pod\s+(node-debugger-\S+)",
        r"(\S+-debug-\S+)\s+created",
    ):
        m = re.search(pattern, output)
        if m:
            return m.group(1).rstrip(".,;:")
    return ""


async def discover_created_debug_pod(
    node_name: str,
    namespace: str,
    created_after_ts: float,
    kubeconfig: str = "",
    context: str = "",
    cluster: str = "",
) -> str:
    """Live fallback discovery for a debug pod whose name failed to parse.

    Per the project convention for debug timeouts/parse failures, run ONE
    ``kubectl get pods`` and match by ``spec.nodeName`` + the
    ``node-debugger-`` prefix. A recency filter
    (``creationTimestamp >= created_after_ts - 60s``, clock-skew margin) is
    added because the same node can host several stale debug pods at once —
    without it discovery could return a leftover from an earlier attempt
    (task-29848471 k3 had two such pods coexisting).

    Returns the NEWEST matching pod name, or ``""`` if none. Only invoked on
    the parse-failure path — the normal path pays zero extra cost.
    """
    ns = namespace or _DEFAULT_DEBUG_NS
    cmd = build_kubectl_cmd(
        "get", ["pods", "-n", ns, "-o", "json"],
        kubeconfig, context, cluster,
    )
    try:
        result = await execute_via_transport(
            cmd, TransportTarget.from_state({}),
            timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
    except Exception:
        logger.debug(
            "Debug pod discovery failed for node %s in %s", node_name, ns,
            exc_info=True,
        )
        return ""
    if result.exit_code != 0:
        return ""
    try:
        data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return ""

    cutoff = datetime.fromtimestamp(
        created_after_ts, tz=timezone.utc,
    ) - timedelta(seconds=60)
    best_name = ""
    best_created: datetime | None = None
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") or {}
        name = metadata.get("name") or ""
        if not name.startswith(DEBUG_POD_NAME_PREFIX):
            continue
        spec = item.get("spec") or {}
        if node_name and spec.get("nodeName") != node_name:
            continue
        raw_ts = metadata.get("creationTimestamp") or ""
        try:
            created = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if created < cutoff:
            continue
        if best_created is None or created > best_created:
            best_name = name
            best_created = created
    if best_name:
        logger.info(
            "Debug pod discovery hit: %s on node %s (ns=%s)",
            best_name, node_name, ns,
        )
    return best_name


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
    meta_match = re.search(r'\[debug-pod-meta:\s*(\{.*?\})\]', tool_message_content)
    if meta_match:
        try:
            metadata = json.loads(meta_match.group(1))
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        # Task-5193538b: a POD-scoped ``kubectl debug`` attaches an
        # EPHEMERAL container to the TARGET pod — no debug pod is created,
        # yet the meta tag still carries the target pod's name/namespace.
        # Treating it as a probe pod made both cleanup paths
        # (planning cleanup + verifier finalize) delete the FAULT TARGET.
        # Mirrors the ephemeral skip in ``execution_artifacts``
        # (artifact collection). Returning empty here is safe: the
        # name-pattern fallback below cannot match (ephemeral debug emits no
        # "Creating debugging pod" banner).
        if metadata.get("ephemeral_container"):
            return ("", "")
        pod_name = str(metadata.get("name") or "")
        namespace = str(metadata.get("namespace") or "")
        if pod_name and namespace:
            return (pod_name, namespace)

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
    _target = TransportTarget.from_state({})
    # Preferred: kubectl wait --for=condition=Ready
    wait_cmd = build_kubectl_cmd("wait", [
        "--for=condition=Ready", f"pod/{pod_name}",
        "-n", ns, f"--timeout={timeout}s",
    ], kubeconfig=kubeconfig)
    try:
        result = await execute_via_transport(
            wait_cmd, _target, timeout=timeout + 10, task_id=task_id,
            expect_profile=PROFILE_K8S,
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
        check_cmd = build_kubectl_cmd("get", [
            pod_name, "-n", ns,
            "-o", "jsonpath={.status.containerStatuses[0].ready}",
        ], kubeconfig=kubeconfig)
        try:
            check_result = await execute_via_transport(
                check_cmd, _target, timeout=settings.timeout_kubectl, task_id=task_id,
                expect_profile=PROFILE_K8S,
            )
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
    _target = TransportTarget.from_state({})
    # Try default first
    cmd = build_kubectl_cmd("get", ["namespace", "default", "--no-headers"],
                            kubeconfig=kubeconfig)
    try:
        result = await execute_via_transport(
            cmd, _target, timeout=settings.timeout_kubectl, task_id=task_id,
            expect_profile=PROFILE_K8S,
        )
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
        result = await execute_via_transport(
            list_cmd, _target, timeout=settings.timeout_kubectl, task_id=task_id,
            expect_profile=PROFILE_K8S,
        )
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
    ns = namespace or await _find_available_namespace(kubeconfig, task_id)
    if not ns:
        logger.warning("No accessible namespace found for debug pod creation")
        return None

    debug_cmd = build_kubectl_cmd("debug", [
        f"node/{node_name}", "-n", ns,
        "--image=busybox", "--", "sleep", "3600",
    ], kubeconfig=kubeconfig)
    _target = TransportTarget.from_state({})
    try:
        debug_result = await execute_via_transport(
            debug_cmd, _target, timeout=settings.timeout_kubectl_exec, task_id=task_id,
            expect_profile=PROFILE_K8S,
        )
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

    # A created but unready pod is not an execution carrier. Clean it up now so
    # callers cannot accidentally exec into an ImagePullBackOff artifact.
    ready = await wait_for_debug_pod_ready(
        pod_name, kubeconfig, task_id, namespace=ns,
    )
    if not ready:
        await delete_debug_pod(pod_name, kubeconfig, task_id, namespace=ns)
        return None
    return (pod_name, ns)


async def delete_debug_pod(
    pod_name: str, kubeconfig: str, task_id: str,
    namespace: str = "",
) -> str:
    """Force-delete a debug pod. Best-effort, logs warning on failure.

    Returns a confirmation outcome so callers can distinguish a confirmed
    removal from an unlanded request:

    - ``"confirmed"`` — the API accepted the delete (exit 0) or reported the
      pod already absent (``NotFound``); the pod is gone.
    - ``"unconfirmed"`` — the delete command did not confirm removal (timeout,
      transport exception, or other non-zero exit). Under an in-progress
      network fault the delete rides the very API path the fault is severing,
      so this usually means "request did not land". Cleanup treats the delete
      as fire-and-forget and does NOT retry — the pod's bounded ``-- sleep``
      lifetime lets an unlanded delete lapse on its own.

    Args:
        namespace: Target namespace. Defaults to ``default`` if empty.
    """
    ns = namespace or _DEFAULT_DEBUG_NS
    del_cmd = build_kubectl_cmd("delete", [
        "pod", pod_name, "-n", ns,
        "--force", "--grace-period=0",
    ], kubeconfig=kubeconfig)
    _target = TransportTarget.from_state({})
    try:
        result = await execute_via_transport(
            del_cmd, _target, timeout=30, task_id=task_id,
            expect_profile=PROFILE_K8S,
        )
    except Exception:
        logger.warning("Failed to delete debug pod %s in namespace %s", pod_name, ns)
        return "unconfirmed"
    if result.exit_code == 0:
        return "confirmed"
    combined = f"{result.stderr or ''} {result.stdout or ''}".lower()
    if "notfound" in combined or "not found" in combined:
        # Pod already gone — deletion goal is satisfied.
        return "confirmed"
    logger.warning(
        "Delete debug pod %s in namespace %s did not confirm removal: %s",
        pod_name, ns, (result.stderr or result.stdout or "")[:200],
    )
    return "unconfirmed"
