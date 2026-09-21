"""ChaosBlade tool-pod discovery — framework-side cluster infrastructure.

Pure kubectl-based cluster discovery of the ChaosBlade tool pods (the
``otel-c-tool`` / ``chaosblade-tool`` DaemonSet family). Physically owned by
the tools layer since phase-4 T3: every dependency points DOWNWARD
(transports / tools.kubectl_cli command building / config), so both the generic
nodes (gates / planning / side_effect / baseline / verify) and the provider
execution domains can import it as a plain forward dependency — no
providers→nodes or nodes→nodes sideways coupling for a capability none of
them owns. The transitional re-exports ``nodes/execute/_injection_detection.py``
used to keep were retired with phase-5.

Symbols:
  Constants: TOOL_POD_LABEL_SELECTOR, TOOL_POD_NAMESPACE,
             TOOL_POD_LABEL_CANDIDATES, TOOL_POD_JSONPATH,
             TOOL_POD_PRESENT, TOOL_POD_ABSENT, TOOL_POD_UNKNOWN
  Functions: parse_all_ns_pods, parse_tool_pod_rows
  Async:     discover_tool_pod_on_node, discover_tool_pod_state_on_node,
             discover_tool_pods_cluster_wide,
             discover_tool_pods_cluster_wide_with_nodes
"""

import logging

logger = logging.getLogger(__name__)

# Label selector for ChaosBlade tool pods
TOOL_POD_LABEL_SELECTOR = "app=otel-c-tool"
TOOL_POD_NAMESPACE = "chaosblade"

# Known tool pod label selectors (tried in order)
TOOL_POD_LABEL_CANDIDATES = ["app=chaosblade-tool", "app=otel-c-tool"]

# Three-state tool-pod presence verdict (R69). The health pre-check must
# tell "genuinely not there" (ABSENT → BLOCK) from "could not tell"
# (UNKNOWN → fail-open, never block). A bare ``None`` conflated the two,
# which is why the old hand-rolled check in target_health lied. Callers
# that only need the carrier (execution side) keep using
# ``discover_tool_pod_on_node`` — ABSENT and UNKNOWN both map to None there.
TOOL_POD_PRESENT = "present"
TOOL_POD_ABSENT = "absent"
TOOL_POD_UNKNOWN = "unknown"


def parse_all_ns_pods(output: str) -> list[tuple[str, str]]:
    """Parse kubectl get pods -A --no-headers output.

    Format: NAMESPACE  NAME  READY  STATUS  RESTARTS  AGE

    Returns:
        List of (pod_name, namespace) tuples for Running pods.
    """
    if not output or not isinstance(output, str):
        return []

    result: list[tuple[str, str]] = []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 4:
            namespace = parts[0]
            pod_name = parts[1]
            status = parts[3]
            if status == "Running":
                result.append((pod_name, namespace))
    return result


# Explicit-field jsonpath for tool-pod listing. NEVER use ``-o wide`` +
# positional column parsing here: RESTARTS may carry an annotation like
# ``1 (14d ago)`` whose spaces shift every column after it — a wide parse
# then reads the AGE token as the node name (observed live: pods with
# restarts attributed to node ``123d``, and the target-node carrier went
# unreported by the preplan probe).
TOOL_POD_JSONPATH = (
    "jsonpath={range .items[*]}{.metadata.namespace}|{.metadata.name}"
    "|{.status.phase}|{.spec.nodeName}{'\\n'}{end}"
)


def parse_tool_pod_rows(output: str) -> list[tuple[str, str, str]]:
    """Parse explicit-field jsonpath rows: ``ns|name|phase|node`` per line.

    Fields are delimiter-separated (not column positions), so restart
    annotations or any whitespace in unrelated columns cannot shift them.
    Returns: List of (pod_name, namespace, node_name) tuples for Running pods.
    """
    if not output or not isinstance(output, str):
        return []
    result: list[tuple[str, str, str]] = []
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        ns, name, phase, node = parts[0], parts[1], parts[2], parts[3]
        if phase == "Running" and name:
            result.append((name, ns, node))
    return result


