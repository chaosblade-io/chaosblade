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
    sync_kubewiz_runtime,
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
    PROFILE_K8S,
    profile_of,
    resolve_channel_name,
)

logger = logging.getLogger(__name__)

NODE_NAME = "preplan_probe"
# Outer budget for the whole probe gather (defense-in-depth on top of the
# per-probe ``settings.preplan_probe_timeout``; probes run in parallel so the
# wall clock is normally bounded by the single slowest probe).
_NODE_BUDGET_SECONDS = 30.0

# Probe item statuses: ok / warning / unknown / skipped


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
) -> tuple[str, str, dict]:
    from chaos_agent.preflight import check_chaosblade_operator

    result = await check_chaosblade_operator()
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
            " be reconciled until the operator recovers"
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
                kubeconfig, task_id=task_id,
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


async def _probe_metrics_server(kubeconfig: str) -> tuple[str, str, dict]:
    from chaos_agent.tools.kubectl import exec_kubectl_raw

    result = await exec_kubectl_raw(
        "get",
        ["apiservice", "v1beta1.metrics.k8s.io"],
        kubeconfig=kubeconfig,
        timeout=5.0,
    )
    if result.exit_code == 0:
        return "ok", "metrics.k8s.io available (kubectl top works)", {}
    return (
        "warning",
        "metrics-server unavailable (kubectl top will fail; use logs/events for resource signals)",
        {},
    )


async def _probe_carrier_images(kubeconfig: str) -> tuple[str, str, dict]:
    """Auto-discover recovery-carrier image candidates (healthy DS images).

    Restricted-network clusters (VPC without docker.io egress) cannot pull
    the default allowlist (busybox/curl) — the operator's manual env-var
    workaround was fragile (run8: lost across sessions → opaque
    REJECT_DRIFT). A HEALTHY DaemonSet (desired == ready > 0) proves its
    images are cached on every node the carrier can land on: the scheduler
    never places the carrier on a cordoned node, and a fully-ready DS
    covers every schedulable one. Those images are therefore usable by the
    carrier without any network pull — publish them into
    ``settings.recovery_carrier_discovered_images`` (process-lifetime,
    re-probed every task) and into the observation message so the planner
    picks a candidate directly instead of re-discovering in-loop.

    Toolchain verification (sh/curl/sleep inside the image) stays with the
    planner per recovery-carrier.md section 9 — the probe contributes
    placement facts, not image-content verdicts.
    """
    import json as _json

    from chaos_agent.tools.kubectl import exec_kubectl_raw

    result = await exec_kubectl_raw(
        "get", ["ds", "-A", "-o", "json"], kubeconfig=kubeconfig, timeout=8.0,
    )
    if result.exit_code != 0:
        return (
            "unknown",
            "carrier-image auto-discovery failed (kubectl get ds); "
            "falling back to the configured allowlist only",
            {},
        )
    try:
        items = _json.loads(result.stdout).get("items", [])
    except (TypeError, ValueError):
        return (
            "unknown",
            "carrier-image auto-discovery got unparseable ds output",
            {},
        )

    candidates: dict[str, str] = {}
    for item in items:
        status = (item.get("status") or {})
        desired = status.get("desiredNumberScheduled") or 0
        ready = status.get("numberReady") or 0
        if desired <= 0 or ready != desired:
            continue  # unhealthy DS: coverage unproven, fail closed
        ds_name = f"{(item.get('metadata') or {}).get('namespace', '?')}/{(item.get('metadata') or {}).get('name', '?')}"
        for container in (
            (item.get("spec") or {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        ):
            image = str(container.get("image") or "").strip()
            if image:
                candidates.setdefault(image, ds_name)

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

    if not candidates:
        return (
            "warning",
            "no healthy DaemonSet images found; carrier image allowlist "
            "stays as configured (busybox/curl default)",
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


async def _probe_faultdrill_crd(kubeconfig: str) -> tuple[str, str, dict]:
    """FaultDrill CRD installability — the D3 planning-route signal.

    Read-only two-step: (1) the CRD already exists → the channel needs no
    install (schema compatibility stays with the execute-time lazy check —
    the probe contributes a routing hint, not a verdict); (2) otherwise
    ``kubectl auth can-i create customresourcedefinitions`` decides whether
    the provider channel could install it. ``can-i`` reports a denial as
    exit 0 + ``no`` (a denial is an answer, not an error) — only a nonzero
    exit is a probe failure (``unknown``). A denied can-i at plan time
    means the CR route is unavailable, so the planner routes an
    ``apiserver-write`` case onto the recovery-carrier SOP form directly —
    no CR attempt round is spent (design D3: degradation completes at the
    plan layer).

    Probed ONLY while ``faultdrill_enabled`` is on (the wiring site) —
    dark launch keeps the observation message identical to pre-change.
    """
    from chaos_agent.tools.kubectl import exec_kubectl_raw

    crd_name = f"faultdrills.{settings.faultdrill_crd_group}"
    exists = await exec_kubectl_raw(
        "get", ["crd", crd_name], kubeconfig=kubeconfig, timeout=5.0,
    )
    if exists.exit_code == 0:
        return (
            "ok",
            f"FaultDrill CRD {crd_name} already installed — the CR channel "
            "needs no install (schema compatibility is re-checked lazily "
            "at apply)",
            {},
        )
    can = await exec_kubectl_raw(
        "auth", ["can-i", "create", "customresourcedefinitions"],
        kubeconfig=kubeconfig, timeout=5.0,
    )
    if can.exit_code != 0:
        return (
            "unknown",
            "FaultDrill CRD installability probe failed (kubectl auth "
            "can-i errored); treat the CR channel as unverified",
            {},
        )
    if can.stdout.strip().lower() == "yes":
        return (
            "ok",
            "FaultDrill CRD not installed but installable (can create "
            "customresourcedefinitions) — the CR channel installs it "
            "lazily on first use",
            {},
        )
    return (
        "warning",
        "FaultDrill CRD not installed and NOT installable (cannot create "
        "customresourcedefinitions) — the CR channel is unavailable: plan "
        "recovery_channel: apiserver-write cases onto the recovery-carrier "
        "SOP form",
        {},
    )


# ── Probe runner ─────────────────────────────────────────────────────


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
        # Host-scope channel: the K8s probe set is meaningless there — no
        # probes run and no message is published (no noise on host channel).
        tracker.complete("Pre-task probes skipped: host channel")
        return {}

    try:
        sync_kubewiz_runtime(state)
    except Exception:  # noqa: BLE001
        logger.debug("sync_kubewiz_runtime failed (non-fatal)", exc_info=True)
    kubeconfig = _resolve_kubeconfig(state)

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
        )),
        ("metrics_server", _probe_metrics_server(kubeconfig)),
        ("carrier_images", _probe_carrier_images(kubeconfig)),
    ]
    if settings.faultdrill_enabled:
        # D3 planning-route signal (openspec faultdrill-cr-channel): the
        # CRD installability line the routing guide points the planner at.
        # Probed only while the channel is enabled — dark launch keeps the
        # observation message identical to pre-change.
        probe_specs.append(
            ("faultdrill_crd", _probe_faultdrill_crd(kubeconfig)),
        )
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
    ``messages`` — it is persisted with the conversation, survives replan
    re-entries as plain history, and needs no dedicated state field.
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
    # TaskStore sync by convention (never raises — errors are swallowed in
    # _store_sync). The message above is checkpointed with the conversation
    # and the session entry is the audit artifact.
    await sync_to_store(state, result)
    return result
