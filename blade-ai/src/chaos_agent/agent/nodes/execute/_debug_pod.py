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
from chaos_agent.tools.kubectl_cli import build_kubectl_cmd
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

# Fallback image when neither settings nor discovery provides one.
_DEFAULT_DEBUG_IMAGE = "busybox"

# Container waiting reasons that make a debug pod deterministically dead —
# no amount of further waiting helps (image cannot be pulled / entrypoint
# cannot start). Detected during the readiness wait so a doomed candidate is
# abandoned in seconds instead of burning the full timeout (#23/#28: every
# doomed busybox pod cost a whole 60s wait).
_DETERMINISTIC_FAILURE_REASONS = frozenset({
    "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "CrashLoopBackOff", "CreateContainerConfigError", "CreateContainerError",
})


def _resolve_debug_pod_images() -> list[str]:
    """Ordered candidate images for framework-created debug pods.

    Priority: explicit ``settings.debug_pod_image`` (manual override — a
    single candidate, honoured exactly as configured) > every entry of
    ``settings.recovery_carrier_discovered_images`` in stored order >
    ``busybox`` (historic default).

    A HEALTHY DaemonSet proves its images are cached on every schedulable
    node (same proof recovery-carrier relies on), so every discovered
    candidate pulls without network. What it does NOT prove is that the
    image can host the ``-- sleep N`` skeleton: discovery is alphabetical
    (probe merges with ``sorted``) and probe deliberately skips toolchain
    verification — a Go single-binary DaemonSet image (NPD/CSI/kube-proxy)
    can rank first while lacking ``sleep`` or overriding the entrypoint so
    ``--`` args crash on startup. The caller therefore tries candidates in
    order with fast-fail readiness detection instead of trusting the first
    (restricted-network reality: first candidate here is
    ack-node-problem-detector, the known-good terway ranks last).
    """
    explicit = str(settings.debug_pod_image or "").strip()
    if explicit:
        return [explicit]
    ordered: list[str] = []
    for img in str(settings.recovery_carrier_discovered_images or "").split(","):
        img = img.strip()
        if img and img not in ordered:
            ordered.append(img)
    if _DEFAULT_DEBUG_IMAGE not in ordered:
        ordered.append(_DEFAULT_DEBUG_IMAGE)
    return ordered

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
        kubeconfig,
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


