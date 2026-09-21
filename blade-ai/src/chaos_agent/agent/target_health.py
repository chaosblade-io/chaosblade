"""Patch D — pluggable target health checker.

Why this module exists:

    The user-reported turn (task-9209c7052240) burned 5+ minutes
    trying to inject a CPU fullload onto a node that had been
    ``DiskPressure=True`` for 103 days. Kubernetes reported the node
    as ``Ready`` (the only signal the agent_loop was reading), so
    the LLM happily picked it as the inject target. The kubelet
    eviction loop made the ChaosBlade Agent pod unschedulable and
    every ``blade create`` attempt failed.

    The fix: before the LLM enters the confirm gate, run a
    scope-specific *health pre-check* that surfaces blocker
    conditions in the confirm card payload. The user (or the LLM in
    auto-mode) can see "this target has DiskPressure=True for 103d"
    and pick a different node.

Design (kept deliberately small):

    1. ``HealthSeverity`` is the routing-relevant outcome
       (``OK / WARN / BLOCK``).
    2. ``HealthIssue`` carries a stable ``code`` (e.g.
       ``node.disk_pressure``) so downstream logging / metrics /
       i18n can key off it without parsing the human ``message``.
    3. ``TargetHealthChecker`` is a Protocol — each scope (node /
       pod / namespace / future deployment / kafka topic) plugs in
       its own checker. Built-in node + pod checkers cover the
       chaos-engineering 80%; skill packs can register more.
    4. ``assess_target_health`` is the single entry — agent_loop
       calls it once, gets a ``HealthReport``, attaches it to the
       confirm payload. No graph topology change.

The checkers themselves do *not* run shell commands directly here —
they're stubs that document the kubectl logic. Wiring real kubectl
calls is the job of the integration layer (``inject_context.py`` /
existing kubectl wrapper). Tests mock the checker output.

Backwards-compat:

    Default ``settings.target_health_check_enabled = True`` but
    ``settings.target_health_check_block_on_blocker = False`` — i.e.
    we attach the report to the confirm card but **never** silently
    veto an inject. The user / LLM still gets to decide. Set the
    block flag to ``True`` to opt in to hard blocking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

logger = logging.getLogger(__name__)


class HealthSeverity(Enum):
    """How worried should the operator be about this target."""

    OK = "ok"
    """No issues found."""

    WARN = "warn"
    """Injectable but flagged — the inject may still work but the
    target has anomalies (e.g. high but not extreme load)."""

    BLOCK = "block"
    """Likely-fatal precondition — inject will fail or is unsafe.

    Whether this is enforced as a hard block depends on
    ``settings.target_health_check_block_on_blocker``; the default is
    ``False`` (warn-only).
    """


@dataclass
class HealthIssue:
    """A single problem found by a checker."""

    severity: HealthSeverity
    code: str
    """Stable machine-readable identifier (e.g. ``node.disk_pressure``).

    Convention: ``<scope>.<condition>``. Used for log keying, i18n,
    metrics. Never localised, never user-facing.
    """

    message: str
    """Human-readable description suitable for confirm card display."""

    duration_hint: str = ""
    """Rough duration the condition has been active, e.g. ``103d``.

    Empty if the checker can't tell. Useful for the LLM / operator
    to gauge whether retry would help — a 103d-old DiskPressure is
    different from a 30s blip.
    """


@dataclass
class HealthReport:
    """Aggregated outcome of a target health pre-check."""

    target: dict
    """The target spec the checker examined (echoed for audit)."""

    overall: HealthSeverity
    """Worst severity found across ``issues``. Drives routing."""

    issues: list[HealthIssue] = field(default_factory=list)
    """All problems found, ordered by ``severity`` desc then ``code``."""

    checked_detail: str = ""
    """Scope-specific one-liner describing what was verified.

    Set by each checker. Examples:
      - node: "Node Ready, no DiskPressure/MemoryPressure/PIDPressure/NetworkUnavailable"
      - pod: "1 pod(s) checked, no Evicted/CrashLoopBackOff/ImagePullBackOff"
    Used by confirm card to show what the check actually covered.
    """

    def is_blocking(self) -> bool:
        """True iff a hard-block condition was found."""
        return self.overall == HealthSeverity.BLOCK

    def has_warnings(self) -> bool:
        """True iff any non-OK issue was found."""
        return self.overall != HealthSeverity.OK

    def summary(self) -> str:
        """Compact one-liner for log lines / confirm card subtitle."""
        if not self.issues:
            return "healthy"
        return "; ".join(
            f"{i.code}({i.severity.value})" for i in self.issues
        )

    def to_dict(self) -> dict:
        """Serialised form embedded into confirm-card payload."""
        return {
            "target": self.target,
            "overall": self.overall.value,
            "issues": [
                {
                    "severity": i.severity.value,
                    "code": i.code,
                    "message": i.message,
                    "duration_hint": i.duration_hint,
                }
                for i in self.issues
            ],
            "summary": self.summary(),
            "checked_detail": self.checked_detail,
        }


class TargetHealthChecker(Protocol):
    """Plugin interface — one implementation per inject scope."""

    scope: str
    """Scope this checker covers (e.g. ``"node"`` / ``"pod"``)."""

    async def check(
        self, target: dict, kubeconfig: str
    ) -> HealthReport:
        """Inspect ``target`` and return a ``HealthReport``."""
        ...


# ---------------------------------------------------------------------------
# Built-in checkers — small, pure, easy to test
# ---------------------------------------------------------------------------


class NodeHealthChecker:
    """Detects node-level conditions that block kubelet from scheduling.

    Inspects the four pressure conditions kubelet enforces:
      - DiskPressure
      - MemoryPressure
      - NetworkUnavailable
      - PIDPressure

    Any of these in ``status: "True"`` is a BLOCK. The kubelet will
    refuse to schedule new pods, which means the ChaosBlade Agent
    DaemonSet pod (the thing that does the actual fault injection)
    won't come back if it gets evicted.

    Real implementation runs ``kubectl get node {name} -o json`` and
    parses ``.status.conditions``. The stub below documents the
    expected return shape so tests / wiring can mock it.
    """

    scope = "node"

    async def check(
        self, target: dict, kubeconfig: str
    ) -> HealthReport:
        from chaos_agent.config.settings import settings
        from chaos_agent.tools.pod_discovery import (
            TOOL_POD_ABSENT,
            TOOL_POD_PRESENT,
        )
        from chaos_agent.utils.coerce import coerce_to_list

        names = coerce_to_list(
            target.get("names"), context="NodeHealthChecker:names"
        )
        if not names:
            return HealthReport(
                target=target,
                overall=HealthSeverity.OK,
                issues=[],
                checked_detail="node: no target",
            )

        conditions = await _query_node_conditions(names[0], kubeconfig)
        report = _build_node_report(target, conditions)

        # chaosblade-tool existence — node scope requires DaemonSet pod on target.
        # R69 three-state: only ABSENT blocks; UNKNOWN fails open (a broken
        # probe must never block the inject), and "online" is claimed ONLY on
        # PRESENT so the report can no longer stamp a carrier it never found.
        tool_state = None
        if settings.blade_agent_check_enabled and kubeconfig:
            tool_state = await _query_blade_agent_on_node(names[0], kubeconfig)
            if tool_state == TOOL_POD_ABSENT:
                report.issues.append(
                    HealthIssue(
                        severity=HealthSeverity.BLOCK,
                        code="node.chaosblade_tool_missing",
                        message=f"chaosblade-tool pod not found on node {names[0]}",
                    )
                )
                report.overall = HealthSeverity.BLOCK

        if report.overall == HealthSeverity.OK:
            detail = "Node Ready, no DiskPressure/MemoryPressure/PIDPressure/NetworkUnavailable"
            if tool_state == TOOL_POD_PRESENT:
                detail += ", chaosblade-tool online"
            report.checked_detail = detail

        return report


class PodHealthChecker:
    """Detects pod-level conditions that prevent fault execution.

    A pod is BLOCK when:
      - ``status.phase`` is ``Pending`` / ``Failed`` / ``Unknown``
      - ``status.conditions`` has ``Ready=False`` and
        ``reason`` matches ``Evicted`` / ``CrashLoopBackOff`` /
        ``ImagePullBackOff``

    Real implementation: ``kubectl get pod {name} -n {namespace} -o
    json``. Stub below.
    """

    scope = "pod"

    async def check(
        self, target: dict, kubeconfig: str
    ) -> HealthReport:
        namespace = target.get("namespace", "default")
        pod_names = await _resolve_pod_names(target, kubeconfig)
        if not pod_names:
            return HealthReport(
                target=target,
                overall=HealthSeverity.OK,
                issues=[],
                checked_detail="pod: no resolvable target",
            )

        _SEV_ORDER = {HealthSeverity.OK: 0, HealthSeverity.WARN: 1, HealthSeverity.BLOCK: 2}
        all_issues: list[HealthIssue] = []
        worst = HealthSeverity.OK
        checked_pods: list[str] = []
        for pod_name in pod_names:
            status = await _query_pod_status(pod_name, namespace, kubeconfig)
            report = _build_pod_report(target, status)
            for issue in report.issues:
                issue.message = f"[{pod_name}] {issue.message}"
                all_issues.append(issue)
            if _SEV_ORDER.get(report.overall, 0) > _SEV_ORDER.get(worst, 0):
                worst = report.overall
            checked_pods.append(pod_name)

        if worst == HealthSeverity.OK:
            detail = (
                f"{len(checked_pods)} pod(s) checked, "
                f"no Evicted/CrashLoopBackOff/ImagePullBackOff"
            )
        else:
            detail = f"{len(all_issues)} issue(s) across {len(checked_pods)} pod(s)"

        return HealthReport(
            target=target,
            overall=worst,
            issues=all_issues,
            checked_detail=detail,
        )


# ---------------------------------------------------------------------------
# Registry — extensible by skill packs
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, TargetHealthChecker] = {
    "node": NodeHealthChecker(),
    "pod": PodHealthChecker(),
}


def register_health_checker(checker: TargetHealthChecker) -> None:
    """Register a custom checker for a new scope.

    Skill packs / third-party plugins call this at import time. The
    last registration for a scope wins (same convention as Python
    package overrides).
    """
    _REGISTRY[checker.scope] = checker
    logger.info(
        "registered target health checker for scope=%s", checker.scope
    )


async def assess_target_health(
    scope: str,
    target: dict,
    kubeconfig: str = "",
) -> HealthReport:
    """Single entry point — agent_loop calls this once per turn.

    Returns a ``HealthReport`` regardless of scope; an unknown scope
    returns an empty OK report (no checker configured for that scope is
    "no such check", not a failed check). A checker that RAISES is
    different — see the except branch: never raise, never block, but
    also never masquerade as healthy (R66).
    """
    checker = _REGISTRY.get(scope)
    if checker is None:
        logger.debug(
            "no health checker for scope=%s, skipping", scope
        )
        return HealthReport(
            target=target, overall=HealthSeverity.OK, issues=[]
        )
    try:
        return await checker.check(target, kubeconfig)
    except Exception as exc:
        # A checker bug must NOT take down the inject pipeline (never
        # raise, never block). R66: but it must not masquerade as a
        # clean bill of health either — degrade to an explicit unknown
        # WARN, the transparent fail-open, so confirm proceeds WITH the
        # information that nothing was verified.
        logger.warning(
            "health checker for scope=%s failed: %s",
            scope,
            exc,
        )
        return HealthReport(
            target=target,
            overall=HealthSeverity.WARN,
            issues=[
                HealthIssue(
                    severity=HealthSeverity.WARN,
                    code=f"{scope}.health_check_unknown",
                    message=(
                        f"Target health could not be verified (checker "
                        f"for scope '{scope}' raised {type(exc).__name__})"
                    ),
                )
            ],
            checked_detail=(
                f"health check for scope '{scope}' FAILED — condition unknown"
            ),
        )


# ---------------------------------------------------------------------------
# Pure helpers (separated so they're trivially testable without async)
# ---------------------------------------------------------------------------


_NODE_BLOCKING_CONDITIONS = {
    "DiskPressure": "node.disk_pressure",
    "MemoryPressure": "node.memory_pressure",
    "NetworkUnavailable": "node.network_unavailable",
    "PIDPressure": "node.pid_pressure",
}


def _build_node_report(
    target: dict, conditions: list[dict] | str | None
) -> HealthReport:
    """Translate kubectl ``status.conditions`` array into a HealthReport.

    ``conditions`` shape::

        [{"type": "DiskPressure", "status": "True",
          "lastTransitionTime": "2026-02-08T12:34:56Z", ...}, ...]

    ``None`` means the query FAILED — see the unknown branch below.
    ``_NODE_NOT_FOUND`` means the node does not exist — a hard BLOCK,
    symmetric with the pod face's ``resource_not_found``.
    """
    if conditions == _NODE_NOT_FOUND:
        # R69: a non-existent node is a real BLOCK, not an "unknown". The
        # pod face already blocks on ``pod.not_found``; before R69 the node
        # face collapsed not-found into ``None`` → WARN (blocking=False),
        # so "node missing" and "pod missing" got opposite verdicts.
        names = target.get("names", [])
        name = names[0] if names else "unknown"
        return HealthReport(
            target=target,
            overall=HealthSeverity.BLOCK,
            issues=[HealthIssue(
                severity=HealthSeverity.BLOCK,
                code="node.not_found",
                message=f"Node '{name}' not found in the cluster",
            )],
            checked_detail=f"Node '{name}' not found",
        )
    if conditions is None:
        # R66: the query failed — the outcome-UNKNOWN third state, not
        # a clean bill of health. Deliberately WARN, not BLOCK: the
        # historical behaviour is fail-open (a broken health check must
        # not block the inject); R66 keeps that but makes the fail-open
        # TRANSPARENT — the confirm card / plan prompt now read
        # "could not verify" instead of a fake "Node Ready".
        return HealthReport(
            target=target,
            overall=HealthSeverity.WARN,
            issues=[
                HealthIssue(
                    severity=HealthSeverity.WARN,
                    code="node.health_check_unknown",
                    message=(
                        "Node health could not be verified (kubectl "
                        "query failed) — condition unknown"
                    ),
                )
            ],
            checked_detail="Node health NOT verified (kubectl query failed)",
        )
    issues: list[HealthIssue] = []
    for cond in conditions or []:
        ctype = cond.get("type", "")
        cstatus = cond.get("status", "")

        # Ready condition: status != "True" means node is unreachable
        if ctype == "Ready" and cstatus != "True":
            duration = _format_condition_duration(
                cond.get("lastTransitionTime", "")
            )
            issues.append(
                HealthIssue(
                    severity=HealthSeverity.BLOCK,
                    code="node.not_ready",
                    message=f"Node is NotReady for {duration or 'unknown duration'}",
                    duration_hint=duration,
                )
            )
            continue

        # Pressure conditions: status == "True" means active pressure
        if cstatus != "True":
            continue
        code = _NODE_BLOCKING_CONDITIONS.get(ctype)
        if not code:
            continue
        duration = _format_condition_duration(
            cond.get("lastTransitionTime", "")
        )
        issues.append(
            HealthIssue(
                severity=HealthSeverity.BLOCK,
                code=code,
                message=f"Node has {ctype}=True for {duration or 'unknown duration'}",
                duration_hint=duration,
            )
        )

    overall = HealthSeverity.BLOCK if issues else HealthSeverity.OK
    return HealthReport(target=target, overall=overall, issues=issues)


def _build_pod_report(target: dict, status: dict) -> HealthReport:
    """Translate kubectl ``status`` block into a HealthReport.

    ``status`` shape::

        {"phase": "Running", "conditions": [...], "reason": "Evicted"?}
    """
    _error = status.get("_error")
    if _error == "namespace_not_found":
        ns = target.get("namespace", "unknown")
        return HealthReport(
            target=target,
            overall=HealthSeverity.BLOCK,
            issues=[HealthIssue(
                severity=HealthSeverity.BLOCK,
                code="pod.namespace_not_found",
                message=f"Namespace '{ns}' does not exist in the cluster",
            )],
            checked_detail=f"Namespace '{ns}' not found",
        )
    if _error == "resource_not_found":
        names = target.get("names", [])
        name = names[0] if names else "unknown"
        ns = target.get("namespace", "default")
        return HealthReport(
            target=target,
            overall=HealthSeverity.BLOCK,
            issues=[HealthIssue(
                severity=HealthSeverity.BLOCK,
                code="pod.not_found",
                message=f"Pod '{name}' not found in namespace '{ns}'",
            )],
            checked_detail=f"Pod '{name}' not found in '{ns}'",
        )
    if _error == "query_failed":
        # R66: the query failed — the outcome-UNKNOWN third state, not
        # a clean bill of health. Same transparent-fail-open ruling as
        # the node face: WARN (visible, never blocking) so the report
        # no longer implies "pod is fine" when nothing was read.
        return HealthReport(
            target=target,
            overall=HealthSeverity.WARN,
            issues=[
                HealthIssue(
                    severity=HealthSeverity.WARN,
                    code="pod.health_check_unknown",
                    message=(
                        "Pod health could not be verified (kubectl "
                        "query failed) — condition unknown"
                    ),
                )
            ],
            checked_detail="Pod health NOT verified (kubectl query failed)",
        )

    issues: list[HealthIssue] = []
    phase = status.get("phase", "")
    reason = status.get("reason", "")

    if phase in {"Pending", "Failed", "Unknown"}:
        severity = HealthSeverity.BLOCK if phase != "Pending" else HealthSeverity.WARN
        issues.append(
            HealthIssue(
                severity=severity,
                code=f"pod.phase.{phase.lower()}",
                message=f"Pod phase is {phase}"
                + (f" (reason: {reason})" if reason else ""),
            )
        )

    if reason in {"Evicted", "CrashLoopBackOff", "ImagePullBackOff"}:
        issues.append(
            HealthIssue(
                severity=HealthSeverity.BLOCK,
                code=f"pod.reason.{reason.lower()}",
                message=f"Pod reason: {reason}",
            )
        )

    if issues:
        # Aggregate severity = max
        overall = HealthSeverity.BLOCK if any(
            i.severity == HealthSeverity.BLOCK for i in issues
        ) else HealthSeverity.WARN
    else:
        overall = HealthSeverity.OK

    return HealthReport(target=target, overall=overall, issues=issues)


def _format_condition_duration(iso_timestamp: str) -> str:
    """Format ``2026-02-08T12:34:56Z`` → ``"103d"`` style hint.

    Best effort — returns empty string on parse failure (caller
    handles missing duration gracefully).
    """
    if not iso_timestamp:
        return ""
    from datetime import datetime, timezone

    try:
        ts = iso_timestamp.replace("Z", "+00:00")
        then = datetime.fromisoformat(ts)
        now = datetime.now(timezone.utc)
        delta = now - then
        days = delta.days
        if days >= 1:
            return f"{days}d"
        hours = delta.total_seconds() // 3600
        if hours >= 1:
            return f"{int(hours)}h"
        return f"{int(delta.total_seconds() // 60)}m"
    except (ValueError, AttributeError):
        return ""


# ---------------------------------------------------------------------------
# Async stubs — patched by integration layer / mocked by tests
# ---------------------------------------------------------------------------


# R69 sentinel: ``_query_node_conditions`` returns this (a str, distinct
# from both ``None`` = query-failed and ``[]`` = verified-no-conditions)
# when kubectl reports the node does not exist. ``_build_node_report``
# turns it into a BLOCK, symmetric with the pod face's ``resource_not_found``.
_NODE_NOT_FOUND = "node_not_found"


async def _query_node_conditions(
    node_name: str, kubeconfig: str
) -> list[dict] | str | None:
    """Real impl: ``kubectl get node {name} -o json``, return
    ``.status.conditions``.

    R66: a FAILED query no longer collapses into ``[]`` — an empty
    list is a VERIFIED "queried fine, no conditions", byte-identical
    to a healthy node, which let the report layer stamp a fake
    "Node Ready, no DiskPressure..." even when nothing was read. Every
    failure mode (transport, RBAC, timeout, non-JSON) returns ``None``
    instead — the distinguishable "could not verify" marker the report
    layer turns into an explicit unknown WARN. Never raises (a broken
    health check must not crash the inject pipeline); the information
    is now CARRIED, not discarded.

    R69: runs through ``query_kubectl`` (the internal read entrypoint,
    tri-state ``.ok``) instead of ``_kubectl_impl`` (the LLM-presentation
    face that decorates output with hints), and distinguishes a genuinely
    missing node (returns ``_NODE_NOT_FOUND`` → BLOCK) from a failed query
    (returns ``None`` → WARN unknown).
    """
    if not node_name:
        return None
    from chaos_agent.tools.kubectl_cli import query_kubectl

    outcome = await query_kubectl(
        ["node", node_name, "-o", "json"],
        kubeconfig or "",
        log_name="_query_node_conditions",
    )
    if not outcome.ok:
        # "not found" ⇒ the node genuinely does not exist (a real BLOCK —
        # you cannot inject on a node that is not in the cluster). Any
        # other failure ⇒ could not verify (fail-open WARN). The old
        # ``_kubectl_impl`` face could only guess via ``s.startswith("Error")``
        # on decorated text.
        if "not found" in outcome.error.lower():
            return _NODE_NOT_FOUND
        return None
    import json as _json
    try:
        data = _json.loads(outcome.text)
    except _json.JSONDecodeError:
        logger.debug("_query_node_conditions: non-JSON output for %s", node_name)
        return None
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, dict):
        return None
    conds = status.get("conditions")
    if not isinstance(conds, list):
        # Missing / malformed conditions — could not verify, which is
        # NOT the same as "verified healthy".
        return None
    return conds


async def _query_blade_agent_on_node(
    node_name: str, kubeconfig: str
) -> str:
    """Three-state ChaosBlade tool-pod presence on ``node_name``.

    Returns one of ``TOOL_POD_PRESENT`` / ``TOOL_POD_ABSENT`` /
    ``TOOL_POD_UNKNOWN`` (see ``pod_discovery``).

    R69: this used to be a hand-rolled ``kubectl get pod -n chaosblade
    -l app=chaosblade-tool`` + ``len(raw.strip()) > 0`` check running
    through the LLM-presentation entrypoint (``_kubectl_impl``). Two
    defects made it lie: (1) the empty-selector hint appended to ``raw``
    turned "no match" into non-empty text, so it ALWAYS returned True;
    (2) it disagreed with the authoritative execution-side discovery
    (``pod_discovery.discover_tool_pod_on_node``) in 8 of 12 scenarios —
    the report stamped "chaosblade-tool online" while the injector could
    find no carrier. Now it delegates to that single authority and only
    the three-state verdict is consumed here.
    """
    from chaos_agent.tools.pod_discovery import discover_tool_pod_state_on_node

    state, _found = await discover_tool_pod_state_on_node(node_name, kubeconfig)
    return state


async def _resolve_pod_names(target: dict, kubeconfig: str) -> list[str]:
    """Resolve real pod names from a target dict.

    When the user selects pods via labels (e.g. app=accounting), ``names``
    contains the app/deployment name, not actual pod names.  This function
    resolves to the list of real pod names that kubectl can query.

    Resolution order:
      1. ``labels`` non-empty → ``kubectl get pod -l … -n …`` → all pod names
      2. ``names`` as-is (assumed to be real pod names)
      3. empty list
    """
    from chaos_agent.utils.coerce import coerce_to_list

    labels = target.get("labels") or {}
    namespace = target.get("namespace", "default")
    names = coerce_to_list(target.get("names"), context="_resolve_pod_names")

    if labels:
        label_selector = ",".join(f"{k}={v}" for k, v in labels.items())
        from chaos_agent.tools.kubectl_cli import query_kubectl
        outcome = await query_kubectl(
            [
                "pod", "-l", label_selector, "-n", namespace,
                "--field-selector=status.phase=Running",
                "-o", "jsonpath={.items[*].metadata.name}",
            ],
            kubeconfig or "",
            log_name="_resolve_pod_names",
        )
        # R69: gate on ``.ok`` (tri-state) instead of sniffing the decorated
        # ``_kubectl_impl`` string. The old path split ANY non-empty text —
        # including an "Error from server ..." failure string — on whitespace
        # and returned the tokens as pod names (so "Error" became a pod).
        # ``.text`` is the undecorated payload and is empty unless ok.
        if outcome.ok:
            pod_names = [n for n in outcome.text.split() if n]
            if pod_names:
                return pod_names

    return list(names)


async def _query_pod_status(
    pod_name: str, namespace: str, kubeconfig: str
) -> dict:
    """Real impl: ``kubectl get pod -n {ns} {name} -o json``, return
    ``.status``.

    Same defensive pattern as ``_query_node_conditions`` — never
    raises. R66: a generic failure no longer collapses into ``{}``
    (byte-identical to a healthy empty status); it returns
    ``{"_error": "query_failed"}`` through the SAME ``_error`` channel
    that already carries the two not-found pre-classifications — the
    report layer turns it into an explicit unknown WARN.
    """
    if not pod_name:
        return {"_error": "query_failed"}
    from chaos_agent.tools.kubectl_cli import query_kubectl

    args = ["pod", pod_name]
    if namespace:
        args += ["-n", namespace]
    args += ["-o", "json"]

    outcome = await query_kubectl(
        args, kubeconfig or "", log_name="_query_pod_status",
    )
    if not outcome.ok:
        # R69: classify from ``.error`` (the merged stdout+stderr diagnosis
        # produced by query_kubectl) instead of sniffing decorated
        # ``_kubectl_impl`` text with ``s.startswith("Error")``. Under the
        # wiz relay the NotFound text can land in stdout — the dual-stream
        # merge in query_kubectl keeps it in ``.error`` either way.
        err = outcome.error.lower()
        if "not found" in err and "namespace" in err:
            return {"_error": "namespace_not_found", "phase": "", "reason": ""}
        if "not found" in err:
            return {"_error": "resource_not_found", "phase": "", "reason": ""}
        return {"_error": "query_failed"}
    import json as _json
    try:
        data = _json.loads(outcome.text)
    except _json.JSONDecodeError:
        return {"_error": "query_failed"}
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, dict):
        return {"_error": "query_failed"}
    # Surface the top-level reason if any — used by _build_pod_report
    # to detect Evicted / CrashLoopBackOff at a glance without walking
    # the conditions array.
    out = {
        "phase": status.get("phase", ""),
        "reason": status.get("reason", "")
        or data.get("metadata", {}).get("annotations", {}).get(
            "kubernetes.io/eviction-reason", ""
        ),
        "conditions": status.get("conditions") or [],
    }
    # Inspect container statuses for CrashLoopBackOff / ImagePullBackOff
    # which surface in waiting.reason rather than top-level reason.
    for cs in status.get("containerStatuses") or []:
        waiting = (cs.get("state") or {}).get("waiting") or {}
        wreason = waiting.get("reason", "")
        if wreason in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}:
            out["reason"] = wreason
            break
    return out