async def discover_tool_pod_state_on_node(
    node_name: str, kubeconfig: str, task_id: str = "",
) -> tuple[str, tuple[str, str] | None]:
    """Three-state tool-pod presence on ``node_name`` (single authority).

    Returns ``(state, found)`` where ``state`` is one of
    ``TOOL_POD_PRESENT`` / ``TOOL_POD_ABSENT`` / ``TOOL_POD_UNKNOWN`` and
    ``found`` is the ``(pod_name, namespace)`` carrier when present, else
    ``None``:

      - PRESENT: a Running tool pod matched the node.
      - ABSENT: at least one label query SUCCEEDED (transport ok, exit 0)
        but no Running pod matched the node — the tool is genuinely not
        there. Safe to BLOCK on.
      - UNKNOWN: every label query FAILED (exception or non-zero exit) —
        cannot tell. Callers MUST fail-open, never BLOCK on this.

    This is the authoritative implementation; ``discover_tool_pod_on_node``
    is a thin carrier-only wrapper kept for the execution side.
    """
    # Function-local imports (kept from the pre-migration implementation):
    # tests patch the ORIGIN module attributes
    # (``chaos_agent.transports.execute_via_transport`` /
    # ``chaos_agent.tools.kubectl_cli.build_kubectl_cmd``) and rely on the
    # call-time lookup these local imports perform.
    from chaos_agent.config.settings import settings
    from chaos_agent.tools.kubectl_cli import build_kubectl_cmd
    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )

    _target = TransportTarget.from_state({})
    any_query_ok = False
    for label in TOOL_POD_LABEL_CANDIDATES:
        cmd = build_kubectl_cmd("get", [
            "pods", "-A", "-l", label, "--no-headers", "-o", TOOL_POD_JSONPATH,
        ], kubeconfig=kubeconfig)
        try:
            result = await execute_via_transport(
                cmd, _target,
                timeout=settings.timeout_kubectl,
                task_id=task_id,
                source="baseline-capture",
                expect_profile=PROFILE_K8S,
            )
        except Exception as e:
            logger.warning(
                "Failed to discover tool pods on node %s with label %s: %s",
                node_name, label, e,
            )
            continue
        if result.exit_code != 0:
            # Non-zero exit is a failed query, not an empty answer — do not
            # let it masquerade as "tool absent" (that would turn a
            # connectivity blip into a false BLOCK downstream).
            logger.warning(
                "Tool-pod discovery on node %s with label %s returned exit=%s",
                node_name, label, result.exit_code,
            )
            continue
        any_query_ok = True
        pods = parse_tool_pod_rows(result.stdout)
        for pod_name, ns, node in pods:
            if node == node_name:
                return (TOOL_POD_PRESENT, (pod_name, ns))
    return ((TOOL_POD_ABSENT if any_query_ok else TOOL_POD_UNKNOWN), None)


async def discover_tool_pod_on_node(
    node_name: str, kubeconfig: str, task_id: str = "",
) -> tuple[str, str] | None:
    """Find a Running ChaosBlade tool pod on the specified node (cluster-wide).

    Carrier-only wrapper over ``discover_tool_pod_state_on_node``: returns
    the ``(pod_name, namespace)`` tuple when PRESENT, else ``None`` (both
    ABSENT and UNKNOWN collapse to None — the execution side has no use
    for the distinction, it simply cannot proceed without a carrier).
    """
    _state, found = await discover_tool_pod_state_on_node(
        node_name, kubeconfig, task_id,
    )
    return found


async def discover_tool_pods_cluster_wide(
    kubeconfig: str, task_id: str = "",
) -> list[tuple[str, str]]:
    """Discover ChaosBlade tool pods across all namespaces.

    Tries known label selectors in order, returns on first success.
    Uses -A (all-namespaces) to avoid hardcoding the namespace.

    Returns:
        List of (pod_name, namespace) tuples for Running pods.
    """
    from chaos_agent.config.settings import settings
    from chaos_agent.tools.kubectl_cli import build_kubectl_cmd
    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )

    _target = TransportTarget.from_state({})
    for label in TOOL_POD_LABEL_CANDIDATES:
        cmd = build_kubectl_cmd("get", [
            "pods", "-A", "-l", label, "--no-headers",
        ], kubeconfig=kubeconfig)
        result = await execute_via_transport(
            cmd, _target,
            timeout=settings.timeout_kubectl,
            task_id=task_id,
            source="conflict-check",
            expect_profile=PROFILE_K8S,
        )
        pods = parse_all_ns_pods(result.stdout)
        if pods:
            return pods
    return []


async def discover_tool_pods_cluster_wide_with_nodes(
    kubeconfig: str, task_id: str = "",
) -> list[tuple[str, str, str]]:
    """Discover ChaosBlade tool pods across all namespaces with node info.

    Tries known label selectors in order, returns on first success.
    Uses -A (all-namespaces) and an explicit-field jsonpath so restart
    annotations cannot shift the node column (see ``TOOL_POD_JSONPATH``).

    Returns:
        List of (pod_name, namespace, node_name) tuples for Running pods.
    """
    from chaos_agent.config.settings import settings
    from chaos_agent.tools.kubectl_cli import build_kubectl_cmd
    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )

    _target = TransportTarget.from_state({})
    for label in TOOL_POD_LABEL_CANDIDATES:
        cmd = build_kubectl_cmd("get", [
            "pods", "-A", "-l", label, "--no-headers", "-o", TOOL_POD_JSONPATH,
        ], kubeconfig=kubeconfig)
        try:
            result = await execute_via_transport(
                cmd, _target,
                timeout=settings.timeout_kubectl,
                task_id=task_id,
                source="tool-pod-discovery",
                expect_profile=PROFILE_K8S,
            )
        except Exception as e:
            logger.warning("Failed to discover tool pods with label %s: %s", label, e)
            continue
        pods = parse_tool_pod_rows(result.stdout)
        if pods:
            return pods
    return []
