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

from chaos_agent.agent.execution_artifacts import debug_meta_scope
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

# ---------------------------------------------------------------------------
# Carrier-creation failure reasons
# ---------------------------------------------------------------------------
#
# ``create_and_wait_debug_pod`` retries over an IMAGE candidate chain, so that
# retry dimension can only cure failures whose cause IS the image. Case #67
# (W-67-1) aimed the carrier at a pod name instead of a node name: the API
# answered ``Error from server (NotFound): nodes "<POD>" not found`` and the
# loop rotated every candidate, reporting the identical rejection once per
# image, because each non-zero exit took an unconditional ``continue``.
#
# The "image-INDEPENDENT → stop the chain" principle already existed in that
# loop for parse failures; it was simply never generalized. These constants are
# the generalization, and they carry a second duty: telling the CALLER which
# fact was disproven, so a fallback resting on the same fact can be skipped
# instead of independently "confirming" it.

#: Success — no failure to report.
CARRIER_OK = ""
#: The node the carrier anchors on does not exist (or no longer does). No
#: image can fix this, and no pod can ever carry ``spec.nodeName == <it>``
#: either, so node-scoped fallbacks are provably futile and MUST be skipped:
#: running them turns one wrong name into a confident-looking second opinion
#: (``discover_tool_pod_state_on_node`` would answer ABSENT, which its
#: contract marks as safe to BLOCK on).
CARRIER_TARGET_MISSING = "target_missing"
#: The API rejected the caller or the verb (Forbidden / Unauthorized /
#: MethodNotSupported). Image-independent — stop the chain — but the target is
#: real, so a fallback using a different verb (``get pods`` instead of creating
#: an ephemeral container) may still be permitted and is worth trying.
CARRIER_REQUEST_REJECTED = "request_rejected"
#: Every image candidate was tried; none yielded a usable carrier.
CARRIER_IMAGE_EXHAUSTED = "image_exhausted"
#: ``kubectl debug`` succeeded but its output did not parse into a pod name.
#: Image-independent (output shape) — retrying leaks one uncleanable pod per
#: candidate.
CARRIER_PARSE_FAILURE = "parse_failure"
#: No namespace was available to create in; the request never reached a node.
CARRIER_NO_NAMESPACE = "no_namespace"
#: Transport-level failure (guard rejection, timeout, unexpected exception),
#: an empty target, or an unrecognized stderr shape. The historic retryable
#: bucket: an unclassified failure keeps the chain going, so this fix can only
#: ever stop retries that are provably useless.
CARRIER_RETRYABLE = "retryable"

# kubectl renders every API-status rejection as
# ``Error from server (<Reason>): <message>``. The reason token comes from
# apimachinery's fixed StatusReason vocabulary, so matching it is stable across
# kubectl versions and resource kinds — it is not a per-case string.
_API_STATUS_RE = re.compile(r"error from server \(([A-Za-z]+)\)")

_TARGET_MISSING_REASONS = frozenset({"notfound"})
_REQUEST_REJECTED_REASONS = frozenset({
    "forbidden", "unauthorized", "methodnotsupported",
})

# Bare ``<kind> "<name>" not found`` shape, for stderr that lost the
# ``Error from server (...)`` prefix (wrapped transports, older clients).
# Anchored on a QUOTED object name immediately followed by the phrase, so a
# container-runtime image error cannot match — those read
# ``failed to resolve reference "repo/img:tag": not found`` (colon in between).
_OBJECT_NOT_FOUND_RE = re.compile(r'\b[a-z]+ "[^"]+" not found\b')

# An explicit image complaint stays retryable even when it arrives wearing a
# rejection-shaped prefix: swapping the candidate is precisely the cure.
_IMAGE_COMPLAINT_MARKERS = (
    "image", "manifest", "registry", "pull access", "imagepullbackoff",
)


