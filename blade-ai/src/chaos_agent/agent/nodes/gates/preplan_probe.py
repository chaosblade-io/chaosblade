"""preplan_probe node: fresh read-only probes at task start.

Runs between ``pipeline_init`` and the pipeline entry routing.  Collects the
minimal probe set with proven payback — ChaosBlade operator status (plus the
tool-pod fallback path when it is not ready) and metrics-server availability
— and publishes ONE observation message into the conversation history.  The
planner sees it as a normal persisted context message; replan re-entries see
it too, aged naturally by its position in history.

Scope discipline: only probes whose in-loop rediscovery is expensive AND
whose result changes planning belong here.  Safety-domain checks (target
health, feasibility headroom, conflicts, namespace compliance) stay in the
Phase 2 ``safety_check`` gate — the planner has no action on them and the
gate re-runs them authoritatively anyway.

Design discipline:
- Deterministic, no LLM, strictly read-only (kubectl get / apiservice only).
- Never blocks: a per-probe failure or timeout degrades to ``unknown`` and
  the graph always advances.  Probes are acceleration, not a gate.
- A hint, not a verdict: no safety_score, no safety_status.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from langchain_core.messages import SystemMessage

from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.nodes.execute._kubeconfig_inject import (
    _resolve_kubeconfig,
)
from chaos_agent.agent.nodes.planning.handoff_strip import CONTEXT_ANCHOR_FLAG
from chaos_agent.agent.nodes.store._store_sync import (
    sync_node_status_to_session,
    sync_to_store,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.observability.status_tracker import StatusCategory, get_tracker
from chaos_agent.transports.registry import (
    PROFILE_HOST,
    PROFILE_K8S,
    TransportTarget,
    profile_of,
    resolve_channel_name,
    resolve_transport_target,
)

logger = logging.getLogger(__name__)

NODE_NAME = "preplan_probe"
# Outer budget for the whole probe gather (defense-in-depth on top of the
# per-probe ``settings.preplan_probe_timeout``; probes run in parallel so the
# wall clock is normally bounded by the single slowest probe).
_NODE_BUDGET_SECONDS = 30.0

# Probe item statuses: ok / warning / unknown / skipped

# ── Host observation-toolchain fallback map ──────────────────────────
# The host-scope planner gets NO static guidance about which observation
# binaries the target actually ships, so it rediscovers absence the
# expensive way: a blind ``ss -lntp`` round (exit 127), then ``netstat``
# (exit 127), before falling back to ``/proc/net/tcp`` — ~2 wasted LLM
# rounds / ~60s per run, reproduced across the whole python-app host
# ledger (O-P2 / O-C3-4 / O-C6-2, and Cases #6/#7). The reactive twin of
# this fact lives in ``baseline/_llm_derive.py``'s binary-absence poison
# block, but that only fires AFTER a baseline command already failed —
# the planning phase gets nothing. Probing presence up front (charter:
# expensive in-loop rediscovery AND the result changes planning) lets the
# planner pick the working form on its FIRST observation round.
#
# Single source for the dimension → kernel-interface fallback. The
# baseline poison block carries the same three examples in prose (a
# reactive, differently-framed copy); if a dimension fallback changes,
# update both.
_HOST_OBSERVATION_FALLBACKS = {
    "sockets": "/proc/net/tcp (+ /proc/net/tcp6, /proc/net/udp)",
    "cpu": "/proc/stat, /proc/loadavg",
    "memory": "/proc/meminfo",
    "disk-io": "/proc/diskstats",
}
# binary → the observation dimension it serves. The probe set is the
# recurring-absent observation tools named across the ledger, not an
# exhaustive toolchain scan — a PRESENT binary publishes nothing, so a
# fully-tooled host stays silent.
_HOST_TOOLKIT_DIMENSIONS = {
    "ss": "sockets",
    "netstat": "sockets",
    "mpstat": "cpu",
    "sar": "cpu",
    "iostat": "disk-io",
    "iotop": "disk-io",
    "free": "memory",
}


def _observation_message(items: dict) -> SystemMessage:
    """Render the probe bundle as ONE conversation message.

    The planner (and any later replan re-entry) sees it as a normal
    persisted history entry — aged naturally by its position, with no
    special prompt plumbing.
    """
    lines = [
        f"- {name} [{item['status']}]: {item['summary']}"
        for name, item in items.items()
        if item["status"] != "skipped"
    ]
    content = (
        "[Pre-task environment probes — collected at task start, hints only]\n"
        + "\n".join(lines)
        + "\nReuse these facts directly instead of re-probing them. They are "
        "planning hints, not safety verdicts — the Phase 2 safety gate "
        "re-verifies authoritatively. If your own runtime observation "
        "directly contradicts one, trust your observation."
    )
    return SystemMessage(
        content=content,
        # Handoff-retention anchor: planning/handoff_strip keys on this
        # flag — the environment facts feed the Phase 2 executor's path
        # decisions (e.g. operator availability routing), so the bundle
        # must survive the planning-round strip.
        additional_kwargs={CONTEXT_ANCHOR_FLAG: True},
    )


# ── Individual probes ────────────────────────────────────────────────
# Each returns ``(status, summary, detail)`` and must stay read-only.


async def _probe_operator(
    kubeconfig: str, target_node: str = "", task_id: str = "",
    *, target: TransportTarget | None = None,
) -> tuple[str, str, dict]:
    from chaos_agent.preflight import check_chaosblade_operator

    result = await check_chaosblade_operator(target=target)
    detail: dict = {"fix": result.fix} if result.fix else {}
    status = "ok" if result.passed else "warning"
    summary = result.message
    if not result.passed:
        # Observed consequence, stated heuristically — never as an imperative
        # planning directive. Path choice stays with the planner (ReAct):
        # the probe contributes fresh facts and their directly derivable
        # consequences; discovery/verification of anything else happens
        # in-loop. This also keeps the probe decoupled from the fault
        # tooling's mechanism details — if the operator model changes
        # upstream, only this observation ages; no prescriptive policy to
        # maintain in lockstep.
        summary += (
            "; expected consequence: newly created experiment CRDs will not"
            " be reconciled until the operator recovers — the ChaosBlade"
            " CRD injection channel is unavailable while the operator is"
            " down"
        )
        # Best-effort fallback path discovery: when the operator cannot run
        # CRD injection, the cri/tool-pod path is the alternative — knowing
        # the tool pods up front spares the planner a discovery round-trip.
        # Node attribution is the load-bearing fact: tool pods are DaemonSet
        # pods, and the fallback carrier for ANY fault family is the pod on
        # the target node. Without it the planner must re-discover placement
        # in-loop (observed: ~5 LLM rounds / ~100s in inject-ccfadf7d).
        try:
            from chaos_agent.tools.pod_discovery import (
                discover_tool_pods_cluster_wide_with_nodes,
            )

            pods = await discover_tool_pods_cluster_wide_with_nodes(
                kubeconfig, task_id=task_id, target=target,
            )
            if pods:
                detail["tool_pods"] = [
                    {"pod": name, "namespace": ns, "node": node}
                    for name, ns, node in pods
                ]
                by_node: dict[str, tuple[str, str]] = {
                    node: (name, ns) for name, ns, node in pods
                }
                summary += "; tool pods (name on node)"
                if target_node and target_node in by_node:
                    name, ns = by_node[target_node]
                    detail["tool_pod_on_target"] = {
                        "pod": name, "namespace": ns, "node": target_node,
                    }
                    summary += (
                        f" — ON the target node {target_node}: {ns}/{name}"
                        " (exec carrier for the fallback path)"
                    )
                    others = [n for n in by_node if n != target_node]
                    if others:
                        summary += f"; other nodes covered: {', '.join(sorted(others)[:3])}"
                else:
                    summary += ": " + ", ".join(
                        f"{name} on {node}"
                        for name, _, node in sorted(pods, key=lambda p: p[2])[:4]
                    )
                if target_node and target_node not in by_node:
                    summary += (
                        f" (none on target node {target_node} — the"
                        " exec-carrier fallback is currently unavailable"
                        " there)"
                    )
            elif target_node:
                summary += (
                    "; no tool pods found anywhere — the exec-carrier"
                    " fallback is currently unavailable"
                )
        except Exception as exc:  # noqa: BLE001 — best-effort only
            logger.debug("tool-pod discovery failed (non-fatal): %s", exc)
            summary += "; tool-pod discovery failed — carrier availability currently unknown"
    return status, summary, detail


async def _probe_metrics_server(
    kubeconfig: str, *, target: TransportTarget | None = None,
) -> tuple[str, str, dict]:
    from chaos_agent.tools.kubectl_cli import exec_kubectl_raw

    result = await exec_kubectl_raw(
        "get",
        ["apiservice", "v1beta1.metrics.k8s.io"],
        kubeconfig=kubeconfig,
        timeout=5.0,
        target=target,
    )
    if result.exit_code == 0:
        return "ok", "metrics.k8s.io available (kubectl top works)", {}
    return (
        "warning",
        "metrics-server unavailable (kubectl top will fail; use logs/events for resource signals)",
        {},
    )


# R (run5 lesson): the ds fetch is jitter-exposed — the same command
# measured 2.0s against a healthy gateway timed out an 8s budget under
# gateway slow-tail. A single attempt treats that jitter as a durable
# fact and starves the image allowlist's discovery half for the whole
# process; a second attempt squares the odds for one extra budget slot
# (8+0.5+8 = 16.5s worst case, still under the 30s node budget — probes
# run concurrently, so the gather sees the max, not the sum).
_DS_FETCH_TIMEOUT_SECONDS = 8.0
_DS_FETCH_ATTEMPTS = 2
_DS_FETCH_RETRY_BACKOFF_SECONDS = 0.5


# DaemonSet-like workloads whose fully-ready pods prove node-cached images.
# ``ds`` = the built-in apps/v1 DaemonSet; ``daemonsets.apps.kruise.io`` =
# the OpenKruise Advanced DaemonSet (same every-node coverage guarantee and
# the same ``desiredNumberScheduled``/``numberReady`` status shape). Both are
# queried so a cluster whose node agents run as kruise DaemonSets — e.g. the
# terway CNI on ASI/sigma, empirically the only openssl-capable node-cached
# image there — still contributes its images to the carrier allowlist. The
# standard-only query silently missed them: the assembler then rejected the
# planner's node-cached carrier and the run degraded to the slow manual
# path. CloneSet / Advanced StatefulSet are deliberately EXCLUDED — they do
# not guarantee every-node coverage, so their images are not provably
# node-cached.
_DS_RESOURCE_TYPES = ("ds", "daemonsets.apps.kruise.io")
# The authoritative resource: its fetch failure fails the whole attempt and
# triggers the retry. Every other resource is best-effort additive — a
# missing CRD, a guard rejection, or a timeout there just contributes
# nothing and never starves the primary discovery.
_DS_PRIMARY_RESOURCE = "ds"


async def _get_ds_items(
    kubeconfig: str, resource: str,
    *, target: TransportTarget | None = None,
) -> tuple[bool, list, str]:
    """Fetch + parse ONE DaemonSet-like resource into its raw item list.

    Returns ``(ok, items, failure_note)``. A non-zero exit — including a
    missing-CRD ``the server doesn't have a resource type`` and a guard
    rejection (``exit_code=-1``) — or unparseable output degrades to
    ``ok=False`` so callers can skip best-effort resources without failing
    the whole discovery.
    """
    import json as _json

    from chaos_agent.tools.kubectl_cli import exec_kubectl_raw

    try:
        result = await exec_kubectl_raw(
            "get", [resource, "-A", "-o", "json"], kubeconfig=kubeconfig,
            timeout=_DS_FETCH_TIMEOUT_SECONDS, target=target,
        )
    except Exception as exc:  # noqa: BLE001 — fetch resilience, degrade below
        return False, [], f"kubectl get {resource} raised {exc.__class__.__name__}"
    if result.exit_code != 0:
        return False, [], f"kubectl get {resource} failed"
    try:
        items = _json.loads(result.stdout).get("items", [])
    except (TypeError, ValueError):
        return False, [], f"unparseable {resource} output"
    return True, items, ""


async def _count_schedulable_nodes(
    kubeconfig: str, *, target: TransportTarget | None = None,
) -> tuple[bool, int, str]:
    """Count the nodes a freely-scheduled carrier could land on.

    The carrier Pod is built with no ``nodeName``/``nodeSelector`` and
    tolerations derived from the cluster's taints, so the scheduler may
    place it on ANY node that is Ready and not cordoned
    (``spec.unschedulable``). This count is the coverage floor for the
    image gate in :func:`_collect_healthy_images`: a DaemonSet proves its
    images are node-cached on every possible landing node only when its
    ``desiredNumberScheduled`` reaches this floor.

    Returns ``(ok, count, failure_note)``. Any fetch/parse failure is
    ``ok=False`` so the caller fails the attempt CLOSED — an
    undeterminable floor must never silently degrade to "no gate".
    """
    import json as _json

    from chaos_agent.tools.kubectl_cli import exec_kubectl_raw

    try:
        result = await exec_kubectl_raw(
            "get", ["nodes", "-o", "json"], kubeconfig=kubeconfig,
            timeout=_DS_FETCH_TIMEOUT_SECONDS, target=target,
        )
    except Exception as exc:  # noqa: BLE001 — fetch resilience, degrade below
        return False, 0, f"kubectl get nodes raised {exc.__class__.__name__}"
    if result.exit_code != 0:
        return False, 0, "kubectl get nodes failed"
    try:
        items = _json.loads(result.stdout).get("items", [])
        count = 0
        for node in items:
            if not isinstance(node, dict):
                continue
            if (node.get("spec") or {}).get("unschedulable"):
                continue  # cordoned: scheduler will not place the carrier here
            conditions = (node.get("status") or {}).get("conditions") or []
            if any(
                isinstance(c, dict)
                and c.get("type") == "Ready"
                and c.get("status") == "True"
                for c in conditions
            ):
                count += 1
    except (TypeError, ValueError, AttributeError):
        return False, 0, "unparseable nodes output"
    return True, count, ""


def _collect_healthy_images(items: list, min_coverage: int) -> dict[str, str]:
    """Filter to CLUSTER-WIDE fully-ready DaemonSets; collect their images.

    Single source of truth for "what counts as a healthy candidate" — shared
    by every discovered resource type so they can never drift apart. TWO
    gates, both fail closed:

    1. ``desiredNumberScheduled > 0`` and ``numberReady == desired`` — the
       DaemonSet is fully rolled out on the nodes it targets.
    2. ``desired >= min_coverage`` — those targeted nodes cover EVERY node
       the freely-scheduled carrier could land on (``min_coverage`` = the
       schedulable node count from :func:`_count_schedulable_nodes`).

    Gate 2 is the correctness fix: ``ready == desired`` alone only proves
    coverage of the DaemonSet's OWN ``nodeSelector``/``affinity`` subset.
    A DaemonSet restricted to a few nodes (empirically ``desired == 1``
    pinned to a single hostname, ``desired == 4`` to one resource pool) is
    fully ready yet its images are cached on that subset ONLY — admitting
    them asserts "node-cached everywhere" for an image the carrier may not
    find when it lands elsewhere, causing ImagePullBackOff and the full
    ``_wait_pod_running`` timeout on a pull-less cluster. Only cluster-wide
    coverage is provable from ``desired``/``ready``; a restricted-but-
    pullable image is deliberately NOT admitted (no config escape hatch is
    added — an unprovable image is left out rather than vouched for).

    ALL of a passing DaemonSet's container images are collected (the
    openssl-capable one may not be ``containers[0]`` — terway carries it at
    ``containers[1]``).
    """
    candidates: dict[str, str] = {}
    for item in items:
        status = (item.get("status") or {})
        desired = status.get("desiredNumberScheduled") or 0
        ready = status.get("numberReady") or 0
        if desired <= 0 or ready != desired:
            continue  # unhealthy DS: coverage unproven, fail closed
        if desired < min_coverage:
            continue  # restricted DS: fully-ready only on its own subset,
            # not provably node-cached on every carrier landing node
        meta = (item.get("metadata") or {})
        ds_name = f"{meta.get('namespace', '?')}/{meta.get('name', '?')}"
        for container in (
            (item.get("spec") or {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        ):
            image = str(container.get("image") or "").strip()
            if image:
                candidates.setdefault(image, ds_name)
    return candidates


async def _fetch_healthy_ds_images(
    kubeconfig: str,
    *, target: TransportTarget | None = None,
) -> tuple[bool, dict[str, str] | None, str]:
    """ONE discovery attempt: fetch + parse + healthy-DS image filter.

    Returns ``(ok, image→DaemonSet mapping or None, failure_note)``.
    Pure fetch, no settings mutation — the preplan probe and the
    guard-time lazy refresh
    (:func:`refresh_carrier_image_discovery`) compose retry + merge on
    top of this single-attempt core, so the two writers can never drift
    apart on what counts as a healthy candidate.

    Every resource in :data:`_DS_RESOURCE_TYPES` is fetched CONCURRENTLY so
    the attempt's wall clock stays bounded by the single per-fetch timeout,
    not their sum — the retry math (R above) still fits the 30s node budget.
    The primary resource (``ds``) is authoritative: its failure fails the
    attempt and triggers the retry. The rest are best-effort additive: a
    cluster without the OpenKruise CRD (or where that query is rejected or
    times out) simply contributes no extra images.
    """
    # The node-count fetch runs CONCURRENTLY with the DS fetches so the
    # attempt's wall clock stays bounded by ONE per-fetch timeout, not
    # their sum — the retry math (R above) still fits the 30s node budget.
    results, node_result = await asyncio.gather(
        asyncio.gather(*(
            _get_ds_items(kubeconfig, resource, target=target)
            for resource in _DS_RESOURCE_TYPES
        )),
        _count_schedulable_nodes(kubeconfig, target=target),
    )
    p_ok, _p_items, p_note = results[_DS_RESOURCE_TYPES.index(_DS_PRIMARY_RESOURCE)]
    if not p_ok:
        return False, None, p_note
    n_ok, min_coverage, n_note = node_result
    if not n_ok:
        # Coverage floor undeterminable → cannot prove ANY image is
        # node-cached on every landing node → fail the attempt CLOSED
        # (triggers the retry; ultimately degrades to unknown, i.e.
        # configured-only). Never silently drop the gate.
        return False, None, n_note
    candidates: dict[str, str] = {}
    for ok, items, _note in results:
        if not ok:
            continue  # best-effort resource unavailable (CRD absent, etc.)
        for image, ds_name in _collect_healthy_images(items, min_coverage).items():
            # setdefault: the primary resource wins any image collision, so
            # ds_name attribution stays stable regardless of gather order.
            candidates.setdefault(image, ds_name)
    return True, candidates, ""


async def _fetch_healthy_ds_images_with_retry(
    kubeconfig: str,
    *, target: TransportTarget | None = None,
) -> tuple[bool, dict[str, str] | None, str, int]:
    """Jitter-hardened fetch: N attempts with a short backoff (R above).

    Returns ``(ok, candidates_or_None, failure_note, attempts)`` — the
    attempt count feeds the probe's unknown message so an operator can
    tell "one-shot failure" from "retried and still failing".
    """
    ok = False
    candidates: dict[str, str] | None = None
    note = "not attempted"
    attempts = 0
    for attempt in range(_DS_FETCH_ATTEMPTS):
        attempts += 1
        ok, candidates, note = await _fetch_healthy_ds_images(kubeconfig, target=target)
        if ok:
            break
        if attempt + 1 < _DS_FETCH_ATTEMPTS:
            await asyncio.sleep(_DS_FETCH_RETRY_BACKOFF_SECONDS)
    return ok, candidates, note, attempts


def _merge_discovered_images(candidates: dict[str, str]) -> list[str]:
    """Merge healthy-DS images into the discovery set; returns the NEW ones.

    Single merge point for both writers (probe + lazy refresh):
    configured-set dedup, previously-discovered dedup, sorted stable
    order — identical semantics wherever the set gets written.
    """
    from chaos_agent.config.settings import settings as _settings

    configured = {
        img.strip()
        for img in str(_settings.recovery_carrier_allowed_images or "").split(",")
        if img.strip()
    }
    previously = {
        img.strip()
        for img in str(_settings.recovery_carrier_discovered_images or "").split(",")
        if img.strip()
    }
    new_images = sorted(set(candidates) - configured - previously)
    if new_images:
        merged = sorted(previously | set(new_images))
        _settings.recovery_carrier_discovered_images = ",".join(merged)
    return new_images


# L: ONE lazy refresh per process (see refresh_carrier_image_discovery).
_LAZY_REFRESH_DONE = False


async def refresh_carrier_image_discovery(
    kubeconfig: str, *, target: TransportTarget | None = None,
) -> bool:
    """L: execute-time second chance for a starved discovery set (run5).

    The preplan probe is the discovery set's primary writer, but its
    failure mode is structural: ONE gateway jitter leaves the set empty
    for the whole process lifetime, and both sync allowlist consumers
    (the carrier-run shape check, the drill-target manifest T4 check)
    then fail closed against the configured docker.io-only set — on a
    VPC cluster without docker.io egress that is a dead end no amount
    of LLM adaptation can escape (run5: every allowlisted image
    ErrImagePull'd while the node-cached terway image sat outside the
    set purely because the probe had timed out).

    This re-fetches (retry-hardened, same core as the probe) and merges.
    Bounded to ONE attempt per process: a genuinely empty cluster must
    not pay a futile probe on every carrier call, and the preplan probe
    still re-runs at every task start as the primary path. Returns True
    when the discovery set gained at least one image.
    """
    global _LAZY_REFRESH_DONE
    if _LAZY_REFRESH_DONE:
        return False
    _LAZY_REFRESH_DONE = True
    ok, candidates, _note, _attempts = (
        await _fetch_healthy_ds_images_with_retry(kubeconfig, target=target)
    )
    if not ok or not candidates:
        return False
    return bool(_merge_discovered_images(candidates))


async def _probe_carrier_images(
    kubeconfig: str, *, target: TransportTarget | None = None,
) -> tuple[str, str, dict]:
    """Auto-discover recovery-carrier image candidates (healthy DS images).

    Restricted-network clusters (VPC without docker.io egress) cannot pull
    the default allowlist (the curl images, docker.io) — the operator's
    manual env-var
    workaround was fragile (run8: lost across sessions → opaque
    REJECT_DRIFT). A HEALTHY DaemonSet (desired == ready > 0) proves its
    images are cached on every node the carrier can land on: the scheduler
    never places the carrier on a cordoned node, and a fully-ready DS
    covers every schedulable one. Those images are therefore usable by the
    carrier without any network pull — publish them into
    ``settings.recovery_carrier_discovered_images`` (process-lifetime,
    re-probed every task) and into the observation message so the planner
    picks a candidate directly instead of re-discovering in-loop.

    The fetch is retry-hardened (R, run5): a single gateway timeout is
    jitter, not a cluster fact — the probe only reports unknown after
    ``_DS_FETCH_ATTEMPTS`` attempts have failed.

    Toolchain verification (sh/curl/sleep inside the image) stays with the
    planner per recovery-carrier.md section 9 — the probe contributes
    placement facts, not image-content verdicts.
    """
    ok, candidates, note, attempts = (
        await _fetch_healthy_ds_images_with_retry(kubeconfig, target=target)
    )
    if not ok:
        return (
            "unknown",
            f"carrier-image auto-discovery failed ({note}; "
            f"{attempts} attempt(s)); falling back to the configured "
            "allowlist only",
            {},
        )
    new_images = _merge_discovered_images(candidates)

    if not candidates:
        return (
            "warning",
            "no healthy DaemonSet images found; carrier image allowlist "
            "stays as configured (the curl-only default)",
            {},
        )
    # No cap: the summary line is the ONLY channel the planner sees (the
    # detail dict stays tracker-side), so a "…and N more" truncation hides
    # live candidates and forces in-loop re-discovery. #36 retest evidence:
    # the [:6] alphabetical cap buried terway — the empirically preferred
    # carrier image — behind "…and 3 more", costing two re-verification
    # rounds (~110s). Candidate count is bounded by the cluster's healthy
    # DaemonSets, so the unbounded line stays small in practice.
    shown = ", ".join(
        f"{img} (ds {candidates[img]})" for img in sorted(candidates)
    )
    return (
        "ok",
        f"carrier image candidates (healthy DaemonSet images = node-cached, "
        f"no pull needed): {shown} — verify sh/curl/sleep toolchain "
        "per recovery-carrier.md section 9 before use; auto-added to the "
        "carrier shape allowlist for this task",
        {"images": sorted(candidates), "added_now": new_images},
    )


# ── Probe runner ─────────────────────────────────────────────────────


async def _probe_host_toolkit(
    *, target: TransportTarget | None = None, task_id: str = "",
) -> tuple[str, str, dict]:
    """Detect ABSENT host observation binaries; publish their /proc fallback.

    Host-channel analogue of the K8s probe set: a strictly read-only
    presence sweep (``command -v`` in a POSIX shell loop) over the
    recurring-absent observation tools, publishing ONE planning hint that
    maps each absent binary's dimension to the kernel interface the planner
    should read instead. This is the PROACTIVE twin of the reactive
    binary-absence poison block in ``baseline/_llm_derive.py`` — it lands
    BEFORE the planner burns a round on a doomed ``ss``/``netstat`` call,
    rather than after a baseline command already exited 127.

    Charter discipline:
    - Read-only, deterministic, no LLM (a ``command -v`` sweep).
    - Never blocks and stays SILENT when it has nothing to change: any
      failure / timeout / non-zero exit / unparseable output degrades to
      ``skipped`` (no message), and a fully-tooled host (every probed binary
      present) also returns ``skipped`` — so the host channel carries no
      noise, matching the pre-change skip behavior for a healthy host.
    - A hint, not a verdict: states the observed absence and its directly
      derivable consequence (the working interface), never an imperative.

    ``sh -c`` is the vehicle because ``command -v`` is a shell builtin and
    the exec-form channel never interprets metacharacters on its own — both
    host channels (ssh, kubewiz_host) dispatch the argv through a remote
    shell, so an explicit ``sh -c '<script>'`` is the portable form (the
    same wrapper ``channels._wiz_preserve_stderr`` relies on). A target
    without ``sh`` fails the sweep → ``skipped`` (never fatal).
    """
    from chaos_agent.transports.executor import execute_via_transport

    names = sorted(_HOST_TOOLKIT_DIMENSIONS)
    script = (
        "for b in " + " ".join(names) + "; do "
        'if command -v "$b" >/dev/null 2>&1; then echo "P $b"; '
        'else echo "A $b"; fi; done'
    )
    resolved = target if target is not None else resolve_transport_target(None)
    try:
        result = await execute_via_transport(
            ["sh", "-c", script],
            resolved,
            timeout=settings.preplan_probe_timeout,
            task_id=task_id,
            # Internal read-only probe: skip the guard (``sh`` and the loop's
            # metacharacters sit outside ALLOWED_COMMANDS) and skip the audit
            # trail (not an LLM-originated command) — mirrors the K8s probes'
            # ``exec_kubectl_raw`` internal-probe path.
            skip_guard=True,
            source="preplan-host-toolkit",
            expect_profile=PROFILE_HOST,
            audit=False,
        )
    except Exception as exc:  # noqa: BLE001 — probe failure is never fatal
        logger.debug("host toolkit probe failed (non-fatal): %s", exc)
        return (
            "skipped",
            f"host toolkit probe unavailable: {exc.__class__.__name__}",
            {},
        )
    if result.exit_code != 0:
        return "skipped", "host toolkit probe returned non-zero", {}

    absent: list[str] = []
    present: list[str] = []
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if line.startswith("A "):
            tok = line[2:].strip()
            if tok in _HOST_TOOLKIT_DIMENSIONS:
                absent.append(tok)
        elif line.startswith("P "):
            tok = line[2:].strip()
            if tok in _HOST_TOOLKIT_DIMENSIONS:
                present.append(tok)
    if not absent and not present:
        # Empty / unparseable — degrade silently rather than guess a verdict.
        return "skipped", "host toolkit probe produced no usable result", {}
    if not absent:
        # Fully-tooled host: nothing to change in planning, no noise.
        return "skipped", "all probed observation binaries present", {
            "present": sorted(present),
        }

    # Collapse absent binaries by the dimension they observe — the planner
    # needs the interface to read, not a per-binary list, so ss+netstat fold
    # into ONE sockets fallback line.
    dims: dict[str, list[str]] = {}
    for b in absent:
        dims.setdefault(_HOST_TOOLKIT_DIMENSIONS[b], []).append(b)
    fallback_parts = [
        f"{dim} ({', '.join(sorted(dims[dim]))} absent) → read "
        f"{_HOST_OBSERVATION_FALLBACKS[dim]}"
        for dim in sorted(dims)
    ]
    summary = (
        "these host observation binaries are NOT installed on the target — a "
        "command invoking any of them exits 127 / 'command not found', so "
        "probing them wastes a round: "
        + "; ".join(fallback_parts)
        + ". Pick the kernel-interface form on the first observation round "
        "instead of trying the absent binaries."
    )
    return "ok", summary, {
        "absent": sorted(absent),
        "present": sorted(present),
        "dimensions": {d: sorted(v) for d, v in dims.items()},
    }


# ── Probe runner (K8s) ───────────────────────────────────────────────


async def _run_one(name: str, coro, tracker=None) -> tuple[str, dict]:
    """Run a single probe with a per-item timeout; never raises."""
    started = time.monotonic()
    timeout = settings.preplan_probe_timeout
    try:
        status, summary, detail = await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("preplan probe %s timed out (%.0fs)", name, timeout)
        status, summary, detail = "unknown", f"probe timed out ({timeout:.0f}s)", {}
    except Exception as exc:  # noqa: BLE001 — probe failure is never fatal
        logger.warning("preplan probe %s failed (non-fatal): %s", name, exc)
        status, summary, detail = "unknown", f"probe failed: {str(exc)[:120]}", {}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "preplan probe %s: status=%s elapsed=%dms summary=%s",
        name, status, elapsed_ms, summary,
    )
    if tracker is not None:
        tracker.update(f"Probe {name}: {status}", {"debug": True, name: detail or {}})
    return name, {
        "status": status,
        "summary": summary,
        "detail": detail or {},
        "elapsed_ms": elapsed_ms,
    }


def _skipped(reason: str) -> dict:
    return {"status": "skipped", "summary": reason, "detail": {}, "elapsed_ms": 0}


async def preplan_probe(state: AgentState) -> dict:
    """Collect fresh pre-task probes and publish ONE observation message.

    Never raises and never blocks the pipeline: every failure mode degrades
    to ``unknown`` items and the graph advances.
    """
    task_id = state.get("task_id", "") or ""
    tracker = get_tracker(task_id)
    tracker.start(StatusCategory.NODE, NODE_NAME, "Running pre-task probes")

    channel = resolve_channel_name(state)
    probes: dict = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "items": {},
    }

    # Skip markers — emit nothing; behavior matches pre-change exactly.
    skip_reason = ""
    if not settings.preplan_probes_enabled:
        skip_reason = "disabled by settings"

    if skip_reason:
        tracker.complete(f"Pre-task probes skipped: {skip_reason}")
        return {}

    is_k8s = profile_of(channel) == PROFILE_K8S
    if not is_k8s:
        # Host-scope channel: the K8s probe set (operator / metrics-server /
        # carrier images) is meaningless here. Run the ONE host-relevant probe
        # — the observation-toolchain presence sweep — and publish its /proc
        # fallback hint only when a binary is actually absent. A fully-tooled
        # host (or any probe failure) degrades to ``skipped`` and publishes
        # nothing, preserving the pre-change "no noise on host channel"
        # behavior for the healthy case.
        _host_target = resolve_transport_target(state)
        host_specs = [
            ("host_toolkit", _probe_host_toolkit(
                target=_host_target, task_id=task_id,
            )),
        ]
        try:
            host_results = await asyncio.wait_for(
                asyncio.gather(*(
                    _run_one(name, coro, tracker) for name, coro in host_specs
                )),
                timeout=_NODE_BUDGET_SECONDS,
            )
            probes["items"] = dict(host_results)
        except asyncio.TimeoutError:  # pragma: no cover — per-probe timeouts bound this
            logger.warning("host preplan probe gather exceeded node budget")
            probes["items"] = {
                name: _skipped("node budget exceeded") for name, _ in host_specs
            }
        return await _finish(state, tracker, probes)

    kubeconfig = _resolve_kubeconfig(state)
    # Single-authority transport target resolved from THIS node's state (the
    # seam replaces the settings side-effect ``sync_kubewiz_runtime`` used to
    # establish). Threaded into every probe leaf so the read-only recon reads
    # per-session kubewiz values from state, not settings.
    _xport_target = resolve_transport_target(state)

    # Node-scope specs name the target node directly — pass it down so the
    # operator probe can attribute the fallback tool pod to the target.
    # Pod/container scopes resolve their node later in planning; the generic
    # node-attributed list serves them too.
    target_node = ""
    try:
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        spec = read_fault_spec(state)
        if spec and spec.scope == "node" and spec.names:
            target_node = spec.names[0]
    except Exception:  # noqa: BLE001 — hint enrichment only
        logger.debug("fault_spec read for probe hint failed", exc_info=True)

    probe_specs = [
        ("chaosblade_operator", _probe_operator(
            kubeconfig, target_node=target_node, task_id=task_id,
            target=_xport_target,
        )),
        ("metrics_server", _probe_metrics_server(kubeconfig, target=_xport_target)),
        ("carrier_images", _probe_carrier_images(kubeconfig, target=_xport_target)),
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(
                _run_one(name, coro, tracker) for name, coro in probe_specs
            )),
            timeout=_NODE_BUDGET_SECONDS,
        )
        probes["items"] = dict(results)
    except asyncio.TimeoutError:  # pragma: no cover — per-probe timeouts bound this
        logger.warning("preplan probe gather exceeded node budget")
        probes["items"] = {
            name: _skipped("node budget exceeded") for name, _ in probe_specs
        }

    return await _finish(state, tracker, probes)


async def _finish(state: AgentState, tracker, probes: dict) -> dict:
    """Observability fan-out + persistence, then the state update.

    The probe bundle is published as ONE observation message appended to
    ``messages`` — it is persisted with the conversation and survives replan
    re-entries as plain history, so the AGENT planner needs no dedicated state
    field (it reads the message track). The host-toolkit absence fact is the one
    exception: ``baseline_capture``'s LLM derivation is a separate off-graph call
    that builds its own prompt and never reads ``messages``, so the absent-binary
    set is ALSO mirrored to ``absent_host_binaries`` for that single consumer.
    """
    counts: dict[str, int] = {}
    for item in probes["items"].values():
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    counts_line = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))

    # TUI streaming summary — one human-readable line.
    parts = [
        f"{name}: {item['summary']}"
        for name, item in probes["items"].items()
        if item["status"] != "skipped"
    ]
    summary_line = (
        f"Pre-task probes done ({counts_line}). " + "; ".join(parts)
        if parts
        else f"Pre-task probes done ({counts_line})."
    )
    await dispatch_node_message(NODE_NAME, summary_line + "\n\n")
    tracker.complete(f"Pre-task probes: {counts_line}")
    sync_node_status_to_session(
        state, NODE_NAME, summary_line, detail={"preplan_probes": probes},
    )

    result: dict = {}
    if parts:
        result["messages"] = [_observation_message(probes["items"])]
    # Mirror the host-toolkit absence set to state for the one consumer that
    # cannot read the observation message above (baseline's off-graph derive).
    # Only a definitive ``ok`` result with a non-empty absent list is mirrored —
    # skipped/unknown/fully-tooled hosts leave the field untouched (None), so
    # the baseline derive behaves exactly as before on every non-host channel.
    _toolkit = probes["items"].get("host_toolkit") or {}
    if _toolkit.get("status") == "ok":
        _absent = (_toolkit.get("detail") or {}).get("absent") or []
        if _absent:
            result["absent_host_binaries"] = tuple(sorted(set(_absent)))
    # TaskStore sync by convention (never raises — errors are swallowed in
    # _store_sync). The message above is checkpointed with the conversation
    # and the session entry is the audit artifact.
    await sync_to_store(state, result)
    return result