def parse_debug_pod_info(tool_message_content: str) -> tuple[str, str, bool]:
    """Extract debug pod name, namespace AND tool-cleaned flag from a
    ToolMessage content block.

    The ToolMessage typically contains the full kubectl command invocation
    (with ``-n <namespace>``) followed by the output (containing the pod name).
    The kubectl tool also appends a structured ``[debug-pod-ns: <ns>]`` tag
    for reliable namespace extraction.

    Returns:
        (pod_name, namespace, tool_cleaned) tuple. namespace defaults to
        "default" if not found in the message text. ``tool_cleaned`` is True
        ONLY when the ``[debug-pod-meta]`` tag explicitly declares the kubectl
        tool already removed the pod (the one-shot branch auto-deletes its
        probe pod and sets ``cleaned: true``) — cleanup scanners must skip
        those pods or they fire redundant NotFound deletes against a pod
        that no longer exists (#31: 18 such wasted deletes across inject
        finalize + recover). The name-pattern fallback path has no meta, so
        it conservatively returns False (unknown → still cleanable).
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
            return ("", "", False)
        pod_name = str(metadata.get("name") or "")
        namespace = str(metadata.get("namespace") or "")
        if pod_name and namespace:
            return (pod_name, namespace, bool(metadata.get("cleaned")))

    pod_name = parse_debug_pod_name(tool_message_content)
    if not pod_name:
        return ("", "", False)
    # Priority 1: structured tag appended by kubectl tool
    ns_tag = re.search(r'\[debug-pod-ns:\s*(\S+)\]', tool_message_content)
    if ns_tag:
        return (pod_name, ns_tag.group(1), False)
    # Priority 2: -n / --namespace flag in the message text
    ns_match = re.search(r'(?:-n\s+|--namespace[=\s])(\S+)', tool_message_content)
    if ns_match:
        return (pod_name, ns_match.group(1), False)
    # Fallback: kubectl default namespace
    return (pod_name, "default", False)


async def wait_for_debug_pod_ready(
    pod_name: str, kubeconfig: str, task_id: str,
    timeout: int = 60, namespace: str = "",
) -> bool:
    """Wait for debug pod container to be ready before exec.

    kubectl debug returns after creating the Pod object in etcd, NOT after
    the container is running.  This wait bridges the gap.  Best-effort:
    returns False on timeout.

    The debug pod's command is ``sleep N`` (never exits on its own), so any
    non-zero restart or a terminal waiting reason means the container is
    deterministically dead — the image cannot host the skeleton.  That is
    detected within the first polling window (seconds) instead of burning
    the whole timeout, which is what lets ``create_and_wait_debug_pod``
    abandon a doomed image candidate cheaply and try the next one.
    """
    ns = namespace or _DEFAULT_DEBUG_NS
    _target = TransportTarget.from_state({})

    # Combined probe: ready flag | restart count | waiting reason.
    # The `pod/` prefix is REQUIRED: debug pod names are
    # `node-debugger-<node>-<suffix>` and kubectl parses a bare first token
    # as `<resource-type>-...`, e.g. `get node-debugger-cn-sh.foo` -> resource
    # type "node-debugger-cn-sh" -> "server doesn't have a resource type"
    # (observed live in task inject-43173315: all Phase A probes returned
    # empty and fast-fail never fired; only Phase B's prefixed `kubectl wait`
    # saved the run).
    status_cmd = build_kubectl_cmd("get", [
        f"pod/{pod_name}", "-n", ns,
        "-o", "jsonpath={.status.containerStatuses[0].ready}"
              "|{.status.containerStatuses[0].restartCount}"
              "|{.status.containerStatuses[0].state.waiting.reason}",
    ], kubeconfig=kubeconfig)

    async def _probe() -> list[str]:
        try:
            result = await execute_via_transport(
                status_cmd, _target, timeout=settings.timeout_kubectl,
                task_id=task_id, expect_profile=PROFILE_K8S,
            )
        except (ToolGuardError, ToolTimeoutError):
            return ["", "", ""]
        return (result.stdout or "").strip().split("|") + ["", "", ""]

    def _dead(parts: list[str]) -> str | None:
        if len(parts) > 2 and parts[2] in _DETERMINISTIC_FAILURE_REASONS:
            return parts[2]
        # sleep-only skeleton: a restart implies the container already died
        # once (entrypoint crash) — waiting for the next backoff cycle buys
        # nothing.
        if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) >= 1:
            return f"restartCount={parts[1]}"
        return None

    # Phase A — fast-fail window: catch ready success and deterministic
    # failures within seconds of creation (scheduling + image start).
    for _ in range(4):
        await asyncio.sleep(3)
        parts = await _probe()
        if parts and parts[0] == "true":
            return True
        reason = _dead(parts)
        if reason:
            logger.warning(
                "Debug pod %s failed deterministically (%s), not waiting further",
                pod_name, reason,
            )
            return False

    # Phase B — still transiently Pending/ContainerCreating (no failure
    # signal): delegate the remainder to one long kubectl wait (fewest API
    # calls for the slow-scheduling case).
    remaining = max(timeout - 12, 5)
    wait_cmd = build_kubectl_cmd("wait", [
        "--for=condition=Ready", f"pod/{pod_name}",
        "-n", ns, f"--timeout={remaining}s",
    ], kubeconfig=kubeconfig)
    try:
        result = await execute_via_transport(
            wait_cmd, _target, timeout=remaining + 10, task_id=task_id,
            expect_profile=PROFILE_K8S,
        )
        if result.exit_code == 0:
            return True
    except (ToolGuardError, ToolTimeoutError):
        logger.info(
            "kubectl wait blocked/timed out for %s, doing a final probe",
            pod_name,
        )

    # Phase C — the long wait covers neither: a pod that flipped to a
    # terminal state while we were waiting.
    parts = await _probe()
    if parts and parts[0] == "true":
        return True
    reason = _dead(parts)
    if reason:
        logger.warning(
            "Debug pod %s failed deterministically after wait (%s)",
            pod_name, reason,
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

    # Try image candidates in order (explicit config > discovered > busybox)
    # with fast-fail readiness detection: a candidate that cannot host the
    # sleep skeleton (missing sleep binary / entrypoint crash / unpullable)
    # is abandoned in seconds and cleaned up, then the next candidate runs.
    # Deterministic-order discovery is alphabetical, so the known-good image
    # may rank last — the retry loop is what makes the chain reliable.
    #
    # ``--profile=sysadmin`` (B49): the executor-phase LLM path already
    # creates sysadmin debug pods, and read-only host-level probes (iptables
    # reads, nsenter into host namespaces) REQUIRE that privilege level — a
    # bare debug container fails them deterministically ("Permission denied
    # (you must be root)", case #33 baseline). One "debug pod" concept, one
    # capability level: probes are still content-gated by the readonly
    # classifier; privilege only widens which read-only shapes CAN run.
    for image in _resolve_debug_pod_images():
        debug_cmd = build_kubectl_cmd("debug", [
            f"node/{node_name}", "-n", ns,
            f"--image={image}", "--profile=sysadmin", "--", "sleep", "3600",
        ], kubeconfig=kubeconfig)
        _target = TransportTarget.from_state({})
        try:
            debug_result = await execute_via_transport(
                debug_cmd, _target, timeout=settings.timeout_kubectl_exec,
                task_id=task_id, expect_profile=PROFILE_K8S,
            )
        except (ToolGuardError, ToolTimeoutError) as e:
            logger.warning(
                "Failed to create debug pod with image %s on node %s: %s",
                image, node_name, e,
            )
            continue
        except Exception as e:
            logger.warning(
                "Failed to create debug pod with image %s on node %s: %s",
                image, node_name, e,
            )
            continue

        if debug_result.exit_code != 0:
            logger.warning(
                "Failed to create debug pod with image %s on node %s: %s",
                image, node_name, debug_result.stderr[:200],
            )
            continue

        pod_name = parse_debug_pod_name(debug_result.stdout)
        if not pod_name:
            # Parse failure is image-INDEPENDENT (kubectl output shape), so
            # retrying other candidates would only leak additional
            # unparseable (hence uncleanable) pods — one per candidate.
            # Bail out with the historic single-failure semantics instead.
            logger.warning(
                "Failed to parse debug pod name from: %s",
                debug_result.stdout[:200],
            )
            break

        # A created but unready pod is not an execution carrier. Clean it up
        # so callers cannot accidentally exec into a dead artifact, then try
        # the next image candidate.
        ready = await wait_for_debug_pod_ready(
            pod_name, kubeconfig, task_id, namespace=ns,
        )
        if ready:
            return (pod_name, ns)
        await delete_debug_pod(pod_name, kubeconfig, task_id, namespace=ns)
        logger.warning(
            "Debug pod image %s not ready on node %s, trying next candidate",
            image, node_name,
        )
    logger.warning(
        "No debug pod image candidate could host the sleep skeleton on node %s",
        node_name,
    )
    return None


async def delete_debug_pod(
    pod_name: str, kubeconfig: str, task_id: str,
    namespace: str = "",
    kind: str = "pod",
) -> str:
    """Force-delete a debug pod (or another task vehicle, e.g. an occupant
    Deployment). Best-effort, logs warning on failure.

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
        kind, pod_name, "-n", ns,
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