def _classify_debug_create_failure(stderr: str, stdout: str = "") -> str:
    """Map a failed ``kubectl debug`` create call onto a carrier reason.

    Structural evidence is weighed before keyword evidence: an object name
    that merely happens to contain ``image``/``registry`` (a pod called
    ``image-cache-0``, a node in the ``registry`` pool) must not downgrade a
    NotFound rejection into "keep rotating candidates".
    """
    text = f"{stderr or ''}\n{stdout or ''}"
    low = text.lower()
    if not low.strip():
        return CARRIER_RETRYABLE

    api_status = _API_STATUS_RE.search(low)
    if api_status:
        reason = api_status.group(1)
        if reason in _TARGET_MISSING_REASONS:
            return CARRIER_TARGET_MISSING
        if reason in _REQUEST_REJECTED_REASONS:
            return CARRIER_REQUEST_REJECTED
    elif _OBJECT_NOT_FOUND_RE.search(low):
        return CARRIER_TARGET_MISSING

    if any(m in low for m in _IMAGE_COMPLAINT_MARKERS):
        return CARRIER_RETRYABLE
    if "forbidden" in low or "unauthorized" in low:
        return CARRIER_REQUEST_REJECTED
    return CARRIER_RETRYABLE


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
        # Classification comes from the shared semantic authority
        # (debug_meta_scope) so this guard cannot drift from its siblings
        # in execution_artifacts (artifact collection / vehicle screening).
        # Returning empty here is safe: the name-pattern fallback below
        # cannot match (ephemeral debug emits no "Creating debugging pod"
        # banner).
        if debug_meta_scope(metadata) == "pod":
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

    Carrier-only view over ``create_and_wait_debug_pod_with_reason`` — the
    failure reason is dropped. Callers that have a fallback path resting on
    the SAME node name must use the reason-aware variant instead, or they
    will re-ask a question the API server just answered.
    """
    carrier, _reason = await create_and_wait_debug_pod_with_reason(
        node_name, kubeconfig, task_id, namespace=namespace,
    )
    return carrier


async def create_and_wait_debug_pod_with_reason(
    node_name: str, kubeconfig: str, task_id: str,
    namespace: str = "",
) -> tuple[tuple[str, str] | None, str]:
    """Same as ``create_and_wait_debug_pod``, plus WHY it failed.

    Returns ``(carrier, reason)`` where ``carrier`` is ``(pod_name,
    namespace)`` or ``None``, and ``reason`` is one of the ``CARRIER_*``
    constants (``CARRIER_OK`` on success). The reason is the single authority
    on whether the failure was image-dependent — the only dimension this
    function retries over — so callers can decide whether THEIR fallback is
    still worth an API round.
    """
    if not (node_name or "").strip():
        # An empty target is definitionally missing. Bail before spending an
        # API round on ``kubectl debug node/`` (which the server rejects with
        # a shape that varies by version) and before letting a caller run a
        # node-scoped fallback against "".
        logger.warning("Cannot create a debug pod without a node name")
        return (None, CARRIER_TARGET_MISSING)

    ns = namespace or await _find_available_namespace(kubeconfig, task_id)
    if not ns:
        logger.warning("No accessible namespace found for debug pod creation")
        return (None, CARRIER_NO_NAMESPACE)

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
            # Classify before deciding to rotate: the candidate chain is an
            # IMAGE retry, so it may only absorb image-shaped failures. A
            # rejection of the TARGET or of the CALLER repeats verbatim for
            # every remaining candidate (case #67: ten identical
            # ``nodes "<POD>" not found`` lines), and the reason is what lets
            # the caller skip a fallback built on that same disproven name.
            reason = _classify_debug_create_failure(
                debug_result.stderr, debug_result.stdout,
            )
            logger.warning(
                "Failed to create debug pod with image %s on node %s "
                "(reason=%s): %s",
                image, node_name, reason, debug_result.stderr[:200],
            )
            if reason in (CARRIER_TARGET_MISSING, CARRIER_REQUEST_REJECTED):
                # Image-INDEPENDENT: another candidate cannot change the
                # outcome, and each attempt is a real API mutation request.
                return (None, reason)
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
            return (None, CARRIER_PARSE_FAILURE)

        # A created but unready pod is not an execution carrier. Clean it up
        # so callers cannot accidentally exec into a dead artifact, then try
        # the next image candidate.
        ready = await wait_for_debug_pod_ready(
            pod_name, kubeconfig, task_id, namespace=ns,
        )
        if ready:
            return ((pod_name, ns), CARRIER_OK)
        await delete_debug_pod(pod_name, kubeconfig, task_id, namespace=ns)
        logger.warning(
            "Debug pod image %s not ready on node %s, trying next candidate",
            image, node_name,
        )
    logger.warning(
        "No debug pod image candidate could host the sleep skeleton on node %s",
        node_name,
    )
    return (None, CARRIER_IMAGE_EXHAUSTED)


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
